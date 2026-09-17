"""The command line.

    wmirror SOURCE --to TARGET [--region WxH+X+Y] [--scaling fit|cover|exact]
    wmirror --list | --stop TARGET | --stop-all | --check

This is its own command, not a wxrandr flag, and it is deliberate: see the
"Why a separate command" section of docs/WMIRROR.md. The short of it is that a
mirror here is a resident process, not a layout, so it cannot be spelled in
a saved layout script that has to keep running on a plain X11 box with the
real xrandr.
"""

import argparse
import shlex
import sys

from w11common import session, stdio

from . import VERSION
from hacks.mirror import cinnamon, core, supervise


def _out(line: str = ""):
    sys.stdout.write(line + "\n")


def _err(lines):
    """One refusal: `wmirror: <first line>`, the rest indented under it."""
    if isinstance(lines, str):
        lines = [lines]
    for i, line in enumerate(lines):
        stdio.warn(("wmirror: " if i == 0 else "  ") + line + "\n")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wmirror", add_help=True,
        usage="%(prog)s SOURCE --to TARGET [options]\n"
              "       %(prog)s --list | --stop TARGET | --stop-all | --check",
        description="Mirror an output or region onto another output by "
                    "starting and stopping wl-mirror. Requires output-management "
                    "and capture protocols (including on COSMIC); use --check.",
        epilog="For same-size outputs, use wxrandr --output TARGET --same-as SOURCE, "
               "or --keep-layout to keep TARGET at its separate position. "
               "The mirror keeps running after this command exits; "
               "--list shows it and --stop TARGET ends it.")

    p.add_argument("source", nargs="?", metavar="SOURCE", help="the output to capture")
    p.add_argument("--to", metavar="TARGET", dest="to", help="destination output")
    p.add_argument("--region", metavar="WxH+X+Y",
                   help="capture WIDTHxHEIGHT+X+Y within SOURCE; X and Y are "
                        "measured from the desktop origin, as in wxrandr --query")
    # default=None, not core.DEFAULT_SCALING: both paths resolve the default themselves and write the resolved
    # mode into the record, so `--list` prints the mode the mirror is drawing rather than the flag the user
    # happened to type.
    p.add_argument("--scaling", choices=core.SCALINGS,
                   default=None,
                   help="resize the captured image: fit "
                        "(letterbox, default), cover (fill and crop), exact "
                        "(enlarge by 2x, 3x, etc. or reduce to 1/2, 1/3, etc.)")
    p.add_argument("--keep-layout", action="store_true",
                   help="mirror even where a shared position would do it, "
                        "so TARGET keeps its own place in the layout")
    p.add_argument("--replace", action="store_true", help="stop the mirror already running on TARGET first")
    p.add_argument("--dry-run", action="store_true", help="print the wl-mirror command line and stop")
    p.add_argument("--list", action="store_true",
                   help="list running mirrors and remove records for "
                        "processes that have exited")
    p.add_argument("--stop", metavar="TARGET", help="stop the mirror on TARGET")
    p.add_argument("--stop-all", action="store_true", help="stop every mirror we started")
    p.add_argument("--check", action="store_true",
                   help="say whether this session can mirror at all, and "
                        "what is missing if it cannot")
    p.add_argument("--version", action="version", version=VERSION)
    return p


# -- the queries --------------------------------------------------------------

def _state():
    """(state, records, whether reaping changed them). Every command that reads the records verifies them first:
    the helper is invisible to output management, so this file is the only record there is, and a stale line in
    it would be a lie.

    Two reapers, one over each kind of mirror: `supervise.reap` for the wl-mirror processes (over /proc), and
    `cinnamon.reap` for the in-muffin Clutter actors (over Eval, and only if there is a cinnamon record to
    check, so a wl-mirror session opens no bus)."""
    state = core.load_state()
    recs = core.records(state)
    changed = supervise.reap(recs)
    changed = cinnamon.reap(recs) or changed
    return state, recs, changed


def _stop_any(rec: dict) -> bool:
    """Stop one mirror whatever its kind: a cinnamon actor over Eval, a wl-mirror process over its pids."""
    if rec.get("kind") == "cinnamon":
        return cinnamon.stop_record(rec)
    return supervise.stop_record(rec)


