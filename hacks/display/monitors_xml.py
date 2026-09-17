"""GNOME's saved display configuration (`~/.config/monitors.xml`): read it, judge it,
keep a copy of it.  Nothing here ever writes that file -- only Mutter does.

Mutter's writer and Mutter's reader do not agree, and the disagreement is expensive:

- The writer, `meta_monitor_config_manager_save_current()`, verifies nothing.  It
  serialises whatever configuration is current, which on GNOME-on-Xorg (and for anything
  that reaches libmutter behind DisplayConfig's back) can be a layout no validator ever
  saw.
- The reader verifies every `<configuration>` in the file with the same
  `meta_verify_logical_monitor_config_list()` the D-Bus call uses -- and one failure
  throws away *the whole file*, not the offending entry.  A file holds one entry per
  monitor set the user ever saved, so a single bad entry silently loses the lot, at
  every login, for ever.

wxrandr cannot land in that state itself: every layout we apply goes through
`ApplyMonitorsConfig`, which validates before anything is applied and long before
anything is written, and Mutter writes the file only after the user confirms its
"Keep changes?" dialog (measured on GNOME 46 and 50: nothing is written before the
confirmation, and rejected layouts leave the file byte-identical).  What we can do is
not be the tool that makes somebody else's damage permanent: a confirmed `--persistent`
apply rewrites the file from what Mutter holds in memory, which after a discarded read
is *only* the layout being applied.  So before every persistent apply wxrandr reads the
file, says so when Mutter has already discarded it, and -- once the apply is accepted --
keeps the previous bytes next to it in `monitors.xml.wxrandr-backup`.

GNOME keeps one generation of its own, `monitors.xml~` (glib writes it, as a side
effect of how Mutter replaces the file), and that is not the same thing: it is
overwritten by every save, GNOME Settings' saves included, so the copy of the file as
it stood before *this* apply is gone as soon as anything else writes one.  Ours is
written once per persistent apply and by nothing else.

Everything here is off the common path: a plain (temporary) apply never opens the file.

Whose file, though, is not always the caller's.  Run from `ssh root@box` or from cron
the process environment is root's -- `$HOME` is /root and `$XDG_CONFIG_HOME`, if it is
set at all, is root's -- while the session being reconfigured belongs to somebody else,
and Mutter reads that user's `~/.config/monitors.xml` and no other.  So `default_path()`
takes a uid, and with one that is not ours it resolves that account's `pw_dir` instead
of reading the environment.  The copy is then written into their home, and it has to
end up owned by them: a root-owned file in a user's config directory is one they cannot
replace and one their next `--persistent` cannot overwrite.  `keep_backup()` chowns it
to the owner of the file it copied, and when it cannot it removes the copy and says so
-- the one line this module prints, because it is also the only moment at which anyone
knows a backup was wanted and is not there.
"""

import os
import pwd
import stat
import xml.etree.ElementTree as ET

from hacks.display.core import round_half_away, warn

#: what Mutter reads, and the copy we keep beside it.  Muffin reads
#: `cinnamon-monitors.xml` in the same directory (the string in
#: libmuffin.so.0.0.0 [M recon2/cinnamon.md §2.2]), which reaches every function
#: here as the `name` argument -- hacks/display/mutter.py's Flavor carries it.
NAME = "monitors.xml"
BACKUP_SUFFIX = ".wxrandr-backup"
#: a saved configuration file is a few KB; anything huge is not one, and we do not slurp it
MAX_BYTES = 1 << 20
#: the copy could not be given to the account that will have to live with it, so it was
#: not left behind at all: a root-owned file in their config directory is worse than none
NO_BACKUP_NOTE = ("no copy of %s could be kept for uid %d (%s), so none was left "
                  "behind; the file itself is untouched\n")

LOGICAL, PHYSICAL = "logical", "physical"


def home_of(uid) -> str | None:
    """`pw_dir` of that account, or None when there is no such account (or no passwd
    database to ask, which is what a minimal container is)."""
    try:
        return pwd.getpwuid(uid).pw_dir or None
    except (KeyError, OSError, TypeError, OverflowError):
        return None


