"""wmctrl-compatible option parsing and dispatch.

Byte-parity target: wmctrl 1.07 (the binary in the devshell is the oracle;
its --help output is embedded verbatim below, and error strings/exit codes
follow main.c). -V/--version print "1.07" exactly like the oracle so
version-sniffing scripts keep working (wwmctl.VERSION carries the real
identity). Deliberate deviations, all documented in the tests:
- -h / -V / --help / --version work without a session (real wmctrl needs an
  open X display for -h and -V),
- "Cannot open display." becomes the backend detector's actual error text,
- acting on a window id that does not exist exits 1 silently (real wmctrl
  fires the ClientMessage into the void and exits 0),
- a broken stdout pipe exits 1 quietly (real wmctrl dies of SIGPIPE),
- the option set is the UNION of the two oracle generations (see below), so
  `-j -S -Y -y -z -E` and `-k toggle` work on every flavor while wmctrl
  1.07 rejects them.

Two generations of oracle are in the field and both print "1.07" for -V:
wmctrl 1.07 (Ubuntu 24.04) and 1.07+git20240228 (Ubuntu 25.04+, Debian 13+),
which adds `-j` (list the current desktop), `-S` (list in stacking order),
`-Y` (iconify), `-y` (move/resize then activate), the undocumented `-z`
(lower) and `-E` (print the title), and `-k toggle`. We implement all of
them everywhere; only the --help text, which is that generation's
documentation rather than its behavior, follows the oracle installed on
this box (one cached `wmctrl --help`, or $WWMCTL_WMCTRL_GENERATION).
"""

import getopt
import os
import sys

from w11common import passthrough, stdio
from w11common.errors import CmdError
from wdotool import backend

from wwmctl import core

# what -V/--version print: byte parity with the oracle binary
WMCTRL_VERSION = "1.07"

# vanilla wmctrl 1.07 optstring, kept for the record; what we parse is the union below (1.07+git20240228's
# main.c adds S j Y: y: z: E:, and the Debian/Ubuntu packaging adds M: and L on top)
OPTSTRING_107 = "FGVvhlupidmxa:r:s:c:t:w:k:o:n:g:e:b:N:I:T:R:"
OPTSTRING = ("FGVvhSlupidjmxa:r:s:c:t:w:k:o:n:g:e:y:b:z:E:N:I:T:LR:Y:M:")

#: The one option of ours, and wmctrl has never had it: `-G` prints the ORIGINAL's doubled origin (bug and
#: all -- `core.Core._geometry_column` has the measurement), and this prints the rectangle `xdotool
#: getwindowgeometry` and `xwininfo` agree on. Named the way `wxrandr --persistent` is named, and kept out
#: of `help_text()` for the same reason that one is kept out of xrandr's: the help text is the oracle's
#: bytes. Two things key on this tuple -- `getopt` takes it as the long-option list, and `main()` asks
#: whether the argv carries one before either handover, because an option of ours means our code and the
#: original would answer `invalid option` to it.
OWN_FLAGS = ("true-geometry",)

def own_flags_in(args) -> list:
    """The options of OWN_FLAGS this argv really carries, in the order they appear.

    A scan of the whole argv, `wxrandr.own_flags_in`'s shape, and it needs no value-tracking the way that
    one does: every wmctrl option that takes a value takes a window title, a class, a state argument or a
    number, and none of those begins with `--`, while `xrandr --output --persistent --off` really can name
    an output after our own flag.

    An unambiguous PREFIX counts, because that is what `getopt` itself accepts a few lines below -- `wwmctl
    --true -lG` parses as `--true-geometry` there, so it has to be ours here too or an X11 session would
    hand it to a wmctrl that answers `invalid option`. `--true-geometry=x` counts as well: it is ours, and
    the usage error it earns is ours to print."""
    out = []
    for a in args:
        if not a.startswith("--") or a == "--":
            continue
        name = a[2:].split("=", 1)[0]
        if name and len([f for f in OWN_FLAGS if f.startswith(name)]) == 1:
            out.append(a)
    return out


