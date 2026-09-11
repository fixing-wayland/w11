"""Command-line driver: option handling, command chaining, script mode.

Mirrors xdotool.c (xdotool_main / args_main / script_main / context_execute): same messages, same exit codes,
same argument-consumption accounting.
"""

import io
import os
import sys

from w11common import passthrough, stdio
from w11common.errors import CmdError
from wdotool import backend, commands
from wdotool.cnum import atoi as _atoi
from wdotool.ctx import Context

# What `version`/-v prints. Must match the real xdotool byte-for-byte so
# version-sniffing scripts keep working: it is xdotool's version, not ours.
XDO_VERSION = "4.20260303.1"

_USAGE = "Usage: %s <cmd> <args>\n"


class ChainAbort(Exception):
    """Abort the command chain with a specific exit code and no CmdError-style message (e.g. `exec --sync`
    propagating the child's exit status). If msg is given, it is printed to stderr first."""

    def __init__(self, code: int, msg: str | None = None):
        super().__init__(msg or f"exit {code}")
        self.code = code
        self.msg = msg


class GetoptError(Exception):
    """str() is the glibc-formatted error line (no trailing newline). .opts holds the options parsed before the
    failure: getopt processes options one at a time, so callers must honor an already-seen --help before
    reporting the error, exactly like the C loops do."""

    def __init__(self, msg: str, opts: list):
        super().__init__(msg)
        self.opts = opts


def _short_spec(shortopts: str, c: str):
    """None if c is not a short option, else whether it takes an argument."""
    k = shortopts.find(c)
    if k < 0 or c == ":":
        return None
    return k + 1 < len(shortopts) and shortopts[k + 1] == ":"


def _long_opt(cmd, args, i, tok, body, longopts, opts):
    """Try args[i] (tok, body = tok without dashes) as a long option. Returns the next argv index, or None if
    nothing matches. Raises GetoptError for ambiguity and argument errors."""
    name, eq, val = body.partition("=")
    matches = [lo for lo in longopts if lo[0] == name] or [lo for lo in longopts if lo[0].startswith(name)]
    if not matches:
        return None
    if len(matches) > 1:
        poss = "".join(" '--%s'" % m[0] for m in matches)
        raise GetoptError("%s: option '%s' is ambiguous; possibilities:%s" % (cmd, tok, poss), opts)
    cname, takes = matches[0]
    if eq:
        if not takes:
            raise GetoptError("%s: option '--%s' doesn't allow an argument" % (cmd, cname), opts)
        opts.append((cname, val))
        return i + 1
    if takes:
        if i + 1 >= len(args):
            raise GetoptError("%s: option '--%s' requires an argument" % (cmd, cname), opts)
        opts.append((cname, args[i + 1]))
        return i + 2
    opts.append((cname, None))
    return i + 1


def _short_opts(cmd, args, i, body, shortopts, opts):
    j = 0
    while j < len(body):
        c = body[j]
        takes = _short_spec(shortopts, c)
        if takes is None:
            raise GetoptError("%s: invalid option -- '%c'" % (cmd, c), opts)
        if takes:
            if j + 1 < len(body):
                opts.append((c, body[j + 1 :]))
                return i + 1
            if i + 1 >= len(args):
                raise GetoptError("%s: option requires an argument -- '%c'" % (cmd, c), opts)
            opts.append((c, args[i + 1]))
            return i + 2
        opts.append((c, None))
        j += 1
    return i + 1


def getopt_long_only(cmd, args, shortopts, longopts):
    """glibc getopt_long_only clone in POSIX mode ("+..."): stops at the first non-option token. args excludes
    the command name; longopts is a list of (name, takes_arg). Returns (opts, ntokens) where opts is
    [(canonical_long_name_or_short_char, value_or_None), ...] in argv order and ntokens is how many leading
    tokens were consumed (including a "--"). Single-dash long options and unambiguous abbreviations work, as in
    C."""
    opts: list = []
    i, n = 0, len(args)
    while i < n:
        tok = args[i]
        if tok == "--":
            i += 1
            break
        if len(tok) < 2 or tok[0] != "-":
            break
        if tok[1] == "-":
            j = _long_opt(cmd, args, i, tok, tok[2:], longopts, opts)
            if j is None:
                raise GetoptError("%s: unrecognized option '%s'" % (cmd, tok), opts)
            i = j
        else:
            body = tok[1:]
            spec = _short_spec(shortopts, body[0])
            j = None
            if len(body) > 1 or spec is None:
                j = _long_opt(cmd, args, i, tok, body, longopts, opts)
            if j is not None:
                i = j
            elif spec is None:
                raise GetoptError("%s: unrecognized option '%s'" % (cmd, tok), opts)
            else:
                i = _short_opts(cmd, args, i, body, shortopts, opts)
    return opts, i


