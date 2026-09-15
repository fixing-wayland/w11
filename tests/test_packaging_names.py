#!/usr/bin/env python3
"""One logical dependency, four package names, five files that spell them.

`wxprop` hands over to the real xprop on an X11 session, and when it is not
there the tool says so and names the package to install.  That package is
called `x11-utils` on Debian, `xprop` on Fedora, `xorg-xprop` on Arch and
`xorg.xprop` in nixpkgs -- measured, all four -- and the same four names have to
appear in four other places: debian/control's Suggests, the spec's
Recommends/Suggests, the PKGBUILD's optdepends and the NixOS module's
x11Tools/wlMirror options.  Nine places, one fact, and until now nothing
connected them: the tools printed `apt install x11-utils` on Fedora, on Arch
and on NixOS, which has no apt at all [recon2/fedora.md, arch.md, nixos.md].

w11common/distro.py is the fifth column -- the table the running tool reads --
and this file is what keeps it and the four packagings saying the same thing.
The last test is the sharp one: a packaging that names ANOTHER distribution's
package (the spec saying x11-utils, the PKGBUILD saying xorg-x11-server-utils)
is a copy-paste that installs nothing and is invisible until somebody tries it.
"""

import os
import re
import sys
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common import distro                                       # noqa: E402

CONTROL = os.path.join(ROOT, "debian", "control")
SPEC = os.path.join(ROOT, "packaging", "rpm", "w11.spec")
PKGBUILD = os.path.join(ROOT, "packaging", "arch", "PKGBUILD")
MODULE = os.path.join(ROOT, "nix", "module.nix")

#: The table.  Row = the thing a user needs; column = the family; cell = what
#: that family calls it, exactly as w11common/distro.py says it, so the two are
#: comparable string for string.  Every cell was measured:
#:
#:   Fedora 44 (mdapi): /usr/bin/xprop -> `xprop` 1.2.8-5, /usr/bin/xrandr ->
#:   `xrandr` 1.5.3-4, `xdotool` 1:3.20211022.1-10, `wmctrl` 1.07-41,
#:   `wl-mirror` 0.18.5-1, and Gtk-3.0.typelib inside `gtk3` itself (Fedora has
#:   no gir1.2-* split) [recon2/pkg-rpm.md 2].
#:   Arch (`pacman -Si` in a bootstrap root): `xorg-xprop` 1.2.8-1,
#:   `xorg-xrandr` 1.5.4-1, `xdotool` 4.20260303.1-1, `wmctrl` 1.07-6,
#:   `wl-mirror` 0.18.5-1 (in extra, not a third-party repo), `python-gobject`
#:   3.56.3-1 + `gtk3` 1:3.24.52-1, and `acl` in core [recon2/pkg-arch.md 1].
#:   Debian's four are today's bytes in w11common/passthrough.py, unchanged.
TABLE = {
    "xdotool":     {"debian": "xdotool", "fedora": "xdotool",
                    "arch": "xdotool", "nixos": "nixpkgs.xdotool"},
    "wmctrl":      {"debian": "wmctrl", "fedora": "wmctrl",
                    "arch": "wmctrl", "nixos": "nixpkgs.wmctrl"},
    "xprop":       {"debian": "x11-utils", "fedora": "xprop",
                    "arch": "xorg-xprop", "nixos": "nixpkgs.xorg.xprop"},
    "xrandr":      {"debian": "x11-xserver-utils", "fedora": "xrandr",
                    "arch": "xorg-xrandr", "nixos": "nixpkgs.xorg.xrandr"},
    "wl-mirror":   {"debian": "wl-mirror", "fedora": "wl-mirror",
                    "arch": "wl-mirror", "nixos": "nixpkgs.wl-mirror"},
    "gtk3-python": {"debian": "python3-gi gir1.2-gtk-3.0",
                    "fedora": "python3-gobject gtk3",
                    "arch": "python-gobject gtk3",
                    "nixos": "nixpkgs.python3Packages.pygobject3 nixpkgs.gtk3"},
}

#: `acl` is the seventh logical dependency and the one no packaging declares:
#: it is in core on Arch and pulled in by systemd, absent from Fedora's cloud
#: image, and optional everywhere -- the removal scriptlets fall back to
#: `os.removexattr` when setfacl is not there.  So its row is a table of what
#: the INSTALL HINT would say, which is what distro.py's default rule produces,
#: and the assertion is that no packaging Requires it.
ACL = {"debian": "acl", "fedora": "acl", "arch": "acl", "nixos": "nixpkgs.acl"}

#: What each family's own file is.
FILES = {"debian": CONTROL, "fedora": SPEC, "arch": PKGBUILD, "nixos": MODULE}