HELP = '''wmctrl 1.07
Usage: wmctrl [OPTION]...
Actions:
  -m                   Show information about the window manager and
                       about the environment.
  -l                   List windows managed by the window manager.
  -d                   List desktops. The current desktop is marked
                       with an asterisk.
  -s <DESK>            Switch to the specified desktop.
  -a <WIN>             Activate the window by switching to its desktop and
                       raising it.
  -c <WIN>             Close the window gracefully.
  -R <WIN>             Move the window to the current desktop and
                       activate it.
  -r <WIN> -t <DESK>   Move the window to the specified desktop.
  -r <WIN> -e <MVARG>  Resize and move the window around the desktop.
                       The format of the <MVARG> argument is described below.
  -r <WIN> -b <STARG>  Change the state of the window. Using this option it's
                       possible for example to make the window maximized,
                       minimized or fullscreen. The format of the <STARG>
                       argument and list of possible states is given below.
  -r <WIN> -N <STR>    Set the name (long title) of the window.
  -r <WIN> -I <STR>    Set the icon name (short title) of the window.
  -r <WIN> -T <STR>    Set both the name and the icon name of the window.
  -k (on|off)          Activate or deactivate window manager's
                       "showing the desktop" mode. Many window managers
                       do not implement this mode.
  -o <X>,<Y>           Change the viewport for the current desktop.
                       The X and Y values are separated with a comma.
                       They define the top left corner of the viewport.
                       The window manager may ignore the request.
  -n <NUM>             Change number of desktops.
                       The window manager may ignore the request.
  -g <W>,<H>           Change geometry (common size) of all desktops.
                       The window manager may ignore the request.
  -h                   Print help.

Options:
  -i                   Interpret <WIN> as a numerical window ID.
  -p                   Include PIDs in the window list. Very few
                       X applications support this feature.
  -G                   Include geometry in the window list.
  -x                   Include WM_CLASS in the window list or
                       interpret <WIN> as the WM_CLASS name.
  -u                   Override auto-detection and force UTF-8 mode.
  -F                   Modifies the behavior of the window title matching
                       algorithm. It will match only the full window title
                       instead of a substring, when this option is used.
                       Furthermore it makes the matching case sensitive.
  -v                   Be verbose. Useful for debugging.
  -w <WA>              Use a workaround. The option may appear multiple
                       times. List of available workarounds is given below.

Arguments:
  <WIN>                This argument specifies the window. By default it's
                       interpreted as a string. The string is matched
                       against the window titles and the first matching
                       window is used. The matching isn't case sensitive
                       and the string may appear in any position
                       of the title.

                       The -i option may be used to interpret the argument
                       as a numerical window ID represented as a decimal
                       number. If it starts with "0x", then
                       it will be interpreted as a hexadecimal number.

                       The -x option may be used to interpret the argument
                       as a string, which is matched against the window's
                       class name (WM_CLASS property). Th first matching
                       window is used. The matching isn't case sensitive
                       and the string may appear in any position
                       of the class name. So it's recommended to  always use
                       the -F option in conjunction with the -x option.

                       The special string ":SELECT:" (without the quotes)
                       may be used to instruct wmctrl to let you select the
                       window by clicking on it.

                       The special string ":ACTIVE:" (without the quotes)
                       may be used to instruct wmctrl to use the currently
                       active window for the action.

  <DESK>               A desktop number. Desktops are counted from zero.

  <MVARG>              Specifies a change to the position and size
                       of the window. The format of the argument is:

                       <G>,<X>,<Y>,<W>,<H>

                       <G>: Gravity specified as a number. The numbers are
                          defined in the EWMH specification. The value of
                          zero is particularly useful, it means "use the
                          default gravity of the window".
                       <X>,<Y>: Coordinates of new position of the window.
                       <W>,<H>: New width and height of the window.

                       The value of -1 may appear in place of
                       any of the <X>, <Y>, <W> and <H> properties
                       to left the property unchanged.

  <STARG>              Specifies a change to the state of the window
                       by the means of _NET_WM_STATE request.
                       This option allows two properties to be changed
                       simultaneously, specifically to allow both
                       horizontal and vertical maximization to be
                       altered together.

                       The format of the argument is:

                       (remove|add|toggle),<PROP1>[,<PROP2>]

                       The EWMH specification defines the
                       following properties:

                           modal, sticky, maximized_vert, maximized_horz,
                           shaded, skip_taskbar, skip_pager, hidden,
                           fullscreen, above, below

Workarounds:

  DESKTOP_TITLES_INVALID_UTF8      Print non-ASCII desktop titles correctly
                                   when using Window Maker.

The format of the window list:

  <window ID> <desktop ID> <client machine> <window title>

The format of the desktop list:

  <desktop ID> [-*] <geometry> <viewport> <workarea> <title>


Author, current maintainer: Tomas Styblo <tripie@cpan.org>
Released under the GNU General Public License.
Copyright (C) 2003
'''


