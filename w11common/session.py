"""Discovery of the graphical session's sockets, tolerant of running under sudo
(where XDG_RUNTIME_DIR etc. point at root's empty runtime dir or are unset).
Every tool here starts by asking this module which session it is aimed at, so a
change reaches all six: docs/Technical.md section 2 is the contract.

Four cases that were each broken on a stock GNOME box, and are each a rule now:

* `ssh root@box` (no sudo): pam_systemd gives root its own
  XDG_RUNTIME_DIR=/run/user/0 with a `bus` in it, so the old "trust
  $XDG_RUNTIME_DIR first" order found root's empty bus and no compositor.
  Candidate runtime dirs are now anchored on the graphical session: a dir
  that holds a `wayland-*` socket sorts before one that does not, then the
  sudo/pkexec invoking user, then real users, then the rest. $XDG_RUNTIME_DIR
  still wins when it has a Wayland socket (the normal in-session case, and
  the test rigs' private runtime dirs).
* Likewise `find_user_bus()`: root's own `DBUS_SESSION_BUS_ADDRESS`
  (pam_systemd exports `/run/user/0/bus` to an `ssh root@` login) only wins
  when that bus lives next to a Wayland socket; otherwise the scanned bus of
  the graphical session is used.
* `PKEXEC_UID` is honoured next to `SUDO_UID`.
* The X plane of a GNOME session (Xwayland) is found with `find_x_display()`
  (DISPLAY: $DISPLAY, gnome-shell's own environment via /proc, or the
  session user's /tmp/.X11-unix/X* socket) and `find_xauthority()`
  ($XAUTHORITY, Mutter's $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.* cookie,
  GDM's xauth_*, ~/.Xauthority). The GNOME backend asks the bridge first
  (XInfo = gnome-shell's DISPLAY/XAUTHORITY) and uses these as fallbacks.
  `xwayland_running()` tells whether Xwayland is actually up without
  connecting to its socket (Mutter spawns it on demand).
* `find_x_display()` prefers a socket owned by the session user over a
  root-owned one instead of taking the lowest number of either: SDDM leaves
  its greeter's root-owned Xorg on `:0` while the KDE session's Xwayland is
  `:1`, so under `sudo`/`ssh root@` the old order answered with a display the
  session's cookie cannot open and every X-side answer (real `WM_CLASS`,
  client geometry, `wxprop -id`) silently degraded to the synthesized one."""

import glob
import os
import pwd
import stat

from w11common.errors import CmdError

X11_SOCKET_DIR = "/tmp/.X11-unix"
#: test seam (production value): the per-user runtime directories
RUN_USER_DIR = "/run/user"
#: test seam (production value): where the two compositors that can run with no
#: runtime directory put their socket instead (i3 and Wayfire, below)
TMP_DIR = "/tmp"
#: Stand-in for $XDG_RUNTIME_DIR when the session did not give us one.
FALLBACK_RUNTIME_DIR = "/tmp/wdotool-%d"


