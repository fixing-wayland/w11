"""xrandr 1.5.4 option parsing + dispatch.

Byte-parity notes, against xrandr 1.5.4:
- strict `--long` options plus short -d -s -r -v -x -y -o -q; `-help` and
  `--help` both work; no other single-dash long forms.
- parse errors: stderr `xrandr: <msg>` + `Try 'xrandr --help' for more
  information.`, exit 1. Fatals: `xrandr: <msg>`, exit 1. A bad --output NAME
  is only a bare `warning: output NAME not found; ignoring` (exit 0).
- bare invocation / -q / --verbose query; --verbose with a 1.0-only set adds
  the RandR 1.0 table; mode-store operations with no other action exit
  silently like the real thing.
- wxrandr's own options stay out of the usage text, so `--help` is still
  xrandr's bytes: `--persistent`, `--backend NAME`, `--print-backend`,
  `--backends`, `--unsafe-gnome-overlap`, its one modifier
  `--unsafe-gnome-overlap-unmeasured MAJOR`, and the three
  `--gnome-overlap-{allow,forget,status}` that manage its agreement.
"""

import contextlib
import io
import math
import os
import re
import sys

from w11common import distro, passthrough, stdio
from wxrandr import core, gnome_overlap
from wxrandr.core import ArgErr, Fatal, Stanza

_PERSIST_CONFLICT = (
    "--persistent and %s cannot be used together: %s applies a layout GNOME's "
    "own validator refuses, and the file --persistent saves to is read back "
    "through that same validator -- one entry it refuses discards the whole "
    "file, every other saved monitor arrangement with it, at every boot\n"
    % (gnome_overlap.FLAG, gnome_overlap.FLAG))

USAGE = """usage: xrandr [options]
  where options are:
  --display <display> or -d <display>
  --help
  -o <normal,inverted,left,right,0,1,2,3>
            or --orientation <normal,inverted,left,right,0,1,2,3>
  -q        or --query
  -s <size>/<width>x<height> or --size <size>/<width>x<height>
  -r <rate> or --rate <rate> or --refresh <rate>
  -v        or --version
  -x        (reflect in x)
  -y        (reflect in y)
  --screen <screen>
  --verbose
  --current
  --dryrun
  --nograb
  --prop or --properties
  --fb <width>x<height>
  --fbmm <width>x<height>
  --dpi <dpi>/<output>
  --output <output>
      --auto
      --mode <mode>
      --preferred
      --pos <x>x<y>
      --rate <rate> or --refresh <rate>
      --reflect normal,x,y,xy
      --rotate normal,inverted,left,right
      --left-of <output>
      --right-of <output>
      --above <output>
      --below <output>
      --same-as <output>
      --set <property> <value>
      --scale <x>[x<y>]
      --scale-from <w>x<h>
      --transform <a>,<b>,<c>,<d>,<e>,<f>,<g>,<h>,<i>
      --filter nearest,bilinear
      --off
      --crtc <crtc>
      --panning <w>x<h>[+<x>+<y>[/<track:w>x<h>+<x>+<y>[/<border:l>/<t>/<r>/<b>]]]
      --gamma <r>[:<g>:<b>]
      --brightness <value>
      --primary
  --noprimary
  --newmode <name> <clock MHz>
            <hdisp> <hsync-start> <hsync-end> <htotal>
            <vdisp> <vsync-start> <vsync-end> <vtotal>
            [flags...]
            Valid flags: +HSync -HSync +VSync -VSync
                         +CSync -CSync CSync Interlace DoubleScan
  --rmmode <name>
  --addmode <output> <name>
  --delmode <output> <name>
  --listproviders
  --setprovideroutputsource <prov-xid> <source-xid>
  --setprovideroffloadsink <prov-xid> <sink-xid>
  --listmonitors
  --listactivemonitors
  --setmonitor <name> {auto|<w>/<mmw>x<h>/<mmh>+<x>+<y>} {none|<output>,<output>,...}
  --delmonitor <name>
"""

_DIRECTION = ("normal", "left", "inverted", "right")


class Opts:
    def __init__(self):
        self.query = False
        self.query_1 = False
        self.verbose = False
        self.dryrun = False
        self.version = False
        self.current = False
        self.props = False
        self.screen = -1
        self.fb = None
        self.fbmm = None
        self.dpi = None             # float or output name
        self.noprimary = False
        self.persistent = False     # --persistent (Mutter: write monitors.xml)
        self.overlap = False        # --unsafe-gnome-overlap (see wxrandr/gnome_overlap.py)
        #: --unsafe-gnome-overlap-unmeasured MAJOR: {"shell_major": N}, or None.
        #: None is the only default there is or can be -- nothing else in this
        #: class, no environment variable and no file sets it, and it is a usage
        #: error without the flag above, so it can never be what turns the
        #: feature on.
        self.overlap_force = None
        #: the three that manage the *agreement* to the above, and change no layout
        self.overlap_allow = False   # --gnome-overlap-allow
        self.overlap_forget = False  # --gnome-overlap-forget
        self.overlap_status = False  # --gnome-overlap-status
        self.backend = None         # --backend NAME (None: auto)
        self.print_backend = False  # --print-backend
        self.list_backends = False  # --backends
        self.global_auto = False
        self.stanzas = []
        self.mode_ops = []         # ("new", name, modeline)/("rm", name)/
        #                            ("add"|"del", output, name)
        self.monitor_op = None     # ("list",)/("listactive",)/("set",...)/
        #                            ("del", name)
        self.providers = False
        # RandR 1.0
        self.size = -1             # index, or (w, h)
        self.rate = -1.0
        self.rot = -1              # index into _DIRECTION
        self.toggle_x = False
        self.toggle_y = False
        # bookkeeping
        self.setit = False
        self.setit_1_2 = False
        self.action = False


def _number(s: str) -> float:
    """A number an option can be given.  Python's float() also takes `nan`, `inf` and anything that overflows to
    one (`1e400`), and none of those is a size, a rate or a pixel clock: left alone they surface much later as
    raw interpreter text (`cannot convert float NaN to integer`) or, worse, get written to the state file as a
    mode line nothing can render."""
    try:
        v = float(s)
    except ValueError:
        raise ArgErr("failed to parse '%s' as a number\n" % s)
    if not math.isfinite(v):
        raise ArgErr("failed to parse '%s' as a number\n" % s)
    return v


