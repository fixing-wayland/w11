"""Input-injection commands: key/keydown/keyup/type, mouse, getmouselocation.

Command functions follow the ctx.py contract: consume their own flags and positionals from `args`, return the
token count consumed (excluding the command name), raise CmdError to abort the chain.
"""

import math
import sys
import time

from w11common.errors import CmdError
from hacks.input.edges import EDGES as _EDGES, EdgeMachine
from hacks.window import backend as _backend
from wdotool import cli, commands
from wdotool.cli import ChainAbort, _opts
from wdotool.cnum import atoi as _atoi, strtol as _strtonum

# ---------------------------------------------------------------------------
# option parsing, via cli.py's glibc getopt_long_only clone


def _parse(cmdname, args, usage, shortopts, longopts, shortmap=None):
    """cli._opts with the options as a dict, which is how the input commands read them: longopts is [(name,
    takes_arg), ...] and shortmap maps short chars to the long name used in the dict. Returns (opts, tokens
    consumed, help_requested); when help was requested, usage has already been printed and opts is empty."""
    parsed = _opts(cmdname, args, shortopts, longopts, usage, shortmap)
    if parsed is None:
        return {}, len(args), True
    pairs, i = parsed
    return {name: True if val is None else val for name, val in pairs}, i, False


def _activate_settle(ctx, wid):
    """Approximation of send-to-window: activate the target, give the
    compositor a moment to move focus, then inject normally."""
    ctx.backend().activate(wid)
    time.sleep(0.05)


def _backend_pointer_ex(ctx):
    """(position, why-not): the compositor's real pointer position, or None with the reason it is None.

    Two very different Nones live here and the refusal in _pointer has to tell them apart. A compositor with no
    pointer query at all -- sway and i3, whose IPC carries no cursor position -- gives (None, None): there is
    nothing to report and nothing to fix. A compositor that HAS one which failed gives (None, exc), and that
    exception is the message every other command in the chain prints for the same session: on GNOME 51.beta as
    shipped the bridge is marked out of date for the running shell and `wdotool search` says so, so
    getmouselocation there has to say so too rather than lecture a GNOME user about zwlr_virtual_pointer_v1.
    `ctx.backend()` raising (no session at all) is in the second group for the same reason: its message is the
    accurate one. Callers that only want the position keep using _backend_pointer()."""
    try:
        fn = getattr(ctx.backend(), "pointer", None)
        if fn is None:
            return None, None
        hit = fn()
    except CmdError as e:
        return None, e
    if hit is None:
        return None, None
    return (int(hit[0]), int(hit[1])), None


def _backend_pointer(ctx):
    """The compositor's real pointer position, or None when this compositor
    has no pointer query (sway/i3 IPC has none) or there is no session."""
    return _backend_pointer_ex(ctx)[0]


def _pointer_opt(ctx, seed=True):
    """_pointer(), for the callers that only want the answer if there is one.

    `mousemove` and `mousemove_relative` ask where the pointer is before they move it -- the first to remember
    it for `mousemove restore`, the second to count the delta from the real position (B1/B6). Neither NEEDS the
    answer: an absolute move states its own destination, and a relative one is a delta the compositor applies
    wherever the cursor happens to be. On sway with no /dev/uinput -- the whole point of the virtual-pointer
    path -- there IS no answer (zwlr_virtual_pointer_v1 has no events), so a query that raises would fail the
    move itself and leave the pointer commands as unusable as they were before the protocol. So: ask, and shrug
    if the answer is that nobody knows. The real failure, if there is one, still comes out of the move.
    """
    try:
        return _pointer(ctx, seed)
    except CmdError:
        return None


