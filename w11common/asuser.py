"""One function run as another uid: the half of a command that has to happen inside somebody else's home.

Root over ssh and `sudo` are documented ways to drive these tools (docs/Technical.md section 12), and the
session scan in `w11common/session.py` finds the seated user's sway or Hyprland socket across uids -- so the
IPC half of `wxrandr --persistent` crosses the uid boundary and lands on their screen.  `$HOME` does not
cross it: it is still the caller's, so the file half went into `/root/.config/sway/w11-outputs.conf`, a file
that sway, running as uid 1000, has never opened.  Writing it into THEIR `~/.config` as root is not the fix
by itself either, because root follows whatever symlink is waiting there -- measured 2026-09-17 with a
throwaway account (`tests/test_asuser_root.py` repeats it under sudo): with `~/.config/sway/w11-outputs.conf.new`
planted as a symlink to `/etc/w11target/pwned`, a plain root write created that file as root:root with our
header in it, and the same write in a child dropped to the account got EACCES and wrote nothing.  So the
write happens as the user: a forked child that IS them, whose every open is checked against their own
permissions exactly as if they had run the command themselves.

The shape is `hacks/input/xkbmap.py`'s `_read_all_as` (:1353): fork, close every descriptor but the pipe,
drop, do the work, write a JSON status up the pipe, `os._exit`, and a parent that kills a child which
outstays its deadline.  Four things differ, and each is here for the caller this module has:

  * `os.initgroups`, not `os.setgroups([])`.  The sibling drops to a uid to make a D-Bus connection that
    the bus attributes to it; this child IS the user for the length of a write into their home, so it gets
    their supplementary groups too -- a home on a group-writable share is theirs to write.
  * `HOME` is rewritten from the passwd entry and `XDG_CONFIG_HOME` is dropped, so that
    `core.sway_config_dir()`, `core._sway_user_configs()` and `hypr.hypr_config_dir()` -- all three of which
    read the environment -- recompute in the child to the very path `core._seated_config_path()` already
    prints in the notes.  That keeps one seam: the path the failure note names and the path the child writes
    cannot disagree, because both come from the passwd database.
  * The child's stderr is RELAYED rather than written.  `core.warn()` resolves `sys.stderr` per call, and in
    the child that is a copy of whatever the parent had -- under `contextlib.redirect_stderr`, which is how
    every unit test here reads a note, a StringIO whose contents die with `os._exit`.  So the child swaps in
    its own StringIO, the JSON carries the text, and the parent writes it to its own stderr before its own
    line: every note the child printed is observable, and the order is deterministic (child first).
  * stdout and stderr are flushed before the fork.  A block-buffered stdout -- which is what a tool's stdout
    is when it is a file or a pipe rather than a terminal -- is otherwise duplicated into the child and
    written a second time when it flushes (measured; the subprocess case in tests/test_asuser.py pins it).

Standard library only, and no import of `hacks`: `w11common` is the closed package a display tool's zipapp
carries, so `home_of` below is a duplicate of `hacks/display/monitors_xml.home_of` rather than a call to it.
The two siblings that fork and drop -- `w11common/dbus_mini._drop_privileges` and `xkbmap._read_all_as` --
are left exactly as they are: each carries the reasons of its own caller (the bus's SO_PEERCRED pinning,
PR_SET_DUMPABLE for the portal) and folding three callers into one function would make all three of them
argue about which of those apply.
"""

import io
import json
import os
import pwd
import select
import signal
import sys
import time
import warnings

#: Seconds past the caller's `timeout` before an overstaying child is SIGKILLed.  The caller's deadline is
#: about the work; this is about a child that is not doing it any more (a test patches it down).
GRACE = 5.0


def home_of(uid) -> "str | None":
    """`pw_dir` of that account, or None when there is no such account (or no passwd database to ask, which
    is what a minimal container is).

    The twin of `hacks/display/monitors_xml.home_of`, duplicated rather than imported because `w11common`
    imports nothing from `hacks` -- see the module docstring."""
    try:
        return pwd.getpwuid(uid).pw_dir or None
    except (KeyError, OSError, TypeError, OverflowError):
        return None


def name_of(uid) -> "str | None":
    """`pw_name` of that account, or None -- what a note calls the user when it has a name to call them."""
    try:
        return pwd.getpwuid(uid).pw_name or None
    except (KeyError, OSError, TypeError, OverflowError):
        return None


def _fd_bound() -> int:
    """One past the highest descriptor worth closing, the way `xkbmap._fd_bound` computes it -- and clamped
    for the same reason: a container can report SC_OPEN_MAX = 1073741816 and closerange() would walk every
    one of them."""
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (ValueError, OSError):
        limit = 4096
    if not isinstance(limit, int) or limit < 3 or limit > 65536:
        limit = 65536
    return limit


def _drop(uid: int, ent) -> None:
    """Become `uid` for good: supplementary groups, gid, uid, in that order and no other.

    `initgroups` first, because it is the one call here that still needs the privilege it is giving up, and
    `setuid` last for the same reason.  The check after it is not ceremony: `setuid` on a process that kept
    a saved-set uid is reversible, and a child that could climb back is not the thing this module claims to
    hand the write to."""
    os.initgroups(ent.pw_name, ent.pw_gid)
    os.setgid(ent.pw_gid)
    os.setuid(uid)
    if os.getuid() != uid or os.geteuid() != uid:
        raise PermissionError("uid %d did not stick" % uid)