def parse(argv: list) -> Opts:
    o = Opts()
    cur: Stanza | None = None
    i = 0

    def need(n=1):
        nonlocal i
        if i + n >= len(argv):
            if n == 1:
                raise ArgErr("%s requires an argument\n" % argv[i])
            if n == 3:
                raise ArgErr("%s requires three argument\n" % argv[i])
            raise ArgErr("%s requires two arguments\n" % argv[i])
        i += 1
        return argv[i]

    def per_output(opt):
        if cur is None:
            raise ArgErr("%s must be used after --output\n" % opt)

    while i < len(argv):
        a = argv[i]
        if a in ("-help", "--help"):
            sys.stdout.write(USAGE)
            raise SystemExit(0)
        elif a in ("-d", "--display"):
            v = need()
            # X display syntax (":0") means "the current session" here; a
            # real socket name selects the wayland display to talk to.
            if not v.startswith(":"):
                os.environ["WAYLAND_DISPLAY"] = v
        elif a in ("-v", "--version"):
            o.version = True
            o.action = True
        elif a in ("-q", "--query"):
            o.query = True
        elif a == "--q1":
            o.query_1 = True
        elif a == "--q12":
            pass
        elif a == "--verbose":
            o.verbose = True
        elif a == "--dryrun":
            o.dryrun = True
            o.verbose = True
        elif a == "--current":
            o.current = True
        elif a == "--nograb":
            pass
        elif a in ("--prop", "--properties"):
            o.props = True
        elif a == "--screen":
            v = need()
            o.screen = int(_number(v))
            if o.screen < 0:
                raise ArgErr("--screen argument must be nonnegative\n")
        elif a == "--fb":
            v = need()
            m = re.fullmatch(r"(\d+)x(\d+)", v)
            if not m:
                raise ArgErr("failed to parse '%s' as a framebuffer size\n" % v)
            o.fb = (int(m.group(1)), int(m.group(2)))
            o.setit_1_2 = True
            o.action = True
        elif a == "--fbmm":
            v = need()
            m = re.fullmatch(r"(\d+)x(\d+)", v)
            if not m:
                raise ArgErr("failed to parse '%s' as a physical size\n" % v)
            o.fbmm = (int(m.group(1)), int(m.group(2)))
            o.setit_1_2 = True
            o.action = True
        elif a == "--dpi":
            v = need()
            try:
                o.dpi = float(v)
            except ValueError:
                o.dpi = v  # an output name: dpi from its physical size
            o.setit_1_2 = True
            o.action = True
        elif a in ("-o", "--orientation"):
            v = need()
            if v in ("0", "1", "2", "3"):
                o.rot = int(v)
            elif v in _DIRECTION:
                o.rot = _DIRECTION.index(v)
            else:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            o.setit = True
            o.action = True
        elif a in ("-s", "--size"):
            v = need()
            m = re.fullmatch(r"(\d+)x(\d+)", v)
            if m:
                o.size = (int(m.group(1)), int(m.group(2)))
            else:
                o.size = int(_number(v))
                if o.size < 0:
                    raise ArgErr("--size argument must be nonnegative\n")
            o.setit = True
            o.action = True
        elif a in ("-r", "--rate", "--refresh"):
            v = _number(need())
            o.setit = True
            if cur is not None:
                cur.rate = v
                o.setit_1_2 = True
            else:
                o.rate = v
            o.action = True
        elif a == "-x":
            o.toggle_x = True
            o.setit = True
            o.action = True
        elif a == "-y":
            o.toggle_y = True
            o.setit = True
            o.action = True
        elif a == "--output":
            cur = Stanza(name=need())
            o.stanzas.append(cur)
            o.setit_1_2 = True
            o.action = True
        elif a == "--auto":
            if cur is not None:
                cur.auto = True
            else:
                o.global_auto = True
            o.setit_1_2 = True
            o.action = True
        elif a == "--mode":
            per_output(a)
            cur.mode = need()
        elif a == "--preferred":
            per_output(a)
            cur.preferred = True
        elif a == "--pos":
            per_output(a)
            v = need()
            m = re.fullmatch(r"(-?\d+)x(-?\d+)", v)
            if not m:
                raise ArgErr("failed to parse '%s' as a position\n" % v)
            cur.pos = (int(m.group(1)), int(m.group(2)))
        elif a == "--rotate":
            per_output(a)
            v = need()
            if v not in core.ROTATIONS:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            cur.rotate = v
        elif a == "--reflect":
            per_output(a)
            v = need()
            if v not in core.REFLECTIONS:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            cur.reflect = v
        elif a in ("--left-of", "--right-of", "--above", "--below", "--same-as"):
            per_output(a)
            cur.relation = (a[2:], need())
        elif a == "--set":
            per_output(a)
            if i + 2 >= len(argv):
                raise ArgErr("%s requires two arguments\n" % a)
            cur.props.append((argv[i + 1], argv[i + 2]))
            i += 2
        elif a == "--scale":
            per_output(a)
            v = need()
            m = re.fullmatch(r"([0-9.eE+-]+)x([0-9.eE+-]+)", v)
            try:
                if m:
                    sx, sy = float(m.group(1)), float(m.group(2))
                else:
                    sx = sy = float(v)
            except ValueError:
                raise ArgErr("failed to parse '%s' as a scaling factor\n" % v)
            if not (math.isfinite(sx) and math.isfinite(sy)):
                # before the positivity test: `nan <= 0` is False, so a nan went through and came back out as an
                # "anisotropic scaling nanxnan" warning followed by a truncation failure
                raise ArgErr("failed to parse '%s' as a scaling factor\n" % v)
            if sx <= 0 or sy <= 0:
                raise ArgErr("scaling factors must be positive\n")
            cur.scale = (sx, sy)
        elif a == "--scale-from":
            per_output(a)
            v = need()
            m = re.fullmatch(r"(-?\d+)x(-?\d+)", v)
            if not m:
                raise ArgErr("failed to parse '%s' as a scale-from size\n" % v)
            w, h = int(m.group(1)), int(m.group(2))
            if w < 0 or h < 0:
                raise ArgErr("--scale-from dimensions must be nonnegative\n")
            cur.scale_from = (w, h)
        elif a == "--transform":
            per_output(a)
            v = need()
            if v != "none":
                parts = v.split(",")
                if len(parts) != 9:
                    raise ArgErr("failed to parse '%s' as a transformation\n" % v)
                for p in parts:
                    try:
                        float(p)
                    except ValueError:
                        raise ArgErr("failed to parse '%s' as a " "transformation\n" % v)
            core.warn("--transform is not supported on Wayland; ignoring\n")
        elif a == "--filter":
            per_output(a)
            v = need()
            if v not in ("nearest", "bilinear"):
                raise ArgErr("Bad argument: %s, for a filter\n" % v)
            cur.props.append(("__filter", v))
        elif a == "--off":
            per_output(a)
            cur.off = True
        elif a == "--crtc":
            per_output(a)
            need()  # crtc assignment is meaningless here; accepted, ignored
        elif a == "--panning":
            per_output(a)
            need()
            core.warn("--panning is not supported on Wayland; ignoring\n")
        elif a == "--gamma":
            per_output(a)
            v = need()
            parts = v.split(":")
            try:
                vals = [float(p) for p in parts]
            except ValueError:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            if len(vals) == 1:
                vals = vals * 3
            if len(vals) != 3:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            if not all(math.isfinite(g) for g in vals):
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            if any(g <= 0 for g in vals):
                raise ArgErr("gamma correction factors must be positive\n")
            cur.gamma = tuple(vals)
            o.setit_1_2 = True
        elif a == "--brightness":
            per_output(a)
            v = need()
            try:
                cur.brightness = float(v)
            except ValueError:
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            if not math.isfinite(cur.brightness):
                raise ArgErr("%s: invalid argument '%s'\n" % (a, v))
            o.setit_1_2 = True
        elif a == "--primary":
            per_output(a)
            cur.primary = True
        elif a == "--noprimary":
            o.noprimary = True
            o.setit_1_2 = True
            o.action = True
        elif a == "--persistent":
            # wxrandr extension (not in xrandr's usage): on Mutter apply with
            # method 2 so the layout lands in ~/.config/monitors.xml
            if o.overlap:
                raise ArgErr(_PERSIST_CONFLICT)
            o.persistent = True
        elif a == gnome_overlap.FLAG:
            # wxrandr extension, off by default, and the one flag in this tree
            # that can end a session.  It does nothing unless the layout being
            # applied is one GNOME's own validator refuses; see
            # wxrandr/gnome_overlap.py for what it then does and why there is no
            # confirmation prompt.
            if o.persistent:
                raise ArgErr(_PERSIST_CONFLICT)
            o.overlap = True
        elif a == gnome_overlap.FORCE_FLAG:
            # wxrandr extension, and the only thing in this tree that gets past
            # a refusal.  It widens `--unsafe-gnome-overlap` and never replaces
            # it (see the check after the loop), it takes the GNOME Shell major
            # that is running as a required argument so that a line copied from
            # a forum is refused on anybody else's machine, and it is off unless
            # it is typed.  wxrandr/gnome_overlap.py says what it skips.
            try:
                o.overlap_force = gnome_overlap.parse_force(need())
            except ValueError as e:
                raise ArgErr(str(e))
        elif a in (gnome_overlap.ALLOW_FLAG, gnome_overlap.FORGET_FLAG,
                   gnome_overlap.STATUS_FLAG):
            # wxrandr extension: the agreement to the flag above, given once
            # instead of a paragraph printed every time.  None of the three
            # changes a layout, and none of them turns the flag on.
            setattr(o, {gnome_overlap.ALLOW_FLAG: "overlap_allow",
                        gnome_overlap.FORGET_FLAG: "overlap_forget",
                        gnome_overlap.STATUS_FLAG: "overlap_status"}[a], True)
            o.action = True
        elif a == "--backends":
            # wxrandr extension: one line per backend with its availability
            o.list_backends = True
            o.action = True
        elif a == "--print-backend":
            # wxrandr extension: the chosen backend, then exit
            o.print_backend = True
            o.action = True
        elif a == "--backend" or a.startswith("--backend="):
            # wxrandr extension: force the backend for this invocation
            v = a.split("=", 1)[1] if a.startswith("--backend=") else need()
            if canonical_backend(v) is None:
                raise ArgErr("--backend: invalid argument '%s'; valid: %s\n" % (v, ", ".join(BACKEND_NAMES)))
            o.backend = canonical_backend(v)
        elif a == "--newmode":
            name = need()
            clock = _number(need())
            nums = [int(_number(need())) for _ in range(8)]
            flags = []
            while i + 1 < len(argv) and argv[i + 1].lower() in core.MODE_FLAGS:
                i += 1
                flags.append(argv[i].lower())
            o.mode_ops.append(("new", name, clock, nums, flags))
            o.action = True
        elif a == "--rmmode":
            o.mode_ops.append(("rm", need()))
            o.action = True
        elif a in ("--addmode", "--delmode"):
            if i + 2 >= len(argv):
                raise ArgErr("%s requires two arguments\n" % a)
            o.mode_ops.append((a[2:5], argv[i + 1], argv[i + 2]))
            i += 2
            o.action = True
        elif a == "--listproviders":
            o.providers = True
            o.action = True
        elif a in ("--setprovideroutputsource", "--setprovideroffloadsink"):
            if i + 2 >= len(argv):
                raise ArgErr("%s requires two arguments\n" % a)
            i += 2
            core.warn("%s is not supported on Wayland; ignoring\n" % a)
            o.action = True
        elif a == "--listmonitors":
            o.monitor_op = ("list",)
            o.action = True
        elif a == "--listactivemonitors":
            o.monitor_op = ("listactive",)
            o.action = True
        elif a == "--setmonitor":
            if i + 3 >= len(argv):
                raise ArgErr("%s requires three argument\n" % a)
            i += 3
            core.warn("--setmonitor is not supported on Wayland; ignoring\n")
            o.action = True
        elif a == "--delmonitor":
            o.monitor_op = ("del", need())
            o.action = True
        else:
            raise ArgErr("unrecognized option '%s'\n" % a)
        i += 1
    _check_force(o)
    _check_allow(o)
    return o


#: The same shape as _PERSIST_CONFLICT, and for the same reason: two options on
#: one command line that cannot both mean what they say.
_ALLOW_CLASH = (
    "%s and a layout on one command line cannot both happen: %s runs the checks "
    "and records an agreement, it applies nothing, and the stanzas typed beside "
    "it (%s) were silently dropped.  Record the agreement first, then apply the "
    "layout\n")


def _check_allow(o):
    """`--gnome-overlap-allow` answers for itself and returns; a `--output ...`
    stanza typed with it is therefore a layout that never happens.

    Measured at HEAD: `wxrandr --gnome-overlap-allow --output Virtual-2 --pos
    960x0` exited 0, probed, wrote the agreement and moved nothing, with no
    line anywhere saying the move had been dropped -- which is the command
    somebody types once, believes, and then wonders about.  `--query` and the
    other informational options stay allowed: they say something about a
    session rather than ask for a change to one."""
    if not o.overlap_allow:
        return
    named = [s.name for s in o.stanzas if s.name]
    if named:
        raise ArgErr(_ALLOW_CLASH % (gnome_overlap.ALLOW_FLAG,
                                     gnome_overlap.ALLOW_FLAG, ", ".join(named)))


