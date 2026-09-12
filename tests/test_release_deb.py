#!/usr/bin/env python3
"""What actually ships: the package committed in `release/`, and what the documents say about it.

Every other file in this suite runs the *tree*.  What a user runs is
`sudo apt install ./release/w11_<version>_all.deb`, which is a binary in the
repository -- and nothing in the tree makes that binary agree with the tree.  While 0.4
was being finished it did not agree: the committed file was the build of the v0.3 tag,
put there by "Release version 0.3" and never rebuilt, so a user who followed the README
got none of the release's changes.  Measured on both default GNOME images, from the shipped
package: the input daemon still outlived a removed socket and then held the lock that
stops the next one starting, a chord the layout cannot produce was still pressed at its
US position in silence, the layout notice still reached only the first command, and the
overlap extension was not in the package at all -- while README.md said "the package
carries a second, separate extension".

So: the payload is compared with the tree, file by file.  It is the only test here that
can fail for something nobody typed.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` is a bare import: it resolves only with the tests directory
# on sys.path, which `python3 tests/<file>.py` gives for free and
# `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from w11common import VERSION
from support import documents

RELEASE = os.path.join(ROOT, "release")
DIST = "usr/lib/python3/dist-packages"
OVERLAP_UUID = "w11-overlap@w11"
BRIDGE_UUID = "w11-bridge@w11"
EXT = "usr/share/gnome-shell/extensions"

#: Every file the package carries that is *not* a Python module, paired with
#: the file in the tree it is a copy of.  The .py half is compared by the walk
#: in `test_every_module_in_it_is_the_one_in_the_tree`; these are the ones no
#: other test in this repository ever looks at, and each of them is a thing
#: that runs on the user's machine: a udev rule, a modules-load line, the
#: autostart script and its .desktop entry, the menu entry, the licence.
PAIRS = {
    "usr/lib/udev/rules.d/60-w11-uinput.rules":
        "gnome/60-w11-uinput.rules",
    "usr/lib/modules-load.d/w11-uinput.conf":
        "gnome/modules-load-uinput.conf",
    # packaging/common/, not debian/: the enabler and its autostart entry are a
    # per-user gsettings enable and an XDG entry, needed identically by the
    # .deb, the rpm and the PKGBUILD, and an Arch recipe reaching into a
    # directory named for dpkg was what said they were in the wrong place.  The
    # bytes did not move, only the directory, so the package in release/ still
    # matches them byte for byte with no rebuild.
    "usr/lib/w11/enable-bridge": "packaging/common/enable-bridge",
    "usr/lib/w11/enable-bridge.desktop": "packaging/common/enable-bridge.desktop",
    "usr/share/applications/warandr.desktop": "warandr.desktop",
    "usr/share/doc/w11/copyright": "debian/copyright",
    "usr/share/lintian/overrides/w11": "debian/w11.lintian-overrides",
}


def _deb_filter(member, dest_path):
    """tarfile's `data` rules, minus the one a .deb legitimately breaks.

    The autostart entry is a symlink to `/usr/lib/w11/...`, an absolute
    path, which `data_filter` refuses on principle -- it is a link that would
    escape the extraction root if the root were `/`.  For a package payload
    that is the whole point of the file, and `dpkg-deb -x` writes it, so the
    member is kept as it is.  Every other rule (no `..`, no absolute member
    names, no devices, no setuid) still applies."""
    try:
        return tarfile.data_filter(member, dest_path)
    except tarfile.AbsoluteLinkError:
        if member.name.startswith("/") or ".." in member.name.split("/"):
            raise
        return member


def _unzstd(body):
    """`compression.zstd` is Python 3.14's (PEP 784); 24.04's 3.12 has none, so
    there the `zstd` command does it (Ubuntu installs it with dpkg), and a host
    with neither cannot run the three tests that need the payload."""
    try:
        from compression import zstd
    except ImportError:
        if shutil.which("zstd") is None:
            raise unittest.SkipTest("no compression.zstd (Python < 3.14) and no zstd command")
        return subprocess.run(["zstd", "-d", "-c"], input=body, stdout=subprocess.PIPE,
                              check=True).stdout
    return zstd.decompress(body)


def unpack_deb(path, dest):
    """Extract a binary package with the standard library alone.

    `dpkg-deb` is not on a machine that is not Debian, and three tests here
    used to skip themselves for want of it -- on exactly the payload that has
    no other test.  A .deb is an `ar` archive of three members in a fixed
    order, and the third is the payload tar; since 1.21 dpkg compresses it
    with zstd by default, which Python 3.14 reads (`compression.zstd`, PEP
    784).  Returns the sorted list of relative names extracted.
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    if blob[:8] != b"!<arch>\n":
        raise ValueError("%s is not an ar archive" % path)
    at = 8
    members = {}
    while at + 60 <= len(blob):
        header = blob[at:at + 60]
        name = header[0:16].decode("ascii").rstrip("/ ").strip()
        size = int(header[48:58].decode("ascii").strip())
        body = blob[at + 60:at + 60 + size]
        members[name] = body
        at += 60 + size + (size & 1)        # members are padded to even
    for name, body in members.items():
        if not name.startswith("data.tar"):
            continue
        if name.endswith(".zst"):
            body = _unzstd(body)
            mode = "r:"
        else:
            mode = "r:" + name.rsplit(".", 1)[1] if "." in name[8:] else "r:"
        with tarfile.open(fileobj=io.BytesIO(body), mode=mode) as tar:
            tar.extractall(dest, filter=_deb_filter)
            return sorted(m.name.lstrip("./") for m in tar.getmembers()
                          if m.name not in ("./", "."))
    raise ValueError("%s has no data member: %s" % (path, sorted(members)))


