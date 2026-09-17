"""U25: wxrandr against Cinnamon's Muffin, which is wxrandr's Mutter backend under three other names.

Muffin is a Mutter fork, and `gdbus introspect` on a live Cinnamon 6.4.13 gives `GetCurrentState` and
`ApplyMonitorsConfig` signatures that are byte-for-byte Mutter's -- `APPLY_SIG` applies unchanged
[M recon2/cinnamon.md §2.2, recon2/cinnamon/displayconfig-introspect.txt]. The recon prototype changed the
three constants and nothing else and drove a nested muffin with it: `--query`, `--listmonitors`, and an
`--output LVDS1 --mode 1024x768` that really applied and read back changed [M cinnamon.md §4]. So this file
is not a second backend's tests; it is the same backend's, asked in Cinnamon's name.

No `GetCurrentState` body was ever dumped, on either session, so neither fixture below is a recording and
this file does not call them one. The nested body is §4's `--query` block reconstituted as the reply that
would have produced it (four modes at 60.00, 800x600 current and preferred, no mm size); `renderer: dummy`
and layout-mode 2 are what a nested muffin on a box with no KMS reports, and they are asserted here, not
measured. The X11 body carries the two properties §3.1 did measure by hand -- `'renderer': <'xrandr'>` and
`'max-screen-size': <(1920, 1080)>` -- around a head that is Xvfb's `screen` as `warandr --command` reported
it there; `global-scale-required` is Mutter's X11 backend's flag and is unmeasured on muffin. Closing both
is batch 15's `cinnamon-wayland.sh`.

What it pins, all of it a difference a Cinnamon user would otherwise meet as a lie:

* both `GetCurrentState` bodies read back whole -- the nested one and the X11 session's, whose extra
  properties this backend has never read and must not trip over;
* the file a confirmed `--persistent` writes is `cinnamon-monitors.xml`, not `monitors.xml` (the string in
  libmuffin.so.0.0.0), and the dialog it warns about is Cinnamon's `Keep these display settings?`
  (`usr/share/cinnamon/js/ui/windowManager.js:77`) [M cinnamon.md §2.2];
* a refusal is relayed in Muffin's name. Muffin carries Mutter's validator with Mutter's own strings, so
  `Logical monitors not adjacent` arrives here word for word and the prefix is the only thing that says which
  program said no;
* `--unsafe-gnome-overlap` is refused on the flavour: the route reads a private `MetaMonitorsConfig` at
  offsets keyed on a Meta typelib version, and muffin's is `Meta-0` for every release ever made
  [M cinnamon.md §2.3].

The double is tests/test_wxrandr_mutter.py's FakeMutter on a MutterMockBus whose flavour is MUFFIN, so the
mock owns `org.cinnamon.Muffin.DisplayConfig` and nothing else -- a backend that reached for Mutter's name
would find no owner here.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common.dbus_mini import Bus
from test_wxrandr_mutter import M, FakeMutter, MutterMockBus
from wxrandr import cli
from hacks.display import gnome_overlap, monitors_xml, mutter
from hacks.display.core import State

os.environ["W11_PASSTHROUGH"] = "never"

MUFFIN = mutter.MUFFIN

# ---------------------------------------------------------------- the two recorded bodies
#
# The nested head, read off `wxrandr-cinnamon.py --query` against `muffin --wayland --nested` [M cinnamon.md
# §4]: four modes at 60.00 in this order, 800x600 current and preferred, no mm size in the reply at all
# (the query printed `0mm x 0mm`).
LVDS = ("LVDS1", "unknown", "unknown", "unknown")
XVFB = ("screen", "unknown", "unknown", "unknown")
NESTED_MONITORS = {
    "LVDS1": (LVDS, [M("1600x920@60.000", 1600, 920, 60.0, [1.0]),
                     M("1440x900@60.000", 1440, 900, 60.0, [1.0]),
                     M("1024x768@60.000", 1024, 768, 60.0, [1.0]),
                     M("800x600@60.000", 800, 600, 60.0, [1.0], preferred=True)],
               {"is-builtin": False, "display-name": "Unknown"}),
    # the X11 session's single Xvfb head, from the one place its geometry was written down: `warandr
    # --command` there printed `xrandr --output screen --mode 1920x1080 --pos 0x0 --rotate normal`, which
    # is xrandr's view of it and not Muffin's reply [M cinnamon.md §3.1]
    "screen": (XVFB, [M("1920x1080@60.000", 1920, 1080, 60.0, [1.0], preferred=True)],
               {"is-builtin": False, "display-name": "Unknown"}),
}


def nested():
    """`muffin --wayland --nested --no-x11`: one 800x600 head. §4's `--query` block turned back into the
    reply that would print it; the layout mode and `renderer: dummy` are asserted, not recorded (no
    GetCurrentState body was dumped on either session)."""
    svc = FakeMutter(["LVDS1"], [(0, 0, 1.0, 0, True, [("LVDS1", "800x600@60.000")])],
                     layout_mode=2, table=NESTED_MONITORS,
                     extra_props={"renderer": _v("s", "dummy")})
    return svc


def on_x11():
    """The X11 Cinnamon session, where Muffin's DisplayConfig is on the bus too and answers with
    `'renderer': <'xrandr'>` and `'max-screen-size': <(1920, 1080)>` -- those two values are §3.1's, read
    by hand off `gdbus call`. The head is Xvfb's `screen` as `warandr --command` printed it there, and
    `global-scale-required` is Mutter's X11 backend's flag, unmeasured on muffin: what this fixture is for
    is the two properties, and the point of the tests below is that the backend carries what it does not
    read."""
    svc = FakeMutter(["screen"], [(0, 0, 1.0, 0, True, [("screen", "1920x1080@60.000")])],
                     layout_mode=2, table=NESTED_MONITORS,
                     extra_props={"renderer": _v("s", "xrandr"),
                                  "max-screen-size": _v("(ii)", (1920, 1080))})
    svc.global_scale_required = True
    return svc


def _v(sig, value):
    from w11common.dbus_mini import Variant
    return Variant(sig, value)


class CinnamonCase(unittest.TestCase):
    """One mock bus per class, owning Muffin's name and no other."""

    @classmethod
    def setUpClass(cls):
        cls.mock = MutterMockBus(flavor=MUFFIN)

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def fixture(self):
        return nested()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-cinnamon-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_path = os.path.join(self.tmp, "state.json")
        self.opened = []
        self.mock.mutter = self.fixture()
        self._xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.tmp
        self.addCleanup(self._restore_xdg)

    def _restore_xdg(self):
        if self._xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._xdg

    def tearDown(self):
        for mo in self.opened:
            mo.close()
        self.mock.mutter = None

    @property
    def svc(self):
        return self.mock.mutter

    def outputs(self, flavor=MUFFIN):
        mo = mutter.MutterOutputs(bus=Bus(self.mock.address), wl_socket=False, flavor=flavor)
        self.opened.append(mo)
        return mo

    def run_cli(self, *argv, env=None):
        """cli.main with the Session pinned to a Muffin-flavoured backend on the mock bus. Backend
        *selection* is tests/test_wxrandr_backend.py:CinnamonProbe's; this is what happens after it."""
        tc = self

        def fake_init(sess, forced=None):
            sess.backend = cli.canonical_backend(forced) or "cinnamon"
            sess.impl = tc.outputs()
            sess.persistent = os.environ.get("WXRANDR_PERSIST", "") not in ("", "0")
            sess.state = State("cinnamon-test", path=tc.state_path)
        env = env or {}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        orig = cli.Session.__init__
        cli.Session.__init__ = fake_init
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
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

    def applied(self):
        return [c for c in self.svc.calls if c[1] != 0]