def _check_force(o):
    """`--unsafe-gnome-overlap-unmeasured` on its own is a usage error, and so
    is pairing it with the agreement.

    Two rules, and both of them are about reachability rather than taste:

    * it is a *modifier*, never an entry point.  `--unsafe-gnome-overlap` stays
      the only option in this program that turns the overlap route on, so there
      is no arrangement of a command line in which forgetting the dangerous flag
      and typing this one does anything at all;
    * `--gnome-overlap-allow` records an agreement to a build the checks have
      just passed on, and a forced run is by definition the one where they did
      not.  Recording one for it would be recording a yes nobody can be held to,
      so the two cannot be typed together.
    """
    if o.overlap_force is None:
        return
    if not o.overlap:
        raise ArgErr("%s only means something together with %s, which is the "
                     "only option that turns this on at all\n"
                     % (gnome_overlap.FORCE_FLAG, gnome_overlap.FLAG))
    if o.overlap_allow:
        raise ArgErr("%s and %s cannot be used together: an agreement names a "
                     "build every check passed on, and forcing is what is done "
                     "when they have not\n"
                     % (gnome_overlap.ALLOW_FLAG, gnome_overlap.FORCE_FLAG))
    if o.dryrun:
        # Measured, on a real GNOME 51: a forced --dryrun ended the session.
        # Forcing selects a description by its size, that description names the
        # library it was built for, GIRepository cannot open that library on
        # another build, and gjs aborts instead of raising -- so gnome-shell
        # dies before anything of ours decides whether to write. A dry run is
        # asked for by somebody being careful, and it cannot be made safe here
        # while the description carries a library name, so it is refused rather
        # than offered and hoped for.
        raise ArgErr("%s cannot be rehearsed with --dryrun: reaching an "
                     "unmeasured build means loading a description built for "
                     "another one, which can end the session before anything "
                     "is decided, dry run or not. Run it for real, on a "
                     "machine you can afford to lose the session on, or add "
                     "the build first (the refusal without %s says how)\n"
                     % (gnome_overlap.FORCE_FLAG, gnome_overlap.FORCE_FLAG))


# -- backends -----------------------------------------------------------------

#: what `--backend` accepts.  Real xrandr has no such option (nor
#: `--print-backend`/`--backends`), so every byte of its own surface --
#: `--help`, the query, the errors -- is untouched by them.
WAYLAND_BACKENDS = ("sway", "hypr", "wlr", "mutter", "cinnamon", "kwin")
BACKEND_NAMES = ("auto", "x11") + WAYLAND_BACKENDS
BACKEND_ALIASES = {"gnome": "mutter", "kde": "kwin",
                   "muffin": "cinnamon", "hyprland": "hypr"}
#: the auto-detection order: a sway/i3 IPC socket, then a Hyprland one, then a
#: compositor advertising kde_output_management_v2, then a session bus owning
#: org.gnome.Mutter.DisplayConfig or Muffin's copy of it -- and wlr as what is
#: left, which is therefore never probed for the decision.
#:
#: hypr sits second for the reason wdotool's own order gives: the wlr path is
#: honest for reading a Hyprland layout and cannot apply one -- the second apply
#: of a session times out at 10 s with nothing changed, identically for
#: `wlr-randr`, and with a second output present even the first one hangs
#: [M recon2/hyprland.md §4, and the same on 0.56.2 in recon2/arch.md].
#: cinnamon sits last of the bus names because Muffin's DisplayConfig is only
#: ever there when Mutter's is not [M recon2/cinnamon.md §2.2].
AUTO_ORDER = ("sway", "hypr", "kwin", "mutter", "cinnamon")
AUTO_FALLBACK = "wlr"
_WLR_IFACE = "zwlr_output_manager_v1"
#: COSMIC's extension to wlr-output-management. It changes no code path -- the
#: token stays `wlr`, and rotation and 1.5x scale matched `cosmic-randr list
#: --kdl` over the plain protocol [M recon2/cosmic.md §3] -- only the name the
#: probe prints, which said `wlroots` on a desktop nobody calls that.
_COSMIC_OUTPUT_IFACE = "zcosmic_output_manager_v1"
#: Muffin's DisplayConfig: the same interface as Mutter's under Cinnamon's own
#: bus name [M recon2/cinnamon.md §2.2, displayconfig-introspect.txt]
CINNAMON_DEST = "org.cinnamon.Muffin.DisplayConfig"

#: options that consume arguments, for the argv look-ahead below.  Kept in
#: step with parse(): everything else consumes none, `--newmode` is special.
_ARITY = {
    "-d": 1, "--display": 1, "--screen": 1, "--fb": 1, "--fbmm": 1,
    "--dpi": 1, "-o": 1, "--orientation": 1, "-s": 1, "--size": 1, "-r": 1,
    "--rate": 1, "--refresh": 1, "--output": 1, "--mode": 1, "--pos": 1,
    "--rotate": 1, "--reflect": 1, "--left-of": 1, "--right-of": 1,
    "--above": 1, "--below": 1, "--same-as": 1, "--scale": 1,
    "--scale-from": 1, "--transform": 1, "--filter": 1, "--crtc": 1,
    "--panning": 1, "--gamma": 1, "--brightness": 1, "--rmmode": 1,
    "--delmonitor": 1, "--backend": 1, gnome_overlap.FORCE_FLAG: 1,
    "--set": 2, "--addmode": 2, "--delmode": 2,
    "--setprovideroutputsource": 2, "--setprovideroffloadsink": 2,
    "--setmonitor": 3,
}


def canonical_backend(value):
    """The canonical spelling of a backend name (aliases resolved), or None
    when it is not one of ours."""
    v = (value or "").strip().lower()
    v = BACKEND_ALIASES.get(v, v)
    return v if v in BACKEND_NAMES else None


#: our own options that modify an *apply* rather than answer for themselves.
#: Real xrandr has neither, so neither may reach it: on an X11 session
#: `--persistent` came back as its `unrecognized option '--persistent'` and
#: exit 1 with the layout unapplied, where the documents say an X11 apply
#: works and simply saves nothing (WXRANDR.md, "Keeping a layout").
PERSISTENT_FLAG = "--persistent"
OWN_APPLY_FLAGS = (PERSISTENT_FLAG, gnome_overlap.FLAG)


def _walk_argv(argv):
    """`(option, how many argv entries it takes)` for each option in a raw argv, walked exactly the way parse()
    walks it -- every option consuming its own arguments, so a *value* that happens to spell one of our options
    (`--output --backend`, `--mode --backends`) is a value here too and can never be mistaken for the flag.
    Never raises: an argv the parser will reject is not this hook's business."""
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        take = 1 + _ARITY.get(a, 0)
        if a == "--newmode":                    # name, clock, 8 numbers, flags
            take = 11
            while i + take < n and argv[i + take].lower() in core.MODE_FLAGS:
                take += 1
        yield a, take
        i += take


def own_flags_in(argv):
    """The options of OWN_APPLY_FLAGS this argv really carries, as a set -- an output *named* `--persistent`
    is not one of them.  main() asks before handing over: one of these on an X11 session is a user asking for
    something the original has never heard of, and the answer has to be ours."""
    return {a for a, _take in _walk_argv(argv) if a in OWN_APPLY_FLAGS}


def scan_backend_argv(argv):
    """`(value of --backend or None, an informational option is present, argv without our own options)`, read
    out of a raw argv *before* anything is parsed -- which is where main() has to decide whether this X11
    session hands over to the real xrandr.  The stripped argv is what the original is then exec'd with:
    `--backend x11` asks for the real xrandr, which has no such option to be handed, and neither does it have
    the two in OWN_APPLY_FLAGS.  A `--backend` with no value at all comes back as `""` -- present, naming
    nothing -- so that the flag's own error is ours to print on every session, not the original's.

    argv is walked by `_walk_argv`, so an output named like one of our options is a value here too.  Never
    raises: an argv the parser will reject is not this hook's business.
    """
    backend = None
    info = False
    rest = []
    i, n = 0, len(argv)
    while i < n:
        a = argv[i]
        if a == "--backend":
            # last one wins, like parse().  A missing value still *is* the flag -- returned as "", which no
            # backend is called -- or an X11 session would hand `--backend` to the original and answer with its
            # `unrecognized option` instead of our own error.
            backend = argv[i + 1] if i + 1 < n else ""
            i += 2
            continue
        if a.startswith("--backend="):
            backend = a.split("=", 1)[1]
            i += 1
            continue
        if a in ("--print-backend", "--backends", gnome_overlap.ALLOW_FLAG,
                 gnome_overlap.FORGET_FLAG, gnome_overlap.STATUS_FLAG):
            # informational or bookkeeping: they answer for themselves everywhere,
            # and handing one to the real xrandr would only get "unrecognized option"
            info = True
        take = 1 + _ARITY.get(a, 0)
        if a == "--newmode":                    # name, clock, 8 numbers, flags
            take = 11
            while i + take < n and argv[i + take].lower() in core.MODE_FLAGS:
                take += 1
        if a not in OWN_APPLY_FLAGS:
            rest.extend(argv[i:i + take])
        i += take
    return backend, info, rest


class Probe:
    """What one backend's availability check found.  `handle` is the live connection the probe opened, which
    Session reuses so a session still opens exactly one; `close()` drops it when nothing wants it."""

    def __init__(self, name, available, reason="", detail="", compositor=None, protocol=None, handle=None):
        self.name = name
        self.available = available
        self.reason = reason            # short, true, only when unavailable
        self.detail = detail            # what makes it available
        self.compositor = compositor
        self.protocol = protocol
        self.handle = handle

    def close(self):
        h, self.handle = self.handle, None
        try:
            if hasattr(h, "close"):
                h.close()
        except Exception:               # a probe never raises, closing least
            pass


def _probe_x11(env):
    try:
        real = passthrough.real_tool("xrandr", env)
    except passthrough.RealToolError as e:
        return Probe("x11", False, str(e).split("\n")[0])
    if real is None:
        # the package name is this box's, not Debian's: Fedora has no
        # x11-xserver-utils and no xorg-x11-server-utils either (the package is
        # called `xrandr`), Arch calls it xorg-xrandr, and a NixOS box was told
        # to run apt [M recon2/fedora.md, arch.md, nixos.md]
        return Probe("x11", False, "no real xrandr on PATH (%s)" % distro.hint("xrandr"))
    return Probe("x11", True, detail=real, compositor="X server (RandR)")


