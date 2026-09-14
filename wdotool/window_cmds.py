"""Window commands: search/selectwindow/getactivewindow/... (cmd_search.c and friends). Output strings are
byte-parity copies of the real xdotool.

Command contract per ctx.py: cmd_foo(ctx, args) -> tokens consumed."""

import os
import re
import sys
import time

from w11common.errors import CmdError
from hacks.window import backend as _wbackend
from wdotool import cli, commands
from wdotool.cli import ChainAbort, GetoptError, _opts, getopt_long_only
from wdotool.cnum import atoi as _atoi, strtol as _strtol
from wdotool.ctx import SoftCmdError

_SEE_STACK = "If no window is given, %1 is used. See WINDOW STACK in xdotool(1)\n"


def _out(line: str):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _window_arg(ctx, rest, min_args, usage):
    """C window_get_arg(): decide whether rest[0] is an optional window
    argument. Returns (window_arg_or_None, tokens_consumed 0/1)."""
    # The empty-stack refusal ends with this command's usage; the C code has
    # it in scope there, and this is the one place every such command passes.
    ctx.cmd_usage = usage
    if len(rest) < min_args:
        raise CmdError(
            "Too few arguments (got %d, minimum is %d)\n%s"
            % (len(rest), min_args, usage.rstrip("\n"))
        )
    if len(rest) > min_args and not commands.is_command(rest[min_args]):
        return rest[0], 1
    return None, 0


def _display_size(ctx) -> tuple[int, int]:
    try:
        return ctx.backend().display_size()
    except CmdError:
        pass
    sys.stderr.write("wdotool: cannot determine display size; assuming 1920x1080\n")
    return 1920, 1080


def _warn_noop(what: str):
    sys.stderr.write("wdotool: %s\n" % what)


# ---------------------------------------------------------------------------
# search

_SEARCH_LONGOPTS = [
    ("all", False), ("any", False), ("class", False), ("classname", False),
    ("help", False), ("maxdepth", True), ("name", False), ("shell", False),
    ("prefix", True), ("onlyvisible", False), ("pid", True), ("screen", True),
    ("title", False), ("desktop", True), ("limit", True), ("sync", False),
    ("role", False),
]


