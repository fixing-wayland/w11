#!/usr/bin/env python3
"""Detection on a desktop that owns org.gnome.Shell without being GNOME.

CI run 34308982263 (commit 4d6fd40) failed every window check on resolute-budgie with one line:

    gnome backend: the w11 bridge extension is installed in
    /usr/share/gnome-shell/extensions/w11-bridge@w11 but the running GNOME Shell has not
    loaded it (it knows nothing about w11-bridge@w11): log out and back in

There is no GNOME Shell on that flavor.  Ubuntu Budgie 10.10.2 is labwc under a panel, and
`busctl --user list --no-pager --acquired` in the guest on 2026-09-09 (recorded whole in
tests/fixtures/live/busnames-resolute-budgie-10.10.2.txt) shows `org.gnome.Shell` owned by
budgie-power-dialog, pid 2697, connection :1.85 -- and no `org.gnome.Mutter.*` name anywhere on that bus.
`detect()` read the one name as "this is GNOME" and stopped, on a session whose compositor advertises
zwlr_foreign_toplevel_manager_v1 and whose windows the wlr backend lists fine (61 pass / 0 fail on the rig
after the fix, against 35 pass / 10 fail in that CI run).

Two questions per test here, both about ONE ListNames answer and one registry: which arm a name list lands
on, and how many round trips it took to get there.  The bus is `MockBus` and the compositor is a
`wl_fake.registry_server` replaying a recorded global list, so nothing below is a stub agreeing with itself.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py.  This line is what covers `python3 tests/<file>.py`, where conftest is not
# loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from w11common import session
from w11common.dbus_mini import Bus
from w11common.errors import CmdError
from support import env
from test_dbus_mini import MockBus
from wdotool import backend_detect

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
BUDGIE_NAMES = os.path.join(FIXTURES, "live", "busnames-resolute-budgie-10.10.2.txt")


def recorded_names(path=BUDGIE_NAMES) -> list:
    """The well-known names the fixture recorded, comments stripped."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