# ---------------------------------------------------------------- the flavour itself

class TheFlavour(unittest.TestCase):
    """The record, and the one place the rest of the tree has to agree with it.

    The three constant-equals-literal tests here cannot fail for any reason but an edit to the constant,
    which is the point: the plan asks for these bytes to be pinned because they are what a Cinnamon session
    answers on, and a typo in one of them is a backend that talks to nobody. The behaviour they stand for
    is asserted through the mock everywhere below."""

    def test_the_three_names_are_muffins(self):
        self.assertEqual(MUFFIN.dest, "org.cinnamon.Muffin.DisplayConfig")
        self.assertEqual(MUFFIN.path, "/org/cinnamon/Muffin/DisplayConfig")
        self.assertEqual(MUFFIN.iface, "org.cinnamon.Muffin.DisplayConfig")
        self.assertEqual(MUFFIN.match,
                         "type='signal',interface='org.cinnamon.Muffin.DisplayConfig',"
                         "member='MonitorsChanged'")

    def test_the_backend_selection_tables_name_the_same_bus(self):
        """wxrandr/cli.py's probe and this flavour have to reach the same service: the probe says a session
        is Cinnamon by owning this name, and the backend then talks to it."""
        self.assertEqual(cli.CINNAMON_DEST, MUFFIN.dest)
        self.assertIn(MUFFIN.name, cli.WAYLAND_BACKENDS)
        self.assertEqual(cli.BACKEND_ALIASES["muffin"], MUFFIN.name)
        order = list(cli.AUTO_ORDER)
        self.assertGreater(order.index("cinnamon"), order.index("mutter"))

    def test_gnomes_flavour_is_unchanged(self):
        """The regression guard for every other file in the tree: the module constants are still Mutter's."""
        self.assertEqual((mutter.DEST, mutter.PATH, mutter.IFACE),
                         (mutter.MUTTER.dest, mutter.MUTTER.path, mutter.MUTTER.iface))
        self.assertEqual(mutter.MUTTER.dest, "org.gnome.Mutter.DisplayConfig")
        self.assertEqual(mutter.MUTTER.config_name, monitors_xml.NAME)
        self.assertEqual(mutter._MATCH, mutter.MUTTER.match)

    def test_the_saved_file_is_cinnamons(self):
        self.assertEqual(MUFFIN.config_name, "cinnamon-monitors.xml")
        self.assertEqual(monitors_xml.default_path({"HOME": "/home/t"}, name=MUFFIN.config_name),
                         "/home/t/.config/cinnamon-monitors.xml")
        self.assertEqual(monitors_xml.default_path({"HOME": "/home/t"}),
                         "/home/t/.config/monitors.xml")

    def test_the_apply_signature_is_the_one_mutter_uses(self):
        """`gdbus introspect` on muffin 6.4.1: `ApplyMonitorsConfig(in u serial, in u method,
        in a(iiduba(ssa{sv})) logical_monitors, in a{sv} properties)` -- Mutter's, character for character
        [M recon2/cinnamon/displayconfig-introspect.txt]."""
        self.assertEqual(mutter.APPLY_SIG, "uua(iiduba(ssa{sv}))a{sv}")