def _error_text(e: BaseException) -> str:
    """The child's exception as the one line a note will quote.

    `Fatal` and `CmdError` are this tree's user-facing refusals: their text is already the sentence the user
    is owed, so it crosses as itself (minus the trailing newline a `Fatal` carries, because the note that
    quotes it has its own).  Everything else is a defect rather than a refusal and is named by type as well,
    because `[Errno 13] Permission denied: '...'` without `PermissionError` in front of it reads like a
    sentence we wrote."""
    if type(e).__name__ in ("Fatal", "CmdError"):
        return str(e).rstrip("\n")
    return "%s: %s" % (type(e).__name__, e)


def _read_until_eof(fd: int, pid: int, timeout: float) -> bytes:
    """Everything the child writes before it closes, or b"" if it outstays `timeout`.

    Drained as it arrives rather than after the wait, which is what keeps a long relayed stderr from
    deadlocking against the 64 KiB pipe buffer: a child blocked in `write` would never reach `_exit` and the
    parent would never reach the read.  `xkbmap._read_until_eof` (:1440) for the same reason."""
    deadline = time.monotonic() + timeout
    buf = b""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            _kill(pid)
            return b""
        try:
            if not select.select([fd], [], [], left)[0]:
                continue
            chunk = os.read(fd, 65536)
        except OSError:
            return buf
        if not chunk:
            return buf
        buf += chunk


def _kill(pid: int):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _reap(pid: int):
    """Wait for the child, so a command that runs several of these leaves no zombies behind.  Its own
    function so a test can wrap it and learn the pid the fork produced."""
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass


def run_as_uid(uid: int, fn, timeout: float = 10.0) -> "tuple[bool, str | None]":
    """Run `fn()` in a forked child that IS `uid`, and say whether it got there.

    `(True, None)` when the child ran `fn` to the end, or `(False, reason)` where `reason` is one of:

      * `uid N has no passwd entry` -- nothing to drop to, and no fork is made;
      * `uid N has no home directory in the passwd database` -- same, before the fork;
      * the child's own error text: `PermissionError: [Errno 1] Operation not permitted` when the caller is
        not root at all, `PermissionError: [Errno 13] Permission denied: <path>` when something in their
        config directory refuses them (a symlink of theirs pointing somewhere they cannot write), or the
        bare sentence of a `Fatal` the work itself raised;
      * `the writer for uid N said nothing within T s and was killed` (T is `timeout` + `GRACE`) -- the
        child died without a word or outstayed the deadline and was SIGKILLed.

    There is no root pre-check in front of any of it: a non-root caller's child fails at `initgroups` with
    the EPERM above, which is the same answer one sentence later and keeps the fork -- the part the unit
    tests exercise with `_drop` stubbed -- on one path for every caller.

    Anything `fn` mutates is mutated in the child and is lost: the caller gets the two-tuple and whatever
    reached the filesystem, and nothing else.  `hypr.HyprOutputs._apply_by_reload` is the case that has to
    know it -- its `self.rows` and `self.mirrors` come back from the parent's own re-read."""
    try:
        ent = pwd.getpwuid(uid)
    except (KeyError, OverflowError, TypeError):
        return False, "uid %d has no passwd entry" % uid
    if not ent.pw_dir:
        return False, "uid %d has no home directory in the passwd database" % uid
    # Before the fork, not after: a block-buffered stdout is inherited with its buffer and written twice.
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    with warnings.catch_warnings():
        # 3.12 warns about fork() in a threaded process; the child writes a file and _exit()s, and never
        # touches a lock the interpreter took in another thread.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:                                      # child
        out = {"ok": False, "error": "the child fell off the end"}
        cap = io.StringIO()
        try:
            # Everything but `w`, before the drop.  fork() hands this child the whole fd table of whatever
            # was running -- the compositor IPC socket, the state file's lock, a bus connection -- and the
            # next line makes it the seated user, who is exactly who should not be holding root's open
            # descriptors.  `xkbmap._read_all_as` closes them for the same reason and measured the
            # difference: 12 entries in /proc/self/fd against the 2 it has with the two closeranges.
            os.close(r)
            os.closerange(3, w)
            os.closerange(w + 1, _fd_bound())
            sys.stderr = cap
            _drop(uid, ent)
            # The environment the work reads its paths out of is the caller's; from here it is theirs.
            os.environ["HOME"] = ent.pw_dir
            os.environ.pop("XDG_CONFIG_HOME", None)
            fn()
            out = {"ok": True}
        except BaseException as e:
            out = {"ok": False, "error": _error_text(e)}
        out["stderr"] = cap.getvalue()
        try:
            os.write(w, json.dumps(out).encode())
        except BaseException:
            pass
        os._exit(0 if out["ok"] else 1)
    os.close(w)
    try:
        raw = _read_until_eof(r, pid, timeout + GRACE)
    finally:
        os.close(r)
        _reap(pid)
    try:
        out = json.loads(raw) if raw else None
    except ValueError:
        out = None
    if not isinstance(out, dict):
        return False, ("the writer for uid %d said nothing within %g s and was killed"
                       % (uid, timeout + GRACE))
    if out.get("stderr"):
        sys.stderr.write(out["stderr"])
    if not out.get("ok"):
        return False, out.get("error") or ("the writer for uid %d failed" % uid)
    return True, None
