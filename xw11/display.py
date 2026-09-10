"""Being a display: the number, both sockets, the lock file, the xauth entry
and the file that says where the proxy is.

Everything here is a measurement of what a real X server does on this box.

* **Both sockets, abstract first.** A Linux X server binds `/tmp/.X11-unix/XN`
  *and* the abstract `@/tmp/.X11-unix/XN` (measured on sway's own Xwayland,
  recon/wire.md 9.1), and libxcb tries the abstract name first: with a listener
  on both names of `:134`, `xdpyinfo` was accepted on the abstract one
  (re-measured 2026-09-10 for this batch, scratchpad/b1/sockpref.py, libxcb
  1.17.0). A proxy that binds only the path is silently bypassed by anything
  already holding the abstract name -- which happened during recon, twice, with
  no error anywhere (recon/wire.md 9.2). So both are bound, the abstract one
  first, and `EADDRINUSE` on either means the number is taken.
* **A file is not evidence.** `/tmp/.X11-unix/X8` and `X9` exist on this box with
  no listener at either name (recon/env.md 4). A filesystem socket is probed with
  `connect()`: refused means stale and is unlinked, accepted means taken.
* **The lock file is the gate a second X server hits**, not the socket:
  `/tmp/.XN-lock`, mode 0444, 11 bytes, the pid right-justified in 10 columns
  plus a newline (recon/env.md 4, read off a live Xvfb's refusal).
* **The xauth entry is the whole authentication design.** The server matches on
  (protocol name, cookie bytes) and ignores the display recorded in the client's
  entry, and it re-reads its own `-auth` file: a cookie appended for `:N` after
  the server started was accepted with no restart (recon/wire.md 8.3, measured
  twice). So the proxy appends one record for its own number to the file
  Xwayland's cookie came from, forwards every setup verbatim, and removes the
  record at exit. The `xauth` binary is not a dependency; the record is 20 lines
  of the big-endian format of recon/wire.md 8.1.

Teardown unlinks exactly what this process created, in reverse. The hazard is
measured too: `xtrace -D :1 -d :1` unlinked a LIVE `/tmp/.X11-unix/X1` and left
the path dead while the abstract name kept serving, and every libxcb client kept
working, so nothing reported it (recon/env.md 4).
"""

import os
import socket
import stat
import struct

from wdotool import x11_mini

#: Where the sockets live. Read at call time and not captured, so that a test
#: pointing `x11_mini._SOCK_DIR` at a temp directory moves the proxy and the
#: client that dials it together.
_LOCK_DIR = "/tmp"

#: The scan, when no number was asked for: below the rigs' 70..119
#: (tests/support.py:HeadlessXvfb) and the parity oracle's :99, above the
#: session servers' low numbers.
FIRST_NUM = 20
LAST_NUM = 69

#: `$XDG_RUNTIME_DIR/xw11/`, and the one file in it.
DIR_NAME = "xw11"
FILE_NAME = "display"

MIT_COOKIE = b"MIT-MAGIC-COOKIE-1"
FAMILY_LOCAL = 256


class DisplayError(Exception):
    """No display number could be taken, or the one asked for is in use."""


def sock_dir() -> str:
    return x11_mini._SOCK_DIR


def sock_path(num: int) -> str:
    return "%s/X%d" % (sock_dir(), num)


def lock_path(num: int) -> str:
    return "%s/.X%d-lock" % (_LOCK_DIR, num)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True     # somebody else's live process
    return True


def _lock_holder(path: str):
    """The pid in an X lock file, or None when there is no readable one. A lock
    whose contents are not a pid is treated as held: X's own message tells the
    user to remove it by hand, and so do we."""
    try:
        with open(path, "rb") as f:
            raw = f.read(64)
    except FileNotFoundError:
        return None
    except OSError:
        return -1
    try:
        return int(raw.strip() or b"-1")
    except ValueError:
        return -1