class NoMuffinOnTheBus(CinnamonCase):
    def fixture(self):
        return nested()

    def test_a_bus_without_the_name_is_one_line_naming_cinnamon(self):
        self.mock.mutter = None
        with self.assertRaises(mutter.Fatal) as cm:
            mutter.MutterOutputs(bus=Bus(self.mock.address), wl_socket=False, flavor=MUFFIN)
        self.assertEqual(str(cm.exception.args[0]),
                         "org.cinnamon.Muffin.DisplayConfig is not on the session bus "
                         "(not a Cinnamon session?)\n")

    def test_the_gnome_flavour_finds_nothing_here(self):
        """A Cinnamon session owns Muffin's name and never Mutter's [M cinnamon.md §2.2], and the mock is
        the same way round: nothing in this file can be passing by accident of both names being served."""
        with self.assertRaises(mutter.Fatal) as cm:
            mutter.MutterOutputs(bus=Bus(self.mock.address), wl_socket=False)
        self.assertIn("org.gnome.Mutter.DisplayConfig is not on the session bus",
                      str(cm.exception.args[0]))
        self.assertIn("not a GNOME session?", str(cm.exception.args[0]))

    def test_probe_asks_for_the_flavours_name(self):
        bus = mutter.probe(self.mock.address, flavor=MUFFIN)
        self.assertIsNotNone(bus)
        bus.close()
        self.assertIsNone(mutter.probe(self.mock.address))