def _fowner(fd) -> tuple[int, int] | None:
    """`(uid, gid)` that own an open descriptor, or None.  Both halves, not the uid
    alone: root's primary group is root's, so a copy given only the right uid lands as
    `them:root` in somebody else's `~/.config` -- readable, but not the ownership of the
    file it was copied from, which is the whole point.

    A function of its own so that the branches keyed on somebody else's uid -- the
    directory check in `snapshot()` and the chown in `keep_backup()` -- are reachable
    from a test that is not root: a runner that cannot chown a file cannot make one
    really belong to another account either, so it says so by patching this
    (tests/test_wxrandr_mutter.py's `session_of`, tests/test_monitors_xml.py)."""
    try:
        st = os.fstat(fd)
    except OSError:
        return None
    return st.st_uid, st.st_gid


def _owner(uid_path) -> tuple[int, int] | None:
    """`(uid, gid)` that own `uid_path` **by name**, or None -- which is deliberately
    not what the copy is given to any more.  `os.stat` follows symlinks and answers
    about whatever the name resolves to at the moment it is asked, and the name lives in
    the directory of the account we are copying for; `snapshot()` reads the owner off
    the descriptor it opened and carries it to `keep_backup()` instead (`_fowner` above).
    Kept as the by-name answer the test that pins the difference compares against."""
    try:
        st = os.stat(uid_path)
    except OSError:
        return None
    return st.st_uid, st.st_gid


def default_path(env=None, uid=None, name=NAME) -> str:
    """`$XDG_CONFIG_HOME/<name>`, else `~/.config/<name>` -- the path Mutter
    itself builds (it never looks anywhere else in a user's home), and the path Muffin
    builds for `cinnamon-monitors.xml`.

    `uid` is the graphical session's owner (`session.session_uid()`).  When it is ours,
    or unknown, the environment is that session's own and is read as before.  When it is
    somebody else's the environment belongs to whoever ran the command -- root, over ssh
    or under sudo -- and reading `$HOME` there would name root's file, which Mutter has
    never opened and which `--persistent` would then have backed up instead of theirs."""
    env = os.environ if env is None else env
    if uid is not None and uid != os.geteuid():
        home = home_of(uid)
        if home:
            return os.path.join(home, ".config", name)
    base = env.get("XDG_CONFIG_HOME") or ""
    if not base.startswith("/"):
        base = os.path.join(env.get("HOME", ""), ".config")
    return os.path.join(base, name)


class Region:
    """One `<logicalmonitor>`: its connectors and the rectangle it claims."""

    __slots__ = ("connectors", "x", "y", "w", "h", "scale", "primary")

    def __init__(self, connectors, x, y, w, h, scale, primary):
        self.connectors, self.x, self.y = connectors, x, y
        self.w, self.h, self.scale, self.primary = w, h, scale, primary

    def rect(self, layout_mode):
        """The rectangle Mutter's verifier compares: pixels swapped for a quarter turn,
        then divided by the scale in layout-mode `logical` and left alone in `physical`."""
        w, h = self.w, self.h
        if layout_mode == LOGICAL and self.scale:
            w = round_half_away(w / self.scale)
            h = round_half_away(h / self.scale)
        return (self.x, self.y, w, h)


class Config:
    """One `<configuration>`: the monitor set it is keyed by, and its regions."""

    __slots__ = ("layout_mode", "regions")

    def __init__(self, layout_mode, regions):
        self.layout_mode, self.regions = layout_mode, regions

    @property
    def connectors(self):
        return [c for r in self.regions for c in r.connectors]


def _int(text, default=0):
    try:
        return int((text or "").strip())
    except (TypeError, ValueError):
        return default


