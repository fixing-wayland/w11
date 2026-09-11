"""`xw11` -- start the proxy, find it, or stop it.

    xw11 [--display :N] [--upstream :M] [--foreground] [--passthrough]
         [--print-display] [--stop]

With no option at all it daemonises: the proxy is a background process for the
rest of the session and the command returns as soon as it is listening.
`--foreground` serves in this process instead (the rigs, `scripts/parity-
oracle.sh` and `tests/test_xw11_live.py` all want that), and `__serve` is the
argv the daemonised child is re-exec'd with -- `wdotool __daemon`'s precedent:
a word no shell user types, and one `ps` can be grepped for.

Nobody has to run any of this by hand.  `xdotool`, `wmctrl`, `xprop` and
`xrandr` (the symlinks over the clones) start the proxy themselves on the first
call that needs it (`xw11/wrap.py`); what is here is for the long tail --
`DISPLAY=$(xw11 --print-display) python3 my-xlib-script.py` -- and for taking it
away again.
"""

import argparse
import os
import signal
import socket
import sys
import time

from w11common import session, stdio
from wdotool import backend, x11_mini
from xw11 import display as display_mod
from xw11 import server as server_mod
from xw11.server import Server

#: The argv the daemonised child is re-exec'd with. `xw11.wrap.SERVE_ARGV` is
#: the same string, spelled there so that the spawn needs nothing of this module.
SERVE_ARGV = "__serve"

#: How long a second starter waits for the winner's display file before giving
#: up, and how long `--print-display` waits for a proxy it just spawned.
#: `wdotool/daemon.py:2052`'s 2 s at 50 ms, which is also `wrap.POLL_SECONDS`.
WAIT_SECONDS = 2.0
WAIT_INTERVAL = 0.05

USAGE = "xw11 [--display :N] [--upstream :M] [--foreground] [--passthrough] [--print-display] [--stop]"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xw11", usage=USAGE,
        description="An X11 display in front of the session's X server, so that "
                    "the X tools answer about the whole desktop.",
        epilog="Environment: W11_PROXY=never stops the four tools using it, "
               "XW11_DISPLAY forces the display number, XW11_LOG names the log "
               "(default /tmp/xw11-<uid>.log), XW11_DEBUG=1 logs one line per "
               "request and per packet.")
    p.add_argument("--display", metavar=":N",
                   help="the display number to take; the default scans :%d..:%d"
                        % (display_mod.FIRST_NUM, display_mod.LAST_NUM))
    p.add_argument("--upstream", metavar=":M",
                   help="the X server to forward to (default $DISPLAY, else the "
                        "session's own)")
    p.add_argument("--foreground", action="store_true",
                   help="serve in this process instead of daemonising")
    p.add_argument("--passthrough", action="store_true",
                   help="forward every byte and synthesize nothing")
    p.add_argument("--print-display", action="store_true",
                   help="print the :N of the running proxy, starting one if needed")
    p.add_argument("--stop", action="store_true",
                   help="stop the running proxy")
    return p


def _fail(msg: str) -> int:
    stdio.warn("xw11: %s\n" % msg)
    return 1


def _upstream(args) -> str:
    """The X server this proxy forwards to (design section 8.2).

    `--upstream`, else `$DISPLAY`, else `session.find_x_display()` -- and the
    third is not a nicety: the daemonised child is spawned out of a wrapper that
    may itself have no DISPLAY (`ssh`, `cron`, `sudo`, the case
    `passthrough.repair_x_env` exists for), and the wrapper runs that repair
    only AFTER `ensure_proxy()` has come back, so the proxy is the one that has
    to find the session's Xwayland.  `find_x_display` reads $DISPLAY itself
    first and only answers with it when the socket is there, so this is one
    lookup and not two.  The cookie side needs nothing here: `find_cookie()`
    falls through to `x11_mini._session_xauthority()`, which is the same search
    for the file."""
    got = args.upstream or os.environ.get("DISPLAY") or session.find_x_display() or ""
    if not got:
        raise ValueError("no upstream display: pass --upstream :M or set $DISPLAY "
                         "(no X server socket was found for this session either)")
    x11_mini._parse_display(got)            # raises XUnavailable on nonsense
    return got


def _asked_display(args):
    """The display number this proxy was TOLD to take, or None when it scans.

    The distinction decides two things: a proxy that was told a number takes no
    session lock (the parity oracle runs one on `:98` beside a live session
    proxy), and it is the only one that can be asked for the upstream's own
    number."""
    return args.display or os.environ.get("XW11_DISPLAY") or None


def _same_display(a: str, b: str) -> bool:
    try:
        return x11_mini._parse_display(a)[0] == x11_mini._parse_display(b)[0]
    except Exception:
        return False