class ThePackageInTheTree(unittest.TestCase):
    """release/ holds exactly one .deb, it is this version, and its payload is
    this tree."""

    @classmethod
    def setUpClass(cls):
        cls.debs = sorted(n for n in os.listdir(RELEASE) if n.endswith(".deb"))

    def deb(self):
        self.assertEqual(len(self.debs), 1, self.debs)
        return os.path.join(RELEASE, self.debs[0])

    def unpacked(self):
        """The package's payload, extracted once per test that wants it.

        `unpack_deb` rather than `dpkg-deb -x`: the two are proved identical by
        `StdlibUnpacking` below, and this way the payload tests run on a
        machine with no dpkg -- which is where they were being skipped."""
        tmp = tempfile.mkdtemp(prefix="w11-deb-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        unpack_deb(self.deb(), tmp)
        return tmp

    def control(self):
        """The control member's files, as name -> text (`dpkg-deb -e`)."""
        if shutil.which("dpkg-deb") is None:
            self.skipTest("no dpkg-deb")
        tmp = tempfile.mkdtemp(prefix="w11-deb-ctl-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        subprocess.run(["dpkg-deb", "-e", self.deb(), tmp], check=True)
        out = {}
        for n in sorted(os.listdir(tmp)):
            with open(os.path.join(tmp, n), encoding="utf-8", errors="replace") as f:
                out[n] = f.read()
        return out

    def field(self, name):
        if shutil.which("dpkg-deb") is None:
            self.skipTest("no dpkg-deb")
        out = subprocess.run(["dpkg-deb", "-f", self.deb(), name],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_exactly_one_package_named_for_this_version(self):
        self.assertEqual(self.debs, ["w11_%s_all.deb" % VERSION])

    def test_its_own_control_says_the_same_version(self):
        self.assertEqual(self.field("Version"), VERSION)

    def test_every_module_in_it_is_the_one_in_the_tree(self):
        """The finding itself.  A stale binary is invisible from inside the
        tree: every test passes, every document is right, and the thing people
        install is a previous release.

        EXPECTED TO FAIL while the X11 proxy is being built: `w11common/
        passthrough.py` grew `OUR_NAMES += ("xw11",)`, `PROXY_MODE_VAR` and
        `proxy_mode()` (design section 8.1), and the four CLIs grew the call to
        `xw11.wrap.maybe_exec_through_proxy`, so the committed package's copies
        are older than the tree's by exactly those edits.  `sh
        scripts/build-deb.sh` is the whole fix and it is the release step, not
        a batch's -- which is why this is a marked gap and not a silent one: the
        day the package is rebuilt this test PASSES, the marker turns the suite
        red for an unexpected success, and the marker comes off."""
        tmp = self.unpacked()
        dist = os.path.join(tmp, DIST)
        self.assertTrue(os.path.isdir(dist), dist)
        seen = 0
        for base, dirs, names in os.walk(dist):
            dirs[:] = [d for d in dirs
                       if d != "__pycache__" and not d.endswith(".dist-info")]
            for n in names:
                if not n.endswith(".py"):
                    continue
                packaged = os.path.join(base, n)
                rel = os.path.relpath(packaged, dist)
                mine = os.path.join(ROOT, rel)
                self.assertTrue(os.path.exists(mine),
                                "%s is in the package and not in the tree" % rel)
                with open(packaged, "rb") as f:
                    a = f.read()
                with open(mine, "rb") as f:
                    b = f.read()
                self.assertEqual(a, b, "%s in the package is not the one in "
                                       "the tree: rebuild it with "
                                       "`sh scripts/build-deb.sh`" % rel)
                seen += 1
        self.assertGreater(seen, 50, seen)

    def test_it_carries_both_extensions_the_udev_rule_and_the_menu_entry(self):
        tmp = self.unpacked()
        for rel in (
                "usr/lib/udev/rules.d/60-w11-uinput.rules",
                "usr/share/applications/warandr.desktop",
                "usr/share/gnome-shell/extensions/%s/extension.js" % BRIDGE_UUID,
                "usr/share/gnome-shell/extensions/%s/extension.js" % OVERLAP_UUID,
                "usr/share/gnome-shell/extensions/%s/typelib/"
                "W11Overlap18-1.0.typelib" % OVERLAP_UUID):
            self.assertTrue(os.path.exists(os.path.join(tmp, rel)), rel)

    def test_the_shipped_extensions_are_the_ones_in_gnome(self):
        tmp = self.unpacked()
        for uuid in (BRIDGE_UUID, OVERLAP_UUID):
            src = os.path.join(ROOT, "gnome", uuid)
            packaged = os.path.join(tmp, "usr/share/gnome-shell/extensions", uuid)
            for base, _dirs, names in os.walk(packaged):
                for n in names:
                    mine = os.path.join(src, os.path.relpath(
                        os.path.join(base, n), packaged))
                    self.assertTrue(os.path.exists(mine), mine)
                    with open(os.path.join(base, n), "rb") as f:
                        a = f.read()
                    with open(mine, "rb") as f:
                        b = f.read()
                    self.assertEqual(a, b, mine)

    def test_every_non_python_file_in_it_is_the_one_in_the_tree(self):
        """The seven files the module walk above does not reach.  Each of them
        runs, or is read by something that runs, on the user's machine: the
        udev rule and the modules-load line decide whether input works at all,
        `enable-bridge` is what turns the extension on in the first session,
        and the .desktop pair is how it is started and how warandr appears in
        the menu.  A stale copy of any of them is invisible from the tree."""
        tmp = self.unpacked()
        for rel, mine in sorted(PAIRS.items()):
            with self.subTest(rel):
                packaged = os.path.join(tmp, rel)
                self.assertTrue(os.path.exists(packaged), rel)
                with open(packaged, "rb") as f:
                    a = f.read()
                with open(os.path.join(ROOT, mine), "rb") as f:
                    b = f.read()
                self.assertEqual(a, b, "%s in the package is not %s: rebuild "
                                       "it with `sh scripts/build-deb.sh`" % (rel, mine))

    def test_the_maintainer_scripts_are_the_ones_in_debian(self):
        """postinst and postrm are compared up to debhelper's own additions:
        dh_installdeb appends `# Automatically added by ...` blocks and
        substitutes `#DEBHELPER#`, so the shipped file is ours plus that.  What
        is checked is that every line we wrote is still there, in order."""
        control = self.control()
        for name in ("postinst", "postrm"):
            with self.subTest(name):
                with open(os.path.join(ROOT, "debian", "w11." + name),
                          encoding="utf-8") as f:
                    mine = [ln for ln in f.read().splitlines()
                            if ln.strip() != "#DEBHELPER#"]
                shipped = control[name].splitlines()
                # dh_installdeb substitutes the token; a shipped script that
                # still had it would be one debhelper never processed.
                self.assertNotIn("#DEBHELPER#", control[name])
                rest = iter(shipped)
                for line in mine:
                    for candidate in rest:
                        if candidate == line:
                            break
                    else:
                        self.fail("%s: the packaged script is missing %r" % (name, line))

    def test_every_generation_in_the_table_has_its_typelib_in_the_payload(self):
        """generations.json is the single record of which libmutter layouts the
        overlap extension knows, and one compiled type description per row is
        what makes each of them reachable.  The old assertion named
        W11Overlap18 and nothing else, so a package that shipped one typelib out
        of four passed it -- and on GNOME 46 or 51 the extension would load
        and then fail at its first call."""
        tmp = self.unpacked()
        with open(os.path.join(tmp, EXT, OVERLAP_UUID, "generations.json"),
                  encoding="utf-8") as f:
            table = json.load(f)
        names = [g["namespace"] for g in table["generations"]]
        # 46, 49, 50 and 51: one row per measured libmutter layout (GNOME 49 landed 2026-09-11)
        self.assertEqual(len(names), 4, names)
        for ns in names:
            with self.subTest(ns):
                self.assertTrue(os.path.exists(os.path.join(
                    tmp, EXT, OVERLAP_UUID, "typelib", "%s-1.0.typelib" % ns)), ns)

    def test_the_packaged_overlap_metadata_names_the_measured_majors(self):
        """gnome-shell refuses to load an extension whose metadata.json does
        not name the running Shell major, so this list is the whole of what the
        package route can reach: on GNOME 51.beta the shipped bridge was
        OUT_OF_DATE for exactly this reason and one added line fixed it
        (measured on stonking-gnome).  The list has to be the table's, or the
        package silently supports a different set of desktops from the one the
        extension has code for."""
        tmp = self.unpacked()
        base = os.path.join(tmp, EXT, OVERLAP_UUID)
        with open(os.path.join(base, "generations.json"), encoding="utf-8") as f:
            table = json.load(f)
        with open(os.path.join(base, "metadata.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["shell-version"],
                         [str(g["shell_major"]) for g in table["generations"]],
                         "the package route cannot force the extension on any "
                         "other major: metadata.json is what gnome-shell reads")

    def test_the_autostart_entry_is_a_symlink_to_the_one_file(self):
        """One file, two names: /usr/lib/w11/enable-bridge.desktop is
        the payload and /etc/xdg/autostart/ holds a link to it, so a user who
        does not want it can mask the link in ~/.config/autostart without dpkg
        putting it back at the next upgrade."""
        tmp = self.unpacked()
        link = os.path.join(tmp, "etc/xdg/autostart/w11-enable-bridge.desktop")
        self.assertTrue(os.path.islink(link), link)
        self.assertEqual(os.readlink(link),
                         "/usr/lib/w11/enable-bridge.desktop")

    @staticmethod
    def _declared_and_built():
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            declared = sorted(tomllib.load(f)["project"]["scripts"])
        with open(os.path.join(ROOT, "scripts", "build-pyz.sh"), encoding="utf-8") as f:
            built = sorted(re.findall(r"^build (\w+) ", f.read(), re.M))
        return declared, built

    def test_the_scripts_table_and_the_build_script_name_the_same_seven(self):
        """Two of the three lists, and the two that are both in the tree: what
        pyproject declares and what `scripts/build-pyz.sh` builds.  Seven since
        the X11 proxy -- a zipapp of `xw11` that nobody built is a route to the
        proxy that only exists for people who installed the package."""
        declared, built = self._declared_and_built()
        self.assertEqual(declared, built)
        self.assertEqual(len(declared), 7, declared)
        self.assertIn("xw11", declared)

    def test_usr_bin_is_exactly_the_project_scripts_table(self):
        """The third list: what dpkg installs.  It is written in a fourth file
        -- the committed binary -- and nothing but this test connects it to the
        other two.

        EXPECTED TO FAIL until the release is rebuilt: `xw11` is in
        `[project.scripts]` and in `build-pyz.sh` and is not in the package in
        `release/`, because building one is the release step and not a batch's
        (`sh scripts/build-deb.sh`).  The sibling above holds the two tree
        lists against each other meanwhile, so a seventh name added to one and
        not the other is still caught today."""
        tmp = self.unpacked()
        packaged = sorted(os.listdir(os.path.join(tmp, "usr/bin")))
        declared, built = self._declared_and_built()
        self.assertEqual(packaged, declared)
        self.assertEqual(packaged, built)

    def test_the_readme_installs_the_file_that_is_there(self):
        text = documents()["README.md"]
        named = set(re.findall(r"release/(w11_[0-9.]+_all\.deb)", text))
        self.assertEqual(named, set(self.debs), "README.md names a package "
                                                "that is not in release/")


class TheDocumentsAboutIt(unittest.TestCase):
    """Three claims the 0.4 retest measured false.  All three are cheap to
    check against a fact rather than against prose."""

    def test_no_document_says_the_package_leaves_the_overlap_extension_out(self):
        """`debian/w11.install` decides this, and since 0.4 it lists
        the overlap extension -- while gnome/README.md, docs/Technical.md and
        the installer's own header still said "not in the .deb", the last of
        them four lines under "since 0.4 it is packaged"."""
        with open(os.path.join(ROOT, "debian", "w11.install"),
                  encoding="utf-8") as f:
            shipped = OVERLAP_UUID in f.read()
        self.assertTrue(shipped, "debian/w11.install no longer ships "
                                 "the overlap extension: fix this test's "
                                 "premise, not the documents")
        claims = ("not in the .deb", "not installed by the .deb",
                  "not part of the .deb", "is not in the package")
        for name, text in documents().items():
            for claim in claims:
                self.assertNotIn(claim, text, "%s: %r" % (name, claim))
        with open(os.path.join(ROOT, "gnome", "install-overlap.sh"),
                  encoding="utf-8") as f:
            self.assertNotIn("not installed by\n# the .deb", f.read())

    def test_the_overlap_sample_reports_the_rigs_own_millimetres(self):
        """README.md's overlap example showed `320mm x 200mm`, which is an
        older rig's head.  Measured on the virtio-vga rig the rest of that
        document describes: a 1920x1080 head reports **480mm x 270mm** through
        the Wayland backends, from the EDID size `wl_output` carries.  (The X
        server's own RandR computes millimetres from 96 dpi instead and says
        487mm x 274mm for the same head, which is why vm/README.md quotes that
        number for the Xfce flavors: two right answers, one per path.)"""
        docs = documents()
        section = docs["README.md"].split(
            "#### Overlapping monitors on GNOME")[1].split("\n### ")[0]
        m = re.search(r"Virtual-2 connected 1920x1080\+960\+0[^\n]*?"
                      r"(\d+mm x \d+mm)", section)
        self.assertTrue(m, "the sample --query line is gone from the section")
        self.assertEqual(m.group(1), "480mm x 270mm")
        for name, text in docs.items():
            self.assertNotIn("320mm x 200mm", text, name)

    @unittest.expectedFailure
    def test_the_uinput_opt_out_in_readme_debian_is_the_revoke_sequence(self):
        """Fix 56 (deferred: the author's call), finding F6.1.
        debian/README.Debian tells a reader who does not want the udev rule to
        `rm /usr/lib/udev/rules.d/60-w11-uinput.rules`.  Three things
        are wrong with that.  The file is dpkg's: removing it makes `dpkg -V
        w11` report the package as modified for ever, and the next
        upgrade puts it back.  `udevadm control --reload-rules` alone does not
        undo anything -- udev preserves permissions and ACLs no rule asks it to
        change, and its tags are sticky in its own database, so the node keeps
        the uaccess ACL until the next boot.  And the ACL entry has to be
        *removed*, not masked, because the uaccess builtin only ever adds
        entries: a left-over one makes a later reinstall a no-op.  The right
        opt-out is the one postrm already performs -- mask the rule with an
        empty `/etc/udev/rules.d/60-w11-uinput.rules`, which shadows
        the package's copy by name and survives upgrades, and then
        `install-bridge.sh --udev --uninstall` (or a shipped revoke helper) to
        clear what is already granted."""
        with open(os.path.join(ROOT, "debian", "README.Debian"),
                  encoding="utf-8") as f:
            text = f.read()
        para = text.split("If it is the wrong trade on this")[1].split("\n\n\n")[0]
        self.assertNotIn("rm /usr/lib/udev/rules.d/", para)
        self.assertTrue("install-bridge.sh --udev --uninstall" in para
                        or "uinput-revoke" in para, para)
        self.assertIn("/etc/udev/rules.d/60-w11-uinput.rules", para)

    def test_postrm_still_does_the_revoking_this_paragraph_should_name(self):
        """The premise of the test above, from the code: what `apt remove`
        does to the node is exactly the sequence the opt-out paragraph ought to
        describe, so if postrm ever stops doing it the paragraph is not the
        thing to fix."""
        with open(os.path.join(ROOT, "debian", "w11.postrm"),
                  encoding="utf-8") as f:
            sh = f.read()
        self.assertIn("setfacl -b /dev/uinput", sh)
        self.assertIn("/run/udev/tags/*/", sh)
        self.assertIn("chmod 0600 /dev/uinput", sh)

    def test_the_overlap_installers_exit_status_is_written_down(self):
        """It exits 1 on its ordinary success path, because the extension is
        installed and gnome-shell has not loaded it yet -- the same as the
        bridge installer, whose 1 the README does document.  A `set -e` script
        that follows the three steps stops at the first one."""
        with open(os.path.join(ROOT, "gnome", "install-overlap.sh"),
                  encoding="utf-8") as f:
            sh = f.read()
        # the path that says "log out and back in" really does exit non-zero
        self.assertIn("log out and back in once.  gnome-shell scans extension", sh)
        self.assertRegex(sh, r"comes up idle[^`]*?EOM\n    exit 1")
        self.assertIn("Exit status", sh)
        docs = documents()
        # ...and it is written where somebody is being told to type it
        section = docs["README.md"].split("#### Overlapping monitors on GNOME")[1]
        section = section.split("\n### ")[0]
        self.assertIn("sh gnome/install-overlap.sh", section)
        self.assertIn("exits 1", section)
        line = [ln for ln in docs["gnome/README.md"].splitlines()
                if ln.startswith("sh gnome/install-overlap.sh ")]
        self.assertTrue(line and "exit 1" in line[0], line)


class OneVersionEverywhere(unittest.TestCase):
    """Three files besides `w11common.VERSION` spell this release's version,
    and nothing but this test makes them agree.

    The 0.3-package-in-a-0.4-tree failure was one half of this: the file name
    said 0.3 while `w11common.VERSION` said 0.4, and every test in the suite
    read the second.  That half -- the name in `release/` and the `Version:`
    field inside the package -- is `ThePackageInTheTree` above.  This is the
    other: the three a release forgets, because nothing installs or runs them
    at test time -- pyproject.toml, the changelog stanza dpkg builds the
    package version out of, and flake.nix, which is what `nix build` and
    `nix run github:.../w11` produce."""

    def test_pyproject_says_it(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            self.assertEqual(tomllib.load(f)["project"]["version"], VERSION)

    def test_the_changelogs_top_stanza_says_it(self):
        """dpkg builds the package version out of this line, so a changelog
        that was not opened produces a package named for the previous
        release -- which is how a stale .deb gets committed without anybody
        noticing.  `scripts/build-deb.sh` refuses the build over it; this is
        the same check from the other end."""
        with open(os.path.join(ROOT, "debian", "changelog"), encoding="utf-8") as f:
            top = f.readline()
        m = re.match(r"w11 \(([^)]+)\) ", top)
        self.assertTrue(m, top)
        self.assertEqual(m.group(1), VERSION)

    def test_flake_nix_says_it(self):
        """The third install route in the README: `nix run` builds from this
        number, and a flake left behind produces a package whose --version
        disagrees with its own contents."""
        with open(os.path.join(ROOT, "flake.nix"), encoding="utf-8") as f:
            found = set(re.findall(r'version\s*=\s*"([0-9][^"]*)"', f.read()))
        self.assertEqual(found, {VERSION}, found)

    def test_the_rpm_spec_says_it_twice(self):
        """The Fedora package's Version: and the top %changelog entry, which
        rpm builds the package's name out of.  `sh scripts/build-rpm.sh`
        refuses the build over either; this is the same check from the other
        end, and it is the one that runs on every push."""
        with open(os.path.join(ROOT, "packaging", "rpm", "w11.spec"),
                  encoding="utf-8") as f:
            spec = f.read()
        self.assertEqual(re.findall(r"^Version: *(\S+)$", spec, re.M), [VERSION])
        entries = re.findall(r"^\* \w{3} \w{3} \d{2} \d{4} .+ - (\S+)-\d+$", spec, re.M)
        self.assertTrue(entries, "no %changelog entry in the spec")
        self.assertEqual(entries[0], VERSION)

    def test_the_pkgbuild_says_it(self):
        """pkgver, and pkgrel back at 1: a release that bumps the version and
        leaves pkgrel at 2 produces a package that sorts after itself."""
        with open(os.path.join(ROOT, "packaging", "arch", "PKGBUILD"),
                  encoding="utf-8") as f:
            recipe = f.read()
        self.assertEqual(re.findall(r"^pkgver=(\S+)$", recipe, re.M), [VERSION])
        self.assertEqual(re.findall(r"^pkgrel=(\S+)$", recipe, re.M), ["1"])

    def test_the_changelogs_top_heading_names_this_release(self):
        """CHANGELOG.md is the long form of the README's per-version section,
        and its top heading is what a reader compares against the package they
        installed.  It is written two-component (`## Version 0.4`) where
        pyproject is three (`0.4.0`) -- so are the git tags, v0.4 and v0.3 --
        so either spelling of THIS release passes and no other; which of the
        two the project settles on is design decision 2, and the day it does,
        this test keeps working."""
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            md = f.read()
        headings = re.findall(r"^## Version (\S+)\s*$", md, re.M)
        self.assertTrue(headings, "CHANGELOG.md has no `## Version` heading")
        short = VERSION[:-2] if VERSION.endswith(".0") else VERSION
        self.assertIn(headings[0], (VERSION, short), headings[:3])



class StdlibUnpacking(unittest.TestCase):
    """`unpack_deb()` and `dpkg-deb -x` produce the same tree.

    Three tests in this file skipped themselves when dpkg-deb was missing --
    on the one payload nothing else in the suite reads.  A .deb is an ar
    archive of three members and a zstd tar, both of which Python 3.14 has, so
    the skip was never necessary.  This is the proof that the replacement is
    not merely plausible: it is run against the real thing whenever dpkg-deb
    happens to be there."""

    @classmethod
    def setUpClass(cls):
        debs = sorted(n for n in os.listdir(RELEASE) if n.endswith(".deb"))
        if len(debs) != 1:
            raise unittest.SkipTest("release/ holds %d packages" % len(debs))
        cls.deb = os.path.join(RELEASE, debs[0])

    def trees(self):
        if shutil.which("dpkg-deb") is None:
            self.skipTest("no dpkg-deb to compare against")
        mine = tempfile.mkdtemp(prefix="w11-stdlib-")
        theirs = tempfile.mkdtemp(prefix="w11-dpkgdeb-")
        self.addCleanup(shutil.rmtree, mine, ignore_errors=True)
        self.addCleanup(shutil.rmtree, theirs, ignore_errors=True)
        unpack_deb(self.deb, mine)
        subprocess.run(["dpkg-deb", "-x", self.deb, theirs], check=True)
        return mine, theirs

    @staticmethod
    def walk(top):
        out = {}
        for base, _dirs, names in os.walk(top):
            for n in names:
                path = os.path.join(base, n)
                rel = os.path.relpath(path, top)
                if os.path.islink(path):
                    out[rel] = ("link", os.readlink(path))
                else:
                    with open(path, "rb") as f:
                        out[rel] = ("file", f.read())
        return out

    def test_the_same_names_come_out(self):
        mine, theirs = self.trees()
        self.assertEqual(sorted(self.walk(mine)), sorted(self.walk(theirs)))

    def test_the_same_bytes_and_the_same_symlink_target(self):
        mine, theirs = self.trees()
        a, b = self.walk(mine), self.walk(theirs)
        for rel in sorted(b):
            with self.subTest(rel):
                self.assertEqual(a[rel], b[rel])

    def test_the_symlink_is_a_symlink_and_not_a_copy(self):
        """The one member that is not a plain file, and the one
        `tarfile.data_filter` would have refused: a link to an absolute path.
        Extracting it as a copy would leave two files where the package has
        one, and `dpkg -V` would report the difference forever."""
        mine, _ = self.trees()
        link = os.path.join(mine, "etc/xdg/autostart/w11-enable-bridge.desktop")
        self.assertTrue(os.path.islink(link))


class TheGtkDependency(unittest.TestCase):
    """warandr is one tool of six and the only one that imports GTK."""

    @classmethod
    def setUpClass(cls):
        debs = sorted(n for n in os.listdir(RELEASE) if n.endswith(".deb"))
        if len(debs) != 1:
            raise unittest.SkipTest("release/ holds %d packages" % len(debs))
        cls.deb = os.path.join(RELEASE, debs[0])

    def field(self, name):
        if shutil.which("dpkg-deb") is None:
            self.skipTest("no dpkg-deb")
        out = subprocess.run(["dpkg-deb", "-f", self.deb, name],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_only_warandr_imports_gi(self):
        """The premise, and the reason the dependency is arguable at all:
        `import gi` appears in warandr/ and nowhere else, so five of the six
        tools work on a machine with no GTK at all."""
        # the seven directories dh_python3 installs into dist-packages, which
        # is the whole of what the Depends line is about (gen-gir.py imports
        # gi too, and is a build-time tool that is not in the package)
        importers = set()
        for pkg in ("w11common", "wdotool", "wwmctl", "wxprop", "wxrandr",
                    "warandr", "wmirror"):
            for base, dirs, names in os.walk(os.path.join(ROOT, pkg)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for n in names:
                    if not n.endswith(".py"):
                        continue
                    with open(os.path.join(base, n), encoding="utf-8",
                              errors="replace") as f:
                        if re.search(r"^\s*import gi\b", f.read(), re.M):
                            importers.add(pkg)
        self.assertEqual(importers, {"warandr"}, sorted(importers))

    @unittest.expectedFailure
    def test_the_gtk_stack_is_recommended_and_not_required(self):
        """Fix 57 (deferred: the author's call), finding F6.7.  `Depends:
        gir1.2-gtk-3.0, python3-gi` pulls the whole GTK 3 stack onto a machine
        that installed this for `wdotool type` on sway -- and apt will not
        install the package at all where that stack is unavailable.  Recommends
        is on by default in apt, so the GUI still arrives for everyone who has
        not turned it off, and the five command-line tools no longer depend on
        a toolkit they never import.  The alternative the author may take
        instead is to keep Depends and pin the rationale in debian/rules,
        which is why this is a documented gap and not a bug."""
        depends = self.field("Depends")
        recommends = self.field("Recommends")
        for pkg in ("gir1.2-gtk-3.0", "python3-gi"):
            self.assertIn(pkg, recommends)
            self.assertNotIn(pkg, depends)


if __name__ == "__main__":
    unittest.main()