def _float(text, default=1.0):
    try:
        v = float((text or "").strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def _region(lm):
    connectors, w, h, rotated = [], 0, 0, False
    for mon in lm.findall("monitor"):
        c = mon.findtext("monitorspec/connector")
        if c:
            connectors.append(c.strip())
        mode = mon.find("mode")
        if mode is not None and not w:
            w, h = _int(mode.findtext("width")), _int(mode.findtext("height"))
    rot = (lm.findtext("transform/rotation") or "").strip()
    rotated = rot in ("left", "right")
    if rotated:
        w, h = h, w
    return Region(connectors, _int(lm.findtext("x")), _int(lm.findtext("y")),
                  w, h, _float(lm.findtext("scale")),
                  (lm.findtext("primary") or "").strip() in ("yes", "true", "1"))


def parse(text):
    """Every `<configuration>` in the file, in order.  Raises `ET.ParseError` on XML that
    is not XML -- which Mutter's reader refuses in exactly the same way, and for which
    the same "the whole file is gone" is true."""
    root = ET.fromstring(text)
    out = []
    for cfg in root.findall("configuration"):
        mode = (cfg.findtext("layoutmode") or "").strip().lower() or None
        regions = [_region(lm) for lm in cfg.findall("logicalmonitor")]
        out.append(Config(mode if mode in (LOGICAL, PHYSICAL) else None, regions))
    return out


# -- Mutter's own verifier, on file contents ---------------------------------
# meta_verify_logical_monitor_config_list(), src/backends/meta-monitor-config-utils.c:
# no overlap, every region edge-adjacent to another (a gap is "not adjacent" too),
# and the whole layout anchored at 0,0.  Exact integer arithmetic, as there.

def _overlaps(a, b):
    return (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
            and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])


def _adjacent(a, b):
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    if (a[0] == bx2 or ax2 == b[0]) and not (ay2 <= b[1] or by2 <= a[1]):
        return True
    if (a[1] == by2 or ay2 == b[1]) and not (ax2 <= b[0] or bx2 <= a[0]):
        return True
    return False


def fault(rects):
    """Mutter's refusal for a list of (x, y, w, h) rectangles, or None: the same order
    its verifier uses, so the sentence matches the one in the journal."""
    if not rects:
        return None                      # an empty <configuration> is Mutter's business
    if len(rects) > 1:
        for r in rects:
            if not any(_adjacent(r, o) for o in rects if o is not r):
                return ("logical monitors not adjacent "
                        "(an overlap counts, and so does a gap)")
    for i, r in enumerate(rects):
        if any(_overlaps(r, o) for o in rects[:i]):
            return "logical monitors overlap"
    if min(r[0] for r in rects) != 0 or min(r[1] for r in rects) != 0:
        return "logical monitors are not anchored at 0,0"
    return None


def _fault(regions, layout_mode):
    """Mutter's refusal for this configuration under `layout_mode`, or None.

    In Mutter's order, which is measurable from the outside: adjacency first, so a
    layout that is not exactly edge-adjacent -- an overlap as much as a gap -- comes
    back "Logical monitors not adjacent", and "Logical monitors overlap" is left for a
    region that touches an edge and still lands on top of another one.
    """
    return fault([r.rect(layout_mode) for r in regions])


def problems(configs, layout_mode=None):
    """One line per `<configuration>` Mutter's reader would refuse -- which is one line
    per file, really, since the first refusal discards every other entry too.

    An entry that names its layout mode is judged in that one.  Mutter writes
    `<layoutmode>` only for `logical` (measured: GNOME 50.1 writes it, GNOME 46.0, whose
    default is `physical`, writes nothing), and an entry without one is re-read in
    whatever mode the session is in -- so it is judged in `layout_mode` when the caller
    knows it, and otherwise only reported when it is refused in *both*, a warning that
    might be wrong being worse than none.
    """
    out = []
    for i, cfg in enumerate(configs):
        modes = [cfg.layout_mode or layout_mode] if (cfg.layout_mode or layout_mode) \
            else [LOGICAL, PHYSICAL]
        faults = [_fault(cfg.regions, m) for m in modes]
        if all(faults):
            out.append("configuration %d (%s): %s"
                       % (i + 1, ", ".join(cfg.connectors) or "no monitors", faults[0]))
    return out


# -- reading it, and keeping a copy ------------------------------------------

class Snapshot:
    """What `snapshot()` read, who it came from, and the directory it was read out of.

    It was a `(path, bytes)` pair once and it is still that pair to anything that
    unpacks it -- `describe()` does, and tests/test_wxrandr_cinnamon.py:418 hands
    `describe()` a plain two-tuple of its own, which keeps working.  What a pair could
    not carry is the other half of reading through descriptors: `owner` is the
    `(uid, gid)` off the file's own `fstat`, so that `keep_backup()`'s chown goes to
    whoever the bytes actually came from rather than to whatever the name means by the
    time it is stat'ed a second time, and `dfd` is the open descriptor of the directory
    the file was read out of, so that every name `keep_backup()` touches afterwards is
    resolved relative to *that* directory instead of walking `~/.config` again.

    Holding a descriptor makes a snapshot a thing that is consumed once: `keep_backup()`
    closes it when it is done, and a caller that drops a snapshot without keeping a
    backup closes it itself.  `close()` is a no-op the second time, so both can happen
    (hacks/display/mutter.py's `apply` closes it on the path where the apply raises and
    `keep_backup()` is never reached)."""

    __slots__ = ("path", "data", "owner", "dfd")

    def __init__(self, path, data, owner, dfd):
        self.path, self.data, self.owner, self.dfd = path, data, owner, dfd

    def __getitem__(self, i):
        return (self.path, self.data)[i]    # the pair this used to be, unpacking included

    def close(self):
        if self.dfd >= 0:
            fd, self.dfd = self.dfd, -1
            try:
                os.close(fd)
            except OSError:
                pass


