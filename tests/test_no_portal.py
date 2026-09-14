#!/usr/bin/env python3
"""The no-authorization-dialog guarantee, enforced statically.

GNOME and KDE both show a consent dialog to an application that injects
input through the desktop portal -- xdg-desktop-portal's RemoteDesktop and
InputCapture interfaces, the ones libei speaks -- and a polkit agent window
to anything that asks PolicyKit for an authorization. These tools promise
that neither ever appears: input goes through the kernel's `/dev/uinput` (as
root, or through the udev rule this repo ships), windows and displays go
through the compositor's own session-bus interfaces, and on wlroots input
goes through `zwp_virtual_keyboard_v1` / `zwlr_virtual_pointer_v1`. Not one
of those has a consent step. README.md's "No authorization dialog" section
states that and records the measurement behind it.

A measurement is a snapshot; this test is the ratchet. The day a package
grows a portal proxy, a libei backend or a PolicyKit action, that README
section stops being true and no dbus-monitor is watching -- so the suite
fails here instead, before it ships. A route that genuinely needs one of
these has to change the README section, the support matrix and this list in
the same commit, which is the point: the guarantee cannot be lost by
accident.

Two things the token list deliberately does *not* match, because our own
code says them to claim the opposite and that claim should stay greppable:
the bare word "portal" (`wmirror` names the portal in the message that
explains why it refuses to mirror on GNOME and KDE) and the bare word
"polkit" (`wdotool/backend_kwin.py` and `wxrandr/` note that KWin scripting
and `kde_output_management_v2` have no polkit action behind them).

**One portal interface is exempt, by name, and only one.**
`org.freedesktop.portal.Settings` reads the desktop's own GSettings and has
no consent step at all: it is what every GTK and Qt application calls at
start-up for the colour scheme, it is answered without a permission check,
and there is no allow/deny record for it in the portal's permission store.
`wdotool/xkbmap.py` calls its `ReadAll` on GNOME to learn which keyboard
layout is active, because Mutter will not tell an unfocused client and dconf
publishes no read method on the bus (`ca.desrt.dconf` has `Init`, `Change`
and `Notify` and nothing else). Measured on GNOME 46.0 and 50.1, with the
whole session bus and both screens watched by the method in README.md's
"No authorization dialog": the call answers in 1-3 ms and nothing appears on
screen. So `.Settings` -- and `org.freedesktop.portal.Desktop`, the bus name
it is reached at -- pass this scan, and every other interface on that same
bus name is still a failure here, RemoteDesktop and InputCapture first among
them. The exemption is one lookahead below, and widening it means changing
README.md's guarantee in the same commit, which is the point.

Not scanned: `tests/` -- this file names every token -- and `vm/`, whose rig
drives a real portal client and `pkexec` on purpose, as the positive
controls that prove a dialog would have been seen if there had been one.
"""

import os
import re
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This file spawns nothing, but the suite-wide guard wants the line in every
# test file and the guard is right: the day this one shells a tool, it is
# already here.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everything that runs on a user's machine as one of our commands: the six
# tools, and the package they all share.
PACKAGES = ("w11common", "wdotool", "wwmctl", "wxprop", "wxrandr", "warandr",
            "wmirror", "hacks")

# The other half of what a user installs: the GNOME Shell extension and the
# script that installs it and the udev rule. The extension runs inside
# gnome-shell and the installer runs under sudo, so both are places a
# consent prompt could be introduced. `.md` files are left out on purpose --
# documentation is allowed, and required, to name what we do not use.
EXTRA_DIRS = ("gnome",)
DOC_SUFFIXES = (".md",)

FORBIDDEN = (
    "org.freedesktop.impl.portal",  # the desktop's implementation of it
    "RemoteDesktop",                # the portal interface that injects input
    "InputCapture",                 # its newer sibling, same consent dialog
    "libei",                        # the transport both of them speak
    "PolicyKit",                    # a polkit action means an agent window
)

# The portal's public bus name and every interface on it EXCEPT the two the
# module docstring exempts: `Settings`, which has no consent step, and
# `Desktop`, which is the bus name Settings is reached at. Anything else
# under `org.freedesktop.portal` -- including a bare mention that names no
# interface -- is a hit. (The interface names that prompt are caught by
# that pattern where they are called; as bare words they are left alone,
# like "portal" and "polkit" above -- `wxrandr/kwin.py` names screencast
# in a comment about what KWin blacklists.)
PORTAL = r"org\.freedesktop\.portal(?!\.(?:Settings|Desktop)\b)"

# Case-insensitive: a different spelling is the same call.
_RE = re.compile("|".join([PORTAL] + [re.escape(t) for t in FORBIDDEN]),
                 re.IGNORECASE)