def _print_help(out=None):
    out = out or sys.stdout
    out.write("Available commands:\n")
    for name in commands.REGISTRY:
        out.write("  %s\n" % name)
    out.flush()


def _print_version():
    sys.stdout.write("xdotool version %s\n" % XDO_VERSION)
    sys.stdout.flush()


def cmd_help(ctx, args):
    _print_help()
    return 0


def cmd_version(ctx, args):
    _print_version()
    return 0


def run_chain(ctx: Context, prog: str, tokens: list[str]) -> int:
    """xdotool.c context_execute: dispatch commands until argv is consumed or
    one fails. Returns the abort code (0 = whole chain ran)."""
    debug = os.environ.get("DEBUG") is not None
    i, ret = 0, 0
    while i < len(tokens) and ret == 0:
        name = tokens[i]
        fn = commands.lookup(name)
        if fn is None:
            sys.stderr.write("%s: Unknown command: %s\n" % (prog, name))
            sys.stderr.write("Run '%s help' if you want a command list\n" % prog)
            return 1
        if debug:
            sys.stderr.write("command: %s\n" % name)
        args = tokens[i + 1 :]
        ctx.cmd_name = name  # as typed; commands use it in usage text
        ctx.cmd_usage = None  # set by window_get_arg(); never stale across links
        try:
            n = fn(ctx, args)
        except CmdError as e:
            sys.stderr.write("%s\n" % e)
            # NoSessionError carries rc 2 ("no Wayland session found"); every
            # other failure keeps xdotool's rc 1 (B5).
            return getattr(e, "exit_code", 1) or 1
        except ChainAbort as e:
            if e.msg:
                sys.stderr.write("%s\n" % e.msg)
            return e.code
        if not isinstance(n, int) or n < 0 or n > len(args):
            sys.stderr.write(
                "Can't consume %s args; are only %d available. This is a bug.\n"
                % (n, len(args))
            )
            n = len(args)
        i += 1 + n
    return ret


def _opts(cmd, args, shortopts, longopts, usage, shortmap=None, invalid_usage=False):
    """Leading-option parse via getopt_long_only, the way every command wants it. Returns (opts, nopts) with
    short chars canonicalized through shortmap, or None after printing usage for --help (caller returns
    len(args)). Bad options raise CmdError carrying getopt's message + usage, like the C default: branches.
    `usage` is written verbatim, so it ends in a newline.

    `invalid_usage` adds the extra "Invalid usage" line that cmd_search.c -- alone among the commands -- prints
    between the two (B14)."""
    shortmap = dict(shortmap or ())
    shortmap.setdefault("h", "help")
    try:
        raw, nopts = getopt_long_only(cmd, args, shortopts, longopts)
    except GetoptError as e:
        if any(shortmap.get(n, n) == "help" for n, _ in e.opts):
            sys.stdout.write(usage)
            sys.stdout.flush()
            return None
        head = "%s\nInvalid usage" % e if invalid_usage else str(e)
        raise CmdError("%s\n%s" % (head, usage.rstrip("\n"))) from None
    opts = [(shortmap.get(n, n), v) for n, v in raw]
    if any(n == "help" for n, _ in opts):
        sys.stdout.write(usage)
        sys.stdout.flush()
        return None
    return opts, nopts


class _ScriptError(Exception):
    pass


