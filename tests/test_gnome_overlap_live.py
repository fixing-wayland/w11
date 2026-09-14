"""`--unsafe-gnome-overlap` against a compositor that is really running.

Two halves, and only the first of them runs on a developer's machine:

* **the negative cell**, on a private headless sway booted here: the flag is a
  refusal off GNOME, before any bus call, and that is worth proving against a
  real compositor rather than a mock because the refusal is what stands between
  somebody's KDE session and a feature that has no business there;
* **the GNOME cells**, which write eight bytes into a running gnome-shell and
  are therefore opt-in twice over: `WXRANDR_LIVE_GNOME=1` in the environment,
  *and* an `org.gnome.Shell` that owns its name on the session bus.  They are
  meant for the rig (`vm/vmctl`, the noble-gnome / resolute-gnome /
  stonking-gnome goldens, run as the session user with `vmctl user`), on a
  snapshot instance where a dead session costs nothing.  With the variable
  unset every one of them skips and says which variable it wanted.

The reason for the double gate is the thing being tested.  A wrong offset here
does not fail a test, it ends the session the test is running in -- so this
file must never be able to run its writes because somebody typed
`python3 tests/test_gnome_overlap_live.py` on their own desktop.

What the GNOME cells hold down is what tests/test_gnome_overlap.py can only
model: that the checks pass on a real libmutter, that the agreement records the
build that is actually mapped, that the layout comes back, that
~/.config/monitors.xml is not touched by any of it, and that the refusal which
was measured killing a session (a forced `--dryrun`) refuses instead.  The
other measured killer -- a description naming a shared library that is not
there -- is not here: it needs a scratch extension directory under
~/.local/share/gnome-shell/extensions and a relogin for gnome-shell to scan it,
which no unittest can arrange from inside the session it would end, so it lives
in vm/live-smoke.sh (T63) instead of as a cell that can only skip.

Measured on the rig at 0.4, GNOME 46.0 / noble-gnome, package route: the six
checks pass with the shipped W11Overlap14; the first apply with no agreement
recorded APPLIED (Virtual-2 to +1000+0, screenshots show head 1 repeating head
0's pixels from x=1000); `--gnome-overlap-allow` recorded {"agreed", "format":
1, "how", "libmutter": "14", "libmutter_build": ..., "shell": "46.0",
"struct_size": 72}; a second apply at +1200+0 with a loop appending to
~/.config/monitors.xml every 50 ms did not reach the "ok:false after apply"
branch (rc 0, applied, digest unchanged) -- so that cell records whether it can
be reached at all rather than asserting that it is.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from support import HeadlessSway
from hacks.display import gnome_overlap

#: the one switch that lets anything in this file write into a compositor.
LIVE_VAR = "WXRANDR_LIVE_GNOME"
FLAG = gnome_overlap.FLAG
FORCE = gnome_overlap.FORCE_FLAG
ALLOW = gnome_overlap.ALLOW_FLAG
FORGET = gnome_overlap.FORGET_FLAG
STATUS = gnome_overlap.STATUS_FLAG


def wxrandr(*argv, env=None, timeout=120):
    """`python3 -m wxrandr ...` as a real process, against whatever session the
    environment describes."""
    e = dict(os.environ, PYTHONPATH=ROOT, W11_PASSTHROUGH="never")
    e.update(env or {})
    return subprocess.run([sys.executable, "-m", "wxrandr"] + list(argv),
                          capture_output=True, text=True, env=e, timeout=timeout)


def gnome_shell_pid():
    """The pid of the gnome-shell this user is running, or None."""
    rc = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", "gnome-shell"],
                        capture_output=True, text=True)
    pids = [int(p) for p in rc.stdout.split() if p.strip().isdigit()]
    return pids[0] if pids else None


def shell_owns_its_name():
    """Does org.gnome.Shell own its name on this session bus?

    Asked over the bus rather than by looking for a process, because that is the
    question the tool itself asks, and because a gnome-shell that is starting or
    dying is a process without a name."""
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return False
    try:
        from w11common.dbus_mini import Bus
        bus = Bus()
    except Exception:
        return False
    try:
        return bool(bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                             "org.freedesktop.DBus", "NameHasOwner", "s",
                             (gnome_overlap.SHELL_NAME,)).args()[0])
    except Exception:
        return False
    finally:
        try:
            bus.close()
        except Exception:
            pass


def gnome_reason():
    """Why the writing cells must not run here, or None when they may.

    Two separate answers on purpose: "you did not ask for this" and "this is not
    a GNOME" are different, and a skip message that says which one it is saves
    the next person the ten minutes of wondering."""
    if not os.environ.get(LIVE_VAR):
        return ("%s is not set: these cells write into a running gnome-shell "
                "and are for the rig (vm/vmctl, a GNOME golden, `vmctl user`)"
                % LIVE_VAR)
    if not shell_owns_its_name():
        return ("%s is set but org.gnome.Shell does not own its name on this "
                "session bus: nothing here will be applied" % LIVE_VAR)
    return None


def monitors_xml_digest():
    """sha256 of ~/.config/monitors.xml, or "absent"."""
    from hacks.display import monitors_xml
    path = monitors_xml.default_path()
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return "absent"


def connected(env=None):
    """[(name, x, y, w, h)] from `wxrandr --query`, in the order it printed.

    The environment is an argument and not `os.environ` because the caller that
    matters is the sway cell: asking the developer's own desktop what its heads
    are called and then typing that name at a private headless sway is how a
    "refusal" gets proved against the wrong session (measured here: with no env
    the query returned nothing at all and the assertion ran against the
    HEADLESS-1 fallback)."""
    rc = wxrandr("--query", env=env)
    out = []
    for line in rc.stdout.splitlines():
        m = re.match(r"^(\S+) connected (?:primary )?(\d+)x(\d+)\+(-?\d+)\+(-?\d+)",
                     line)
        if m:
            name, w, h, x, y = m.groups()
            out.append((name, int(x), int(y), int(w), int(h)))
    return out


# ------------------------------------------------- the negative cell (here)

@unittest.skipUnless(shutil.which("sway"), "sway not on PATH (nix develop)")
class NotAGnomeSession(unittest.TestCase):
    """A real compositor that is not GNOME, and the flag on it.

    The mock in tests/test_gnome_overlap.py proves the same refusal by handing
    the CLI a backend name; this proves it against a compositor that really
    answers, chosen by real detection, with no GNOME anywhere -- which is the
    arrangement a KDE or sway user is actually in."""

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessSway("overlap-live-sway-", need_display=False)

    @classmethod
    def tearDownClass(cls):
        cls.rig.stop()

    def env(self):
        e = dict(self.rig.env)
        e["WAYLAND_DISPLAY"] = self.rig.wayland_display()
        # no session bus at all: nothing of GNOME's can be reached even by
        # accident, which is what makes "no bus call" provable rather than
        # asserted
        e["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/no-bus" % self.rig.rtdir
        e.pop("DISPLAY", None)
        return e

    def test_the_flag_is_a_refusal_on_a_session_that_is_not_gnome(self):
        heads = [h[0] for h in connected(env=self.env())]
        self.assertTrue(heads, "the private sway reported no outputs")
        rc = wxrandr(FLAG, "--dryrun", "--output", heads[0], "--pos", "960x0",
                     env=self.env())
        self.assertEqual(rc.returncode, 1, rc.stdout + rc.stderr)
        self.assertIn("only means anything on GNOME", rc.stderr)
        self.assertIn("sway", rc.stderr)

    def test_the_ordinary_query_still_works_on_the_same_session(self):
        """The control: the refusal above is about the flag and not about a
        compositor this cannot talk to."""
        rc = wxrandr("--query", env=self.env())
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        self.assertIn("Screen 0:", rc.stdout)

    def test_the_agreement_options_have_nothing_to_agree_to_here(self):
        rc = wxrandr(ALLOW, env=self.env())
        self.assertEqual(rc.returncode, 1, rc.stdout + rc.stderr)
        self.assertIn("there is nothing to agree to", rc.stderr)
        rc = wxrandr(STATUS, env=self.env())
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        self.assertEqual(rc.stdout.splitlines()[0], "unavailable")


# ------------------------------------------------- the writing cells (rig)

@unittest.skipIf(gnome_reason(), gnome_reason() or "")
class OverlapOnRealGnome(unittest.TestCase):
    """The route on a GNOME that is really running.  Ordered by name, because
    the agreement recorded by one cell is what the next one is about."""

    @classmethod
    def setUpClass(cls):
        cls.heads = connected()
        if len(cls.heads) < 2:
            raise unittest.SkipTest("needs two heads; this session has %d"
                                    % len(cls.heads))
        cls.start_pid = gnome_shell_pid()
        cls.start_xml = monitors_xml_digest()
        cls.restore = gnome_overlap.undo_command(
            [{"connectors": [n], "x": x, "y": y} for n, x, y, _w, _h in cls.heads])

    def setUp(self):
        self.addCleanup(self.put_it_back)

    def put_it_back(self):
        """Whatever a cell did, the layout it started from comes back, and the
        session is still there to put it back into."""
        wxrandr(*self.restore.split()[1:])
        self.assertIsNotNone(gnome_shell_pid(), "gnome-shell is gone")
        self.assertEqual(gnome_shell_pid(), self.start_pid,
                         "gnome-shell was restarted during this test")
        self.assertEqual(monitors_xml_digest(), self.start_xml,
                         "~/.config/monitors.xml moved; nothing here may write it")

    def second(self):
        """The head that gets moved: the second one, and where it starts."""
        return self.heads[1]

    def moved_to(self):
        """Half on top of the first head."""
        name, _x, y, _w, _h = self.second()
        return name, self.heads[0][3] // 2, y

    # -- the cells ---------------------------------------------------------

    def test_10_the_status_says_the_route_is_available(self):
        rc = wxrandr(STATUS)
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        self.assertEqual(rc.stdout.splitlines()[0], "available")

    def test_20_the_agreement_names_the_libmutter_that_is_mapped(self):
        """The build id in the record against `readelf -n` of the file the
        running gnome-shell actually mapped.  The record is the only place that
        number is written down, and a wrong one would make every later run ask
        again for no reason -- or, worse, not ask when it should."""
        rc = wxrandr(FORGET)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        rc = wxrandr(ALLOW)
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        with open(gnome_overlap.consent_path(), encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["format"], gnome_overlap.CONSENT_FORMAT)
        self.assertIn(rec["libmutter"],
                      [g["libmutter"] for g in gnome_overlap.GENERATIONS])
        want = elf_build_id(mapped_libmutter(self.start_pid))
        if want is None:
            self.skipTest("could not read a build id out of the mapped libmutter")
        self.assertEqual(rec["libmutter_build"], want)

    def test_30_an_overlap_applies_and_the_undo_puts_it_back(self):
        """The paragraph route, on purpose: test_20 has just recorded an
        agreement, and an agreed run prints `quiet_line()` and nothing else
        (mutter.py:858) -- no undo line to run.  So the agreement goes first and
        the whole paragraph comes back, which is also the state a first-time
        user is in.

        The undo command is read out of `  To undo:              wxrandr --output
        ...` (gnome_overlap.py:386), which is one label and then a command: the
        line is cut at `wxrandr` rather than split on spaces, because the label
        ends in a colon and `wxrandr undo: ...` is a usage error."""
        rc = wxrandr(FORGET)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        name, x, y = self.moved_to()
        rc = wxrandr(FLAG, "--output", name, "--pos", "%dx%d" % (x, y))
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        where = dict((h[0], (h[1], h[2])) for h in connected())
        self.assertEqual(where[name], (x, y))
        # the undo line the tool printed is a command, and it works
        undo = [ln for ln in rc.stderr.splitlines() if "wxrandr --output" in ln]
        self.assertTrue(undo, rc.stderr)
        back = undo[-1][undo[-1].index("wxrandr"):].split()
        self.assertEqual(back[0], "wxrandr")
        self.assertEqual(subprocess.run(
            [sys.executable, "-m", "wxrandr"] + back[1:],
            env=dict(os.environ, PYTHONPATH=ROOT), timeout=120).returncode, 0)
        self.assertEqual(dict((h[0], (h[1], h[2])) for h in connected())[name],
                         (self.second()[1], self.second()[2]))

    def test_35_an_agreed_apply_says_one_line_and_still_applies(self):
        """The other half of the route: with the agreement back, the same move
        prints `as agreed on <date>` and no paragraph, and the layout still
        moves.  `self.restore` (the class's own undo, built from the heads as
        they were at setUpClass) is what puts it back, because this run prints
        no undo command of its own -- which is the point being pinned."""
        rc = wxrandr(ALLOW)
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        name, x, y = self.moved_to()
        rc = wxrandr(FLAG, "--output", name, "--pos", "%dx%d" % (x, y))
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        self.assertIn("as agreed on", rc.stderr)
        self.assertNotIn("What it risks:", rc.stderr)
        self.assertNotIn("wxrandr --output", rc.stderr)
        self.assertEqual(dict((h[0], (h[1], h[2])) for h in connected())[name],
                         (x, y))

    def test_40_forgetting_brings_the_paragraph_back(self):
        rc = wxrandr(FORGET)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertFalse(os.path.exists(gnome_overlap.consent_path()))
        name, x, y = self.moved_to()
        rc = wxrandr("--dryrun", FLAG, "--output", name, "--pos", "%dx%d" % (x, y))
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        self.assertIn("What it risks:", rc.stderr)
        self.assertIn("dryrun: nothing was written", rc.stderr)
        for check in ("shell-version", "typelib", "sentinel", "pending-dialog",
                      "bounded-read", "public-view"):
            self.assertIn("overlap check %s:" % check, rc.stderr)

    def test_50_a_forced_dryrun_is_refused_before_any_bus_call(self):
        """Measured on a real GNOME 51: the first forced run ever attempted was
        a `--dryrun`, and it ended the session -- a description picked by size
        names a library that is not there, GIRepository cannot open it, and gjs
        aborts rather than raising.  So it is refused at parse time, which is
        before anything of this reaches a bus."""
        major = max(gnome_overlap.SUPPORTED_MAJORS) + 1
        name, x, y = self.moved_to()
        rc = wxrandr("--dryrun", FLAG, FORCE, str(major),
                     "--output", name, "--pos", "%dx%d" % (x, y))
        self.assertEqual(rc.returncode, 1, rc.stdout + rc.stderr)
        self.assertIn("cannot be rehearsed with --dryrun", rc.stderr)
        self.assertIsNotNone(gnome_shell_pid())

    def test_70_whether_a_moved_monitors_xml_can_be_reached_at_all(self):
        """extension.js:1100-1108 clears `ok` when the monitors.xml digest moves
        across an apply.  The orchestrator's attempt to provoke it on GNOME 46.0
        -- a loop appending to ~/.config/monitors.xml every 50 ms under a second
        apply at +1200+0 -- did not: rc 0, applied, digest unchanged.  This cell
        tries again and records the answer rather than asserting one, because
        "unreachable" is a legitimate result and a test that demanded the branch
        fire would be demanding a race."""
        from hacks.display import monitors_xml
        path = monitors_xml.default_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        loop = subprocess.Popen(
            ["sh", "-c",
             'while :; do printf "<!-- %s -->\\n" "$(date +%%s%%N)" >> "$1"; '
             'sleep 0.05; done', "sh", path])
        self.addCleanup(self._stop, loop)
        try:
            name, x, y = self.moved_to()
            rc = wxrandr(FLAG, "--output", name, "--pos", "%dx%d" % (x, y))
        finally:
            self._stop(loop)
        shouted = "CHANGED across this call" in rc.stderr
        # whichever way it went, the session is still here and the tool said
        # something honest about the file
        self.assertIsNotNone(gnome_shell_pid())
        self.assertIn("monitors.xml", rc.stderr)
        if shouted:
            self.assertEqual(rc.returncode, 1, rc.stderr)
            self.assertNotIn("no reason given", rc.stderr)
        else:
            self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        # the file this test scribbled in is not the one the class started with
        self.start_xml = monitors_xml_digest()

    def _stop(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


#: The library the extension itself matches, and only it.  gnome-shell maps
#: libmutter-clutter-14.so.0, libmutter-cogl-14.so.0, libmutter-cogl-pango-14
#: and libmutter-mtk-14.so.0 beside libmutter-14.so.0 (Ubuntu 24.04's
#: libmutter-14-0 ships all five), /proc/<pid>/maps is ordered by address, and a
#: pattern that took any of them took whichever one ASLR had put first -- i.e.
#: compared the recorded build id against some other library's.  The alternation
#: is built from the same table the extension reads.
_MUTTER_SO = r"(/\S*/(?:%s)[^\s]*)" % "|".join(
    re.escape(g["soname"]) for g in gnome_overlap.GENERATIONS)


def mapped_libmutter(pid):
    """The libmutter file the given process has mapped, or None.

    Named exactly: `libmutter-14.so.0` and its siblings from GENERATIONS, never
    `libmutter-cogl-14.so.0`."""
    if pid is None:
        return None
    try:
        with open("/proc/%d/maps" % pid, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    for m in re.finditer(_MUTTER_SO, text):
        path = m.group(1)
        if not path.endswith("(deleted)") and os.path.exists(path):
            return path
    return None


def elf_build_id(path):
    """The GNU build id of an ELF file, as lowercase hex, or None."""
    if not path or shutil.which("readelf") is None:
        return None
    rc = subprocess.run(["readelf", "-n", path], capture_output=True, text=True)
    m = re.search(r"Build ID: ([0-9a-f]+)", rc.stdout)
    return m.group(1) if m else None


if __name__ == "__main__":
    unittest.main()
