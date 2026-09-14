"""The detached supervisor that owns one wl-mirror process.

The detaching, the status pipe and the /proc identity checks are w11common.procs -- the same protocol the gamma
holder runs on, written once and documented there.

One thing the holder does not have: the thing we spawn is not us, it is wl-mirror. So the record carries TWO
(pid, starttime) pairs. If our supervisor is killed the helper is still found and stopped by `wmirror --stop`;
if the helper dies on its own the supervisor exits and the record is reaped on the next `--list`.

The supervisor is what makes the mirror end honestly. It watches the output layout over zwlr_output_management
(the protocol wxrandr already speaks) and kills the helper when its source or target goes away, is switched off,
or the two come to share pixels -- the geometry in which wl-mirror would capture its own picture. wl-mirror
itself only handles the first of those: it exits when the SOURCE disappears, and it happily survives the target
disappearing, which on sway relocates the mirror window onto the source.
"""

import os
import select
import signal
import subprocess
import tempfile
import time

from w11common import procs
# the detach protocol and the pid-reuse guards, imported rather than copied: one implementation of "is that
# still the process we started?", and one of "start something that outlives us", in the tree.
from w11common.procs import alive, proc_starttime
from hacks.display import core as wxcore

from . import core

#: How long a start waits to see whether wl-mirror stays up. It reaches its
#: surface in ~150 ms and fails (bad output name, no protocol) faster than
#: that, so a second is generous; the command blocks for it, once.
STARTUP_SECONDS = 1.0
#: Longest a supervisor waits between polls with nothing to do.
POLL_SECONDS = 1.0
#: Longest a SIGTERM'd wl-mirror gets before SIGKILL. Kept well under the
#: second `procs.wait_gone` gives US after a SIGTERM, so a stop
#: normally ends with the supervisor exiting of its own accord rather than
#: being killed halfway through ending the helper. Normally is the word:
#: measured against the stub on this host, a SIGTERM to the supervisor ends
#: the helper in 2-4 ms, so this budget is not spent -- but only because
#: `_unblock_term` keeps the spawn-time SIGTERM block out of the helper. With
#: the mask inherited every stop spent all of it and ended in a SIGKILL.
STOP_SECONDS = 0.5


# -- identity -----------------------------------------------------------------

#: `--list` and `--stop` find our own supervisor by name only when the
#: record has no start time. `procs.alive` matches this against comm AND the
#: command line, and the command line is the half that matters: the process
#: is `python3` in every shape it ships in (a clone, a pyz, `python3 -m
#: wmirror`), so comm says "python3" and only the argv says "wmirror".
#: "python" used to be in this tuple, which made a `'?'` record claim every
#: python process this user was running -- a bystander `python3 -c "import
#: time; time.sleep(30)"` answered alive, and `--stop-all` would have
#: SIGTERMed it.
SUPERVISOR_COMM = ("wmirror",)


# -- records ------------------------------------------------------------------

def liveness(rec: dict) -> tuple:
    """(supervisor alive, helper alive) for one record."""
    return (alive(rec.get("pid"), rec.get("start"), SUPERVISOR_COMM),
            alive(rec.get("helper_pid"), rec.get("helper_start"),
                  core.HELPER))


def reap(recs: dict) -> bool:
    """Drop the records whose processes are gone, flag the ones whose supervisor died but whose helper is still
    painting.

    Mutates `recs`; returns whether anything changed, so a query that found nothing to correct does not rewrite
    the state file."""
    changed = False
    for target in list(recs):
        rec = recs[target]
        if not isinstance(rec, dict):
            del recs[target]
            changed = True
            continue
        sup, helper = liveness(rec)
        if not sup and not helper:
            del recs[target]
            changed = True
            continue
        orphan = bool(helper and not sup)
        if rec.get("orphan") != orphan:
            rec["orphan"] = orphan
            changed = True
    return changed


def stop_record(rec: dict) -> bool:
    """Stop one mirror: the supervisor first (it forwards the signal and waits), then the helper directly, in
    case the supervisor was killed before it could. True if anything was actually running."""
    killed = False
    if alive(rec.get("pid"), rec.get("start"), SUPERVISOR_COMM):
        killed = procs.kill_bounded(rec["pid"], rec.get("start")) or killed
    if alive(rec.get("helper_pid"), rec.get("helper_start"), core.HELPER):
        killed = procs.kill_bounded(rec["helper_pid"], rec.get("helper_start")) or killed
    return killed


# -- starting -----------------------------------------------------------------

