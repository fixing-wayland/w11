"""`--unsafe-gnome-overlap`: every refusal, the version gate, the exact warning,
and the proof that no other code path can reach the applying one.

The dangerous half of this feature is eight bytes written inside gnome-shell,
and it needs a GNOME session to run at all; everything *around* it does not, and
that is what is here.  The mock org.gnome.Mutter.DisplayConfig from
tests/test_wxrandr_mutter.py is extended with a mock org.gnome.Shell (for the
ShellVersion property the version gate reads) and a mock
org.w11.Overlap, so a whole `wxrandr --unsafe-gnome-overlap ...`
invocation runs end to end on a bus in this process, and every refusal can be
demanded rather than argued.

The rules the extension applies are in gnome/w11-overlap@w11/
rules.js, deliberately in a file with no `gi` imports so that plain node can run
it: `RulesJS` checks it against wxrandr/monitors_xml.py, which is the same rule
written twice, and skips itself where there is no node.

Four things this file is here to hold still:

* the extension is never contacted for a layout GNOME would accept, whatever
  flags were typed;
* nothing but the flag reaches it -- no environment variable, no default, no
  other backend, and not `--persistent`;
* the warning is printed in full, before the call, every time -- these tests
  record no agreement, and tests/test_overlap_consent.py is what happens when
  one is recorded; and
* the shipped type description cannot name Mutter's monitors.xml writer, and
  the apply method is a constant.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from w11common import dbus_mini
from w11common.dbus_mini import Bus, Message, Variant
import test_wxrandr_mutter as twm
from wxrandr import cli, gnome_overlap, monitors_xml, mutter

os.environ["W11_PASSTHROUGH"] = "never"

ERR = dbus_mini.ERR
EXT_DIR = os.path.join(ROOT, "gnome", "w11-overlap@w11")
GIR_DIR = os.path.join(ROOT, "gnome", "overlap-typelib")
FLAG = gnome_overlap.FLAG

# The whole of it, byte for byte.  This is the one thing a user gets before the
# session is put at risk, so it is a golden and not a set of `assertIn`s: a
# sentence that quietly stops being printed is a regression, and so is one that
# stops saying how to get back.
WARNING = """\
xrandr: --unsafe-gnome-overlap: GNOME will not place these monitors, so they are going to be
  placed by writing into the running gnome-shell instead of asking it.
  What it does:         move Virtual-2 from +1920+0 to +960+0
                        by writing 8 bytes per monitor inside gnome-shell
                        (GNOME Shell 50.1), through the w11-overlap@w11
                        extension, and then asking Mutter to apply the result.
  What it risks:        those 8 bytes go where this build of libmutter keeps a
                        logical monitor's position.  The extension re-checks
                        that this really is such a build before every write --
                        struct size against the GType registry, a sentinel
                        through Mutter's own setter, and every value it reads
                        against what Mutter reports publicly -- and refuses if
                        anything disagrees.  If all of that is wrong anyway,
                        gnome-shell crashes, and on Wayland gnome-shell is the
                        session: every program running in it goes with it.
  What it saves:        nothing.  ~/.config/monitors.xml is never written on
                        this path, so this layout does not survive a logout,
                        and --persistent is refused together with this flag.
  To undo:              wxrandr --output Virtual-1 --pos 0x0 --output Virtual-2 --pos 1920x0
                        or log out and back in.
  If the session dies:  gnome-shell is the session on Wayland, so you land at
                        the login screen.  Log in again -- nothing was saved,
                        so the layout is the one you started with.
                        If a session will not start at all, switch to a text
                        console with Ctrl+Alt+F3, log in and run
                            gnome-extensions disable w11-overlap@w11
                        (or delete whichever of these two is there:
                            ~/.local/share/gnome-shell/extensions/w11-overlap@w11
                            /usr/share/gnome-shell/extensions/w11-overlap@w11   <- from the .deb ),
                        then Ctrl+Alt+F1 back to the login screen.