def snapshot(path=None, env=None, uid=None, name=NAME):
    """A `Snapshot` of the saved configuration -- `(path, bytes)` to anything that
    unpacks it -- or None when there is none to keep (a fresh GNOME install has no file
    at all) and when there is one we will not read (everything below).  Reads; never
    writes.  `uid`: whose file, `name`: which file, see
    `default_path()`.

    The directory is opened first and every name after it is relative to that
    descriptor, because whose file it is decides what the *whole* path may resolve to:
    with a `uid` that is not ours the path is inside *their* home (`default_path()`
    above) while the process doing the reading is root's, over ssh or under sudo, and
    every component from `.config` down is a name that account can replace.  `O_NOFOLLOW`
    on the file alone guards the last component only -- a symlink at `~/.config` itself,
    pointing at any directory root can traverse, still sent the read and the copy
    somewhere else entirely.  So: open the directory (following a link there is fine --
    `~/.config -> ~/dotfiles/config` is a real setup), `fstat` it, and refuse unless it
    belongs to the account whose file this is; then open the basename with `dir_fd=`,
    which cannot be re-pointed underneath us because the directory is a descriptor and
    no longer a path to be walked.

    The file itself is still judged as `hacks/display/core.py`'s `_read_state` and
    `xw11/display.py`'s `_open_regular` judge theirs: `O_NOFOLLOW` refuses a link at the
    name (ELOOP) -- root reading `/etc/shadow` and `keep_backup()` handing the bytes
    straight back in a copy chowned to whoever planted it -- the `S_ISREG` check refuses
    a directory or a device node, and `O_NONBLOCK` is there so that a FIFO left at the
    name cannot park root inside `open()` for as long as its author likes.  The size is
    bounded twice, because `fstat` only says how big the file was when it was asked and
    the owner may go on appending: once off the descriptor, and once on what actually
    arrived -- `read(MAX_BYTES + 1)`, refused if that is what came back."""
    p = path or default_path(env, uid, name)
    # whose directory it has to be: theirs when the path was built out of their passwd
    # entry (or handed to us with their uid beside it), ours when `default_path()` fell
    # back to the environment -- which, for a session whose account no longer exists, is
    # the caller's own home and not that of the uid we were given.
    want = os.geteuid()
    if uid is not None and uid != want and (path or home_of(uid)):
        want = uid
    try:
        dfd = os.open(os.path.dirname(p) or ".", os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return None
    fd = None
    try:
        # O_DIRECTORY has already refused everything that is not a directory; whose
        # directory it is is what is left to check, and it is checked on the descriptor.
        downer = _fowner(dfd)
        if not stat.S_ISDIR(os.fstat(dfd).st_mode) or downer is None or downer[0] != want:
            return None
        fd = os.open(os.path.basename(p), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=dfd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None
        owner = _fowner(fd)
        with os.fdopen(fd, "rb") as f:      # the descriptor is the file's, closed by the with
            fd = None
            data = f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            return None                     # it grew after the fstat: the same refusal
        kept, dfd = Snapshot(p, data, owner, dfd), None     # the snapshot holds it now
        return kept
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)
        if dfd is not None:
            os.close(dfd)


def describe(snap, layout_mode=None, desktop="GNOME", compositor="Mutter"):
    """The warnings to print before a persistent apply, given `snapshot()`'s result:
    what the compositor has already thrown away, and what this apply is about to replace.
    `layout_mode` is the session's own, for the entries that do not name one; `desktop`
    and `compositor` are who threw it away, which on a Cinnamon session is Cinnamon and
    Muffin and not GNOME and Mutter (muffin carries Mutter's reader and Mutter's
    all-or-nothing rule with it [M recon2/cinnamon.md §2.2]).  The second name is not
    decoration: the whole point of the sentence is that one reader takes the file or
    leaves it, so the sentence has to say whose reader."""
    if not snap:
        return []
    p, data = snap
    try:
        bad = problems(parse(data.decode("utf-8", "replace")), layout_mode)
    except ET.ParseError as e:
        bad = ["the file is not valid XML (%s)" % e]
    if not bad:
        return []
    return ["%s has already discarded %s -- %s -- so every layout saved in it is "
            "inactive; %s's reader drops the whole file, not the one bad entry\n"
            % (desktop, p, bad[0], compositor)]


def keep_backup(snap):
    """Write the bytes read before the apply to `<path>.wxrandr-backup` and return that
    path (None when there was no file, or when the copy cannot be kept -- a backup is a
    courtesy and never a reason to fail an apply that Mutter has already accepted).

    The copy is given to whoever owns the file it was copied from, user and group both, so
    that it matches that file rather than picking up the caller's own primary group.  That
    matters only when the two accounts differ: root reconfiguring somebody else's session
    would otherwise leave a root-owned file in their `~/.config`, which they cannot
    replace and which their own next `--persistent` cannot overwrite either.  A chown we
    are not allowed to make means the copy would be exactly that, so it is removed
    instead and one line says so.

    Nothing here is done by path.  Every name is a basename resolved against the
    directory descriptor `snapshot()` opened and is still holding (`Snapshot.dfd`), so
    the directory this writes into is the one the bytes were read out of and cannot have
    become another one in between -- a symlink swapped in at `~/.config` after the read
    moves nothing, because there is no `~/.config` left to walk.

    The temp is created through a descriptor for the reason the chown exists at all: the
    directory is the session user's and the process is root's, and the temp's name --
    `<path>.wxrandr-backup.tmp` -- is fixed, so they can put a symlink at it before we
    ever get there.  A plain `open(tmp, "wb")` would truncate and fill whatever that link
    named -- `/etc/cron.d/w11` -- and the chown would then hand that file to them, which
    is root.  So: `O_NOFOLLOW` to refuse the link, `O_EXCL` so that a name already taken
    is a refusal and not a write, and a stale temp of ours unlinked and re-created rather
    than opened, exactly as `core.State.save` does in hacks/display/core.py (`unlink`
    removes the link, never its target).  0o666 under the umask is the mode
    `open(tmp, "wb")` produced, so the copy is left no more readable than it has always
    been.  The chown goes to the descriptor -- `os.fchown` rather than
    `os.chown(tmp, ...)` -- and to the owner `snapshot()` read off the file's own
    `fstat`, never to a second `os.stat` of the name: between the read and here the name
    can have become a symlink to anybody's file, and that file's uid is not who the copy
    belongs to.  `os.replace(tmp, backup, src_dir_fd=..., dst_dir_fd=...)` needs no such
    care beyond the descriptors: rename acts on the name, so a symlink sitting at
    `<path>.wxrandr-backup` is replaced by our file and is never written through.

    The snapshot is consumed: its directory descriptor is closed on the way out, whether
    a copy was kept or not."""
    if not snap:
        return None
    p, data, owner, dfd = snap.path, snap.data, snap.owner, snap.dfd
    if dfd < 0:
        return None                 # a snapshot somebody already consumed; nothing to write into
    backup = p + BACKUP_SUFFIX
    name = os.path.basename(backup)
    tmp = name + ".tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    note = None
    try:
        try:
            fd = os.open(tmp, flags, 0o666, dir_fd=dfd)
        except FileExistsError:
            os.unlink(tmp, dir_fd=dfd)      # removes a symlink, not its target
            fd = os.open(tmp, flags, 0o666, dir_fd=dfd)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            if owner is not None and owner[0] != os.geteuid():
                try:
                    os.fchown(fd, owner[0], owner[1])
                except OSError as e:
                    note = NO_BACKUP_NOTE % (p, owner[0], e)
        finally:
            os.close(fd)            # closed before the rename, and before the unlink below
        if note is not None:
            os.unlink(tmp, dir_fd=dfd)
            warn(note)
            return None
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        return backup
    except OSError:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        return None
    finally:
        snap.close()
