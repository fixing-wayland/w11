"""X11 passthrough: on a plain X11 session, hand over to the real tool.

SHARED, frozen after landing (like `session.py` / `dbus_mini.py`): wire-level
fixes allowed, API changes need a note.

These tools are drop-in replacements *installed over* the originals
(`/usr/local/bin/xdotool` is us, `/usr/bin/xdotool` is the distribution's).
On Wayland that is the whole point. On a plain X11 session (Xfce, i3, MATE,
Cinnamon, LXQt/Openbox, GNOME/KDE on Xorg) it is a regression: the X server is
authoritative there and the originals have XTEST, the real property store and
real RandR, which we cannot beat from outside. Every desktop in that list was
measured on a live one; docs/Technical.md section 2 carries the same
enumeration and the numbers behind it. So on X11 we get out of the way and
`execve()` the original with the argv we were given.

Three parts:

* `session_kind()` — "wayland", "x11" or None. Wayland is tested *first*:
  `$DISPLAY` is set on a Wayland session too (Xwayland), so it is never
  evidence of an X11 session, while a live Wayland socket of *our* session is
  conclusive. The rest of the order exists for sudo / `ssh root@` / cron, where
  the environment says nothing or lies.
* `real_tool()` — the next binary of that name on PATH that is not us, with
  four independent "not us" guards plus one that is guaranteed to terminate.
* `maybe_exec_real()` — the handover itself: default SIGPIPE/SIGXFSZ, flush,
  `os.execve` (never `subprocess`: exit status, signal death, the controlling
  terminal and the process group all survive for free), plus the environment
  repair that makes `sudo xdotool key a` work *through* us where the original
  alone fails.

Environment:

* `W11_PASSTHROUGH=never|always|auto` (per-tool: `WDOTOOL_PASSTHROUGH`,
  `WWMCTL_PASSTHROUGH`, `WXPROP_PASSTHROUGH`, `WXRANDR_PASSTHROUGH`).
  `never` = run our own code whatever the session (the test suite and
  developers on an X11 laptop); `always` = hand over whatever the session.
  Both are about the *handover*, so a caller that never hands over ignores
  them (`warandr`: `session_kind(respect_override=False)`).
* `W11_PROXY=auto|never|always` (per-tool: `WDOTOOL_PROXY`, `WWMCTL_PROXY`,
  `WXPROP_PROXY`, `WXRANDR_PROXY`) — the *other* direction, read by
  `xw11.wrap.maybe_exec_through_proxy`: on a Wayland session, whether the
  original runs against the proxy's display instead of our own code.
  `proxy_mode()` is the reader; nothing in this file acts on it, because the
  handover it describes is the proxy's and not this module's.
* `WDOTOOL_REAL_XDOTOOL`, `WWMCTL_REAL_WMCTRL`, `WXPROP_REAL_XPROP`,
  `WXRANDR_REAL_XRANDR` — the original's path, skipping the PATH walk.
* `_W11_PASSTHROUGH` (private) — the recursion stop. It carries the
  realpaths we have already handed over to; a process that finds *itself* in
  that list was exec'd as "the real tool" and refuses to go round again.
"""

import os
import signal
import sys

from w11common import distro

# Test seams (production values). `session_kind()` looks at nothing else, so a
# test can describe a whole session with a temporary directory.
_X11_SOCK_DIR = "/tmp/.X11-unix"
_LOGIND_DIR = "/run/systemd/sessions"
_RUN_USER_DIR = "/run/user"

#: names our own executables can carry (`realpath` of an installed symlink).
#: `xw11` is one of them although it clones nothing: the proxy wrapper asks
#: `real_tool()` for the original it is about to run *through* the proxy, and a
#: PATH walk that took the proxy's own script for an `xdotool` would exec the
#: proxy under xdotool's argv. Guard 2 (the name the candidate resolves to) and
#: `_OUR_MODULES` (the generated console script imports `xw11.cli`) both read
#: this tuple.
OUR_NAMES = ("wdotool", "wwmctl", "wxprop", "wxrandr", "warandr", "xw11")

#: our name -> the original we replace
REAL_NAME = {
    "wdotool": "xdotool",
    "wwmctl": "wmctrl",
    "wxprop": "xprop",
    "wxrandr": "xrandr",
}