def _probe_sway(verbose=False):
    from w11common import session as wsession
    sock = wsession.find_sway_socket()
    if not sock:
        return Probe("sway", False, "no sway or i3 IPC socket ($SWAYSOCK)")
    p = Probe("sway", True, detail="IPC socket %s" % sock, compositor="sway",
              protocol="sway IPC (i3-ipc)", handle=sock)
    if verbose:
        try:
            ipc = core.SwayIPC(sock)
            try:
                # `i3 4.25.1 (2026-02-06)` or `sway 1.11`: the same GET_VERSION this used to read by hand,
                # now read by the client that also decides the dialect -- on i3 this line said `sway 4.25.1`
                # [M recon2/i3.md §2b].
                p.compositor = ipc.compositor_label()
            finally:
                ipc.close()
        except Exception:
            pass
    return p


def _probe_kwin():
    from w11common import session as wsession
    from wxrandr import kwin as kwin_mod
    conn = kwin_mod.probe()
    if conn is None:
        if wsession.find_wayland_socket() is None:
            return Probe("kwin", False, "no wayland socket")
        return Probe("kwin", False, "the compositor does not advertise " + kwin_mod.MGMT)
    ver = None
    try:
        for iface, v in conn.get_registry().values():
            if iface == kwin_mod.MGMT:
                ver = v
    except Exception:
        pass
    what = kwin_mod.MGMT + ("" if ver is None else " version %d" % ver)
    return Probe("kwin", True, detail=what, compositor="KWin", protocol=what, handle=conn)


def _probe_mutter():
    from w11common import session as wsession
    from wxrandr import mutter as mutter_mod
    bus = mutter_mod.probe()
    if bus is None:
        if not wsession.find_session_bus():
            return Probe("mutter", False, "no session bus")
        return Probe("mutter", False, "%s is not on the session bus" % mutter_mod.DEST)
    return Probe("mutter", True,
                 detail="%s on the session bus" % mutter_mod.DEST,
                 compositor="Mutter", protocol="%s (D-Bus)" % mutter_mod.DEST,
                 handle=bus)


def _probe_wlr(env=None):
    env = os.environ if env is None else env
    from w11common import session as wsession
    try:
        from w11common.wayland_mini import WlConn
        hit = wsession.find_wayland_socket()
        if hit is None:
            return Probe("wlr", False, "no wayland socket")
        conn = WlConn(hit[2])
        conn.sock.settimeout(10.0)
    except Exception:
        return Probe("wlr", False, "cannot connect to the compositor")
    try:
        g = conn.find_global(_WLR_IFACE)
        ifaces = {i for i, _v in conn.registry.values()}
    except Exception:
        g, ifaces = None, set()
    if g is None:
        try:
            conn.close()
        except Exception:
            pass
        return Probe("wlr", False, "the compositor does not advertise " + _WLR_IFACE)
    what = "%s version %d" % (_WLR_IFACE, g[1])
    return Probe("wlr", True, detail=what, compositor=_wlr_name(ifaces, env),
                 protocol=what, handle=conn)


def _wlr_name(ifaces, env) -> str:
    """What to call the compositor behind zwlr_output_manager_v1.

    `wlroots` was the only answer, on desktops nobody calls that: `--print-backend --verbose` said `wlroots` on
    COSMIC [M recon2/cosmic.md §3], on labwc, on Budgie and on Xfce-on-Wayland [M labwc.md, budgie.md,
    xfce-wayland.md]. COSMIC is named from the protocol it adds to the wlr one, which is evidence; everything
    else gets $XDG_CURRENT_DESKTOP appended, which is a hint and never a gate -- it decides no code path, it
    only stops the line from being useless. sway sets the variable too and is already named by its own backend,
    so it is left alone."""
    if _COSMIC_OUTPUT_IFACE in ifaces:
        return "COSMIC (wlr-output-management)"
    xdg = (env.get("XDG_CURRENT_DESKTOP") or "").strip()
    if xdg and xdg.lower() != "sway":
        return "wlroots (XDG_CURRENT_DESKTOP=%s)" % xdg
    return "wlroots"


def _probe_hypr(verbose=False):
    """Hyprland's own IPC. The socket is checked here rather than in wxrandr/hypr.py so that a session without
    one costs no import and no connection, exactly as _probe_sway does."""
    from w11common import session as wsession
    sock = wsession.find_hypr_socket()
    if not sock:
        return Probe("hypr", False,
                     "no Hyprland IPC socket ($HYPRLAND_INSTANCE_SIGNATURE)")
    try:
        from wxrandr import hypr as hypr_mod
    except ImportError:
        # The socket says this really is Hyprland, and `hypr` is second in AUTO_ORDER, so an ImportError here
        # escapes probe_backend() and kills every wxrandr invocation on a Hyprland desktop -- including
        # `--query`, which reads honestly over the wlr floor [M recon2/hyprland.md §4]. A probe never raises;
        # an unbuilt backend is one more unavailable row, and the wlr fallback still answers.
        return Probe("hypr", False, "the hypr display backend is not built into this install")
    return hypr_mod.probe(sock, verbose=verbose)


def _probe_cinnamon():
    """Muffin's DisplayConfig, which is Mutter's interface under Cinnamon's bus name. A Cinnamon session owns
    org.cinnamon.Muffin.DisplayConfig and never org.gnome.Mutter.DisplayConfig [M recon2/cinnamon.md §2.2], so
    this is a separate name on the same bus and not a second flavour of the mutter probe."""
    from w11common import session as wsession
    from wxrandr import mutter as mutter_mod
    bus = mutter_mod.probe(flavor=mutter_mod.MUFFIN)
    if bus is None:
        if not wsession.find_session_bus():
            return Probe("cinnamon", False, "no session bus")
        return Probe("cinnamon", False, "%s is not on the session bus" % CINNAMON_DEST)
    return Probe("cinnamon", True,
                 detail="%s on the session bus" % CINNAMON_DEST,
                 compositor="Muffin", protocol="%s (D-Bus)" % CINNAMON_DEST,
                 handle=bus)


def probe_backend(name, env=None, verbose=False):
    """Whether `name` can be used in this session, and why not when it
    cannot.  Never raises -- this runs during backend selection."""
    env = os.environ if env is None else env
    if name == "x11":
        return _probe_x11(env)
    if name == "sway":
        return _probe_sway(verbose)
    if name == "hypr":
        return _probe_hypr(verbose)
    if name == "kwin":
        return _probe_kwin()
    if name == "mutter":
        return _probe_mutter()
    if name == "cinnamon":
        return _probe_cinnamon()
    if name == "wlr":
        return _probe_wlr(env)
    return Probe(name, False, "unknown backend")


def detect_wayland(probes=None, verbose=False):
    """`(name, probes)` -- the detection order, unchanged.  wlr is the
    fallback and is not probed for the decision: it is what is left."""
    probes = {} if probes is None else probes
    for name in AUTO_ORDER:
        p = probes.get(name) or probe_backend(name, verbose=verbose)
        probes[name] = p
        if p.available:
            return name, probes
    return AUTO_FALLBACK, probes


def resolve_backend(flag=None, env=None):
    """`(name, source, note)` for the two explicit steps of the precedence rule -- `--backend` beats
    `$WXRANDR_BACKEND` beats auto-detection. `name` is None when neither spoke and the caller must detect."""
    env = os.environ if env is None else env
    name = canonical_backend(flag)
    if name is not None and name != "auto":
        return name, "flag", "--backend %s" % name
    raw = env.get("WXRANDR_BACKEND", "")
    name = canonical_backend(raw)
    if name is not None and name != "auto":
        return name, "environment", "WXRANDR_BACKEND=%s" % raw.strip()
    return None, "detection", None


def chosen_backend(flag=None, env=None, verbose=False, probes=None):
    """`(name, source, note, probes)` -- the backend this invocation uses, without touching the layout.
    Detection asks the session kind first: on X11 the answer is `x11`, because that is where main() hands
    over."""
    env = os.environ if env is None else env
    probes = {} if probes is None else probes
    name, source, note = resolve_backend(flag, env)
    if name is None:
        if passthrough.session_kind("xrandr", env) == "x11":
            name = "x11"
        else:
            name, probes = detect_wayland(probes, verbose=verbose)
    return name, source, note, probes


def print_backend_lines(flag=None, env=None, verbose=False):
    """`--print-backend`: the token, machine-readable, on the first line;
    with --verbose the session, the reason, and what is on the other end."""
    env = os.environ if env is None else env
    name, source, note, probes = chosen_backend(flag, env, verbose=verbose)
    lines = [name]
    if verbose:
        p = probes.get(name) or probe_backend(name, env=env, verbose=True)
        probes[name] = p
        lines.append("session: %s" % (passthrough.session_kind("xrandr", env) or "unknown"))
        lines.append("chosen by: %s" % (source if note is None else "%s (%s)" % (source, note)))
        if p.compositor:
            lines.append("compositor: %s" % p.compositor)
        if p.protocol:
            lines.append("protocol: %s" % p.protocol)
        if name == "x11" and p.available:
            lines.append("real xrandr: %s" % p.detail)
        lines.append("available: %s" % ("yes" if p.available else "no (%s)" % p.reason))
    for p in probes.values():
        p.close()
    return lines


def backends_lines(env=None):
    """`--backends`: every backend, its availability in this session, a
    short true reason when it has none, and `*` on the one auto picks."""
    env = os.environ if env is None else env
    probes = {}
    if passthrough.session_kind("xrandr", env) == "x11":
        auto = "x11"
    else:
        auto, probes = detect_wayland(probes)
    lines = []
    for name in AUTO_ORDER + (AUTO_FALLBACK, "x11"):
        p = probes.get(name) or probe_backend(name, env=env)
        probes[name] = p
        # the name column is 8 wide, not 6: `cinnamon` is the longest token
        # `--backend` takes and a ragged table is harder to read than a wide one
        lines.append("%s %-8s  %-11s  %s"
                     % ("*" if name == auto else " ", name,
                        "available" if p.available else "unavailable",
                        p.detail if p.available else p.reason))
    for p in probes.values():
        p.close()
    return lines