# ---------------------------------------------------------------- reading

class Query(CinnamonCase):
    def test_the_nested_head_renders_the_measured_block(self):
        """The bytes the recon prototype printed against a live nested muffin [M cinnamon.md §4]."""
        code, out, err = self.run_cli("--query")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "\n".join([
            "Screen 0: minimum 16 x 16, current 800 x 600, maximum 32767 x 32767",
            "LVDS1 connected primary 800x600+0+0 (normal left inverted right x axis "
            "y axis) 0mm x 0mm",
            "   1600x920      60.00  ",
            "   1440x900      60.00  ",
            "   1024x768      60.00  ",
            "   800x600       60.00*+",
        ]) + "\n")

    def test_the_provider_line_names_the_backend_the_user_chose(self):
        """`--listproviders` prints the compositor's name, and on Cinnamon that is `cinnamon`: the class
        attribute says `mutter`, which would name a compositor that is not running here."""
        code, out, err = self.run_cli("--listproviders")
        self.assertEqual(code, 0, err)
        self.assertIn("name:cinnamon", out)
        self.assertEqual(self.outputs().name, "cinnamon")
        self.assertEqual(mutter.MutterOutputs.name, "mutter")


class QueryOnX11(CinnamonCase):
    """The other recorded body. On an X11 Cinnamon session Muffin's DisplayConfig is on the bus as well and
    answers with `renderer: xrandr` and a `max-screen-size` -- two properties this backend has never read
    [M cinnamon.md §3.1]. Reading the body must not depend on knowing them."""

    def fixture(self):
        return on_x11()

    def test_the_x11_body_reads_back_whole(self):
        code, out, err = self.run_cli("--query")
        self.assertEqual(code, 0, err)
        self.assertIn("current 1920 x 1080", out)
        self.assertIn("screen connected primary 1920x1080+0+0", out)

    def test_the_unread_properties_are_carried_but_not_needed(self):
        mo = self.outputs()
        mo.snapshot(State("cinnamon-test", path=self.state_path))
        self.assertEqual(mo.props["renderer"], "xrandr")
        self.assertEqual(tuple(mo.props["max-screen-size"]), (1920, 1080))
        self.assertTrue(mo.global_scale_required)
        self.assertEqual(mo.layout_mode, mutter.LAYOUT_PHYSICAL)


# ---------------------------------------------------------------- applying

class Apply(CinnamonCase):
    def test_a_mode_change_is_one_applymonitorsconfig_and_reads_back(self):
        """`--output LVDS1 --mode 1024x768` -- rc 0 and `current 1024 x 768` on the next query, which is what
        the prototype measured against the real thing [M cinnamon.md §4]."""
        code, out, err = self.run_cli("--output", "LVDS1", "--mode", "1024x768")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.applied()), 1)
        serial, method, lms, props = self.applied()[0]
        self.assertEqual(method, mutter.TEMPORARY)
        self.assertEqual(lms, [(0, 0, 1.0, 0, True, [("LVDS1", "1024x768@60.000", {})])])
        code, out, err = self.run_cli("--query")
        self.assertIn("current 1024 x 768", out)

    def test_the_apply_went_out_under_the_documented_signature(self):
        """The mock refuses any other signature outright, so a byte-level assertion needs the mock's own
        record: it accepted the call, which it only does for APPLY_SIG."""
        code, out, err = self.run_cli("--output", "LVDS1", "--mode", "1024x768")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.svc.logical[0][5], [("LVDS1", "1024x768@60.000")])

    def test_a_refusal_is_relayed_in_muffins_name(self):
        """Muffin carries Mutter's validator with Mutter's strings [M cinnamon.md §2.2], so the refusal text
        arrives identical and the prefix is the only place the user learns who refused.

        `Monitor configuration via D-Bus is disabled` because it is the one refusal a single head can
        provoke; the geometry refusals need two, which this box could not measure on muffin (no KMS
        [M cinnamon.md §8]) and which the next test pins at the function that renders them."""
        self.svc.allowed = False
        code, out, err = self.run_cli("--output", "LVDS1", "--mode", "1024x768")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xrandr: Cinnamon's Muffin refused this layout: "
                              "Monitor configuration via D-Bus is disabled\n")

    def test_an_adjacency_refusal_carries_cinnamons_name_in_the_hint_too(self):
        """`Logical monitors not adjacent` is Muffin's string as much as Mutter's (both carry the same
        `meta_verify_logical_monitor_config_list`), and the hint after it advises about a rule that is
        Cinnamon's here, not GNOME's."""
        from w11common.dbus_mini import DBusError
        e = DBusError("org.freedesktop.DBus.Error.InvalidArgs", "Logical monitors not adjacent")
        self.assertEqual(mutter._refused(e, MUFFIN),
                         "Cinnamon's Muffin refused this layout: Logical monitors not adjacent"
                         " (Cinnamon allows neither a gap nor an overlap between outputs; "
                         "re-place the neighbours in the same command)\n")
        self.assertIn("GNOME's Mutter refused", mutter._refused(e))