def _script_line_tokens(line: str, argv: list[str], prog: str) -> list[str]:
    """Tokenize one script line exactly like script_main in xdotool.c:
    whitespace-separated tokens, leading '"' or '\\'' quotes a token, an
    unquoted token that starts with '#' ends the line as a comment (xdotool
    4.20260303.1 does this at any position; 3.2016 only honoured it first, and
    the parity oracle is the 4.x one), and a token beginning with '$' is
    replaced wholesale by a positional parameter ($N -> argv[N+1]) or an
    environment variable. Empty tokens are kept -- `echo A "" B` passes three
    arguments."""
    tokens: list[str] = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i] in " \t":
            i += 1
        if i >= n:
            break
        c = line[i]
        if c == "#":
            break
        if c in "\"'":
            i += 1
            j = line.find(c, i)
            if j == -1:
                j = n
            raw = line[i:j]
            i = j + 1
        else:
            j = i
            while j < n and line[j] not in " \t":
                j += 1
            raw = line[i:j]
            i = j + 1
        if raw.startswith("$"):
            name = raw[1:]
            # `in "0123456789"` is True for the empty string, so a bare "$" became "$0" -- the script path --
            # instead of the environment lookup that fails.
            if name[:1].isdigit():
                pos = _atoi(name) + 1  # $1 is argv[2]
                if pos >= len(argv):
                    sys.stderr.write(
                        "%s: error: `%s' needs at least %d %s; only %d given\n"
                        % (
                            prog,
                            argv[1],
                            pos - 1,
                            "argument" if pos == 2 else "arguments",
                            len(argv) - 2,
                        )
                    )
                    raise _ScriptError()
                token = argv[pos]
            else:
                token = os.environ.get(name)
                if token is None:
                    sys.stderr.write("%s: error: environment variable $%s is not set.\n" % (prog, name))
                    raise _ScriptError()
        else:
            token = raw
        tokens.append(token)
    return tokens


def script_main(argv: list[str], prog: str,
                layout_mode: str | None = None,
                vkbd_mode: str | None = None) -> int:
    """xdotool.c script_main: read commands from a file or stdin ("-"), expand $N/$ENV, and execute each line as
    a chain sharing one context. A failing line does not stop later lines; the last executed line's status
    wins."""
    path = argv[1]
    if path == "-":
        buf = getattr(sys.stdin, "buffer", None)
        f = io.TextIOWrapper(buf, errors="surrogateescape") if buf else sys.stdin
    else:
        try:
            f = open(path, "r", errors="surrogateescape")
        except IsADirectoryError:
            return 0  # glibc fopen(dir) succeeds, fgets sees EOF: empty script
        except OSError as e:
            sys.stderr.write("Failure opening '%s': %s\n" % (path, e.strerror))
            return 1
    ctx = Context()
    ctx.layout_mode = layout_mode
    ctx.vkbd_mode = vkbd_mode
    result = 0
    with f:
        for line in f:
            if line.endswith("\n"):
                line = line[:-1]
            try:
                tokens = _script_line_tokens(line, argv, prog)
            except _ScriptError:
                return 1
            if tokens:
                result = run_chain(ctx, prog, tokens)
    return result if result else ctx.exit_code