def _do_backend_info(opts) -> int:
    """`--print-backend` / `--backends`: answer and exit 0, having touched
    no layout (and, on an X11 session, having handed nothing over)."""
    if opts.print_backend:
        for line in print_backend_lines(opts.backend, verbose=opts.verbose):
            print(line)
    if opts.list_backends:
        for line in backends_lines():
            print(line)
    return 0


# -- the agreement to --unsafe-gnome-overlap ---------------------------------
#
# Three options that change no layout and turn nothing on.  They exist because
# the paragraph `--unsafe-gnome-overlap` prints is right the first time and
# noise the fiftieth, and a warning that is noise is not read.  What they do
# *not* do is skip anything: the agreement is consulted for what to print, never
# for whether to check, and wxrandr/gnome_overlap.py says so at more length.


def _do_overlap_forget() -> int:
    """`--gnome-overlap-forget`: withdraw the agreement, and say which file went.

    It builds no Session and opens no socket, on purpose and not by accident:
    the moment somebody most needs to withdraw this is from a text console, with
    a session that will not start, and a withdrawal that needs a compositor
    would be no use there."""
    path, removed = gnome_overlap.forget_consent()
    print("withdrawn: %s removed" % path if removed else
          "nothing to withdraw: no agreement was recorded (%s)" % path)
    return 0


def overlap_status_lines(sess, unavailable=None) -> list:
    """`--gnome-overlap-status`: the token on the first line, machine-readable
    like `--print-backend`'s, then what is behind it.

    `agreed` -- an overlapping layout applies here, quietly.
    `available` -- it applies here, and asks first.
    `unavailable` -- it does not apply here, and why.  `unavailable` is also the
    answer when there was no session to ask, which is why that reason comes in
    as an argument: the recorded agreement is a file, and reporting what is in
    it must not need a compositor.

    Nothing in here reads a byte out of gnome-shell: the Shell's version is its
    own public property and the extension's presence is a bus name.  A status
    query must be cheap enough for a GUI to run at startup, and running the
    private-memory checks to answer "would this work?" would be the wrong shape
    entirely."""
    rec = gnome_overlap.load_consent()
    lines = []
    if unavailable:
        lines += ["unavailable", "reason: %s" % unavailable]
    elif sess.backend != "mutter":
        # one sentence for the three callers that have to say it (here, --gnome-overlap-allow, the flag
        # itself): on Cinnamon "places overlapping monitors without any of this" is false -- Muffin carries
        # Mutter's validator with Mutter's own strings and refuses exactly the layout the flag exists for
        # [M recon2/cinnamon.md §2.2]
        lines += ["unavailable", "reason: %s" % gnome_overlap.not_gnome_reason(sess.backend)]
    else:
        version, why = sess.impl.overlap_available()
        lines.append("")                        # the token, filled in below
        lines.append("shell: %s" % (version or "unknown"))
        if why:
            lines[0] = "unavailable"
            # One key per line is the contract, so a multi-line reason folds
            # onto one -- and folding has to collapse the indentation along
            # with the newline it belonged to, or a reason whose next line was
            # an indented command reads "... install it with<five spaces>sh
            # gnome/install-overlap.sh and log out ...", which is what
            # `--gnome-overlap-status` printed on 26.04.
            lines.append("reason: %s" % " ".join(why.split()))
        else:
            lines.append("extension: running")
            covered, no = gnome_overlap.consent_covers(rec, version)
            lines[0] = "agreed" if covered else "available"
            if not covered:
                lines.append("asks first: %s" % no)
    if rec:
        lines.append("agreed on: %s" % rec.get("agreed"))
        lines.append("agreed for: %s" % gnome_overlap.describe_build(rec))
        lines.append("agreed by: %s" % (rec.get("how") or "?"))
    lines.append("file: %s" % gnome_overlap.consent_path())
    return lines


def _do_overlap_status(opts) -> int:
    """Answer, always.  The session is built here rather than in `_run_session`
    so that a machine with no compositor -- a text console, a broken session, an
    X11 box -- gets `unavailable` and the recorded agreement read back to it,
    instead of xrandr's "Can't open display".

    Both ways a session can fail to build are caught, because they are not the
    same one: a backend that is named but not there raises `Fatal`, while no
    compositor at all leaves through `Session._cant_open()`, which writes
    xrandr's own line and raises `SystemExit`.  That second one is the text
    console this command exists for, so its line is captured and becomes the
    reason rather than being printed over the answer."""
    sess = None
    try:
        said = io.StringIO()
        try:
            with contextlib.redirect_stderr(said):
                sess = Session(opts.backend)
            if said.getvalue():         # a warning on the way up: not ours to eat
                stdio.warn(said.getvalue())
            lines = overlap_status_lines(sess)
        except Fatal as e:
            lines = overlap_status_lines(
                None, unavailable=" ".join(str(e.args[0]).split()))
        except SystemExit:
            lines = overlap_status_lines(
                None, unavailable=" ".join(said.getvalue().split()) or
                "there is no session to ask")
        for line in lines:
            print(line)
    finally:
        if sess is not None:
            sess.close()
    return 0


def _do_overlap_allow(sess, dryrun=False) -> int:
    """`--gnome-overlap-allow`: run every check, show what passed, and record an
    agreement against the build they passed on.

    The probe comes first and the record second, and never the other way round:
    an agreement is an agreement to a *measured* risk, so there must be no way
    to record one for a compositor the checks have not just run on.

    `--dryrun` stops between the two.  Everywhere else in this program a dry run
    changes nothing on disk, and this is the one command whose whole effect is a
    file; measured at HEAD, `wxrandr --dryrun --gnome-overlap-allow` wrote the
    agreement and said "recorded in ...", which is a dry run that recorded a
    consent."""
    flag = gnome_overlap.ALLOW_FLAG
    if sess.backend != "mutter":
        raise Fatal("%s: %s -- there is nothing to agree to\n"
                    % (flag, gnome_overlap.not_gnome_reason(sess.backend)))
    reply = sess.impl.overlap_probe()
    if not reply.get("ok"):
        raise Fatal("%s: %s" % (flag, gnome_overlap.refusal_text(reply)))
    for check in reply.get("checks") or []:
        print("check %s: %s" % (check.get("name"), check.get("detail")))
    print(gnome_overlap.notes_text(reply), end="")
    facts = gnome_overlap.facts(reply)
    if not facts["shell"] or facts["struct_size"] is None:
        raise Fatal("%s: the extension did not say which build it verified "
                    "(no MetaMonitorsConfig size in its answer), so there is "
                    "nothing to record an agreement against\n" % flag)
    print("")
    print(gnome_overlap.agreement_text(facts), end="")
    if dryrun:
        print("dryrun: nothing was recorded (%s would be written without it)"
              % gnome_overlap.consent_path())
        return 0
    print("recorded in %s" % gnome_overlap.save_consent(facts, "wxrandr " + flag))
    return 0