def _cmd_list() -> int:
    # under the lock like every other command that may write: a reap that dropped a record a concurrent start
    # had just replaced would take the new mirror out of the file and leave it running, unfindable.
    with core.state_lock():
        state, recs, changed = _state()
        if changed:
            state.save()
        lines = [core.fmt_record(t, recs[t]) for t in sorted(recs)]
    for line in lines:
        _out(line)
    return 0


def _cmd_stop(target: str) -> int:
    with core.state_lock():
        state, recs, changed = _state()
        rec = recs.pop(target, None)
        if changed or rec is not None:
            state.save()
        if rec is None:
            _err(["no mirror is running on %s" % target, "`wmirror --list` shows the ones that are"])
            return 1
        _stop_any(rec)
    _out("stopped  %s" % core.fmt_record(target, rec))
    return 0


def _cmd_stop_all() -> int:
    with core.state_lock():
        state, recs, changed = _state()
        stopped = []
        for target in sorted(recs):
            rec = recs[target]
            _stop_any(rec)
            stopped.append(core.fmt_record(target, rec))
        if recs or changed:
            recs.clear()
            state.save()
    for line in stopped:
        _out("stopped  %s" % line)
    return 0


def _cmd_check() -> int:
    ok = True
    hit = session.find_wayland_socket()
    conn, problem = None, ()
    try:
        conn = core.open_conn()
    except core.Refusal as e:
        problem = e.lines
    # T21, fix 40's `--check` half.  The session's own problem is printed FIRST, above `helper:` and the
    # `apt install wl-mirror` line under it.  On an X11 session this printed `helper:   not installed`, the
    # install hint, `wayland:  none`, and only then, on line 4, "this is an X11 session: there is no
    # wl-mirror here" -- so a reader who stops at the first thing that looks like an instruction installs a
    # package that cannot help them.  Measured on all nine X11 flavors of run 34628777544 with the evidence
    # `[helper:   not installed]`, and reproduced on the guest's own X server at :355
    # [goal2/recon/gaps.md 1b #9].  `_cmd_start` in this module has ordered the same two answers this way
    # round since 0.4; this is the query catching up with the verb.  The other rows still print, because what
    # is or is not installed on this box is true whatever the session is.
    for i, line in enumerate(problem):
        _out(("problem:  " if i == 0 else "          ") + line)
    # Cinnamon mirrors over Eval and needs no wl-mirror binary, so a missing helper is not a failure there;
    # computed once (never raises) and reused by the capture row below.
    cin = cinnamon.available() if hit else False
    helper = core.find_helper()
    if helper:
        version = core.helper_version(helper)
        _out("helper:   %s%s" % (helper, " (%s)" % version if version else ""))
    elif cin:
        _out("helper:   not installed (not needed on Cinnamon: mirrors over %s)" % cinnamon.ROUTE)
    else:
        ok = False
        _out("helper:   not installed")
        for line in core.missing_helper_lines():
            _out("          " + line)
    _out("wayland:  %s" % (hit[2] if hit else "none"))
    if conn is None:
        return 1
    try:
        have = core.capture_support(conn)
        if have:
            _out("capture:  %s" % ", ".join("%s v%d" % (i, v) for i, v in have))
        elif cin:
            # no wlr capture protocol, but this is Cinnamon: the mirror rides org.Cinnamon.Eval and a Clutter
            # clone (AGENTS.md route 2, measured AE 0 on the rig -- see hacks/mirror/cinnamon.py), so this is a
            # session that CAN mirror, not one that cannot.
            _out("capture:  %s" % cinnamon.ROUTE)
        else:
            ok = False
            for i, line in enumerate(core.no_capture_lines()):
                _out(("capture:  " if i == 0 else "          ") + line)
        try:
            outputs = core.read_outputs(conn)
            on = [o for o in outputs if o.active]
            off = [o.name for o in outputs if not o.active]
            _out("outputs:  %s" % (", ".join("%s %s" % (o.name, o.geom()) for o in on) or "none"))
            if off:
                _out("          off: %s" % ", ".join(off))
        except core.Refusal as e:
            ok = False
            for i, line in enumerate(e.lines):
                _out(("outputs:  " if i == 0 else "          ") + line)
    finally:
        conn.close()
    _, recs, _changed = _state()
    if recs:
        for i, target in enumerate(sorted(recs)):
            _out(("mirrors:  " if i == 0 else "          ") + core.fmt_record(target, recs[target]))
    else:
        _out("mirrors:  none")
    return 0 if ok else 1


# -- starting one -------------------------------------------------------------

