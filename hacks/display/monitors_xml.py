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
import xml.etree.ElementTree as ET

from hacks.display.core import round_half_away, warn

#: what Mutter reads, and the copy we keep beside it.  Muffin reads
#: `cinnamon-monitors.xml` in the same directory (the string in
#: libmuffin.so.0.0.0 [M recon2/cinnamon.md §2.2]), which reaches every function
#: here as the `name` argument -- wxrandr/mutter.py's Flavor carries it.
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


def _owner(uid_path) -> tuple[int, int] | None:
    """`(uid, gid)` that own `uid_path`, or None.  A function of its own so that the
    chown branch below is reachable from a test that is not root.

    Both halves, not the uid alone: root's primary group is root's, so a copy given only
    the right uid lands as `them:root` in somebody else's `~/.config` -- readable, but
    not the ownership of the file it was copied from, which is the whole point."""
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

def snapshot(path=None, env=None, uid=None, name=NAME):
    """`(path, bytes)` of the saved configuration, or None when there is none to keep
    (a fresh GNOME install has no file at all).  Reads; never writes.  `uid`: whose
    file, `name`: which file, see `default_path()`."""
    p = path or default_path(env, uid, name)
    try:
        if os.path.getsize(p) > MAX_BYTES:
            return None
        with open(p, "rb") as f:
            return (p, f.read())
    except OSError:
        return None


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
    instead and one line says so."""
    if not snap:
        return None
    p, data = snap
    owner = _owner(p)
    backup = p + BACKUP_SUFFIX
    tmp = backup + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        if owner is not None and owner[0] != os.geteuid():
            try:
                os.chown(tmp, owner[0], owner[1])
            except OSError as e:
                os.unlink(tmp)
                warn(NO_BACKUP_NOTE % (p, owner[0], e))
                return None
        os.replace(tmp, backup)
        return backup
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