class Session:
    """Compositor connection bundle: chosen backend + state file + wlr
    enrichment. Built lazily — --help/parse errors never touch a socket.

    Backends: sway (IPC socket present), kwin (KDE Plasma: the compositor
    advertises kde_output_management_v2 — no portal, no polkit), mutter
    (GNOME: the session bus owns org.gnome.Mutter.DisplayConfig — no
    extension, no root), wlr (anything with zwlr_output_management).
    `--backend NAME` beats WXRANDR_BACKEND=sway|wlr|kwin (alias kde)|mutter
    (alias gnome), which beats detection; the order there is unchanged --
    sway, then KWin, then GNOME, then wlroots, which is the fallback and is
    not probed for it. KWin's probe *is* the Wayland connection the backend
    then keeps, so a KDE session still opens exactly one, and a backend
    forced with the flag is probed the same way: an unavailable one is one
    fatal line naming what was missing, never a silent fallback.  The
    environment variable keeps its older behaviour (no pre-check: whatever
    the backend itself says when it cannot connect)."""

    BACKENDS = WAYLAND_BACKENDS

    # the backend object close() drops, as a class default: __init__ always rebinds it, and a Session built any
    # other way (the backend tests stub __init__ with their own fake) is still closeable
    impl = None
    probes: dict = {}
    #: --unsafe-gnome-overlap, and never anything else: no environment variable
    #: sets it, no default turns it on, and a Session built any other way (the
    #: backend tests stub __init__) still reads False here.
    overlap = False
    #: --unsafe-gnome-overlap-unmeasured, likewise: None is the default here,
    #: in Opts, and in every method that takes it, so forgetting to pass it
    #: anywhere makes a run safer rather than more dangerous.
    overlap_force = None

    def __init__(self, forced=None):
        from w11common import session as wsession
        name, self.backend_source, self.backend_note = resolve_backend(forced)
        probes = {}
        if name == "x11":
            # unreachable from a command line: main() hands an X11 choice over to the real xrandr -- asked for
            # by the flag or by the variable -- before a single option is parsed.
            raise Fatal("%s hands over to the real xrandr, which an embedded "
                        "call cannot do\n" % self.backend_note)
        if name is None:
            name, probes = detect_wayland()
        elif self.backend_source == "flag":
            p = probes[name] = probe_backend(name)
            if not p.available:
                p.close()
                raise Fatal("--backend %s is not available in this session: " "%s\n" % (name, p.reason))
        self.backend = name

        def reuse(bname):
            p = probes.get(bname)
            return p.handle if p is not None and p.available else None
        sway_sock = reuse("sway")
        kprobe = reuse("kwin")
        probe = reuse("mutter")
        cprobe = reuse("cinnamon")
        wprobe = reuse("wlr")
        # detection may have opened a connection per backend it tried; only the chosen one is reused, so the
        # rest are closed here rather than left to the garbage collector (which reports them as a
        # ResourceWarning at whatever moment it gets round to them)
        self.probes = probes
        keep = {id(h) for h in (sway_sock, kprobe, probe, cprobe, wprobe) if h is not None}
        for p in probes.values():
            if p.handle is not None and id(p.handle) not in keep:
                p.close()
        self.impl = None
        self.persistent = os.environ.get("WXRANDR_PERSIST", "") not in ("", "0")
        # No environment variable turns this on.  It is a typed flag or nothing.
        self.overlap = False
        self.overlap_force = None
        # OSError as well as Fatal: WlConn's connect() can raise ConnectionRefusedError on a stale-but-present
        # socket, and sway IPC can drop mid-handshake — both must read as "Can't open display", never a
        # traceback.
        if self.backend == "sway":
            try:
                ipc = core.SwayIPC(sway_sock)
            except (Fatal, OSError):
                self._cant_open()
            # the enrichment connection is opened only once the IPC is up: a
            # session without sway has already left through _cant_open()
            self.impl = core.SwayBackend(ipc, core.wlr_snapshot_safe())
        elif self.backend == "kwin":
            from wxrandr import kwin as kwin_mod
            if kprobe is None and wsession.find_wayland_socket() is None:
                self._cant_open()
            try:
                # a socket without kde_output_management_v2 raises Fatal with a one-line explanation; an
                # unusable one is "Can't open display", like everywhere else
                self.impl = kwin_mod.KwinOutputs(conn=kprobe)
            except (OSError, RuntimeError, ValueError):
                self._cant_open()
        elif self.backend == "mutter":
            from wxrandr import mutter as mutter_mod
            try:
                # no bus at all -> "Can't open display"; a bus without
                # DisplayConfig raises Fatal with a one-line explanation
                self.impl = mutter_mod.MutterOutputs(bus=probe)
            except (mutter_mod.DBusError, OSError, ValueError):
                self._cant_open()
        elif self.backend == "hypr":
            from wxrandr import hypr as hypr_mod
            # the probe's HyprIPC, so the socket path is found once per run; it holds no connection, so
            # reusing it costs nothing and closing it twice is safe. No arm for a missing socket: HyprIPC
            # opens nothing here and the probe has already refused a session that has none, by name.
            self.impl = hypr_mod.HyprOutputs(ipc=reuse("hypr"))
        elif self.backend == "cinnamon":
            from wxrandr import mutter as mutter_mod
            try:
                # Muffin's DisplayConfig is Mutter's interface under Cinnamon's three names, so this is the
                # mutter arm above with the flavour swapped -- including the connection the probe opened,
                # which `keep` holds for exactly this. Never a fall-through to WlrOutputs: Muffin has no wlr
                # output protocol at all, so answering as `wlr` here would be a wrong answer, not a missing
                # one. Measured live on resolute-cinnamon-wayland (Cinnamon 6.4.13 / muffin 6.4.1).
                self.impl = mutter_mod.MutterOutputs(bus=cprobe, flavor=mutter_mod.MUFFIN)
            except (mutter_mod.DBusError, OSError, ValueError):
                self._cant_open()
        else:
            try:
                self.impl = core.WlrOutputs(conn=wprobe)
            except (Fatal, OSError):
                self._cant_open()
        # state is keyed by the compositor's wayland socket so all backends share one primary/custom-mode store
        # per session; sway is the only one that can run without such a socket, and it keys by its own
        hit = wsession.find_wayland_socket()
        key = hit[2] if hit else getattr(self.impl, "sockpath", "?")
        self.state = core.State(key)

    @staticmethod
    def _cant_open():
        sys.stderr.write("Can't open display %s\n" % os.environ.get("WAYLAND_DISPLAY", ""))
        raise SystemExit(1)

    def snapshot(self):
        return self.impl.snapshot(self.state)

    def dims(self, t) -> tuple:
        """Pending logical size of an enabled target in the backend's own coordinate space (Mutter and KWin
        round, and Mutter may not scale at all; wlroots truncates)."""
        return self.impl.predicted_dims(t, self.state)

    def positions(self, targets, dims) -> dict:
        """Pending positions the way the backend will lay them out: xrandr's set_positions everywhere; on Mutter
        (no holes allowed) also the follow-your-neighbour shift the apply performs, so the plan, --fb and
        screen-size checks see the real layout (the warnings are printed once, by the apply/verify)."""
        pos = core.resolve_positions(targets, dims)
        # the impl, not the token: a Cinnamon session is a Muffin-flavoured MutterOutputs under the token
        # `cinnamon` and has the same no-holes rule.  Measured on the three-head fixture of
        # tests/test_wxrandr_cinnamon.py: without this the `--dryrun --verbose` plan omits the crtc lines
        # for the neighbours the apply shifts and promises `screen 0: 5760x1600` where the run leaves 5520
        if getattr(self.impl, "flavor", None) is not None:
            from wxrandr import mutter as mutter_mod
            moved = {n for n, _p, _via in mutter_mod.keep_adjacent(targets, dims, pos)}
            for t in targets:
                if t.name in moved:
                    t.changed = True   # its crtc line belongs in the plan
        for t in targets:
            # so does a shift resolve_positions itself made: it normalises the whole layout to min x = min y =
            # 0, so a --pos -400x-400 on one output really does move every other one, and a --dryrun that
            # printed a crtc line only for the outputs the command names under-reported what the run would do
            if t.enabled and t.name in pos and pos[t.name] != (t.output.x, t.output.y):
                t.changed = True
        return pos

    def close(self):
        """Drop the compositor connections.  Every backend has a close() and nothing called any of them, so
        every run handed its socket (and, on sway, a second one for the wlr enrichment) to the garbage collector
        -- which reports it as a ResourceWarning whenever it gets round to it.  Idempotent: the probe objects
        hold the same handles and swallow a second close."""
        if self.impl is not None:
            try:
                self.impl.close()
            except OSError:
                pass
        self.impl = None
        for probe in self.probes.values():
            probe.close()

    def apply(self, targets):
        # --unsafe-gnome-overlap is not an apply of its own: it is asked first,
        # and answers None for every layout GNOME would accept -- which is every
        # layout but the one the flag exists for.  The ordinary path below is
        # what actually runs unless a user typed that flag AND asked for
        # something Mutter refuses.
        if self.overlap and self.backend == "mutter":
            fresh = self.impl.apply_overlap(self.state, targets, self.overlap_force)
            if fresh is not None:
                return fresh
        return self.impl.apply(self.state, targets, self.persistent)

    @property
    def compositor_name(self):
        return self.impl.name


# -- action helpers -----------------------------------------------------------

def _do_mode_ops(sess: Session, opts: Opts, outputs):
    st = sess.state
    names = {o.name for o in outputs}
    for op in opts.mode_ops:
        if op[0] == "new":
            _, name, clock, nums, flags = op
            st.modes()[name] = {"clock": clock, "h": nums[0:4], "v": nums[4:8], "flags": flags}
        elif op[0] == "rm":
            name = op[1]
            if name not in st.modes():
                raise Fatal('cannot find mode "%s"\n' % name)
            del st.modes()[name]
            for lst in st.addmodes().values():
                if name in lst:
                    lst.remove(name)
        else:  # add / del
            _, out, name = op
            if out not in names:
                raise Fatal('cannot find output "%s"\n' % out)
            if name not in st.modes():
                raise Fatal('cannot find mode "%s"\n' % name)
            lst = st.addmodes().setdefault(out, [])
            if op[0] == "add":
                if name not in lst:
                    lst.append(name)
            else:
                if name in lst:
                    lst.remove(name)
    st.save()


def _dpi_and_mm(opts: Opts, outputs, new_w, new_h):
    """The verbose/dryrun screen line pieces (xrandr main + set_screen_size):
    dpi from the *current* screen height unless --dpi/--fbmm."""
    if opts.fbmm:
        return (25.4 * new_h / opts.fbmm[1] if opts.fbmm[1] else 96.0, opts.fbmm[0], opts.fbmm[1])
    dpi = None
    if isinstance(opts.dpi, float):
        dpi = opts.dpi
    elif isinstance(opts.dpi, str):
        for o in outputs:
            if o.name == opts.dpi and o.active and o.mm_h:
                dpi = 25.4 * o.h / o.mm_h
        if dpi is None:
            core.warn("output %s has no physical size; using 96dpi\n" % opts.dpi)
            dpi = 96.0
    if dpi is None:
        x0, y0, x1, y1 = core.layout_box(outputs)
        cur_h = y1 - y0
        mm_h = core.screen_mm(cur_h)
        dpi = 25.4 * cur_h / mm_h if mm_h else 96.0
    if not math.isfinite(dpi) or dpi <= 0:
        # xrandr reads --dpi with sscanf("%lf") and then divides by it, so 0, a negative one, `nan` and `inf`
        # all get that far; none of them is a resolution a screen line can be printed at.  Fall back to the same
        # 96 the no-physical-size paths use rather than abort -- an unusable --dpi is not worth refusing a whole
        # layout over.
        dpi = 96.0
    return dpi, int(25.4 * new_w / dpi), int(25.4 * new_h / dpi)