def cmd_search(ctx, args):
    cmd = getattr(ctx, "cmd_name", "search")
    usage = (
        "Usage: xdotool %s [options] regexp_pattern\n"
        "--class         check regexp_pattern against the window class\n"
        "--classname     check regexp_pattern against the window classname\n"
        "--role          check regexp_pattern against the window role\n"
        "--maxdepth N    set search depth to N. Default is infinite.\n"
        "                -1 also means infinite.\n"
        "--onlyvisible   matches only windows currently visible\n"
        "--pid PID       only show windows belonging to specific process\n"
        "                Not supported by all X11 applications\n"
        "--screen N      only search a specific screen. Default is all screens\n"
        "--desktop N     only search a specific desktop number\n"
        "--limit N       break search after N results\n"
        "--name          check regexp_pattern against the window name\n"
        "--shell         print results as shell array WINDOWS=( ... )\n"
        "--prefix STR    use prefix (max 16 chars) for array name STRWINDOWS\n"
        "--title         DEPRECATED. Same as --name.\n"
        "--all           Require all conditions match a window. Default is --any\n"
        "--any           Windows matching any condition will be reported\n"
        "--sync          Wait until a search result is found.\n"
        "-h, --help      show this help output\n"
        "\n"
        "If none of --name, --classname, --class, or --role are specified, the \n"
        "defaults are: --name --classname --class --role\n" % cmd
    )
    parsed = _opts(cmd, args, "h", _SEARCH_LONGOPTS, usage, invalid_usage=True)
    if parsed is None:
        return len(args)
    opts, nopts = parsed

    want_name = want_class = want_classname = want_role = False
    only_visible = op_sync = shell = require_all = False
    pid = 0
    pid_want = False
    desktop = None
    limit = 0
    maxdepth = -1
    screen = None
    prefix = ""
    for name, val in opts:
        if name == "maxdepth":
            maxdepth = _strtol(val)
        elif name == "pid":
            pid = _atoi(val)
            pid_want = True
        elif name == "any":
            require_all = False
        elif name == "all":
            require_all = True
        elif name == "screen":
            screen = _strtol(val)
        elif name == "onlyvisible":
            only_visible = True
        elif name == "class":
            want_class = True
        elif name == "classname":
            want_classname = True
        elif name == "role":
            want_role = True
        elif name == "title":
            sys.stderr.write(
                "This flag is deprecated. Assuming you mean --name (the"
                " window name).\n"
            )
            want_name = True
        elif name == "name":
            want_name = True
        elif name == "shell":
            shell = True
        elif name == "prefix":
            prefix = val[:16]
        elif name == "desktop":
            desktop = _strtol(val)
        elif name == "limit":
            limit = _atoi(val)
        elif name == "sync":
            op_sync = True

    rest = args[nopts:]
    if not rest and pid == 0:
        raise CmdError(usage.rstrip("\n"))

    pattern = None
    consumed = nopts
    if rest:
        pattern = rest[0]
        consumed += 1
        if not (want_name or want_class or want_classname or want_role):
            sys.stderr.write("Defaulting to search window name, class, classname, and role\n")
            want_name = want_class = want_classname = want_role = True

    rx = None
    rx_broken = False
    if pattern is not None:
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except (re.error, RecursionError, OverflowError) as e:
            # re.compile can also raise RecursionError (deeply nested groups) and OverflowError (huge repetition
            # counts); C regcomp fails gracefully on those, so treat them all as a bad regex.
            sys.stderr.write("Failed to compile regex: '%s'; error %s\n" % (pattern, e))
            rx_broken = True

    def matches(w):
        if rx_broken:
            return False
        if only_visible and not w.visible:
            return False
        if desktop is not None and w.desktop != desktop:
            return False
        if screen not in (None, 0):
            return False
        if maxdepth == 0:
            return False
        conds = []
        if pid_want:
            conds.append(w.pid == pid)
        if rx is not None:
            if want_name:
                conds.append(rx.search(w.title or "") is not None)
            if want_class:
                # WM_CLASS class for X clients; for native toplevels Mutter's wm_class (the app id the
                # client gave), which is what an X11 script's --class pattern was written against.
                conds.append(rx.search(w.class_ or "") is not None)
            if want_classname:
                # WM_CLASS *instance* -- `xterm -name myinst` is findable by "myinst" like it is under X11 (B4).
                # Native Wayland toplevels have no instance, so they keep matching their app_id and
                # --class/--classname stay equivalent there.
                conds.append(rx.search(w.instance or w.class_ or "") is not None)
            if want_role:
                # Window roles do not exist on Wayland; match against ""
                # exactly like libxdo does for a window with no role set.
                conds.append(rx.search("") is not None)
        if require_all:
            return all(conds)
        return any(conds)

    debug = os.environ.get("DEBUG") is not None
    while True:
        found = [w.id for w in ctx.backend().list() if matches(w)]
        if limit > 0:
            found = found[:limit]
        if found or not op_sync:
            break
        if debug:
            sys.stderr.write("No search results, still waiting...\n")
        time.sleep(0.5)

    is_last = consumed == len(args)
    if is_last or shell:
        if shell:
            sys.stdout.write("%sWINDOWS=(" % prefix)
        for wid in found:
            sys.stdout.write("%d\n" % wid)
        if shell:
            sys.stdout.write(")\n")
        sys.stdout.flush()

    ctx.stack = list(found)
    if not found and not shell:
        # real xdotool returns EXIT_FAILURE from a matchless search and the
        # chain aborts (verified against 4.20260303.1)
        raise ChainAbort(1)
    return consumed


# ---------------------------------------------------------------------------
# window queries

def cmd_selectwindow(ctx, args):
    cmd = getattr(ctx, "cmd_name", "selectwindow")
    usage = "Usage: %s\n" % cmd
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    wid = ctx.backend().select_window()
    if nopts == len(args):
        _out("%d" % wid)
    ctx.stack = [wid]
    return nopts


def _focused_window(ctx, errmsg):
    for w in ctx.backend().list():
        if w.focused:
            return w.id
    raise CmdError(errmsg)


def cmd_getactivewindow(ctx, args):
    cmd = getattr(ctx, "cmd_name", "getactivewindow")
    usage = "Usage: %s\n" % cmd
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    wid = _focused_window(ctx, "xdo_get_active_window reported an error")
    if nopts == len(args):
        _out("%d" % wid)
    ctx.stack = [wid]
    return nopts