#: Names that belong to another distribution.  A packaging that carries one of
#: these is a copy-paste, and the failure is silent: `dnf install x11-utils`
#: says "No match for argument" and the user reads it as our bug.
FOREIGN = {
    "fedora": ("x11-utils", "x11-xserver-utils", "python3-gi", "gir1.2-gtk-3.0",
               "xorg-xprop", "xorg-xrandr", "python-gobject"),
    "arch": ("x11-utils", "x11-xserver-utils", "python3-gi", "gir1.2-gtk-3.0",
             "python3-gobject", "xorg-x11-server-utils"),
    "debian": ("xorg-xprop", "xorg-xrandr", "python-gobject", "python3-gobject"),
}


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def code_of(path):
    """A packaging file with its comment lines dropped.

    Every one of these files explains its choices in prose that names the other
    distributions' packages on purpose, so the FOREIGN check has to read the
    declarations and not the reasons."""
    out = []
    for line in read(path).splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def control_field(name):
    """A debian/control field's entries, version constraints and substitution
    variables dropped."""
    text = read(CONTROL)
    m = re.search(r"^%s:(.*?)(?=^\S|\Z)" % name, text, re.M | re.S)
    if not m:
        raise AssertionError("%s: no %s field" % (CONTROL, name))
    out = []
    for entry in m.group(1).replace("\n", " ").split(","):
        entry = entry.strip()
        entry = re.sub(r"\s*\(.*?\)", "", entry)
        if entry and not entry.startswith("${"):
            out.append(entry)
    return out


def spec_tags(name):
    return re.findall(r"^%s: *(.+?)\s*$" % name, code_of(SPEC), re.M)


def arch_optdepends():
    """The package names out of optdepends, without their clauses."""
    m = re.search(r"^optdepends=\((.*?)\)\s*$", read(PKGBUILD), re.M | re.S)
    if not m:
        raise AssertionError("%s: no optdepends" % PKGBUILD)
    return [e.split(":")[0].strip() for e in re.findall(r"'([^']*)'", m.group(1))]


class TheFifthColumn(unittest.TestCase):
    """w11common/distro.py against the table above.

    distro.py is the column the running tool reads -- the one that decides what
    `wxprop` prints on a box with no xprop -- so if it and the packagings ever
    disagree, the message tells a user to install something the packaging does
    not name."""

    def test_every_cell_is_what_distro_py_answers(self):
        self.maxDiff = None
        for what, row in sorted(TABLE.items()):
            for family, name in sorted(row.items()):
                with self.subTest("%s/%s" % (what, family)):
                    self.assertEqual(distro.package(what, family), name)

    def test_the_table_covers_every_family_and_every_what(self):
        """Both directions: a family distro.py grew and this file did not know
        about is a column nobody checked, and a `what` in the table that
        distro.py does not carry would be tested against its default rule
        rather than against its row."""
        self.assertEqual(sorted(TABLE), sorted(distro.WHAT))
        for row in TABLE.values():
            self.assertEqual(sorted(row), sorted(distro.FAMILIES))

    def test_acl_falls_through_to_the_default_rule(self):
        """No packaging declares acl, so distro.py carries no row for it and
        the default rule spells it -- which on NixOS is `nixpkgs.acl` and
        everywhere else the bare name.  gnome/install-bridge.sh's two `apt
        install acl` hints are what read this."""
        for family, name in sorted(ACL.items()):
            with self.subTest(family):
                self.assertEqual(distro.package("acl", family), name)

    def test_the_hint_is_the_familys_own_installer(self):
        """The sentence a user actually sees.  `nix-env -iA nixpkgs.xorg.xprop`
        is the one that mattered most: a NixOS box was told `apt install
        xdotool` (measured)."""
        self.assertEqual(distro.hint("xprop", "debian"), "apt install x11-utils")
        self.assertEqual(distro.hint("xprop", "fedora"), "dnf install xprop")
        self.assertEqual(distro.hint("xprop", "arch"), "pacman -S xorg-xprop")
        self.assertEqual(distro.hint("xprop", "nixos"),
                         "nix-env -iA nixpkgs.xorg.xprop")


