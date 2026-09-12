#!/usr/bin/env python3
"""The .pkg.tar.zst `sh scripts/build-pkgbuild.sh` produces, against the tree.

The Arch half of tests/test_release_deb.py and tests/test_release_rpm.py: what
a user installs is a binary, and nothing in the tree makes a binary agree with
it.  Here that gap is wider than the .deb's, because makepkg cannot run in this
sandbox at all -- it needs /proc for bash process substitution, and dsb refuses
every mount (`/dev/fd/63: No such file or directory`, then fakeroot "nested
operation not yet supported") [recon2/pkg-arch.md 2].  So the recipe was
executed by hand there, and everything below is written for the CI `pkgbuild`
job in archlinux:base-devel, which has a real /proc and a real makepkg.

Four questions the hermetic tests/test_pkgbuild.py cannot answer:

  * does the package contain the tree, file for file;
  * is the console-script shebang `#!/usr/bin/python`?  It read
    `#!/usr/sbin/python` in the recon's bootstrap chroot, because that chroot
    merges /usr/sbin into /usr/bin and `bash -l` put /usr/sbin first -- an
    artefact of the sandbox, and exactly the sort of thing a real build settles;
  * what does `pacman -Qi` say about Architecture, Depends and Licenses;
  * does namcap print anything packaging/arch/namcap.expected does not name?

bsdtar and pacman are not standard library and are not on a non-Arch box, so
every class here skips without them.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common import VERSION                                      # noqa: E402

PKGDIR = os.environ.get("W11_PKG_DIR") or os.path.join(ROOT, "dist")
PKGBUILD = os.path.join(ROOT, "packaging", "arch", "PKGBUILD")
NAMCAP_EXPECTED = os.path.join(ROOT, "packaging", "arch", "namcap.expected")
BRIDGE_UUID = "w11-bridge@w11"
OVERLAP_UUID = "w11-overlap@w11"
EXT = "usr/share/gnome-shell/extensions"

#: Every non-Python file the package carries, paired with the file in the tree
#: it is a copy of.  Arch keeps dpkg's /usr/lib/w11 for the helper (it
#: has no /usr/libexec convention), so this is test_release_deb.py's table with
#: one path re-pointed -- and the autostart entry IS a byte copy here, unlike
#: the rpm's, because nothing rewrites its Exec= line.
PAIRS = {
    "usr/lib/udev/rules.d/60-w11-uinput.rules":
        "gnome/60-w11-uinput.rules",
    "usr/lib/modules-load.d/w11-uinput.conf":
        "gnome/modules-load-uinput.conf",
    "usr/lib/w11/enable-bridge": "packaging/common/enable-bridge",
    "etc/xdg/autostart/w11-enable-bridge.desktop":
        "packaging/common/enable-bridge.desktop",
    "usr/share/applications/warandr.desktop": "warandr.desktop",
    # BSD-2-Clause is not one of /usr/share/licenses/common/, so the package
    # carries the text.  Byte for byte, because a licence file that is not the
    # project's licence file is the one payload error nobody looks for.
    "usr/share/licenses/w11/LICENSE": "LICENSE",
}

TOOLS = ("warandr", "wdotool", "wmirror", "wwmctl", "wxprop", "wxrandr", "xw11")


def load_gen_gir():
    """gnome/overlap-typelib/gen-gir.py as a module: it has a hyphen in its
    name and lives outside any package, so it is loaded by path.  Same loader
    as tests/test_gnome_overlap.py's, and for the same reason -- the rules for
    what a typelib means live in one file and no test may keep its own copy."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gen_gir", os.path.join(ROOT, "gnome", "overlap-typelib", "gen-gir.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def find():
    if not os.path.isdir(PKGDIR):
        return []
    return sorted(os.path.join(PKGDIR, n) for n in os.listdir(PKGDIR)
                  if n.startswith("w11-%s-" % VERSION)
                  and ".pkg.tar." in n)


class PkgCase(unittest.TestCase):
    """The built package, or a skip.

    Per test and not per class: a class-level skip reports `Ran 0 tests`, and
    the suite's own test count (R26) would then depend on whether the machine
    happens to have bsdtar and a built package.  Skipped tests still count."""

    def setUp(self):
        if shutil.which("bsdtar") is None:
            self.skipTest("no bsdtar (libarchive) to read a .pkg.tar.zst")
        found = find()
        if not found:
            self.skipTest("no w11 %s package in %s (sh scripts/build-pkgbuild.sh)"
                          % (VERSION, PKGDIR))
        self.pkg = found[0]

    def members(self):
        got = subprocess.run(["bsdtar", "-tf", self.pkg], capture_output=True,
                             text=True, timeout=300)
        self.assertEqual(got.returncode, 0, got.stderr)
        return sorted(n.rstrip("/") for n in got.stdout.split()
                      if n and not n.startswith("."))

    def member(self, name):
        got = subprocess.run(["bsdtar", "-xOf", self.pkg, name],
                             capture_output=True, timeout=300)
        self.assertEqual(got.returncode, 0, got.stderr[-500:])
        return got.stdout

    def unpack(self):
        tmp = tempfile.mkdtemp(prefix="w11-pkg-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        subprocess.run(["bsdtar", "-xf", self.pkg], cwd=tmp, check=True, timeout=300)
        return tmp


class ThePayload(PkgCase):

    def test_every_module_in_it_is_the_one_in_the_tree(self):
        """Arch's site-packages is version-pinned (/usr/lib/python3.14), which
        is why a built package is a CI artefact and never a release file; the
        directory is therefore found rather than named."""
        tmp = self.unpack()
        sites = [os.path.join(base, d)
                 for base, dirs, _n in os.walk(tmp) for d in dirs
                 if d == "site-packages"]
        self.assertEqual(len(sites), 1, sites)
        seen = 0
        for base, dirs, names in os.walk(sites[0]):
            dirs[:] = [d for d in dirs
                       if d != "__pycache__" and not d.endswith(".dist-info")]
            for n in names:
                if not n.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(base, n), sites[0])
                mine = os.path.join(ROOT, rel)
                self.assertTrue(os.path.exists(mine), "%s is not in the tree" % rel)
                with open(os.path.join(base, n), "rb") as f:
                    a = f.read()
                with open(mine, "rb") as f:
                    b = f.read()
                self.assertEqual(a, b, "%s is stale" % rel)
                seen += 1
        self.assertGreater(seen, 50, seen)

    def test_every_non_python_file_in_it_is_the_one_in_the_tree(self):
        tmp = self.unpack()
        for rel, mine in sorted(PAIRS.items()):
            with self.subTest(rel):
                packaged = os.path.join(tmp, rel)
                self.assertTrue(os.path.exists(packaged), rel)
                with open(packaged, "rb") as f:
                    a = f.read()
                with open(os.path.join(ROOT, mine), "rb") as f:
                    b = f.read()
                self.assertEqual(a, b, "%s is not %s" % (rel, mine))

    def test_both_extension_trees_are_the_ones_in_gnome(self):
        """One package carries both extensions here, where Fedora splits them
        into subpackages: pacman has no weak dependencies, so there is nothing
        for a `Supplements: (w11 and gnome-shell)` to be expressed
        with, and gnome-shell stays an optdepends clause.

        typelib/ is left out of this walk on purpose and has its own test
        below.  build() recompiles those three files with Arch's
        g-ir-compiler, which is the whole reason `arch=('any')` is honest, and
        1.86 writes a different reserved word into every FunctionBlob than the
        1.80 that compiled the checked-in blobs -- 17 four-byte words per
        typelib, 0x00000001 against 0x03FF0FFD (gen-gir.py, the note above
        `_check_meaning`).  A byte comparison here would be red in the only job
        that runs it."""
        tmp = self.unpack()
        for uuid in (BRIDGE_UUID, OVERLAP_UUID):
            src = os.path.join(ROOT, "gnome", uuid)
            packaged = os.path.join(tmp, EXT, uuid)
            self.assertTrue(os.path.isdir(packaged), packaged)
            seen = 0
            for base, dirs, names in os.walk(packaged):
                dirs[:] = [d for d in dirs if d != "typelib"]
                for n in names:
                    rel = os.path.relpath(os.path.join(base, n), packaged)
                    with self.subTest("%s/%s" % (uuid, rel)):
                        with open(os.path.join(base, n), "rb") as f:
                            a = f.read()
                        with open(os.path.join(src, rel), "rb") as f:
                            b = f.read()
                        self.assertEqual(a, b, rel)
                    seen += 1
            self.assertGreaterEqual(seen, 3, uuid)

    def test_the_typelibs_are_all_four_there_and_describe_what_the_tree_does(self):
        """The regenerated files, compared by meaning and not by bytes.

        build() recompiles them, so the bytes differ from the checked-in blobs
        by compiler version alone; what the extension depends on is the
        namespace, that no shared library is named, every function's C symbol
        and return transfer, and every record's size and field offsets.  That
        is exactly gen-gir.py's `compare_typelibs`, which
        tests/test_gnome_overlap.py already uses for the checked-in files.

        Reading a typelib needs GIRepository, which is not a standard-library
        import and is absent on most machines; without it this asserts what it
        still can -- one file per record in generations.json, each a typelib --
        and says in the skip message which half did not run."""
        tmp = self.unpack()
        with open(os.path.join(ROOT, "gnome", OVERLAP_UUID, "generations.json"),
                  encoding="utf-8") as f:
            names = [g["namespace"] for g in json.load(f)["generations"]]
        self.assertEqual(len(names), 4, names)
        packaged_dir = os.path.join(tmp, EXT, OVERLAP_UUID, "typelib")
        shipped_dir = os.path.join(ROOT, "gnome", OVERLAP_UUID, "typelib")
        for ns in names:
            with self.subTest(ns):
                path = os.path.join(packaged_dir, "%s-1.0.typelib" % ns)
                self.assertTrue(os.path.exists(path), ns)
                with open(path, "rb") as f:
                    self.assertTrue(f.read(4).startswith(b"GOBJ"), ns)
        gen = load_gen_gir()
        built, why = gen.typelib_summary(packaged_dir, names[0])
        if why == gen.NO_GIREPOSITORY:
            self.skipTest("no GIRepository here to read a typelib with; the "
                          "three files are present and are typelibs")
        for ns in names:
            with self.subTest(ns):
                built, why = gen.typelib_summary(packaged_dir, ns)
                self.assertIsNotNone(built, "%s: %s" % (ns, why))
                mine, why = gen.typelib_summary(shipped_dir, ns)
                self.assertIsNotNone(mine, "%s: %s" % (ns, why))
                self.assertEqual(gen.compare_typelibs(mine, built), [])
                self.assertEqual(built["namespace"], ns)
                self.assertEqual(built["shared_libraries"], [])

    def test_usr_bin_is_the_seven_commands(self):
        got = [m for m in self.members() if m.startswith("usr/bin/")]
        self.assertEqual(sorted(got), ["usr/bin/%s" % n for n in TOOLS])

    def test_the_console_scripts_start_with_usr_bin_python(self):
        """It read `#!/usr/sbin/python` in the recon's bootstrap chroot, whose
        /usr/sbin is merged into /usr/bin and came first on that shell's PATH.
        A shebang naming a path that is a symlink on one machine and absent on
        another is a package that runs nowhere else, so the real build's answer
        is pinned here."""
        for name in TOOLS:
            with self.subTest(name):
                head = self.member("usr/bin/%s" % name).splitlines()[0]
                self.assertEqual(head, b"#!/usr/bin/python")

    def test_nothing_of_debians_is_in_the_payload(self):
        """A recipe that still reached into debian/ would ship a second copy of
        the enabler under a path nothing runs."""
        for m in self.members():
            self.assertNotIn("debian", m)


class WhatPacmanSays(PkgCase):
    """`pacman -Qip` on the file: the metadata a user reads."""

    def setUp(self):
        super().setUp()
        if shutil.which("pacman") is None:
            self.skipTest("no pacman")

    def info(self):
        got = subprocess.run(["pacman", "-Qip", self.pkg], capture_output=True,
                             text=True, timeout=300)
        self.assertEqual(got.returncode, 0, got.stderr)
        out = {}
        for line in got.stdout.splitlines():
            if ":" in line and not line.startswith(" "):
                key, _sep, value = line.partition(":")
                out[key.strip()] = value.strip()
        return out

    def test_architecture_version_depends_and_licenses(self):
        """Licenses is read out of LICENSE's own SPDX-License-Identifier
        header, not typed: the tree has been BSD-2-Clause since HEAD 375d815,
        and a relicence should move the recipe rather than this file."""
        info = self.info()
        self.assertEqual(info["Architecture"], "any")
        self.assertEqual(info["Version"], "%s-1" % VERSION)
        self.assertEqual(info["Depends On"], "python")
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            spdx = fh.readline().strip().split(":", 1)[1].strip()
        self.assertEqual(info["Licenses"], spdx)

    def test_the_optdepends_are_the_recipes(self):
        """`pacman -Qi` prints them one per line after the first, so the check
        is on the names the recipe declares being present in the output."""
        got = subprocess.run(["pacman", "-Qip", self.pkg], capture_output=True,
                             text=True, timeout=300).stdout
        for name in ("python-gobject", "gtk3", "wl-mirror", "gnome-shell",
                     "xdotool", "wmctrl", "xorg-xprop", "xorg-xrandr"):
            with self.subTest(name):
                self.assertIn(name, got)


class InstallAndRemove(PkgCase):
    """`pacman -U` and `pacman -R`, as root, on a machine that has pacman.

    The measured run: the package installed clean, printed the banner, listed
    the optdepends, and pacman ran its own `(1/2) Reloading device manager
    configuration` from 35-systemd-udev-reload.hook; `pacman -R` removed every
    file and the /var/lib/w11 stamp [recon2/pkg-arch.md 2]."""

    def setUp(self):
        super().setUp()
        if shutil.which("pacman") is None:
            self.skipTest("no pacman")
        if os.geteuid() != 0:
            self.skipTest("pacman -U needs root")

    def test_install_then_remove_leaves_nothing_of_ours(self):
        subprocess.run(["pacman", "-U", "--noconfirm", self.pkg],
                       check=True, capture_output=True, timeout=900)
        try:
            self.assertTrue(shutil.which("wdotool"), "the tools are not on PATH")
            check = subprocess.run(["pacman", "-Qkk", "w11"],
                                   capture_output=True, text=True, timeout=300)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        finally:
            subprocess.run(["pacman", "-R", "--noconfirm", "w11"],
                           check=True, capture_output=True, timeout=900)
        self.assertIsNone(shutil.which("wdotool"))
        for path in ("/usr/lib/udev/rules.d/60-w11-uinput.rules",
                     "/etc/xdg/autostart/w11-enable-bridge.desktop",
                     "/usr/lib/w11/enable-bridge",
                     "/var/lib/w11/installed",
                     "/usr/share/gnome-shell/extensions/" + BRIDGE_UUID):
            with self.subTest(path):
                self.assertFalse(os.path.exists(path), path)


class Namcap(PkgCase):
    """namcap on the recipe and on the package.

    The recipe has to be clean -- the recon proved the check is live by
    breaking `license=()` and watching `E: Missing license` appear -- and the
    package's findings have to be ones packaging/arch/namcap.expected names,
    each with its reason.  The ~90 "Referenced python module ... is an
    uninstalled dependency" lines are namcap resolving the package's own
    modules against installed packages; whether a real makepkg build still
    produces them is what this job answers, and the filter is written narrowly
    enough that the answer is a green job either way."""

    def setUp(self):
        super().setUp()
        if shutil.which("namcap") is None:
            self.skipTest("no namcap")

    def run_namcap(self, target):
        got = subprocess.run(["namcap", target], capture_output=True, text=True,
                             timeout=900)
        return [ln.strip() for ln in got.stdout.splitlines() if ln.strip()]

    def test_the_recipe_itself_prints_nothing(self):
        self.assertEqual(self.run_namcap(PKGBUILD), [])

    def test_the_package_prints_only_what_namcap_expected_names(self):
        with open(NAMCAP_EXPECTED, encoding="utf-8") as fh:
            patterns = [re.compile(ln) for ln in fh.read().splitlines()
                        if ln.strip() and not ln.startswith("#")]
        unexpected = [ln for ln in self.run_namcap(self.pkg)
                      if not any(p.search(ln) for p in patterns)]
        self.maxDiff = None
        self.assertEqual(unexpected, [])


if __name__ == "__main__":
    unittest.main()