#: the original -> (env override, Debian package)
_OVERRIDE = {
    "xdotool": "WDOTOOL_REAL_XDOTOOL",
    "wmctrl": "WWMCTL_REAL_WMCTRL",
    "xprop": "WXPROP_REAL_XPROP",
    "xrandr": "WXRANDR_REAL_XRANDR",
}
#: the Debian names, which are `w11common/distro.py`'s debian column and are pinned equal to it by
#: tests/test_distro.py. The message below asks that table, so the package named follows the box's own
#: distribution: `dnf install xprop` on Fedora, `pacman -S xorg-xprop` on Arch, an attribute path on NixOS.
_PACKAGE = {
    "xdotool": "xdotool",
    "wmctrl": "wmctrl",
    "xprop": "x11-utils",
    "xrandr": "x11-xserver-utils",
}
_MODE_VAR = {
    "xdotool": "WDOTOOL_PASSTHROUGH",
    "wmctrl": "WWMCTL_PASSTHROUGH",
    "xprop": "WXPROP_PASSTHROUGH",
    "xrandr": "WXRANDR_PASSTHROUGH",
}
#: the same shape for the *proxy* hook (`xw11.wrap.maybe_exec_through_proxy`),
#: which is a second question about the same handover: `W11_PASSTHROUGH` says
#: whether our own code may be replaced at all, `W11_PROXY` says whether a
#: Wayland session replaces it with the original running on `xw11`'s display.
#: Two variables and not one because the two answers are independent -- a box
#: that wants the clone on Wayland (`W11_PROXY=never`) still wants the original
#: on X11 -- and because `W11_PASSTHROUGH=never` is this suite's escape hatch
#: and has to keep meaning "our own code, no handover to anything".
PROXY_MODE_VAR = {
    "xdotool": "WDOTOOL_PROXY",
    "wmctrl": "WWMCTL_PROXY",
    "xprop": "WXPROP_PROXY",
    "xrandr": "WXRANDR_PROXY",
}

#: private recursion stop, set in the exec'd environment
GUARD_VAR = "_W11_PASSTHROUGH"
#: absolute backstop if the realpath identity ever fails to match
GUARD_DEPTH = 8

#: the project word; `scripts/build-pyz.sh` stamps `MARKER + b"-clone: <tool>"`
#: into every zipapp
MARKER = b"w11"
#: ...and *that*, the full stamp, is what the head sniff looks for. A bare
#: `w11` — let alone a bare `wmctrl` — is not enough: a third-party
#: wrapper that merely mentions the project (`# w11 fallback wrapper`
#: above an `exec /usr/bin/wmctrl "$@"`) would be skipped as "us", and the
#: user told to install a package they already have.
STAMP = MARKER + b"-clone:"
#: the other shape we ship: a generated console script (`pip install .`),
#: recognised by the import of one of our packages, not by a substring.
#: `w11common` is on this list and not in OUR_NAMES: no executable is ever
#: called that, but a script of ours may import it and nothing else.
_OUR_MODULES = tuple(n.encode() for n in OUR_NAMES + ("w11common",))
_HEAD_BYTES = 4096

_NEVER = ("never", "no", "off", "0", "false", "disable", "disabled")
_ALWAYS = ("always", "yes", "on", "1", "true", "force", "forced")


class RealToolError(Exception):
    """The configured original cannot be used (bad `*_REAL_*` override, or a
    handover loop). str() is the complete message, without the tool prefix."""


def real_name(tool: str) -> str:
    """"wdotool" -> "xdotool"; an original's own name passes through."""
    return REAL_NAME.get(tool, tool)


# ---------------------------------------------------------------------------
# session detection
# ---------------------------------------------------------------------------

_ENV_KEYS = (
    "W11_PASSTHROUGH", "WDOTOOL_PASSTHROUGH", "WWMCTL_PASSTHROUGH",
    "WXPROP_PASSTHROUGH", "WXRANDR_PASSTHROUGH", "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "DISPLAY", "SUDO_UID", "PKEXEC_UID",
)
_CACHE: dict = {}


def reset_cache() -> None:
    """Drop the memoised session kind (tests; nothing in production changes
    session mid-process)."""
    _CACHE.clear()


def session_kind(tool: str | None = None, env=None,
                 respect_override: bool = True) -> str | None:
    """"wayland" (run our own code), "x11" (hand over) or None (neither found).

    Order — the first three cost one `stat` at most:

    1. `$W11_PASSTHROUGH` / the per-tool variable: `never` -> wayland,
       `always` -> x11. Skipped when `respect_override=False`, which is for
       callers that never hand over: those variables say what to do about the
       *handover*, and answering "wayland" to `warandr` — which only picks
       between the `xrandr` and `wxrandr` command words — would have
       `W11_PASSTHROUGH=never` select `wxrandr` on an X11 box, i.e.
       exactly the breakage this file exists to fix (`passthrough_mode()` is
       the way to ask about the variables themselves).
    2. `$WAYLAND_DISPLAY` *and* its socket exists -> wayland. The in-session
       case; `$DISPLAY` is deliberately not consulted (Xwayland sets it).
    3. `$XDG_SESSION_TYPE` (pam_systemd sets it at every login) -> that, unless
       `SUDO_UID`/`PKEXEC_UID` says the environment came from another login
       (`sudo` keeps root's `XDG_SESSION_TYPE=tty` from an `ssh root@`).
    4. logind's own record of the session (`/run/systemd/sessions/*`,
       key=value, read-only best-effort): the active, local, non-greeter
       session of the target user, and its `TYPE=`.
    5. socket scan: a `wayland-*` socket **owned by the target user** (a GDM
       greeter's `wayland-0` under another uid must not turn an Xfce box into
       a Wayland session), else an X socket -> x11.
    6. Nothing -> None.

    Memoised per (tool, relevant environment, seams); `reset_cache()` clears.
    """
    e = os.environ if env is None else env
    name = real_name(tool) if tool else None
    key = (name, bool(respect_override), tuple(e.get(k) for k in _ENV_KEYS),
           _X11_SOCK_DIR, _LOGIND_DIR, _RUN_USER_DIR)
    try:
        return _CACHE[key]
    except KeyError:
        pass
    kind = _detect(name, e, respect_override)
    _CACHE[key] = kind
    return kind