SKIP_DIRS = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo", ".png", ".gif", ".svg")


def _files(root, skip_docs=False):
    """Every text file under `root`, build and image droppings aside."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(SKIP_SUFFIXES):
                continue
            if skip_docs and name.endswith(DOC_SUFFIXES):
                continue
            out.append(os.path.join(dirpath, name))
    return out


def _hits_of(paths, rx):
    """(path, lineno, line) for every line of `paths` that `rx` matches."""
    found = []
    for path in paths:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if rx.search(line):
                    found.append((os.path.relpath(path, ROOT), n,
                                  line.strip()[:120]))
    return found


def _hits(paths):
    """(path, lineno, line) for every forbidden token in `paths`."""
    return _hits_of(paths, _RE)


def _report(hits):
    return "\n".join("%s:%d: %s" % h for h in hits)


class NoPortalNoPolkit(unittest.TestCase):
    """See the module docstring: this is the README's guarantee as a test."""

    def test_every_package_is_all_there(self):
        """A rename must not turn the scan below into a no-op."""
        for pkg in PACKAGES:
            self.assertTrue(os.path.isdir(os.path.join(ROOT, pkg)),
                            "package %s is gone: fix PACKAGES, or the "
                            "guarantee stops being checked for it" % pkg)

    def test_no_package_references_the_portal_or_polkit(self):
        paths = []
        for pkg in PACKAGES:
            paths += _files(os.path.join(ROOT, pkg))
        # Guard against a scan that walks nothing and passes vacuously.
        self.assertGreater(len(paths), len(PACKAGES))
        hits = _hits(paths)
        self.assertEqual(hits, [], "the tools must never reach for the "
                         "desktop portal or PolicyKit -- that is what puts "
                         "GNOME's and KDE's consent dialog on screen, and "
                         "README.md promises it never appears:\n"
                         + _report(hits))

    def test_the_settings_exemption_is_exactly_one_interface(self):
        """The one hole in the scan, pinned open at its own width. Settings
        reads the desktop's GSettings and prompts for nothing; every other
        interface on the same bus name is still a failure, and so is a bare
        mention that names no interface at all."""
        for allowed in ('org.freedesktop.portal.Settings.ReadAll',
                        'org.freedesktop.portal.Settings',
                        '"org.freedesktop.portal.Desktop"',
                        '/org/freedesktop/portal/desktop'):
            self.assertIsNone(_RE.search(allowed), allowed)
        for banned in ('org.freedesktop.portal.RemoteDesktop',
                       'org.freedesktop.portal.InputCapture',
                       'org.freedesktop.portal.ScreenCast',
                       'org.freedesktop.portal.Settingsy',
                       'org.freedesktop.portal.DesktopFoo',
                       'org.freedesktop.portal',
                       'org.freedesktop.impl.portal.Settings',
                       'polkit.PolicyKit1', 'libei_setup', 'RemoteDesktop'):
            self.assertIsNotNone(_RE.search(banned), banned)

    def test_the_only_portal_call_we_make_is_that_one(self):
        """And it is where the docstring says it is: one file, one
        interface. A second caller has to justify itself here first."""
        callers = sorted({h[0] for h in _hits_of(
            [f for pkg in PACKAGES for f in _files(os.path.join(ROOT, pkg))],
            re.compile(r"org\.freedesktop\.portal", re.IGNORECASE))})
        self.assertEqual(callers, ["hacks/input/xkbmap.py"])

    def test_the_extension_and_installer_do_not_either(self):
        """`gnome/` is installed too, and the installer runs as root."""
        paths = []
        for d in EXTRA_DIRS:
            paths += _files(os.path.join(ROOT, d), skip_docs=True)
        self.assertGreater(len(paths), 0)
        hits = _hits(paths)
        self.assertEqual(hits, [], "the bridge extension and its installer "
                         "must not introduce a prompt either:\n"
                         + _report(hits))

    def test_the_matcher_would_catch_a_real_one(self):
        """The ratchet is worthless if the pattern does not match."""
        samples = [
            'bus.call("org.freedesktop.portal.Desktop", "/org/freedesktop'
            '/portal/desktop", "org.freedesktop.portal.RemoteDesktop", '
            '"CreateSession", ...)',
            "from gi.repository import Libei",
            "pkaction = 'org.freedesktop.policykit.exec'",
            "org.freedesktop.impl.portal.InputCapture",
        ]
        for s in samples:
            self.assertTrue(_RE.search(s), s)
        # ... and does not fire on our own "we do not use it" comments.
        for s in ["no portal, no polkit, no security-context check",
                  "the desktop portal, which asks the user for permission",
                  "PKEXEC_UID"]:
            self.assertIsNone(_RE.search(s), s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