"""


# ---------------------------------------------------------------- the mocks

#: two sha256 digests of ~/.config/monitors.xml, before and after one apply.
#: Only their difference matters; they are full-length because the message
#: prints them and a truncated digest in a bug report is not one.
SAVED_BEFORE = "9d4f0e8c" + "1" * 56
SAVED_AFTER = "7a2b6f31" + "2" * 56


class FakeOverlap:
    """The extension, as a bus name that answers Probe and ApplyOverlap.

    `present` False means the name is not on the bus at all -- the common case
    of "installed but the user has not logged out yet", and the one the tool
    has to explain rather than crash on.
    """

    def __init__(self, shell="50.1", present=True):
        self.shell = shell
        self.present = present
        self.calls = []                  # [(member, request dict)]
        self.reply = None                # override the canned answer
        self.error = None                # answer with a D-Bus error instead
        # the build the answer claims to have measured -- what an agreement is
        # recorded against (tests/test_overlap_consent.py)
        self.libmutter = 18
        self.instance_size = 80          # the field; None: an extension too old to say
        self.declared_size = 80          # what the typelib check's own detail reports
        self.strip_typelib_check = False  # ...an extension too old for either
        # the build of libmutter the checks ran against, and anything the
        # extension noticed that decides nothing (see gnome_overlap.notes_text)
        self.build_id = "0f3a1b2c3d4e5f60718293a4b5c6d7e8f9012345"
        self.notes = []
        # what the version gate measures before it can refuse, and what a
        # refusal on an unmeasured build hands back for the maintainer message
        self.sonames = ["libmutter-18.so.0"]
        self.meta_typelib = "18"
        # The one reply shape that says both things at once: the write went in
        # and the answer is not ok.  extension.js:1100-1108 digests
        # ~/.config/monitors.xml before and after the apply and clears `ok` when
        # the two differ, leaving `applied: true` and no `check`/`reason` at all
        # -- so it is set here as an attribute rather than assembled by hand in
        # a test, because a hand-written dict drifts away from the extension and
        # then the test proves nothing.
        self.saved_config_moved = False

    def found(self):
        """The public measurements the real extension carries out with a
        refusal: version strings, the libmutter mapped, and the size the GType
        registry reports.  Nothing private is read to produce them, which is why
        they exist on a build it has just refused to write to."""
        return {"shell": self.shell,
                "shell_major": gnome_overlap.shell_major(self.shell),
                "sonames": list(self.sonames),
                "meta_typelib": self.meta_typelib,
                "instance_size": self.instance_size,
                "known": gnome_overlap.describe_table()}

    def _refuse(self, check, reason, forceable):
        return {"ok": False, "version": 1, "check": check, "reason": reason,
                "forceable": forceable, "found": self.found(), "checks": []}

    def answer(self, member, req):
        self.calls.append((member, req))
        if self.reply is not None:
            return self.reply
        # The version gate, modelled: this is the one refusal forcing can get
        # past, and the fake has to reproduce it or the forced path is never
        # exercised end to end.
        forced = None
        if gnome_overlap.generation_for(self.shell) is None:
            force = req.get("force")
            no = (gnome_overlap.force_reason(force, self.shell) if force
                  else "not forced")
            if no:
                return self._refuse(
                    "shell-version",
                    "GNOME Shell %s: this extension knows the private layout of "
                    "GNOME %s only"
                    % (self.shell,
                       " and ".join(str(m) for m in
                                    gnome_overlap.SUPPORTED_MAJORS)), True)
            picked = [g for g in gnome_overlap.GENERATIONS
                      if g["struct_size"] == self.instance_size]
            # the same rule as rules.js selectByStructSize: records of one size
            # that agree on `tail_slots` describe the same bytes under two
            # names, so the newest of them is a selection rather than a guess;
            # records that disagree cannot both be right and are a refusal.
            shapes = {g["tail_slots"] for g in picked}
            if not picked or len(shapes) > 1:
                return self._refuse(
                    "struct-size",
                    "this build's MetaMonitorsConfig is %s bytes and no "
                    "description shipped here describes a struct that size"
                    % self.instance_size, False)
            use = picked[-1]
            same = ([g["namespace"] for g in picked[:-1]])
            forced = {"shell_major": gnome_overlap.shell_major(self.shell),
                      "using": use["namespace"],
                      "because": "MetaMonitorsConfig is %s bytes here, which is "
                                 "the size %s describes (measured on GNOME %s%s)"
                                 % (self.instance_size, use["namespace"],
                                    use["shell_major"],
                                    (", and the same bytes as %s" % ", ".join(same))
                                    if same else "")}
        monitors = [dict(g, w=1920, h=1080, scale=1, transform=0,
                         primary=(g["x"] == 0))
                    for g in (req.get("want") or req.get("expect") or [])]
        out = {
            "ok": True, "version": 1, "shell": self.shell,
            "libmutter": self.libmutter, "instance_size": self.instance_size,
            "forced": forced, "found": self.found(),
            "checks": [{"name": "shell-version", "ok": True,
                        "detail": "GNOME Shell %s, libmutter-%s"
                                  % (self.shell, self.libmutter)},
                       {"name": "typelib", "ok": True,
                        "detail": "W11Overlap%s, MetaMonitorsConfig %s bytes as declared"
                                  % (self.libmutter, self.declared_size)},
                       {"name": "sentinel", "ok": True,
                        "detail": "switch_config round-tripped at the declared offset"},
                       {"name": "pending-dialog", "ok": True,
                        "detail": 'nothing holds a modal grab, so GNOME is not '
                                  'asking "Keep changes?"'},
                       {"name": "bounded-read", "ok": True,
                        "detail": "2 logical monitors, every address range-checked"},
                       {"name": "public-view", "ok": True,
                        "detail": "identical to Mutter's public view"}],
            "monitors": monitors,
            "libmutter_build": self.build_id,
            "notes": list(self.notes),
            "wrote": member == "ApplyOverlap",
        }
        if member == "ApplyOverlap":
            out.update({
                "applied": True, "wrote_words": 2,
                "fault": "logical monitors overlap",
                "verify": "refused: Logical monitors not adjacent",
                "saved_config": {"path": "/home/u/.config/monitors.xml",
                                 "before": "absent", "after": "absent",
                                 "unchanged": True},
            })
            if self.saved_config_moved:
                out["saved_config"].update(
                    {"before": SAVED_BEFORE, "after": SAVED_AFTER, "unchanged": False})
                out["ok"] = False
        if self.instance_size is None:
            out.pop("instance_size")
        if self.strip_typelib_check:
            out["checks"] = [c for c in out["checks"] if c["name"] != "typelib"]
        return out


class _Conn(twm._MutterConn):
    """The Mutter mock's connection, plus org.gnome.Shell's ShellVersion
    property and the overlap extension."""

    SERVED = (gnome_overlap.SHELL_NAME, gnome_overlap.BUS_NAME)

    def dispatch(self, m):
        # the base mock routes by a fixed list of destinations; ours are two more
        if (m.type == dbus_mini.METHOD_CALL and m.destination in self.SERVED
                and m.interface != "org.freedesktop.DBus.Peer"):
            self._test_method(m)
            return
        super().dispatch(m)

    def _bus_method(self, m):
        ov = getattr(self.bus, "overlap", None)
        if m.member == "NameHasOwner":
            name = m.args()[0]
            if name == gnome_overlap.BUS_NAME:
                self.send(Message.method_return(
                    m, "b", (ov is not None and ov.present,)))
                return
            if name == gnome_overlap.SHELL_NAME:
                self.send(Message.method_return(m, "b", (ov is not None,)))
                return
        super()._bus_method(m)

    def _test_method(self, m):
        ov = getattr(self.bus, "overlap", None)
        if m.destination == gnome_overlap.SHELL_NAME:
            if ov is None:
                self.send(Message.error(m, ERR + "ServiceUnknown", "no shell"))
            elif m.interface == dbus_mini.PROPS_IFACE and m.member == "Get":
                _iface, name = m.args()
                if name == "ShellVersion" and ov.shell is not None:
                    self.send(Message.method_return(
                        m, "v", (Variant("s", ov.shell),)))
                else:
                    self.send(Message.error(m, ERR + "InvalidArgs", "no such property"))
            else:
                self.send(Message.error(m, ERR + "UnknownMethod", "no %s" % m.member))
            return
        if m.destination == gnome_overlap.BUS_NAME:
            if ov is None or not ov.present:
                self.send(Message.error(m, ERR + "ServiceUnknown", "not running"))
                return
            req = json.loads(m.args()[0])
            if ov.error is not None:
                ov.calls.append((m.member, req))
                self.send(Message.error(m, ERR + "Failed", ov.error))
                return
            reply = ov.answer(m.member, req)
            # a str is sent as-is, so a test can hand the client something that
            # is not JSON at all
            self.send(Message.method_return(
                m, "s", (reply if isinstance(reply, str) else json.dumps(reply),)))
            return
        super()._test_method(m)


class _MockBus(twm.MutterMockBus):
    def __init__(self, **kw):
        self.overlap = None
        super().__init__(**kw)

    def _accept(self):
        while True:
            try:
                s, _ = self.srv.accept()
            except OSError:
                return
            c = _Conn(self, s)
            with self.lock:
                self.conns.append(c)
            threading.Thread(target=c.serve, daemon=True).start()


VIRTUALS = {
    "Virtual-1": (("Virtual-1", "QEMU", "Virtual", "0"),
                  [twm.M("1920x1080@60.000", 1920, 1080, 60.0, [1.0], preferred=True)],
                  {"width-mm": 508, "height-mm": 286, "is-builtin": False,
                   "display-name": "Virtual 1"}),
    "Virtual-2": (("Virtual-2", "QEMU", "Virtual", "0"),
                  [twm.M("1920x1080@60.000", 1920, 1080, 60.0, [1.0], preferred=True),
                   twm.M("1280x720@60.000", 1280, 720, 60.0, [1.0])],
                  {"width-mm": 508, "height-mm": 286, "is-builtin": False,
                   "display-name": "Virtual 2"}),
    # a third head, so a layout can have more groups than the two the extension
    # was first written against, and so an undo command has three stanzas
    "Virtual-3": (("Virtual-3", "QEMU", "Virtual", "0"),
                  [twm.M("1920x1080@60.000", 1920, 1080, 60.0, [1.0], preferred=True)],
                  {"width-mm": 508, "height-mm": 286, "is-builtin": False,
                   "display-name": "Virtual 3"}),
    # the scaled one: the same panel, offering 2.0 as well.  A scale changes the
    # logical size a position is measured against, and that arithmetic happens
    # out here rather than in the extension -- which is handed positions only.
    "Virtual-S": (("Virtual-S", "QEMU", "Virtual", "0"),
                  [twm.M("1920x1080@60.000", 1920, 1080, 60.0, [1.0, 2.0],
                         preferred=True)],
                  {"width-mm": 508, "height-mm": 286, "is-builtin": False,
                   "display-name": "Virtual S"}),
}


# FakeMutter looks its connectors up by name in the shared table
twm.MONITORS.update(VIRTUALS)


def two_virtuals():
    return twm.FakeMutter(
        ["Virtual-1", "Virtual-2"],
        [(0, 0, 1.0, 0, True, [("Virtual-1", "1920x1080@60.000")]),
         (1920, 0, 1.0, 0, False, [("Virtual-2", "1920x1080@60.000")])],
        layout_mode=1)


def three_virtuals():
    """Three 1920x1080 heads in a row, 1:1: 0, 1920, 3840."""
    return twm.FakeMutter(
        ["Virtual-1", "Virtual-2", "Virtual-3"],
        [(0, 0, 1.0, 0, True, [("Virtual-1", "1920x1080@60.000")]),
         (1920, 0, 1.0, 0, False, [("Virtual-2", "1920x1080@60.000")]),
         (3840, 0, 1.0, 0, False, [("Virtual-3", "1920x1080@60.000")])],
        layout_mode=1)


def mixed_virtuals(layout_mode=1):
    """Virtual-1 at 1:1 and Virtual-S at scale 2 beside it.

    In `layout_mode` 1 (logical, GNOME's default with fractional scaling on)
    Virtual-S is 960x540 of layout space and sits at 1920..2880; in mode 2
    (physical) the same monitor is 1920x1080 of it and sits at 1920..3840.  The
    extension is handed positions and nothing else, so which of those is true
    is decided out here -- and getting it wrong would offer the overlap route
    for a layout Mutter accepts, or the other way round."""
    return twm.FakeMutter(
        ["Virtual-1", "Virtual-S"],
        [(0, 0, 1.0, 0, True, [("Virtual-1", "1920x1080@60.000")]),
         (1920, 0, 2.0, 0, False, [("Virtual-S", "1920x1080@60.000")])],
        layout_mode=layout_mode)


class Case(unittest.TestCase):
    """A GNOME session with two 1920x1080 heads side by side, an overlap
    extension on the bus, and a shell that says it is 50.1."""

    @classmethod
    def setUpClass(cls):
        cls.mock = _MockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-overlap-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_path = os.path.join(self.tmp, "state.json")
        self.opened = []
        self.mock.mutter = two_virtuals()
        self.mock.overlap = FakeOverlap()
        self._xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp
        self.addCleanup(self._restore)

    def _restore(self):
        if self._xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._xdg

    def tearDown(self):
        for mo in self.opened:
            mo.close()
        self.mock.mutter = None
        self.mock.overlap = None

    def outputs(self, backend="mutter"):
        mo = mutter.MutterOutputs(bus=Bus(self.mock.address), wl_socket=False)
        self.opened.append(mo)
        return mo

    def run_cli(self, *argv, backend="mutter", env=None):
        tc = self

        def fake_init(sess, forced=None):
            sess.backend = cli.canonical_backend(forced) or backend
            sess.impl = tc.outputs()
            sess.persistent = os.environ.get("WXRANDR_PERSIST", "") not in ("", "0")
            sess.state = core_state(tc.state_path)
        env = env or {}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        orig = cli.Session.__init__
        cli.Session.__init__ = fake_init
        out, err = io.StringIO(), io.StringIO()
        try:
            with _redirect(out, err):
                try:
                    code = cli.main(list(argv))
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 0
        finally:
            cli.Session.__init__ = orig
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, out.getvalue(), err.getvalue()

    # the two questions every test in here asks about a run
    def ext_calls(self):
        return [c[0] for c in self.mock.overlap.calls]

    def applied(self):
        return [c for c in self.mock.mutter.calls if c[1] != 0]


def core_state(path):
    from wxrandr.core import State
    return State("overlap-test", path=path)


def identifiers_in_gir(ns):
    """Every `c:identifier` in one shipped .gir: the C symbols the description
    makes callable, which is the list the compiled typelib has to carry and
    nothing more."""
    text = open(os.path.join(GIR_DIR, "%s-1.0.gir" % ns), encoding="utf-8").read()
    return re.findall(r'c:identifier="([^"]+)"', text)


def load_gen_gir():
    """gnome/overlap-typelib/gen-gir.py as a module.  It has a hyphen in its
    name and lives outside any package, so it is loaded by path -- three test
    classes here need what is in it and none of them may have its own copy of
    the rules."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gen_gir", os.path.join(GIR_DIR, "gen-gir.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _redirect:
    def __init__(self, out, err):
        self.out, self.err = out, err

    def __enter__(self):
        self._o, self._e = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self.out, self.err

    def __exit__(self, *a):
        sys.stdout, sys.stderr = self._o, self._e
        return False


# ---------------------------------------------------------------- pure logic

class VersionGate(unittest.TestCase):
    def test_the_measured_releases_are_the_only_ones(self):
        """Every major in the table, and the two spellings a shell uses -- the
        bare major and a point release of it.  Written down as "46 and 50" once,
        this test went on passing about two releases while a third was being
        added, which is the kind of half-edit the table exists to prevent."""
        for major in gnome_overlap.SUPPORTED_MAJORS:
            for v in (str(major), "%d.0" % major, "%d.2" % major):
                self.assertIsNone(gnome_overlap.unsupported_reason(v), v)

    def test_everything_else_is_refused_by_name(self):
        """...and the refusal lists every measured major, not a prefix of them.

        `assertIn("46 and 50", why)` passed vacuously the day GNOME 51 was
        added, because the sentence had become "46 and 50 and 51" and the old
        pin is a substring of it.  The list is built from the table here, so it
        cannot go stale in that direction."""
        listed = " and ".join(str(m) for m in gnome_overlap.SUPPORTED_MAJORS)
        below = min(gnome_overlap.SUPPORTED_MAJORS) - 1
        above = max(gnome_overlap.SUPPORTED_MAJORS) + 1
        unmeasured = ["%d.9" % below, "3.38.5", "%d.0" % above]
        unmeasured += ["%d.1" % m for m in range(below + 1, above)
                       if m not in gnome_overlap.SUPPORTED_MAJORS]
        for v in unmeasured:
            why = gnome_overlap.unsupported_reason(v)
            self.assertIsNotNone(why, v)
            self.assertIn(v, why)
            self.assertIn(listed, why)
            for major in gnome_overlap.SUPPORTED_MAJORS:
                self.assertIn(str(major), why)

    def test_an_unreadable_version_is_refused_not_guessed(self):
        for v in (None, "", "banana", "  ", object()):
            self.assertIn("cannot tell", gnome_overlap.unsupported_reason(v))

    def test_the_gate_matches_what_the_extension_declares(self):
        meta = json.load(open(os.path.join(EXT_DIR, "metadata.json")))
        self.assertEqual(sorted(int(v) for v in meta["shell-version"]),
                         sorted(gnome_overlap.SUPPORTED_MAJORS))


class TheGnome49Record(unittest.TestCase):
    """GNOME 49 / libmutter-17, added by following docs/Technical.md section 6
    on the fedora43-gnome golden on 2026-09-11.

    What was measured there, in order: `GObject.type_query(MetaMonitorsConfig)
    .instance_size` **80** out of that build's own GType registry (GNOME Shell
    49.9, mutter 49.7-1.fc43, libmutter-17.so.0 build c7c9add6935d, Meta typelib
    17, three heads); then, with this record in the table and the generated
    W11Overlap17 loaded, all six checks green -- `sentinel: switch_config
    round-tripped at the declared offset`, `bounded-read: 3 logical monitors`,
    `public-view: identical to Mutter's public view` -- an overlap applied at
    +1000+0 with the shared 920 columns byte-identical between the two heads
    (sha256 ecf41c1210e8c94d, 0 of 993600 pixels differing), and the whole
    overlap phase of vm/live-smoke.d/gnome.sh green at 16 pass, 0 fail.

    The record is written twice because the extension and wxrandr are installed
    in different places and neither can read the other's files; the field for
    field comparison of the two whole tables is
    tests/test_overlap_force.py's `test_the_two_copies_are_one_table`.  What is
    here is the 49 row itself, which that comparison would happily agree about
    if both copies had it wrong."""

    RECORD = {"shell_major": 49,
              "libmutter": "17",
              "soname": "libmutter-17.so.0",
              "meta_typelib": "17",
              "namespace": "W11Overlap17",
              "struct_size": 80,
              "tail_slots": 3}

    def json_record(self):
        with open(os.path.join(EXT_DIR, "generations.json"), encoding="utf-8") as fh:
            table = json.load(fh)
        got = [g for g in table["generations"] if g["shell_major"] == 49]
        self.assertEqual(len(got), 1, "generations.json has no GNOME 49 record")
        return got[0]

    def test_both_tables_carry_it_with_the_numbers_read_off_gnome_49(self):
        for name, rec in (("generations.json", self.json_record()),
                          ("GENERATIONS", gnome_overlap.generation_for("49.9"))):
            self.assertIsNotNone(rec, name)
            for field, want in sorted(self.RECORD.items()):
                self.assertEqual(rec[field], want, "%s: %s" % (name, field))

    def test_it_says_where_it_was_measured(self):
        """`measured_on` is the field that makes a claim in this table traceable
        to a machine somebody ran it on, and a record copied from its neighbour
        is exactly what it is there to catch: 49's layout is 50's, so the two
        rows differ only in their names and in this."""
        for rec in (self.json_record(), gnome_overlap.generation_for("49")):
            self.assertIn("49.9", rec["measured_on"])
            self.assertIn("Fedora 43", rec["measured_on"])
            self.assertIn("c7c9add6935d", rec["measured_on"])

    def test_the_gate_lets_gnome_49_through_and_the_extension_declares_it(self):
        for v in ("49", "49.0", "49.9"):
            self.assertIsNone(gnome_overlap.unsupported_reason(v), v)
        with open(os.path.join(EXT_DIR, "metadata.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        # gnome-shell will not LOAD an extension that does not name the running
        # major, so a record with no entry here is a record no 49 can reach.
        self.assertIn("49", meta["shell-version"])

    def test_the_description_is_shipped_and_named_by_the_record(self):
        """One compiled description per record, and 49's is not a spelling of
        50's: the namespace is what `install-overlap.sh` looks for and what the
        extension loads by name."""
        self.assertTrue(os.path.exists(os.path.join(
            EXT_DIR, "typelib", "W11Overlap17-1.0.typelib")))
        self.assertTrue(os.path.exists(os.path.join(
            GIR_DIR, "W11Overlap17-1.0.gir")))


class Shapes(unittest.TestCase):
    PLAN = [{"x": 0, "y": 0, "scale": 1.0, "transform": 0, "primary": True,
             "members": [("Virtual-1", "m1", False)]},
            {"x": 960, "y": 0, "scale": 1.0, "transform": 0, "primary": False,
             "members": [("Virtual-3", "m3", False), ("Virtual-2", "m2", False)]}]

    def test_groups_are_sorted_and_mirrors_stay_together(self):
        self.assertEqual(gnome_overlap.groups(self.PLAN),
                         [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                          {"connectors": ["Virtual-2", "Virtual-3"], "x": 960, "y": 0}])

    def test_rects_need_a_size_for_every_group(self):
        dims = {"Virtual-1": (1920, 1080), "Virtual-3": (1920, 1080)}
        self.assertEqual(gnome_overlap.rects(self.PLAN, dims),
                         [(0, 0, 1920, 1080), (960, 0, 1920, 1080)])
        self.assertIsNone(gnome_overlap.rects(self.PLAN, {"Virtual-1": (1, 1)}))

    def test_moves_text_names_only_what_moves(self):
        before = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                  {"connectors": ["Virtual-2"], "x": 1920, "y": 0}]
        after = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                 {"connectors": ["Virtual-2"], "x": 960, "y": 0}]
        self.assertEqual(gnome_overlap.moves_text(before, after),
                         ["Virtual-2 from +1920+0 to +960+0"])
        self.assertEqual(gnome_overlap.moves_text(before, before), [])

    def test_the_undo_command_is_the_layout_that_is_running_now(self):
        self.assertEqual(
            gnome_overlap.undo_command(
                [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                 {"connectors": ["Virtual-2", "Virtual-3"], "x": 1920, "y": 0}]),
            "wxrandr --output Virtual-1 --pos 0x0 --output Virtual-2 --pos 1920x0")

    def test_only_positions_differ(self):
        current = mutter._canon(self.PLAN)
        moved = [dict(self.PLAN[0]), dict(self.PLAN[1], x=480)]
        self.assertTrue(gnome_overlap.only_positions_differ(moved, current))
        for change in ({"scale": 2.0}, {"transform": 1}, {"primary": True},
                       {"members": [("Virtual-2", "other", False)]}):
            other = [dict(self.PLAN[0]), dict(self.PLAN[1], **change)]
            self.assertFalse(gnome_overlap.only_positions_differ(other, current),
                             change)

    def test_a_dropped_monitor_is_not_only_a_position(self):
        self.assertFalse(gnome_overlap.only_positions_differ(
            [self.PLAN[0]], mutter._canon(self.PLAN)))


class Messages(unittest.TestCase):
    def test_the_warning_is_this(self):
        expect = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                  {"connectors": ["Virtual-2"], "x": 1920, "y": 0}]
        want = [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                {"connectors": ["Virtual-2"], "x": 960, "y": 0}]
        self.assertEqual(
            gnome_overlap.warning("50.1", gnome_overlap.moves_text(expect, want),
                                  gnome_overlap.undo_command(expect)),
            WARNING)

    def test_the_warning_survives_a_layout_where_nothing_moves(self):
        # it is never printed in that case (the backend says "nothing to do"
        # first), but a message that can raise IndexError is not a message
        text = gnome_overlap.warning("50.1", [], "wxrandr ...")
        self.assertIn("move nothing (no monitor changes position)", text)

    def test_the_warning_names_every_move(self):
        text = gnome_overlap.warning(
            "46.0", ["A from +0+0 to +10+0", "B from +100+0 to +5+0"], "wxrandr ...")
        self.assertIn("move A from +0+0 to +10+0", text)
        self.assertIn("move B from +100+0 to +5+0", text)

    def test_a_refusal_names_the_check_that_refused(self):
        self.assertEqual(
            gnome_overlap.refusal_text({"ok": False, "check": "struct-size",
                                        "reason": "80 != 72"}),
            "the overlap extension refused (struct-size): 80 != 72\n")

    def test_an_answer_with_no_reason_still_reads(self):
        self.assertIn("no reason given", gnome_overlap.refusal_text({"ok": False}))

    def test_the_saved_file_is_reported_not_assumed(self):
        text = gnome_overlap.applied_text(
            {"verify": "refused: Logical monitors not adjacent",
             "saved_config": {"path": "/p/monitors.xml", "before": "abc",
                              "after": "abc", "unchanged": True}})
        self.assertIn("mutter's own validator on the result: refused: "
                      "Logical monitors not adjacent\n", text)
        self.assertIn("/p/monitors.xml: unchanged (abc)\n", text)

    def test_a_changed_saved_file_is_shouted_about(self):
        text = gnome_overlap.applied_text(
            {"saved_config": {"path": "/p/monitors.xml", "before": "abc",
                              "after": "def", "unchanged": False}})
        self.assertIn("CHANGED across this call (abc -> def)", text)
        self.assertIn("should be impossible", text)

    def test_a_note_is_relayed_and_an_empty_one_is_nothing(self):
        """Notes decide nothing -- they are what the extension noticed and the
        user could not.  Today there is one: libmutter replaced on disk under a
        live session, which `apt upgrade` does and which the session cannot
        otherwise see."""
        self.assertEqual(gnome_overlap.notes_text({"notes": ["libmutter has been replaced"]}),
                         "note: libmutter has been replaced\n")
        self.assertEqual(gnome_overlap.notes_text({}), "")
        self.assertEqual(gnome_overlap.notes_text({"notes": []}), "")
        self.assertEqual(gnome_overlap.notes_text({"notes": [None, ""]}), "")


# ---------------------------------------------------------------- the CLI

class Refusals(Case):
    def test_persistent_and_the_flag_cannot_be_combined(self):
        for argv in ((FLAG, "--persistent"), ("--persistent", FLAG)):
            code, out, err = self.run_cli(*argv, "--output", "Virtual-2",
                                          "--pos", "960x0")
            self.assertEqual(code, 1, argv)
            self.assertIn("--persistent and --unsafe-gnome-overlap cannot be "
                          "used together", err)
            self.assertIn("discards the whole file", err)
            self.assertEqual(self.ext_calls(), [])
            self.assertEqual(self.applied(), [])

    def test_the_flag_means_nothing_off_gnome(self):
        for backend in ("kwin", "sway", "wlr"):
            code, out, err = self.run_cli(FLAG, "--output", "Virtual-2",
                                          "--pos", "960x0", backend=backend)
            self.assertEqual(code, 1, backend)
            self.assertIn("only means anything on GNOME", err)
            self.assertIn(backend, err)
            self.assertEqual(self.ext_calls(), [])

    def test_an_unmeasured_shell_is_refused_with_what_a_maintainer_needs(self):
        """The one *cautious* refusal, and the only one whose message is written
        for somebody who is going to add a generation rather than for somebody
        who is going to stop.

        The extension is asked exactly one thing here, and it is a Probe whose
        first check refuses: nothing private is read and nothing is written, and
        what comes back is the public measurements -- the versions in the room
        and the size the GType registry reports -- which is the half of the
        message this process cannot produce on its own."""
        self.mock.overlap.shell = "48.3"
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("GNOME Shell 48.3 is not a build this has been measured on", err)
        self.assertIn("Nothing was read out of gnome-shell and nothing was written", err)
        # the versions found, the size reported, what was expected
        self.assertIn("libmutter-18.so.0", err)
        self.assertIn("MetaMonitorsConfig 80 bytes, from this build's GType registry", err)
        self.assertIn("GNOME 46 -> libmutter-14.so.0, W11Overlap14", err)
        # and where the answer goes
        self.assertIn("gnome/w11-overlap@w11/generations.json", err)
        self.assertIn("wxrandr/gnome_overlap.py", err)
        self.assertIn('docs/Technical.md section 6, "Adding a GNOME generation"', err)
        self.assertEqual(self.ext_calls(), ["Probe"])
        self.assertEqual(self.applied(), [])

    def test_a_shell_that_will_not_say_its_version_is_refused(self):
        self.mock.overlap.shell = None
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("cannot tell which GNOME Shell this is", err)
        self.assertEqual(self.ext_calls(), [])

    def test_an_extension_that_is_not_running_says_how_to_install_it(self):
        self.mock.overlap.present = False
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("sh gnome/install-overlap.sh", err)
        self.assertIn("log out and back in once", err)
        self.assertEqual(self.applied(), [])

    def test_more_than_a_position_has_to_go_the_ordinary_way_first(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0", "--mode", "1280x720")
        self.assertEqual(code, 1)
        self.assertIn("changes more than where the monitors are", err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])

    def test_a_refusal_from_the_extension_is_relayed_with_its_check(self):
        self.mock.overlap.reply = {"ok": False, "check": "struct-size",
                                   "reason": "this build's MetaMonitorsConfig "
                                             "is 88 bytes"}
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (struct-size)", err)
        self.assertIn("88 bytes", err)
        self.assertEqual(self.applied(), [])

    def test_an_exception_inside_the_shell_is_a_message_not_a_traceback(self):
        self.mock.overlap.error = "TypeError: this.lib.dup_cfg is not a function"
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension failed", err)
        self.assertIn("dup_cfg is not a function", err)
        self.assertEqual(self.applied(), [])

    def test_an_answer_that_is_not_json_is_a_message_too(self):
        ov = self.mock.overlap

        def not_json(member, req):
            ov.calls.append((member, req))
            return "<html>"
        ov.answer = not_json
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("answered something that is not JSON", err)


class TheOrdinaryPathIsUntouched(Case):
    def test_an_adjacent_layout_never_reaches_the_extension(self):
        # the flag is typed, and does nothing whatever: Mutter takes this
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "1920x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertNotIn(FLAG, err)

    def test_a_move_that_stays_adjacent_goes_through_displayconfig(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-1", "--pos", "1920x0",
                                      "--output", "Virtual-2", "--pos", "0x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(len(self.applied()), 1)
        self.assertEqual(self.applied()[0][1], mutter.TEMPORARY)

    def test_without_the_flag_an_overlap_is_still_mutters_refusal(self):
        code, out, err = self.run_cli("--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("GNOME's Mutter refused this layout", err)
        self.assertEqual(self.ext_calls(), [])

    def test_no_environment_variable_turns_it_on(self):
        env = {"WXRANDR_UNSAFE_GNOME_OVERLAP": "1", "WXRANDR_OVERLAP": "1",
               "WXRANDR_UNSAFE": "1", "WXRANDR_GNOME_OVERLAP": "yes"}
        code, out, err = self.run_cli("--output", "Virtual-2", "--pos", "960x0",
                                      env=env)
        self.assertEqual(code, 1)
        self.assertIn("GNOME's Mutter refused this layout", err)
        self.assertEqual(self.ext_calls(), [])

    def _pair(self, layout, argv, expect):
        """The same command line with and without the flag, as (without, with).

        A fresh mock for each, because the point is the *bytes* on stderr and a
        layout carried over from the first run would change them; `layout` is
        the head arrangement the command is typed at, because a stanza naming an
        output that is not there is answered by the parser
        ("warning: output Virtual-2 not found; ignoring") and never reaches
        plan() at all -- two identical strings that agree about nothing.
        `expect` is the plan() warning the run must have produced, asserted on
        the plain half, so that a row which stops short is a failure here rather
        than a pass everywhere."""
        out = []
        for flags in ((), (FLAG,)):
            self.mock.mutter = layout()
            self.mock.overlap = FakeOverlap()
            code, _o, err = self.run_cli(*flags, *argv)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.ext_calls(), [])
            out.append(err)
        self.assertIn(expect, out[0])
        return out

    @unittest.expectedFailure
    def test_the_flag_changes_not_one_byte_of_a_layout_gnome_accepts(self):
        """Fix 20 (F1.6/F2.4): `overlap_route()` plans the layout to find out
        whether Mutter would refuse it, throws the plan away and returns None,
        and `apply()` then plans it a second time -- so every warning `plan()`
        emits on the way is printed twice as soon as the flag is typed.

        Measured at HEAD on this harness: over two_virtuals(), `--output
        Virtual-2 --scale 7x7` prints "scale 7 is not available for Virtual-2 at
        1920x1080; using 1" once without the flag and twice with it, on an apply
        and on a --dryrun (two_virtuals and not three_monitors, which has no
        Virtual-2: there the stanza is dropped by the parser and plan() never
        runs, so both halves say "output Virtual-2 not found" and agree);
        `--output DP-1 --mode 1920x1080` over twm.three_monitors() prints
        "output HDMI-1 moved to +3840+0 to stay adjacent to DP-1" once and
        twice the same way.  Nothing about the layout changed and nothing about
        the flag applies -- the extension is not even asked -- so the flag has
        made the tool talk differently about a run it had no part in, which is
        the one thing `--unsafe-gnome-overlap` promises it does not do.

        The fix: `overlap_route()` returns the plan it made, `apply()` and
        `verify()` take it as `plan=`, and it is made once.
        """
        scale = "scale 7 is not available for Virtual-2 at 1920x1080; using 1"
        moved = "output HDMI-1 moved to +3840+0 to stay adjacent to DP-1"
        for layout, argv, expect in (
                (two_virtuals, ("--output", "Virtual-2", "--scale", "7x7"), scale),
                (two_virtuals, ("--dryrun", "--output", "Virtual-2",
                                "--scale", "7x7"), scale),
                (twm.three_monitors, ("--output", "DP-1",
                                      "--mode", "1920x1080"), moved),
                (twm.three_monitors, ("--dryrun", "--output", "DP-1",
                                      "--mode", "1920x1080"), moved)):
            with self.subTest(argv=argv):
                plain, flagged = self._pair(layout, argv, expect)
                self.assertEqual(plain, flagged)

    def test_a_position_that_cannot_fit_on_a_screen_never_reaches_the_extension(self):
        """xrandr's own screen limit is 32767x32767, and it is checked before
        anything of this feature runs.

        The three here are the ones that would wrap: 4294968256 and 4294967296
        are 2**32 + a bit, -2147483649 is one past INT32_MIN.  A number that
        survives to the extension is written into `MetaLogicalMonitorConfig.x`
        as four little-endian bytes (rules.js le32), so a wrap here would place
        a monitor somewhere nobody asked for -- inside gnome-shell, where the
        refusals have already been passed."""
        for pos in ("4294968256x0", "-2147483649x0", "0x4294967296"):
            with self.subTest(pos=pos):
                self.mock.mutter = two_virtuals()
                self.mock.overlap = FakeOverlap()
                code, out, err = self.run_cli(FLAG, "--output", "Virtual-2",
                                              "--pos", pos)
                self.assertEqual(code, 1)
                self.assertIn("screen cannot be larger than 32767x32767", err)
                self.assertEqual(self.ext_calls(), [])
                self.assertEqual(self.applied(), [])

    def test_a_position_that_does_fit_reaches_the_extension_unchanged(self):
        """The control for the test above, and the pin on a claim that was made
        and is wrong: +30000+0 is inside the 32767 screen limit, so it is an
        ordinary far-away position, it takes the overlap route (Mutter calls the
        gap non-adjacent) and the number the extension is handed is the number
        that was typed.  Nothing clamps it and nothing refuses it."""
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "30000x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        _member, req = self.mock.overlap.calls[0]
        self.assertEqual([g["x"] for g in req["want"]], [0, 30000])

    def test_a_session_built_without_the_flag_has_it_off(self):
        self.assertIs(cli.Session.overlap, False)
        self.assertIsNone(cli.Session.overlap_force)


class Applying(Case):
    def test_the_whole_run(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        # the warning, in full, before anything happened
        self.assertTrue(err.startswith(WARNING), err[:400])
        # Mutter tests adjacency first, so an overlap and a gap get one sentence
        self.assertIn("GNOME's rule this breaks: logical monitors not adjacent", err)
        self.assertIn("mutter's own validator on the result: refused: "
                      "Logical monitors not adjacent", err)
        self.assertIn("monitors.xml: unchanged", err)
        # DisplayConfig was never asked to apply anything
        self.assertEqual(self.applied(), [])

    def test_what_the_extension_is_told(self):
        self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        member, req = self.mock.overlap.calls[0]
        self.assertEqual(member, "ApplyOverlap")
        self.assertEqual(req["layout_mode"], 1)
        self.assertEqual(req["expect"],
                         [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                          {"connectors": ["Virtual-2"], "x": 1920, "y": 0}])
        self.assertEqual(req["want"],
                         [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                          {"connectors": ["Virtual-2"], "x": 960, "y": 0}])

    def test_a_gap_is_the_same_route_as_an_overlap(self):
        # Mutter refuses both with one sentence; the flag covers both, because
        # what it means is "a layout GNOME will not place"
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "3000x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.assertIn("logical monitors not adjacent", err)

    def test_asking_for_the_layout_that_is_running_writes_nothing(self):
        # the overlap is applied ...
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 0, err)
        # ... and asking for it again does not go near the extension a second
        # time: there is nothing to write, so nothing is written
        self.mock.mutter.logical[1] = (960, 0, 1.0, 0, False,
                                       [("Virtual-2", "1920x1080@60.000")])
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.assertIn("already where this asks for them; nothing was written", err)
        self.assertNotIn("What it risks", err)

    def test_a_note_reaches_the_user_on_an_apply_and_on_a_dryrun(self):
        """The one thing a session cannot see for itself: libmutter replaced
        on disk under it, which every `apt upgrade` of GNOME does.  It changes
        no decision -- the checks ran against the library in memory, which is
        the one being written to -- so it is a note, and a note is printed."""
        self.mock.overlap.notes = ["libmutter has been replaced on disk since "
                                   "this session started"]
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertIn("note: libmutter has been replaced on disk", err)
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertIn("note: libmutter has been replaced on disk", err)

    def generation(self, g):
        """The mock extension, dressed as one measured generation: the shell
        version, libmutter's label, the size that build's MetaMonitorsConfig
        really is, and the soname and Meta typelib that release carries."""
        ov = FakeOverlap(shell="%d.0" % g["shell_major"])
        ov.libmutter = g["libmutter"]
        ov.instance_size = ov.declared_size = g["struct_size"]
        ov.sonames = [g["soname"]]
        ov.meta_typelib = g["meta_typelib"]
        self.mock.overlap = ov
        return ov

    def test_every_measured_generation_runs_the_whole_route(self):
        """The route end to end on each record in GENERATIONS, so a generation
        added to the table is exercised the day it lands rather than the day
        somebody remembers to add a test.

        The numbers are the measured ones: GNOME 46.0 carries libmutter-14 with
        a 72-byte MetaMonitorsConfig (Ubuntu 24.04, noble-gnome golden, six
        checks green live), 50.1 carries libmutter-18 at 80 bytes (26.04), and
        51.beta carries libmutter-51 -- still 80 bytes, and its label is the
        text "51" rather than a number derived from the major, because mutter
        51 set libmutter_api_version to the GNOME major and the arithmetic that
        used to produce it is gone."""
        for g in gnome_overlap.GENERATIONS:
            with self.subTest(shell=g["shell_major"]):
                self.mock.mutter = two_virtuals()
                ov = self.generation(g)
                code, out, err = self.run_cli(FLAG, "--output", "Virtual-2",
                                              "--pos", "960x0")
                self.assertEqual(code, 0, err)
                self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
                self.assertIn("(GNOME Shell %d.0)" % g["shell_major"], err)
                self.assertIn("move Virtual-2 from +1920+0 to +960+0", err)
                self.assertEqual(ov.calls[0][1]["want"][1]["x"], 960)
                # and a dryrun on the same generation names the library the
                # checks ran against, by the table's label and not by a number
                # worked out from the major
                self.mock.mutter = two_virtuals()
                ov = self.generation(g)
                code, out, err = self.run_cli("--dryrun", FLAG, "--output",
                                              "Virtual-2", "--pos", "960x0")
                self.assertEqual(code, 0, err)
                self.assertIn("overlap check shell-version: GNOME Shell %d.0, "
                              "libmutter-%s" % (g["shell_major"], g["libmutter"]),
                              err)
                self.assertIn("overlap check typelib: W11Overlap%s, "
                              "MetaMonitorsConfig %d bytes as declared"
                              % (g["libmutter"], g["struct_size"]), err)

    def test_a_layout_mode_decides_the_route_and_is_relayed_verbatim(self):
        """A scaled neighbour, and the arithmetic that happens out here.

        Virtual-S is a 1920x1080 panel at scale 2 beside Virtual-1.  In layout
        mode 1 (logical -- GNOME's default once fractional scaling is on) it
        occupies 960 of layout space and runs 1920..2880, so Virtual-1 moved to
        +2880+0 is adjacent and Mutter takes it: the extension is never asked.
        In mode 2 (physical) the same monitor occupies 1920 and runs 1920..3840,
        so the same command overlaps and the route opens.  One command, two
        answers, decided by a number the extension is handed rather than one it
        works out -- and the number goes across the wire as it is."""
        self.mock.mutter = mixed_virtuals(1)
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-1", "--pos", "2880x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.mock.mutter = mixed_virtuals(2)
        self.mock.overlap = FakeOverlap()
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-1", "--pos", "2880x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        req = self.mock.overlap.calls[0][1]
        self.assertEqual(req["layout_mode"], 2)
        self.assertEqual(req["expect"],
                         [{"connectors": ["Virtual-1"], "x": 0, "y": 0},
                          {"connectors": ["Virtual-S"], "x": 1920, "y": 0}])
        self.assertEqual(req["want"],
                         [{"connectors": ["Virtual-S"], "x": 0, "y": 0},
                          {"connectors": ["Virtual-1"], "x": 960, "y": 0}])

    def test_three_heads_send_three_groups_and_the_undo_names_all_three(self):
        """The undo line is the layout that is running now, whole: a user whose
        session survives reads it off the terminal and types it back, and a line
        that named only what moved would leave the other two where the failed
        run put them."""
        self.mock.mutter = three_virtuals()
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-3", "--pos", "2880x0")
        self.assertEqual(code, 0, err)
        req = self.mock.overlap.calls[0][1]
        self.assertEqual([g["connectors"] for g in req["expect"]],
                         [["Virtual-1"], ["Virtual-2"], ["Virtual-3"]])
        self.assertEqual([g["x"] for g in req["want"]], [0, 1920, 2880])
        self.assertIn("wxrandr --output Virtual-1 --pos 0x0 --output Virtual-2 "
                      "--pos 1920x0 --output Virtual-3 --pos 3840x0", err)

    def test_a_vertical_overlap_is_the_same_route(self):
        """Mutter's rule is about edges, not about axes: stacking Virtual-2 half
        on top of Virtual-1 is refused for exactly the reason a side-by-side
        overlap is, and takes the same route."""
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "0x540")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.assertEqual(self.mock.overlap.calls[0][1]["want"][1],
                         {"connectors": ["Virtual-2"], "x": 0, "y": 540})
        self.assertIn("move Virtual-2 from +1920+0 to +0+540", err)

    def test_a_saved_file_that_moved_is_the_shout_and_not_a_reason_less_refusal(self):
        """The reply extension.js:1100-1108 actually produces: `applied: true`,
        `ok: false`, no `check` and no `reason`, because the monitors.xml digest
        moved across a call that had already written the words.

        Until wxrandr/mutter.py:851 was `if not reply.get("ok") and not
        reply.get("applied")`, that reply took the refusal path and printed
        "the overlap extension refused (?): no reason given" -- about a layout
        that is on the screen.  What has to happen instead is everything an
        applied run does: the CHANGED shout with both digests, the post-apply
        snapshot, and exit 1 for the `ok: false`, in that order.

        Reachability, measured: on GNOME 46.0 (noble-gnome golden, package
        route) a second apply at +1200+0 with a loop appending to
        ~/.config/monitors.xml every 50 ms did not trigger it -- rc 0, applied,
        digest unchanged.  So the branch is held down here rather than there.
        """
        self.mock.overlap.saved_config_moved = True
        # Every snapshot, tagged with how many extension calls had happened when
        # it was taken: a snapshot at 1 is a snapshot after the write, which is
        # the one the old code never reached.
        taken = []
        real = mutter.MutterOutputs.snapshot

        def spy(impl, state):
            taken.append(len(self.mock.overlap.calls))
            return real(impl, state)
        mutter.MutterOutputs.snapshot = spy
        self.addCleanup(setattr, mutter.MutterOutputs, "snapshot", real)
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "960x0")
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.assertIn("CHANGED across this call (%s -> %s)" % (SAVED_BEFORE, SAVED_AFTER),
                      err)
        self.assertIn("that should be impossible, please report it", err)
        # the sentence the old branch printed instead, about a layout that had
        # already been placed
        self.assertNotIn("no reason given", err)
        self.assertNotIn("the overlap extension refused", err)
        # the post-apply snapshot, and the line that says which layout is up
        self.assertIn(1, taken)
        self.assertIn("the layout above is what is running now", err)
        self.assertIn("wxrandr --output Virtual-1 --pos 0x0 --output Virtual-2 "
                      "--pos 1920x0", err)
        # the state file, which is the other half of "take the snapshot": the
        # caller's own `sess.state.save()` (wxrandr/cli.py:1509) is three lines
        # past the call that raises here, so on this branch alone the file is
        # written by apply_overlap itself.  Without that save the run leaves no
        # state file at all -- the screen has the new layout and the record of
        # it is gone.
        self.assertTrue(os.path.exists(self.state_path), "no state file was written")
        with open(self.state_path, encoding="utf-8") as fh:
            saved = json.load(fh)["overlap-test"]
        self.assertEqual(saved["primary"], "Virtual-1")   # re-synced by snapshot()
        # ...and only then the status
        self.assertEqual(code, 1)