def _cmd_start(args) -> int:
    source, target = args.source, args.to
    region = core.parse_region(args.region) if args.region else None

    helper = core.find_helper()
    if helper is None:
        # on an X11 box the missing binary is not the point: there is no wl-mirror for X11 and never will be,
        # and `xrandr --same-as` is the answer. Say what is missing only where installing it would help.
        if session.find_wayland_socket() is None:
            raise core.Refusal(core.no_session_lines())
        # A wl-mirror-less Wayland session may still be Cinnamon, which mirrors over org.Cinnamon.Eval and a
        # Clutter clone (AGENTS.md route 2, measured AE 0 on the rig -- hacks/mirror/cinnamon.py) and needs no
        # helper at all. Only when it is NOT Cinnamon is the missing package the answer.
        if cinnamon.available():
            return _cinnamon_start(args, source, target, region)
        raise core.Refusal(core.missing_helper_lines())

    conn = core.open_conn()
    try:
        try:
            core.require_capture(conn)
            have = True
        except core.Refusal as e:
            have, cap_refusal = False, e
        outputs = core.read_outputs(conn)
    finally:
        conn.close()

    if have:
        with core.state_lock():
            return _start_locked(args, source, target, region, outputs, helper)
    # wl-mirror is installed but this compositor advertises no capture protocol. On Cinnamon that binary could
    # never capture anyway (no wlr protocol) -- the Eval clone is the route; elsewhere (GNOME, KDE) it is the
    # no-capture refusal, which already names the portal route.
    if cinnamon.available():
        return _cinnamon_start(args, source, target, region)
    raise cap_refusal


def _cinnamon_start(args, source, target, region) -> int:
    """The Cinnamon capture path: read the layout the same way the wl-mirror path does, then build the mirror
    inside muffin over Eval. Shares `core.decide`'s geometry policy verbatim -- the only difference from
    `_start_locked` is the thing that gets started."""
    conn = core.open_conn()
    try:
        outputs = core.read_outputs(conn)
    finally:
        conn.close()
    with core.state_lock():
        return _start_cinnamon_locked(args, source, target, region, outputs)


def _recorded_cinnamon(target: str, rec: dict) -> bool:
    """Is that cinnamon mirror really on disk? -- the token, not a pid, is its identity. A start that could not
    write the record down destroys the actor again rather than leave a mirror `--stop` cannot find."""
    try:
        on_disk = core.records(core.load_state()).get(target)
    except Exception:
        return False
    return (isinstance(on_disk, dict) and on_disk.get("kind") == "cinnamon"
            and on_disk.get("token") == rec.get("token"))


def _start_cinnamon_locked(args, source, target, region, outputs) -> int:
    state, recs, changed = _state()
    # --replace must not be destructive on a refusal: decide FIRST, with the record it would replace out of the
    # way, and only then stop it.
    running = {k: v for k, v in recs.items() if not (args.replace and k == target)}
    decision = core.decide(outputs, source, target, region, args.keep_layout, running)
    if decision.verdict != core.RUN:
        if changed:
            state.save()
        _err(decision.lines)
        return 0 if decision.verdict == core.DONE else 1

    src = core.by_name(outputs, source)
    dst = core.by_name(outputs, target)
    # A whole-output mirror onto a differently-sized head is a clone of the source's full rectangle; a --region
    # is that rectangle. Either way it is a layout rectangle, scaled into the viewport that covers the target.
    eff_region = region if region is not None else src.rect()
    # The mode the program is built with and the mode the record carries are the same string, resolved here
    # exactly as the wl-mirror path resolves it -- the parser leaves `--scaling` at None, and `fit` is the
    # default here as everywhere -- so the start line and `--list` say what the mirror is drawing rather than
    # what was typed.
    scaling = args.scaling or core.DEFAULT_SCALING
    if args.dry_run:
        if changed:
            state.save()
        _out("org.Cinnamon.Eval: " + cinnamon.build_scaled_program(eff_region, dst.rect(), scaling))
        return 0
    if target in recs:                       # --replace, and it is going
        _stop_any(recs.pop(target))

    err = cinnamon.start(recs, source, target, eff_region, dst, scaling=scaling)
    state.save()
    if err:
        _err(err)
        return 1
    rec = recs[target]
    if not _recorded_cinnamon(target, rec):
        _stop_any(rec)
        recs.pop(target, None)
        state.save()
        _err(["started the mirror but could not write it down in %s"
              % core.state_path(),
              "stopped it again rather than leave a mirror nothing can end"])
        return 1
    _out(core.fmt_record(target, rec))
    return 0