def runtime_dir(*, uid: int | None = None) -> str:
    """The directory these tools keep session-lifetime files in -- the input daemon's socket, the KWin script
    lock, the per-compositor state files: $XDG_RUNTIME_DIR when the session gave us one, else a private
    `/tmp/wdotool-<uid>`, created 0700 and then verified.

    The fallback is not exotic: `sudo` drops XDG_RUNTIME_DIR, and so do `su -`, cron and a bare container -- all
    documented ways to run these tools. /tmp is world-writable, so any file we would put there under a guessable
    name can be created by another local user first. For the socket that means every request -- the text of
    `type` included -- is delivered to them, and they can reply {"ok":true} so the caller sees a success; for a
    state file it means they choose the answers it holds, including the pid a root process signals. A directory
    nobody else may enter closes that for everything inside it. It is verified after creation because an
    attacker may have created it first.

    Raises CmdError when that directory cannot be made, or is not ours. Callers for which a runtime path is a
    convenience rather than a contract catch it and fall back to their own name under /tmp.

    `uid` (keyword-only, default None = every answer above, unchanged) names ANOTHER user's session: root
    looking for the seated user's proxy. `ssh root@box` gets its own XDG_RUNTIME_DIR=/run/user/0 from
    pam_systemd and `os.getuid()` is 0, so the unqualified answer is /run/user/0 or /tmp/wdotool-0 and the
    seated user's files are never found [M goal2/recon/gaps.md §3d: with XDG_RUNTIME_DIR unset this answers
    /tmp/wdotool-1000 for uid 1000, and /tmp/wdotool-0 for root]. The uid'd answer is `_foreign_runtime_dir`
    below; xw11/display.py:display_file_path() is the caller that needs it (xw11/wrap.py hands it
    `passthrough.session_uid`)."""
    if uid is not None and uid != os.getuid():
        return _foreign_runtime_dir(uid)
    rd = os.environ.get("XDG_RUNTIME_DIR")
    if rd and os.path.isdir(rd):
        return rd
    d = FALLBACK_RUNTIME_DIR % os.getuid()
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    except OSError as e:
        raise CmdError(f"cannot create {d}: {e}") from None
    try:
        st = os.lstat(d)
    except OSError as e:
        raise CmdError(f"cannot stat {d}: {e}") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise CmdError(
            f"{d} is not a private directory owned by uid {os.getuid()}; "
            "refusing to put the wdotool socket there")
    return d


def _foreign_runtime_dir(uid: int) -> str:
    """The runtime directory of the session belonging to `uid`, for a process that is not that uid -- root
    attaching to the seated user's proxy.

    `runtime_dir_candidates()` already returns `(uid, dir)` best-first and prefers a directory that holds a
    `wayland-*` socket, so the graphical session's own dir is picked out of it by uid; the /tmp fallback is
    checked the same way `runtime_dir()` checks its own, and the same refusal applies (anyone may create
    /tmp/wdotool-<uid> first).

    Nothing is ever CREATED here, which is the one rule that differs from `runtime_dir()`: a root-owned
    /tmp/wdotool-1000 made on the user's behalf is a directory their own `runtime_dir()` would then refuse
    for the rest of the box's uptime ("not a private directory owned by uid 1000"). With no directory there
    the path is returned as it is, the lookup above it finds no display file, and xw11/wrap.py's guard
    declines to start a proxy as root rather than starting a second one nobody can reach."""
    for u, d in runtime_dir_candidates():
        if u == uid:
            return d
    d = FALLBACK_RUNTIME_DIR % uid
    try:
        st = os.lstat(d)
    except OSError:
        return d
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
        raise CmdError(
            f"{d} is not a private directory owned by uid {uid}; "
            "refusing to look for that session's files there")
    return d


def _owner(path: str) -> int | None:
    """uid owning `path`, or None when it cannot be stat()ed."""
    try:
        return os.stat(path).st_uid
    except OSError:
        return None


def _sudo_uid():
    for var in ("SUDO_UID", "PKEXEC_UID"):
        try:
            return int(os.environ[var])
        except (KeyError, ValueError):
            continue
    return None


def _has_wayland_socket(d: str) -> bool:
    try:
        return any(n.startswith("wayland-") and not n.endswith(".lock") for n in os.listdir(d))
    except OSError:
        return False


def runtime_dir_candidates() -> list[tuple[int, str]]:
    """(uid, dir) candidates, best first: dirs holding a wayland-* socket (the graphical session) before the
    rest; within each group $XDG_RUNTIME_DIR, then the sudo/pkexec-invoking user, then real users (uid>=1000),
    then the others."""
    out = []
    d = os.environ.get("XDG_RUNTIME_DIR")
    if d and os.path.isdir(d):
        out.append((os.stat(d).st_uid, d))
    target = _sudo_uid()
    dirs = []
    try:
        for name in os.listdir(RUN_USER_DIR):
            if name.isdigit():
                dirs.append((int(name), os.path.join(RUN_USER_DIR, name)))
    except OSError:
        pass
    dirs.sort(key=lambda t: (t[0] != target, t[0] < 1000, t[0]))
    seen = {p for _u, p in out}
    out.extend(t for t in dirs if t[1] not in seen)
    # stable: keeps the order above inside each group
    out.sort(key=lambda t: not _has_wayland_socket(t[1]))
    return out