def cmd_getwindowfocus(ctx, args):
    cmd = getattr(ctx, "cmd_name", "getwindowfocus")
    usage = (
        "Usage: %s [-f]\n"
        "-f     - Report the window with focus even if we don't think it is a \n"
        "         top-level window. The default is to find the top-level window\n"
        "         that has focus.\n" % cmd
    )
    parsed = _opts(cmd, args, "fh", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed  # -f is accepted; all Wayland toplevels are "sane"
    wid = _focused_window(ctx, "xdo_focus_window reported an error")
    if nopts == len(args):
        _out("%d" % wid)
    ctx.stack = [wid]
    return nopts


def cmd_getwindowname(ctx, args):
    cmd = getattr(ctx, "cmd_name", "getwindowname")
    usage = "Usage: %s [window=%%1]\n%s" % (cmd, _SEE_STACK)
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    warg, used = _window_arg(ctx, args[nopts:], 0, usage)
    for wid in ctx.resolve_windows(warg):
        _out(ctx.backend().find(wid).title)
    return nopts + used


def cmd_getwindowclassname(ctx, args):
    cmd = getattr(ctx, "cmd_name", "getwindowclassname")
    usage = "Usage: %s [window=%%1]\n%s" % (cmd, _SEE_STACK)
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    warg, used = _window_arg(ctx, args[nopts:], 0, usage)
    for wid in ctx.resolve_windows(warg):
        _out(ctx.backend().find(wid).class_)
    return nopts + used


def cmd_getwindowpid(ctx, args):
    cmd = getattr(ctx, "cmd_name", "getwindowpid")
    usage = "Usage: %s [window=%%1]\n%s" % (cmd, _SEE_STACK)
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    warg, used = _window_arg(ctx, args[nopts:], 0, usage)
    for wid in ctx.resolve_windows(warg):
        pid = ctx.backend().find(wid).pid
        if pid == 0:
            raise CmdError("window %d has no pid associated with it." % wid)
        _out("%d" % pid)
    return nopts + used


def cmd_getwindowgeometry(ctx, args):
    cmd = getattr(ctx, "cmd_name", "getwindowgeometry")
    usage = (
        "Usage: %s [window=%%1] [--shell] [--prefix <STR>]\n"
        "--shell      - output shell variables for use with eval\n"
        "--prefix STR - use prefix for shell variables names (max 16 chars) \n"
        "%s" % (cmd, _SEE_STACK)
    )
    parsed = _opts(cmd, args, "h", [("help", False), ("shell", False), ("prefix", True)], usage)
    if parsed is None:
        return len(args)
    opts, nopts = parsed
    shell = False
    prefix = ""
    for name, val in opts:
        if name == "shell":
            shell = True
        elif name == "prefix":
            prefix = val[:16]
    warg, used = _window_arg(ctx, args[nopts:], 0, usage)
    for wid in ctx.resolve_windows(warg):
        w = ctx.backend().find(wid)
        if shell:
            _out("%sWINDOW=%d" % (prefix, wid))
            _out("%sX=%d" % (prefix, w.x))
            _out("%sY=%d" % (prefix, w.y))
            _out("%sWIDTH=%d" % (prefix, w.w))
            _out("%sHEIGHT=%d" % (prefix, w.h))
            _out("%sSCREEN=0" % prefix)
        else:
            _out("Window %d" % wid)
            _out("  Position: %d,%d (screen: 0)" % (w.x, w.y))
            _out("  Geometry: %dx%d" % (w.w, w.h))
    return nopts + used


# ---------------------------------------------------------------------------
# window actions

# Bounded --sync waits (B3). xdotool's waits are bounded too -- xdo.c loops MAX_TRIES (500) x 30ms = 15s and
# then quietly returns success -- but ours looped for ever, so a windowsize --sync that Mutter snapped to a
# different size, or ten concurrent `windowactivate --sync`, hung until the script was killed (8-9 of 10 hung
# for 614s in the stress run). Ten seconds is generous for any compositor round trip we make;
# WDOTOOL_SYNC_TIMEOUT overrides it in seconds, and 0 restores the old wait-for-ever behaviour.
#
# `search --sync` is deliberately NOT bounded: its manpage entry is the one that promises to "block until there
# are results", for scripts that launch an application and wait for its window. selectwindow and behave are
# likewise unbounded by definition.
SYNC_TIMEOUT = 10.0


def _sync_timeout() -> float:
    raw = os.environ.get("WDOTOOL_SYNC_TIMEOUT")
    if raw is None:
        return SYNC_TIMEOUT
    try:
        val = float(raw)
    except ValueError:
        return SYNC_TIMEOUT
    return val if val > 0 else 0.0


def _wait_until(pred, what, interval=0.03, timeout=None):
    """Poll `pred` until it holds; give up after the --sync timeout with one
    line on stderr and a failing exit status."""
    limit = _sync_timeout() if timeout is None else timeout
    deadline = None if limit <= 0 else time.monotonic() + limit
    while not pred():
        if deadline is not None and time.monotonic() >= deadline:
            raise CmdError("wdotool: gave up waiting for %s after %gs" % (what, limit))
        time.sleep(interval)


def _is_mapped(ctx, wid) -> bool:
    """X11 map state, as close as the backend can tell (WindowBackend.is_mapped
    carries the rule and the default)."""
    return ctx.backend().is_mapped(wid)


def _simple_action(ctx, args, usage_body, act, sync_pred=None, has_sync=False, sync_what="%d"):
    """Shared skeleton for [options] [window=%1] action commands."""
    usage = usage_body
    longopts = [("help", False)] + ([("sync", False)] if has_sync else [])
    parsed = _opts(getattr(ctx, "cmd_name", "?"), args, "h", longopts, usage)
    if parsed is None:
        return len(args)
    opts, nopts = parsed
    opsync = any(n == "sync" for n, _ in opts)
    warg, used = _window_arg(ctx, args[nopts:], 0, usage)
    for wid in ctx.resolve_windows(warg):
        act(ctx, wid)
        if opsync and sync_pred is not None:
            _wait_until(lambda w=wid: sync_pred(ctx, w), sync_what % wid)
    return nopts + used


def cmd_windowactivate(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowactivate")
    usage = (
        "Usage: %s [options] [window=%%1]\n"
        "--sync    - only exit once the window is active (is visible + active)\n"
        "%s" % (cmd, _SEE_STACK)
    )
    return _simple_action(
        ctx, args, usage,
        lambda c, wid: c.backend().activate(wid),
        sync_pred=lambda c, wid: c.backend().find(wid).focused,
        has_sync=True, sync_what="window %d to become active",
    )


def cmd_windowfocus(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowfocus")
    usage = (
        "Usage: %s [window=%%1]\n"
        "--sync    - only exit once the window has focus\n" % cmd
    )
    return _simple_action(
        ctx, args, usage,
        lambda c, wid: c.backend().focus(wid),
        sync_pred=lambda c, wid: c.backend().find(wid).focused,
        has_sync=True, sync_what="window %d to take focus",
    )


def cmd_windowraise(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowraise")
    usage = "Usage: %s [window=%%1]\n%s" % (cmd, _SEE_STACK)
    return _simple_action(ctx, args, usage, lambda c, wid: c.backend().raise_(wid))


def cmd_windowlower(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowlower")
    usage = "Usage: %s [window=%%1]\n%s" % (cmd, _SEE_STACK)
    return _simple_action(ctx, args, usage, lambda c, wid: c.backend().lower(wid))


def cmd_windowmap(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowmap")
    usage = (
        "Usage: %s [options] [window=%%1]\n"
        "--sync    - only exit once the window has been mapped (is visible)\n"
        "%s" % (cmd, _SEE_STACK)
    )
    return _simple_action(
        ctx, args, usage,
        lambda c, wid: c.backend().map(wid),
        sync_pred=_is_mapped,
        has_sync=True, sync_what="window %d to be mapped",
    )


def cmd_windowunmap(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowunmap")
    usage = (
        "Usage: %s [--sync] [window=%%1]\n"
        "--sync    - only exit once the window has been unmapped (is hidden)\n"
        "%s" % (cmd, _SEE_STACK)
    )
    return _simple_action(
        ctx, args, usage,
        lambda c, wid: c.backend().unmap(wid),
        sync_pred=lambda c, wid: not _is_mapped(c, wid),
        has_sync=True, sync_what="window %d to be unmapped",
    )


def cmd_windowminimize(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowminimize")
    usage = (
        "Usage: %s [options] [window=%%1]\n"
        "--sync    - only exit once the window has minimized (is not visible)\n"
        "%s" % (cmd, _SEE_STACK)
    )
    return _simple_action(
        ctx, args, usage,
        lambda c, wid: c.backend().minimize(wid),
        sync_pred=lambda c, wid: not _is_mapped(c, wid),
        has_sync=True, sync_what="window %d to be minimized",
    )


def cmd_windowclose(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowclose")
    usage = "Usage: %s [window=%%1]\n%s" % (cmd, _SEE_STACK)
    return _simple_action(ctx, args, usage, lambda c, wid: c.backend().close(wid))


def cmd_windowquit(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowquit")
    usage = "Usage: %s [window=%%1]\n%s" % (cmd, _SEE_STACK)
    # Wayland has only one way to close a window: a polite close request.
    return _simple_action(ctx, args, usage, lambda c, wid: c.backend().close(wid))


def cmd_windowkill(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowkill")
    usage = "Usage: %s [window=%%1]\n%s" % (cmd, _SEE_STACK)
    return _simple_action(ctx, args, usage, lambda c, wid: c.backend().kill(wid))


def cmd_windowreparent(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowreparent")
    usage = "Usage: %s [window_source=%%1] window_destination\n" % cmd
    # Alone among the window commands, xdotool's windowreparent answers the long --help (and every abbreviation
    # of it) on stderr with rc 1, and only the short -h on stdout with rc 0. Measured against 3.20160805.1.
    try:
        raw, nopts = getopt_long_only(cmd, args, "h", [("help", False)])
    except GetoptError as e:
        raise CmdError("%s\n%s" % (e, usage.rstrip("\n"))) from None
    if any(n == "h" for n, _ in raw):
        sys.stdout.write(usage)
        sys.stdout.flush()
        return len(args)
    if any(n == "help" for n, _ in raw):
        raise CmdError(usage.rstrip("\n"))
    rest = args[nopts:]
    warg, used = _window_arg(ctx, rest, 1, usage)
    dest_arg = rest[used]
    dests = ctx.resolve_windows(dest_arg)  # validates the destination ref
    if len(dests) > 1:
        raise CmdError(
            "It doesn't make sense to have multiple destinations as the "
            "new parent window. Your destination selection '%s' resulted in %d "
            "windows." % (dest_arg, len(dests))
        )
    ctx.resolve_windows(warg)  # validates the source ref
    # AGENTS.md: a missing feature is a route and its cost, never a sentence about what Wayland
    # allows.  xdotool sends one XReparentWindow; the same call over the X plane this process
    # already opens would do it for an XWayland window (route 5), and a native toplevel has no
    # parent to change until route 6.  Until one of the two is taken this stays a warn-and-succeed,
    # which is what the whole `warn and succeed` column of docs/WDOTOOL.md is.
    _warn_noop("windowreparent: not done here yet -- for an XWayland window the route is one "
               "XReparentWindow over the X plane (AGENTS.md route 5), for a native one a patched "
               "compositor (route 6); ignoring")
    return nopts + used + 1


def cmd_windowmove(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowmove")
    usage = (
        "Usage: %s [options] [window=%%1] x y\n"
        "--sync      - only exit once the window has moved\n"
        "--relative  - make movements relative to the current window position\n"
        "If you use literal 'x' or 'y' for the x coordinates, then the current\n"
        "coordinate will be used. This is useful for moving the window along\n"
        "only one axis.\n" % cmd
    )
    parsed = _opts(cmd, args, "h", [("help", False), ("sync", False), ("relative", False)], usage)
    if parsed is None:
        return len(args)
    opts, nopts = parsed
    opsync = any(n == "sync" for n, _ in opts)
    relative = any(n == "relative" for n, _ in opts)
    rest = args[nopts:]
    warg, used = _window_arg(ctx, rest, 2, usage)
    rest = rest[used:]

    x_cur = rest[0][:1] == "x"
    y_cur = rest[1][:1] == "y"
    # Bug-compatible with cmd_windowmove.c: whether y is a percentage is
    # decided by looking for '%' in the *x* argument.
    x_pct = (not x_cur) and "%" in rest[0]
    y_pct = (not y_cur) and "%" in rest[0]
    x = 0 if (x_cur or x_pct) else _strtol(rest[0])
    y = 0 if (y_cur or y_pct) else _strtol(rest[1])
    xval = _strtol(rest[0])
    yval = _strtol(rest[1])

    for wid in ctx.resolve_windows(warg):
        if x_pct or y_pct:
            rw, rh = _display_size(ctx)
            if x_pct:
                x = rw * xval // 100
            if y_pct:
                y = rh * yval // 100
        ox = oy = 0
        if opsync or x_cur or y_cur or relative:
            w = ctx.backend().find(wid)
            ox, oy = w.x, w.y
            if ox == x and oy == y:
                continue
        tx, ty = x, y
        if relative:
            tx, ty = ox + x, oy + y
        if x_cur:
            tx = ox
        if y_cur:
            ty = oy
        try:
            ctx.backend().move_window(wid, tx, ty)
        except CmdError as e:
            sys.stderr.write("%s\n" % e)
            msg = ("xdo_move_window reported an error while moving window %d" % wid)
            sys.stderr.write("%s\n" % msg)
            if not isinstance(e, SoftCmdError):
                # A stale window id, a KWin script that failed, a compositor that said no: xdotool's own loop
                # keeps going but returns the error, and exiting 0 here made every one of those look like a move
                # that happened.
                raise CmdError(msg) from None
            continue
        if opsync:
            def moved():
                w2 = ctx.backend().find(wid)
                return not (ox == w2.x and oy == w2.y and abs(x - w2.x) > 10 and abs(y - w2.y) > 50)
            _wait_until(moved, "window %d to move" % wid)
    return nopts + used + 2


# Mutter, like any window manager honouring WM_NORMAL_HINTS increments, snaps a resize to the client's cell
# grid: an xterm asked for 497x392 stays at 496x392, so "the size changed" never became true and windowsize
# --sync waited for ever (B3a). xdotool's equivalent loop is bounded and its X11 clients usually take the exact
# pixels; on Wayland we additionally treat a size the compositor has snapped to -- within one cell-ish tolerance
# of the request -- as the answer, the way xdotool treats a size that already satisfies the window's hints as
# done.
_SNAP_TOL = 32  # px; a terminal cell is at most ~24px tall, ~12px wide


def _snapped(cur_w, cur_h, req_w, req_h) -> bool:
    return abs(cur_w - req_w) <= _SNAP_TOL and abs(cur_h - req_h) <= _SNAP_TOL


def cmd_windowsize(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowsize")
    usage = (
        "Usage: %s [--sync] [--usehints] [window=%%1] width height\n"
        "%s"
        "--usehints  - Use window sizing hints (like font size in terminals)\n"
        "--sync      - only exit once the window has resized\n" % (cmd, _SEE_STACK)
    )
    parsed = _opts(
        cmd, args, "uh",
        [("usehints", False), ("help", False), ("sync", False)], usage,
        shortmap={"u": "usehints"},
    )
    if parsed is None:
        return len(args)
    opts, nopts = parsed
    opsync = any(n == "sync" for n, _ in opts)
    usehints = any(n == "usehints" for n, _ in opts)
    rest = args[nopts:]
    if len(rest) < 2:
        raise CmdError(
            "Too few arguments (got %d, minimum is %d)\n"
            "Invalid argument count, got %d, expected %d\n%s"
            % (len(rest), 2, 3, len(rest), usage.rstrip("\n"))
        )
    warg, used = _window_arg(ctx, rest, 2, usage)
    rest = rest[used:]
    if usehints:
        _warn_noop("windowsize: --usehints is not supported on Wayland; "
                   "width/height are interpreted as pixels")
    w_pct = "%" in rest[0]
    h_pct = "%" in rest[1]
    wval = _strtol(rest[0])
    hval = _strtol(rest[1])

    for wid in ctx.resolve_windows(warg):
        w_px, h_px = wval, hval
        if w_pct or h_pct:
            rw, rh = _display_size(ctx)
            if w_pct:
                w_px = rw * wval // 100
            if h_pct:
                h_px = rh * hval // 100
        ow = oh = None
        if opsync:
            cur = ctx.backend().find(wid)
            ow, oh = cur.w, cur.h
            if ow == w_px and oh == h_px:
                break  # C `break`s out of the whole window loop here
        try:
            ctx.backend().resize(wid, w_px, h_px)
        except CmdError as e:
            sys.stderr.write("%s\n" % e)
            msg = "xdo_set_window_size on window:%d reported an error" % wid
            if isinstance(e, SoftCmdError):
                # the tiled-sway case, as in windowmove: warn, rc 0
                sys.stderr.write("%s\n" % msg)
                continue
            raise CmdError(msg) from None
        if opsync:
            def resized(wid=wid, ow=ow, oh=oh, rw=w_px, rh=h_px):
                cur2 = ctx.backend().find(wid)
                if (cur2.w, cur2.h) != (ow, oh):
                    return True          # changed at all -- xdotool's rule
                return _snapped(cur2.w, cur2.h, rw, rh)

            _wait_until(resized, "window %d to be resized" % wid)
    return nopts + used + 2


# The _NET_WM_STATE properties EWMH defines -- what xdotool's own
# windowstate usage lists, and the set wmctrl -b takes.
_EWMH_STATES = frozenset((
    "MODAL", "STICKY", "MAXIMIZED_VERT", "MAXIMIZED_HORZ", "SHADED",
    "SKIP_TASKBAR", "SKIP_PAGER", "HIDDEN", "FULLSCREEN", "ABOVE", "BELOW",
    "DEMANDS_ATTENTION", "FOCUSED",
))


def cmd_windowstate(ctx, args):
    cmd = getattr(ctx, "cmd_name", "windowstate")
    usage = (
        "Usage: %s [options] [window=%%1]\n"
        "%s"
        "--add property  - add a property\n"
        "--remove property - remove a property\n"
        "--toggle property - toggle a property\n"
        "property can be one of \n"
        "MODAL, STICKY, MAXIMIZED_VERT, MAXIMIZED_HORZ, SHADED, SKIP_TASKBAR, \n"
        "SKIP_PAGER, HIDDEN, FULLSCREEN, ABOVE, BELOW, DEMANDS_ATTENTION\n"
        % (cmd, _SEE_STACK)
    )
    parsed = _opts(
        cmd, args, "ha:r:t:",
        [("add", True), ("remove", True), ("toggle", True), ("help", False)],
        usage, shortmap={"a": "add", "r": "remove", "t": "toggle"},
    )
    if parsed is None:
        return len(args)
    opts, nopts = parsed
    # The last --add/--remove/--toggle on the line wins, and that is parity, not an oversight: xdotool's own
    # cmd_windowstate.c keeps a single action/arg_property pair and its getopt_long_only switch overwrites both
    # in every arm, so `--add MAXIMIZED_VERT --add MAXIMIZED_HORZ` maximizes horizontally only there too (read
    # from 4.20260303.1, the release this tree claims parity against). Applying each option would be a nicer
    # command and a different one; README says so under Compatibility.
    action = None
    prop = None
    for name, val in opts:
        if name in ("add", "remove", "toggle"):
            action = {"remove": 0, "add": 1, "toggle": 2}[name]
            prop = val
    if action is None or prop is None:
        raise CmdError(usage.rstrip("\n"))
    warg, used = _window_arg(ctx, args[nopts:], 0, usage)
    name = prop.upper()
    if name not in _EWMH_STATES:
        # A name no _NET_WM_STATE has is a typo, not a capability gap: the backends turned it into "not
        # supported by the <backend> backend", which read as "this desktop cannot do it" and sent people looking
        # for a compositor feature.
        raise CmdError(
            "windowstate: no such property %s\n"
            "property can be one of \n"
            "%s" % (prop, ", ".join(sorted(_EWMH_STATES))))
    has_error = False
    for wid in ctx.resolve_windows(warg):
        try:
            why = ctx.backend().set_state(wid, name, action)
            if why:
                # the compositor took the request and did not apply it; the X tools cannot tell either, so this
                # is a warning, not a failure (wwmctl has a second route and uses it instead)
                sys.stderr.write("wdotool: %s\n" % why)
        except CmdError as e:
            has_error = True
            sys.stderr.write("%s\n" % e)
            sys.stderr.write("xdo_window_property reported an error on window %d\n" % wid)
    if has_error:
        raise ChainAbort(1)
    return nopts + used


def cmd_set_window(ctx, args):
    cmd = getattr(ctx, "cmd_name", "set_window")
    usage = (
        "Usage: %s [options] [window=%%1]\n"
        "--name NAME  - set the window name (aka title)\n"
        "--icon-name NAME - set the window name while minimized/iconified\n"
        "--role ROLE - set the window's role string\n"
        "--class CLASS - set the window's class\n"
        "--classname CLASSNAME - set the window's classname\n"
        "--overrideredirect OVERRIDE - set override_redirect.\n"
        "  1 means the window manager will not manage this window.\n"
        "--urgency URGENT - set the window's urgency hint.\n"
        "  1 sets the urgency flag, 0 removes it.\n" % cmd
    )
    parsed = _opts(
        cmd, args, "hn:i:r:C:N:u:",
        [("name", True), ("icon-name", True), ("role", True), ("class", True),
         ("classname", True), ("overrideredirect", True), ("urgency", True),
         ("help", False)],
        usage,
        shortmap={"n": "name", "i": "icon-name", "r": "role", "C": "class",
                  "N": "classname", "u": "urgency"},
    )
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    warg, used = _window_arg(ctx, args[nopts:], 0, usage)
    ctx.resolve_windows(warg)  # validates the window reference
    _warn_noop("set_window: window properties cannot be changed from outside "
               "on Wayland; ignoring")
    return nopts + used


_USAGE_BEHAVE = (
    "Usage: %s window event action [args...]\n"
    "The event is a window event, such as mouse-enter, resize, etc.\n"
    "The action is any valid xdotool command (chains OK here)\n"
    "\n"
    "Events: \n"
    "  mouse-enter      - When the mouse moves into the window\n"
    "  mouse-leave      - When the mouse leaves a window\n"
    "  mouse-click      - Fired when the mouse button is released\n"
    "  focus            - When the window gets focus\n"
    "  blur             - When the window loses focus\n"
)


_BEHAVE_EVENTS = ("mouse-enter", "mouse-leave", "mouse-click", "focus", "blur")

# The one sentence for the click event, named with its rung so it is a gap and not a decline -- test_one_rule
# pins it.  A released button wdotool did not itself press is not in any window-management protocol; it is on
# the input device, which is a rung-4 read.
MOUSE_CLICK_NO_EVDEV = (
    "behave mouse-click is not yet wired here: a button release wdotool did not itself inject is not in any "
    "window protocol -- the route is an evdev read on /dev/input (AGENTS.md route 4, the same device access "
    "the uinput typing path already takes), else the GNOME bridge's captured-event signal (route 3); until "
    "one is wired the click event is a gap, not a decline")


def _current_focus(ctx) -> int:
    """The window id that has focus now, or 0 -- the seed for focus/blur, so the first `focus` event that names
    another window is a blur of this one and not a spurious focus of it."""
    for w in ctx.backend().list():
        if w.focused:
            return w.id
    return 0


def _behave_focus_loop(ctx, targets, event, action, events_iter, focused):
    """`focus`/`blur` from the backend's event stream: every backend's `events()` yields `focus` on the rising
    edge (the window that just took focus), so a `focus` naming another window is the blur of whoever held it.

    xdotool subscribes both names to the one FocusChangeMask and runs the action on the watched window on BOTH
    its FocusIn and its FocusOut regardless of which name was asked (`cmd_behave.c` lines 10-11, 124-127), so
    `behave W focus cmd` also fires when W loses focus and `behave W blur cmd` also fires when it gains it.  We
    reproduce that (AGENTS.md: the originals' bugs are features), so `event` picks nothing here -- a target that
    either takes or loses focus runs the action, against that target.  Never returns on a live stream; a finite
    test stream ends it."""
    for wid, change in events_iter:
        if change != "focus":
            continue
        prev = focused
        focused = wid
        if wid in targets and wid != prev:      # a target took focus (FocusIn)
            cli.run_behave_action(ctx, action, wid)
        if prev in targets and prev != wid:     # a target lost focus (FocusOut)
            cli.run_behave_action(ctx, action, prev)


def _behave_mouse_loop(ctx, targets, event, action, source, list_fn,
                       poll=0.02, sleep=time.sleep):
    """`mouse-enter`/`mouse-leave` from a pointer poll hit-tested against `list()` -- `backend.hit_test` is the
    one rule the desktop uses everywhere.  Seeds each target's inside/outside from the first sample so a window
    the pointer already sits in does not fire an enter.  `source()` raising StopIteration ends the loop (a test
    source); the real one polls without end."""
    inside = dict.fromkeys(targets)  # None until seeded
    while True:
        try:
            pos = source()
        except StopIteration:
            return
        cur = _wbackend.hit_test(list_fn(), pos[0], pos[1]) if pos else 0
        for t in targets:
            now_in = cur == t
            was = inside[t]
            if was is not None:
                if event == "mouse-enter" and now_in and not was:
                    cli.run_behave_action(ctx, action, t)
                elif event == "mouse-leave" and was and not now_in:
                    cli.run_behave_action(ctx, action, t)
            inside[t] = now_in
        sleep(poll)


def cmd_behave(ctx, args):
    # Help and a wrong argument count are answered before anything else: they are ours to get right.
    cmd = getattr(ctx, "cmd_name", "behave")
    usage = _USAGE_BEHAVE % cmd
    parsed = _opts(cmd, args, "h", [("help", False)], usage)
    if parsed is None:
        return len(args)
    _o, nopts = parsed
    if len(args) - nopts < 3:
        raise CmdError("Invalid number of arguments (minimum is 3)\n" + usage.rstrip("\n"))
    window_arg, event = args[nopts], args[nopts + 1]
    action = list(args[nopts + 2:])
    if event not in _BEHAVE_EVENTS:
        raise CmdError("Unknown event name: %s\n%s" % (event, usage.rstrip("\n")))
    if event == "mouse-click":
        raise CmdError(MOUSE_CLICK_NO_EVDEV)
    targets = ctx.resolve_windows(window_arg)
    if event in ("focus", "blur"):
        _behave_focus_loop(ctx, targets, event, action,
                           ctx.backend().events(), _current_focus(ctx))
    else:
        from wdotool.input_cmds import _edge_sample
        _behave_mouse_loop(ctx, targets, event, action,
                           lambda: _edge_sample(ctx), ctx.backend().list)
    return len(args)