def _pointer(ctx, seed=True):
    """Where the pointer really is (B6).

    The input daemon only knows the last position *it* injected; REL events, a physical mouse, another daemon
    (one per euid and per XDG_RUNTIME_DIR) or the compositor itself move the pointer behind its back, and a
    daemon that has just started knows nothing at all. So ask the compositor first and, when it answers, correct
    the daemon's model from it so a following mousemove_relative counts from the real position (B1). Compositors
    without a pointer query keep the tracked model."""
    real, why = _backend_pointer_ex(ctx)
    if real is None:
        x, y, known = ctx.daemon().pointer()
        if not known:
            # The compositor could not be asked and the daemon has moved nothing: 0,0 is the tablet's own
            # untouched axis state, not a pointer position. Reporting it as one is the defect -- `wdotool
            # getmouselocation` printed `x:0 y:0 screen:0 window:0` with rc 0 on a fresh sway daemon while
            # the cursor sat wherever sway had put it, and docs/WDOTOOL.md has always promised a refusal.
            # The daemon raises this itself where /dev/uinput is unopenable; here it is the other half, the
            # one where the devices opened fine and nobody has yet told anyone where the pointer is.
            # `why` is the compositor's own reason when it had a query that failed (the out-of-date bridge on
            # GNOME 51, KWin's scripting service refusing): re-raised as it came, so the exit code a
            # NoSessionError carries survives too, and so the answer names the thing the user can act on.
            if why is not None:
                raise why
            # Local, like ctx.daemon()'s own import of the same module two frames up: importing daemon.py at
            # module scope pulls keymap/keystate/layoutbox/uinput/vkbd/vptr/xkbmap in behind it, 39 ms
            # measured here, onto every `wdotool search` that never goes near a daemon. By this line the
            # module is in sys.modules regardless -- ctx.daemon() just imported it.
            from wdotool.daemon import POINTER_UNKNOWN
            raise CmdError(POINTER_UNKNOWN)
        return (x, y)
    if seed:
        try:
            ctx.daemon().seed_pointer(*real)
        except CmdError:
            pass  # no daemon available: the compositor's reading still stands
    return real


def _target_windows(ctx, window_arg):
    """Resolved window list for a --window value, or [None] for 'current'."""
    if window_arg is None:
        return [None]
    return ctx.resolve_windows(window_arg)


# --clearmodifiers travels *with* the injection request (`clearmods=`), never as a clear call, an injection and
# a restore call: the daemon does all three under one hold of its injection lock, so a second wdotool process
# cannot land an injection in between, with the modifiers down or across the restore. What it can put back is
# what wdotool itself was holding -- see the daemon's "modifiers around an injection" note for why a modifier
# held on a physical keyboard can be neither released nor safely pressed back from here; the daemon says so,
# once per command, when it can tell.
#
# The whole command travels as ONE `key_batch`/`type_batch` request -- every key sequence, every `--repeat`
# pass, every `type` argument -- so that single lock hold covers all of it, which is what cmd_key.c and
# cmd_type.c do with their one clear before the loop and one restore after it. One request per sequence
# put the modifiers back at the end of the first one and sent the rest with them down.


# ---------------------------------------------------------------------------
# keyboard

_USAGE_KEY = """Usage: %s [options] <keysequence> [keysequence ...]
--clearmodifiers     - clear active keyboard modifiers during keystrokes
--delay DELAY        - Use DELAY milliseconds between keystrokes
--repeat TIMES       - How many times to repeat the key sequence
--repeat-delay DELAY - DELAY milliseconds between repetitions
--window WINDOW      - send keystrokes to a specific window
Each keysequence can be any number of modifiers and keys, separated by plus (+)
  For example: alt+r

Any letter or key symbol such as Shift_L, Return, Dollar, a, space are valid,
including those not currently available on your keyboard.

If no window is given, and there are windows in the stack, %%1 is used. Otherwise
the currently-focused window is used
"""