def passthrough_mode(tool: str | None = None, env=None) -> str:
    """"never", "always" or "auto" — what `$W11_PASSTHROUGH` (or the per-tool variable) asks for. A
    statement about the *handover* only; a caller that does not hand over must not read it as a session type."""
    e = os.environ if env is None else env
    return _mode(real_name(tool) if tool else None, e)


def proxy_mode(tool: str | None = None, env=None) -> str:
    """"never", "always" or "auto" -- what `$W11_PROXY` (or the per-tool `$WDOTOOL_PROXY`) asks for.

    API note, this file being frozen: added for the X11 proxy (design section 8.3).  `always` says the original
    must run through the proxy or not at all -- a missing original or a proxy that will not start is exit 127,
    never a quiet fall back to our own code, because a script that asks for the original's bytes gets them or
    an error.  The spellings are `_NEVER`/`_ALWAYS`'s, the same ones `W11_PASSTHROUGH` takes."""
    e = os.environ if env is None else env
    return _mode(real_name(tool) if tool else None, e, PROXY_MODE_VAR, "W11_PROXY")


def _mode(tool, e, table=None, fallback="W11_PASSTHROUGH") -> str:
    table = _MODE_VAR if table is None else table
    names = ([table[tool]] if tool in table else []) + \
        [fallback]
    for var in names:
        v = (e.get(var) or "").strip().lower()
        if v in _NEVER:
            return "never"
        if v in _ALWAYS:
            return "always"
    return "auto"


def _detect(tool, e, respect_override=True):
    if respect_override:
        mode = _mode(tool, e)
        if mode == "never":
            return "wayland"
        if mode == "always":
            return "x11"

    if _env_wayland_socket(e):
        return "wayland"

    stype = (e.get("XDG_SESSION_TYPE") or "").strip().lower()
    if _sudo_uid(e) is None:        # `is None`: SUDO_UID=0 is still a sudo
        if stype == "wayland":
            return "wayland"
        if stype == "x11":
            return "x11"

    uid = target_uid(e)
    sess = logind_session(uid)
    if sess is not None:
        # recover the uid *before* the return: logind is the only source that knows whose session this is when
        # nothing invoked us as that user (root over ssh, root cron), and the socket scan below wants it
        if uid is None:
            uid = _int(sess.get("UID"))
        t = sess.get("TYPE", "").strip().lower()
        if t in ("wayland", "x11"):
            return t

    if find_wayland_socket(e, uid):
        return "wayland"
    if find_x_display(e, uid):
        return "x11"
    return None


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _sudo_uid(e):
    for var in ("SUDO_UID", "PKEXEC_UID"):
        uid = _int(e.get(var))
        if uid is not None:
            return uid
    return None


def target_uid(e=None):
    """uid of the session we are aimed at: the sudo/pkexec invoking user, else our own when we are not root,
    else None (= unknown, decide from logind or the sockets themselves).

    uid 0 is never an answer, from either source: root owns no graphical session, and `sudo -i` run *by* root
    (an `ssh root@box` doing `sudo -i xdotool ...`) leaves `SUDO_UID=0` behind, which would otherwise send the
    search off looking for a cookie in /root and hand the original an environment with no authority at all.
    `SUDO_UID=0` with a non-root uid is `root` running `sudo -u someone`, and there our own uid is the
    answer."""
    e = os.environ if e is None else e
    uid = _sudo_uid(e)
    if uid:
        return uid
    me = os.getuid()
    return me if me != 0 else None