class EachPackagingNamesItsOwn(unittest.TestCase):
    """The four packaging files against their own column."""

    def test_debian_control_recommends_the_four_originals_and_suggests_wl_mirror(self):
        """The four moved from Suggests to Recommends with the X11 proxy: on a
        Wayland session the clones now hand over to the ORIGINAL running against
        xw11's display, so the original is what a default install should have --
        apt honours Recommends unless the admin turned it off, and W11_PROXY=never
        is how a user asks for the clones instead.  xprop and xrandr arrive as
        x11-utils and x11-xserver-utils, which is why the table has a debian cell
        for both although neither is a package name.  wl-mirror stays a Suggests:
        it is one tool's helper and nothing hands over to it."""
        self.assertEqual(sorted(control_field("Suggests")), ["wl-mirror"])
        self.assertEqual(sorted(control_field("Recommends")),
                         sorted(TABLE[w]["debian"] for w in
                                ("xdotool", "wmctrl", "xprop", "xrandr")))

    def test_debian_control_depends_on_the_gtk_pair_by_its_debian_names(self):
        """The one place a packaging differs from the others on purpose: the
        .deb makes the GTK stack a hard Depends where the spec and the PKGBUILD
        make it weak.  That divergence is design decision 5, and
        tests/test_release_deb.py carries it as an expectedFailure; what this
        test holds is only that the NAMES are Debian's."""
        depends = control_field("Depends")
        for name in TABLE["gtk3-python"]["debian"].split():
            with self.subTest(name):
                self.assertIn(name, depends)

    def test_the_spec_suggests_wl_mirror_and_recommends_the_four(self):
        """The same move the .deb made, in Fedora's names: dnf honours weak
        dependencies by default, so `dnf install w11` on a Wayland box arrives
        with the originals the clones hand over to."""
        self.assertEqual(spec_tags("Suggests"), [TABLE["wl-mirror"]["fedora"]])
        self.assertEqual(spec_tags("Recommends"),
                         TABLE["gtk3-python"]["fedora"].split()
                         + [TABLE[w]["fedora"] for w in
                            ("xdotool", "wmctrl", "xprop", "xrandr")])

    def test_the_pkgbuild_optdepends_are_archs(self):
        """gnome-shell is the sixth: it is not in the table because there is
        one name for it everywhere and no install hint prints it."""
        got = arch_optdepends()
        want = [TABLE[w]["arch"] for w in
                ("xdotool", "wmctrl", "xprop", "xrandr", "wl-mirror")]
        want += TABLE["gtk3-python"]["arch"].split()
        for name in want:
            with self.subTest(name):
                self.assertIn(name, got)
        self.assertEqual(sorted(set(got) - set(want)), ["gnome-shell"])

    @unittest.skipUnless(os.path.exists(MODULE), "nix/module.nix is batch 4's")
    def test_the_nixos_module_names_the_same_five(self):
        """nixpkgs' attribute path, not a package name -- `pkgs.xorg.xprop` and
        `pkgs.wl-mirror` -- which is what distro.py's nixos column spells with
        its `nixpkgs.` prefix, because that is what `nix-env -iA` takes."""
        code = code_of(MODULE)
        for what in ("xdotool", "wmctrl", "xprop", "xrandr", "wl-mirror"):
            with self.subTest(what):
                attr = TABLE[what]["nixos"][len("nixpkgs."):]
                self.assertTrue(
                    re.search(r"pkgs\.%s\b" % re.escape(attr), code)
                    or re.search(r"pkgs\.%s\b" % re.escape(attr.split(".")[-1]), code),
                    "%s: neither pkgs.%s nor pkgs.%s" % (what, attr, attr.split(".")[-1]))

    def test_no_packaging_requires_acl(self):
        """The removal scriptlets fall back to `os.removexattr` when setfacl is
        not on PATH, and Fedora's cloud image has no acl at all (measured).
        Requiring it would put a package on every machine to undo a grant it
        may never have made."""
        self.assertNotIn("acl", control_field("Depends"))
        self.assertEqual([v for v in spec_tags("Requires") if v.strip() == "acl"], [])
        self.assertNotIn("acl", arch_optdepends())


class NoPackagingNamesAnothersPackage(unittest.TestCase):
    """The sharp one.  A spec that says `x11-utils` installs nothing on Fedora
    and says so in a sentence the user reads as our bug."""

    def test_each_packaging_is_free_of_the_others_names(self):
        self.maxDiff = None
        for family, names in sorted(FOREIGN.items()):
            code = code_of(FILES[family])
            for name in names:
                with self.subTest("%s/%s" % (family, name)):
                    self.assertNotIn(name, code)

    def test_the_check_reads_declarations_and_not_the_comments(self):
        """Every one of these files explains its choices in prose that names
        the other distributions on purpose -- the spec's Suggests comment says
        "where Debian has x11-utils and x11-xserver-utils" -- so a FOREIGN scan
        over the raw text would fail on the explanation.  If code_of() stopped
        dropping comments, the test above would go red for the right words in
        the wrong place, and this is what says which."""
        self.assertIn("x11-utils", read(SPEC))
        self.assertNotIn("x11-utils", code_of(SPEC))


if __name__ == "__main__":
    unittest.main()