def _scan(match) -> tuple[int, str] | None:
    for uid, d in runtime_dir_candidates():
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if match(n):
                return uid, os.path.join(d, n)
    return None


def _scan_sub(sub: str):
    """(uid, path) of every entry of `<candidate runtime dir>/<sub>/`, best runtime dir first, names sorted.

    `_scan` lists one directory level, and two compositors put their socket one level further down: i3 writes
    `$XDG_RUNTIME_DIR/i3/ipc-socket.<pid>` and Hyprland `$XDG_RUNTIME_DIR/hypr/<instance>/.socket.sock`. Neither
    was findable before [M recon2/i3.md §1: with XDG_RUNTIME_DIR=/tmp/rti3, `i3 --get-socketpath` said
    /tmp/rti3/i3/ipc-socket.65857 and find_sway_socket() answered None]."""
    for uid, d in runtime_dir_candidates():
        base = os.path.join(d, sub)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for n in names:
            yield uid, os.path.join(base, n)


def _candidate_uids() -> list[int]:
    """The uids of the candidate runtime dirs, best first, with our own last.

    The two /tmp fallbacks below (i3 with no XDG_RUNTIME_DIR, Wayfire with none either) have no runtime
    directory to be anchored on, so the owner they check against comes from here; our own uid is appended
    because a session with no /run/user entry at all still has a socket in /tmp that belongs to us."""
    out = [u for u, _d in runtime_dir_candidates()]
    mine = os.getuid()
    if mine not in out:
        out.append(mine)
    return out


def find_wayland_socket() -> tuple[int, str, str] | None:
    """(uid, runtime_dir, socket_path) of the graphical session, or None.

    $WAYLAND_DISPLAY alone names the socket. It used to be honoured only together with $XDG_RUNTIME_DIR, and the
    two do not always travel together: under `sudo` XDG_RUNTIME_DIR is root's own (or unset) while
    WAYLAND_DISPLAY survives, and a display named on the command line (`wxrandr -d wayland-1`, which sets
    WAYLAND_DISPLAY and nothing else) sets only the one variable. Requiring both dropped the named display on
    the floor and scanned up whichever socket sorted first instead -- so `sudo wxrandr -d wayland-1` answered
    about wayland-0. The name is therefore looked for in $XDG_RUNTIME_DIR first (the in-session case, unchanged)
    and then in the candidate runtime dirs; only a name that exists nowhere falls through to the scan."""
    rd = os.environ.get("XDG_RUNTIME_DIR")
    wd = os.environ.get("WAYLAND_DISPLAY")
    if wd and wd.startswith("/"):
        uid = _owner(wd)
        if uid is not None:
            return uid, rd or os.path.dirname(wd), wd
    elif wd:
        dirs = ([rd] if rd else []) + [d for _u, d in runtime_dir_candidates()]
        for d in dirs:
            sock = os.path.join(d, wd)
            uid = _owner(sock)
            if uid is not None:
                return uid, d, sock
    hit = _scan(lambda n: n.startswith("wayland-") and not n.endswith(".lock"))
    if hit:
        uid, sock = hit
        return uid, os.path.dirname(sock), sock
    return None