# The wmctrl of Ubuntu 25.04+/Debian 13+: upstream 1.07+git20240228 (six more options and `-k toggle`) plus the
# two options the distro patches add, -M and -L. This is the text the installed oracle prints, which is the one
# worth matching -- the unpatched tarball is what nobody has.
HELP_GIT = '''wmctrl 1.07
Usage: wmctrl [OPTION]...
Actions:
  -m                   Show information about the window manager and
                       about the environment.
  -l                   List windows managed by the window manager.
  -d                   List desktops. The current desktop is marked
                       with an asterisk.
  -j                   List current desktop.
  -s <DESK>            Switch to the specified desktop.
  -a <WIN>             Activate the window by switching to its desktop and
                       raising it.
  -c <WIN>             Close the window gracefully.
  -R <WIN>             Move the window to the current desktop and
                       activate it.
  -Y <WIN>             Iconify (minimize) the window.
  -r <WIN> -t <DESK>   Move the window to the specified desktop.
  -r <WIN> -e <MVARG>  Resize and move the window around the desktop.
                       The format of the <MVARG> argument is described below.
  -r <WIN> -y <MVARG>  Resize and move like above, then reactivate.
  -r <WIN> -b <STARG>  Change the state of the window. Using this option it's
                       possible for example to make the window maximized,
                       shaded or fullscreen. The format of the <STARG>
                       argument and list of possible states is given below.
  -r <WIN> -N <STR>    Set the name (long title) of the window.
  -r <WIN> -I <STR>    Set the icon name (short title) of the window.
  -r <WIN> -M <PATH>   Set the mini-icon of the window to the xpm bitmap in <PATH>.
  -r <WIN> -T <STR>    Set both the name and the icon name of the window.
  -r <WIN> -L          List information about the window.
  -k (on|off|toggle)   Activate or deactivate window manager's
                       "showing the desktop" mode. Many window managers
                       do not implement this mode.
  -o <X>,<Y>           Change the viewport for the current desktop.
                       The X and Y values are separated with a comma.
                       They define the top left corner of the viewport.
                       The window manager may ignore the request.
  -n <NUM>             Change number of desktops.
                       The window manager may ignore the request.
  -g <W>,<H>           Change geometry (common size) of all desktops.
                       The window manager may ignore the request.
  -h                   Print help.

Options:
  -S                   List windows in stacking order (bottom to top).
  -i                   Interpret <WIN> as a numerical window ID.
  -p                   Include PIDs in the window list. Very few
                       X applications support this feature.
  -G                   Include geometry in the window list.
  -x                   Include WM_CLASS in the window list or
                       interpret <WIN> as the WM_CLASS name.
  -u                   Override auto-detection and force UTF-8 mode.
  -F                   Modifies the behavior of the window title matching
                       algorithm. It will match only the full window title
                       instead of a substring, when this option is used.
                       Furthermore it makes the matching case sensitive.
  -v                   Be verbose. Useful for debugging.
  -w <WA>              Use a workaround. The option may appear multiple
                       times. List of available workarounds is given below.

Arguments:
  <WIN>                This argument specifies the window. By default it's
                       interpreted as a string. The string is matched
                       against the window titles and the first matching
                       window is used. The matching isn't case sensitive
                       and the string may appear in any position
                       of the title.

                       The -i option may be used to interpret the argument
                       as a numerical window ID represented as a decimal
                       number. If it starts with "0x", then
                       it will be interpreted as a hexadecimal number.

                       The -x option may be used to interpret the argument
                       as a string, which is matched against the window's
                       class name (WM_CLASS property). The first matching
                       window is used. The matching isn't case sensitive
                       and the string may appear in any position
                       of the class name. So it's recommended to  always use
                       the -F option in conjunction with the -x option.

                       The special string ":SELECT:" (without the quotes)
                       may be used to instruct wmctrl to let you select the
                       window by clicking on it.

                       The special string ":ACTIVE:" (without the quotes)
                       may be used to instruct wmctrl to use the currently
                       active window for the action.

  <DESK>               A desktop number. Desktops are counted from zero.

  <MVARG>              Specifies a change to the position and size
                       of the window. The format of the argument is:

                       <G>,<X>,<Y>,<W>,<H>

                       <G>: Gravity specified as a number. The numbers are
                          defined in the EWMH specification. The value of
                          zero is particularly useful, it means "use the
                          default gravity of the window".
                       <X>,<Y>: Coordinates of new position of the window.
                       <W>,<H>: New width and height of the window.

                       The value of -1 may appear in place of
                       any of the <X>, <Y>, <W> and <H> properties
                       to left the property unchanged.

  <STARG>              Specifies a change to the state of the window
                       by the means of _NET_WM_STATE request.
                       This option allows two properties to be changed
                       simultaneously, specifically to allow both
                       horizontal and vertical maximization to be
                       altered together.

                       The format of the argument is:

                       (remove|add|toggle),<PROP1>[,<PROP2>]

                       The EWMH specification defines the
                       following properties:

                           modal, sticky, maximized_vert, maximized_horz,
                           shaded, skip_taskbar, skip_pager, hidden,
                           fullscreen, above, below

Workarounds:

  DESKTOP_TITLES_INVALID_UTF8      Print non-ASCII desktop titles correctly
                                   when using Window Maker.

The format of the window list:

  <window ID> <desktop ID> <client machine> <window title>

The format of the desktop list:

  <desktop ID> [-*] <geometry> <viewport> <workarea> <title>


Author, current maintainer: Tomas Styblo <tripie@cpan.org>
Released under the GNU General Public License.
Copyright (C) 2003
'''