def session_uid(e=None):
    """uid of the graphical session we are aimed at: `target_uid()` when it knows, else the uid logind records
    for the session we would use, else None.

    `ssh root@box` and root cron have no `SUDO_UID`, so `target_uid()` is None there — and an unqualified search
    then takes the *first* runtime directory, which on any box with a display manager is the greeter's (uid
    ~125): its cookie authorises nothing on the user's X server, and the original we exec would die with an
    authorisation error where it should have worked."""
    e = os.environ if e is None else e
    uid = target_uid(e)
    if uid is not None:
        return uid
    sess = logind_session(None)
    if sess is not None:
        return _int(sess.get("UID"))
    return None


def _owner(path):
    try:
        return os.stat(path).st_uid
    except OSError:
        return None


def _env_wayland_socket(e):
    """Path of the compositor socket named by $WAYLAND_DISPLAY, or None."""
    wd = e.get("WAYLAND_DISPLAY")
    if not wd:
        return None
    if wd.startswith("/"):
        return wd if os.path.exists(wd) else None
    dirs = [e.get("XDG_RUNTIME_DIR")]
    uid = target_uid(e)
    if uid is not None:
        dirs.append(os.path.join(_RUN_USER_DIR, str(uid)))
    for d in dirs:
        if d and os.path.exists(os.path.join(d, wd)):
            return os.path.join(d, wd)
    return None


def _runtime_dirs(e):
    """Runtime directories to scan, best first: $XDG_RUNTIME_DIR, then
    /run/user/* (as `(uid, dir)`)."""
    out = []
    d = e.get("XDG_RUNTIME_DIR")
    if d and os.path.isdir(d):
        out.append((_owner(d), d))
    try:
        names = sorted(os.listdir(_RUN_USER_DIR), key=lambda n: _int(n) or 0)
    except OSError:
        names = []
    for n in names:
        uid = _int(n)
        if uid is None:
            continue
        p = os.path.join(_RUN_USER_DIR, n)
        if p not in [q for _u, q in out]:
            out.append((uid, p))
    return out


def find_wayland_socket(e=None, uid=None):
    """A `wayland-*` socket belonging to the target session, or None.

    The uid qualification is the one real trap in this design: on an Xfce box whose display manager runs a
    Wayland greeter, `/run/user/<gdm>/wayland-0` exists while the *user's* session is X11. With no target uid
    known (root over ssh, cron) only a real user's socket counts."""
    e = os.environ if e is None else e
    sock = _env_wayland_socket(e)
    if sock is not None and (uid is None or _owner(sock) in (uid, None)):
        return sock
    for duid, d in _runtime_dirs(e):
        # /run/user/<uid> is that user's by construction, so a mismatching
        # directory is somebody else's session however the socket is owned
        if uid is not None and duid is not None and duid != uid:
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.startswith("wayland-") or n.endswith(".lock"):
                continue
            p = os.path.join(d, n)
            owner = _owner(p)
            if owner is None:
                owner = duid
            if uid is not None:
                if owner in (uid, None):
                    return p
            elif owner is not None and owner >= 1000:
                return p
    return None


def display_ok(d) -> bool:
    """Does this DISPLAY value name a server we can see? A local `:N` needs its socket; anything with a host
    part (`host:0`, `localhost:10.0` from `ssh -X`, `unix:0`) is not ours to check -- the original will say
    so."""
    d = (d or "").strip()
    if not d:
        return False
    if not d.startswith(":"):
        return True
    num = d[1:].split(".")[0]
    return bool(num.isdigit()
                and os.path.exists(os.path.join(_X11_SOCK_DIR, "X" + num)))


def find_x_display(e=None, uid=None):
    """DISPLAY of the X server of the target session, or None.

    `$DISPLAY` when its socket is there (or when it names another host: a forwarded or remote display is an X11
    session as far as we are concerned — the original works over it and we must not shadow that), else logind's
    recorded `DISPLAY=`, else the lowest-numbered `/tmp/.X11-unix/X*` owned by the target user (a root-owned one
    only when the user owns none: see below)."""
    e = os.environ if e is None else e
    d = (e.get("DISPLAY") or "").strip()
    if display_ok(d):
        return d
    sess = logind_session(uid if uid is not None else target_uid(e))
    if sess is not None:
        sd = sess.get("DISPLAY", "").strip()
        if sd:
            return sd
    mine, root = [], []
    try:
        names = os.listdir(_X11_SOCK_DIR)
    except OSError:
        names = []
    for n in names:
        if not n.startswith("X") or not n[1:].isdigit():
            continue
        owner = _owner(os.path.join(_X11_SOCK_DIR, n))
        if owner is None:
            continue
        if uid is None or owner == uid:
            mine.append(int(n[1:]))
        elif owner == 0:
            root.append(int(n[1:]))
    # The target user's own socket first, and only then a root-owned one: the order `session.find_x_display()`
    # already uses, for the same reason. A Wayland compositor creates the listening socket for its Xwayland
    # itself, as the session user, while a display manager leaves its greeter's root-owned Xorg behind on the
    # *lower* number (SDDM on KDE). "lowest wins, root accepted" therefore handed `sudo warandr` -- and every
    # handover -- a DISPLAY the session's cookie cannot open, so the X plane silently vanished from the answers.
    # A plain X11 session's Xorg *is* root-owned, so that stays the fallback.
    found = mine or root
    if found:
        return ":%d" % min(found)
    return None