def find_sway_socket() -> str | None:
    """The sway or i3 IPC socket, or None.

    $SWAYSOCK then $I3SOCK when the path they name exists, then the three names a live compositor really
    writes. The `i3-ipc.*.sock` pattern this used to scan for matches nothing any i3 or sway has ever written
    [M recon2/i3.md §1]: sway writes `sway-ipc.<uid>.<pid>.sock` at the top of the runtime dir, i3 writes
    `i3/ipc-socket.<pid>` one level down, and with no runtime dir at all `/tmp/i3-<user>.XXXXXX/ipc-socket.<pid>`
    (measured: `/tmp/i3-yans.cxtC3m/ipc-socket.53335`, the directory 0700). i3 exports $I3SOCK into the
    processes it spawns but not into its own environ, so on i3 the scan is the only route for anything the
    compositor did not start."""
    for var in ("SWAYSOCK", "I3SOCK"):
        p = os.environ.get(var)
        if p and os.path.exists(p):
            return p
    hit = _scan(lambda n: n.startswith("sway-ipc.") and n.endswith(".sock"))
    if hit:
        return hit[1]
    for _uid, p in _scan_sub("i3"):
        if os.path.basename(p).startswith("ipc-socket."):
            return p
    return _find_tmp_i3_socket()


def _find_tmp_i3_socket() -> str | None:
    """`<TMP_DIR>/i3-<user>.XXXXXX/ipc-socket.<pid>`, or None. /tmp is world-writable, so both the directory
    and the socket have to be owned by the user whose session we are aimed at: anyone may create
    `/tmp/i3-<someone else>.aaaaaa/ipc-socket.1` and answer for a compositor that is not there."""
    for uid in _candidate_uids():
        try:
            name = pwd.getpwuid(uid).pw_name
        except KeyError:
            continue
        pattern = os.path.join(glob.escape(TMP_DIR), "i3-%s.*" % glob.escape(name))
        for d in sorted(glob.glob(pattern)):
            if _owner(d) != uid:
                continue
            for p in sorted(glob.glob(os.path.join(glob.escape(d), "ipc-socket.*"))):
                if _owner(p) == uid:
                    return p
    return None


def find_hypr_socket() -> str | None:
    """Hyprland's request/response IPC socket (`.socket.sock`), or None.

    `$HYPRLAND_INSTANCE_SIGNATURE` names the instance directory, and Hyprland puts that variable into the
    systemd user manager rather than into anything we inherit under sudo, so the signature is a shortcut and
    never a requirement. With no signature the runtime dirs are walked for `hypr/*/.socket.sock` and the
    instance whose directory still holds `hyprland.lock` wins: stale instance directories accumulate beside the
    live one, five of them after five restarts [M recon2/hyprland.md §1, socket at
    /run/user/1000/hypr/<sig>/.socket.sock]. The event socket is `.socket2.sock` in the same directory."""
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    rd = os.environ.get("XDG_RUNTIME_DIR")
    if sig and rd:
        p = os.path.join(rd, "hypr", sig, ".socket.sock")
        if os.path.exists(p):
            return p
    locked = stale = None
    for uid, d in _scan_sub("hypr"):
        p = os.path.join(d, ".socket.sock")
        # the socket has to belong to the owner of the runtime directory it sits
        # in: an instance directory another user planted in this session's
        # runtime dir would otherwise be answered with, and every request --
        # `dispatch`, and the window titles that come back -- would go there
        if _owner(p) != uid:
            continue
        if sig and os.path.basename(d) == sig:
            return p
        if locked is None and os.path.exists(os.path.join(d, "hyprland.lock")):
            locked = p
        if stale is None:
            stale = p
    return locked or stale