def start(recs: dict, source: str, target: str, argv: list, region=None,
          scaling: str = core.DEFAULT_SCALING, wayland_socket=None,
          src_rect=None):
    """Spawn the detached supervisor for one mirror.

    Writes the record into `recs` as soon as the supervisor names itself -- before anything can go wrong -- so a
    start that then hangs is still stoppable. Returns None on success, else the lines to print (the record is
    removed again when the helper reported a clean failure)."""
    rec = {"source": source, "target": target,
           "region": list(region) if region else None,
           "scaling": scaling, "argv": list(argv), "since": int(time.time())}
    named = False

    def on_line(line: str) -> bool:
        nonlocal named
        parts = line.split()
        if line.startswith("pid ") and len(parts) == 3:
            rec["pid"], rec["start"] = int(parts[1]), parts[2]
            recs[target] = rec           # stoppable from here on
            named = True
            return True
        if line.startswith("helper ") and len(parts) == 3:
            rec["helper_pid"] = int(parts[1])
            rec["helper_start"] = parts[2]
            return True
        return False

    def child(status_fd):
        supervisor_main(argv, source, target, status_fd=status_fd,
                        wayland_socket=wayland_socket, region=region,
                        src_rect=src_rect)

    status = procs.spawn_detached(child, STARTUP_SECONDS + 4.0, on_line)

    if status == "ok":
        recs[target] = rec
        return None
    if status is not None:
        recs.pop(target, None)
        msg = status[len("failed "):] if status.startswith("failed ") else status
        return ["%s did not start: %s" % (core.HELPER, msg)]
    if named:                 # no verdict, but it named itself: keep it
        recs[target] = rec
        return ["%s did not report that it started" % core.HELPER,
                "if it is running, `wmirror --list` shows it and "
                "`wmirror --stop %s` stops it" % target]
    return ["%s could not be started (no answer from the supervisor)" % core.HELPER]


# -- the detached supervisor --------------------------------------------------

def _on_term(signum, frame):
    raise SystemExit(0)


def _unblock_term():
    """Run in the forked child, before exec: undo the supervisor's SIGTERM block.

    CPython restores the PARENT's pre-fork signal mask in the child (3.14.4's fork_exec does it right before
    exec) and execve keeps the mask, so a helper spawned inside the block above inherits it: measured here,
    /proc/<helper>/status said `SigBlk: 0000000000004000` -- bit 14, SIGTERM -- and the same plain `Popen(["sleep"])`
    under a blocked SIGTERM shows it too, while this preexec_fn brings it back to 0. A helper that ignores SIGTERM
    is not academic: `_stop_child` terminates then waits STOP_SECONDS before SIGKILL, so every `wmirror --stop`
    ended wl-mirror by SIGKILL 0.502 s later instead of by SIGTERM in 2-4 ms (both measured with the stub, this
    host). preexec_fn is safe here because the supervisor is single-threaded -- it is the grandchild of
    procs.spawn_detached and has started no thread."""
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})


#: `error:` lines wl-mirror prints while it is working perfectly. Measured
#: on the rig: a mirror that ran for minutes, and was pixel-checked, printed
#: `error: mirror-screencopy::on_dmabuf_allocated(): failed to allocate
#: dmabuf` and then fell back to shm. Blaming one of these for an exit turns
#: "it crashed, here is why" into a confident wrong answer.
BENIGN_ERROR = ("dmabuf", "libegl", "libgl", "mesa", "dri2", "dri3")


def _benign(line: str) -> bool:
    low = line.lower()
    return any(word in low for word in BENIGN_ERROR)


def _exit_words(rc) -> str:
    """How the helper ended, when it did not say so itself."""
    if isinstance(rc, int) and rc < 0:
        try:
            name = signal.Signals(-rc).name
        except ValueError:
            name = "signal %d" % -rc
        return "%s was killed by %s" % (core.HELPER, name)
    return "%s exited with status %s" % (core.HELPER, rc)


def _diagnosis(tail: list, rc) -> str:
    """What to blame when wl-mirror exits during the startup window.

    Its own `error:` line if it printed a fatal one. Nothing here reads stderr as failure on its own: libEGL
    prints both `warning:` and `error:` lines there while wl-mirror works perfectly, so these lines are only
    ever consulted for a process that has actually exited -- and even then the chatter is skipped, because a
    helper that dies for some other reason (a signal, an exit with nothing said) must not be reported in the
    words of an error it survived."""
    lines = [line.strip() for line in tail if line.strip()]
    for want_prefix in (True, False):
        for line in reversed(lines):
            hit = (line.startswith("error:") if want_prefix else "error" in line.lower())
            if hit and not _benign(line):
                return line
    return _exit_words(rc)


