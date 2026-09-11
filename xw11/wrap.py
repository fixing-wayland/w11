"""The wrapper: on a Wayland session, run the ORIGINAL against the proxy.

`w11common/passthrough.py` answers one question -- "is this an X11 session, and
should we get out of the way?" -- and this file answers the other one, which
only exists because `xw11` does: *this is a Wayland session, and the original
xdotool works here now, because there is an X display in front of the
compositor that answers for the whole desktop.* So the four clones become
wrappers: each `main()` calls `maybe_exec_through_proxy()` right after
`passthrough.maybe_exec_real()`, and on a Wayland box with the original
installed the process is replaced by the original with `DISPLAY` pointing at
`xw11` (design section 8.1).

Six rules decide it, in order, and the first five cost no process at all:

1. `entry=False` -- a library caller (the whole test suite) is never replaced.
2. `W11_PASSTHROUGH=never` -- the suite's escape hatch keeps its whole meaning,
   "our own code, no handover to anything", so no test file needs a new line
   and `tests/test_passthrough.py:SuiteGuard` needs no new belt. `W11_PROXY`
   (per-tool `WDOTOOL_PROXY` etc.) `never` says the same about the proxy alone.
3. the session is not Wayland -- on X11 `maybe_exec_real()` has already handed
   over above this call and the proxy is never started, never imported and
   never spawned; with no session at all the clone's own refusal is the better
   message than a proxy with nothing to proxy.
4. the argv carries a clone-only token: an option or command of OURS that the
   original has never had (`CLONE_ONLY`). AGENTS.md's rule for the things that
   are better than X -- they live behind a flag the original never had, so a
   command that uses one is asking for our code by name.
5. no original is installed: the clone runs, silently. This is the normal
   wlroots box with no xdotool on it.
5b. the command is a help or version request that opens no display
   (`_is_help_only`):
   the original runs, as it does on X11, and no proxy is started -- none of
   those opens a display, and a usage string is not worth a fifteen-minute
   process and a pinned Xwayland.
6. the proxy cannot be started: the clone runs, and one line goes to stderr
   under `XW11_DEBUG`.

`W11_PROXY=always` turns 5 and 6 into exit 127 with `passthrough`'s own
`_missing_message(x11=False)` wording: a script that asks for the original's
bytes gets them or an error, never a silently different implementation.

`always` also overrides rule 4 -- but only for the tokens that REACH this hook,
which is not all of `CLONE_ONLY`. `--persistent`, `--unsafe-gnome-overlap*`
(wxrandr/cli.py:1829, which passes the full args) and `--layout`/`--vkbd`
(wdotool/cli.py:414, the same) are sent on and the original rejects them in its
own words. `--backend`, `--backends`, `--print-backend`, `keys` and `__keymap`
never get here at all: their own `main()` has answered and returned several
screens above this call, whatever the variable says -- which is right. A user
who spells `--backend sway` has named the backend, and `--print-backend` and
`wdotool keys explain` describe our own code and have nothing in the original
to hand over to. `CLONE_ONLY` still lists them, because the table's job is
"what belongs to the clone" and the hook must answer that correctly whichever
argv it is handed.

The spawn is `wdotool/daemon.py`'s, twice measured and not re-invented: fork,
setsid, fork, stdio to the log, every inherited fd closed, out of the
launcher's transient scope, cwd `/`, re-exec as `xw11 __serve`. Ten wrappers
starting at once leave exactly one `__serve`: the session lock is an abstract
socket bind in `xw11/cli.py:serve`, and the nine losers exit as soon as the
winner's display file is there (R11, `tests/test_xw11_wrap.py::TwoStarters`).
"""

import os
import sys
import time

from w11common import passthrough