def find_wayfire_socket() -> str | None:
    """Wayfire's JSON IPC socket, or None.

    `$WAYFIRE_SOCKET` is what Wayfire exports to its own children; `$_WAYFIRE_SOCKET` is what the user may set
    to choose the path. Then `wayfire-*.socket` in the runtime dirs, and last the /tmp fallback the plugin uses
    when there is no runtime dir, owner-checked because /tmp is world-writable. `plugins/ipc/ipc.cpp` writes
    two different names: the runtime-dir one carries no pid at all (`wayfire-<display>-.socket`, measured here
    as `/tmp/wfrt1/wayfire-wayland-1-.socket` with XDG_RUNTIME_DIR=/tmp/wfrt1) and the /tmp one does
    (`wayfire-<display>-<pid>.socket`) [M recon2/wayfire.md §1.2]. So the match is on the prefix and the
    suffix and never on the shape between them."""
    for var in ("WAYFIRE_SOCKET", "_WAYFIRE_SOCKET"):
        p = os.environ.get(var)
        if p and os.path.exists(p):
            return p
    hit = _scan(lambda n: n.startswith("wayfire-") and n.endswith(".socket"))
    if hit:
        return hit[1]
    uids = _candidate_uids()
    for p in sorted(glob.glob(os.path.join(glob.escape(TMP_DIR), "wayfire-*.socket"))):
        if _owner(p) in uids:
            return p
    return None


def find_user_bus() -> tuple[int, str] | None:
    """(uid, DBUS_SESSION_BUS_ADDRESS) of the graphical session, or None.

    $DBUS_SESSION_BUS_ADDRESS wins when its socket sits next to a Wayland socket (the in-session case).
    `ssh root@box` gets its own DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/0/bus from pam_systemd -- a bus
    with no compositor on it -- so a scanned bus that does live in the graphical session's runtime dir beats an
    environment bus that does not. With no Wayland socket anywhere the old order holds (env, then scan)."""
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    env_hit = None
    if addr.startswith("unix:path="):
        path = addr[len("unix:path=") :].split(",")[0]
        if os.path.exists(path):
            env_hit = (os.stat(path).st_uid, addr)
            if _has_wayland_socket(os.path.dirname(path)):
                return env_hit
    hit = _scan(lambda n: n == "bus")
    if hit and _has_wayland_socket(os.path.dirname(hit[1])):
        return hit[0], f"unix:path={hit[1]}"
    if env_hit:
        return env_hit
    if hit:
        return hit[0], f"unix:path={hit[1]}"
    return None


def find_session_bus() -> tuple[int, str] | None:
    """(uid, DBUS_SESSION_BUS_ADDRESS) of the bus the *compositor* lives on. Additive (GNOME): under
    `ssh root@host` pam_systemd hands root its own /run/user/0 (with a user bus of its own, where no Mutter is),
    so find_user_bus() lands on the wrong bus. Anchor on the runtime dir that owns the Wayland socket instead;
    $DBUS_SESSION_BUS_ADDRESS still wins when it belongs to that same user (the normal session / `sudo -E`)."""
    hit = find_wayland_socket()
    if hit is None:
        return find_user_bus()
    uid, rd, _sock = hit
    env = find_user_bus()
    if env is not None and env[0] == uid:
        return env
    p = os.path.join(rd, "bus")
    if os.path.exists(p):
        return os.stat(p).st_uid, f"unix:path={p}"
    return env


# -- X plane (Xwayland) ------------------------------------------------------

def session_uid() -> int | None:
    """uid of the graphical session we are aimed at (Wayland socket owner,
    else the runtime-dir candidate order), or None."""
    hit = find_wayland_socket()
    if hit:
        return hit[0]
    cands = runtime_dir_candidates()
    return cands[0][0] if cands else None


def _runtime_dir_of(uid: int | None) -> str | None:
    for u, d in runtime_dir_candidates():
        if uid is None or u == uid:
            return d
    return None