def _socket_answers(path: str) -> bool:
    """Is there a listener at this filesystem path? A refusal means the file is
    the corpse of a dead server and may be unlinked; an accepted connection
    means the number is somebody's."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _bind(name: str):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(name)
        s.listen(64)
    except OSError:
        s.close()
        raise
    return s


class Display:
    """One taken display number and everything this process created for it."""

    def __init__(self, num, abs_sock, fs_sock, lock):
        self.num = num
        self.abs_sock = abs_sock
        self.fs_sock = fs_sock
        self.lock_path = lock
        self.fs_path = sock_path(num)
        self.abs_name = "\0" + self.fs_path
        #: (path, record) of the xauth entry we appended, or None
        self.xauth = None
        #: the display file, once written
        self.file_path = None
        self._released = False

    @property
    def name(self) -> str:
        """Always `:N`. Never `unix:N`: libxcb 1.17 refuses that spelling
        outright (recon/wire.md 9.3)."""
        return ":%d" % self.num

    def sockets(self):
        return [s for s in (self.abs_sock, self.fs_sock) if s is not None]

    def write_display_file(self, upstream: str) -> str:
        """`:N <pid> <upstream>` in `$XDG_RUNTIME_DIR/xw11/display`. One line,
        no second control protocol: a reader dials the abstract socket it names
        and checks who answers (`read_display`)."""
        path = display_file_path()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        tmp = path + ".%d" % os.getpid()
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, ("%s %d %s\n" % (self.name, os.getpid(), upstream)).encode())
        finally:
            os.close(fd)
        os.rename(tmp, path)
        self.file_path = path
        return path

    def add_xauth(self, upstream_num: int) -> bool:
        """Append our number's entry to the file Xwayland's cookie came from.
        No cookie (sway starts Xwayland with no `-auth`, recon/env.md 2) means
        nothing is written and the empty auth is forwarded, which is what was
        measured to work there."""
        path, cookie = find_cookie(upstream_num)
        if not path or not cookie:
            return False
        rec = xauth_append(path, self.num, cookie)
        self.xauth = (path, rec)
        return True

    def release(self):
        """Unlink exactly what was created, in reverse, and never anything
        else."""
        if self._released:
            return
        self._released = True
        if self.file_path:
            _unlink(self.file_path)
            self.file_path = None
        if self.xauth:
            path, rec = self.xauth
            xauth_remove_record(path, rec)
            self.xauth = None
        if self.lock_path:
            _unlink(self.lock_path)
            self.lock_path = None
        for s in (self.fs_sock, self.abs_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self.fs_sock = self.abs_sock = None
        _unlink(self.fs_path)


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _lstat(path):
    """`os.lstat`, or None when there is nothing there. `lstat` and not `stat`:
    a symlink at XN is the link itself, never what it points at."""
    try:
        return os.lstat(path)
    except OSError:
        return None


def _take(num: int, force: bool):
    """Try to become `:num`. Returns a Display, or None when it is taken and we
    are scanning. Raises DisplayError when the caller named this number."""
    lock = lock_path(num)
    holder = _lock_holder(lock)
    if holder is not None:
        if holder == -1 or _pid_alive(holder):
            if force:
                raise DisplayError("display :%d is locked by pid %s (%s)"
                                   % (num, holder, lock))
            return None
        _unlink(lock)       # a dead server's lock, the case X tells users to clear by hand
    fs = sock_path(num)
    st = _lstat(fs)
    if st is not None:
        if not stat.S_ISSOCK(st.st_mode):
            # recon/env.md 4 measured stale SOCKETS (/tmp/.X11-unix/X8 and X9,
            # nothing listening at either name): those are a dead server's
            # corpse and are cleared. A regular file, a directory or a symlink
            # at XN is not that -- it is somebody else's, this process did not
            # create it, and the rule of this module is to unlink exactly what
            # it created. So the number is taken and the scan moves on.
            if force:
                raise DisplayError("%s exists and is not a socket" % fs)
            return None
        if _socket_answers(fs):
            if force:
                raise DisplayError("display :%d already answers on %s" % (num, fs))
            return None
        _unlink(fs)
    abs_sock = fs_sock = None
    try:
        abs_sock = _bind("\0" + fs)
    except OSError as e:
        if force:
            raise DisplayError("cannot bind the abstract socket of :%d: %s" % (num, e)) from None
        return None
    try:
        fs_sock = _bind(fs)
    except OSError as e:
        abs_sock.close()
        if force:
            raise DisplayError("cannot bind %s: %s" % (fs, e)) from None
        return None
    try:
        # 11 bytes, mode 0444, the pid right-justified in 10 columns plus a
        # newline: byte for byte what Xvfb writes and what its "Server is
        # already active" refusal reads back (recon/env.md 4).
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        try:
            os.write(fd, b"%10d\n" % os.getpid())
        finally:
            os.close(fd)
    except OSError as e:
        abs_sock.close()
        fs_sock.close()
        _unlink(fs)
        if force:
            raise DisplayError("cannot create %s: %s" % (lock, e)) from None
        return None
    return Display(num, abs_sock, fs_sock, lock)


def allocate(display=None) -> Display:
    """Take a display number and bind both of its sockets.

    `display` (or `$XW11_DISPLAY`) names one and there is no scan: a caller who
    said `:42` gets `:42` or an error, because a script that exported
    `DISPLAY=:42` must not end up talking to something else. Otherwise the first
    free number in 20..69, decided by BINDING and never by looking.
    """
    if display is None:
        display = os.environ.get("XW11_DISPLAY") or None
    if display is not None:
        num, _screen = x11_mini._parse_display(display)
        got = _take(num, force=True)
        if got is None:                                  # pragma: no cover - force raises
            raise DisplayError("display :%d is taken" % num)
        return got
    for num in range(FIRST_NUM, LAST_NUM + 1):
        got = _take(num, force=False)
        if got is not None:
            return got
    raise DisplayError("no free display number in :%d..:%d" % (FIRST_NUM, LAST_NUM))


# -- the display file ---------------------------------------------------------


def display_file_path() -> str:
    from w11common import session
    return os.path.join(session.runtime_dir(), DIR_NAME, FILE_NAME)


def read_display_file(path=None):
    """`(":N", pid, upstream)` off the display file, or None."""
    try:
        with open(path or display_file_path(), "r", encoding="utf-8") as f:
            parts = f.readline().split()
    except (OSError, ValueError):
        return None
    if len(parts) < 2:
        return None
    try:
        pid = int(parts[1])
    except ValueError:
        return None
    return parts[0], pid, (parts[2] if len(parts) > 2 else "")


def _peer(sock):
    """(pid, uid) at the other end of a connected AF_UNIX socket."""
    try:
        data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                               struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", data)
        return pid, uid
    except (OSError, AttributeError, struct.error):
        return None, None


def read_display(path=None):
    """The `:N` of a running proxy, or None.

    The file is a hint and the socket is the proof: we dial the abstract name it
    names and refuse anything that is not the pid the file claims, running as
    us. A display file in a shared runtime directory can be planted, and what
    goes down that socket is every keystroke a wrapped `xdotool type` sends --
    the same reason `DaemonClient._try_connect` checks SO_PEERCRED before it
    speaks (wdotool/daemon.py:2072).
    """
    got = read_display_file(path)
    if not got:
        return None
    name, pid, _upstream = got
    try:
        num, _screen = x11_mini._parse_display(name)
    except Exception:
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect("\0" + sock_path(num))
    except OSError:
        s.close()
        return None
    try:
        peer_pid, peer_uid = _peer(s)
    finally:
        s.close()
    if peer_uid is not None and peer_uid != os.geteuid():
        return None
    if peer_pid is not None and peer_pid != pid:
        return None
    return ":%d" % num


# -- xauth --------------------------------------------------------------------


def xauth_record(num: int, cookie: bytes, host=None) -> bytes:
    """One `.Xauthority` record for `:num`, in the big-endian format of
    recon/wire.md 8.1: family 256 (FamilyLocal) and the real hostname, which is
    the pair a client's lookup keys on first
    (`x11_mini._auth_candidates`)."""
    host = (host or x11_mini.hostname()).encode()
    out = struct.pack(">H", FAMILY_LOCAL)
    for field in (host, str(num).encode(), MIT_COOKIE, cookie):
        out += struct.pack(">H", len(field)) + field
    return out


def _open_regular(path: str, flags: int):
    """Open a path that must be a regular file we own. An XAUTHORITY pointing
    at a FIFO must not block us and a symlink into somebody else's file must not
    be written through -- the rule `x11_mini._read_xauth` already applies to
    reading, applied to writing."""
    # O_NONBLOCK as well as O_NOFOLLOW: opening a FIFO for writing BLOCKS until
    # somebody opens the read end, which would hang the proxy at startup on an
    # XAUTHORITY somebody pointed at one. With it the open fails ENXIO instead,
    # and a FIFO opened read-write still fails the regular-file check below.
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("%s is not a regular file" % path)
    except OSError:
        os.close(fd)
        raise
    return fd


def xauth_append(path: str, num: int, cookie: bytes) -> bytes:
    """Append the entry for `:num` and return the record's bytes, which is what
    `xauth_remove_record` takes back off again."""
    rec = xauth_record(num, cookie)
    fd = _open_regular(path, os.O_WRONLY | os.O_APPEND)
    try:
        os.write(fd, rec)
    finally:
        os.close(fd)
    return rec


def xauth_remove_record(path: str, rec: bytes) -> bool:
    """Take exactly the bytes we appended back out, leaving the file
    byte-identical to what it was. Cutting the record out rather than
    re-serialising what parses keeps a file with a truncated tail (which
    `_read_xauth` tolerates on purpose) exactly as it was found."""
    try:
        fd = _open_regular(path, os.O_RDWR)
    except OSError:
        return False
    try:
        data = b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            data += chunk
            if len(data) > x11_mini._MAX_XAUTH_BYTES:
                return False
        at = data.rfind(rec)
        if at < 0:
            return False
        data = data[:at] + data[at + len(rec):]
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, data)
        os.ftruncate(fd, len(data))
    finally:
        os.close(fd)
    return True


def xauth_remove(path: str, num: int) -> bool:
    """Remove our own entry for `:num` -- the record with family 256, this
    host, that number and an MIT cookie -- without touching anything else."""
    try:
        entries = x11_mini._read_xauth(path)
    except OSError:
        return False
    host = x11_mini.hostname().encode()
    want = str(num).encode()
    for family, addr, number, name, data in entries:
        if (family, addr, number, name) == (FAMILY_LOCAL, host, want, MIT_COOKIE):
            if xauth_remove_record(path, xauth_record(num, data)):
                return True
    return False


def find_cookie(upstream_num: int):
    """`(path, cookie)` for the upstream display: the cookie
    `x11_mini._auth_candidates` prefers, and the file it is in -- the file the
    server was started with, which is the one it re-reads.

    `(None, None)` when there is none, which is the normal wlroots case and is
    not a failure: the cookie-less same-uid setup is forwarded and accepted
    (recon/env.md 2).
    """
    wanted = [(n, d) for n, d in x11_mini._auth_candidates(upstream_num)
              if n == MIT_COOKIE and d]
    if not wanted:
        return None, None
    paths = []
    for p in (os.environ.get("XAUTHORITY") or os.path.expanduser("~/.Xauthority"),
              x11_mini._session_xauthority()):
        if p and p not in paths:
            paths.append(p)
    for name, data in wanted:
        for p in paths:
            try:
                entries = x11_mini._read_xauth(p)
            except OSError:
                continue
            for _family, _addr, _num, ename, edata in entries:
                if (ename, edata) == (name, data):
                    return p, data
    return None, None