def _key_common(ctx, args, default_name, direction):
    cmdname = getattr(ctx, "cmd_name", default_name)
    usage = _USAGE_KEY % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "d:hcw:",
        [("clearmodifiers", False), ("delay", True), ("repeat-delay", True),
         ("help", False), ("window", True), ("repeat", True)],
        {"c": "clearmodifiers", "d": "delay", "h": "help", "w": "window"},
    )
    if want_help:
        return len(args)
    delay = _strtonum(opts.get("delay", 12))
    repeat = _atoi(opts.get("repeat", 1))
    if "repeat" in opts and repeat < 1:
        raise CmdError(f"Invalid '--repeat' value given: {opts['repeat']}")
    # `--repeat-delay` is a strtol, so it takes a negative just as xdotool's does; xdotool then sleeps only
    # `if (repeat_delay > 0 && ...)`, i.e. never, and exits 0. Clamping here is that same no-sleep: the value
    # now crosses the socket, and the daemon's [0, 300000] bound on it (_num) would otherwise turn a command
    # X accepts into rc 1 with nothing injected.
    repeat_delay = max(0, _strtonum(opts.get("repeat-delay", 0)))
    window_arg = opts.get("window")
    if window_arg is None and ctx.stack:
        window_arg = "%1"

    if i == len(args):
        raise CmdError("You specified the wrong number of args.\n" + usage.rstrip("\n"))

    seqs = []
    j = i
    while j < len(args) and not commands.is_command(args[j]):
        seqs.append(args[j])
        j += 1

    daemon = ctx.daemon()
    failed = 0
    clearmods = bool(opts.get("clearmodifiers"))
    # xdo.c converts the key sequence to keycodes once per press/release pass: `key` runs both (down then up),
    # `keydown`/`keyup` only one. Every failing pass prints BOTH diagnostics and adds 1 to the command's exit
    # status, so `key 'a b'` exits 2 with five stderr lines while `keydown 'a b'` exits 1 with three (B12).
    passes = 2 if direction == "press" else 1
    for wid in _target_windows(ctx, window_arg):
        if wid is not None:
            _activate_settle(ctx, wid)
        # `_mode_kw` carries only the mode kwargs `--layout`/`--vkbd` actually gave, so an older daemon and
        # every test double keep their existing signature.
        #
        # Nothing to inject -- `key <next-command>` names no sequence at all -- is a no-op with rc 0, which is
        # what `xdotool key getdisplaygeometry` does (measured on Xvfb, 2026-09-16: it prints the geometry and
        # exits 0). The empty list is never put on the wire: the daemon answers a batch request that names
        # nothing with {"ok": false}, which would make this exit 1. The activation above still happens,
        # exactly as xdotool still walks its window list.
        results = daemon.key_batch(seqs, direction, delay, repeat, repeat_delay,
                                   clearmods, **_mode_kw(ctx)) if seqs else []
        for item in results:
            for warning in item.get("warnings") or []:
                print(warning, file=sys.stderr)
            err = item.get("error")
            if err is None:
                continue
            if not err.startswith("Error: Invalid key sequence"):
                raise CmdError(err)
            seq = item.get("spec", "")
            for _ in range(passes):
                print(err, file=sys.stderr)
                print("Failure converting key sequence '%s' to keycodes" % seq, file=sys.stderr)
            print(f"xdo_send_keysequence_window reported an error for string '{seq}'",
                  file=sys.stderr)
            failed += passes
    if failed:
        # real xdotool aborts the chain, exiting with the failure count (the C
        # code sums the per-sequence keyfunc results and returns it)
        raise ChainAbort(failed)
    return j


def cmd_key(ctx, args):
    return _key_common(ctx, args, "key", "press")


def cmd_keydown(ctx, args):
    return _key_common(ctx, args, "keydown", "down")


def cmd_keyup(ctx, args):
    return _key_common(ctx, args, "keyup", "up")


_USAGE_TYPE = """Usage: %s [--window windowid] [--delay milliseconds] <things to type>
--window <windowid>    - specify a window to send keys to
--delay <milliseconds> - delay between keystrokes
--clearmodifiers       - reset active modifiers (alt, etc) while typing
--args N  - how many arguments to expect in the exec command. This is
            useful for ending an exec and continuing with more xdotool
            commands
--terminator TERM - similar to --args, specifies a terminator that
                    marks the end of 'exec' arguments. This is useful
                    for continuing with more xdotool commands.
--file <filepath> - specify a file, the contents of which will be
                    be typed as if passed as an argument. The filepath
                    may also be '-' to read from stdin.
-h, --help             - show this help output
If no window is given, %%1 is used. See WINDOW STACK in xdotool(1)
"""


def _mode_kw(ctx):
    """`layout_mode=`/`vkbd_mode=` for the daemon call, each only when its flag was given: an absent flag must
    leave the request exactly as it was, so an older daemon and every test double keep their signature."""
    kw = {}
    for attr in ("layout_mode", "vkbd_mode"):
        mode = getattr(ctx, attr, None)
        if mode:
            kw[attr] = mode
    return kw


def _vkbd_kw(ctx):
    """The same, for the pointer commands: `--vkbd` selects which pointer injects as well as which keyboard (one
    switch, one decision -- see the daemon's POLICY note), while `--layout` is a keyboard-only question and has
    no meaning here."""
    mode = getattr(ctx, "vkbd_mode", None)
    return {"vkbd_mode": mode} if mode else {}