#: Session processes whose own environment names the session's X plane
#: ($DISPLAY, $XAUTHORITY), best first. The eight desktops that were already
#: here keep their ranks: a session that runs one of them is decided by it,
#: and the nine appended below only speak where none of the eight is running.
#:
#: The nine are not a wish list. From `ssh root@box` with an empty environment
#: all four tools failed on LXQt with "Authorization required, but no
#: authorization protocol specified": SDDM 0.21 writes /tmp/xauth_<random>,
#: which is in no runtime directory and is not ~/.Xauthority, and no LXQt
#: process was in this tuple -- so `_shell_environ()` never read the one
#: environment that names the cookie. Appending the names turned all four
#: tools from broken to working in the same session [M recon2/openbox.md §2].
#: The same gap was measured for `mate-session` [M recon2/mate.md: _shell_environ
#: {} -> DISPLAY=:91 with the name added] and for i3 [M recon2/i3.md §2d].
_SESSION_LEADERS = ("gnome-shell", "startplasma-x11", "kwin_x11",
                    "kwin_wayland", "plasmashell", "ksmserver",
                    "xfce4-session", "sway",
                    "i3", "mate-session", "cinnamon-session", "cinnamon",
                    "lxqt-session", "lxsession", "openbox", "labwc",
                    "wayfire")


def _leader_comms(name: str) -> tuple[str, str]:
    """The two /proc/<pid>/comm spellings of a leader called `name`.

    comm is 15 bytes and comes from the executed file, so both spellings are truncated here rather than assumed
    to fit: "startplasma-x11" is exactly 15 and "cinnamon-session" is 16, which comm reports as
    "cinnamon-sessio". (`startlxqtwayland` is 16 too and is deliberately not in the tuple: its truncation
    collides with nothing, but the process that matters on LXQt-Wayland is `lxqt-session` [M openbox.md].)

    The second spelling is nixpkgs': a `wrapGAppsHook`/`wrapQtAppsHook` program is a wrapper binary beside a
    hidden real one, and the real one is what gets executed -- nixpkgs' gnome-shell runs with comm
    ".gnome-shell-wr", so on a NixOS GNOME session the scan found no session leader at all and
    $DISPLAY/$XAUTHORITY discovery for root and cron was gone [M recon2/nixos.md §6.1, measured on this repo's
    own nix build: `.wdotool-wrappe`]. sway's nixpkgs wrapper is a shell script that execs sway, so `sway` keeps
    its plain comm and both spellings have to be accepted."""
    return name[:15], ("." + name + "-wrapped")[:15]


#: {comm as /proc reports it: rank in _SESSION_LEADERS}. Built once; a spelling
#: that collides with an earlier entry's keeps the better rank.
_LEADER_RANK: dict[str, int] = {}
for _rank, _leader in enumerate(_SESSION_LEADERS):
    for _comm in _leader_comms(_leader):
        _LEADER_RANK.setdefault(_comm, _rank)