#: Our own options and commands, per original -- the argv tokens that mean "run
#: the clone, whatever the session".  The `xrandr` set is
#: `scripts/check-docs.py:SILENT["wxrandr"]` minus `--q1`/`--q12` (which are
#: xrandr's OWN compatibility tokens, accepted and ignored, and absent from its
#: usage text for that reason -- they belong to the original and must reach
#: it);
#: `tests/test_xw11_wrap.py::CloneOnlyTokens::test_the_table_is_check_docs_own_silent_list`
#: pins the two tables equal so that an option added to one is added to the
#: other.  A literal and not an import of that table: `scripts/` is in no
#: packaging (design section 8.5), so an installed `xw11` reaching into it would
#: find nothing -- the test is where the two meet.
#:
#: `wdotool`'s three: `--layout` and `--vkbd` are ours, and `keys` is our own
#: command word (`wdotool keys explain a`).  `wmctrl` and `xprop` have none --
#: every byte either of those clones accepts, the original accepts too.
CLONE_ONLY = {
    "xdotool": frozenset({"--layout", "--vkbd", "keys"}),
    "wmctrl": frozenset(),
    "xprop": frozenset(),
    "xrandr": frozenset({
        "--backend", "--backends", "--print-backend", "--persistent",
        "--gnome-overlap-status", "--gnome-overlap-allow",
        "--gnome-overlap-forget", "--unsafe-gnome-overlap",
        "--unsafe-gnome-overlap-unmeasured",
    }),
}

#: How long a wrapper waits for a proxy it just spawned, and how often it looks.
#: `DaemonClient.connect_or_spawn`'s numbers (wdotool/daemon.py:2052): 2 s at
#: 50 ms.  The proxy's own startup is an interpreter, one bind, one connect and
#: one setup exchange: measured on this box on 2026-09-11, six runs of
#: `ensure_proxy()` against a `support.FakeUpstream` took 53, 105, 104, 105, 109
#: and 203 ms from the fork to a display file a client can dial -- one to four
#: polls, which is why they cluster on multiples of the 50 ms interval, and a
#: typical 105.  By the `python -m xw11` route, the slow one: an installed
#: console script skips the PYTHONPATH.  So the two seconds are for a loaded
#: machine and not for the normal case, and the interval is a fraction of a
#: startup rather than a twentieth of one.
POLL_SECONDS = 2.0
POLL_INTERVAL = 0.05

#: What the spawned proxy keeps, on top of `daemon.clean_env`'s allow-list
#: (`_KEEP_ENV` + `WDOTOOL_*`).  A proxy is not an input daemon: it needs the
#: upstream X server (`DISPLAY`, `XAUTHORITY`), the compositor it asks about
#: windows and outputs (`WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, the four
#: compositor sockets) and the bus the GNOME/KDE backends speak on.
_KEEP_ENV = ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR",
             "SWAYSOCK", "I3SOCK", "HYPRLAND_INSTANCE_SIGNATURE",
             "WAYFIRE_SOCKET", "DBUS_SESSION_BUS_ADDRESS")
#: ...and by prefix.  `WXRANDR_BACKEND` is the one that matters most: a user who
#: forces a backend for `wxrandr` means it for `xrandr` through the proxy too
#: (requests-batch-7.md item 5).  `XW11_*` carries the display number, the log
#: and the debug switch.
_KEEP_PREFIX = ("WDOTOOL_", "WXRANDR_", "XW11_")

#: The argv word the daemonised proxy is re-exec'd with.  `xw11.cli.SERVE_ARGV`
#: is the same string; it is spelled here as well so that the spawn does not
#: import the CLI (and its argparse, its Server and every backend behind it)
#: in a process that is about to `execve` anyway.
SERVE_ARGV = "__serve"


def _debug(env, text: str) -> None:
    """One line to stderr, only under `XW11_DEBUG`.  A wrapper is a CLI that is
    about to become the original: anything it prints unasked lands in the middle
    of somebody's `xdotool search` output."""
    e = os.environ if env is None else env
    if (e.get("XW11_DEBUG") or "").strip() not in ("", "0", "no", "off", "false"):
        sys.stderr.write("xw11: %s\n" % text)


# -- starting the proxy -------------------------------------------------------