def _stdin_text() -> str:
    """`type --file -`: everything on stdin, as text.

    Decoded with errors="replace", exactly like the `--file PATH` branch beside it: xdotool types what it is
    given, and a byte that is not UTF-8 is no reason to end in a traceback -- which is what `sys.stdin.read()`
    did, since its own decoder is strict under a UTF-8 locale (and only quietly surrogate-escaping under
    LANG=C).  The bytes come from the buffer under the text layer where there is one; a test double or an
    embedding caller may hand us a text stream with none. An fd 0 that was closed before we started
    (`--file - <&-`) leaves sys.stdin None and types nothing, as the C fread() does."""
    stdin = getattr(sys, "stdin", None)
    if stdin is None:
        return ""
    buf = getattr(stdin, "buffer", None)
    if buf is None:
        return stdin.read()
    return buf.read().decode("utf-8", "replace")


def cmd_type(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "type")
    usage = _USAGE_TYPE % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "w:d:ch",
        [("clearmodifiers", False), ("delay", True), ("help", False),
         ("window", True), ("args", True), ("terminator", True), ("file", True)],
        {"c": "clearmodifiers", "d": "delay", "h": "help", "w": "window"},
    )
    if want_help:
        return len(args)
    delay = _strtonum(opts.get("delay", 12))
    window_arg = opts.get("window")
    arity = _atoi(opts["args"]) if "args" in opts else -1
    terminator = opts.get("terminator")
    file = opts.get("file")
    remaining = args[i:]

    if not remaining and file is None:
        raise CmdError("You specified the wrong number of args.\n" + usage.rstrip("\n"))
    if arity > 0 and terminator is not None:
        raise CmdError("Don't use both --terminator and --args.")
    if len(remaining) < arity:
        raise CmdError(f"You said '--args {arity}' but only gave {len(remaining)} arguments.")

    data = []
    if file is not None:
        try:
            if file == "-":
                data.append(_stdin_text())
            else:
                with open(file, "r", encoding="utf-8", errors="replace") as f:
                    data.append(f.read())
        except OSError as e:
            raise CmdError(f"Failure opening '{file}': {e.strerror}") from None

    consumed = 0
    for idx, arg in enumerate(remaining):
        if 0 < arity == idx:
            break
        if terminator is not None and arg == terminator:
            consumed += 1  # consume the terminator, too
            break
        data.append(arg)
        consumed += 1

    daemon = ctx.daemon()
    clearmods = bool(opts.get("clearmodifiers"))
    for wid in _target_windows(ctx, window_arg):
        if wid is not None:
            _activate_settle(ctx, wid)
        # `type --terminator END END` collects no pieces: nothing typed, rc 0, and no request (see
        # _key_common; the daemon refuses an empty batch rather than treating it as a no-op).
        if data:
            daemon.type_batch(data, delay, clearmods=clearmods, **_mode_kw(ctx))
    return i + consumed


# ---------------------------------------------------------------------------
# mouse

_USAGE_CLICK = """Usage: %s [options] <button>
--clearmodifiers       - reset active modifiers (alt, etc) while typing
--window WINDOW        - specify a window to send click to
--repeat REPEATS       - number of times to click. Default is 1
--delay MILLISECONDS   - delay in milliseconds between clicks.
    This has no effect if you do not use --repeat.
    Default is 100ms

Button is a button number. Generally, left = 1, middle = 2, 
right = 3, wheel up = 4, wheel down = 5
"""


def cmd_click(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "click")
    usage = _USAGE_CLICK % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "cw:h",
        [("clearmodifiers", False), ("help", False), ("window", True),
         ("delay", True), ("repeat", True)],
        {"c": "clearmodifiers", "w": "window", "h": "help"},
    )
    if want_help:
        return len(args)
    clearmods = bool(opts.get("clearmodifiers"))
    window_arg = opts.get("window")
    if window_arg is not None:
        clearmods = True  # quirk copied from cmd_click.c
    delay = _strtonum(opts.get("delay", 100))
    repeat = _atoi(opts.get("repeat", 1))
    if "repeat" in opts and repeat <= 0:
        raise CmdError(f"Invalid repeat value '{opts['repeat']}' (must be >= 1)\n" + usage.rstrip("\n"))
    if i >= len(args):
        raise CmdError(usage.rstrip("\n") + "\nYou specified the wrong number of args.")
    button = _atoi(args[i])

    daemon = ctx.daemon()
    for wid in _target_windows(ctx, window_arg):
        if wid is not None:
            _activate_settle(ctx, wid)
        daemon.click(button, repeat, delay, clearmods=clearmods, **_vkbd_kw(ctx))
    return i + 1