def _main(argv: list[str] | None = None) -> int:
    entry = argv is None
    argv = list(sys.argv) if argv is None else list(argv)

    # The console script entry point is cli:main, so route the daemon re-invocation here too (python -m wdotool
    # routes it in __main__.py). This is our own re-invocation of ourselves: never a passthrough.
    if len(argv) > 1 and argv[1] == "__daemon":
        from wdotool.daemon import daemon_main

        try:
            return daemon_main()
        except CmdError as e:
            sys.stderr.write("%s\n" % e)
            return 1

    # --layout: which character table the typing commands use, ahead of the WDOTOOL_LAYOUT environment variable.
    # `us` is the one that promises something: the compositor's keymap is not read and the bypass check does not
    # run, so no layout code executes at all. Ours, not xdotool's, so it is stripped here and never reaches a
    # command's own parser or the parity-checked usage text.
    #
    # It is a *leading* option, and the scan stops where xdotool's own getopt_long_only(argc, argv, "++hv", ...)
    # stops: at the first token that is not an option. Walking to the end of the command line ate the flag
    # wherever it appeared -- inside `exec` arguments, inside a script's positional parameters, and before the
    # X11 handover, so the real xdotool was handed a mangled argv.
    #
    # --vkbd: which devices the injecting commands go through -- the pointer ones as well as the typing ones,
    # because it is one decision and the daemon makes it the same way for both. Same shape and same place as
    # --layout, and documented next to it: `off` is the kernel device (/dev/uinput) whatever the compositor
    # offers, `on` is zwp_virtual_keyboard_v1 / zwlr_virtual_pointer_v1 or a clean error, `auto` is the default.
    _FLAGS = (("layout", "us, fixed, auto or xkb"), ("vkbd", "auto, on or off"))
    modes = {"layout": None, "vkbd": None}
    rest = []
    i = 1
    while i < len(argv):
        a = argv[i]
        hit = False
        for flag, valid in _FLAGS:
            if a == "--" + flag:
                if i + 1 >= len(argv):
                    sys.stderr.write("wdotool: --%s requires an argument "
                                     "(%s)\n" % (flag, valid))
                    return 1
                modes[flag] = argv[i + 1]
                i += 2
                hit = True
                break
            if a.startswith("--%s=" % flag):
                modes[flag] = a.split("=", 1)[1]
                i += 1
                hit = True
                break
        if hit:
            continue
        if a == "--" or a == "-" or not a.startswith("-"):
            break          # the command name, the script path, or "--"
        rest.append(a)
        i += 1
    rest.extend(argv[i:])
    layout_mode, vkbd_mode = modes["layout"], modes["vkbd"]
    if layout_mode is not None:
        layout_mode = layout_mode.strip().lower()
        if layout_mode not in ("us", "fixed", "auto", "xkb"):
            sys.stderr.write("wdotool: --layout: invalid argument %r; "
                             "valid: us, fixed, auto, xkb\n" % layout_mode)
            return 1
    if vkbd_mode is not None:
        vkbd_mode = vkbd_mode.strip().lower()
        if vkbd_mode not in ("auto", "on", "off"):
            sys.stderr.write("wdotool: --vkbd: invalid argument %r; "
                             "valid: auto, on, off\n" % vkbd_mode)
            return 1
    # The arguments as the user wrote them, kept before the two options above
    # are taken out of them.  The X11 handover is given the STRIPPED argv (they
    # are ours and the real xdotool has never had them), but the proxy hook has
    # to see them: its rule 4 is "an option of ours means our own code runs",
    # and an argv that has already lost `--layout` would send `--layout xkb type
    # a` to the original with the layout silently gone.  wxrandr's call passes
    # the full args for exactly this reason (wxrandr/cli.py, `--persistent`).
    proxy_args = list(argv[1:])
    if layout_mode is not None or vkbd_mode is not None:
        argv = argv[:1] + rest

    # Hidden diagnostic (B13): dump the compositor's keymap and what wdotool
    # makes of it. Ours, like __daemon: never a passthrough, never in `help`.
    if len(argv) > 1 and argv[1] == "__keymap":
        from wdotool import xkbmap

        return xkbmap.diagnostic_main(argv[2:])

    # `wdotool keys watch|explain` (README: Keyboard layouts). Ours, and routed here for the same three reasons
    # as __keymap: xdotool has no `keys`, so there is nothing to hand a passthrough over to; the command
    # registry is what `help` prints and that output is byte-compatible with the real xdotool's; and, like every
    # one of the 48 built-ins, a command name beats a file of the same name in script mode.
    if len(argv) > 1 and argv[1] == "keys":
        from wdotool import keys_cmds

        return keys_cmds.keys_main(argv[2:])

    # X11 session: this is the real xdotool's job. Before option parsing and before --help/--version --
    # installed as `xdotool`, even the version string has to be theirs (ours pins one upstream version and will
    # drift). argv here includes argv[0]; the hook wants the arguments alone.
    rc = passthrough.maybe_exec_real("xdotool", argv[1:], entry=entry)
    if rc is not None:
        return rc
    # Wayland with the original installed: the original ITSELF, run against
    # the xw11 display, which answers for the whole desktop (design section
    # 8.1).  Imported here and not at the top of the file: on an X11 session
    # main() has already left above, so `xw11.wrap` is never imported there --
    # and the wdotool zipapp carries no xw11/ (scripts/build-pyz.sh).
    try:
        from xw11.wrap import maybe_exec_through_proxy
    except ImportError:                 # pragma: no cover - a bundle without xw11/
        maybe_exec_through_proxy = None
    if maybe_exec_through_proxy is not None:
        rc = maybe_exec_through_proxy("xdotool", proxy_args, entry=entry)
        if rc is not None:
            return rc

    prog = _prog_name(argv)

    # Script mode: argv[1] is "-" or an existing file, and not a command name.
    if (
        len(argv) >= 2
        and not commands.is_command(argv[1])
        and (argv[1] == "-" or os.path.exists(argv[1]))
    ):
        return script_main(argv, prog, layout_mode, vkbd_mode)

    if len(argv) < 2:
        sys.stderr.write(_USAGE % prog)
        _print_help()
        return 1

    a1 = argv[1].lower()
    if a1 == "help":
        _print_help()
        return 0
    if a1 == "version":
        _print_version()
        return 0

    # getopt_long_only(argc, argv, "++hv", {help, version}) over the front of the command line. Any option
    # either exits or errors; like the C code, the chain then starts at argv[1] regardless.
    try:
        opts, _ = getopt_long_only(prog, argv[1:], "hv", [("help", False), ("version", False)])
    except GetoptError as e:
        opts = e.opts
        for name, _v in opts:
            if name in ("h", "help"):
                _print_help()
                return 0
            if name in ("v", "version"):
                _print_version()
                return 0
        sys.stderr.write("%s\n" % e)
        sys.stderr.write(_USAGE % prog)
        return 1
    for name, _v in opts:
        if name in ("h", "help"):
            _print_help()
            return 0
        if name in ("v", "version"):
            _print_version()
            return 0

    ctx = Context()
    ctx.layout_mode = layout_mode
    ctx.vkbd_mode = vkbd_mode
    ret = run_chain(ctx, prog, argv[1:])
    return ret if ret else ctx.exit_code


