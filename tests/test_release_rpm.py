#!/usr/bin/env python3
"""The three rpms `sh scripts/build-rpm.sh` produces, against the tree.

tests/test_release_deb.py exists because the committed .deb was once the build
of the previous tag and every test in the suite passed anyway: what a user runs
is a binary, and nothing in the tree makes a binary agree with it.  The rpm has
the same hole and one more edge, because its payload is assembled by a spec that
lists paths by hand -- so a file added to debian/w11.install and not to
%install is a Fedora package silently missing it.  tests/test_rpm_spec.py holds
the two lists against each other; this holds the built package against the tree.

Nothing is committed: an rpm is one file per Fedora release (the payload is
version-bound, `python(abi) = 3.14`), so there is no counterpart to the .deb's
"one file for both supported releases" and dist/ is where they land.  This file
therefore looks in dist/, or in $W11_RPM_DIR, and skips when there is nothing
there -- which is every run outside the CI `rpm` job in fedora:44 and any box
where somebody has just built them by hand.

rpm2cpio and cpio are not standard library, so the payload half skips without
them; `rpm` itself is what answers the dependency and scriptlet questions.  The
install-and-erase check needs real root (`rpm --root` as a plain user answers
"Unable to change root directory: Operation not permitted", measured), which the
CI container is and a developer's box is not.
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

RPMDIR = os.environ.get("W11_RPM_DIR") or os.path.join(ROOT, "dist")
BRIDGE_UUID = "w11-bridge@w11"
OVERLAP_UUID = "w11-overlap@w11"
EXT = "usr/share/gnome-shell/extensions"

#: Every file the three packages carry that is NOT a Python module, paired with
#: the file in the tree it is a copy of.  test_release_deb.py's PAIRS with the
#: two Fedora layout changes: the helper is under /usr/libexec, and the enabler
#: comes from packaging/common.  The autostart entry is deliberately absent --
#: it is the one file that is NOT a byte copy, because %install rewrites its
#: Exec= line, and it has a test of its own below.
PAIRS = {
    "usr/lib/udev/rules.d/60-w11-uinput.rules":
        "gnome/60-w11-uinput.rules",
    "usr/lib/modules-load.d/w11-uinput.conf":
        "gnome/modules-load-uinput.conf",
    "usr/libexec/w11/enable-bridge": "packaging/common/enable-bridge",
    "usr/share/applications/warandr.desktop": "warandr.desktop",
}

#: The command phrases the scriptlets are compared by, as in
#: tests/test_rpm_scripts.py -- here against what rpm actually stored, macros
#: expanded, rather than against the spec's text.
PHRASES = (
    "modprobe uinput",
    "udevadm control --reload-rules",
    "udevadm trigger --name-match=uinput",
    "udevadm settle --timeout=5",
    "udevadm info -q property",
    "static_node-tags/uaccess/uinput",
    "setfacl -b",
    "removexattr",
    "chown root:root",
    "chmod 0600",
)


def phrases(text):
    found = []
    for phrase in PHRASES:
        start = 0
        while True:
            at = text.find(phrase, start)
            if at < 0:
                break
            found.append((at, phrase))
            start = at + 1
    return [p for _at, p in sorted(found)]


def find(prefix):
    if not os.path.isdir(RPMDIR):
        return []
    return sorted(os.path.join(RPMDIR, n) for n in os.listdir(RPMDIR)
                  if n.startswith(prefix) and n.endswith(".rpm"))


class RpmCase(unittest.TestCase):
    """The three packages, or a skip.

    The skip is per test and not per class on purpose: a class-level skip
    reports `Ran 0 tests`, and the suite's own test count (R26, docs/Technical
    section 10) would then depend on whether the machine happens to have a
    built rpm lying about.  Skipped tests still count."""

    def setUp(self):
        if shutil.which("rpm") is None:
            self.skipTest("no rpm")
        self.main = find("w11-%s-" % VERSION)
        self.bridge = find("gnome-shell-extension-w11-bridge-%s-" % VERSION)
        self.overlap = find("gnome-shell-extension-w11-overlap-%s-" % VERSION)
        if not (self.main and self.bridge and self.overlap):
            self.skipTest("no w11 %s rpms in %s (sh scripts/build-rpm.sh)"
                          % (VERSION, RPMDIR))

    def q(self, path, *args):
        got = subprocess.run(["rpm", "-qp", "--nosignature"] + list(args) + [path],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(got.returncode, 0, got.stderr)
        return got.stdout

    def unpack(self, path):
        """The payload, extracted with rpm2cpio and cpio.

        Neither is standard library and neither is on every box; the CI
        container has both, and this is the one thing the payload tests cannot
        be written without."""
        for tool in ("rpm2cpio", "cpio"):
            if shutil.which(tool) is None:
                self.skipTest("no %s" % tool)
        tmp = tempfile.mkdtemp(prefix="w11-rpm-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        payload = subprocess.run(["rpm2cpio", path], stdout=subprocess.PIPE,
                                 check=True, timeout=300).stdout
        subprocess.run(["cpio", "-idm", "--quiet"], input=payload, cwd=tmp,
                       check=True, timeout=300)
        return tmp


class ThePackagesInDist(RpmCase):

    def test_all_three_are_named_for_this_version_and_are_noarch(self):
        """One build covers Fedora 43 and 44 and not rawhide, which is a claim
        about the arch and about python(abi) rather than about three files."""
        for path in self.main + self.bridge + self.overlap:
            with self.subTest(os.path.basename(path)):
                self.assertIn(VERSION, os.path.basename(path))
                self.assertEqual(self.q(path, "--qf", "%{ARCH}"), "noarch")
                self.assertEqual(self.q(path, "--qf", "%{VERSION}"), VERSION)

    def test_every_module_in_it_is_the_one_in_the_tree(self):
        """The finding test_release_deb.py was written for, in its Fedora
        form.  %{python3_sitelib} is version-bound, so the directory is found
        rather than named."""
        tmp = self.unpack(self.main[0])
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
                self.assertEqual(a, b, "%s is stale: sh scripts/build-rpm.sh" % rel)
                seen += 1
        self.assertGreater(seen, 50, seen)

    def test_every_non_python_file_in_it_is_the_one_in_the_tree(self):
        tmp = self.unpack(self.main[0])
        ext = self.unpack(self.bridge[0])
        for rel, mine in sorted(PAIRS.items()):
            with self.subTest(rel):
                packaged = os.path.join(tmp, rel)
                if not os.path.exists(packaged):
                    packaged = os.path.join(ext, rel)
                self.assertTrue(os.path.exists(packaged), rel)
                with open(packaged, "rb") as f:
                    a = f.read()
                with open(os.path.join(ROOT, mine), "rb") as f:
                    b = f.read()
                self.assertEqual(a, b, "%s is not %s" % (rel, mine))

    def test_the_autostart_entry_is_the_common_one_with_libexec_in_its_exec(self):
        """The one file that is not a byte copy: %install rewrites Exec= to
        %{_libexecdir}/w11/enable-bridge, because that is where the
        helper goes on Fedora.  Every other line has to be the shared file's,
        or two packagings have drifted into two autostart entries."""
        tmp = self.unpack(self.bridge[0])
        with open(os.path.join(tmp, "etc/xdg/autostart",
                               "w11-enable-bridge.desktop"),
                  encoding="utf-8") as f:
            shipped = f.read().splitlines()
        with open(os.path.join(ROOT, "packaging/common/enable-bridge.desktop"),
                  encoding="utf-8") as f:
            common = f.read().splitlines()
        self.assertIn("Exec=/usr/libexec/w11/enable-bridge", shipped)
        self.assertEqual([ln for ln in shipped if not ln.startswith("Exec=")],
                         [ln for ln in common if not ln.startswith("Exec=")])

    def test_the_two_extension_trees_are_the_ones_in_gnome(self):
        for path, uuid in ((self.bridge[0], BRIDGE_UUID),
                           (self.overlap[0], OVERLAP_UUID)):
            tmp = self.unpack(path)
            src = os.path.join(ROOT, "gnome", uuid)
            packaged = os.path.join(tmp, EXT, uuid)
            for base, _dirs, names in os.walk(packaged):
                for n in names:
                    rel = os.path.relpath(os.path.join(base, n), packaged)
                    with self.subTest("%s/%s" % (uuid, rel)):
                        with open(os.path.join(base, n), "rb") as f:
                            a = f.read()
                        with open(os.path.join(src, rel), "rb") as f:
                            b = f.read()
                        self.assertEqual(a, b, rel)

    def test_every_generation_in_the_table_has_its_typelib_in_the_payload(self):
        tmp = self.unpack(self.overlap[0])
        with open(os.path.join(ROOT, "gnome", OVERLAP_UUID, "generations.json"),
                  encoding="utf-8") as f:
            names = [g["namespace"] for g in json.load(f)["generations"]]
        self.assertEqual(len(names), 3, names)
        for ns in names:
            with self.subTest(ns):
                self.assertTrue(os.path.exists(os.path.join(
                    tmp, EXT, OVERLAP_UUID, "typelib", "%s-1.0.typelib" % ns)), ns)

    def test_usr_bin_is_the_seven_commands(self):
        got = self.q(self.main[0], "-l")
        names = sorted(re.findall(r"^/usr/bin/(\w+)$", got, re.M))
        self.assertEqual(names, ["warandr", "wdotool", "wmirror", "wwmctl",
                                 "wxprop", "wxrandr", "xw11"])


class WhatDnfWillDo(RpmCase):
    """The dependency tags, which decide what arrives with the package."""

    def test_the_payload_is_bound_to_one_python(self):
        """`python(abi) = 3.14` is generated, not written: it is why one build
        covers Fedora 43 and 44 (both 3.14.7) and rawhide needs its own."""
        requires = self.q(self.main[0], "--requires")
        self.assertTrue(re.search(r"^python\(abi\) = 3\.\d+$", requires, re.M),
                        requires)

    def test_the_gtk_stack_is_recommended_and_not_required(self):
        """Design decision 5.  dnf honours Recommends by default, so the GUI
        arrives for everyone who has not turned weak deps off, and
        `dnf install w11` on a sway box pulls in no toolkit."""
        recommends = self.q(self.main[0], "--recommends").split()
        self.assertEqual(sorted(recommends),
                         ["gtk3", "python3-gobject", "wmctrl", "xdotool", "xprop", "xrandr"])
        self.assertNotIn("gtk3", self.q(self.main[0], "--requires").split())

    def test_the_originals_are_recommended_and_the_mirror_helper_suggested(self):
        """The four originals moved from Suggests to Recommends with the proxy
        (design section 8.5): on a Wayland session the wrapper execs them
        through xw11, so a default install gets the real tools; wl-mirror stays
        a suggestion."""
        suggests = sorted(self.q(self.main[0], "--suggests").split())
        self.assertEqual(suggests, ["wl-mirror"])

    def test_the_bridge_supplements_both_halves_and_the_overlap_supplements_nothing(self):
        """The whole reason the extensions are subpackages: dnf installs the
        bridge by itself wherever both halves are present, and installs the one
        thing that can cost a session for nobody, ever."""
        self.assertIn("(w11 and gnome-shell)",
                      self.q(self.bridge[0], "--supplements"))
        self.assertEqual(self.q(self.overlap[0], "--supplements").strip(), "")


class TheScriptletsThatShipped(RpmCase):
    """What rpm stored, against what the spec says -- macros expanded.

    Two claims, and the second is the one only a built rpm can make: the
    phrase lists below say what the commands are, and
    test_they_are_the_specs_own_sections says that what rpm stored is what the
    spec in this tree says.  Without the second a %post that grew a fifth
    command would pass here and be caught only in tests/test_rpm_spec.py, which
    never sees an rpm."""

    def test_they_are_the_specs_own_sections(self):
        """R17's `--qf '%{POSTIN}%{POSTUN}'` against the spec text, through the
        slicer tests/test_rpm_spec.py already uses -- one reader, so a change
        to how a section is found cannot make the two files disagree about
        what they compared."""
        import test_rpm_spec as spec
        for tag, name in (("%{POSTIN}", "post"), ("%{POSTUN}", "postun")):
            with self.subTest(name):
                stored = self.q(self.main[0], "--qf", tag)
                # comments stripped: rpm does not store them, and the sentence
                # above %post quotes `udevadm control --reload-rules` while
                # explaining why the reload is ours and not systemd-udev's
                written = "\n".join(ln for ln in spec.expand(spec.section(name)).splitlines()
                                    if not ln.lstrip().startswith("#"))
                self.assertEqual(phrases(stored), phrases(written))

    def test_post_is_the_four_udev_commands_and_the_stamp(self):
        post = self.q(self.main[0], "--qf", "%{POSTIN}")
        self.assertEqual(phrases(post)[:4], [
            "modprobe uinput",
            "udevadm control --reload-rules",
            "udevadm trigger --name-match=uinput",
            "udevadm settle --timeout=5",
        ])
        self.assertIn("/var/lib/w11/installed", post,
                      "%{_sharedstatedir} did not expand to /var/lib")

    def test_postun_is_the_revoke_sequence(self):
        postun = self.q(self.main[0], "--qf", "%{POSTUN}")
        self.assertEqual(phrases(postun), [
            "udevadm control --reload-rules",
            "static_node-tags/uaccess/uinput",
            "udevadm info -q property",
            "udevadm info -q property",
            "setfacl -b",
            "removexattr",
            "chown root:root",
            "chmod 0600",
        ])
        self.assertIn('if [ "$1" = 0 ]', postun)

    def test_the_stamp_is_a_ghost_and_is_not_in_the_payload(self):
        """%ghost: rpm owns the path and carries no bytes for it, so the file
        %post writes belongs to the package and an erase takes it."""
        listing = self.q(self.main[0], "-l")
        self.assertIn("/var/lib/w11/installed", listing)
        ghosts = self.q(self.main[0], "--qf", "[%{FILENAMES} %{FILEFLAGS}\n]")
        line = [ln for ln in ghosts.splitlines()
                if ln.startswith("/var/lib/w11/installed ")]
        self.assertEqual(len(line), 1, ghosts)
        # bit 6 (64) is RPMFILE_GHOST
        self.assertTrue(int(line[0].split()[1]) & 64, line)


class InstallAndErase(RpmCase):
    """Into a scratch root, as root, and out again.

    `rpm --root` as a plain user answers "Unable to change root directory:
    Operation not permitted" (measured), so this is the CI container's test and
    nobody else's."""

    def setUp(self):
        super().setUp()
        if os.geteuid() != 0:
            self.skipTest("rpm --root needs real root")

    def test_an_erase_leaves_nothing_under_usr_or_etc_but_directories(self):
        """The same sentence test_debian_scripts.py makes about the .deb.  A
        file left behind is a file no package owns any more: on the next
        install rpm will not replace it, and on this one it is a stale copy of
        something that has moved."""
        root = tempfile.mkdtemp(prefix="w11-rpm-root-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        pkgs = self.main + self.bridge + self.overlap
        subprocess.run(["rpm", "--root", root, "-Uvh", "--noscripts", "--nodeps"]
                       + pkgs, check=True, capture_output=True, timeout=600)
        self.assertTrue(os.path.exists(os.path.join(root, "usr/bin/wdotool")))
        subprocess.run(["rpm", "--root", root, "-e", "--noscripts", "--nodeps",
                        "w11",
                        "gnome-shell-extension-w11-bridge",
                        "gnome-shell-extension-w11-overlap"],
                       check=True, capture_output=True, timeout=600)
        left = []
        for top in ("usr", "etc"):
            for base, _dirs, names in os.walk(os.path.join(root, top)):
                left += [os.path.relpath(os.path.join(base, n), root) for n in names]
        self.assertEqual(sorted(left), [])


if __name__ == "__main__":
    unittest.main()