def _prog() -> str:
    """argv[0] verbatim, which is what wmctrl's getopt diagnostics print -- `/usr/bin/wmctrl -Q` says
    "/usr/bin/wmctrl: invalid option -- 'Q'". `python -m wwmctl` has no name worth printing."""
    if sys.argv and sys.argv[0]:
        if os.path.basename(sys.argv[0]) != "__main__.py":
            return sys.argv[0]
    return "wwmctl"


_GENERATION = None


def _oracle_generation() -> str:
    """"1.07" or "git": which wmctrl this box's scripts were written against.

    Both generations answer "1.07" to -V, so the only observable difference is the help text -- which is what we
    are choosing here, so reading it off the installed oracle is exactly right. One subprocess, memoised, and
    only on the paths that print help. Without an oracle on PATH (we may BE /usr/bin/wmctrl) nothing can be
    compared, and the documented target of this clone is 1.07.

    $WWMCTL_WMCTRL_GENERATION forces the answer, for tests and for a box whose oracle is not the one its scripts
    expect."""
    global _GENERATION
    forced = (os.environ.get("WWMCTL_WMCTRL_GENERATION") or "").strip()
    if forced in ("1.07", "git"):
        return forced
    if _GENERATION is not None:
        return _GENERATION
    _GENERATION = "1.07"
    try:
        real = passthrough.real_tool("wmctrl")
        if real:
            import subprocess
            out = subprocess.run([real, "--help"], capture_output=True,
                                 stdin=subprocess.DEVNULL, timeout=10).stdout
            if b"\n  -j " in out:
                _GENERATION = "git"
    except Exception:
        pass
    return _GENERATION