class _Stderr:
    """The helper's stderr, on an unlinked temp file rather than a pipe.

    A pipe would tie wl-mirror's life to ours twice over: it fills if nobody drains it (stalling the helper),
    and after our own death its first write would be SIGPIPE -- so a mirror that is supposed to survive a killed
    supervisor, and stay stoppable, would die at an unpredictable moment instead. A file does neither. The child
    gets its own O_APPEND descriptor so our reads never move its write offset, and the file is truncated when it
    grows: `-v` writes ~20 kB/s, and nothing here needs more than the last few lines."""

    CAP = 64 * 1024

    def __init__(self):
        fd, path = tempfile.mkstemp(prefix="wmirror-helper-")
        self.wfd = os.open(path, os.O_WRONLY | os.O_APPEND)
        os.unlink(path)
        self.fd = fd

    def tail(self) -> list:
        try:
            size = os.fstat(self.fd).st_size
            start = max(0, size - self.CAP)
            data = os.pread(self.fd, min(size, self.CAP), start)
        except OSError:
            return []
        return data.decode(errors="replace").splitlines()

    def trim(self):
        try:
            if os.fstat(self.fd).st_size > self.CAP:
                os.ftruncate(self.fd, 0)      # O_APPEND: the child follows
        except OSError:
            pass

    def close_write(self):
        if self.wfd is not None:
            os.close(self.wfd)
            self.wfd = None

    def close(self):
        self.close_write()
        try:
            os.close(self.fd)
        except OSError:
            pass


def supervisor_main(argv: list, source: str, target: str, status_fd=None,
                    wayland_socket=None, region=None, src_rect=None) -> int:
    """Run wl-mirror, report the start, then own it until it must end."""

    def emit(msg: str, close: bool = False):
        procs.emit(status_fd, msg, close)

    # name ourselves before anything can fail (the protocol's rule)
    emit("pid %d %s" % (os.getpid(), proc_starttime(os.getpid()) or "?"))
    # The handler goes on BEFORE the helper exists, and SIGTERM is blocked
    # across the spawn itself. Installed after Popen (as it was) there were
    # two windows in which `wmirror --stop` killed the supervisor outright:
    # the default disposition meant WIFSIGNALED 15 with the finally clause
    # never entered, and wl-mirror was left fullscreen on the target with
    # nobody watching it. Blocking is the second half, and it is not
    # theoretical: a signal delivered between fork/exec and the assignment
    # unwinds out of the Popen call itself, so `proc` is never bound and the
    # helper we just started is a pid we no longer hold. Blocked, that
    # SIGTERM stays pending until the mask is restored one line later, by
    # which time _stop_child has something to stop.
    signal.signal(signal.SIGTERM, _on_term)
    stderr = _Stderr()
    proc = None
    try:
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=stderr.wfd, env=helper_env(
                                        wayland_socket),
                                    preexec_fn=_unblock_term)
        except OSError as e:
            emit("failed cannot run %s: %s" % (argv[0], e), close=True)
            return 1
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        stderr.close_write()      # only the helper holds the write end now
        emit("helper %d %s" % (proc.pid, proc_starttime(proc.pid) or "?"))
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                emit("failed %s" % _diagnosis(stderr.tail(), proc.returncode), close=True)
                return 1
            time.sleep(0.02)
        emit("ok", close=True)
        _supervise(proc, stderr, source, target, wayland_socket, region=region, src_rect=src_rect)
    finally:
        _stop_child(proc)
        stderr.close()
    return 0


def helper_env(wayland_socket):
    """The environment wl-mirror is run with. wmirror works from a hotkey, from `sudo` and from `ssh root@box`
    with an empty environment, because the session's socket is found by scanning /run/user/* -- so the helper is
    told which one rather than left to read a WAYLAND_DISPLAY that may not be there. libwayland takes an
    absolute path in that variable."""
    if not wayland_socket:
        return None
    env = dict(os.environ)
    env["WAYLAND_DISPLAY"] = wayland_socket
    env["XDG_RUNTIME_DIR"] = os.path.dirname(wayland_socket)
    return env


def _stop_child(proc):
    # proc is None when the spawn itself never returned a handle -- an OSError
    # from Popen, or a SIGTERM that unwound out of it.
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=STOP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass


def _open_watch(wayland_socket):
    """A zwlr_output_manager view of the layout, or None. A supervisor without one still owns its helper; it
    just cannot notice an output disappearing before wl-mirror does."""
    if not wayland_socket:
        return None
    try:
        from w11common.wayland_mini import WlConn
        conn = WlConn(wayland_socket)
        return wxcore.WlrOutputs(conn=conn)
    except Exception:
        return None


def _supervise(proc, stderr, source, target, wayland_socket, region=None, src_rect=None):
    """Until the helper dies, an output change makes the mirror impossible,
    or the compositor goes away."""
    wlr = _open_watch(wayland_socket)
    serial = getattr(wlr, "serial", None)
    while True:
        if wlr is not None:
            try:
                ready, _, _ = select.select([wlr.conn.sock.fileno()], [], [], POLL_SECONDS)
            except InterruptedError:
                ready = []
            if ready:
                try:
                    wlr.conn.dispatch(timeout=0.1)
                except Exception:
                    return      # compositor gone: the helper goes with it
                if wlr.serial != serial:
                    serial = wlr.serial
                    if core.watch_reason(wxcore.snapshot_wlr(wlr),
                                         source, target, region=region,
                                         src_rect=src_rect):
                        return
        else:
            time.sleep(POLL_SECONDS)
        if stderr is not None:
            stderr.trim()
        if proc.poll() is not None:
            return