def _exec_plan(env, extra_argv=()):
    """`(path, argv, env)` candidates for re-execing as the proxy, best first.

    `DaemonClient._exec_plan`'s twin (wdotool/daemon.py:2091), with one
    difference: the launcher is `xdotool`, never `xw11`, so the console script
    has to be found on PATH rather than read off `sys.argv[0]`.  `is_us()` is
    what makes that safe -- `xw11` is in `OUR_NAMES`, so a stranger's program of
    that name on PATH is not taken for ours.

    `extra_argv` goes after `__serve` and is the proxy's own flags when a human
    asked for a particular one (`xw11 --passthrough`, `xw11 --upstream :1`,
    design section 8.2): the daemonised child parses everything after `__serve`
    with the same argparse, so a flag that is not carried here is a flag the
    command silently dropped.  The wrapper's own spawn passes none of it.

    Paths are resolved before the caller chdir()s to `/`."""
    extra = list(extra_argv)
    plan = []
    for d in (env.get("PATH") or os.defpath).split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, "xw11")
        if not os.path.isfile(cand) or not os.access(cand, os.X_OK):
            continue
        if passthrough.is_us(cand):
            plan.append((cand, [cand, SERVE_ARGV] + extra, dict(env)))
        break
    if sys.executable:
        menv = dict(env)
        try:
            import xw11 as _pkg
            # the parent of the package directory; for a zipapp that is the
            # .pyz itself, which is a valid PYTHONPATH entry too
            parent = os.path.dirname(os.path.dirname(os.path.abspath(_pkg.__file__)))
        except Exception:                   # pragma: no cover - diagnostics only
            parent = ""
        if parent:
            menv["PYTHONPATH"] = parent
        plan.append((sys.executable,
                     [sys.executable, "-m", "xw11", SERVE_ARGV] + extra, menv))
    return plan


def spawn_env(env=None) -> dict:
    """The environment the spawned proxy keeps.  `daemon.clean_env`'s
    allow-list plus `_KEEP_ENV` and `_KEEP_PREFIX`: a proxy outlives the command
    that started it by up to fifteen minutes (design section 2.6), so it must
    not pin that command's session state -- `DESKTOP_STARTUP_ID`, the
    launcher's `PWD`, anything derived from its argv."""
    from wdotool import daemon
    src = dict(os.environ if env is None else env)
    out = daemon.clean_env(src)
    for k, v in src.items():
        if k in _KEEP_ENV or k.startswith(_KEEP_PREFIX):
            out[k] = v
    return out