def help_text() -> str:
    return HELP_GIT if _oracle_generation() == "git" else HELP


def _envir_utf8(force: bool) -> bool:
    """wmctrl's init_charset(): locale charset, with LC_CTYPE/LANG able to
    force UTF-8 on. Verbose-only cosmetics for us (we always emit UTF-8)."""
    if force:
        return True
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = (os.environ.get(var) or "").upper()
        if "UTF8" in val or "UTF-8" in val:
            return True
    return False


def main(argv=None) -> int:
    """Entry point: _run() plus the plumbing wmctrl gets from libc for free (stdout may be a closed fd or a
    broken pipe; wmctrl dies of SIGPIPE or lets printf fail silently — we exit quietly instead of tracing
    back)."""
    stdio.repair_std()      # fd 1 or 2 closed before Python started
    backend.set_program("wwmctl")
    args = list(sys.argv[1:] if argv is None else argv)
    # An option of ours means our code, on either kind of session -- the same rule wxrandr's main() applies
    # before its own two handovers, and for the same reason: the original has never heard of the option and
    # would answer `wmctrl: invalid option -- '-'` to a command line we understand.  `--true-geometry` is
    # the whole list (OWN_FLAGS); everything else in this argv is a byte wmctrl takes too.
    if not own_flags_in(args):
        # X11 session: hand over to the real wmctrl (argv here is already
        # sys.argv[1:], wmctrl's own convention).
        rc = passthrough.maybe_exec_real("wmctrl", args, entry=argv is None)
        if rc is not None:
            return rc
        # Wayland with the original installed: the original ITSELF, run against
        # the xw11 display, which answers for the whole desktop (design section
        # 8.1).  Imported here and not at the top of the file: on an X11 session
        # main() has already left above, so `xw11.wrap` is never imported there --
        # and the wwmctl zipapp carries no xw11/ (scripts/build-pyz.sh).
        try:
            from xw11.wrap import maybe_exec_through_proxy
        except ImportError:             # pragma: no cover - a bundle without xw11/
            maybe_exec_through_proxy = None
        if maybe_exec_through_proxy is not None:
            rc = maybe_exec_through_proxy("wmctrl", args, entry=argv is None)
            if rc is not None:
                return rc
    quiet = False
    try:
        rc = _run(argv)
    except SystemExit as e:
        stdio.exit_after_flush(_prog(), e)
        raise                       # unreachable; the line above raises
    except KeyboardInterrupt:
        rc = 130                    # 128 + SIGINT, what the shell reports and what wdotool/wxprop already return
    except BrokenPipeError:
        rc = 1
    except Exception as e:
        # never a traceback: a listing whose write to a full stdout
        # failed, a compositor that went away mid-query.  DEBUG set to anything (including "") is the escape
        # hatch for a bug report: the exception propagates and Python prints the traceback it would have.
        if os.environ.get("DEBUG") is not None:
            raise
        stdio.warn("%s: %s\n" % (_prog(), e))
        # An OSError here is a write to stdout that failed (a full disk, a quota, `>/dev/full`): the flush below
        # is about to fail with the same errno, and the originals print one line, not two.
        quiet = isinstance(e, OSError)
        rc = 1
    return rc if stdio.flush_stdout(_prog(), quiet) else (rc or 1)