def _print_plan(opts: Opts, outputs, targets, dims, pos):
    """The crtc/screen plan lines xrandr prints under --verbose/--dryrun."""
    crtc_of = {}
    n = 0
    for o in outputs:
        if o.active:
            crtc_of[o.name] = n
            n += 1
    for t in targets:
        if t.name not in crtc_of and t.enabled:
            crtc_of[t.name] = n
            n += 1
    for t in targets:
        if not t.changed:
            continue
        was = t.output
        mode_changed = t.enabled and t.mode is not None and (
            was.current is None
            or (t.mode.w, t.mode.h, t.mode.refresh_mhz)
            != (was.current.w, was.current.h, was.current.refresh_mhz))
        if (not t.enabled and was.active) or mode_changed:
            print("crtc %d: disable" % crtc_of.get(t.name, 0))
    xs = [pos[t.name][0] + dims[t.name][0] for t in targets if t.enabled]
    ys = [pos[t.name][1] + dims[t.name][1] for t in targets if t.enabled]
    new_w = max(xs) if xs else 0
    new_h = max(ys) if ys else 0
    if opts.fb:
        new_w, new_h = opts.fb
    dpi, mm_w, mm_h = _dpi_and_mm(opts, outputs, new_w, new_h)
    print("screen 0: %dx%d %dx%d mm %6.2fdpi" % (new_w, new_h, mm_w, mm_h, dpi))
    for t in targets:
        if not t.changed or not t.enabled:
            continue
        mode = t.mode or t.output.current
        if mode is None:
            continue
        x, y = pos.get(t.name, (t.output.x, t.output.y))
        print('crtc %d: %12s %6.2f +%d+%d "%s"'
              % (crtc_of.get(t.name, 0), mode.display_name, mode.refresh_hz,
                 x, y, t.name))


def _check_fb(opts: Opts, targets, dims, pos):
    if not opts.fb:
        return
    fw, fh = opts.fb
    for t in targets:
        if not t.enabled or t.name not in pos:
            continue
        x, y = pos[t.name]
        w, h = dims[t.name]
        if x + w > fw or y + h > fh:
            core.warn("specified screen %dx%d not large enough for output"
                      " %s (%dx%d+%d+%d)\n" % (fw, fh, t.name, w, h, x, y))


def _check_screen_size(opts: Opts, targets, dims, pos):
    """xrandr's set_screen_size bound (xrandr.c:2109): the resolved layout (or an explicit --fb) may not exceed
    maxWidth/maxHeight. Guards against a far-flung --pos/--fb being handed to the compositor (and, on the wlr
    backend, against the out-of-range struct pack it would otherwise trip).

    The other end of the same bound: the Screen line advertises a 16x16 minimum, so an enabled output may not be
    scaled below it either. A big enough --scale/--scale-from truncates the logical size to 0x0 (logical size is
    int(px / scale)), which sway and KWin both accept -- leaving an output that occupies no space and cannot be
    clicked back."""
    for t in targets:
        if not t.enabled or t.name not in dims:
            continue
        w, h = dims[t.name]
        if w < core.MIN_WIDTH or h < core.MIN_HEIGHT:
            raise Fatal("output %s cannot be smaller than %dx%d (desired "
                        "size %dx%d)\n" % (t.name, core.MIN_WIDTH,
                                           core.MIN_HEIGHT, w, h))
    if opts.fb:
        desired_w, desired_h = opts.fb
    else:
        xs = [pos[t.name][0] + dims[t.name][0] for t in targets if t.enabled and t.name in pos]
        ys = [pos[t.name][1] + dims[t.name][1] for t in targets if t.enabled and t.name in pos]
        desired_w = max(xs) if xs else 0
        desired_h = max(ys) if ys else 0
    if desired_w > core.MAX_WIDTH or desired_h > core.MAX_HEIGHT:
        raise Fatal("screen cannot be larger than %dx%d (desired size "
                    "%dx%d)\n" % (core.MAX_WIDTH, core.MAX_HEIGHT,
                                  desired_w, desired_h))


def _apply_gamma(sess: Session, opts: Opts, outputs):
    from wxrandr import gamma as gammamod
    from w11common import session as wsession
    hit = wsession.find_wayland_socket()
    sock = hit[2] if hit else None
    known = {o.name for o in outputs}
    changed = False
    for s in opts.stanzas:
        if s.brightness is None and s.gamma is None:
            continue
        if s.name not in known:
            # build_targets already printed the bare not-found warning and xrandr keeps exit 0 for a typo'd
            # --output; don't spawn a holder against a name the compositor has never heard of.
            continue
        flavor = getattr(sess.impl, "flavor", None)
        if flavor is not None:
            # Neither flavour has zwlr_gamma_control or a DisplayConfig LUT call, so warn and succeed --
            # named by the compositor the user is running (Mutter, Muffin).  Without this the cinnamon
            # token fell through to the wlr gamma path below and died `cannot set gamma: no wayland
            # socket`, a failure about a protocol that session never had.
            core.warn("--brightness/--gamma are not supported on %s "
                      "(no gamma LUT API); ignoring for %s\n" % (flavor.compositor, s.name))
            continue
        if sess.backend == "kwin" and not sess.impl.has_gamma:
            # probed, not assumed: kde-output-management-v2 carries no LUT
            # call and KWin advertises no zwlr_gamma_control_manager_v1 either
            core.warn("--brightness/--gamma are not supported on KWin "
                      "(no gamma LUT API); ignoring for %s\n" % s.name)
            continue
        rec = sess.state.gamma().get(s.name, {})
        brightness = (s.brightness if s.brightness is not None else rec.get("brightness", 1.0))
        gam = (s.gamma if s.gamma is not None else tuple(rec.get("gamma", (1.0, 1.0, 1.0))))
        err = gammamod.set_output_gamma(sess.state, s.name, brightness, gam, wayland_socket=sock)
        changed = True
        if err == "refused":
            sess.state.save()
            raise Fatal("Gamma size is 0.\n")
        if err is not None:
            sess.state.save()
            raise Fatal("cannot set gamma: %s\n" % err)
    if changed:
        sess.state.save()


def _do_setit_1_2(sess: Session, opts: Opts, outputs):
    filter_cmds = []
    for s in opts.stanzas:
        for prop, val in s.props:
            if prop == "__filter":
                if sess.backend == "sway":
                    filter_cmds.append("output %s scale_filter %s" % (
                        s.name, "linear" if val == "bilinear" else val))
                else:
                    core.warn("--filter needs the sway backend; ignoring\n")
            else:
                core.warn("--set %s is not supported on Wayland; ignoring\n" % prop)
    targets = core.build_targets(outputs, opts.stanzas, sess.state, opts.global_auto)
    dims = {t.name: sess.dims(t) for t in targets if t.enabled}
    pos = sess.positions(targets, dims)
    _check_fb(opts, targets, dims, pos)
    _check_screen_size(opts, targets, dims, pos)
    if opts.verbose:
        _print_plan(opts, outputs, targets, dims, pos)
    # --dryrun mutates nothing, the primary included: the two assignments below run first so Mutter's verify
    # sees the primary the real call would send, and the dryrun branch puts this back before it saves.
    primary_before = sess.state.primary
    if opts.noprimary:
        nflavor = getattr(sess.impl, "flavor", None)
        if (nflavor is not None and sess.impl.primary and not any(s.primary for s in opts.stanzas)):
            core.warn("%s requires a primary output; keeping %s\n" % (nflavor.desktop, sess.impl.primary))
        if (sess.backend == "kwin" and sess.impl.primary and not any(s.primary for s in opts.stanzas)):
            # neither set_priority nor set_primary_output has an inverse:
            # KWin's output order always has a first entry
            core.warn("KWin keeps a primary output; keeping %s\n" % sess.impl.primary)
        sess.state.primary = None
    for s in opts.stanzas:
        if s.primary and any(t.name == s.name and t.stanza is s for t in targets):
            sess.state.primary = s.name
    if opts.dryrun:
        if (opts.overlap and sess.backend == "mutter"
                and sess.impl.overlap_dryrun(sess.state, targets, opts.overlap_force)):
            sess.state.primary = primary_before
            sess.state.save()
            return outputs
        # what a verify can promise is the backend's business: Mutter really validates, with method 0, the exact
        # call a real run would make (adjacency, overlap, primary, scales), and a rejection is the same one-line
        # `xrandr: <mutter message>` the apply would give; KWin has no such request and re-runs the plan
        # client-side (mode resolution, the last-output refusal); sway and wlroots have nothing to ask.
        sess.impl.verify(sess.state, targets)
        vflavor = getattr(sess.impl, "flavor", None)
        if vflavor is not None:
            # the verdict goes to stderr: stdout stays xrandr's dryrun bytes.  The word is the backend
            # token off the flavour, so GNOME keeps `mutter verify: ok` byte-identical and Cinnamon --
            # whose method 0 really did go to Muffin -- says `cinnamon verify: ok` instead of nothing
            sys.stderr.write("%s verify: ok\n" % vflavor.name)
        # Nothing was sent, so nothing may be claimed about the compositor -- including the primary: a --dryrun
        # that recorded one would make the next --query name a primary the compositor was never asked for.
        # (Mutter and KWin re-sync this from the compositor in snapshot(), so putting back what the run started
        # with is not the same as clearing it -- their own primary survives, the request does not.)
        sess.state.primary = primary_before
        sess.state.save()
        return outputs
    for cmd in filter_cmds:
        sess.impl.ipc.run(cmd)
    fresh = sess.apply(targets)
    still = {o.name for o in fresh}
    if sess.state.primary and sess.state.primary not in still:
        sess.state.primary = None
    sess.state.save()
    _apply_gamma(sess, opts, outputs)
    return fresh