class BudgieIsNotGnome(unittest.TestCase):
    """One MockBus per test, owning the names the guest was measured owning.

    `session.RUN_USER_DIR` is a private empty tree and WAYLAND_DISPLAY is cleared, so the box the suite runs
    on cannot decide any of this; a test that wants a compositor points WAYLAND_DISPLAY at its own fake."""

    def setUp(self):
        backend_detect.reset()
        self.addCleanup(backend_detect.reset)
        self.rundir = tempfile.mkdtemp(prefix="wdotool-budgie-run-")
        self.addCleanup(shutil.rmtree, self.rundir, ignore_errors=True)
        p = mock.patch.object(session, "RUN_USER_DIR", self.rundir)
        p.start()
        self.addCleanup(p.stop)
        self.mock = MockBus()
        self.addCleanup(self.mock.close)
        self.rtdir = tempfile.mkdtemp(prefix="wdotool-budgie-rt-")
        self.addCleanup(shutil.rmtree, self.rtdir, ignore_errors=True)
        ctx = env(DBUS_SESSION_BUS_ADDRESS=self.mock.address, XDG_RUNTIME_DIR=self.rtdir,
                  WAYLAND_DISPLAY=None, SWAYSOCK=None, I3SOCK=None, WDOTOOL_BACKEND=None,
                  HYPRLAND_INSTANCE_SIGNATURE=None, SUDO_UID=None, PKEXEC_UID=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.made = []
        for name in ("_sway", "_hypr", "_wayfire", "_wlr", "_cosmic", "_kwin", "_gnome", "_cinnamon"):
            self._replace(name)

    def _replace(self, name):
        real = getattr(backend_detect, name)
        self.addCleanup(setattr, backend_detect, name, real)

        def maker(_name=name):
            self.made.append(_name)
            raise CmdError("%s: stub" % _name[1:])
        setattr(backend_detect, name, maker)

    def own(self, *names):
        """Claim `names` on the mock bus from one connection, the way one daemon owning several does."""
        bus = Bus(self.mock.address)
        self.addCleanup(bus.close)
        for n in names:
            self.assertEqual(bus.request_name(n), 1, n)
        return bus

    def compositor(self, fixture):
        self.fake = wl_fake.registry_server(fixture)
        self.addCleanup(self.fake.close)
        ctx = env(WAYLAND_DISPLAY=self.fake.path)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return self.fake

    # -- the fixture is the measurement, and it has to still say what the test claims
    def test_the_recorded_budgie_bus_owns_the_shell_name_and_nothing_of_mutters(self):
        names = recorded_names()
        self.assertIn(backend_detect.GNOME_NAME, names)
        self.assertNotIn(backend_detect.MUTTER_NAME, names)
        self.assertNotIn(backend_detect.BRIDGE_NAME, names)
        self.assertEqual([n for n in names if n.startswith("org.gnome.Mutter")], [])
        # ...and it really is Budgie's own bus and not an edited one
        self.assertIn("org.buddiesofbudgie.Services", names)
        self.assertIn("org.budgie_desktop.Panel", names)

    # -- the arm each shape of session lands on
    def test_budgies_names_over_labwcs_registry_land_on_wlr(self):
        """The CI failure. Every name the guest owned, and the registry labwc really advertises."""
        self.own(*recorded_names())
        self.compositor("budgie")
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertEqual(self.made, ["_wlr"], "org.gnome.Shell alone still sends Budgie to the GNOME arm")

    def test_gnomes_own_second_name_keeps_the_gnome_arm(self):
        """Mutter's DisplayConfig beside org.gnome.Shell: GNOME, and the registry is never asked."""
        self.own(backend_detect.GNOME_NAME, backend_detect.MUTTER_NAME)
        self.compositor("budgie")
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertEqual(self.made, ["_gnome"])
        self.assertIsNone(backend_detect._registry_conn,
                          "a GNOME session paid for a registry round trip it does not need")

    def test_our_bridge_alone_keeps_the_gnome_arm(self):
        """org.w11.Bridge can only be owned from inside gnome-shell, so it answers on its own --
        which is what a session whose Mutter name is somehow not up yet needs."""
        self.own(backend_detect.GNOME_NAME, backend_detect.BRIDGE_NAME)
        self.compositor("budgie")
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertEqual(self.made, ["_gnome"])

    def test_a_shell_name_with_no_toplevel_protocol_still_gets_the_bridge_hint(self):
        """The GNOME arm is still not swallowed where nothing below it could answer: Muffin advertises 23
        globals and no foreign-toplevel protocol of either flavour, so a session shaped like that keeps the
        install hint rather than being told the compositor offers neither family."""
        self.own(backend_detect.GNOME_NAME)
        self.compositor("cinnamon")
        with self.assertRaises(CmdError) as caught:
            backend_detect.detect()
        self.assertEqual(self.made, ["_gnome"])
        self.assertIn("gnome: stub", str(caught.exception))

    def test_a_shell_name_over_a_cosmic_registry_lands_on_cosmic(self):
        """The other half of the fall-through, and the reason `_toplevel_family()` is ONE function.

        The GNOME arm asks "does this compositor publish a foreign-toplevel protocol" one step above the
        arms that then pick the backend from the answer, and cosmic-comp's answer is the awkward one: no
        `zwlr_foreign_toplevel_manager_v1` at all, `ext_foreign_toplevel_list_v1` + `zcosmic_toplevel_info_v1`
        instead [M recon2/cosmic.md §2, §3].  If the two readings ever disagree about that pair, a
        cosmic-comp session that also owns org.gnome.Shell gets the bridge install hint on a box with no
        gnome-shell -- the Budgie failure again, one desktop over.  Both readings run here, in one detect()."""
        self.own(backend_detect.GNOME_NAME)
        self.compositor("cosmic")
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertEqual(self.made, ["_cosmic"])

    def test_kwin_still_beats_a_shell_name_that_is_not_gnomes(self):
        """A Plasma box that also owns org.gnome.Shell is Plasma, and the KWin arm is above all of this."""
        self.own(backend_detect.KWIN_NAME, backend_detect.GNOME_NAME)
        self.compositor("budgie")
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertEqual(self.made, ["_kwin"])

    def test_the_whole_detection_is_one_listnames_and_one_registry(self):
        """The fall-through costs a round trip, not a round trip per arm: session_names() is the same object
        afterwards and the registry connection is the one session_conn() hands the backend."""
        self.own(*recorded_names())
        self.compositor("budgie")
        before = backend_detect.session_names()
        with self.assertRaises(CmdError):
            backend_detect.detect()
        self.assertIs(backend_detect.session_names(), before)
        self.assertEqual(before.count(backend_detect.GNOME_NAME), 1)
        self.assertEqual(self.fake.connections, 1)


class SessionRegistryRetries(unittest.TestCase):
    """A2: a momentary connect/roundtrip failure or an empty registry is retried once before the
    session is called unreachable.  Under CI load a single failure here reported COSMIC as "offers
    neither wlr-foreign-toplevel nor the COSMIC toplevel protocols" on a golden whose backend is
    fine by hand [arch-cosmic, CI run 34676441862]."""

    def setUp(self):
        backend_detect.reset()
        self.addCleanup(backend_detect.reset)
        # a socket the finder will name; the fake WlConn below never touches it
        self.hit = (1000, "/run/user/1000", "/run/user/1000/wayland-1")
        p = mock.patch.object(session, "find_wayland_socket", return_value=self.hit)
        p.start(); self.addCleanup(p.stop)

    def _install(self, sequence):
        """`sequence` is a list of get_registry() outcomes, one per WlConn() built: an Exception
        instance to raise from get_registry, or a {name: (iface, ver)} dict to return."""
        self.built = 0
        outer = self

        class FakeConn:
            def __init__(self, path):
                outer.built += 1
                self._outcome = sequence[outer.built - 1]

            def get_registry(self):
                if isinstance(self._outcome, Exception):
                    raise self._outcome
                return self._outcome

            def close(self):
                pass

        import w11common.wayland_mini as wm
        p = mock.patch.object(wm, "WlConn", FakeConn)
        p.start(); self.addCleanup(p.stop)

    def test_a_transient_connect_failure_is_retried_and_then_succeeds(self):
        self._install([OSError("momentary"),
                       {1: ("wl_compositor", 6), 2: ("ext_foreign_toplevel_list_v1", 1)}])
        reg = backend_detect.session_registry()
        self.assertEqual(self.built, 2, "it did not retry")
        self.assertEqual(reg, {"wl_compositor": 6, "ext_foreign_toplevel_list_v1": 1})

    def test_an_empty_registry_is_retried_and_then_succeeds(self):
        self._install([{}, {1: ("wl_compositor", 6)}])
        reg = backend_detect.session_registry()
        self.assertEqual(self.built, 2, "an empty registry is an unfinished roundtrip, not an answer")
        self.assertEqual(reg, {"wl_compositor": 6})

    def test_two_failures_in_a_row_give_up_and_report_none(self):
        self._install([OSError("one"), OSError("two")])
        self.assertIsNone(backend_detect.session_registry())
        self.assertEqual(self.built, 2, "exactly two attempts, no more")

    def test_a_first_try_success_does_not_pay_the_backoff(self):
        self._install([{1: ("wl_compositor", 6)}])
        self.assertEqual(backend_detect.session_registry(), {"wl_compositor": 6})
        self.assertEqual(self.built, 1, "a good first read is not retried")


if __name__ == "__main__":
    unittest.main()