class Persistent(CinnamonCase):
    """`--persistent` is the one path that opens a file, and on Cinnamon it is a different file."""

    def saved(self):
        return os.path.join(self.tmp, MUFFIN.config_name)

    def test_it_reads_and_backs_up_cinnamon_monitors_xml(self):
        with open(self.saved(), "w") as fh:
            fh.write("<monitors version=\"2\"/>\n")
        with open(os.path.join(self.tmp, monitors_xml.NAME), "w") as fh:
            fh.write("<monitors version=\"2\"><!-- GNOME's --></monitors>\n")
        code, out, err = self.run_cli("--output", "LVDS1", "--mode", "1024x768",
                                      "--persistent")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.applied()[0][1], mutter.PERSISTENT)
        backup = self.saved() + monitors_xml.BACKUP_SUFFIX
        self.assertTrue(os.path.exists(backup), err)
        with open(backup) as fh:
            self.assertEqual(fh.read(), "<monitors version=\"2\"/>\n")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, monitors_xml.NAME)
                                        + monitors_xml.BACKUP_SUFFIX))
        self.assertIn("cinnamon-monitors.xml", err)

    def test_it_warns_about_cinnamons_own_dialog(self):
        code, out, err = self.run_cli("--output", "LVDS1", "--mode", "1024x768",
                                      "--persistent")
        self.assertEqual(code, 0, err)
        self.assertIn('Cinnamon will ask "Keep these display settings?" for 20 s', err)
        self.assertNotIn("Keep changes?", err)

    def test_the_discarded_file_line_names_the_reader_that_discarded_it(self):
        """The whole claim of that sentence is that one reader takes the file or leaves it, so it has to
        say whose reader -- and on Cinnamon that is Muffin's, carrying Mutter's all-or-nothing parse
        [M cinnamon.md §2.2]. GNOME's wording is unchanged: `monitors_xml.describe`'s defaults are
        Mutter's."""
        bad = ('<monitors version="2">'
               '<configuration><logicalmonitor><x>0</x><y>0</y><primary>yes</primary>'
               '<monitor><monitorspec><connector>A</connector><vendor>v</vendor>'
               '<product>p</product><serial>s</serial></monitorspec>'
               '<mode><width>800</width><height>600</height><rate>60</rate></mode>'
               '</monitor></logicalmonitor>'
               '<logicalmonitor><x>100</x><y>0</y>'
               '<monitor><monitorspec><connector>B</connector><vendor>v</vendor>'
               '<product>p</product><serial>s</serial></monitorspec>'
               '<mode><width>800</width><height>600</height><rate>60</rate></mode>'
               '</monitor></logicalmonitor></configuration></monitors>\n')
        with open(self.saved(), "w") as fh:
            fh.write(bad)
        code, out, err = self.run_cli("--output", "LVDS1", "--mode", "1024x768",
                                      "--persistent")
        self.assertEqual(code, 0, err)
        self.assertIn("Cinnamon has already discarded", err)
        self.assertIn("Muffin's reader drops the whole file, not the one bad entry", err)
        line, = monitors_xml.describe(("/x/monitors.xml", bad.encode()))
        self.assertIn("GNOME has already discarded", line)
        self.assertIn("Mutter's reader drops the whole file", line)

    def test_the_layout_mode_warning_keeps_gnomes_own_label_for_the_switch(self):
        """`Fractional Scaling` is the name of the toggle in GNOME's Settings, quoted capitalised
        everywhere else in the tree (README.md, docs/Technical.md, hacks/input/layoutbox.py); only the desktop
        word is the flavour's."""
        self.assertIn("this session has Fractional Scaling off", mutter.LAYOUT_MODE_WARNING)
        self.assertIn("and GNOME then refuses the whole saved file",
                      mutter.LAYOUT_MODE_WARNING % "GNOME")
        self.assertIn("and Cinnamon then refuses the whole saved file",
                      mutter.LAYOUT_MODE_WARNING % MUFFIN.desktop)

    def test_a_temporary_apply_opens_no_file_at_all(self):
        with open(self.saved(), "w") as fh:
            fh.write("<monitors version=\"2\"/>\n")
        code, out, err = self.run_cli("--output", "LVDS1", "--mode", "1024x768")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.saved() + monitors_xml.BACKUP_SUFFIX))