def _open_log():
    """The proxy's log, opened the way the daemon opens its own
    (wdotool/daemon.py:2140): `O_NOFOLLOW`, and never a file that is not ours.
    /tmp is world-writable and the log carries session diagnostics."""
    from xw11 import server as server_mod
    try:
        fd = os.open(server_mod.log_path(),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
        if os.fstat(fd).st_uid != os.geteuid():
            os.close(fd)
            raise OSError("log file is not ours")
        return fd
    except OSError:
        return os.open(os.devnull, os.O_WRONLY)


def _spawn(env, extra_argv=()) -> None:
    """Daemonize and re-exec as `xw11 __serve` (plus `extra_argv`).

    `DaemonClient._spawn` (wdotool/daemon.py:2119) with the log and the argv
    changed: fork, setsid, fork, stdio to the log, every inherited fd closed
    (the session bus socket most of all -- a proxy that held the launcher's bus
    connection would show up in `ss` as a bus client for fifteen minutes), out
    of the launcher's transient scope, cwd `/`.

    There is no in-process fallback, unlike the daemon's: `python -m xw11
    __serve` is always in the plan when `sys.executable` is set, and a proxy
    that never came up is rule 6 -- the clone runs and says so under
    `XW11_DEBUG`."""
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return
    code = 1
    try:
        os.setsid()
        if os.fork():
            os._exit(0)                     # session leader exits; grandchild serves
        sys.stdout.flush()
        sys.stderr.flush()
        plan = _exec_plan(env, extra_argv)  # resolves paths before chdir
        null = os.open(os.devnull, os.O_RDONLY)
        log = _open_log()
        os.dup2(null, 0)
        os.dup2(log, 1)
        os.dup2(log, 2)
        os.close(null)
        os.close(log)
        from wdotool import daemon
        daemon._close_inherited_fds()
        daemon._escape_transient_scope()
        try:
            os.chdir("/")                   # never hold the launcher's cwd busy
        except OSError:
            pass
        for path, argv, e in plan:
            try:
                os.execve(path, argv, e)
            except OSError:
                continue
    except BaseException:
        import traceback
        traceback.print_exc()
    finally:
        os._exit(code)


def ensure_proxy(env=None, timeout=POLL_SECONDS, extra_argv=()) -> str | None:
    """The proxy's display (`":N"`), starting one if there is none.

    The display file is a hint and the socket is the proof: `read_display()`
    dials the abstract name the file gives and refuses anything that is not the
    pid the file claims, running as us (xw11/display.py, and
    `DaemonClient._try_connect`'s refusal before it).  What goes down that
    socket is every keystroke a wrapped `xdotool type` sends.

    `env` is what the spawned child is given (`spawn_env`); the lookup that
    comes first reads the process environment, because `display_file_path()`
    resolves the runtime directory off `os.environ`. Outside this file's tests
    the two are the same dict.

    `extra_argv` is `xw11.cli.spawn_argv`'s: the flags a human typed for the
    proxy itself.  A running proxy is still the answer when there are none --
    that is the wrapper's whole case -- and `xw11.cli.start_refusal` is what
    stops a flagged command from being answered with a proxy somebody else's
    flags started.

    Returns None when no proxy came up within `timeout`."""
    from xw11 import display as display_mod
    got = display_mod.read_display()
    if got:
        return got
    _spawn(spawn_env(env), extra_argv)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        got = display_mod.read_display()
        if got:
            return got
    return None


def proxy_xauthority() -> str | None:
    """The file the proxy appended its own cookie to, or None when there was no
    cookie to append (sway starts Xwayland with no `-auth`: recon/env.md 2, and
    the empty auth is forwarded and accepted).

    No `env` parameter, unlike the hook's: everything under it reads the process
    environment -- `find_cookie()` takes `$XAUTHORITY` off `os.environ` and
    `display_file_path()` takes the runtime directory off it too -- and a
    parameter this function accepted and then ignored would be a lie in the
    signature. The hook's `env` is what the CHILD is given; what the lookups
    read is this process, which is the same dict everywhere but a test.

    Not read out of the display file -- the file carries `:N <pid> <upstream>`
    and no second control protocol -- but recomputed from the upstream it names,
    by the same `find_cookie()` the proxy used, which answers with the file the
    cookie came from.  That file is the one the proxy wrote into (design
    section 2.5)."""
    from wdotool import x11_mini
    from xw11 import display as display_mod
    got = display_mod.read_display_file()
    if not got:
        return None
    _name, _pid, upstream = got
    try:
        num, _screen = x11_mini._parse_display(upstream)
    except Exception:
        return None
    path, cookie = display_mod.find_cookie(num)
    return path if (path and cookie) else None


# -- the hook -----------------------------------------------------------------

def clone_only_tokens(tool: str, args) -> list:
    """The clone-only tokens this argv really carries.

    For `xrandr` the question is asked of `wxrandr.cli` itself -- the same
    `scan_backend_argv`/`own_flags_in` pair `wxrandr.main()` asks before the X11
    handover -- so an *output* named like one of our options is a value here
    too: `xrandr --output --persistent --off` carries none of them and goes to
    the original whole, which is what tests/test_passthrough_exec.py already
    pins for the X11 direction."""
    args = list(args)
    plain = sorted(CLONE_ONLY.get(tool, frozenset()).intersection(args))
    if tool != "xrandr":
        return plain
    try:
        from wxrandr import cli as wxrandr_cli
    except ImportError:                     # pragma: no cover - a bundle without wxrandr
        return plain
    flag, info, _rest = wxrandr_cli.scan_backend_argv(args)
    got = set(wxrandr_cli.own_flags_in(args))
    if info:
        got |= CLONE_ONLY["xrandr"].intersection(args)
    if flag is not None:
        got.add("--backend")
    return sorted(got)


#: The two originals that answer a bare command line with no display at all:
#: `xdotool` prints its list of commands and `wmctrl 1.07` its usage, each to
#: stderr with exit 1 -- measured 2026-09-11 against the pinned pair with
#: DISPLAY and XAUTHORITY unset.  The other two do open a display for it:
#: `xrandr` with no argument prints the screen configuration and `xprop` with
#: none waits for a click on a window (both measured the same way: "Can't open
#: display" / "unable to open display ''"), and both are exactly the question
#: the proxy exists to answer.
_USAGE_WITH_NO_ARGS = ("xdotool", "wmctrl")


def _is_help_only(tool, args) -> bool:
    """A help or version request, narrowed to the ones that open no display.

    `passthrough._is_help_request` answers True for an EMPTY argv as well, which
    is right where it is used -- with no original installed a bare `xdotool`
    must print our usage and never "not found" -- and wrong here, where the
    answer decides whether a proxy is started: a bare `xrandr` is a query about
    the outputs and it must go through the proxy like any other."""
    if not args:
        return tool in _USAGE_WITH_NO_ARGS
    return passthrough._is_help_request(tool, args)


def maybe_exec_through_proxy(tool, args, *, entry=True, env=None):
    """The second hook every wrapped `main()` calls.  Returns None to keep
    running our own code, or an exit status to return from `main()` -- and
    usually does not return at all, because `os.execve` replaces the process.

    `args` is the tool's arguments **without** argv[0], the same convention
    `passthrough.maybe_exec_real` takes and the same normalisation each
    `main()` already does for it."""
    tool = passthrough.real_name(tool)
    e = os.environ if env is None else env
    if not entry:                                                       # 1
        return None
    if passthrough.passthrough_mode(tool, e) == "never":                # 2
        return None
    mode = passthrough.proxy_mode(tool, e)
    if mode == "never":
        return None
    if passthrough.session_kind(tool, e) != "wayland":                  # 3
        return None
    args = list(args)
    mine = clone_only_tokens(tool, args)                                # 4
    if mine and mode != "always":
        _debug(e, "%s: %s is ours, not %s's: running the clone"
               % (tool, " ".join(mine), tool))
        return None
    try:
        real = passthrough.real_tool(tool, e)                           # 5
    except passthrough.RealToolError as exc:
        sys.stderr.write("%s: %s\n" % (tool, exc))
        return 127
    if real is None:
        if mode == "always" and not _is_help_only(tool, args):
            # `x11=False`: the reason to install the original is the REQUEST
            # and not the session, and "this is an X11 session" would be untrue
            # on the Wayland box this ran on.  The help request is the one
            # exception `maybe_exec_real` makes too (passthrough.py:932): a
            # request for the usage text must never answer "not found".
            sys.stderr.write(passthrough._missing_message(tool, x11=False))
            return 127
        _debug(e, "%s: no original on PATH: running the clone" % tool)
        return None
    if _is_help_only(tool, args):
        # `xdotool --help`, `xdotool -v`, `xrandr --help`, `xprop -help`, and a
        # bare `wmctrl` with no argument at all: none of them opens a display,
        # so none of them is worth a proxy -- a fifteen-minute process and a
        # pinned Xwayland (105 ms, measured; POLL_SECONDS' comment) for a usage
        # string.  The original still runs, because on X11 it does: the same
        # `_is_help_request` decides there (passthrough.py:932) and the bytes it
        # prints are the parity oracle.  The environment goes through as it is,
        # with no DISPLAY of ours in it.
        return passthrough.exec_real(tool, real, args, dict(e))
    display = ensure_proxy(e)                                           # 6
    if display is None:
        if mode == "always":
            sys.stderr.write(
                "%s: W11_PROXY=always and no xw11 proxy could be started "
                "(see %s)\n" % (tool, _log_path_for_message()))
            return 127
        _debug(e, "%s: no proxy could be started: running the clone" % tool)
        return None
    # After the repair, and the repair is never asked to choose: `repair_x_env`
    # fills `DISPLAY` only when the current one does not work
    # (passthrough.py:784), and on a Wayland session Xwayland's own `:0` works
    # perfectly -- so left to itself it would keep Xwayland's display and this
    # whole wrapper would change nothing at all. It is still run first, because
    # under `sudo` and cron there is no DISPLAY and no XAUTHORITY to inherit and
    # everything else it fills in is wanted.
    child = passthrough.repair_x_env(dict(e))
    child["DISPLAY"] = display
    auth = proxy_xauthority()
    if auth:
        child["XAUTHORITY"] = auth
    # exec_real() runs child_env() over this, which adds the handover guard
    # exactly once and leaves the two values above alone (both name things that
    # exist, which is the whole of what the repair looks at).
    return passthrough.exec_real(tool, real, args, child)


def _log_path_for_message() -> str:
    try:
        from xw11 import server as server_mod
        return server_mod.log_path()
    except Exception:                       # pragma: no cover - diagnostics only
        return "the xw11 log"