def _run(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # wmctrl special-cases exactly `wmctrl --help` / `wmctrl --version`
    if len(argv) == 1:
        if argv[0] == "--help":
            sys.stdout.write(help_text())
            return 0
        if argv[0] == "--version":
            print(WMCTRL_VERSION)
            return 0

    try:
        opts, _positional = getopt.gnu_getopt(argv, OPTSTRING, list(OWN_FLAGS))
    except getopt.GetoptError as e:
        opt = getattr(e, "opt", "") or ""
        if len(opt) > 1:
            # wmctrl uses plain getopt: "--anything" is the unknown short
            # option '-' (glibc prints exactly this, verified vs the oracle)
            sys.stderr.write("%s: invalid option -- '-'\n" % _prog())
        elif "requires argument" in str(e):
            sys.stderr.write("%s: option requires an argument -- '%s'\n" % (_prog(), opt))
        else:
            sys.stderr.write("%s: invalid option -- '%s'\n" % (_prog(), opt))
        return 1

    if not opts:  # wmctrl: "missing_option" -> full help on stderr
        sys.stderr.write(help_text())
        return 1

    show_pid = show_geometry = show_class = False
    match_by_id = match_by_cls = full_match = False
    verbose = force_utf8 = true_geometry = False
    param_window = None
    param = None
    action = None

    for name, val in opts:
        if name.startswith("--"):
            # ours, not wmctrl's; `getopt` has already rejected every other long spelling
            true_geometry = true_geometry or name[2:] == "true-geometry"
            continue
        c = name[1:]
        if c == "F":
            full_match = True
        elif c == "G":
            show_geometry = True
        elif c == "i":
            match_by_id = True
        elif c == "v":
            verbose = True
        elif c == "u":
            force_utf8 = True
        elif c == "x":
            match_by_cls = True
            show_class = True
        elif c == "p":
            show_pid = True
        elif c == "S":
            # 1.07+git: list in stacking order. Ours always is (docs/WWMCTL.md),
            # so this only silences the option.
            pass
        elif c in ("a", "c", "R", "Y", "z", "E"):
            param_window = val
            action = c
        elif c == "r":
            param_window = val
        elif c in ("t", "e", "y", "b", "N", "I", "T", "M", "s", "k", "o", "n", "g"):
            param = val
            action = c
        elif c == "w":
            if val != "DESKTOP_TITLES_INVALID_UTF8":
                sys.stderr.write("Unknown workaround: %s\n" % val)
                return 1
        elif c == "L":  # 1.07+git, distro patch: no argument
            action = c
        else:  # V h l d j m
            action = c

    envir_utf8 = _envir_utf8(force_utf8)
    if verbose:
        sys.stderr.write("envir_utf8: %d\n" % int(envir_utf8))

    if action == "V":
        print(WMCTRL_VERSION)
        return 0
    if action == "h":
        sys.stdout.write(help_text())
        return 0
    if action is None:  # e.g. plain `wwmctl -p`: options but nothing to do
        return 0

    ctl = core.Core(verbose=verbose, utf8=envir_utf8, true_geometry=true_geometry)
    try:
        if action == "l":
            return ctl.list_windows(show_pid, show_geometry, show_class)
        if action == "d":
            return ctl.list_desktops()
        if action == "j":
            return ctl.list_current_desktop()
        if action == "m":
            return ctl.wm_info()
        if action == "s":
            return ctl.switch_desktop(param or "")
        if action == "k":
            return ctl.showing_desktop(param or "")
        if action == "o":
            return ctl.change_viewport(param or "")
        if action == "n":
            return ctl.change_number_of_desktops(param or "")
        if action == "g":
            return ctl.change_geometry(param or "")
        # window actions
        if param_window is None:
            sys.stderr.write("No window was specified.\n")
            return 1
        return ctl.action_window(action, param_window, param,
                                 match_by_id, match_by_cls, full_match,
                                 show_pid, show_geometry, show_class)
    except CmdError as e:
        sys.stderr.write("%s\n" % e)
        return 1