def _do_1_0(sess: Session, opts: Opts, outputs) -> int:
    """The RandR 1.0 path: -s/-o/-x/-y/global -r against the first output."""
    if not outputs:
        raise Fatal("cannot find preferred mode\n")
    first = outputs[0]
    sizes = core.q1_sizes(outputs)
    if isinstance(opts.size, tuple):
        w, h = opts.size
        idx = next((i for i, (sw, sh, _r) in enumerate(sizes) if (sw, sh) == (w, h)), None)
        if idx is None:
            sys.stderr.write("Size %dx%d not found in available modes\n" % (w, h))
            return 1
    elif opts.size >= 0:
        if opts.size >= len(sizes):
            sys.stderr.write("Size index %d is too large, there are only "
                             "%d sizes\n" % (opts.size, len(sizes)))
            return 1
        idx = opts.size
    else:
        idx = None
        if first.current is not None:
            for i, (sw, sh, _r) in enumerate(sizes):
                if (sw, sh) == (first.current.w, first.current.h):
                    idx = i
        if idx is None:
            idx = 0
    if opts.rate >= 0 and sizes:
        rates = sizes[idx][2]
        if rates and round(opts.rate) not in rates:
            sys.stderr.write("Rate %.2f Hz not available for this size\n" % opts.rate)
            return 1
    cur_rot, cur_refl = core.RANDR_VIEW.get(first.transform, ("normal", "normal"))
    rot = _DIRECTION[opts.rot] if opts.rot >= 0 else cur_rot
    refl = set()
    if "x" in cur_refl:
        refl.add("x")
    if cur_refl in ("y", "xy"):
        refl.add("y")
    if opts.toggle_x:
        refl.symmetric_difference_update("x")
    if opts.toggle_y:
        refl.symmetric_difference_update("y")
    new_refl = ("xy" if refl == {"x", "y"} else "x" if refl == {"x"} else "y" if refl == {"y"} else "normal")
    if opts.query or opts.query_1:
        for line in core.render_q1(outputs, sess.state):
            print(line)
    if opts.query:
        for line in core.render_q1_state(first):
            print(line)
    if opts.verbose and opts.setit:
        print("Setting size to %d, rotation to %s" % (idx, rot))
        refl_word = ("X Axis " if new_refl == "x" else
                     "Y Axis" if new_refl == "y" else
                     "X Axis Y Axis" if new_refl == "xy" else "neither axis")
        print("Setting reflection on %s" % refl_word)
    if not opts.setit or opts.dryrun:
        return 0
    s = Stanza(name=first.name)
    if sizes:
        s.mode = "%dx%d" % (sizes[idx][0], sizes[idx][1])
    if opts.rate >= 0:
        s.rate = opts.rate
    s.rotate = rot
    s.reflect = new_refl
    targets = core.build_targets(outputs, [s], sess.state)
    sess.apply(targets)
    sess.state.save()
    return 0


# -- entry point --------------------------------------------------------------

def _run(argv) -> int:
    opts = parse(argv)
    if opts.print_backend or opts.list_backends:
        # informational and layout-free: they answer even where an action
        # would have been handed over to the real xrandr
        return _do_backend_info(opts)
    if opts.overlap_forget:
        # before any Session: withdrawing has to work with no compositor at all
        return _do_overlap_forget()
    if opts.overlap_status:
        # ...and so does asking what is recorded
        return _do_overlap_status(opts)
    if not opts.action:
        opts.query = True
    if opts.verbose:
        opts.query = True
        if opts.setit and not opts.setit_1_2:
            opts.query_1 = True
    if opts.version:
        print("xrandr program version       " + core.PROGRAM_VERSION)
    sess = Session(opts.backend)
    try:
        return _run_session(sess, opts)
    finally:
        sess.close()


def _run_session(sess: Session, opts: Opts) -> int:
    if opts.persistent:
        sess.persistent = True
    if opts.overlap:
        if sess.backend != "mutter":
            raise Fatal("%s only means anything on GNOME; %s\n"
                        % (gnome_overlap.FLAG, gnome_overlap.not_gnome_reason(sess.backend)))
        sess.overlap = True
        sess.overlap_force = opts.overlap_force
    if opts.screen > 0:
        sys.stderr.write("Invalid screen number %d (display has 1)\n" % opts.screen)
        return 1
    if opts.version:
        print("Server reports RandR version 1.6")
    outputs = sess.snapshot()
    if opts.overlap_allow:
        return _do_overlap_allow(sess, opts.dryrun)
    if opts.mode_ops:
        _do_mode_ops(sess, opts, outputs)
        if not (opts.setit_1_2 or opts.monitor_op or opts.props):
            return 0
        outputs = sess.snapshot()
    if opts.providers:
        for line in core.render_providers(outputs, sess.compositor_name):
            print(line)
        return 0
    if opts.monitor_op:
        if opts.monitor_op[0] in ("list", "listactive"):
            # KWin has a real primary XWayland knows about (measured: its
            # own --listmonitors puts it first), so it lists it first too
            # Muffin has the same real primary, on the same logical-monitor flag this backend already
            # reads and syncs, so the flavour answers for both of them
            primary_first = (getattr(sess.impl, "flavor", None) is not None or sess.backend == "kwin")
            for line in core.render_monitors(outputs, sess.state, primary_first):
                print(line)
            return 0
        if opts.monitor_op[0] == "del":
            print("No monitor named '%s'" % opts.monitor_op[1])
            return 0
    if opts.setit_1_2:
        _do_setit_1_2(sess, opts, outputs)
        # xrandr exits right after apply() — no query follows a 1.2 set, even under --verbose/--dryrun
        # (xrandr.c:3654, oracle capture xrandr2-dryrun-mode).
        return 0
    if opts.setit and not opts.setit_1_2:
        return _do_1_0(sess, opts, outputs)
    if opts.query_1 and not opts.setit:
        for line in core.render_q1(outputs, sess.state):
            print(line)
        for line in core.render_q1_state(outputs[0] if outputs else core.OutputState("none", False)):
            print(line)
        return 0
    if opts.query:
        for line in core.render_query(outputs, sess.state,
                                      verbose=opts.verbose, props=opts.props,
                                      fb=opts.fb):
            print(line)
    return 0


def main(argv=None) -> int:
    stdio.repair_std()          # fd 1 or 2 closed before Python started
    # X11 session: the X server's RandR is authoritative, hand over -- but the handover happens before any
    # parsing, so it has to look ahead for `--backend`: one of our own backends must run our own code whatever
    # the session, `--backend x11` must hand over whatever the session, and `--print-backend`/`--backends`
    # answer for themselves everywhere.
    entry = argv is None
    args = list(sys.argv[1:] if entry else argv)
    flag, info_only, stripped = scan_backend_argv(args)
    asked = canonical_backend(flag)
    forced = asked
    if forced in (None, "auto") and canonical_backend(os.environ.get("WXRANDR_BACKEND")) == "x11":
        # the variable's one say over the handover, so that it cannot ask for a backend this process is then
        # unable to be: `x11` there means the real xrandr on any session, exactly like the flag.  A Wayland name
        # in it is still left alone -- not pre-checked, and not allowed to suppress an X11 session's handover.
        forced = "x11"
    ours = info_only or (flag is not None and asked not in ("auto", "x11"))
    mine = own_flags_in(args)
    if not ours:
        # The X11 session's own answer to our two apply options, given before the handover because after it
        # there is no code of ours left to give one.  `--persistent` is dropped and the apply goes through:
        # the X server keeps no layout of its own, which is what "on sway and X11 nothing is written either
        # way" has always meant, and the real xrandr would have refused the whole command over a flag it has
        # never had.  `--unsafe-gnome-overlap` is refused in the same words a KDE or a sway session refuses it
        # in: this is not GNOME, and X11 overlaps monitors without any of it.
        handover = entry and (forced == "x11"
                              or passthrough.session_kind("xrandr", os.environ) == "x11")
        if handover and gnome_overlap.FLAG in mine:
            stdio.warn("xrandr: %s only means anything on GNOME; this session "
                       "is x11, which places overlapping monitors without it\n"
                       % gnome_overlap.FLAG)
            return 1
        if handover and PERSISTENT_FLAG in mine:
            # Dropped rather than refused: on X11 the apply itself is exactly
            # what was asked for, and nothing is saved either way -- so the
            # command goes through and only the option is gone.  Said out loud
            # because "gone" is otherwise indistinguishable from "honoured", and
            # a script that has been asking for a persistent layout on an X11
            # box has never been getting one.
            stdio.warn("xrandr: %s is dropped on X11: the X server keeps no "
                       "saved layout of its own and the real xrandr has never "
                       "had the option; the rest of the command is handed over "
                       "unchanged\n" % PERSISTENT_FLAG)
        rc = passthrough.maybe_exec_real(
            "xrandr", args if (flag is None and not mine) else stripped,
            entry=entry, force=forced == "x11")
        if rc is not None:
            return rc
        # Wayland with the original installed: the original ITSELF, against the
        # xw11 display (design section 8.1).  The FULL args and not `stripped`:
        # the hook's fourth rule is "an option of ours means our code", and
        # `--persistent` -- which `stripped` has already removed -- is exactly
        # such an option, so a stripped argv here would send a persistent apply
        # to the original with the persistence silently gone.  Imported inside
        # main(): on an X11 session the handover above has already replaced this
        # process, and the wxrandr zipapp carries no xw11/.
        try:
            from xw11.wrap import maybe_exec_through_proxy
        except ImportError:             # pragma: no cover - a bundle without xw11/
            maybe_exec_through_proxy = None
        if maybe_exec_through_proxy is not None:
            rc = maybe_exec_through_proxy("xrandr", args, entry=entry)
            if rc is not None:
                return rc
    if argv is None:
        argv = sys.argv[1:]
    quiet = False
    try:
        code = _run(list(argv))
    except SystemExit as e:
        # `-help`/`--help` and `Can't open display` leave through here
        stdio.exit_after_flush("xrandr", e)
        raise                   # unreachable; the line above raises
    except ArgErr as e:
        stdio.warn("xrandr: %s" % e.args[0])
        stdio.warn("Try 'xrandr --help' for more information.\n")
        code = 1
    except Fatal as e:
        stdio.warn("xrandr: %s" % e.args[0])
        code = 1
    except BrokenPipeError:
        code = 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # never a traceback: an out-of-range --pos/--rate/--scale that trips a struct pack, a lost compositor
        # connection mid-apply, malformed IPC — all become one-line xrandr: fatals, like the real thing.
        stdio.warn("xrandr: %s\n" % e)
        # An OSError here is a write to stdout that failed (a full disk, a quota, `>/dev/full`): the flush below
        # is about to fail with the same errno, and the originals print one line, not two.
        quiet = isinstance(e, OSError)
        code = 1
    return code if stdio.flush_stdout("xrandr", quiet) else (code or 1)
