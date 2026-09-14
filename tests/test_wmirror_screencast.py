#!/usr/bin/env python3
"""The route wmirror names when there is no wl-mirror capture protocol -- and it is not the same on all three
desktops that lack one.

wl-mirror needs wlroots' `zwlr_screencopy_manager_v1` or the standard `ext_image_copy_capture_manager_v1`.
GNOME, KDE and Cinnamon publish neither. The answer for each is a route, and the routes differ:

  * **GNOME and KDE**: the desktop portal's ScreenCast (AGENTS.md route 4) -- it exists (GNOME's
    `org.gnome.Mutter.ScreenCast` through `xdg-desktop-portal-gnome`, KDE's through `xdg-desktop-portal-kde`)
    and only wants wiring, which asks the user once per session. Still a not-yet: unwired here.
  * **Cinnamon**: NOT ScreenCast. muffin has the ScreenCast machinery compiled out, measured on the
    `resolute-cinnamon-wayland` golden (Cinnamon 6.4.13 / muffin 6.4.1), 2026-09-14 -- there is no
    `org.cinnamon.Muffin.ScreenCast` on the session bus (not acquired, not activatable, no `.service` file;
    `gdbus introspect` -> `ServiceUnknown ... not provided by any .service files`), `org.freedesktop.portal`
    exposes `Screenshot` but no `ScreenCast`/`RemoteDesktop` (interface count 0) and neither impl backend
    (`.xapp`, `.gtk`) serves ScreenCast, and `libmuffin.so.0.0.0` carries only a vestigial
    `MetaScreenCastWindow`. So route 4 (the portal) is empty here and route 1 (a protocol) never existed.

The old conclusion drawn from those facts was "the lowest reachable rung is 6 -- a muffin built with
screen-cast enabled." That was wrong: rung 2 was never probed. Cinnamon has `org.Cinnamon.Eval` (the same
ungated arbitrary-JS surface the window plane uses), and a `Clutter.Clone` of the on-screen actors reproduces
a region on another head byte-identically with no capture at all -- measured AE 0 / RMSE 0 on the same golden
(hacks/mirror/cinnamon.py, tested in tests/test_wmirror_cinnamon.py, pixel-checked in
vm/live-smoke.d/cinnamon-wayland.sh). So `wmirror` mirrors on Cinnamon over route 2, the start and `--check`
take that path *before* this no-capture refusal is ever built, and Cinnamon is NOT named in it.

There is no bus client to fake here: what is under test is that `wmirror` names the honest route for GNOME and
KDE and does NOT push a Cinnamon user at a ScreenCast (route 4) or a rebuilt muffin (route 6) that the Eval
clone made unnecessary.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ["W11_PASSTHROUGH"] = "never"

from hacks.mirror import cinnamon, core  # noqa: E402


class NoCaptureRoute(unittest.TestCase):
    def setUp(self):
        self.lines = core.no_capture_lines()
        self.joined = " ".join(self.lines)

    def test_gnome_and_kde_stay_the_rung_4_portal(self):
        """The desktops whose portal really serves ScreenCast keep route 4, unchanged."""
        self.assertIn("GNOME and KDE", self.joined)
        self.assertIn("portal", self.joined)
        self.assertIn("AGENTS.md route 4", self.joined)
        self.assertIn("not wired up here yet", self.joined)

    def test_cinnamon_is_not_a_no_capture_refusal(self):
        """Cinnamon mirrors over the Eval clone, so the start and --check take that path before this refusal is
        built. It must not appear in the no-capture message at all -- naming it here would send a Cinnamon user
        looking for a route they do not need."""
        self.assertNotIn("Cinnamon", self.joined)
        self.assertNotIn("muffin", self.joined)

    def test_no_route_6_for_cinnamon_anywhere_the_tool_prints(self):
        """The rejected conclusion: route 6, a muffin built with screen-cast. The Eval clone (route 2) closed
        the gap, so neither the refusal nor the capture-route label may point at route 6 or a rebuilt muffin."""
        self.assertNotIn("route 6", self.joined)
        self.assertNotIn("screen-cast enabled", self.joined)
        self.assertNotIn("route 6", cinnamon.ROUTE)

    def test_cinnamons_route_label_is_the_eval_clutter_clone_route_2(self):
        """What `--check` and `--list` call the Cinnamon capture path: the Eval clone, named as route 2."""
        self.assertIn("Eval", cinnamon.ROUTE)
        self.assertIn("Clutter", cinnamon.ROUTE)
        self.assertIn("route 2", cinnamon.ROUTE)

    def test_the_first_two_lines_are_untouched(self):
        """The capture-protocol names and the wlroots/extcopy line are the ones two other tests and the live
        smoke pin; nothing here moves them."""
        self.assertIn(core.SCREENCOPY, self.lines[0])
        self.assertIn(core.EXTCOPY, self.lines[0])
        self.assertIn(core.SCREENCOPY, self.lines[1])
        self.assertIn(core.EXTCOPY, self.lines[1])
        self.assertIn("COSMIC", self.lines[1])


if __name__ == "__main__":
    unittest.main()
