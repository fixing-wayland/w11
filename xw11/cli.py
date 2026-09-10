"""`xw11` -- start the proxy, find it, or stop it.

    xw11 [--display :N] [--upstream :M] [--foreground] [--passthrough]
         [--print-display] [--stop]

Until pyproject grows the console script (which lands with every packaging
manifest in one go, because tests/test_rpm_spec.py:370 and
tests/test_release_deb.py:343 compare `[project.scripts]` to what the packages
install), the program is `python3 -m xw11`.
"""

import argparse
import os
import signal
import sys

from wdotool import backend, x11_mini
from xw11 import display as display_mod
from xw11 import server as server_mod
from xw11.server import Server

#: The argv the daemonised child is re-exec'd with. `wdotool __daemon`'s
#: precedent: a word no shell user types, and one `ps` can be grepped for.
SERVE_ARGV = "__serve"

USAGE = "xw11 [--display :N] [--upstream :M] [--foreground] [--passthrough] [--print-display] [--stop]"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xw11", usage=USAGE,
        description="An X11 display in front of the session's X server, so that "
                    "the X tools answer about the whole desktop.",
        epilog="Environment: XW11_DISPLAY forces the display number, XW11_LOG "
               "names the log (default /tmp/xw11-<uid>.log), XW11_DEBUG=1 logs "
               "one line per request and per packet.")
    p.add_argument("--display", metavar=":N",
                   help="the display number to take; the default scans :%d..:%d"
                        % (display_mod.FIRST_NUM, display_mod.LAST_NUM))
    p.add_argument("--upstream", metavar=":M",
                   help="the X server to forward to (default $DISPLAY)")
    p.add_argument("--foreground", action="store_true",
                   help="serve in this process instead of daemonising")
    p.add_argument("--passthrough", action="store_true",
                   help="forward every byte and synthesize nothing")
    p.add_argument("--print-display", action="store_true",
                   help="print the :N of the running proxy")
    p.add_argument("--stop", action="store_true",
                   help="stop the running proxy")
    return p


def _fail(msg: str) -> int:
    print("xw11: %s" % msg, file=sys.stderr)
    return 1


def _upstream(args) -> str:
    got = args.upstream or os.environ.get("DISPLAY") or ""
    if not got:
        raise ValueError("no upstream display: pass --upstream :M or set $DISPLAY")
    x11_mini._parse_display(got)            # raises XUnavailable on nonsense
    return got


def serve(args) -> int:
    """Take a display, tell the world where it is, and run the loop."""
    try:
        upstream = _upstream(args)
    except (ValueError, x11_mini.XUnavailable) as e:
        return _fail(str(e))
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
    server = Server(display, upstream)
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


def print_display(args) -> int:
    got = display_mod.read_display()
    if got:
        print(got)
        return 0
    return _fail("no proxy is running: starting one on demand is not yet wired "
                 "up here, and the route is the double fork wdotool's daemon "
                 "already uses (AGENTS.md route 5, which is this proxy), at the "
                 "cost of the spawn racing a second starter -- run "
                 "`python3 -m xw11 --foreground` for now")


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


def main(argv=None) -> int:
    backend.set_program("xw11")
    argv = list(sys.argv[1:] if argv is None else argv)
    daemonised = argv[:1] == [SERVE_ARGV]
    if daemonised:
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    if args.stop:
        return stop(args)
    if args.print_display:
        return print_display(args)
    return serve(args)
