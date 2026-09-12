"""Minimal pure-stdlib X11 wire client.

Three callers share it: wwmctl.core reads the X plane of XWayland windows (WM_CLASS, WM_CLIENT_MACHINE,
geometry) and sends EWMH ClientMessages, wxprop.core does all of its X-window work through it, and
wdotool.backend_kwin reads the XWayland ids KWin 6 does not export.

Talks straight to the XWayland server over its unix socket — enough of the core protocol for wmctrl-style
identity/property work and nothing more: InternAtom, GetProperty (long properties via the offset loop),
ChangeProperty, SendEvent (ClientMessage to root), GetGeometry + TranslateCoordinates, QueryTree, GetInputFocus
(as the post-void-request sync), plus OpenFont / QueryFont / CloseFont for `wxprop -font`, the one place a
resource id is allocated (from the setup reply's base and mask). No extensions, no big-requests; byte order 'l'
only.

Error model: XUnavailable for anything connection-level (no server, bad DISPLAY, auth rejected, connection
lost), X11Error for errors the server reports (BadWindow and friends). Every caller treats both as "degrade
gracefully".

Conventions: property values of format 32 are returned as unsigned 32-bit ints (EWMH's -1 reads as 0xFFFFFFFF);
get_prop_string() truncates at the first NUL exactly like wmctrl's printf("%s") does.
"""

import os
import re
import select
import socket
import stat
import struct
import time

# Test seam: unit tests point this at a directory with a fake server socket.
_SOCK_DIR = "/tmp/.X11-unix"

_TIMEOUT = 5.0

# Hostile-server ceilings: no sane .Xauthority is over 1MB, no core-protocol
# reply body or property value is over 16MB (0x400000 4-byte words).
_MAX_XAUTH_BYTES = 1 << 20
_MAX_REPLY_WORDS = 0x400000
_MAX_PROP_BYTES = 1 << 24
_MAX_PROP_CHUNKS = 4096

# X protocol constants used below
_OP_CHANGE_WINDOW_ATTRIBUTES = 2
_OP_INTERN_ATOM = 16
_OP_GET_ATOM_NAME = 17
_OP_CHANGE_PROPERTY = 18
_OP_DELETE_PROPERTY = 19
_OP_GET_PROPERTY = 20
_OP_LIST_PROPERTIES = 21
_OP_SEND_EVENT = 25
_OP_GET_GEOMETRY = 14
_OP_QUERY_TREE = 15
_OP_TRANSLATE_COORDS = 40
_OP_GET_INPUT_FOCUS = 43
_OP_LOOKUP_COLOR = 92
_OP_OPEN_FONT = 45
_OP_CLOSE_FONT = 46
_OP_QUERY_FONT = 47
_CLIENT_MESSAGE = 33
_EVENT_MASK_SUBSTRUCTURE = 0x180000  # SubstructureNotify|SubstructureRedirect
_CW_EVENT_MASK = 0x800
_EV_DESTROY_NOTIFY = 17
_EV_PROPERTY_NOTIFY = 28

# Bounded event queue (wxprop -spy): a PropertyNotify storm racing a reply
# wait must never grow memory without limit; oldest events are shed first.
_MAX_EVENT_QUEUE = 4096

#: Xlib's text for each core error code (what `wxprop` reprints on e.g.
#: BadWindow); the wire client itself needs only the leading name.
X_ERROR_TEXT = {
    1: "BadRequest (invalid request code or no such operation)",
    2: "BadValue (integer parameter out of range for operation)",
    3: "BadWindow (invalid Window parameter)",
    4: "BadPixmap (invalid Pixmap parameter)",
    5: "BadAtom (invalid Atom parameter)",
    6: "BadCursor (invalid Cursor parameter)",
    7: "BadFont (invalid Font parameter)",
    8: "BadMatch (invalid parameter attributes)",
    9: "BadDrawable (invalid Pixmap or Window parameter)",
    10: "BadAccess (attempt to access private resource denied)",
    11: "BadAlloc (insufficient resources for operation)",
    12: "BadColor (invalid Colormap parameter)",
    13: "BadGC (invalid GC parameter)",
    14: "BadIDChoice (invalid resource ID chosen for this connection)",
    15: "BadName (named color or font does not exist)",
    16: "BadLength (poly request too large or internal Xlib length error)",
    17: "BadImplementation (server does not implement operation)",
}

_ERROR_NAMES = {code: text.split(" ", 1)[0]
                for code, text in X_ERROR_TEXT.items()}

