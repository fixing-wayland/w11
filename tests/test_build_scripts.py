#!/usr/bin/env python3
"""scripts/build-pyz.sh, build-deb.sh, build-rpm.sh and build-pkgbuild.sh.

Every one of them is run against a temporary copy of the tree, never against
the tree itself: `build-pyz.sh` writes into `dist/`, which parallel test runs
would race on, and `build-deb.sh` edits `debian/` and moves files into
`release/`, which is committed.  The copy is what makes it safe to run them at
all, and running them is the only way to know that six zipapps still contain
what their comments say they do.

The zipapps are the second install route (`git clone` plus one command, no
dpkg), and the claim about them is a claim about bundle contents: `wxrandr`,
`warandr` and `wmirror` deliberately carry no `wdotool/` -- that is 680 kB per
file, and it means those three do not drag in the keysym table, the input daemon
and four window backends for three small modules.  Nothing checked it, so the
saving was one careless `build` line from being silently undone.

`build-deb.sh` is checked for its refusals rather than for a build: a real one
needs dpkg-buildpackage, debhelper and eight minutes.  The refusal that matters
is the version gate -- pyproject.toml and debian/changelog disagreeing is
exactly how a package named for the previous release gets committed, which is
what happened between 0.3 and 0.4.

`build-rpm.sh` and `build-pkgbuild.sh` are its siblings and carry the same
option shape, so the same four cases transpose -- with two more places for the
version to be wrong (the spec's Version: AND its top %changelog) and one more
thing to prove: the recipe build-pkgbuild.sh generates has to carry the real
sha256 of the tarball it just made.  `SKIP` there would mean CI builds whatever
the tarball happened to be.  Neither is run for real: rpmbuild here dies on
Fedora's %pyproject_* macros and makepkg needs a /proc this sandbox has not got
[recon2/pkg-rpm.md 4, pkg-arch.md 2], so both get stubbed tools and a PATH that
holds nothing else -- which is also what makes "this tool is missing" a fact of
the test rather than of the machine running it.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from w11common import VERSION, passthrough                          # noqa: E402

PACKAGES = ("w11common", "wdotool", "wwmctl", "wxprop", "wxrandr", "warandr",
            "wmirror", "xw11")
TOOLS = ("wdotool", "wwmctl", "wxprop", "wxrandr", "warandr", "wmirror", "xw11")

#: What each zipapp is supposed to contain, out of build-pyz.sh's own `build`
#: lines and the comments above them.  `w11common` is in all six (session
#: discovery, the X11 handover, CmdError, stdio, procs); `wdotool` rides with
#: the two tools that import its window backends and its X wire client; the
#: three display tools carry none of it.
BUNDLES = {
    "wdotool": {"w11common", "wdotool"},
    "wwmctl": {"w11common", "wdotool", "wwmctl"},
    "wxprop": {"w11common", "wdotool", "wxprop"},
    "wxrandr": {"w11common", "wxrandr"},
    "warandr": {"w11common", "wxrandr", "warandr"},
    "wmirror": {"w11common", "wxrandr", "wmirror"},
    # the widest, and the only one that has to be: the proxy answers window
    # questions with wdotool's backends, property questions with wxprop's
    # synthesis and RandR with wxrandr's model.  `wwmctl` and `warandr` are
    # NOT in it -- nothing under xw11/ imports either, and a bundle is what
    # its build line says or the line is wrong.
    "xw11": {"w11common", "wdotool", "wxprop", "wxrandr", "xw11"},
}


def tree_copy(cls_or_case, extra=("scripts",)):
    """A temporary copy of the packages plus `extra` directories."""
    tmp = tempfile.mkdtemp(prefix="w11-build-")
    cls_or_case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    for d in PACKAGES + tuple(extra):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d),
                        ignore=shutil.ignore_patterns("__pycache__"))
    return tmp


class TheZipapps(unittest.TestCase):
    """One build for the whole class: it is six zipapps and takes a few
    seconds, and nothing here mutates what it produced."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="w11-pyz-")
        for d in PACKAGES + ("scripts",):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(cls.tmp, d),
                            ignore=shutil.ignore_patterns("__pycache__"))
        got = subprocess.run(["sh", os.path.join(cls.tmp, "scripts", "build-pyz.sh")],
                             capture_output=True, text=True, timeout=300)
        if got.returncode != 0:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise AssertionError("build-pyz.sh failed:\n%s" % got.stderr)
        cls.built = got.stdout

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def pyz(self, name):
        return os.path.join(self.tmp, "dist", name)

    def top_level(self, name):
        """The package directories inside one zipapp."""
        with zipfile.ZipFile(self.pyz(name)) as z:
            return {n.split("/")[0] for n in z.namelist() if "/" in n}

    def test_all_seven_are_built(self):
        for name in TOOLS:
            with self.subTest(name):
                self.assertTrue(os.path.exists(self.pyz(name)), name)
                self.assertTrue(os.access(self.pyz(name), os.X_OK), name)

    def test_each_bundle_holds_exactly_what_its_build_line_says(self):
        """The comments in build-pyz.sh are a design decision, and this is the
        only thing that keeps them true.  `wxrandr`, `warandr` and `wmirror`
        carrying no `wdotool/` is 680 kB off each of the three."""
        for name, want in BUNDLES.items():
            with self.subTest(name):
                self.assertEqual(self.top_level(name), want)

    def test_w11common_is_in_all_seven(self):
        """Every tool here finds its session, hands over to the original on an
        X11 one, raises the same exception when it fails and flushes through
        the same stdout on the way out -- all of which is w11common."""
        for name in TOOLS:
            with self.subTest(name):
                self.assertIn("w11common", self.top_level(name))

    def test_no_display_tool_carries_the_input_stack(self):
        for name in ("wxrandr", "warandr", "wmirror"):
            with self.subTest(name):
                self.assertNotIn("wdotool", self.top_level(name))

    def test_the_marker_and_the_stamp_are_in_the_first_four_kilobytes(self):
        """`w11common.passthrough.is_us()` sniffs the head of a file to decide
        whether an executable it is about to `execve` is one of ours -- the only
        "not us" guard that survives two copies under two names in two PATH
        directories.  zipapp stores members uncompressed and `__main__.py`
        sorts first, so the stamp lands in plain text near the front; the guard
        reads 4 KiB and no more."""
        for name in TOOLS:
            with self.subTest(name):
                with open(self.pyz(name), "rb") as fh:
                    head = fh.read(4096)
                self.assertIn(b"w11-clone:", head, name)
                self.assertIn(b"_w11_marker", head, name)

    def test_the_guard_itself_says_each_of_them_is_us(self):
        """Not a re-implementation of the sniff: the shipped function, run over
        the built file.  This is the test that would have caught a build that
        compressed its members or sorted them differently."""
        for name in TOOLS:
            with self.subTest(name):
                self.assertTrue(passthrough.is_us(self.pyz(name)), name)

    def test_four_of_them_run_and_print_their_version(self):
        """`wmirror` and `wxprop` need no session at all; `wxrandr` prints its
        program version and then, with no display, xrandr's own `Can't open
        display` and exit 1 -- which is the original's behaviour verbatim, so
        the assertion is on the line it did print.  `xw11 --help` is the fourth:
        the proxy's bundle is the one with four packages in it, so a missing
        import in any of them is a zipapp that cannot even print its usage."""
        env = dict(os.environ, W11_PASSTHROUGH="never",
                   WWMCTL_WMCTRL_GENERATION="1.07", LC_ALL="C")
        env.pop("PYTHONPATH", None)
        for argv, first in (("wmirror --version", "wmirror "),
                            ("wxprop -version", "xprop 1.2.8"),
                            ("xw11 --help", "usage: xw11 "),
                            ("wxrandr --version", "xrandr program version")):
            name, flag = argv.split()
            with self.subTest(argv):
                got = subprocess.run([sys.executable, self.pyz(name), flag],
                                     capture_output=True, text=True, timeout=120,
                                     env=env)
                self.assertTrue(got.stdout.startswith(first),
                                (got.returncode, got.stdout, got.stderr))
                if name != "wxrandr":
                    self.assertEqual(got.returncode, 0, got.stderr)

    def test_the_script_reports_every_file_it_built(self):
        names = re.findall(r"^built dist/(\w+) \(\d+ bytes\)$", self.built, re.M)
        self.assertEqual(names, list(TOOLS))