def session_lock():
    """The right to be this session's proxy, as an abstract socket bind.

    Abstract and not a lock file: the kernel takes the name back when the
    process ends, so there is no stale lock to recognise, no unlink to race and
    nothing left behind by a SIGKILL -- the same reason the display sockets are
    bound abstract-name-first (design section 2.5).  The name is the display
    file's path with a NUL in front of it, so it is per-session and per-uid
    exactly like the file it protects.

    Ten wrappers spawning at once is the case this exists for (R11): one binds,
    the nine others find the name taken, wait for the winner's display file and
    exit 0.  The socket is never connected to; binding IS the protocol.
    """
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind("\0" + display_mod.display_file_path())
        s.listen(1)
    except OSError:
        s.close()
        return None
    return s


def wait_for_display(timeout=WAIT_SECONDS):
    """The `:N` a proxy in this session is listening on, or None. Verified
    through the socket, never read off the file alone."""
    deadline = time.monotonic() + timeout
    while True:
        got = display_mod.read_display()
        if got or time.monotonic() >= deadline:
            return got
        time.sleep(WAIT_INTERVAL)


def _upstream_answers(upstream: str) -> bool:
    try:
        num, _screen = x11_mini._parse_display(upstream)
    except Exception:
        return False
    sock = server_mod.upstream_socket(num)
    if sock is None:
        return False
    sock.close()
    return True


def _refusal(args, upstream):
    """The two things the proxy will not do, as a message or None.

    Neither is a policy: each names what would close it and what that costs."""
    asked = _asked_display(args)
    if asked is not None and _same_display(asked, upstream):
        return ("%s is the upstream display: a proxy in front of itself would "
                "forward every request to its own loop and wait for the answer "
                "it is the one that has to write. Pick another number, or point "
                "--upstream at the session's real X server." % asked)
    if not _upstream_answers(upstream) and not session.xwayland_running():
        return ("no X server answers %s and no Xwayland is running: xw11 "
                "forwards to the session's X server and does not start one, "
                "not yet -- the route is asking the compositor for it "
                "(AGENTS.md route 2: sway's `exec Xwayland`, Mutter's and "
                "KWin's own on-demand start), at the cost of owning the "
                "lifetime of a server the compositor believes is its. Start one "
                "(`Xwayland %s &`) or name a live display with --upstream."
                % (upstream, upstream))
    return None


def serve(args) -> int:
    """Take a display, tell the world where it is, and run the loop."""
    try:
        upstream = _upstream(args)
    except (ValueError, x11_mini.XUnavailable) as e:
        return _fail(str(e))
    refusal = _refusal(args, upstream)
    if refusal:
        return _fail(refusal)
    lock = None
    if _asked_display(args) is None:
        # The session's proxy, so the session lock decides whether this process
        # is it. Held for the whole run and closed in the `finally` below, which
        # is reached only when the loop has ended.
        lock = session_lock()
        if lock is None:
            got = wait_for_display()
            if got:
                print("xw11: a proxy is already running on %s" % got)
                return 0
            return _fail("another xw11 in this session holds the startup lock "
                         "and never announced a display (see %s)"
                         % server_mod.log_path())
    try:
        return _serve_locked(args, upstream)
    finally:
        if lock is not None:
            lock.close()


def _serve_locked(args, upstream) -> int:
    """serve()'s second half: everything from the display number on, with the
    session lock (when this proxy is the session's) already held."""
    try:
        display = display_mod.allocate(args.display)
    except (display_mod.DisplayError, x11_mini.XUnavailable) as e:
        return _fail(str(e))
    # Before anything else runs -- before the Server, so that a constructor that
    # grows an OwnConn or a backend later reads the right environment: the
    # proxy's own DISPLAY is the UPSTREAM's, so that a backend which reads the X
    # plane itself reaches Xwayland and not the loop it is running inside
    # (design section 2.4, R15).
    server_mod.adopt_upstream_environment(upstream)
    server = Server(display, upstream, passthrough=args.passthrough)
    try:
        num, _screen = x11_mini._parse_display(upstream)
        try:
            display.add_xauth(num)
        except OSError as e:
            # A cookie that exists and cannot be appended to: a read-only
            # ~/.Xauthority, an $XAUTHORITY that belongs to another uid after a
            # sudo, a FIFO at the path (ENXIO out of _open_regular). The proxy
            # still serves; clients whose cookie the upstream then refuses get
            # the upstream's own Failed reason, and this line is what explains
            # it.
            server.say("no xauth entry written (%s): %s" % (e.filename or "?", e))
        # One proxy per session owns the display file (design section 2.1). A
        # second proxy -- the parity oracle starts one with --display :98 while
        # a session proxy may be up -- must not overwrite a live one's line and
        # then unlink it at exit, which would take the live one down with it
        # through its own file-gone check.
        other = display_mod.read_display()
        if other and other != display.name:
            server.say("a proxy already holds %s on %s: this one does not write it"
                       % (display_mod.display_file_path(), other))
        else:
            display.write_display_file(upstream)
        server.install_signal_handlers()
        return server.serve_forever()
    except BaseException:
        display.release()
        raise