def logind_session(uid=None):
    """logind's record of the graphical session of `uid` (any user when None), as a dict, or None.

    `/run/systemd/sessions/<id>` is world-readable key=value. The file says "do not parse"; this is a read-only
    best-effort fast path used only when the environment has told us nothing, and shelling out to `loginctl` is
    not an option (no subprocess spawns in these tools). Anything unexpected just means we fall through to the
    socket scan."""
    best = None
    best_key = None
    try:
        names = sorted(os.listdir(_LOGIND_DIR))
    except OSError:
        return None
    for n in names:
        if "." in n:            # <id>.ref is a FIFO -- never open it
            continue
        p = os.path.join(_LOGIND_DIR, n)
        try:
            if not os.path.isfile(p):
                continue
            with open(p, "r", errors="replace") as f:
                raw = f.read(65536)
        except OSError:
            continue
        rec = {}
        for line in raw.splitlines():
            if line.startswith("#"):
                continue
            k, sep, v = line.partition("=")
            if sep:
                rec[k.strip()] = v.strip()
        if rec.get("CLASS", "user") != "user":
            continue            # greeter / background
        if rec.get("REMOTE", "0") not in ("0", "no", "false"):
            continue            # ssh login: not the graphical session
        if uid is not None and _int(rec.get("UID")) != uid:
            continue
        t = rec.get("TYPE", "").strip().lower()
        if t not in ("wayland", "x11"):
            continue
        active = rec.get("STATE", "") == "active" or rec.get("ACTIVE", "") in ("1", "yes")
        key = (active, t == "wayland" or bool(rec.get("DISPLAY")), n)
        if best_key is None or key > best_key:
            best, best_key = rec, key
    return best


def find_xauthority(e=None, uid=None):
    """Cookie file for the target session's X server, or None: `$XAUTHORITY` when it exists, then the display
    manager's / compositor's cookie in the session runtime dir, then `~/.Xauthority`, then
    `session.find_xauthority()` (which also reads gnome-shell's own environment).

    With no target uid known, a *system* account's runtime directory is skipped — same rule as
    `find_wayland_socket()`, and for the same reason: on a box with a display manager the lowest-numbered
    runtime dir is the greeter's, and handing the original the greeter's cookie is worse than handing it
    none.

    `gdm/Xauthority` is in the runtime-dir list beside `.mutter-Xwaylandauth.*` and `xauth_*` because that is
    where GDM keeps an X11 session's cookie, one directory down: on noble-gnome-x11 the seated session's own
    $XAUTHORITY is /run/user/1000/gdm/Xauthority [M this rig, 2026-09-09]. `session.find_xauthority()` at the
    bottom of this function globs it too, but it is reached only after `~/.Xauthority`, and a home cookie left
    over from some earlier X server exists on plenty of boxes and authorises nothing on this one -- so the
    live file has to be found here, in the same mtime comparison as the other two."""
    import glob

    e = os.environ if e is None else e
    p = (e.get("XAUTHORITY") or "").strip()
    if p and os.path.exists(p):
        return p
    if uid is None:
        uid = session_uid(e)
    for duid, d in _runtime_dirs(e):
        if uid is not None:
            if duid is not None and duid != uid:
                continue
        elif duid is not None and duid < 1000:
            continue            # a greeter's cookie is not the user's
        cands = sorted(glob.glob(os.path.join(d, ".mutter-Xwaylandauth.*"))) + \
            sorted(glob.glob(os.path.join(d, "xauth_*"))) + \
            sorted(glob.glob(os.path.join(d, "gdm", "Xauthority")))
        best, best_m = None, -1.0
        for c in cands:
            try:
                m = os.stat(c).st_mtime
            except OSError:
                continue
            if m > best_m:
                best, best_m = c, m
        if best:
            return best
    if uid is not None:
        import pwd
        try:
            home = pwd.getpwuid(uid).pw_dir
        except KeyError:
            home = None
        if home:
            c = os.path.join(home, ".Xauthority")
            if os.path.exists(c):
                return c
    try:
        from w11common import session
        return session.find_xauthority(uid)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# finding the original
# ---------------------------------------------------------------------------