_USAGE_MOUSEBTN = """Usage: %s [--clearmodifiers] [--window WINDOW] <button>
--window <windowid>    - specify a window to send keys to
--clearmodifiers       - reset active modifiers (alt, etc) while typing
"""


def _mouse_updown(ctx, args, default_name, down, noargs_msg):
    cmdname = getattr(ctx, "cmd_name", default_name)
    usage = _USAGE_MOUSEBTN % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "chw:",
        [("clearmodifiers", False), ("help", False), ("window", True)],
        {"c": "clearmodifiers", "h": "help", "w": "window"},
    )
    if want_help:
        return len(args)
    if i >= len(args):
        raise CmdError(usage.rstrip("\n") + "\n" + noargs_msg)
    button = _atoi(args[i])

    daemon = ctx.daemon()
    for wid in _target_windows(ctx, opts.get("window")):
        if wid is not None:
            _activate_settle(ctx, wid)
        daemon.button(button, down, clearmods=bool(opts.get("clearmodifiers")), **_vkbd_kw(ctx))
    return i + 1


def cmd_mousedown(ctx, args):
    return _mouse_updown(ctx, args, "mousedown", True, "What button do you want me to send?")


def cmd_mouseup(ctx, args):
    return _mouse_updown(ctx, args, "mouseup", False, "You specified the wrong number of args.")


_USAGE_MOUSEMOVE = """Usage: %s [options] <x> <y>
-c, --clearmodifiers      - reset active modifiers (alt, etc) while typing
--screen SCREEN           - which screen to move on, default is current screen
--sync                    - only exit once the mouse has moved
-w, --window <windowid>   - specify a window to move relative to.
"""


def _polar_to_xy(angle, distance, origin_x, origin_y):
    radians = ((360 - angle) + 90) * math.pi / 180
    return (int(origin_x + math.cos(radians) * distance), int(origin_y + -math.sin(radians) * distance))