# ---------------------------------------------------------------- the overlap route

class NoOverlapRoute(CinnamonCase):
    """`--unsafe-gnome-overlap` on Cinnamon. The flag exists because Mutter refuses overlapping layouts, and
    Muffin refuses them in the very same words -- so this is a user who really has the problem. The answer is
    not yet, with the rung that would close it: the GNOME route picks a struct description by Meta typelib
    version and muffin's GIR namespace is `Meta-0` and its library `libmuffin.so.0` for every release there
    has ever been [M cinnamon.md §2.3], so what a Cinnamon table would be keyed on is the Cinnamon release.
    `org.Cinnamon.Eval` reaches muffin with nothing installed, which is AGENTS.md rung 2 -- one below the
    extension GNOME needs -- and that is what the sentence names (reworded 2026-09-09)."""

    def test_the_refusal_names_cinnamon_and_the_typelib(self):
        mo = self.outputs()
        with self.assertRaises(mutter.Fatal) as cm:
            mo._overlap_client([])
        self.assertEqual(str(cm.exception.args[0]),
                         "--unsafe-gnome-overlap only means anything on GNOME; this is Cinnamon, "
                         "whose Meta-0 typelib has no generation to check; not yet here, and the route "
                         "is org.Cinnamon.Eval reaching MetaMonitorsConfig inside muffin with nothing "
                         "installed (AGENTS.md route 2), at the cost of an offset record measured per "
                         "Cinnamon release instead of per Meta generation\n")

    def test_overlap_available_answers_the_typelib_reason(self):
        """The backend method behind `--gnome-overlap-status`. It is not yet what the flag prints: today
        wxrandr/cli.py:1072 short-circuits on the backend token before the impl is asked, which is
        `TheEightCliSites` below."""
        mo = self.outputs()
        version, why = mo.overlap_available()
        self.assertIsNone(version)
        self.assertEqual(why, gnome_overlap.CINNAMON_REASON)

    def test_nothing_asks_gnome_shell_anything(self):
        """The landmine. The bus here owns Muffin's name only, so asking `org.gnome.Shell` for a version or
        `org.w11.Overlap` for a probe is a round trip whose answer could only ever be an error --
        and on a real Cinnamon session it is a round trip charged to somebody who typed a flag that was
        never going to work. Every destination the backend addresses is recorded."""
        mo = self.outputs()
        sent = []
        real = mo.bus.call

        def record(dest, *a, **kw):
            sent.append(dest)
            return real(dest, *a, **kw)
        mo.bus.call = record
        self.assertEqual(mo.overlap_available()[1], gnome_overlap.CINNAMON_REASON)
        with self.assertRaises(mutter.Fatal):
            mo._overlap_client([])
        self.assertEqual(sent, [])

    def test_the_gnome_flavour_still_reaches_the_route(self):
        """The control: the refusal is the flavour's, not something that now refuses everybody."""
        self.assertIsNone(gnome_overlap.not_gnome_reason("mutter"))
        self.assertEqual(gnome_overlap.not_gnome_reason("cinnamon"),
                         gnome_overlap.CINNAMON_REASON)