def spawn_argv(args) -> list:
    """The flags that describe the proxy PROCESS, as the argv the daemonised
    child is re-exec'd with.

    The spawn re-execs `xw11 __serve` and the child parses everything after
    that word with this same parser, so a flag that is not put here is a flag
    the command accepted and threw away: `xw11 --passthrough` is a by-hand
    command (design section 8.2) and it has to start a forwarding proxy, not the
    synthesizing default.  `--foreground`, `--print-display` and `--stop` are
    about THIS process and are not in the list."""
    out = []
    if args.passthrough:
        out.append("--passthrough")
    if args.upstream:
        out += ["--upstream", args.upstream]
    if args.display:
        out += ["--display", args.display]
    return out


def start_refusal(args) -> str | None:
    """Why this command will not start the proxy it was asked for, or None.

    A session has one proxy (the session lock, R11) and these three flags
    describe one that does not exist yet, so answering with the one that is
    already up -- started with somebody else's flags -- would be the silent drop
    `spawn_argv` exists to stop.  `--foreground` is the way to have a second
    proxy beside the session's, which is what `scripts/parity-oracle.sh` does
    with its own `--display :98`."""
    extra = spawn_argv(args)
    if not extra:
        return None
    got = display_mod.read_display()
    if not got:
        return None
    return ("a proxy is already running on %s and `%s` describes another one: "
            "stop that one (xw11 --stop), or run this one in the foreground "
            "(xw11 --foreground %s)" % (got, " ".join(extra), " ".join(extra)))


def start(args) -> str | None:
    """Spawn a daemonised proxy and wait for it to announce a display.

    The spawn itself is `xw11.wrap`'s, which is `wdotool/daemon.py`'s: fork,
    setsid, fork, stdio to the log, every inherited fd closed, out of the
    launcher's scope, cwd `/`, re-exec as `xw11 __serve` plus `spawn_argv`'s
    flags.  Callers ask `start_refusal()` first."""
    from xw11 import wrap
    return wrap.ensure_proxy(extra_argv=spawn_argv(args))


def print_display(args) -> int:
    refusal = start_refusal(args)
    if refusal:
        return _fail(refusal)
    # With flags of its own this command is a start and not a lookup: the proxy
    # that is already there is not the one that was asked for, and
    # `start_refusal` above has already established there is none.
    got = (None if spawn_argv(args) else display_mod.read_display()) or start(args)
    if got:
        print(got)
        return 0
    return _fail("no proxy is running and none could be started (see %s)"
                 % server_mod.log_path())


def stop(_args) -> int:
    """Unlink the display file and SIGTERM the pid it names. Unlinking first is
    what makes it work even against a proxy that is wedged: the file-gone check
    is one of the three the loop makes every 15 seconds."""
    path = display_mod.display_file_path()
    got = display_mod.read_display_file(path)
    if not got:
        return _fail("no proxy is running (%s)" % path)
    name, pid, _upstream = got
    try:
        os.unlink(path)
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return _fail("%s named pid %d, which is gone" % (name, pid))
    except PermissionError:
        return _fail("pid %d belongs to another user" % pid)
    return 0


def _run(argv) -> int:
    daemonised = argv[:1] == [SERVE_ARGV]
    if daemonised:
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    if args.stop:
        return stop(args)
    if args.print_display:
        return print_display(args)
    if daemonised or args.foreground:
        return serve(args)
    # The default: put it in the background and say where it went. The spawn is
    # the wrapper's, so `xw11` by hand and `xdotool` through the wrapper produce
    # exactly one kind of proxy process -- with this command's own flags carried
    # into the child's argv (`spawn_argv`).
    refusal = start_refusal(args)
    if refusal:
        return _fail(refusal)
    got = start(args)
    if got is None:
        return _fail("the proxy did not come up (see %s)" % server_mod.log_path())
    print(got)
    return 0


def main(argv=None) -> int:
    stdio.repair_std()          # fd 1 or 2 closed before Python started
    backend.set_program("xw11")
    quiet = False
    try:
        code = _run(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as e:
        # argparse's --help and its usage errors: without this the help text is
        # still buffered when main() leaves, and a full or closed stdout becomes
        # exit 120 out of the interpreter's own exit-time flush (bug 3,
        # w11common/stdio.py).
        stdio.exit_after_flush("xw11", e)
        raise                   # unreachable; the line above raises
    except KeyboardInterrupt:
        code = 130              # 128 + SIGINT, what the shell reports
    except BrokenPipeError:
        code = 1
    except Exception as e:
        # never a traceback: a compositor that drops the connection mid-query,
        # an upstream that goes away, a runtime directory that is not there --
        # one line, exit 1.
        stdio.warn("xw11: %s\n" % e)
        quiet = isinstance(e, OSError)
        code = 1
    return code if stdio.flush_stdout("xw11", quiet) else (code or 1)