def _start_locked(args, source, target, region, outputs, helper) -> int:
    """Decide and start with the records held still.

    The lock spans read-decide-start-write because every part of it reads the file: two starts that both read it
    empty would both spawn a helper, and the second write would drop the first record -- leaving a wl-mirror
    fullscreen on the target that nothing here could find or stop."""
    state, recs, changed = _state()
    # --replace must not be destructive on a refusal: decide FIRST, with the
    # record it would replace out of the way, and only then stop it.
    running = {k: v for k, v in recs.items() if not (args.replace and k == target)}
    decision = core.decide(outputs, source, target, region, args.keep_layout, running)
    if decision.verdict != core.RUN:
        if changed:
            state.save()                  # the reap, if it found anything
        _err(decision.lines)
        return 0 if decision.verdict == core.DONE else 1

    # the flag's default lives here now (see parser()); wl-mirror's argv and the record are byte-identical to
    # what they were when argparse carried it.
    scaling = args.scaling or core.DEFAULT_SCALING
    argv = core.build_argv(source, target, region, scaling, helper)
    if args.dry_run:
        if changed:
            state.save()
        _out(" ".join(shlex.quote(a) for a in argv))
        return 0
    if target in recs:                       # --replace, and it is going
        supervise.stop_record(recs.pop(target))

    hit = session.find_wayland_socket()
    src = core.by_name(outputs, source)
    try:
        err = supervise.start(recs, source, target, argv, region=region,
                              scaling=scaling,
                              wayland_socket=hit[2] if hit else None,
                              src_rect=src.rect() if src else None)
    finally:
        # even a Ctrl-C in the second this blocks for must leave the mirror written down: the supervisor names
        # itself before it can fail, and an unrecorded helper is one nobody can stop.
        state.save()
    if err:
        _err(err)
        return 1
    rec = recs[target]
    if not core.recorded(target, rec):
        supervise.stop_record(rec)
        recs.pop(target, None)
        state.save()
        _err(["started %s but could not write it down in %s"
              % (core.HELPER, core.state_path()),
              "stopped it again rather than leave a mirror nothing can end"])
        return 1
    _out(core.fmt_record(target, rec))
    return 0


# -- entry --------------------------------------------------------------------

def _run(args, p) -> int:
    queries = [bool(args.list), bool(args.stop), bool(args.stop_all), bool(args.check)]
    if sum(queries) > 1:
        p.error("--list, --stop, --stop-all and --check are one at a time")
    if any(queries):
        if args.source or args.to:
            p.error("--list, --stop, --stop-all and --check take no outputs")
        if args.list:
            return _cmd_list()
        if args.stop:
            return _cmd_stop(args.stop)
        if args.stop_all:
            return _cmd_stop_all()
        return _cmd_check()
    if not args.source or not args.to:
        p.error("name the output to capture and the one to paint it on: " "wmirror SOURCE --to TARGET")
    return _cmd_start(args)


def main(argv=None) -> int:
    """wmirror never hands over to an X11 original: it has none. (warandr is
    the other tool in this box with no original; see passthrough.py.)"""
    stdio.repair_std()      # fd 1 or 2 closed before Python started
    quiet = False
    try:
        p = parser()
        args = p.parse_args(sys.argv[1:] if argv is None else list(argv))
        code = _run(args, p)
    except SystemExit as e:
        # argparse's --help/--version and its usage errors: they used to leave main() with the help text still
        # buffered, so a full or closed stdout became exit 120 out of the interpreter's own exit-time flush.
        stdio.exit_after_flush("wmirror", e)
        raise               # unreachable; the line above raises
    except core.Refusal as e:
        _err(e.lines)
        code = 1
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        code = 1
    except Exception as e:
        # never a traceback: a compositor that drops the connection mid-query, an unreadable state file, a
        # helper that vanishes between the check and the signal -- one line, exit 1.
        _err(["%s" % e])
        # An OSError here is a write to stdout that failed (a full disk, a quota, `>/dev/full`): the flush below
        # is about to fail with the same errno, and the originals print one line, not two.
        quiet = isinstance(e, OSError)
        code = 1
    return code if stdio.flush_stdout("wmirror", quiet) else (code or 1)