# ---------------------------------------------------------------- what the CLI still keys on the token
#
# wxrandr/cli.py belongs to another batch (1 landed it, 9 is in it now), so nothing below is fixed here.
# Every one of these is a `sess.backend == "mutter"` test in cli.py that the flavour does not reach, and
# every expected value was measured by running this file's own `run_cli` under the `mutter` token against
# the same three-head fixture -- so the right-hand side is not a wish, it is what GNOME gets today.
# The request, with the eight sites and the one rule that closes them (gate on `sess.impl`, not the token),
# is scratchpad/requests-batch-7.md item 1.


def three_heads():
    """eDP-1 | DP-1 | HDMI-1 in a row, the table tests/test_wxrandr_mutter.py measured GNOME 46/50 with.
    Cinnamon has no three-head measurement of its own (this box has no KMS and muffin has no
    `--virtual-monitor` [M cinnamon.md §8]); what these tests compare is the two tokens against each other
    on one fixture, which is exactly the difference cli.py introduces."""
    return FakeMutter(["eDP-1", "DP-1", "HDMI-1"],
                      [(0, 0, 1.0, 0, True, [("eDP-1", "1920x1080@60.020")]),
                       (1920, 0, 1.0, 0, False, [("DP-1", "2560x1600@59.972")]),
                       (4480, 0, 1.0, 0, False, [("HDMI-1", "1280x1024@60.020")])],
                      layout_mode=2)