def self_paths():
    """Every path this process might be running from: `sys.argv[0]` (the kernel replaces our chosen argv[0] with
    the script path when a shebang is involved, so this is the real thing for a pyz or a console script), the
    same resolved against PATH when it carries no slash, and `__main__`'s file (for a zipapp that is
    `<pyz>/__main__.py`, so its directory is the pyz)."""
    out = []

    def add(p):
        if p and p not in out:
            out.append(p)

    a0 = sys.argv[0] if sys.argv else ""
    if a0:
        add(a0)
        if os.sep not in a0:
            # ...but only when what PATH answers really is one of ours: if we are *not* first on PATH, this
            # resolves to the original, and mistaking it for ourselves would leave nothing to hand over to
            w = _which_any(a0)
            if w and _is_our_file(w):
                add(w)
    f = getattr(sys.modules.get("__main__"), "__file__", None)
    if f:
        add(f)
        d = os.path.dirname(f)
        if d and os.path.isfile(d):     # zipapp member -> the archive itself
            add(d)
    return out


def _skip_path_element(d: str) -> bool:
    """An empty PATH element (a leading, trailing or doubled colon, and the one in os.defpath) means "the
    current directory" -- and we resolve the real tool *inside* the process, long after the user chose how to
    invoke us. Honouring it would search a directory the user merely cd'd into (an unpacked tarball, a shared
    /tmp, ~/Downloads) for a program we are about to execve, as them or as root; nobody installs xdotool
    there."""
    return not d


def _which_any(name, e=None):
    e = os.environ if e is None else e
    for d in (e.get("PATH") or os.defpath).split(os.pathsep):
        if _skip_path_element(d):
            continue
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _head(path):
    try:
        with open(path, "rb") as f:
            return f.read(_HEAD_BYTES)
    except OSError:
        return b""


def _imports_us(head: bytes) -> bool:
    """Does this file *import* one of our packages at the start of a line — the shape of a generated console
    script (`pip install .`) copied under an original's name? Precise where a substring search is not: a shell
    wrapper that mentions us in a comment has no such line."""
    for line in head.splitlines():
        for kw in (b"from ", b"import "):
            s = line.strip()
            if not s.startswith(kw):
                continue
            rest = s[len(kw):].strip()
            if rest and rest.split()[0].split(b".")[0].split(b",")[0] \
                    in _OUR_MODULES:
                return True
    return False


def _is_our_file(path: str) -> bool:
    """Guards 2 and 3 on their own: is this *file* one of ours, by the name it resolves to or by what its first
    4 KiB contain?

    The head sniff answers yes only to the build's own stamp or to a script that imports one of our packages. It
    deliberately does *not* answer yes to a bare `w11`/`wmctrl` anywhere in the head: a wrapper that
    merely mentions us would then be skipped, `real_tool()` would report nothing installed, and the user would
    be told to install what is already there."""
    base = os.path.basename(os.path.realpath(path))
    if base in OUR_NAMES or base.split(".")[0] in OUR_NAMES:
        return True
    head = _head(path)
    if head[:4] == b"\x7fELF" or head[:2] in (b"MZ", b"\xca\xfe"):
        return False            # a compiled binary is never us: pure Python
    return STAMP in head or _imports_us(head)


def is_us(cand: str) -> bool:
    """Is this candidate one of our own executables? Four independent guards,
    because each alone has a hole:

    1. same file as one of `self_paths()` (`samestat` beats string equality:
       hardlinks, bind mounts, the /usr merge);
    2. it resolves to a file named like one of our tools (the normal install,
       where `xdotool` is a *symlink* to our `wdotool` and therefore not the
       same file as the `xdotool` we were invoked as);
    3. head sniff: a compiled binary is never us (we are pure Python), the
       build's stamp in the first 4 KiB is, and so is an import of one of our
       packages (a generated console script);
    4. `GUARD_VAR`, checked once per process in `maybe_exec_real()` -- the only
       guard that is guaranteed to terminate.
    """
    try:
        st = os.stat(cand)
    except OSError:
        return False
    for me in self_paths():
        try:
            if os.path.samestat(st, os.stat(me)):
                return True
        except OSError:
            continue
    return _is_our_file(cand)


def real_tool(name: str, env=None):
    """Absolute path of the *original* `name` (an original's name or ours), or None when nothing usable is
    installed.

    `$WDOTOOL_REAL_XDOTOOL` and friends win outright; when one is set and unusable that is a `RealToolError`,
    never a silent fallback to PATH (a typo'd override that quietly ran something else would be worse)."""
    e = os.environ if env is None else env
    name = real_name(name)
    var = _OVERRIDE.get(name)
    override = (e.get(var) or "").strip() if var else ""
    if override:
        if os.path.isdir(override) or not os.access(override, os.X_OK):
            raise RealToolError(
                "%s=%s is not an executable file" % (var, override))
        return os.path.abspath(override)
    for d in (e.get("PATH") or os.defpath).split(os.pathsep):
        if _skip_path_element(d):
            continue
        cand = os.path.join(d, name)
        if not os.path.isfile(cand) or not os.access(cand, os.X_OK):
            continue
        if is_us(cand):
            continue
        return os.path.abspath(cand)
    return None