def _prog_name(argv) -> str:
    """What this process is called, for a diagnostic of our own: argv[0]'s basename, and "wdotool" where that is
    the module runner's `__main__.py`."""
    argv = sys.argv if argv is None else argv
    prog = os.path.basename(argv[0]) if argv and argv[0] else "wdotool"
    return "wdotool" if prog == "__main__.py" else prog


def main(argv: list[str] | None = None) -> int:
    """`_main()` plus the plumbing a C program gets from libc for free.

    xdotool never prints a traceback and never exits 120.  Ctrl-C during `wdotool sleep 5`, a reader leaving
    `wdotool search . | head -1`, a stdout that cannot take what we print (`>/dev/full`) and one that was closed
    before we started (`>&-`) are ordinary exits here, and the last thing that happens is the flush that says
    whether the output arrived (w11common/stdio.py)."""
    backend.set_program("wdotool")
    stdio.repair_std()
    prog = _prog_name(argv)
    quiet = False
    try:
        code = _main(argv)
    except SystemExit as e:
        stdio.exit_after_flush(prog, e)
        raise                       # unreachable; the line above raises
    except KeyboardInterrupt:
        code = 130                  # 128 + SIGINT, what the shell reports
    except BrokenPipeError:
        code = 1
    except CmdError as e:
        stdio.warn("%s\n" % e)
        code = getattr(e, "exit_code", 1) or 1
    except Exception as e:
        # one line, never a traceback: an out-of-range `sleep`, a compositor that drops the connection
        # mid-command, a keymap that will not parse.  DEBUG set to anything (including "") is the escape hatch
        # for a bug report: the exception propagates and Python prints the traceback it would have.
        if os.environ.get("DEBUG") is not None:
            raise
        stdio.warn("%s: %s\n" % (prog, e))
        # An OSError here is a write to stdout that failed (a full disk, a quota, `>/dev/full`): the flush below
        # is about to fail with the same errno, and the originals print one line, not two.
        quiet = isinstance(e, OSError)
        code = 1
    return code if stdio.flush_stdout(prog, quiet) else (code or 1)