def cmd_mousemove(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "mousemove")
    usage = _USAGE_MOUSEMOVE % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "chw:pd:",
        [("clearmodifiers", False), ("help", False), ("polar", False),
         ("screen", True), ("sync", False), ("window", True)],
        {"c": "clearmodifiers", "h": "help", "w": "window", "p": "polar",
         "d": "_delay"},  # short -d parses (and is ignored) like the C code
    )
    if want_help:
        return len(args)

    if i >= len(args) or (args[i] != "restore" and len(args) - i < 2):
        raise CmdError(
            usage.rstrip("\n")
            + "\nYou specified the wrong number of args (expected 2 coordinates or 'restore')."
        )

    if args[i] == "restore":
        last = getattr(ctx, "_last_mouse", None)
        if last is None:
            raise CmdError("Have no previous mouse position. Cannot restore.")
        x, y = last
        consumed = 1
    else:
        x, y = _atoi(args[i]), _atoi(args[i + 1])
        consumed = 2

    window_arg = opts.get("window")
    daemon = ctx.daemon()
    for wid in _target_windows(ctx, window_arg):
        if wid is None:
            # `mousemove restore` needs this; the move itself does not, so a
            # compositor that cannot be asked must not fail it (_pointer_opt).
            ctx._last_mouse = _pointer_opt(ctx)  # restore state
        tx, ty = x, y
        if opts.get("polar"):
            if wid is not None:
                win = ctx.backend().find(wid)
                origin = (win.x + win.w // 2, win.y + win.h // 2)
            else:
                gx, gy, gw, gh = daemon.geometry_full()
                origin = (gx + gw // 2, gy + gh // 2)
            tx, ty = _polar_to_xy(x, y, *origin)
        elif wid is not None:
            win = ctx.backend().find(wid)
            tx, ty = win.x + x, win.y + y
        daemon.mousemove_abs(tx, ty, clearmods=bool(opts.get("clearmodifiers")), **_vkbd_kw(ctx))
        # --sync: our injected position is authoritative, nothing to wait for
    return i + consumed


_USAGE_MOUSEMOVE_REL = """Usage: %s [options] <x> <y>
-c, --clearmodifiers      - reset active modifiers (alt, etc) while typing
-p, --polar               - Use polar coordinates. X as an angle, Y as distance
--sync                    - only exit once the mouse has moved

Using polar coordinate mode makes 'x' the angle (in degrees) and
'y' the distance.

If you want to use negative numbers for a coordinate, you'll need to
invoke it this way (with the '--'):
   %s -- -20 -15
otherwise, normal usage looks like this:
   %s 100 140
"""


def cmd_mousemove_relative(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "mousemove_relative")
    usage = _USAGE_MOUSEMOVE_REL % (cmdname, cmdname, cmdname)
    opts, i, want_help = _parse(
        cmdname, args, usage, "cph",
        [("help", False), ("sync", False), ("polar", False),
         ("clearmodifiers", False)],
        {"c": "clearmodifiers", "p": "polar", "h": "help"},
    )
    if want_help:
        return len(args)
    if len(args) - i < 2:
        raise CmdError(usage.rstrip("\n") + "\nYou specified the wrong number of args (expected 2).")
    x, y = _atoi(args[i]), _atoi(args[i + 1])
    if x == 0 and y == 0:
        return i + 2
    if opts.get("polar"):
        x, y = _polar_to_xy(x, y, 0, 0)
    # B1/B6: move from where the pointer really is. On a compositor that can be asked, this also decides
    # pixel-exactness: the daemon then emits the target as an absolute warp instead of REL events that
    # libinput's acceleration curve would scale. Outside the clearmodifiers window: it is a query, not part of
    # the injection -- and an optional one: where nothing can answer it (sway, no /dev/uinput), the delta still
    # moves the cursor the compositor is holding.
    _pointer_opt(ctx)
    ctx.daemon().mousemove_rel(x, y, clearmods=bool(opts.get("clearmodifiers")), **_vkbd_kw(ctx))
    return i + 2


_USAGE_GETMOUSELOCATION = """Usage: %s [--shell] [--prefix <STR>]
--shell      - output shell variables for use with eval
--prefix STR - use prefix for shell variables names (max 16 chars) 
"""


def _window_under_pointer(ctx, x, y) -> int:
    """Hit-test the daemon-tracked pointer against the backend's window list; 0 with no backend, or with nothing
    under the point. backend.hit_test() is the one rule -- the backends that can name a window_type (GNOME,
    KWin) have their desktop-icon and dock layers looked through by it."""
    try:
        wins = ctx.backend().list()
    except Exception:
        return 0
    return _backend.hit_test(wins, x, y)


def cmd_getmouselocation(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "getmouselocation")
    usage = _USAGE_GETMOUSELOCATION % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "h",
        [("help", False), ("shell", False), ("prefix", True)],
        {"h": "help"},
    )
    if want_help:
        return len(args)
    x, y = _pointer(ctx)
    window = _window_under_pointer(ctx, x, y)
    if opts.get("shell"):
        prefix = str(opts.get("prefix", ""))[:16]
        print(f"{prefix}X={x}")
        print(f"{prefix}Y={y}")
        print(f"{prefix}SCREEN=0")
        print(f"{prefix}WINDOW={window}")
    else:
        if i == len(args):  # only print if we're the last command
            print(f"x:{x} y:{y} screen:0 window:{window}")
        ctx.stack = [window]
    return i


_USAGE_BEHAVE_SCREEN_EDGE = """Usage: %s [options] edge-or-corner action [args...]
--delay MILLISECONDS     - delay before activating. During this time,
        your mouse must stay in the area selected (corner or edge)
        otherwise this timer will reset. Default is no delay (0).
--quiesce MILLISECONDS   - quiet time period after activating that no
        new activation will occur. This helps prevent accidental
        re-activation immediately after an event. Default is 2000 (2
        seconds).
edge-or-corner can be any of:
  Edges: left, top, right, bottom
  Corners: top-left, top-right, bottom-left, bottom-right
The action is any valid xdotool command (chains OK here)
"""

# The one sentence for a backend with no pointer source at all: sway/wlroots/COSMIC, whose IPC/toplevel
# protocols carry no cursor position, on a daemon that has injected no motion. Named with its rung so it is a
# gap and not a refusal of the feature -- test_one_rule pins it. The waycorner shape is the route: a 1-px
# zwlr_layer_shell_v1 strip per edge/corner whose `wl_pointer.enter` IS the edge crossing, which needs no
# pointer poll at all; the cost is a surface that eats a click landing on that line.
EDGE_NO_POINTER = (
    "behave_screen_edge has no pointer to poll here: this backend publishes no cursor position and wdotool "
    "has moved none, so the edge is not yet watched here by polling.  The route is a 1-px zwlr_layer_shell_v1 "
    "strip bound per edge and corner, whose wl_pointer.enter is the edge crossing (AGENTS.md route 1, the "
    "waycorner shape), at the cost of a surface that eats a click on that line")


def _screen_box(ctx) -> "tuple[int, int, int, int]":
    """The layout bounding box (x, y, w, h) the edge is measured against, the way `getdisplaygeometry` finds
    the size: the daemon's wl_output query first, then the window backend, then xdotool's 1920x1080 last resort
    (an edge on the wrong box is still an edge, so this warns nowhere and never fails).

    Only the daemon rung carries an origin, and it must: the samples fed to `EdgeMachine` are global layout
    coordinates (`_edge_sample` below returns the compositor's own pointer, or the daemon's tracked px/py,
    both of which the daemon clamps into `[gx, gx+w-1]`), and on a layout laid out as -1920..1919 a box
    anchored at 0 puts `right` on a column that does not exist and `left` across the whole left-hand head.
    The two fallback rungs answer with a size alone -- neither `display_size()` nor xdotool's 1920x1080 has
    an origin to keep -- so they stay anchored at 0 rather than inventing one."""
    try:
        gx, gy, w, h, guessed = ctx.daemon().geometry_box_status()
        if not guessed:
            return (gx, gy, w, h)
    except (CmdError, OSError):
        pass
    try:
        w, h = ctx.backend().display_size()
        return (0, 0, w, h)
    except (CmdError, OSError):
        return (0, 0, 1920, 1080)


def _edge_sample(ctx):
    """One pointer sample for the edge watch: the compositor's own query where it has one, else the daemon's
    tracked position when it knows one, else None (unknown -- not on any edge)."""
    real = _backend_pointer(ctx)
    if real is not None:
        return real
    x, y, known = ctx.daemon().pointer()
    return (x, y) if known else None


def _behave_edge_loop(ctx, source, machine, action, poll=0.02,
                      clock=time.monotonic, sleep=time.sleep):
    """Feed `source()` samples into `machine` for ever, running `action` on every sample it fires. `source()`
    raising StopIteration ends the loop -- which only a finite test source does; the real one polls the pointer
    without end, the way `xdotool behave_screen_edge` runs until it is killed."""
    while True:
        try:
            pos = source()
        except StopIteration:
            return
        x, y = pos if pos else (None, None)
        if machine.feed(x, y, clock()):
            cli.run_behave_action(ctx, action)
        sleep(poll)


def cmd_behave_screen_edge(ctx, args):
    cmdname = getattr(ctx, "cmd_name", "behave_screen_edge")
    usage = _USAGE_BEHAVE_SCREEN_EDGE % cmdname
    opts, i, want_help = _parse(
        cmdname, args, usage, "h",
        [("help", False), ("delay", True), ("quiesce", True)],
        {"h": "help"},
    )
    if want_help:
        return len(args)
    if len(args) - i < 2:
        raise CmdError("Invalid number of arguments (minimum is 2)\n" + usage.rstrip("\n"))
    if args[i] not in _EDGES:
        raise CmdError(f"Invalid edge or corner, '{args[i]}'\n" + usage.rstrip("\n"))
    edge, action = args[i], list(args[i + 1:])
    delay = _atoi(opts.get("delay", "0"))
    quiesce = _atoi(opts["quiesce"]) if "quiesce" in opts else 2000
    if _edge_sample(ctx) is None:
        raise CmdError(EDGE_NO_POINTER)
    machine = EdgeMachine(edge, _screen_box(ctx), delay, quiesce)
    _behave_edge_loop(ctx, lambda: _edge_sample(ctx), machine, action)
    return len(args)