#: The real commands the three build scripts need.  PATH in the classes below
#: is the stubs and these and NOTHING ELSE: this guest happens to have a real
#: rpmbuild (the recon installed it), so "there is no rpmbuild here" cannot be
#: arranged by leaving out a stub while /usr/bin is still on the path.
REAL_TOOLS = ("sed", "head", "cat", "git", "mkdir", "rm", "cp", "tr",
              "sha256sum", "cut", "basename", "dirname", "sudo", "python3")


class BuildDebRefuses(unittest.TestCase):
    """build-deb.sh's three exits before it touches anything.

    A real build is not run here -- it wants dpkg-buildpackage and debhelper
    and it rewrites `release/` -- so the copy is given stub `sudo`, `apt-get`
    and `dpkg-query`, and every case below exits before the first of them is
    reached.  That the stubs are never called is itself asserted: a script that
    escalated before checking the version would be a script that asks for a
    password to tell you it will not build."""

    def setUp(self):
        self.tmp = tree_copy(self)
        for d in ("debian", "release"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(self.tmp, d))
        for name in ("pyproject.toml", "README.md"):
            shutil.copy2(os.path.join(ROOT, name), os.path.join(self.tmp, name))
        self.bin = os.path.join(self.tmp, "stubbin")
        self.log = os.path.join(self.tmp, "calls.log")
        os.makedirs(self.bin)
        for name in ("sudo", "apt-get", "dpkg-buildpackage", "dpkg-checkbuilddeps"):
            self.stub(name, "printf '%%s\\n' \"%s $*\" >> \"$FAKE_LOG\"\nexit 0\n" % name)
        # dpkg-query: every build tool reported installed, so the apt branch is
        # not reached unless a test says otherwise
        self.stub("dpkg-query", "printf '%s\\n' \"dpkg-query $*\" >> \"$FAKE_LOG\"\n"
                                "printf installed\nexit 0\n")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def run_build(self, *args):
        env = {"PATH": self.bin + ":/usr/bin:/bin", "FAKE_LOG": self.log,
               "HOME": self.tmp, "LC_ALL": "C"}
        return subprocess.run(["sh", os.path.join(self.tmp, "scripts", "build-deb.sh")]
                              + list(args), env=env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=300)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    def set_changelog_version(self, version):
        path = os.path.join(self.tmp, "debian", "changelog")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        head, rest = text.split("\n", 1)
        head = re.sub(r"\(([^)]+)\)", "(%s)" % version, head, count=1)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(head + "\n" + rest)

    def test_a_changelog_left_behind_stops_the_build_before_anything_else(self):
        """The 0.3-package-in-a-0.4-tree failure, from the direction that
        prevents it: `dpkg-buildpackage` reads the version out of
        debian/changelog and would have produced a package named 0.3.0 without
        a word of complaint."""
        stale = "0.0.1" if VERSION != "0.0.1" else "0.0.2"
        self.set_changelog_version(stale)
        got = self.run_build()
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("version mismatch", got.stderr)
        # the remedy it prints is the tree's own version, not a literal: the
        # gate has to move with the release, and a test that pins 0.4.0 goes
        # red at the 0.5 bump for a reason that is not build-deb.sh's.
        self.assertIn("dch -v %s" % VERSION, got.stderr)
        self.assertEqual(self.calls(), [], "it escalated before checking")

    def test_no_deps_with_a_missing_build_tool_names_the_tool(self):
        """`--no-deps` is for a machine where apt is not the answer -- a build
        chroot, a CI image, a developer who wants to know what is missing
        rather than have it installed."""
        self.stub("dpkg-query", "printf '%s\\n' \"dpkg-query $*\" >> \"$FAKE_LOG\"\n"
                                "case \"$*\" in *debhelper*) printf 'not-installed'; exit 1 ;; esac\n"
                                "printf installed\nexit 0\n")
        got = self.run_build("--no-deps")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("missing build tools: debhelper", got.stderr)
        self.assertIn("sudo apt-get install -y debhelper", got.stderr)
        self.assertNotIn("sudo apt-get install -y debhelper",
                         "\n".join(self.calls()), "it installed it anyway")

    def test_an_unknown_option_is_two(self):
        """Two, not one: `sh scripts/build-deb.sh --lintain` is a typo in the
        caller, and a wrapper script that distinguishes "you asked for
        something impossible" from "the build failed" wants them apart."""
        got = self.run_build("--bogus")
        self.assertEqual(got.returncode, 2, got.stdout + got.stderr)
        self.assertIn("unknown option: --bogus", got.stderr)

    def test_help_is_the_scripts_own_header(self):
        got = self.run_build("--help")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("release/w11_<version>_all.deb", got.stdout)
        self.assertIn("--no-deps", got.stdout)
        self.assertEqual(self.calls(), [])

    def test_the_script_parses(self):
        got = subprocess.run(["sh", "-n", os.path.join(ROOT, "scripts", "build-deb.sh")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((got.returncode, got.stderr), (0, ""))


class SiblingCase(unittest.TestCase):
    """A temporary clone with packaging/, a git history to `git archive`, and a
    PATH of stubs.  build-rpm.sh and build-pkgbuild.sh share every one of these
    pieces because they are deliberately the same script twice."""

    def setUp(self):
        self.tmp = tree_copy(self)
        for d in ("packaging",):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(self.tmp, d))
        # LICENSE with the other three: both scripts read its first line and
        # refuse a build whose spec / recipe tag disagrees with it.
        for name in ("pyproject.toml", "README.md", "CHANGELOG.md", "LICENSE"):
            shutil.copy2(os.path.join(ROOT, name), os.path.join(self.tmp, name))
        self.bin = os.path.join(self.tmp, "stubbin")
        self.realbin = os.path.join(self.tmp, "realbin")
        self.log = os.path.join(self.tmp, "calls.log")
        os.makedirs(self.bin)
        os.makedirs(self.realbin)
        for name in REAL_TOOLS:
            path = shutil.which(name)
            if path is None:
                self.skipTest("no %s on PATH" % name)
            os.symlink(path, os.path.join(self.realbin, name))
        # `python` and not `python3`: an Arch box has only the first, this one
        # usually only the second, and build-pkgbuild.sh asks for Arch's name
        # because that is what makepkg will run.
        os.symlink(shutil.which("python") or shutil.which("python3"),
                   os.path.join(self.realbin, "python"))
        for name in ("dnf", "pacman", "rpmlint", "namcap"):
            self.stub(name, "printf '%%s\\n' \"%s $*\" >> \"$FAKE_LOG\"\nexit 0\n" % name)

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def unstub(self, name):
        os.unlink(os.path.join(self.bin, name))

    def git_history(self):
        """One commit, so `git archive HEAD` has something to make a tarball
        out of.  The scripts read HEAD and not the working tree on purpose: a
        tarball of somebody's scratch state is not the release."""
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
        for argv in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["commit", "-qm", "tree"]):
            subprocess.run(["git"] + argv, cwd=self.tmp, env=env, check=True,
                           capture_output=True, timeout=120)

    def run_script(self, name, *args):
        env = {"PATH": self.bin + ":" + self.realbin, "FAKE_LOG": self.log,
               "HOME": self.tmp, "LC_ALL": "C"}
        # /bin/sh by path: PATH here is the stubs and REAL_TOOLS, and putting
        # a shell in it would be putting one more thing in the way of the
        # question these tests ask.
        return subprocess.run(["/bin/sh", os.path.join(self.tmp, "scripts", name)]
                              + list(args), env=env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=300)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    def edit(self, rel, pattern, replacement):
        path = os.path.join(self.tmp, rel)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        new, n = re.subn(pattern, replacement, text, count=1, flags=re.M)
        self.assertEqual(n, 1, "%s: %r matched %d times" % (rel, pattern, n))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)

    def stale(self):
        return "0.0.1" if VERSION != "0.0.1" else "0.0.2"