def _shell_environ(uid: int | None) -> dict[str, str]:
    """Environment of the session's own compositor or session leader (readable as root or as that user), {} when
    not found/readable.

    More than gnome-shell, because a display manager may keep the X cookie where no search can find it: SDDM
    writes /tmp/xauth_<random>, which is neither ~/.Xauthority nor anything in a runtime directory, so on a
    Plasma X11 session one of the session's own processes is the only place a root shell (`ssh root@box`, cron)
    can learn the cookie path at all -- without it the original we hand over to dies with
    `Authorization required, but no authorization protocol specified` where it should have worked. The scan is
    uid-qualified, exactly as before, so it never reads another user's session; what it trusts is a process of
    the target user, the same trust ~/.Xauthority already gets."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return {}
    best, best_rank = {}, len(_SESSION_LEADERS)
    for p in pids:
        try:
            with open(f"/proc/{p}/comm") as f:
                comm = f.read().strip()
            rank = _LEADER_RANK.get(comm, -1)
            if rank < 0 or rank >= best_rank:
                continue
            if uid is not None and os.stat(f"/proc/{p}").st_uid != uid:
                continue
            with open(f"/proc/{p}/environ", "rb") as f:
                raw = f.read()
        except OSError:
            continue
        env = {}
        for item in raw.split(b"\0"):
            k, sep, v = item.partition(b"=")
            if sep:
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        if env:
            best, best_rank = env, rank
            if rank == 0:
                break
    return best


def xwayland_running(uid: int | None = None) -> bool:
    """Is an Xwayland server process alive (for `uid`'s session, or any)? Mutter and KWin spawn Xwayland on
    demand and keep the listening socket themselves, so the socket's existence says nothing -- and connecting to
    it to find out would start the server. The process table answers without side effects (comm is
    world-readable)."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for p in pids:
        try:
            with open(f"/proc/{p}/comm") as f:
                if f.read().strip() != "Xwayland":
                    continue
            if uid is not None and os.stat(f"/proc/{p}").st_uid != uid:
                continue
        except OSError:
            continue
        return True
    return False


def find_x_display(uid: int | None = None) -> str | None:
    """DISPLAY of the session's X server (Xwayland), or None. $DISPLAY when its socket exists; else
    gnome-shell's own DISPLAY (procfs); else the lowest-numbered X11_SOCKET_DIR/X* socket owned by the session
    user (a root-owned one only when the user owns none: see below)."""
    d = os.environ.get("DISPLAY", "")
    if d.startswith(":"):
        num = d[1:].split(".")[0]
        if num.isdigit() and os.path.exists(f"{X11_SOCKET_DIR}/X{num}"):
            return d
    if uid is None:
        uid = session_uid()
    env_d = _shell_environ(uid).get("DISPLAY", "")
    if env_d.startswith(":"):
        return env_d
    mine, root = [], []
    for path in glob.glob(X11_SOCKET_DIR + "/X*"):
        num = os.path.basename(path)[1:]
        if not num.isdigit():
            continue
        owner = _owner(path)
        if owner is None:
            continue
        if uid is None or owner == uid:
            mine.append(int(num))
        elif owner == 0:
            root.append(int(num))
    # The session user's own socket first, and only then a root-owned one. A Wayland compositor creates the
    # listening socket for its Xwayland itself, as the session user; a display manager's greeter leaves a
    # root-owned Xorg socket behind on the *lower* number, and taking that one (as "owner == uid or owner == 0,
    # lowest wins" did) hands out a DISPLAY that the session's cookie cannot open -- the whole X plane then
    # silently disappears from a `sudo` or `ssh root@` run, on KDE with SDDM in particular. A plain X11
    # session's Xorg *is* root-owned, so that stays the fallback.
    found = mine or root
    if found:
        return ":%d" % min(found)
    return None


def find_xauthority(uid: int | None = None) -> str | None:
    """Cookie file for the session's X server, or None: $XAUTHORITY when it exists; the session leader's own
    XAUTHORITY (gnome-shell, Plasma's startplasma/kwin/plasmashell, xfce4-session, sway, and the
    i3/MATE/Cinnamon/LXQt/Openbox names beside them -- the only route to SDDM's /tmp/xauth_<random>); the newest
    <runtime dir>/.mutter-Xwaylandauth.* (Mutter), xauth_* (GDM) or gdm/Xauthority (GDM again, one level down);
    ~/.Xauthority of the session user."""
    p = os.environ.get("XAUTHORITY", "")
    if p and os.path.exists(p):
        return p
    if uid is None:
        uid = session_uid()
    env_p = _shell_environ(uid).get("XAUTHORITY", "")
    if env_p and os.path.exists(env_p):
        return env_p
    rd = _runtime_dir_of(uid)
    if rd:
        # GDM keeps an X11 session's cookie one directory down, at
        # <runtime dir>/gdm/Xauthority, and today it is found only through a
        # live readable gnome-shell: with the leader gone, find_xauthority(1000)
        # answered None although the file was there [M recon2/gnome-xorg.md].
        cands = (glob.glob(os.path.join(rd, ".mutter-Xwaylandauth.*"))
                 + glob.glob(os.path.join(rd, "xauth_*"))
                 + glob.glob(os.path.join(rd, "gdm", "Xauthority")))
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
        try:
            home = pwd.getpwuid(uid).pw_dir
        except KeyError:
            home = None
        if home and os.path.exists(os.path.join(home, ".Xauthority")):
            return os.path.join(home, ".Xauthority")
    return None