# ---------------------------------------------------------------------------
# the handover
# ---------------------------------------------------------------------------

def _guard_list(e):
    raw = e.get(GUARD_VAR) or ""
    return [p for p in raw.split(os.pathsep) if p]


def _handover_loop(e):
    """Were *we* exec'd as somebody's "real tool"? Then the install is a loop (two copies of us under two names
    on PATH) and one more handover would just do it again."""
    seen = _guard_list(e)
    if not seen:
        return False
    if len(seen) >= GUARD_DEPTH:
        return True
    for me in self_paths():
        try:
            if os.path.realpath(me) in seen:
                return True
        except OSError:
            continue
    return False


#: exactly what each original spells "print help/version and exit", as its
#: *first* argument. Exact, not a prefix and not "made of h and v", because
#: those options mean other things elsewhere: `-v` is *verbose* in wmctrl
#: (`wmctrl -v -l`) and unrecognised in xprop, and `--verbose` is a real
#: xrandr option. Reading any of them as help would run our own Wayland-only
#: code and print a Wayland error, where the sibling `wmctrl -l` correctly
#: exits 127 saying which package to install.
_HELP_ARGS = {
    "xdotool": ("-h", "--help", "help", "-v", "--version", "version"),
    "wmctrl": ("-h", "-V", "--help", "--version"),
    "xprop": ("-help", "-version", "-grammar"),
    "xrandr": ("-help", "--help", "-v", "--version"),
}


def _is_help_request(tool, args):
    """A help/version request (or nothing at all). With no original installed these keep our own output instead
    of exiting 127 -- a help request must never answer "not found"."""
    if not args:
        return True
    a = args[0]
    if a in _HELP_ARGS.get(tool, ()):
        # wmctrl special-cases exactly `wmctrl --help` / `wmctrl --version`;
        # with anything after them it is back to plain getopt
        return not (tool == "wmctrl" and a.startswith("--") and len(args) != 1)
    # xdotool's only top-level short options are -h and -v (`getopt_long(argc, argv, "+hv", ...)`), so a cluster
    # of them is one of the two, whichever comes first: `xdotool -hv` prints the help, `xdotool -vh key a` the
    # version (both verified against xdotool 3.x)
    if tool == "xdotool" and len(a) > 1 and a[0] == "-" and a[1] != "-":
        return all(c in "hv" for c in a[1:])
    return False


def _missing_message(tool, x11=True):
    """Exit 127's line.  `x11` is False when the session is not an X11 one and the handover was asked for
    anyway (`W11_PASSTHROUGH=always`, `wxrandr --backend x11`): the reason to install the original
    is then the request rather than the session, and "this is an X11 session" would be untrue there.

    The install command comes from `w11common.distro`, keyed on `/etc/os-release`: this line printed
    `apt install xdotool` on a NixOS box with no apt on it, and named packages Fedora and Arch do not have
    [M recon2/nixos.md, fedora.md, arch.md]. Debian's bytes are unchanged, and are also what a distribution
    we cannot identify gets."""
    why = ("this is an X11 session" if x11
           else "a handover to the real tool was asked for")
    return (
        "%s: this is w11's clone and %s, but no "
        "real %s was found on PATH -- install it (%s) or set "
        "%s=/path/to/%s\n" % (tool, why, tool, distro.hint(tool),
                              _OVERRIDE.get(tool, ""), tool)
    )


def _argv0(tool):
    """The original prints argv[0] in its own messages, so invoked as `xdotool` we stay `xdotool`; invoked under
    our own name (or `-m`), the original still has to call itself by its own name for its usage text to be
    internally consistent."""
    base = os.path.basename(sys.argv[0] or "") if sys.argv else ""
    return base if base == tool else tool


def _display_owner_uid(display):
    """uid owning the unix socket `display` names, or None (a remote or
    non-local DISPLAY has no socket to own)."""
    d = (display or "").strip()
    if not d.startswith(":"):
        return None
    num = d[1:].split(".")[0]
    if not num.isdigit():
        return None
    return _owner(os.path.join(_X11_SOCK_DIR, "X" + num))