class BuildRpmRefuses(SiblingCase):
    """build-rpm.sh's four exits, and the two halves of its version gate."""

    SPEC = "packaging/rpm/w11.spec"
    #: What the rpmbuild stub leaves behind.  Built from w11common.VERSION and
    #: not typed, because a test that pins 0.4.0 goes red at the 0.5 bump for a
    #: reason that is not the script's -- the rule the .deb half of this file
    #: already states over `dch -v`.
    RPM_NAME = "w11-%s-1.fc44.noarch.rpm" % VERSION

    def setUp(self):
        super().setUp()
        # a working Fedora-shaped rpm: the two macro packages answer, so the
        # tool check passes and the version gate is what a test is measuring
        self.stub("rpm", "printf '%s\\n' \"rpm $*\" >> \"$FAKE_LOG\"\n"
                         "case \"$2\" in\n"
                         "  '%{_udevrulesdir}') echo /usr/lib/udev/rules.d ;;\n"
                         "  '%{pyproject_wheel}') echo '  %__python3 -m build' ;;\n"
                         "esac\nexit 0\n")
        self.stub("rpmbuild",
                  "printf '%s\\n' \"rpmbuild $*\" >> \"$FAKE_LOG\"\n"
                  "for a in \"$@\"; do case $a in _topdir*) top=${a#_topdir } ;; esac; done\n"
                  "mkdir -p \"$top/RPMS/noarch\"\n"
                  ": > \"$top/RPMS/noarch/" + self.RPM_NAME + "\"\n"
                  "exit 0\n")

    def test_a_spec_left_behind_stops_the_build_before_anything_else(self):
        """The 0.3-package-in-a-0.4-tree failure, in its Fedora form: rpmbuild
        reads the version out of the spec and would produce a package named for
        the previous release without a word of complaint."""
        self.edit(self.SPEC, r"^Version: .*$", "Version:        %s" % self.stale())
        got = self.run_script("build-rpm.sh")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("version mismatch", got.stderr)
        self.assertIn("spec Version:", got.stderr)
        self.assertEqual(self.calls(), [], "it reached for a tool first")

    def test_a_changelog_entry_left_behind_stops_it_too(self):
        """The second half of the gate, and the one a person forgets: the
        Version: is bumped by hand and the %changelog is not, so dnf shows a
        release note for the previous version beside the new package."""
        self.edit(self.SPEC, r"^\* (\w{3} \w{3} \d{2} \d{4} .+) - \S+-1$",
                  r"* \1 - %s-1" % self.stale())
        got = self.run_script("build-rpm.sh")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("%changelog:", got.stderr)
        self.assertIn(self.stale(), got.stderr)
        self.assertEqual(self.calls(), [])

    def test_no_deps_with_a_missing_tool_names_it(self):
        """`--no-deps` is for a machine where dnf is not the answer -- a mock
        chroot, a CI container, somebody who wants to know what is missing
        rather than have it installed."""
        self.unstub("rpmbuild")
        got = self.run_script("build-rpm.sh", "--no-deps")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("missing build tools: rpm-build", got.stderr)
        self.assertIn("sudo dnf install -y rpm-build", got.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("dnf")], [],
                         "it installed it anyway")

    def test_no_deps_names_the_macro_packages_a_plain_rpm_lacks(self):
        """The failure the recon hit: rpm 6.0.1 outside Fedora parses the spec
        and then dies half way through at "%pyproject_buildrequires: not
        found".  A macro package has no binary of its own, so the check is
        `rpm --eval` -- and this is the case that proves it looks."""
        self.stub("rpm", "printf '%s\\n' \"rpm $*\" >> \"$FAKE_LOG\"\n"
                         "case \"$2\" in '%{_udevrulesdir}') echo /usr/lib/udev/rules.d ;;\n"
                         "  *) echo \"$2\" ;; esac\nexit 0\n")
        got = self.run_script("build-rpm.sh", "--no-deps")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("pyproject-rpm-macros", got.stderr)

    def test_an_unknown_option_is_two(self):
        got = self.run_script("build-rpm.sh", "--lintian")
        self.assertEqual(got.returncode, 2, got.stdout + got.stderr)
        self.assertIn("unknown option: --lintian", got.stderr)

    def test_help_is_the_scripts_own_header(self):
        got = self.run_script("build-rpm.sh", "--help")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("--no-deps", got.stdout)
        self.assertIn("--lint", got.stdout)
        self.assertIn("dist/w11-<version>", got.stdout)
        self.assertNotIn("set -eu", got.stdout, "the slice ran past the header")
        self.assertEqual(self.calls(), [])

    def test_a_build_leaves_the_shipped_spec_alone(self):
        """The spec is copied into the scratch topdir and nothing rewrites it,
        so a build cannot leave the tree dirty -- and the rpms land in dist/,
        never in release/, because one rpm is one Fedora release and the .deb's
        "one file for both" story has no counterpart here."""
        self.git_history()
        got = self.run_script("build-rpm.sh", "--no-deps", "--keep")
        self.assertEqual(got.returncode, 0, got.stderr)
        with open(os.path.join(self.tmp, self.SPEC), encoding="utf-8") as fh:
            shipped = fh.read()
        with open(os.path.join(ROOT, "packaging", "rpm", "w11.spec"),
                  encoding="utf-8") as fh:
            self.assertEqual(shipped, fh.read(), "the build edited the spec in the tree")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "dist", self.RPM_NAME)))
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "release")))

    def test_a_spec_tagged_with_terms_the_license_left_behind_stops_the_build(self):
        """The licence gate, which is the version gate's shape over the other
        thing a stale packaging file gets wrong.  A package whose License: tag
        names terms the tree has moved off is invisible from outside: rpm shows
        the tag, nobody diffs it against the LICENSE, and the tag is what a
        redistributor acts on."""
        self.edit(self.SPEC, r"^License: .*$", "License:        MIT")
        got = self.run_script("build-rpm.sh")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("licence mismatch", got.stderr)
        self.assertIn("BSD-2-Clause", got.stderr)
        self.assertIn("MIT", got.stderr)
        self.assertEqual(self.calls(), [], "it reached for a tool first")

    def test_a_license_the_table_cannot_name_stops_the_build(self):
        """Not a fallback to a LicenseRef: a package that ships a licence file
        under the wrong SPDX expression is worse than one that will not build,
        and the message says both ways to fix it."""
        with open(os.path.join(self.tmp, "LICENSE"), "w", encoding="utf-8") as fh:
            fh.write("Copyright (c) 2026 somebody, all rights reserved\n")
        got = self.run_script("build-rpm.sh")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("names no SPDX id this table knows", got.stderr)
        self.assertIn("SPDX-License-Identifier header on line 1", got.stderr)
        self.assertEqual(self.calls(), [], "it reached for a tool first")

    def test_the_script_parses(self):
        got = subprocess.run(["sh", "-n", os.path.join(ROOT, "scripts", "build-rpm.sh")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((got.returncode, got.stderr), (0, ""))


class BuildPkgbuildRefuses(SiblingCase):
    """build-pkgbuild.sh, and the recipe it generates."""

    RECIPE = "packaging/arch/PKGBUILD"
    #: What the makepkg stub leaves behind; see BuildRpmRefuses.RPM_NAME.
    PKG_NAME = "w11-%s-1-any.pkg.tar.zst" % VERSION

    def setUp(self):
        super().setUp()
        self.stub("makepkg",
                  "printf '%s\\n' \"makepkg $*\" >> \"$FAKE_LOG\"\n"
                  ": > " + self.PKG_NAME + "\nexit 0\n")

    def test_a_recipe_left_behind_stops_the_build_before_anything_else(self):
        self.edit(self.RECIPE, r"^pkgver=.*$", "pkgver=%s" % self.stale())
        got = self.run_script("build-pkgbuild.sh")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("version mismatch", got.stderr)
        self.assertIn("pkgver=%s" % VERSION, got.stderr)
        self.assertEqual(self.calls(), [], "it reached for a tool first")

    def test_no_deps_with_a_missing_tool_names_it(self):
        self.unstub("makepkg")
        got = self.run_script("build-pkgbuild.sh", "--no-deps")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("missing build tools: makepkg", got.stderr)
        self.assertIn("pacman -S --needed --noconfirm", got.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("pacman")], [])

    def test_an_unknown_option_is_two(self):
        got = self.run_script("build-pkgbuild.sh", "--lintian")
        self.assertEqual(got.returncode, 2, got.stdout + got.stderr)
        self.assertIn("unknown option: --lintian", got.stderr)

    def test_help_is_the_scripts_own_header(self):
        got = self.run_script("build-pkgbuild.sh", "--help")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("--no-deps", got.stdout)
        self.assertIn("--keep", got.stdout)
        self.assertIn("in a container, be an ordinary user", got.stdout)
        self.assertNotIn("set -eu", got.stdout, "the slice ran past the header")
        self.assertEqual(self.calls(), [])

    def generated(self):
        """One full run with the stub makepkg, keeping the scratch directory."""
        self.git_history()
        got = self.run_script("build-pkgbuild.sh", "--no-deps", "--keep")
        self.assertEqual(got.returncode, 0, got.stderr)
        path = os.path.join(self.tmp, "dist", "pkgbuild", "PKGBUILD.local")
        self.assertTrue(os.path.exists(path), got.stdout)
        with open(path, encoding="utf-8") as fh:
            return got, fh.read()

    def test_the_generated_recipe_carries_a_real_sha256_and_never_skip(self):
        """`SKIP` in what CI builds would mean the tarball is whatever was
        there -- which, for a recipe whose whole job is to prove the tree
        builds, is the one thing that must not be true.  The sum is asserted
        against the tarball on disk, not merely against a regular
        expression."""
        _got, recipe = self.generated()
        # the recipe's own header comment says the word, which is the point of
        # it; what must not carry it is a declaration
        code = "\n".join(ln for ln in recipe.splitlines()
                          if not ln.lstrip().startswith("#"))
        self.assertNotIn("SKIP", code)
        m = re.search(r"^sha256sums=\('([0-9a-f]{64})'\)$", recipe, re.M)
        self.assertTrue(m, recipe)
        tarball = os.path.join(self.tmp, "dist", "pkgbuild",
                               "w11-%s.tar.gz" % VERSION)
        with open(tarball, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), m.group(1))

    def test_the_generated_recipe_points_at_the_local_tarball(self):
        """The shipped recipe's source line names upstream's two-component tag
        (v0.4, measured), which is right for an AUR user and wrong for CI: the
        job builds the commit it is testing.  Three lines differ and no more --
        source, sha256sums and _srcdir."""
        _got, recipe = self.generated()
        self.assertIn('source=("w11-%s.tar.gz")' % VERSION, recipe)
        self.assertIn('_srcdir="$pkgname-$pkgver"', recipe)
        with open(os.path.join(ROOT, "packaging", "arch", "PKGBUILD"),
                  encoding="utf-8") as fh:
            shipped = fh.read()
        differ = [a for a, b in zip(recipe.splitlines(), shipped.splitlines()) if a != b]
        self.assertEqual(len(differ), 3, differ)

    def test_the_build_leaves_nothing_beside_the_shipped_recipe(self):
        """PKGBUILD.local goes into dist/, which .gitignore already covers, so
        a build does not leave a file `git status` has to explain."""
        self.generated()
        left = sorted(os.listdir(os.path.join(self.tmp, "packaging", "arch")))
        self.assertEqual(left, ["PKGBUILD", "README.Arch", "namcap.expected",
                                "w11.install"])

    def test_a_recipe_tagged_with_terms_the_license_left_behind_stops_the_build(self):
        """build-rpm.sh's licence gate, in its Arch form.  `pacman -Qi` prints
        Licenses and namcap checks the file under /usr/share/licenses against
        it, so a recipe naming terms the tree has moved off ships a package
        whose own metadata is wrong about what it may be redistributed under."""
        self.edit(self.RECIPE, r"^license=\(.*\)$", "license=('MIT')")
        got = self.run_script("build-pkgbuild.sh")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("licence mismatch", got.stderr)
        self.assertIn("BSD-2-Clause", got.stderr)
        self.assertIn("MIT", got.stderr)
        self.assertEqual(self.calls(), [], "it reached for a tool first")

    def test_the_script_parses(self):
        got = subprocess.run(["sh", "-n",
                              os.path.join(ROOT, "scripts", "build-pkgbuild.sh")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual((got.returncode, got.stderr), (0, ""))


if __name__ == "__main__":
    unittest.main()