class TheEightCliSites(CinnamonCase):
    """Eight behaviours in wxrandr/cli.py are chosen by `sess.backend == "mutter"`, and a Cinnamon session
    is a Muffin-flavoured MutterOutputs under the token `cinnamon`, so it misses all eight. Four are
    user-visible sentences and four are silence where GNOME speaks.

    These run the real `cli.main`, not the backend.  They were written as eight `expectedFailure`s -- the
    passing assertion, red against the tree that had the token test -- and landed green with the cli.py
    change of 2026-09-09 (batch 20): the four sentences now come off `sess.impl.flavor`, so GNOME's own
    bytes do not move, and the four silences speak.  The class was `TheCliStillKeysOnTheToken` while that was
    still the state; the history belongs here, where a failure line cannot send a reader the wrong way."""

    def fixture(self):
        return three_heads()

    def under_mutter(self, *argv):
        """The same command under the `mutter` token on the same fixture: the control every expected value
        below came from."""
        return self.run_cli("--backend", "mutter", *argv)

    def test_the_status_query_gives_the_typelib_reason(self):
        code, out, err = self.run_cli("--gnome-overlap-status")
        self.assertEqual(code, 0, err)
        self.assertIn("reason: %s" % gnome_overlap.CINNAMON_REASON, out)
        self.assertNotIn("places overlapping monitors without", out)

    def test_the_allow_command_gives_the_typelib_reason(self):
        code, out, err = self.run_cli("--gnome-overlap-allow")
        self.assertEqual(code, 1)
        self.assertIn(gnome_overlap.CINNAMON_REASON, err)

    def test_the_flag_gives_the_typelib_reason(self):
        code, out, err = self.run_cli("--unsafe-gnome-overlap",
                                      "--output", "DP-1", "--pos", "0x0")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xrandr: --unsafe-gnome-overlap only means anything on GNOME; %s\n"
                              % gnome_overlap.CINNAMON_REASON)

    def test_brightness_warns_and_succeeds_the_way_it_does_on_gnome(self):
        """Muffin has no LUT call either (its DisplayConfig is Mutter's), so the answer is GNOME's
        warn-and-succeed. Today the cinnamon token falls through to the wlr gamma path and the run dies
        `cannot set gamma: no wayland socket` -- a failure about a protocol this session never had."""
        self.assertEqual(self.under_mutter("--output", "eDP-1", "--brightness", "0.5")[0], 0)
        code, out, err = self.run_cli("--output", "eDP-1", "--brightness", "0.5")
        self.assertEqual(code, 0, err)
        self.assertIn("--brightness/--gamma are not supported on Muffin", err)
        self.assertNotIn("no wayland socket", err)

    def test_the_dryrun_plan_shows_the_neighbours_the_apply_will_shift(self):
        """`Session.positions()` runs `keep_adjacent` only for the mutter token, and that is what marks the
        shifted outputs `changed` so their crtc lines reach the plan and the screen size. The apply itself
        shifts them either way (that is mutter.py's, and it warns on both tokens), so on Cinnamon today the
        `--dryrun --verbose` block promises a screen 240 px wider than the one the run would leave."""
        _c, want, _e = self.under_mutter("--dryrun", "--verbose",
                                         "--output", "eDP-1", "--mode", "1680x1050")
        self.assertIn("screen 0: 5520x1600", want)
        code, out, err = self.run_cli("--dryrun", "--verbose",
                                      "--output", "eDP-1", "--mode", "1680x1050")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, want)

    def test_noprimary_says_cinnamon_keeps_one(self):
        code, out, err = self.run_cli("--output", "eDP-1", "--mode", "1920x1080", "--noprimary")
        self.assertEqual(code, 0, err)
        self.assertIn("Cinnamon requires a primary output; keeping eDP-1", err)

    def test_a_dryrun_says_which_compositor_verified_it(self):
        """The line is the backend token, so asking the flavour for it leaves GNOME's `mutter verify: ok`
        byte-identical (tests/test_wxrandr_mutter.py:1326 pins it) and gives Cinnamon `cinnamon verify: ok`.
        The verify really happened -- method 0 went to Muffin, which is what `applied()` records -- and a
        dry run that says nothing cannot be told from one that skipped it.

        No `under_mutter` control here: the token is stubbed but the impl is Muffin-flavoured either way,
        so the control would be reading this test's own answer back."""
        code, out, err = self.run_cli("--dryrun", "--output", "eDP-1", "--mode", "1680x1050")
        self.assertEqual(code, 0, err)
        self.assertEqual([c[1] for c in self.svc.calls], [mutter.VERIFY])
        self.assertIn("cinnamon verify: ok", err)

    def test_listmonitors_puts_the_primary_first(self):
        """RandR 1.5 lists the primary monitor first, and that is opt-in per backend because it is only
        observable where the compositor has a real primary XWayland knows about. Muffin has one -- it is
        Mutter's own `primary` flag on the logical monitor, which this backend already reads and syncs."""
        self.svc.logical[0] = (0, 0, 1.0, 0, False, [("eDP-1", "1920x1080@60.020")])
        self.svc.logical[1] = (1920, 0, 1.0, 0, True, [("DP-1", "2560x1600@59.972")])
        code, out, err = self.run_cli("--listmonitors")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[1].split()[1], "+*DP-1")

    def test_the_off_refusal_is_not_one_of_them(self):
        """The control, and the correction: `--output DP-1 --off` in the middle of a row is refused
        `Logical monitors not adjacent` on BOTH tokens -- keep_adjacent deliberately does not re-place an
        output whose neighbour went away (hacks/display/mutter.py's module docstring says so), so this is not
        a cli.py gap and no request asks for it to change."""
        want = self.under_mutter("--output", "DP-1", "--off")
        got = self.run_cli("--output", "DP-1", "--off")
        self.assertEqual(got[0], 1)
        self.assertEqual(got[2], want[2])
        self.assertIn("Logical monitors not adjacent", got[2])


if __name__ == "__main__":
    unittest.main()