def repair_x_env(e):
    """Fill the session's `$DISPLAY`/`$XAUTHORITY` into the dict `e`, in place; returns `e`.

    Under `sudo`, `ssh root@box` and cron, both are absent or point at a dead display, and an X11 program then
    fails where it has no business failing. We already know how to find the session's X plane, so we inject it
    -- which is what makes `sudo xdotool key a` work *through* us. Values that already work are never touched.

    Used for every X11 child we start, not only the handover: `warandr` runs the real `xrandr` as a child rather
    than exec'ing it, and without this it was the one tool that still said `Can't open display` from a root
    shell."""
    # session_uid(), not target_uid(): as root with no SUDO_UID (an `ssh root@box`, a root cron job) the uid is
    # not ours and not in the environment, and only logind knows it -- without it we would pick the first
    # runtime dir there is, which is the display manager's.
    uid = session_uid(e)
    if not display_ok(e.get("DISPLAY")):
        found = find_x_display(e, uid)
        if found:
            e["DISPLAY"] = found
    if uid is None:
        # With no session uid, find_x_display() fell back to scanning /tmp/.X11-unix -- which is world-writable,
        # so the lowest-numbered socket there may be one a local user bound ahead of the real server. Take the
        # cookie owner from the display we are about to use: a planted server then only ever gets its own
        # owner's cookie, never the real user's (which is full X11 access to their session).
        uid = _display_owner_uid(e.get("DISPLAY"))
    xa = (e.get("XAUTHORITY") or "").strip()
    if not xa or not os.path.exists(xa):
        found = find_xauthority(e, uid)
        if found:
            e["XAUTHORITY"] = found
        elif xa:
            # a dead $XAUTHORITY is worse than no $XAUTHORITY: it suppresses
            # the original's own ~/.Xauthority default
            e.pop("XAUTHORITY", None)
    return e


def child_env(tool, real, env=None):
    """The environment the original is exec'd with: ours, plus the X-plane
    repair (`repair_x_env`) and the handover guard."""
    e = repair_x_env(dict(os.environ if env is None else env))
    seen = _guard_list(e)
    try:
        seen.append(os.path.realpath(real))
    except OSError:
        seen.append(real)
    e[GUARD_VAR] = os.pathsep.join(seen[-GUARD_DEPTH:])
    return e


def exec_real(tool, real, args, env=None) -> int:
    """Replace this process with the original. Only returns (127) when the
    exec itself fails."""
    e = child_env(tool, real, env)
    argv = [_argv0(tool)] + list(args)
    # Python ignores SIGPIPE and SIGXFSZ, and an *ignored* disposition survives execve -- so without this reset
    # `xprop -root | head -1` would print an EPIPE error where the original dies quietly of SIGPIPE.
    for sig in ("SIGPIPE", "SIGXFSZ"):
        s = getattr(signal, sig, None)
        if s is None:
            continue
        try:
            signal.signal(s, signal.SIG_DFL)
        except (OSError, ValueError):
            pass
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except Exception:
            pass
    try:
        os.execve(real, argv, e)
    except OSError as exc:
        sys.stderr.write("%s: cannot execute %s: %s\n"
                         % (tool, real, exc.strerror or exc))
        return 127
    return 127                  # unreachable


def maybe_exec_real(tool, args=None, *, fallback_native=False, entry=True,
                    env=None, force=False):
    """The hook every CLI calls first. Returns None to keep running our own code, or an exit status to return
    from `main()` (it usually does not return at all: `os.execve` replaces the process).

    `args` is the tool's arguments **without** argv[0] -- the four `main()`s disagree about that (wdotool's
    takes the whole of `sys.argv`, the other three take `sys.argv[1:]`), so each hook normalises before calling
    here. `entry=False` says an explicit argv was handed to `main()` by a caller embedding us as a library (the
    whole test suite does this): replacing *their* process would be violent, so we never do it.

    `force=True` hands over whatever the session says (API note, this file being frozen: added for
    `wxrandr --backend x11`, which is a user asking for the real tool in so many words -- a Wayland session
    included).  It does not override `entry`: a library caller's process is still never replaced.
    """
    tool = real_name(tool)
    e = os.environ if env is None else env
    if not entry:
        return None
    if not force and session_kind(tool, e) != "x11":
        return None
    args = list(sys.argv[1:] if args is None else args)
    try:
        if _handover_loop(e):
            raise RealToolError(
                "handover loop: this X11 session's %s resolves to w11's "
                "clone again (%s=%s). Install the real %s next to us on PATH, "
                "or set %s=/path/to/%s"
                % (tool, GUARD_VAR, e.get(GUARD_VAR, ""), tool,
                   _OVERRIDE.get(tool, ""), tool))
        real = real_tool(tool, e)
    except RealToolError as exc:
        sys.stderr.write("%s: %s\n" % (tool, exc))
        return 127
    if real is None:
        if fallback_native or _is_help_request(tool, args):
            return None
        # respect_override=False: the variables say what to do about the handover, and the question
        # here is what the session really is, which is what the line about to be printed claims.
        sys.stderr.write(_missing_message(
            tool, session_kind(tool, e, respect_override=False) == "x11"))
        return 127
    return exec_real(tool, real, args, e)