class DryRun(Case):
    def test_dryrun_probes_and_writes_nothing(self):
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["Probe"])
        self.assertTrue(err.startswith(WARNING), err[:200])
        self.assertIn("overlap check struct-size", err.replace("typelib", "struct-size"))
        self.assertIn("dryrun: nothing was written", err)
        self.assertEqual(self.applied(), [])

    def test_dryrun_reports_the_checks_the_extension_ran(self):
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0")
        for name in ("shell-version", "typelib", "sentinel", "pending-dialog",
                     "bounded-read", "public-view"):
            self.assertIn("overlap check %s:" % name, err)

    def test_a_dryrun_of_an_adjacent_layout_is_the_ordinary_one(self):
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "1920x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertIn("mutter verify: ok", err)

    def test_a_dryrun_refusal_still_writes_nothing(self):
        self.mock.overlap.reply = {"ok": False, "check": "sentinel",
                                   "reason": "the tail moved"}
        code, out, err = self.run_cli("--dryrun", FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertIn("(sentinel)", err)
        self.assertEqual(self.applied(), [])


# ------------------------------------------------- nothing else gets in here

class NoOtherWayIn(unittest.TestCase):
    """The applying path has exactly one caller, and it is behind the flag."""

    def _sources(self):
        out = {}
        for sub in ("wxrandr", "warandr", "wdotool", "wwmctl", "wxprop",
                    "wmirror", "w11common"):
            d = os.path.join(ROOT, sub)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.endswith(".py"):
                    p = os.path.join(d, name)
                    out["%s/%s" % (sub, name)] = open(p, encoding="utf-8").read()
        return out

    def test_only_two_modules_know_the_client_exists(self):
        importers = [p for p, s in self._sources().items()
                     if re.search(r"\bgnome_overlap\b", s)
                     and p != "wxrandr/gnome_overlap.py"]
        self.assertEqual(sorted(importers), ["wxrandr/cli.py", "wxrandr/mutter.py"])

    def test_apply_overlap_has_one_caller(self):
        callers = [(p, n) for p, s in self._sources().items()
                   for n, line in enumerate(s.splitlines(), 1)
                   if "apply_overlap(" in line and "def apply_overlap" not in line]
        self.assertEqual([p for p, _n in callers], ["wxrandr/cli.py"])

    def test_that_caller_is_behind_the_flag(self):
        src = open(os.path.join(ROOT, "wxrandr", "cli.py"), encoding="utf-8").read()
        body = src[src.index("    def apply(self, targets):"):]
        body = body[:body.index("\n    @property")]
        guard = body.index("if self.overlap and self.backend == \"mutter\":")
        self.assertLess(guard, body.index("apply_overlap("))
        self.assertIn("return self.impl.apply(self.state, targets, self.persistent)", body)

    def test_the_flag_is_the_only_thing_that_sets_it(self):
        src = open(os.path.join(ROOT, "wxrandr", "cli.py"), encoding="utf-8").read()
        setters = [line.strip() for line in src.splitlines()
                   if re.search(r"\.overlap\s*=|^\s*o\.overlap\s*=|overlap = ", line)]
        for line in setters:
            self.assertFalse("environ" in line, line)

    def test_the_applying_method_never_runs_for_a_layout_gnome_accepts(self):
        src = open(os.path.join(ROOT, "wxrandr", "mutter.py"), encoding="utf-8").read()
        for name in ("apply_overlap", "overlap_dryrun"):
            body = src[src.index("    def %s(self" % name):]
            body = body[:body.index("\n    def ", 10)]
            self.assertIn("route = self.overlap_route(state, targets)", body)
            self.assertIn("if route is None:", body)


# ------------------------------------------------- the shipped extension

class ShippedExtension(unittest.TestCase):
    def setUp(self):
        self.js = open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8").read()

    def test_enable_touches_nothing_but_dbus(self):
        """The whole design rests on this: an extension that is enabled and
        never called must not be able to hurt anybody, including at login."""
        body = self.js[self.js.index("    enable() {"):]
        body = body[:body.index("\n    disable()")]
        for forbidden in ("typelib", "imports.gi", "libmutter", "get_config_manager",
                          "maps", "Guarded", "wr(", "apply(", "Probe", "memdup"):
            self.assertNotIn(forbidden, body, forbidden)
        self.assertIn("Gio.DBusExportedObject.wrapJSObject", body)

    def test_the_pending_dialog_check_reads_a_field_that_exists(self):
        """The guard between this feature and the only lasting damage it can do.

        It used to read `Main.wm._displayChangeDialog`, which exists on neither
        supported release -- measured on 46.0 and 50.1, with the dialog on
        screen and without it -- so it passed always, and a "Keep changes?"
        confirmed while an overlap was applied wrote that overlap into
        ~/.config/monitors.xml, on both.  A check that cannot fire is worse than
        no check, because it is believed.  What it reads now is the modal count,
        which goes 0 -> 1 while the dialog is up.
        """
        body = self.js[self.js.index("    noPendingDialog() {"):]
        body = body[:body.index("\n    }\n") + 6]
        self.assertIn("Main.modalCount", body)
        self.assertIn("modalVerdict(modal)", body)
        self.assertIn("refuse('pending-dialog'", body)
        # the field it cannot see: nowhere in the file, under any spelling
        self.assertNotIn("_displayChangeDialog", self.js)
        # and the pass line is reached only when the verdict was nothing
        self.assertLess(body.index("refuse('pending-dialog'"),
                        body.index("this._pass('pending-dialog'"))

    # ------------------------------------------------------------------
    # T03's static half.  The node harness that runs the whole guard chain is
    # tests/test_bridge_js.py (class OverlapGuards); what is pinned here is
    # what can be read off the file, so that a regression in one of these is
    # caught even where there is no node.

    @unittest.expectedFailure
    def test_a_bounded_string_read_checks_the_bytes_it_will_read(self):
        """Fix 12 (F1.1): `Reader.string()` range-checks *one* byte and then
        calls `lib.strn(p, MAX_CONNECTOR)`, which reads up to 64.

        A connector name at the very end of a mapping -- one byte inside it,
        63 bytes past the end -- passes the check and is read anyway.  The
        whole design of the reader is that no address is touched before
        /proc/self/maps says the *range* is there; a check that clears one
        byte and a read that takes 64 is the check not doing its job.  What it
        has to ask for is `MAX_CONNECTOR + 1` (the bound plus the NUL
        g_strndup writes).

        Wants re-measuring on a live compositor with T59 before it lands: the
        connector strings on 46.0 and 50.1 sit inside libmutter's own
        read-only data, and a stricter check that refuses there would refuse
        every read rather than none."""
        at = re.findall(r"_at\((\w+), p, ([^)]+)\)", self.js)
        self.assertTrue(at)
        # no call clears fewer bytes than the read that follows it
        self.assertNotIn("1", [n.strip() for _what, n in at])
        self.assertIn("MAX_CONNECTOR + 1", [n.strip() for _what, n in at])

    @unittest.expectedFailure
    def test_a_description_that_will_not_say_is_not_a_description_that_names_none(self):
        """Fix 13 (F1.2): `sharedLibraries()` returns `[]` when every spelling
        of the GIRepository call throws, and `[]` is also what a correct
        description produces -- so "the API moved and nothing answered" and
        "this description names no library" arrive as the same answer, and the
        `shared-library` check passes on a namespace nobody could read.

        That check exists because a description naming a library that is not
        mapped makes gjs abort gnome-shell on the first call through it, which
        is what a forced `--dryrun` did on Ubuntu 26.10.  A guard that cannot
        distinguish "no" from "I could not ask" is a guard that passes on the
        input it was built for.  `sharedLibraries()` returns `null` for "no
        answer", and `typelib()` refuses on `null`."""
        body = self.js[self.js.index("function sharedLibraries(repo, ns) {"):]
        body = body[:body.index("\n}\n") + 3]
        self.assertIn("return null", body)
        self.assertIn("refuse('shared-library'", self.js)
        gate = self.js[self.js.index("for (const named of sharedLibraries("):]
        self.assertIn("=== null", gate[:600])

    @unittest.expectedFailure
    def test_a_position_no_screen_can_hold_is_refused_before_mutters_verdict(self):
        """Fix 14 (F1.5), the range half: `_apply` asks `mutterFault(targets)`
        first, and nothing before that looks at whether the numbers are on a
        screen at all.

        `x < 0`, `y < 0`, `x + w > 32767` or `y + h > 32767` is a request the
        writer would put into `MetaLogicalMonitorConfig.x` as four little-endian
        bytes regardless.  wxrandr refuses those out here (see
        TheOrdinaryPathIsUntouched), but the extension is a D-Bus service on the
        session bus and wxrandr is not the only thing that can call it, which is
        the whole reason every other check is repeated inside.

        Split from the naming pin below because the brief leaves the author both
        answers and this is the one that is about a write: a fix that renames
        the check and still writes 2**32 into a position would flip that test
        and must not flip this one."""
        head = self.js[:self.js.index("const fault = mutterFault(targets);")]
        self.assertIn("32767", head)

    @unittest.expectedFailure
    def test_the_check_that_fires_when_mutter_agrees_is_named_for_that(self):
        """Fix 14 (F1.5), the naming half: `not-an-overlap` is refused whenever
        `mutterFault(targets)` comes back null -- which is "Mutter would take
        this layout", not "these rectangles do not overlap".  A gap wider than
        the screen is not an overlap either and is refused under the same name,
        so the word is wrong in both directions.

        The author's call, and either answer closes it: rename the check to
        `not-refused-by-mutter`, or refuse the gaps too so that the name becomes
        true.  Both are accepted here; one line in docs/Technical.md:915 goes
        with whichever it is."""
        renamed = "not-an-overlap" not in self.js and "not-refused-by-mutter" in self.js
        gaps = re.search(r"refuse\(['\"]not-an-overlap['\"]", self.js) is not None \
            and "gap" in self.js
        self.assertTrue(renamed or gaps,
                        "the check is still called not-an-overlap and gaps are "
                        "still allowed through it")

    def test_the_write_passes_no_length_of_its_own(self):
        """The typelib declares the write's length parameter as the length of
        the byte array, so gjs takes it from there and drops a third argument
        with a `JS WARNING: Too many arguments` in the journal.  le32() is four
        bytes by construction."""
        calls = re.findall(r"lib\.wr\((.*?)\);", self.js)
        self.assertTrue(calls)
        for call in calls:
            self.assertEqual(call.count(","), 1, call)

    def test_the_apply_method_is_a_constant(self):
        self.assertIn("const METHOD_TEMPORARY = 1;", self.js)
        calls = re.findall(r"lib\.apply\(([^)]*)\)", self.js)
        self.assertEqual(calls, ["g.mm, g.cfg, METHOD_TEMPORARY"])
        # 2 (PERSISTENT) is what makes Mutter write monitors.xml: it must not
        # appear anywhere near the apply, under any spelling
        self.assertNotIn("METHOD_PERSISTENT", self.js)
        self.assertEqual(re.findall(r"METHOD_TEMPORARY = (\d+)", self.js), ["1"])

    def test_nothing_can_write_the_saved_configuration(self):
        for name in sorted(os.listdir(GIR_DIR)):
            if not name.endswith(".gir"):
                continue
            text = open(os.path.join(GIR_DIR, name), encoding="utf-8").read()
            syms = re.findall(r'c:identifier="([^"]+)"', text)
            self.assertTrue(syms)
            for sym in syms:
                for bad in ("save", "write", "store", "persist"):
                    self.assertNotIn(bad, sym.lower(), "%s: %s" % (name, sym))
        for name in sorted(os.listdir(os.path.join(EXT_DIR, "typelib"))):
            raw = open(os.path.join(EXT_DIR, "typelib", name), "rb").read()
            self.assertNotIn(b"save_current", raw, name)
            self.assertIn(b"meta_monitor_manager_apply_monitors_config", raw, name)

    def test_one_description_per_measured_generation(self):
        got = sorted(os.listdir(os.path.join(EXT_DIR, "typelib")))
        self.assertEqual(got, sorted("%s-1.0.typelib" % g["namespace"]
                                     for g in gnome_overlap.GENERATIONS))

    def test_every_pointer_is_declared_as_a_number(self):
        """The one property that turns a wrong offset from a SIGSEGV into a
        refusal: gjs must never be able to follow a pointer for us."""
        for name in sorted(os.listdir(GIR_DIR)):
            if not name.endswith(".gir"):
                continue
            text = open(os.path.join(GIR_DIR, name), encoding="utf-8").read()
            for record in re.findall(r"<record .*?</record>", text, re.S):
                for field in re.findall(r"<field .*?</field>", record, re.S):
                    if 'c:type="gpointer"' in field or "*" in field:
                        self.fail("%s declares a pointer field: %s" % (name, field))

    def test_the_metadata_is_narrow_and_says_what_it_is(self):
        meta = json.load(open(os.path.join(EXT_DIR, "metadata.json")))
        self.assertEqual(meta["uuid"], gnome_overlap.UUID)
        # derived from the table by gen-gir.py, not kept beside it
        self.assertEqual(meta["shell-version"],
                         [str(g["shell_major"]) for g in gnome_overlap.GENERATIONS])
        self.assertIn("unsupported", meta["name"])
        self.assertIn("nothing at all until it is called", meta["description"])

    def test_the_installer_is_its_own(self):
        sh = open(os.path.join(ROOT, "gnome", "install-overlap.sh"),
                  encoding="utf-8").read()
        self.assertIn(gnome_overlap.UUID, sh)
        self.assertNotIn("w11-bridge@w11", sh)
        subprocess.run(["sh", "-n", os.path.join(ROOT, "gnome", "install-overlap.sh")],
                       check=True)
        bridge = open(os.path.join(ROOT, "gnome", "install-bridge.sh"),
                      encoding="utf-8").read()
        self.assertNotIn("overlap", bridge.lower())

    def summary(self, directory, ns):
        """One shipped-or-fresh typelib, read back through GIRepository."""
        gen = load_gen_gir()
        got, why = gen.typelib_summary(directory, ns)
        if why == gen.NO_GIREPOSITORY:
            self.skipTest("no GIRepository")
        self.assertIsNotNone(got, "%s: %s" % (ns, why))
        return got

    def test_the_shipped_typelibs_mean_what_the_descriptions_beside_them_mean(self):
        """The shipped bytes against a fresh compile of the checked-in .gir,
        compared by meaning: namespace, no shared library, every function name
        and C symbol, every record's size and every field's offset.

        Not by bytes, which is what this test did until g-ir-compiler 1.86
        turned it red on both Ubuntu 26.04 and nixpkgs: 1.86 writes a different
        reserved word into each of the 17 FunctionBlobs than the compiler that
        produced the checked-in files (0x00000001 against 0x03FF0FFD), and the
        two 1.86 compilers agree with each other.  The descriptions themselves
        are right: all six checks pass live on GNOME 46.0 with these exact
        bytes (noble-gnome golden, package route, --dryrun through the shipped
        W11Overlap14).  A hash is not a meaning, and a test that cannot tell a
        compiler upgrade from a wrong offset is a test nobody will believe the
        next time it goes red.
        """
        if shutil.which("g-ir-compiler") is None:
            self.skipTest("no g-ir-compiler")
        gen = load_gen_gir()
        shipped_dir = os.path.join(EXT_DIR, "typelib")
        for g in gnome_overlap.GENERATIONS:
            ns = g["namespace"]
            with self.subTest(ns=ns):
                mine = self.summary(shipped_dir, ns)
                with tempfile.TemporaryDirectory(prefix="fresh-typelib-") as fresh:
                    subprocess.run(
                        ["g-ir-compiler", "--includedir", "/usr/share/gir-1.0",
                         "-o", os.path.join(fresh, "%s-1.0.typelib" % ns),
                         os.path.join(GIR_DIR, "%s-1.0.gir" % ns)], check=True)
                    theirs = self.summary(fresh, ns)
                self.assertEqual(gen.compare_typelibs(mine, theirs), [])
                # and the two properties the comparison alone would not pin,
                # because both files could be wrong the same way
                self.assertEqual(mine["namespace"], ns)
                self.assertEqual(mine["shared_libraries"], [])
                self.assertEqual(sorted(f["symbol"] for f in mine["functions"].values()),
                                 sorted(identifiers_in_gir(ns)))
                # the one property of the shipped bytes the strn fix changed,
                # read out of the artifact gnome-shell loads rather than out of
                # the .gir beside it: 2 is GI_TRANSFER_EVERYTHING, i.e. gjs
                # frees the string g_strndup allocated.  It was 0 in the
                # typelibs checked in before that fix, which leaked one
                # connector name per monitor per read.
                self.assertEqual(mine["functions"]["strn"]["transfer"], 2)

    def test_the_declared_sizes_are_the_measured_ones(self):
        """Every record in the table, not two of them by name: the sizes and
        offsets are read out of the shipped typelib and checked against
        GENERATIONS, so a description added for a new GNOME is measured here
        the day it lands.

        72 bytes on GNOME 46 (libmutter-14) and 80 on 50 and 51 (libmutter-18
        and libmutter-51), `logical_monitor_configs` at offset 40 on all three
        -- measured on live compositors and re-derived from mutter's own
        headers by gen-gir.py --from-header (HeaderDerivation below).  The
        four helper records are frozen shapes: GList 24, MetaLogicalMonitorConfig
        40, MetaMonitorConfig 24, MetaMonitorSpec 32."""
        shipped_dir = os.path.join(EXT_DIR, "typelib")
        for g in gnome_overlap.GENERATIONS:
            ns = g["namespace"]
            with self.subTest(ns=ns):
                got = self.summary(shipped_dir, ns)
                cfg = got["records"]["ConfigN"]
                self.assertEqual(cfg["size"], g["struct_size"])
                self.assertEqual(cfg["fields"]["logical_monitor_configs"], 40)
                self.assertEqual(cfg["fields"]["disabled_monitor_specs"], 48)
                # the two words this extension writes, and nothing else
                self.assertEqual(got["records"]["LMCN"]["fields"]["x"], 0)
                self.assertEqual(got["records"]["LMCN"]["fields"]["y"], 4)
                self.assertEqual([got["records"][n]["size"]
                                  for n in ("NodeN", "LMCN", "MCN", "MSN")],
                                 [24, 40, 24, 32])

    def test_the_shipped_tail_offsets_follow_the_record_the_sentinel_proved(self):
        """The shipped BYTES agree with the record's tail arithmetic, which is
        a staleness check on the typelibs and not the live measurement itself.

        `tail_slots` is the whole of what differs between the descriptions, and
        it decides two offsets: `layout_mode` at 56 + 4 * tail_slots and
        `switch_config` four bytes after it (56 is the end of
        `disabled_monitor_specs`, which sits at 48 on every generation).  60/64
        on GNOME 46 with one slot, 68/72 on 49, 50 and 51 with three.  A record
        edited without regenerating the typelib -- or the reverse -- is caught
        here, which matters because the tail is the one thing a wrong
        description gets wrong QUIETLY: the size, which the struct-size gate
        compares against the GType registry, can be right while the tail is not.

        The offsets this compares against were proved on running compositors by
        the `sentinel` check, which writes 0x5f5a through Mutter's own
        `set_switch_config` and demands it back where the description says --
        only tests/test_gnome_overlap_live.py can make that round trip.
        Measured on for 17, both ways, on the fedora43-gnome golden 2026-09-11
        (GNOME Shell 49.9, libmutter-17.so.0 build c7c9add6935d): the shipped
        W11Overlap17 round-tripped the sentinel at the declared offset, and the
        same description with `layout_mode` and `switch_config` swapped -- the
        same 80 bytes -- was refused by name, `sentinel: switch_config reads 1
        at the offset this description believes, not 24410`, with gnome-shell
        still running."""
        shipped_dir = os.path.join(EXT_DIR, "typelib")
        for g in gnome_overlap.GENERATIONS:
            with self.subTest(ns=g["namespace"]):
                cfg = self.summary(shipped_dir, g["namespace"])["records"]["ConfigN"]
                layout_mode = 56 + 4 * g["tail_slots"]
                self.assertEqual(cfg["fields"]["layout_mode"], layout_mode)
                self.assertEqual(cfg["fields"]["switch_config"], layout_mode + 4)
                # and the tail is the whole of the difference: the size follows
                # from it, so a record cannot declare slots the bytes do not hold
                self.assertEqual(cfg["size"], layout_mode + 12)
                self.assertEqual(cfg["size"], g["struct_size"])

    def test_a_compiler_that_writes_other_bytes_is_a_note_and_not_a_verdict(self):
        """The older compiler's bytes, reconstructed, put through `--check`.

        g-ir-compiler 1.86 writes 0x03FF0FFD into the reserved word of each of
        the 17 FunctionBlobs where the compiler that produced the checked-in
        files wrote 0x00000001 -- measured here by diffing the typelibs at HEAD
        against a fresh 1.86 compile: 69 bytes differ per file, 68 of them those
        17 words and the last one the strn transfer bit this batch changed.
        Rewriting the word back is therefore the historical artifact, byte for
        byte, minus that fix; it must load, mean exactly what the shipped file
        means, and come out of `--check` as a `note:` and rc 0.

        Without this the claim in the name is not exercised at all: the shipped
        typelibs were regenerated by this host's 1.86, so a plain `--check` here
        finds no byte difference to be relaxed about.
        """
        if shutil.which("g-ir-compiler") is None:
            self.skipTest("no g-ir-compiler")
        gen = load_gen_gir()
        shipped_dir = os.path.join(EXT_DIR, "typelib")
        if gen.typelib_summary(shipped_dir,
                               gnome_overlap.GENERATIONS[0]["namespace"])[1] \
                == gen.NO_GIREPOSITORY:
            self.skipTest("no GIRepository")
        with tempfile.TemporaryDirectory(prefix="older-compiler-") as older:
            for g in gnome_overlap.GENERATIONS:
                name = "%s-1.0.typelib" % g["namespace"]
                with open(os.path.join(shipped_dir, name), "rb") as fh:
                    raw = fh.read()
                self.assertEqual(raw.count(b"\xfd\x0f\xff\x03"), 17, name)
                with open(os.path.join(older, name), "wb") as fh:
                    fh.write(raw.replace(b"\xfd\x0f\xff\x03",
                                         b"\x01\x00\x00\x00"))
            rc = subprocess.run([sys.executable,
                                 os.path.join(GIR_DIR, "gen-gir.py"),
                                 "--check", older],
                                capture_output=True, text=True)
            notes = [ln for ln in rc.stdout.splitlines() if "differ in bytes" in ln]
            # Which generation is this host's compiler?  24.04's 1.80 writes the
            # 0x00000001 word itself, so there the reconstruction IS a fresh
            # compile and there is nothing to be relaxed about: three compared,
            # no note.  Measured on the CI container, where this asserted three.
            g0 = gnome_overlap.GENERATIONS[0]["namespace"]
            fresh = os.path.join(older, "fresh.typelib")
            subprocess.run(["g-ir-compiler", "--includedir", "/usr/share/gir-1.0", "-o", fresh,
                            os.path.join(GIR_DIR, "%s-1.0.gir" % g0)], check=True)
            with open(fresh, "rb") as fh:
                newer_compiler = fh.read().count(b"\xfd\x0f\xff\x03") == 17
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        self.assertEqual(len(notes), len(gnome_overlap.GENERATIONS) if newer_compiler else 0,
                         rc.stdout)
        self.assertIn("--check: %d compared, 0 skipped" % len(gnome_overlap.GENERATIONS), rc.stdout)
        for line in notes:
            self.assertTrue(line.startswith("note: "), line)
        self.assertNotIn("stale:", rc.stdout)
        self.assertNotIn("skipped:", rc.stdout)

    def test_the_tree_as_it_stands_is_compared_and_agrees(self):
        """`gen-gir.py --check` over the checked-in files: every namespace
        compared, nothing stale, nothing skipped, rc 0.

        The summary line is asserted because rc 0 alone does not say whether
        anything was looked at -- see the skipped test below."""
        if shutil.which("g-ir-compiler") is None:
            self.skipTest("no g-ir-compiler")
        gen = load_gen_gir()
        if gen.typelib_summary(os.path.join(EXT_DIR, "typelib"),
                               gnome_overlap.GENERATIONS[0]["namespace"])[1] \
                == gen.NO_GIREPOSITORY:
            self.skipTest("no GIRepository")
        rc = subprocess.run([sys.executable, os.path.join(GIR_DIR, "gen-gir.py"),
                             "--check"], capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stdout + rc.stderr)
        self.assertNotIn("stale:", rc.stdout)
        self.assertNotIn("skipped:", rc.stdout)
        self.assertIn("--check: %d compared, 0 skipped" % len(gnome_overlap.GENERATIONS),
                      rc.stdout)

    def test_a_typelib_that_gives_the_string_away_is_stale_and_not_a_note(self):
        """The comparison has to be able to see transfer-ownership, because that
        is the whole of what the strn fix changed in the shipped bytes.

        A .gir whose `strn` return says `transfer-ownership="none"` compiles to a
        typelib with the same namespace, the same 17 symbols, the same record
        sizes and the same field offsets as the shipped one -- and gjs then
        never frees what g_strndup allocated, which is one connector name (up to
        64 bytes) leaked per monitor per read.  The typelibs checked in before
        the fix are exactly this file, so a `--check` that could not see it
        would have called restoring them a no-op.
        """
        if shutil.which("g-ir-compiler") is None:
            self.skipTest("no g-ir-compiler")
        gen = load_gen_gir()
        ns = gnome_overlap.GENERATIONS[0]["namespace"]
        if gen.typelib_summary(os.path.join(EXT_DIR, "typelib"), ns)[1] \
                == gen.NO_GIREPOSITORY:
            self.skipTest("no GIRepository")
        with open(os.path.join(GIR_DIR, "%s-1.0.gir" % ns), encoding="utf-8") as fh:
            text = fh.read()
        head = text[:text.index('c:identifier="g_strndup"')]
        tail = text[text.index('c:identifier="g_strndup"'):]
        cut = tail.index("</function>")
        self.assertIn('transfer-ownership="full"', tail[:cut])
        loosened = head + tail[:cut].replace('transfer-ownership="full"',
                                             'transfer-ownership="none"', 1) + tail[cut:]
        with tempfile.TemporaryDirectory(prefix="loose-transfer-") as d:
            gpath = os.path.join(d, "%s-1.0.gir" % ns)
            with open(gpath, "w", encoding="utf-8") as fh:
                fh.write(loosened)
            subprocess.run(["g-ir-compiler", "--includedir", "/usr/share/gir-1.0",
                            "-o", os.path.join(d, "%s-1.0.typelib" % ns), gpath],
                           check=True)
            loose = self.summary(d, ns)
        shipped = self.summary(os.path.join(EXT_DIR, "typelib"), ns)
        self.assertEqual(loose["records"], shipped["records"])       # same layout
        self.assertEqual(sorted(f["symbol"] for f in loose["functions"].values()),
                         sorted(f["symbol"] for f in shipped["functions"].values()))
        bad = gen.compare_typelibs(loose, shipped)
        self.assertEqual(len(bad), 1, bad)
        self.assertIn("strn returns transfer 0, not 2", bad[0])

    def test_a_machine_that_cannot_read_a_typelib_does_not_pass_the_check(self):
        """`--check` where `import gi` fails: three `skipped:` lines, a summary
        saying nothing was compared, and rc 2 -- not rc 0.

        This is the state of nearly every desktop (the typelibs are checked in
        precisely so that nobody needs libgirepository's python bindings), so it
        is also the state of any CI that forgot to install them: a run that
        compared nothing must not look like a run that agreed about everything.
        The absence is simulated with a `gi.py` on PYTHONPATH that raises
        ImportError, which is what the reader's own `except ImportError` sees.
        """
        if shutil.which("g-ir-compiler") is None:
            self.skipTest("no g-ir-compiler")
        with tempfile.TemporaryDirectory(prefix="no-gi-") as d:
            with open(os.path.join(d, "gi.py"), "w", encoding="utf-8") as fh:
                fh.write('raise ImportError("no GIRepository bindings here")\n')
            env = dict(os.environ, PYTHONPATH=d)
            rc = subprocess.run([sys.executable, os.path.join(GIR_DIR, "gen-gir.py"),
                                 "--check"], capture_output=True, text=True, env=env)
        self.assertEqual(rc.returncode, 2, rc.stdout + rc.stderr)
        self.assertEqual(rc.stdout.count("skipped:"), len(gnome_overlap.GENERATIONS))
        self.assertIn("--check: 0 compared, %d skipped" % len(gnome_overlap.GENERATIONS),
                      rc.stdout)
        self.assertNotIn("stale:", rc.stdout)


class TheLibraryIdentity(unittest.TestCase):
    """Naming the build of libmutter this session runs is bookkeeping, not a
    guard, and the source has to keep it that way: it reads two files and
    decides nothing, so nothing in it may refuse."""

    def setUp(self):
        self.js = open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8").read()

    def body(self, head):
        body = self.js[self.js.index(head):]
        return body[:body.index("\n}\n") + 3]

    def test_reading_the_build_id_can_never_refuse(self):
        for head in ("function elfBuildId(path) {", "function libmutterIdentity(mapping) {"):
            body = self.body(head)
            self.assertNotIn("refuse(", body, head)
            self.assertIn("return null", body, head)

    def test_it_is_wrapped_where_it_is_called(self):
        """A guard that cannot fire is worse than no guard; a *note* that can
        fire is worse than no note, because it would stop an apply that every
        check had passed."""
        body = self.js[self.js.index("    version(force) {"):]
        body = body[:body.index("\n    }\n") + 6]
        at = body.index("libmutterIdentity(")
        self.assertIn("try {", body[:at])
        self.assertIn("catch", body[at:])

    def test_a_replaced_library_is_a_note_and_not_a_check(self):
        # the notes go in their own field, and nothing in the checks list
        self.assertIn("notes: [libmutterNote(this.libmutter)]", self.js)
        self.assertNotIn("refuse('libmutter-build'", self.js)
        self.assertNotIn("_pass('libmutter-build'", self.js)


class HeaderDerivation(unittest.TestCase):
    """`gen-gir.py --from-header` against three releases' own headers.

    This is the answer to a `typelib` or `sentinel` refusal after an upgrade,
    and it is checked here because a deriver nobody has run on a known answer is
    not a deriver.  All three fixtures are excerpts of mutter's own
    src/backends/meta-monitor-config-manager.h, and what comes out of them has
    to be exactly what is shipped -- which is also an independent confirmation
    of the shipped offsets: they were measured on a live compositor, and this
    arrives at the same numbers from upstream source, by arithmetic.

    49's fixture is 49.7's header from the release tag on gitlab.gnome.org
    (fetched 2026-09-11), not from the guest: the Fedora rpm carries no header,
    which is a fact about the binary package and not a reason to skip step 1 of
    docs/Technical.md section 6.  Its whole-header sha256 is 50.1's to the byte
    -- the two releases ship one struct between them -- so what this pins for 49
    is that the record's 80 bytes and 3 slots are what upstream source says, by
    the same arithmetic that answered for 50.  51 still has no fixture."""

    @classmethod
    def setUpClass(cls):
        cls.gen = load_gen_gir()

    def header(self, name):
        return open(os.path.join(ROOT, "tests", "fixtures", "mutter", name),
                    encoding="utf-8").read()

    def test_the_three_headers_derive_three_of_the_shipped_descriptions(self):
        table = {g["shell_major"]: g for g in self.gen.load_table()}
        for name, gen, size, major in (("meta-monitors-config-46.h", 14, 72, 46),
                                       ("meta-monitors-config-49.h", 17, 80, 49),
                                       ("meta-monitors-config-50.h", 18, 80, 50)):
            ints, rows, got = self.gen.build_from_header(self.header(name))
            self.assertEqual(got, size, name)
            self.assertEqual(got, table[major]["struct_size"], name)
            self.assertEqual(ints, table[major]["tail_slots"], name)
            at = {f: off for off, _sz, _t, f in rows}
            self.assertEqual(at["logical_monitor_configs"], 40, name)
            self.assertEqual(at["disabled_monitor_specs"], 48, name)
            # the tail is what differs between 46 and the rest, and it is what
            # the sentinel pins on a live compositor
            self.assertEqual(at["switch_config"], 64 if gen == 14 else 72, name)

    def test_the_generations_really_do_differ(self):
        a = self.gen.build_from_header(self.header("meta-monitors-config-46.h"))
        b = self.gen.build_from_header(self.header("meta-monitors-config-50.h"))
        self.assertNotEqual(a[0], b[0])
        self.assertNotEqual(a[2], b[2])

    def test_a_type_it_does_not_know_is_an_error_and_not_a_guess(self):
        bad = ("struct _MetaMonitorsConfig {\n  GObject parent;\n"
               "  MetaSomethingNew thing;\n};\n")
        with self.assertRaises(SystemExit) as e:
            self.gen.build_from_header(bad)
        self.assertIn("MetaSomethingNew", str(e.exception))

    def test_a_moved_head_is_refused_rather_than_renumbered(self):
        """A release that inserts a field before logical_monitor_configs has
        broken the description's *shape*, and the honest answer is a human
        reading the new struct, not a new number from this script."""
        moved = self.header("meta-monitors-config-46.h").replace(
            "  GObject parent;", "  GObject parent;\n  gint whatever;")
        with self.assertRaises(SystemExit) as e:
            self.gen.build_from_header(moved)
        self.assertIn("written by hand", str(e.exception))

    def test_it_reads_no_running_compositor(self):
        """The independence of the description from the build it is checked
        against is the whole value of the struct-size and sentinel checks.  A
        deriver that asked the compositor would spend it."""
        src = open(os.path.join(GIR_DIR, "gen-gir.py"), encoding="utf-8").read()
        body = src[src.index("def build_from_header"):]
        for forbidden in ("gi.repository", "type_query", "dbus", "gdbus",
                          "/proc/", "libmutter-"):
            self.assertNotIn(forbidden, body[:body.index("def report_header")],
                             forbidden)


# ------------------------------------------------- the rules, run for real

NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(NODE is None, "no node to run rules.js")
class RulesJS(unittest.TestCase):
    """`rules.js` is the extension's half of the decisions, and it is checked
    here against the Python that says the same thing (monitors_xml.fault) --
    which is the only way to test it without a GNOME session."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="rules-js-")
        shutil.copy(os.path.join(EXT_DIR, "rules.js"),
                    os.path.join(cls.dir, "rules.mjs"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def run_js(self, body):
        src = ("import * as R from '%s/rules.mjs';\n"
               "const input = JSON.parse(process.argv[2] || '{}');\n"
               % self.dir) + body
        path = os.path.join(self.dir, "case.mjs")
        with open(path, "w") as fh:
            fh.write(src)
        return path

    def call(self, script, arg):
        rc = subprocess.run([NODE, script, json.dumps(arg)],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        return json.loads(rc.stdout)

    LAYOUTS = [
        [(0, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (960, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (3000, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (3840, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (2880, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (0, 1080, 1920, 1080)],
        [(0, 0, 1920, 1080), (0, 1000, 1920, 1080)],
        [(100, 0, 1920, 1080), (2020, 0, 1920, 1080)],
        [(0, 0, 1920, 1080), (1920, 0, 1920, 1080), (1920, 0, 1280, 720)],
        [],
    ]

    def test_it_says_what_mutter_says_and_what_python_says(self):
        script = self.run_js(
            "const out = input.map(r => R.mutterFault("
            "r.map(([x, y, w, h]) => ({x, y, w, h}))));\n"
            "console.log(JSON.stringify(out));\n")
        got = self.call(script, self.LAYOUTS)
        want = [monitors_xml.fault(rects) for rects in self.LAYOUTS]
        # monitors_xml adds a parenthetical to the adjacency sentence; the rest
        # has to agree word for word, and the verdict always has to agree
        for js, py, rects in zip(got, want, self.LAYOUTS):
            if py is None:
                self.assertIsNone(js, rects)
            else:
                self.assertIsNotNone(js, rects)
                self.assertTrue(py.startswith(js), (js, py, rects))

    def test_the_version_gate_is_the_same_on_both_sides(self):
        """One table, read by both halves: rules.js is handed the JSON the
        extension loads, and Python has its own copy of the same records."""
        script = self.run_js(
            "console.log(JSON.stringify(input.versions.map("
            "v => R.generationFor(input.table, v))));\n")
        versions = ["46.0", "46", "50.1", "50", "47.1", "45.4", "51.0", "banana", ""]
        table = json.load(open(os.path.join(EXT_DIR, "generations.json")))
        got = self.call(script, {"table": table, "versions": versions})
        for v, gen in zip(versions, got):
            self.assertEqual(gen is not None,
                             gnome_overlap.unsupported_reason(v) is None, v)
            if gen is not None:
                self.assertEqual(gen["shell_major"],
                                 gnome_overlap.generation_for(v)["shell_major"], v)

    def test_the_comparison_rejects_every_single_field(self):
        script = self.run_js(
            "console.log(JSON.stringify(input.map(c => R.compare(c[0], c[1]))));\n")
        base = {"x": 0, "y": 0, "w": 1920, "h": 1080, "scale": 1,
                "primary": True, "connectors": ["Virtual-1"]}
        cases = [[[base], [base]]]
        for field, bad in (("x", 1), ("y", 1), ("w", 1919), ("h", 1), ("scale", 2),
                           ("primary", False), ("connectors", ["Virtual-2"])):
            cases.append([[dict(base, **{field: bad})], [base]])
        cases.append([[], [base]])                     # a NULL list read
        cases.append([[base, base], [base]])           # garbage that walked too far
        got = self.call(script, cases)
        self.assertEqual(got[0], [], "an identical read must produce no diff")
        for diffs, case in zip(got[1:], cases[1:]):
            self.assertTrue(diffs, case)

    def test_a_missing_public_connector_name_is_not_a_difference(self):
        # GNOME 46 exposes no connector enumeration; the geometry half of the
        # comparison still has to hold, and the names must not fail it
        script = self.run_js(
            "console.log(JSON.stringify(R.compare(input[0], input[1])));\n")
        priv = [{"x": 0, "y": 0, "w": 1920, "h": 1080, "scale": 1,
                 "primary": True, "connectors": ["Virtual-1"]}]
        pub = [dict(priv[0], connectors=[])]
        self.assertEqual(self.call(script, [priv, pub]), [])

    def test_drift_after_the_write_catches_anything_but_the_two_words(self):
        script = self.run_js(
            "console.log(JSON.stringify(input.map(c => R.drift(c[0], c[1]))));\n")
        want = {"x": 960, "y": 0, "w": 1920, "h": 1080, "scale": 1, "transform": 0,
                "primary": False, "connectors": ["Virtual-2"]}
        cases = [[[want], [want]]]
        for field, bad in (("w", 1280), ("h", 720), ("scale", 2), ("transform", 1),
                           ("primary", True), ("connectors", ["Virtual-9"]),
                           ("x", 0), ("y", 8)):
            cases.append([[dict(want, **{field: bad})], [want]])
        got = self.call(script, cases)
        self.assertEqual(got[0], [])
        for diffs, case in zip(got[1:], cases[1:]):
            self.assertTrue(diffs, case)

    def test_the_pending_dialog_verdict_can_actually_refuse(self):
        """Demanded case by case, because the failure this replaces was a guard
        that returned "fine" for every input there is."""
        script = self.run_js(
            "const cases = {zero: 0, one: 1, two: 2, negative: -1,\n"
            "               missing: undefined, none: null, string: '1',\n"
            "               nan: NaN, fraction: 0.5, object: {}};\n"
            "const out = {};\n"
            "for (const k of Object.keys(cases)) out[k] = R.modalVerdict(cases[k]);\n"
            "console.log(JSON.stringify(out));\n")
        got = self.call(script, None)
        self.assertIsNone(got["zero"], got)
        for name in ("one", "two", "negative", "missing", "none", "string",
                     "nan", "fraction", "object"):
            self.assertIsInstance(got[name], str, name)
            self.assertTrue(got[name].strip(), name)
        # the refusal a user will actually see says what is at stake
        self.assertIn("monitors.xml", got["one"])
        self.assertIn("Keep changes?", got["missing"])
        # ...and what to do about a grab that is not on screen.  Measured once
        # on a real update: the first call after a post-update login refused
        # with nothing visible anywhere, and the next call seconds later
        # applied.  The check was left exactly as strict; the words that sent
        # the user looking for a dialog that was not there were not.
        self.assertIn("Main.modalCount is 1", got["one"])
        self.assertIn("Main.modalCount is 2", got["two"])
        self.assertIn("nothing on screen", got["one"])
        self.assertIn("run the same command again", got["one"])
        self.assertIn("Nothing was read and nothing was written", got["one"])

    def test_the_maps_lines_a_real_release_actually_writes(self):
        """Real lines, because the first cut of this matched none of them: a
        stable Ubuntu maps `libmutter-14.so.0.0.0`, and a pattern anchored on
        the soname reported "no build id" everywhere and looked like a library
        that would not say."""
        maps = "\n".join([
            "7bacc8200000-7bacc8250000 r--p 00000000 fd:02 534233   "
            "/usr/lib/x86_64-linux-gnu/libmutter-14.so.0.0.0",
            "7bacc8250000-7bacc8400000 r-xp 00050000 fd:02 534233   "
            "/usr/lib/x86_64-linux-gnu/libmutter-14.so.0.0.0",
            "7bacc745d000-7bacc7460000 r--p 00000000 fd:02 536272   "
            "/usr/lib/x86_64-linux-gnu/mutter-14/libmutter-cogl-pango-14.so.0.0.0",
            "7bacc7000000-7bacc7100000 r--p 00000000 fd:02 111111   "
            "/usr/lib/x86_64-linux-gnu/mutter-18/libmutter-18.so.0 (deleted)",
            "7fff00000000-7fff00021000 rw-p 00000000 00:00 0        [stack]",
            "",
        ])
        script = self.run_js(
            "console.log(JSON.stringify(R.parseMutterMappings(input)));\n")
        got = self.call(script, maps)
        self.assertEqual(got, [
            {"soname": "libmutter-14.so.0",
             "path": "/usr/lib/x86_64-linux-gnu/libmutter-14.so.0.0.0",
             "inode": 534233, "deleted": False},
            {"soname": "libmutter-18.so.0",
             "path": "/usr/lib/x86_64-linux-gnu/mutter-18/libmutter-18.so.0",
             "inode": 111111, "deleted": True},
        ])
        # cogl, clutter and the rest of the family are not libmutter
        self.assertEqual(self.call(script, "\n".join(maps.splitlines()[2:3])), [])

    def test_a_replaced_library_is_a_note_with_no_verdict_in_it(self):
        script = self.run_js(
            "console.log(JSON.stringify(input.map(i => R.libmutterNote(i))));\n")
        got = self.call(script, [None, {}, {"replaced": False, "path": "/x"},
                                 {"replaced": True, "path": "/lib/libmutter-14.so.0"}])
        self.assertEqual(got[:3], [None, None, None])
        self.assertIn("/lib/libmutter-14.so.0", got[3])
        self.assertIn("this changes nothing now", got[3])
        # it is news, not a refusal: it asks nothing of anybody now
        for imperative in ("Answer it", "run this again", "run the same command"):
            self.assertNotIn(imperative, got[3])

    def test_le32_is_little_endian_and_takes_a_negative(self):
        script = self.run_js(
            "console.log(JSON.stringify(input.map(v => R.le32(v))));\n")
        self.assertEqual(self.call(script, [0, 1, 960, 3840, -1, -960]),
                         [[0, 0, 0, 0], [1, 0, 0, 0], [192, 3, 0, 0], [0, 15, 0, 0],
                          [255, 255, 255, 255], [64, 252, 255, 255]])

    def test_the_extension_itself_is_a_module(self):
        """It cannot be run here -- it needs gi and a compositor -- but a syntax
        error in it would be discovered by a user's session refusing to start,
        which is exactly the moment this feature must not surprise anybody.

        Every import it makes is stubbed, and the rewrite fails loudly on an
        import that is not gi, not a gnome-shell resource and not the rules file
        beside it: this extension has no business importing anything else.
        """
        src = open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8").read()
        out, stubs = [], 0
        for line in src.splitlines(True):
            m = re.match(r"import (.+?) from '(.+?)';\s*$", line)
            if m is None:
                out.append(line)
                continue
            what, where = m.groups()
            if where == "./rules.js":
                out.append(line.replace("./rules.js", "./rules.mjs"))
                continue
            self.assertTrue(where.startswith("gi://")
                            or where.startswith("resource:///org/gnome/shell/"),
                            where)
            stubs += 1
            names = ([what[5:]] if what.startswith("* as ")
                     else re.findall(r"\w+", what) if what.startswith("{")
                     else [what])
            for name in names:
                # a class for what is extended, an object for what is called on
                out.append("class %s {}; %s.PACKAGE_VERSION = '50.1'; %s.wm = {};\n"
                           % (name, name, name))
        self.assertGreater(stubs, 3)
        stub = os.path.join(self.dir, "ext.mjs")
        with open(stub, "w") as fh:
            fh.write("".join(out))
        rc = subprocess.run(
            [NODE, "--input-type=module", "-e",
             "import('%s').then(m => console.log(typeof m.default))" % stub],
            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertEqual(rc.stdout.strip(), "function")


if __name__ == "__main__":
    unittest.main()