_FAMILY_LOCAL = 256
_FAMILY_WILD = 0xFFFF


class XUnavailable(Exception):
    """No usable X server (no socket, handshake refused, connection lost)."""


class X11Error(Exception):
    """An error packet from the X server, mapped from the wire."""

    def __init__(self, code: int, major: int, minor: int, bad_value: int):
        self.code = code
        self.major = major
        self.minor = minor
        self.bad_value = bad_value
        self.name = _ERROR_NAMES.get(code, "XError%d" % code)
        super().__init__("%s (major %d, bad value 0x%x)"
                         % (self.name, major, bad_value))


def hostname() -> str:
    """This machine's name, or "" when the kernel will not say. The window tools print it as WM_CLIENT_MACHINE;
    the auth lookup below wants the same string as the "local" family address."""
    try:
        return socket.gethostname() or ""
    except OSError:
        return ""


def _pad4(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def _parse_display(d: str) -> tuple[int, int]:
    """'[host]:num[.screen]' -> (num, screen). Local displays only."""
    m = re.fullmatch(r"(.*?):(\d+)(?:\.(\d+))?", d.strip())
    if not m:
        raise XUnavailable('cannot parse DISPLAY "%s"' % d)
    host = m.group(1)
    if host not in ("", "unix", "localhost"):
        raise XUnavailable('non-local DISPLAY "%s" is not supported' % d)
    return int(m.group(2)), int(m.group(3) or 0)


def _read_xauth(path: str):
    """Parse the binary .Xauthority format: a list of (family, address, number, name, data) tuples, all lengths
    big-endian. Only regular files are read (an XAUTHORITY pointing at a FIFO must not block us, /dev/zero must
    not OOM us) and the read is bounded at 1MB; anything else raises OSError, which the caller treats as "no
    file"."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("XAUTHORITY is not a regular file: %s" % path)
        chunks = []
        got = 0
        while got < _MAX_XAUTH_BYTES:
            chunk = os.read(fd, _MAX_XAUTH_BYTES - got)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        buf = b"".join(chunks)
    finally:
        os.close(fd)
    entries = []
    off = 0
    try:
        while off < len(buf):
            (family,) = struct.unpack_from(">H", buf, off)
            off += 2
            fields = []
            for _ in range(4):
                (n,) = struct.unpack_from(">H", buf, off)
                off += 2
                fields.append(buf[off:off + n])
                off += n
            entries.append((family, *fields))
    except struct.error:
        pass  # truncated file: keep what parsed cleanly
    return entries


def _session_xauthority() -> str | None:
    """The graphical session's cookie file when the environment names none: Mutter's
    $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.* (KWin's xauth_*, SDDM's /tmp/xauth_<random> off the session
    leader), found by w11common.session even from `ssh root@` with an empty environment. Mutter starts Xwayland
    with -auth, so the cookie is mandatory there -- the cookie-less same-uid pass only works on wlroots.

    The uid is asked for explicitly and never 0: from a root ssh login `session.session_uid()` answers 0 --
    /run/user/0 is a real runtime dir and the first candidate -- and the search then goes to root's own
    environment and /root/.Xauthority, neither of which has anything to do with the graphical session
    [M recon2/openbox.md §2d, on a live SDDM+LXQt VM]. `passthrough.target_uid()` skips uid 0 for the same
    reason. When there is no other candidate this is exactly what it was: session_uid()'s answer."""
    try:
        from w11common import session
        uid = session.session_uid()
        if uid == 0:
            uid = next((u for u, _d in session.runtime_dir_candidates() if u), None)
        return session.find_xauthority(uid)
    except Exception:
        return None


def _auth_candidates(display_num: int, xauthority: str | None = None):
    """(name, data) pairs to try during setup, best match first, always ending with the cookie-less attempt
    (XWayland usually allows same-uid). `xauthority` (the compositor's own cookie file, e.g. from the GNOME
    bridge's XInfo) beats $XAUTHORITY; when the named file yields no usable cookie the session scan
    (_session_xauthority) is tried as well."""
    paths = []
    for p in (xauthority, os.environ.get("XAUTHORITY")
              or os.path.expanduser("~/.Xauthority")):
        if p and p not in paths:
            paths.append(p)
    dnum = str(display_num).encode()
    host = hostname().encode()

    def collect(path):
        try:
            entries = _read_xauth(path)
        except OSError:
            return []
        exact, wild, other = [], [], []
        for family, addr, num, name, data in entries:
            if name != b"MIT-MAGIC-COOKIE-1" or num not in (b"", dnum):
                continue
            if family == _FAMILY_LOCAL and addr == host:
                exact.append((name, data))
            elif family == _FAMILY_WILD:
                wild.append((name, data))
            else:
                other.append((name, data))
        return exact + wild + other

    found = []
    for p in paths:
        found += collect(p)
        if found:
            break
    if not found:
        p = _session_xauthority()
        if p and p not in paths:
            found = collect(p)
    out = []
    for cand in found:
        if cand not in out:
            out.append(cand)
    out.append((b"", b""))
    return out


class X11Conn:
    """Synchronous X11 connection. One request in flight at a time."""

    def __init__(self, display: str | None = None,
                 xauthority: str | None = None):
        """`xauthority` (additive): a cookie file to try before $XAUTHORITY -- the compositor's own, as the
        GNOME bridge reports it, so a root or empty-environment caller reaches Mutter's -auth'ed Xwayland."""
        self._sock = None
        self._seq = 0
        self._max_req_words = 0xFFFF  # refined from the setup reply
        self._atoms: dict[str, int] = {}
        self._xauthority = xauthority
        if display is None:
            display = os.environ.get("DISPLAY")
        if display:
            candidates = [_parse_display(display)]
        else:
            try:
                nums = sorted(
                    int(m.group(1)) for n in os.listdir(_SOCK_DIR)
                    if (m := re.fullmatch(r"X(\d+)", n)))
            except OSError:
                nums = []
            if not nums:
                raise XUnavailable(
                    "no X display (DISPLAY unset, no sockets in %s)"
                    % _SOCK_DIR)
            candidates = [(n, 0) for n in nums]
        err = None
        for num, screen in candidates:
            try:
                self._connect(num, screen)
                return
            except XUnavailable as e:
                err = e
        raise err

    # -- connection setup ----------------------------------------------------

    def _open_socket(self, num: int):
        path = "%s/X%d" % (_SOCK_DIR, num)
        for target in (path, "\0" + path):  # filesystem, then abstract
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(_TIMEOUT)
            try:
                s.connect(target)
                return s
            except OSError:
                s.close()
        raise XUnavailable("cannot connect to X socket %s" % path)

    def _connect(self, num: int, screen: int):
        # A refused setup closes the connection, so each auth candidate gets
        # a fresh socket. Transport hiccups just advance to the next one.
        reason = "no authorization accepted"
        for name, data in _auth_candidates(num, self._xauthority):
            sock = self._open_socket(num)
            try:
                ok, why = self._handshake(sock, name, data)
            except (OSError, XUnavailable) as e:
                sock.close()
                reason = str(e)
                continue
            if ok:
                self._sock = sock
                self._root = self._roots[min(screen, len(self._roots) - 1)]
                return
            sock.close()
            reason = why
        raise XUnavailable("X server :%d refused the connection: %s"
                           % (num, reason.strip()))

    def _handshake(self, sock, auth_name: bytes, auth_data: bytes):
        """One setup attempt. Returns (True, "") and fills in the roots on
        success; (False, reason) when the server refuses this auth."""
        sock.sendall(struct.pack("<BxHHHHxx", 0x6C, 11, 0,
                                 len(auth_name), len(auth_data))
                     + _pad4(auth_name) + _pad4(auth_data))
        head = self._recv_exact(8, sock)
        status = head[0]
        (extra,) = struct.unpack_from("<H", head, 6)
        body = self._recv_exact(extra * 4, sock)
        if status == 0:  # Failed: byte 1 of the head is the reason length
            return False, body[:head[1]].decode("latin-1", "replace")
        if status != 1:  # Authenticate: reason is the whole (padded) body
            return False, body.rstrip(b"\0").decode("latin-1", "replace")
        # A "success" body from a hostile/broken server can still lie about its own shape (screen counts
        # pointing past the end): any parse failure is treated as a refusal, never a leaked struct.error.
        try:
            max_words, = struct.unpack_from("<H", body, 18)
            rid_base, rid_mask = struct.unpack_from("<II", body, 4)
            (vlen,) = struct.unpack_from("<H", body, 16)
            nscreens, nformats = body[20], body[21]
            p = 32 + len(_pad4(b"\0" * vlen)) + 8 * nformats
            roots = []
            cmaps = []
            for _ in range(nscreens):
                root, cmap = struct.unpack_from("<II", body, p)
                ndepths = body[p + 39]
                p += 40
                for _ in range(ndepths):
                    (nvis,) = struct.unpack_from("<H", body, p + 2)
                    p += 8 + 24 * nvis
                roots.append(root)
                cmaps.append(cmap)
        except (struct.error, IndexError):
            return False, "malformed setup reply"
        if not roots:
            return False, "setup reply carries no screens"
        self._roots = roots
        self._cmaps = cmaps      # the screens' default colormaps (-M)
        # resource ids we may allocate (fonts: xprop -font). The mask is a
        # contiguous run of low bits the client owns.
        self._rid_base, self._rid_mask = rid_base, rid_mask
        # maximum-request-length (in 4-byte units); the protocol floor is
        # 4096 words, so never let a nonsense advertisement go below it
        self._max_req_words = max(4096, max_words)
        return True, ""

    # -- wire plumbing -------------------------------------------------------

    def _poison(self, sock) -> None:
        """A failed receive can leave the stream mid-packet: if a stalled server later resumes, the next read
        would misframe its bytes and match replies to the wrong requests. Any receive failure on the established
        connection therefore kills the connection for good (handshake sockets are closed by _connect
        instead)."""
        if sock is not None and sock is self._sock:
            self.close()

    def _recv_exact(self, n: int, sock=None) -> bytes:
        if sock is None:
            sock = self._sock
        if sock is None:
            raise XUnavailable("X connection is closed")
        chunks = []
        while n:
            try:
                b = sock.recv(n)
            except socket.timeout:
                self._poison(sock)
                raise XUnavailable("X server timed out") from None
            except OSError as e:
                self._poison(sock)
                raise XUnavailable("X connection lost: %s" % e) from None
            if not b:
                self._poison(sock)
                raise XUnavailable("X connection closed by server")
            chunks.append(b)
            n -= len(b)
        return b"".join(chunks)

    def _send(self, opcode: int, data_byte: int, payload: bytes = b"") -> int:
        """Send one request (payload already padded); returns its sequence."""
        if self._sock is None:
            raise XUnavailable("X connection is closed")
        words = 1 + len(payload) // 4
        # core protocol carries the request length in a u16 of 4-byte units, capped further by the server's
        # advertised maximum-request-length; oversized payloads (huge -N titles) must fail cleanly, not as a
        # struct.error from the 'H' pack below
        if words > min(self._max_req_words, 0xFFFF):
            raise XUnavailable("request exceeds the X11 maximum request "
                               "length (big-requests unsupported)")
        try:
            self._sock.sendall(struct.pack("<BBH", opcode, data_byte,
                                           words) + payload)
        except OSError as e:
            # a partial send desyncs the outbound stream just like a partial
            # receive desyncs the inbound one: poison the connection
            self.close()
            raise XUnavailable("X connection lost: %s" % e) from None
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def _wait_reply(self, seq: int):
        """Read packets until the reply for `seq`. Any error packet raises X11Error (with the void-request sync
        below, an error always belongs to the request just issued). Events are skipped."""
        while True:
            pkt = self._recv_exact(32)
            kind = pkt[0] & 0x7F
            if kind == 0:
                bad, minor, major = struct.unpack_from("<IHB", pkt, 4)
                err = X11Error(pkt[1], major, minor, bad)
                err.sequence = struct.unpack_from("<H", pkt, 2)[0]
                raise err
            if kind == 1:
                (pseq,) = struct.unpack_from("<H", pkt, 2)
                extra = self._extra_words(pkt)
                body = self._recv_exact(extra * 4) if extra else b""
                if pseq == seq:
                    return pkt, body
                continue  # stale reply: drop
            if kind == 35:  # GenericEvent carries extra length
                extra = self._extra_words(pkt)
                if extra:
                    self._recv_exact(extra * 4)
                continue
            # other events: queue for next_event() when select_input() armed
            # the (bounded) queue; otherwise the old behavior — skip.
            q = getattr(self, "_events", None)
            if q is not None:
                if len(q) >= _MAX_EVENT_QUEUE:
                    q.pop(0)
                q.append(pkt)

    def _extra_words(self, pkt: bytes) -> int:
        """The u32 extra-length word of a reply/GenericEvent, sanity-capped: a length claiming a body over 16MB
        is a lying server, not a core protocol reply — never try to receive (or time out on) it."""
        (extra,) = struct.unpack_from("<I", pkt, 4)
        if extra > _MAX_REPLY_WORDS:
            self.close()
            raise XUnavailable("implausible reply length")
        return extra

    def _void(self, opcode: int, data_byte: int, payload: bytes):
        """A request with no reply, followed by a GetInputFocus roundtrip so
        the server has processed it and any error surfaces here."""
        self._send(opcode, data_byte, payload)
        self._wait_reply(self._send(_OP_GET_INPUT_FOCUS, 0))

    # -- public API (frozen per docs/WWMCTL.md) -----------------------------------

    def root(self) -> int:
        return self._root

    def atom(self, name: str, only_if_exists: bool = False) -> int:
        a = self._atoms.get(name)
        if a is not None:
            return a
        nb = name.encode("latin-1")
        seq = self._send(_OP_INTERN_ATOM, 1 if only_if_exists else 0,
                         struct.pack("<H2x", len(nb)) + _pad4(nb))
        pkt, _ = self._wait_reply(seq)
        (a,) = struct.unpack_from("<I", pkt, 8)
        if a:
            self._atoms[name] = a
        return a

    def client_list(self) -> list[int]:
        """_NET_CLIENT_LIST on the root; QueryTree fallback when absent."""
        r = self._read_property(self._root, "_NET_CLIENT_LIST")
        if r is not None and r[1] == 32:
            return list(struct.unpack("<%dI" % (len(r[2]) // 4), r[2]))
        seq = self._send(_OP_QUERY_TREE, 0, struct.pack("<I", self._root))
        pkt, body = self._wait_reply(seq)
        (n,) = struct.unpack_from("<H", pkt, 16)
        n = min(n, len(body) // 4)  # never trust the count past the body
        return list(struct.unpack_from("<%dI" % n, body, 0))

    def get_prop_ints(self, win: int, name: str) -> list[int]:
        r = self._read_property(win, name)
        if r is None:
            return []
        _t, fmt, data = r
        if fmt == 32:
            return list(struct.unpack("<%dI" % (len(data) // 4), data))
        if fmt == 16:
            return list(struct.unpack("<%dH" % (len(data) // 2), data))
        return list(data)

    def get_prop_string(self, win: int, name: str) -> str:
        r = self._read_property(win, name)
        if r is None or r[1] != 8:
            return ""
        type_a, _fmt, data = r
        if type_a == self.atom("UTF8_STRING"):
            s = data.decode("utf-8", "replace")
        else:  # STRING and friends: latin-1 covers every byte
            s = data.decode("latin-1")
        return s.split("\0", 1)[0]

    def get_wm_class(self, win: int) -> tuple[str, str]:
        r = self._read_property(win, "WM_CLASS")
        if r is None or r[1] != 8:
            return "", ""
        data = r[2]
        nul = data.find(b"\0")
        if nul < 0:  # degenerate single-string WM_CLASS (b"solo")
            return data.decode("latin-1"), ""
        instance = data[:nul].decode("latin-1")
        rest = data[nul + 1:]
        if not rest:  # b"solo\0": no second string either
            return instance, ""
        # the class part is only what a second NUL-terminated string actually carries (callers print "" without
        # a trailing dot, the way wmctrl prints just "inst" for a single-string WM_CLASS)
        end = rest.find(b"\0")
        return instance, (rest[:end] if end >= 0 else rest).decode("latin-1")

    def get_client_machine(self, win: int) -> str:
        return self.get_prop_string(win, "WM_CLIENT_MACHINE")

    def get_pid(self, win: int) -> int:
        ints = self.get_prop_ints(win, "_NET_WM_PID")
        return ints[0] if ints else 0

    def get_geometry_raw(self, win: int) -> tuple[int, int, int, int, int, int]:
        """(x, y, w, h, rel_x, rel_y): the first four as `get_geometry` (root-relative origin + size), the last
        two the window's origin RELATIVE TO ITS X PARENT, straight off the GetGeometry reply's own x,y.

        That relative pair is the second term wmctrl 1.07's `-G` adds to the absolute origin (`list_windows`
        feeds XTranslateCoordinates the window's own x,y instead of 0,0). Under a REPARENTING/framing xwm the
        parent is the server-side frame and the pair is the frame offset the clone cannot otherwise compute;
        under a NON-reparenting one (`wlroots`, sway, hypr, this tree's own shadow plane) the parent is the
        root and the pair equals the absolute origin, which is why that column doubles there. Framing is
        MEASURED on Mutter (noble-gnome mutter 46.2: absolute 398,252, relative 14,49, the two originals both
        printing 412,301 = absolute + relative [M goal2/recon/b17-review-measurements.md 1]) and on
        cosmic-comp 1:1.8.0-1 (reparents, but its Xwayland reports a relative origin of 0,0, so the term adds
        nothing). KWin, Muffin and Wayfire are NOT YET measured for reparenting (requests-batch-18.md item C);
        the formula does not need the list -- `absolute + relative` is right whichever way the xwm goes."""
        seq = self._send(_OP_GET_GEOMETRY, 0, struct.pack("<I", win))
        pkt, _ = self._wait_reply(seq)
        rel_x, rel_y, w, h = struct.unpack_from("<hhHH", pkt, 12)
        seq = self._send(_OP_TRANSLATE_COORDS, 0,
                         struct.pack("<IIhh", win, self._root, 0, 0))
        pkt, _ = self._wait_reply(seq)
        x, y = struct.unpack_from("<hh", pkt, 12)
        return x, y, w, h, rel_x, rel_y

    def get_geometry(self, win: int) -> tuple[int, int, int, int]:
        """Root-relative (x, y, w, h): size from GetGeometry, position by translating the window origin to root
        coordinates (matches xwininfo and the compositor's idea of the rect — NOT wmctrl's doubled values under
        non-reparenting WMs). The parent-relative origin `get_geometry_raw` also returns is dropped here."""
        x, y, w, h, _rel_x, _rel_y = self.get_geometry_raw(win)
        return x, y, w, h

    def _new_rid(self) -> int:
        """One resource id out of the range the setup reply gave us."""
        n = getattr(self, "_rid_next", 0) + 1
        self._rid_next = n
        mask = getattr(self, "_rid_mask", 0) or 0x1FFFFF
        if not (n & mask):
            raise XUnavailable("no resource ids left")
        return (getattr(self, "_rid_base", 0) or 0) | (n & mask)

    def lookup_color(self, name: str) -> "tuple[int, int, int]":
        """XParseColor's server side: the exact 16-bit RGB of a colour name (or a #rrggbb literal) in the
        screen's default colormap. X11Error (BadName) for a name the server's rgb.txt does not have."""
        cmap = (getattr(self, "_cmaps", None) or [0])[0]
        raw = name.encode("latin-1", "replace")
        seq = self._send(_OP_LOOKUP_COLOR, 0,
                         struct.pack("<IHxx", cmap, len(raw)) + _pad4(raw))
        pkt, _body = self._wait_reply(seq)
        return struct.unpack_from("<HHH", pkt, 8)

    def font_properties(self, name: str) -> "list[tuple[int, int]]":
        """[(atom id, CARD32 value)] — a core X font's FONTPROPs, the list `xprop -font` prints. OpenFont's
        BadName (no such font) surfaces as X11Error from the synchronising round trip."""
        fid = self._new_rid()
        raw = name.encode("latin-1", "replace")
        self._void(_OP_OPEN_FONT, 0,
                   struct.pack("<IHxx", fid, len(raw)) + _pad4(raw))
        try:
            seq = self._send(_OP_QUERY_FONT, 0, struct.pack("<I", fid))
            pkt, body = self._wait_reply(seq)
            # QueryFont's fixed part is 60 bytes: the FONTPROPs follow it,
            # and their count sits at byte 46 (body offset 14).
            (n,) = struct.unpack_from("<H", body, 14)
            n = min(n, max(0, (len(body) - 28) // 8))
            return [struct.unpack_from("<II", body, 28 + i * 8)
                    for i in range(n)]
        finally:
            try:
                self._void(_OP_CLOSE_FONT, 0, struct.pack("<I", fid))
            except Exception:
                pass

    def set_name(self, win: int, name: str, icon: bool, long_: bool,
                 utf8: bool = False) -> None:
        """wmctrl -N/-I/-T semantics: long_ sets WM_NAME/_NET_WM_NAME, icon sets WM_ICON_NAME/_NET_WM_ICON_NAME.

        `utf8` is wmctrl's envir_utf8 (a UTF-8 locale, or -u). Set, it follows main.c's window_set_title
        exactly: there is no locale copy of the title to write, so the legacy STRING property is DELETED rather
        than left holding a stale or lossy value, and only the EWMH UTF8_STRING one is written. Clear, both are
        written, the legacy one as STRING (latin-1)."""
        raw = name.encode("utf-8")
        latin = name.encode("latin-1", "replace")
        pairs = []
        if long_:
            pairs.append(("WM_NAME", "_NET_WM_NAME"))
        if icon:
            pairs.append(("WM_ICON_NAME", "_NET_WM_ICON_NAME"))
        for legacy, net in pairs:
            if utf8:
                # XDeleteProperty on the predefined atom: unconditional,
                # like wmctrl's, not "only if the atom is already interned"
                self._void(_OP_DELETE_PROPERTY, 0,
                           struct.pack("<II", win, self.atom(legacy)))
            else:
                self._change_property(win, self.atom(legacy),
                                      self.atom("STRING"), latin)
            self._change_property(win, self.atom(net),
                                  self.atom("UTF8_STRING"), raw)

    def send_root_message(self, win: int, type_name: str,
                          data: list[int]) -> None:
        """ClientMessage (format 32) about `win`, sent to the root window with
        SubstructureNotify|SubstructureRedirect — how every EWMH client request travels."""
        vals = (list(data) + [0] * 5)[:5]
        event = struct.pack("<BBHII", _CLIENT_MESSAGE, 32, 0,
                            win & 0xFFFFFFFF, self.atom(type_name))
        event += struct.pack("<5I", *((v & 0xFFFFFFFF) for v in vals))
        self._void(_OP_SEND_EVENT, 0,
                   struct.pack("<II", self._root,
                               _EVENT_MASK_SUBSTRUCTURE) + event)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # -- property machinery --------------------------------------------------

    def _get_property_chunk(self, win: int, prop: int, offset: int,
                            length: int):
        payload = struct.pack("<IIIII", win, prop, 0, offset, length)
        seq = self._send(_OP_GET_PROPERTY, 0, payload)
        pkt, body = self._wait_reply(seq)
        fmt = pkt[1]
        type_a, bytes_after, nitems = struct.unpack_from("<III", pkt, 8)
        return type_a, fmt, body[:nitems * (fmt // 8)], bytes_after

    def _read_property(self, win: int, name: str):
        """Full value of a property: (type_atom, format, bytes), or None if the property does not exist. Long
        values are fetched via the long-offset loop (offsets counted in 32-bit units)."""
        prop = self.atom(name, only_if_exists=True)
        if not prop:
            return None
        data = b""
        offset = 0
        # A server that keeps claiming bytes_after > 0 must not spin us
        # forever (or OOM us): cap both the total size and the roundtrips.
        for _ in range(_MAX_PROP_CHUNKS):
            type_a, fmt, chunk, after = self._get_property_chunk(
                win, prop, offset, 0x40000)
            if type_a == 0:
                return None
            data += chunk
            if after == 0 or not chunk:  # not chunk: server misbehaving
                return type_a, fmt, data
            if len(data) > _MAX_PROP_BYTES:
                break
            offset += len(chunk) // 4
        self.close()  # the server is lying about the property: distrust it
        raise XUnavailable("property too large or server misbehaving")

    def _change_property(self, win: int, prop: int, type_a: int,
                         data: bytes, fmt: int = 8):
        nitems = len(data) // (fmt // 8) if fmt in (8, 16, 32) else 0
        payload = struct.pack("<IIIB3xI", win, prop, type_a, fmt,
                              nitems) + _pad4(data)
        self._void(_OP_CHANGE_PROPERTY, 0, payload)  # PropModeReplace

    def query_tree(self, win: int) -> list[int]:
        """Children of `win`, bottom-to-top stacking order (wire order)."""
        seq = self._send(_OP_QUERY_TREE, 0, struct.pack("<I", win))
        pkt, body = self._wait_reply(seq)
        (n,) = struct.unpack_from("<H", pkt, 16)
        n = min(n, len(body) // 4)  # never trust the count past the body
        return list(struct.unpack_from("<%dI" % n, body, 0))

    def list_properties(self, win: int) -> list[int]:
        """Atom ids of every property on `win`, in server order (the order
        real xprop dumps them). BadWindow raises X11Error."""
        seq = self._send(_OP_LIST_PROPERTIES, 0, struct.pack("<I", win))
        pkt, body = self._wait_reply(seq)
        (n,) = struct.unpack_from("<H", pkt, 8)
        n = min(n, len(body) // 4)
        return list(struct.unpack_from("<%dI" % n, body, 0))

    def get_atom_name(self, atom: int) -> str | None:
        """Name of an atom id (GetAtomName), None for BadAtom. Cached both
        ways; names decode as latin-1 so raw bytes round-trip exactly."""
        names = getattr(self, "_atom_names", None)
        if names is None:
            names = self._atom_names = {v: k for k, v in self._atoms.items()}
        if atom in names:
            return names[atom]
        try:
            seq = self._send(_OP_GET_ATOM_NAME, 0,
                             struct.pack("<I", atom & 0xFFFFFFFF))
            pkt, body = self._wait_reply(seq)
        except X11Error:
            return None
        (ln,) = struct.unpack_from("<H", pkt, 8)
        name = body[:min(ln, len(body))].decode("latin-1")
        names[atom] = name
        self._atoms.setdefault(name, atom)
        return name

    def read_property(self, win: int, name: str):
        """Full value of a property as (type_name, format, bytes), or None when the atom or the property does
        not exist on `win`. BadWindow raises X11Error; a lying server raises XUnavailable (see _read_property's
        hardening)."""
        r = self._read_property(win, name)
        if r is None:
            return None
        type_a, fmt, data = r
        tname = self.get_atom_name(type_a)
        if tname is None:  # a server would have to lie about its own atom
            tname = "undefined atom # 0x%x" % type_a
        return tname, fmt, data

    def delete_property(self, win: int, name: str) -> bool:
        """DeleteProperty; False (and no request) when the atom does not
        exist — the caller decides how to report that, like xprop does."""
        prop = self.atom(name, only_if_exists=True)
        if not prop:
            return False
        self._void(_OP_DELETE_PROPERTY, 0, struct.pack("<II", win, prop))
        return True

    def change_property(self, win: int, name: str, type_name: str,
                        fmt: int, data: bytes) -> None:
        """Generalized ChangeProperty (PropModeReplace): any property name, type atom, and format 8/16/32.
        `data` is the little-endian wire image (nitems derived from its length). A format outside 8/16/32 is
        sent with nitems=0 so the server's BadValue answer surfaces exactly like Xlib's would."""
        self._change_property(win, self.atom(name), self.atom(type_name),
                              data, fmt)

    def select_input(self, win: int, event_mask: int) -> None:
        """ChangeWindowAttributes(CWEventMask): start receiving events for
        `win`; arms the bounded event queue read by next_event()."""
        if getattr(self, "_events", None) is None:
            self._events = []
        self._void(_OP_CHANGE_WINDOW_ATTRIBUTES, 0,
                   struct.pack("<III", win, _CW_EVENT_MASK,
                               event_mask & 0xFFFFFFFF))

    def next_event(self, timeout: float | None = None):
        """Next X event as a parsed dict ({"type": "PropertyNotify", "window", "atom", "state"} / {"type":
        "DestroyNotify", "window"} / {"type": <code>}), or None when `timeout` seconds pass without one.
        timeout=None blocks indefinitely (xprop -spy semantics), but a mid-packet stall still hits the
        connection timeout and poisons the connection — a trickling server cannot wedge us forever. timeout=0 is
        a true non-blocking poll: kernel-buffered events are drained before None is returned."""
        if getattr(self, "_events", None) is None:
            self._events = []
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._events:
                return self._parse_event(self._events.pop(0))
            if self._sock is None:
                raise XUnavailable("X connection is closed")
            # clamp (never a negative select timeout); a zero timeout still polls the socket once before giving
            # up, so a buffered event is never missed on the deadline boundary
            wait = None if deadline is None \
                else max(0.0, deadline - time.monotonic())
            try:
                ready, _, _ = select.select([self._sock], [], [], wait)
            except (OSError, ValueError):
                self.close()
                raise XUnavailable("X connection lost") from None
            if not ready:
                return None
            pkt = self._recv_exact(32)
            kind = pkt[0] & 0x7F
            if kind == 0:
                bad, minor, major = struct.unpack_from("<IHB", pkt, 4)
                err = X11Error(pkt[1], major, minor, bad)
                err.sequence = struct.unpack_from("<H", pkt, 2)[0]
                raise err
            if kind == 1:  # stale reply: drain its body, drop it
                extra = self._extra_words(pkt)
                if extra:
                    self._recv_exact(extra * 4)
                continue
            if kind == 35:  # GenericEvent: drain, skip (no extensions here)
                extra = self._extra_words(pkt)
                if extra:
                    self._recv_exact(extra * 4)
                continue
            return self._parse_event(pkt)

    @staticmethod
    def _parse_event(pkt: bytes) -> dict:
        code = pkt[0] & 0x7F
        if code == _EV_PROPERTY_NOTIFY:
            window, atom, tstamp = struct.unpack_from("<III", pkt, 4)
            return {"type": "PropertyNotify", "window": window, "atom": atom,
                    "time": tstamp, "state": pkt[16]}
        if code == _EV_DESTROY_NOTIFY:
            _event, window = struct.unpack_from("<II", pkt, 4)
            return {"type": "DestroyNotify", "window": window}
        return {"type": code}
