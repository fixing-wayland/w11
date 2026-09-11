#!/usr/bin/env python3
"""packaging/rpm/w11.spec read as text: does it ship what debian/ ships?

An RPM of this tree is a translation of debian/, not a port -- every payload
path exists on Fedora under the same name -- and the failure mode of a
translation is that one side gains a file and the other does not.  That is
invisible from either side: `sh scripts/build-deb.sh` keeps working, `sh
scripts/build-rpm.sh` keeps working, and the Fedora package quietly stops
carrying a typelib, or the udev rule, or the enabler.

So this reads the spec against debian/w11.install, debian/rules,
debian/w11.links, pyproject.toml and generations.json, through one path
map (usr/lib/udev/rules.d -> %{_udevrulesdir}, usr/lib/w11 ->
%{_libexecdir}/w11, and so on).  Nothing here needs rpmbuild: the
%pyproject_* macros are Fedora-only and this guest's rpm 6.0.1 dies at
"%pyproject_buildrequires: not found" [recon2/pkg-rpm.md 4], so the first real
build is the CI `rpm` job in fedora:44 and everything that can be held without
one is held here.

The scriptlets are checked here for their text -- the four udev commands, in
debian's order -- and RUN as processes in tests/test_rpm_scripts.py.

The licence tag is BSD-2-Clause (HEAD 375d815) and %files carries %license
LICENSE; scripts/build-rpm.sh's spdx_id() reads the id back out of that file
and refuses a build where the spec disagrees with it.  Both ends are tested:
the tag against the file, and the reader itself run as a process over a
temporary tree with nine different licences planted in it.
"""

import json
import os
import re
import shutil
import subprocess
import sys
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

import support                                                    # noqa: E402
from w11common import VERSION                                     # noqa: E402

SPEC = os.path.join(ROOT, "packaging", "rpm", "w11.spec")
RPMLINTRC = os.path.join(ROOT, "packaging", "rpm", "w11.rpmlintrc")
BUILD_RPM = os.path.join(ROOT, "scripts", "build-rpm.sh")
DEB_INSTALL = os.path.join(ROOT, "debian", "w11.install")
DEB_RULES = os.path.join(ROOT, "debian", "rules")
DEB_LINKS = os.path.join(ROOT, "debian", "w11.links")
GENERATIONS = os.path.join(ROOT, "gnome", "w11-overlap@w11",
                           "generations.json")

BRIDGE_UUID = "w11-bridge@w11"
OVERLAP_UUID = "w11-overlap@w11"

#: The spec's own %global definitions plus the two macros rpm defines for it.
#: Expanding these is what lets a dpkg-side path be compared with a spec-side
#: one as a string.
GLOBALS = {
    "%{bridge_uuid}": BRIDGE_UUID,
    "%{overlap_uuid}": OVERLAP_UUID,
    "%{name}": "w11",
    "%{version}": VERSION,
}

#: dpkg's directory -> the spec's spelling of it.  Every one of these is a
#: real difference of layout or of convention, not a rename:
#:
#:   * %{_udevrulesdir} and %{_modulesloaddir} come from systemd-rpm-macros and
#:     are /usr/lib/udev/rules.d and /usr/lib/modules-load.d -- the same two
#:     directories, named by macro because a spec that hard-codes them breaks
#:     on a distribution that moves them;
#:   * %{_libexecdir}/w11 is /usr/libexec, which is where Fedora puts a
#:     program no user runs by hand; dpkg has no /usr/libexec and uses
#:     /usr/lib/w11;
#:   * the autostart entry is a PLAIN FILE under %{_sysconfdir} where debian/
#:     needs a symlink, because debhelper makes every regular file under /etc a
#:     conffile and a conffile survives `apt remove`.  rpm marks nothing
#:     %config unless the spec says so.
PATH_MAP = {
    "usr/lib/udev/rules.d": "%{_udevrulesdir}",
    "usr/lib/modules-load.d": "%{_modulesloaddir}",
    "usr/share/applications": "%{_datadir}/applications",
    "usr/share/gnome-shell/extensions": "%{_datadir}/gnome-shell/extensions",
    "usr/lib/w11": "%{_libexecdir}/%{name}",
    "etc/xdg/autostart": "%{_sysconfdir}/xdg/autostart",
}

#: One deb source path whose spec counterpart is spelled differently on
#: purpose: debhelper's .install takes a bare glob, and `install -pm 0644` in a
#: spec wants a suffix or it would try to install the directory itself.
SOURCE_ALIASES = {
    "gnome/%s/typelib/*" % OVERLAP_UUID: "gnome/%s/typelib/*.typelib" % OVERLAP_UUID,
}

#: Every rpmlint finding this package accepts, and the whole of it.  The
#: rpmlintrc is compared against this list in both directions: a filter that
#: has stopped being needed is a finding too, which is the rule
#: debian/w11.lintian-overrides is held to.
#:
#: zero-perms-ghost is deliberately NOT here.  The recon saw it (rpmlint 2.7.0
#: on the three packages built on this guest) and the answer was to fix it --
#: `%ghost %attr(0644,root,root)` -- rather than to filter it.
#: invalid-license is deliberately NOT here either, and was until HEAD
#: 375d815: while the spec carried a LicenseRef placeholder rpmlint said so
#: once per package.  BSD-2-Clause is a valid SPDX expression, so the filter
#: went with the placeholder.
ACCEPTED_FINDINGS = (
    "non-conffile-in-etc",
    "spelling-error",
    "no-manual-page-for-binary",
)

DIRECTIVE = re.compile(
    r"^%(package|description|prep|generate_buildrequires|build|install|check"
    r"|pre|post|preun|postun|posttrans|files|changelog)\b(.*)$")


def spec_text():
    with open(SPEC, encoding="utf-8") as fh:
        return fh.read()


def expand(text):
    """The spec's %global names replaced by what they stand for."""
    for macro, value in GLOBALS.items():
        text = text.replace(macro, value)
    return text


def sections(text=None):
    """[(header, body)] in file order, header being the whole directive line.

    A spec has %files and %description more than once (one of each per
    subpackage), so this is a list and not a dict: collapsing it would hide
    exactly the mistake of putting a file in the wrong subpackage."""
    text = spec_text() if text is None else text
    out = []
    header, body = None, []
    for line in text.splitlines():
        if DIRECTIVE.match(line):
            if header is not None:
                out.append((header, "\n".join(body)))
            header, body = line.strip(), []
        elif header is not None:
            body.append(line)
    if header is not None:
        out.append((header, "\n".join(body)))
    return out


def preamble(text=None):
    """Everything before the first directive: the tags of the main package."""
    text = spec_text() if text is None else text
    for line in text.splitlines():
        if DIRECTIVE.match(line):
            return text[:text.index(line)]
    return text


def section(name, text=None):
    """The one body of `%name`; an assertion if there is not exactly one."""
    found = [b for h, b in sections(text) if h.split()[0] == "%" + name]
    if len(found) != 1:
        raise AssertionError("%s: %d %%%s sections" % (SPEC, len(found), name))
    return found[0]


def main_files(text=None):
    """The main package's %files body: the one with `-f %{pyproject_files}`.

    There are three %files sections and picking the wrong one is exactly the
    mistake of putting a file in the wrong subpackage, so this is by name."""
    found = [b for h, b in sections(text) if h.startswith("%files -f ")]
    if len(found) != 1:
        raise AssertionError("%s: %d main %%files sections" % (SPEC, len(found)))
    return found[0]


def tag_values(name, text=None):
    """Every `Name: value` in the main package's preamble, in order."""
    return re.findall(r"^%s: *(.+?)\s*$" % name, preamble(text), re.M)


def deb_install_lines():
    """debian/w11.install as [(source, destination directory)]."""
    out = []
    with open(DEB_INSTALL, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            src, dest = line.split()
            out.append((src, dest))
    return out


def deb_rules_installs():
    """The `install -D -m ... SRC DEST` pairs debian/rules runs by hand.

    Written across two lines with a backslash, so the file is joined first --
    and the separator that is left is a space followed by the continuation
    line's TAB, which is why the whitespace classes here are \\s and not the
    ` +` they were.  With ` +` this returned [] and every caller compared an
    empty list against the spec and passed."""
    with open(DEB_RULES, encoding="utf-8") as fh:
        text = fh.read().replace("\\\n", " ")
    out = []
    for m in re.finditer(r"^\tinstall -D -m \d+\s+(\S+)\s+(\S+)\s*$", text, re.M):
        src, dest = m.group(1), m.group(2)
        # debian/w11/ is the package build directory
        dest = re.sub(r"^debian/w11/", "", dest)
        out.append((src, dest))
    return out


def deb_links():
    """debian/w11.links as {installed path: the symlink over it}.

    The .deb installs the autostart entry under /usr/lib and links it into
    /etc/xdg/autostart because debhelper makes every regular file under /etc a
    conffile and a conffile survives `apt remove`.  Neither rpm nor pacman has
    that rule, so both write the file straight into the autostart directory --
    a destination that is the .deb's own, stated here, rather than a path this
    test file invents."""
    out = {}
    with open(DEB_LINKS, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            installed, link = line.split()
            out[installed] = link
    return out


class TheVersion(unittest.TestCase):
    """The spec is the fifth place this release's version is written, and the
    sixth is its own %changelog.  scripts/build-rpm.sh refuses the build over
    either; this is the same check from the other end, and it is the check
    that would have caught the 0.3-package-in-a-0.4-tree failure had there
    been an rpm then."""

    def test_the_version_tag_is_w11commons(self):
        self.assertEqual(tag_values("Version"), [VERSION])

    def test_the_top_changelog_entry_names_this_release(self):
        """rpm validates the date format; what can go stale is the version at
        the end of the line, which is what dnf shows and what a user compares
        against the file they installed."""
        head = [ln for ln in section("changelog").splitlines() if ln.strip()][0]
        m = re.match(r"^\* \w{3} \w{3} \d{2} \d{4} .+ - (\S+)-(\d+)$", head)
        self.assertTrue(m, head)
        self.assertEqual(m.group(1), VERSION)
        self.assertEqual(m.group(2), "1")

    def test_the_release_field_carries_the_dist_tag(self):
        """One noarch build covers Fedora 43 and 44 (both python3 3.14.7) and
        not rawhide (3.15), so the three files a release produces differ in
        %{?dist} and in nothing else."""
        self.assertEqual(tag_values("Release"), ["1%{?dist}"])

    def test_it_is_noarch(self):
        self.assertEqual(tag_values("BuildArch"), ["noarch"])


class EveryPathTheDebShips(unittest.TestCase):
    """The two packagings carry the same payload, through PATH_MAP.

    Both halves are asserted, because the two ways to drift are opposite: a
    file added to debian/ and forgotten here (the source-side test), and a
    directory renamed on one side only (the destination-side test)."""

    def setUp(self):
        self.spec = expand(spec_text())
        self.install = section("install", self.spec)

    def deb_sources(self):
        srcs = [s for s, _d in deb_install_lines()]
        srcs += [s for s, _d in deb_rules_installs()]
        return sorted(set(SOURCE_ALIASES.get(s, s) for s in srcs))

    def test_every_source_file_the_deb_installs_is_named_in_percent_install(self):
        """dpkg's list is the reference because it is the one a release is
        built from today.  A new data file added to debian/w11.install
        and not to the spec is a Fedora package missing it, and nothing else
        would say so."""
        self.maxDiff = None
        missing = [s for s in self.deb_sources() if s not in self.install]
        self.assertEqual(missing, [])

    def test_the_hand_written_install_lines_in_debian_rules_are_read(self):
        """The guard on the two tests either side of it.  deb_rules_installs()
        joins a backslash continuation and then has to match across a space and
        a TAB; while its regex wanted spaces it returned [], and both halves of
        this class compared an empty list against the spec and passed."""
        pairs = deb_rules_installs()
        self.assertGreaterEqual(len(pairs), 3, pairs)
        self.assertIn(("packaging/common/enable-bridge",
                       "usr/lib/w11/enable-bridge"), pairs)

    def test_every_destination_directory_maps_onto_a_macro_the_spec_uses(self):
        self.maxDiff = None
        whole = self.spec
        links = deb_links()
        missing = []
        for _src, dest in deb_install_lines() + deb_rules_installs():
            # The .deb's own restatement of where a file really lands: it
            # installs the .desktop under /usr/lib and symlinks it into
            # /etc/xdg/autostart, and the spec writes it straight there.
            dest = links.get(dest, dest)
            head = dest
            while head and head not in PATH_MAP:
                head = os.path.dirname(head)
            if not head:
                missing.append("%s: no PATH_MAP entry" % dest)
                continue
            want = expand(PATH_MAP[head] + dest[len(head):])
            if want not in whole:
                missing.append("%s -> %s" % (dest, want))
        self.assertEqual(missing, [])

    def test_the_enabler_and_its_desktop_come_from_packaging_common(self):
        """Design decision 8.  They are a per-user gsettings enable and an XDG
        autostart entry -- needed identically by three packagings -- and a spec
        reaching into a directory named for dpkg was the thing that said they
        were in the wrong place."""
        self.assertIn("packaging/common/enable-bridge", self.install)
        self.assertIn("packaging/common/enable-bridge.desktop", self.install)
        self.assertNotIn("debian/enable-bridge", self.install)

    def test_the_autostart_entrys_exec_line_is_rewritten_for_libexec(self):
        """The one difference between the .deb's copy of the .desktop and the
        rpm's: Exec= names %{_libexecdir}/w11/enable-bridge, because
        that is where the helper goes on Fedora.  The script itself is
        installed unchanged, which is what makes one file serve both."""
        self.assertIn("sed 's|^Exec=.*|Exec=%{_libexecdir}/%{name}/enable-bridge|'",
                      section("install"))
        with open(os.path.join(ROOT, "packaging", "common", "enable-bridge.desktop"),
                  encoding="utf-8") as fh:
            self.assertIn("Exec=/usr/lib/w11/enable-bridge", fh.read())

    def test_the_autostart_entry_is_a_plain_file_and_not_a_symlink(self):
        """debian/w11.links exists because debhelper turns a regular
        file under /etc into a conffile that survives `apt remove`.  rpm has no
        such rule, so the file goes straight in and an erase takes it -- and
        the spec must not have copied the symlink dance across."""
        with open(DEB_LINKS, encoding="utf-8") as fh:
            self.assertIn("etc/xdg/autostart", fh.read())
        entry = "%{_sysconfdir}/xdg/autostart/w11-enable-bridge.desktop"
        self.assertIn(entry, spec_text())
        self.assertNotIn("ln -s", section("install"))


class TheConsoleScripts(unittest.TestCase):
    """Three lists of six names that have to agree: pyproject's entry points,
    what the spec lists under %{_bindir}, and what dpkg installs."""

    def test_bindir_is_exactly_the_project_scripts_table(self):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
            want = sorted(tomllib.load(fh)["project"]["scripts"])
        body = main_files()
        got = sorted(re.findall(r"^%\{_bindir\}/(\w+)$", body, re.M))
        self.assertEqual(got, want)

    def test_they_are_listed_beside_pyproject_files_and_the_comment_says_why(self):
        """The one line in the spec nobody could run here: whether
        %pyproject_save_files on pyproject-rpm-macros 1.23.2 also claims the six
        %{_bindir} entries.  The Fedora Packaging Guidelines' own example lists
        them by hand, and Fedora sets %_duplicate_files_terminate_build, so the
        CI `rpm` job settles it safely either way -- which is what the comment
        beside them has to say, because whoever reads that job's output needs
        to know why the lines are there."""
        body = main_files()
        self.assertIn("%pyproject_save_files", body)
        self.assertIn("%_duplicate_files_terminate_build", body)
        self.assertIn("no commands in it", body)


class TheExtensions(unittest.TestCase):
    """Two subpackages, Fedora's convention, and the policy that the overlap
    extension is installed by nobody automatically."""

    def headers(self):
        return [h for h, _b in sections()]

    def test_both_uuids_have_a_files_section(self):
        spec = expand(spec_text())
        for uuid in (BRIDGE_UUID, OVERLAP_UUID):
            with self.subTest(uuid):
                want = "%{_datadir}/gnome-shell/extensions/" + uuid + "/"
                bodies = [b for h, b in sections(spec)
                          if h.startswith("%files") and want in b]
                self.assertEqual(len(bodies), 1, want)

    def test_every_generation_in_the_table_has_its_typelib_installed(self):
        """generations.json is the single record of which libmutter layouts the
        overlap extension knows.  The spec installs typelib/*.typelib, so this
        asserts the glob covers every record's namespace -- the file that would
        be missing is one the extension needs to load on that GNOME."""
        with open(GENERATIONS, encoding="utf-8") as fh:
            table = json.load(fh)
        names = [g["namespace"] for g in table["generations"]]
        self.assertEqual(len(names), 3, names)
        src = os.path.join(ROOT, "gnome", OVERLAP_UUID, "typelib")
        for ns in names:
            with self.subTest(ns):
                self.assertTrue(os.path.exists(
                    os.path.join(src, "%s-1.0.typelib" % ns)), ns)
        self.assertIn("typelib/*.typelib", section("install"))

    def test_the_bridge_subpackage_supplements_both_halves(self):
        """`Supplements: (w11 and gnome-shell)` is the thing the .deb
        cannot say: dnf installs the bridge by itself on a machine that has
        both halves and on no sway or KDE box.  Measured working on this
        guest's rpm 6.0.1 (rich dependencies) [recon2/pkg-rpm.md 4]."""
        body = [b for h, b in sections()
                if h.startswith("%package -n gnome-shell-extension-%{name}-bridge")]
        self.assertEqual(len(body), 1)
        self.assertIn("Supplements:    (%{name} and gnome-shell)", body[0])

    def test_the_overlap_subpackage_has_no_supplements_at_all(self):
        """Nothing installs the one thing that can cost the session you are
        sitting in.  The same policy as every other packaging -- README.md,
        "Overlapping monitors on GNOME" -- expressed here as an absence, so
        the test has to be the absence."""
        body = [b for h, b in sections()
                if h.startswith("%package -n gnome-shell-extension-%{name}-overlap")]
        self.assertEqual(len(body), 1)
        self.assertEqual(re.findall(r"^Supplements:.*$", body[0], re.M), [])
        self.assertIn("Requires:       gnome-shell", body[0])


class TheDependencies(unittest.TestCase):
    """What dnf pulls in, and what it merely mentions."""

    def test_the_gtk_pair_is_recommended_and_not_required(self):
        """warandr is one tool of seven and the only importer of gi.  Weak deps
        are on by default in dnf, so the GUI still arrives for everyone who has
        not turned them off.  This is the deb's deferred fix 57 -- which
        tests/test_release_deb.py carries as an expectedFailure -- taken the
        other way, and the divergence is deliberate (design decision 5)."""
        self.assertEqual(tag_values("Recommends")[:2], ["python3-gobject", "gtk3"])
        self.assertEqual([v for v in tag_values("Requires") if "python3-gobject" in v], [])

    def test_the_handover_tools_are_recommended_by_fedoras_names(self):
        """Recommends and not Suggests since the X11 proxy: on a Wayland session
        the four clones hand over to the ORIGINAL running against xw11's display
        (design section 8.1), so the original is what a default install should
        have, and dnf installs weak dependencies unless they are turned off.
        Fedora ships one binary per package, where Debian has x11-utils and
        x11-xserver-utils, so this names four packages and not two (measured
        from Fedora's own metadata: /usr/bin/xprop -> xprop, /usr/bin/xrandr ->
        xrandr).  wl-mirror is the one weak dependency that stays a Suggests --
        nothing hands over to it, it is one tool's helper."""
        self.assertEqual(tag_values("Recommends")[2:],
                         ["xdotool", "wmctrl", "xprop", "xrandr"])
        self.assertEqual(tag_values("Suggests"), ["wl-mirror"])

    def test_it_does_not_require_acl(self):
        """Fedora's cloud image has no getfacl and no setfacl (measured), so
        %postun's python fallback is the normal path there.  Requiring acl for
        one line of teardown would put a package on every machine to undo a
        grant it may never have made."""
        self.assertEqual([v for v in tag_values("Requires") if v.strip() == "acl"], [])
        self.assertIn("os.removexattr", section("postun"))

    def test_the_build_needs_the_two_macro_packages(self):
        """python3-devel pulls pyproject-rpm-macros in; systemd-rpm-macros is
        what defines %{_udevrulesdir} and %{_modulesloaddir}.  Without the
        second the spec parses and installs the rule into a literal
        `%{_udevrulesdir}` directory."""
        self.assertEqual(tag_values("BuildRequires"),
                         ["python3-devel", "systemd-rpm-macros"])


class TheScriptletsAsText(unittest.TestCase):
    """What %post and %postun say.  What they DO is tests/test_rpm_scripts.py,
    which runs them as processes against a fake root."""

    #: debian/w11.postinst's udev half, in order.  Three copies of one
    #: procedure -- dpkg's, rpm's and pacman's -- and the order is the part
    #: that matters: the trigger has to follow the reload or the node is
    #: re-tagged against the rules that were in force before the install.
    UDEV = ("modprobe uinput",
            "udevadm control --reload-rules",
            "udevadm trigger --name-match=uinput",
            "udevadm settle --timeout=5")

    def test_post_runs_the_four_udev_commands_in_debians_order(self):
        body = section("post")
        at = [body.index(cmd) for cmd in self.UDEV]
        self.assertEqual(at, sorted(at), body)

    def test_post_reloads_the_rules_itself_and_says_why(self):
        """systemd-udev carries a transfiletrigger on /usr/lib/udev/rules.d
        (measured on Fedora 44: `rpm -q --filetriggers systemd-udev`), and it
        runs at the END of the transaction -- after this scriptlet.  So the
        reload here is not redundant with it, and the comment has to name the
        trigger or the next reader deletes the line."""
        body = section("post")
        self.assertIn("udevadm control --reload-rules", body)
        self.assertIn("transfiletrigger", body)
        self.assertIn("END of the transaction", body)

    def test_post_writes_the_stamp_the_enabler_reads(self):
        body = section("post")
        self.assertIn("%{_sharedstatedir}/%{name}/installed", body)
        with open(os.path.join(ROOT, "packaging", "common", "enable-bridge"),
                  encoding="utf-8") as fh:
            self.assertIn("/var/lib/w11/installed", fh.read())

    def test_postun_does_its_work_only_on_the_last_erase(self):
        """rpm has no purge: $1 == 0 is the last erase and $1 == 1 is an
        upgrade, where nothing here may run or every user is set up again."""
        body = section("postun")
        self.assertIn('if [ "$1" = 0 ]; then', body)

    def test_postun_is_debians_revoke_sequence(self):
        """The ACL entry has to go rather than be masked by the 0600: the
        uaccess builtin only ever ADDS entries, so a left-over one makes a
        later reinstall a no-op and leaves input broken."""
        body = section("postun")
        want = ("udevadm control --reload-rules",
                "/run/udev/static_node-tags/uaccess/uinput",
                "udevadm info -q property /dev/uinput",
                "setfacl -b /dev/uinput",
                "chown root:root /dev/uinput",
                "chmod 0600 /dev/uinput")
        at = [body.index(cmd) for cmd in want]
        self.assertEqual(at, sorted(at), body)

    def test_the_stamp_is_a_ghost_with_a_mode(self):
        """A %ghost with no %attr is mode 000 and rpmlint says zero-perms-ghost
        about it.  The file %post writes is 0644, and the fix is the mode
        rather than a line in the rpmlintrc."""
        body = main_files()
        self.assertIn("%ghost %attr(0644,root,root) %{_sharedstatedir}/%{name}/installed",
                      body)
        self.assertIn("%dir %{_sharedstatedir}/%{name}", body)


class TheLicence(unittest.TestCase):
    """The tag, the file %files ships, and the reader that keeps them equal.

    The tree has been BSD-2-Clause since HEAD 375d815, whose LICENSE begins
    with an SPDX-License-Identifier header; the id is read out of that file
    here rather than typed, so a relicence moves the spec and not this test.
    The reader itself, spdx_id(), is sliced out of scripts/build-rpm.sh and run
    over a temporary tree with a LICENSE planted in it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11-spdx-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.body = support.sh_function(BUILD_RPM, "spdx_id")

    def run_spdx(self, first_line=None, text=None):
        if text is None and first_line is not None:
            text = first_line + "\n\nthe rest of the licence text\n"
        if text is not None:
            with open(os.path.join(self.tmp, "LICENSE"), "w", encoding="utf-8") as fh:
                fh.write(text)
        script = os.path.join(self.tmp, "case.sh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("set -eu\n" + self.body + "spdx_id\n")
        return subprocess.run(["sh", script], cwd=self.tmp, capture_output=True,
                              text=True, timeout=60)

    def test_the_license_tag_is_the_id_the_license_file_states(self):
        """Fedora requires a valid SPDX expression and rpmlint says
        invalid-license over anything else -- which is why the rpmlintrc has no
        such filter any more.  A spec tagged with terms the tree has left
        behind is the one packaging error nobody can see from outside."""
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            first = fh.readline().strip()
        self.assertTrue(first.startswith("SPDX-License-Identifier:"), first)
        self.assertEqual(tag_values("License"), [first.split(":", 1)[1].strip()])

    def test_percent_files_ships_the_license_as_a_license(self):
        """%license and not %doc: rpm keeps a %license file through a
        `--nodocs` install, which is what Fedora's guidelines require of every
        package that carries one."""
        self.assertIn("%license LICENSE", main_files())

    def test_with_no_license_file_the_function_answers_nothing(self):
        got = self.run_spdx()
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertEqual(got.stdout, "")

    def test_a_licenses_first_line_becomes_an_spdx_id(self):
        for first, want in (("SPDX-License-Identifier: BSD-2-Clause", "BSD-2-Clause"),
                            ("SPDX-License-Identifier: GPL-3.0-or-later WITH Autoconf-exception-3.0",
                             "GPL-3.0-or-later WITH Autoconf-exception-3.0"),
                            ("MIT License", "MIT"),
                            ("ISC License", "ISC"),
                            ("                                 Apache License", "Apache-2.0"),
                            ("BSD 3-Clause License", "BSD-3-Clause"),
                            ("BSD 2-Clause License", "BSD-2-Clause"),
                            ("GNU General Public License v3.0", "GPL-3.0-or-later"),
                            ("GNU Lesser General Public License v3.0", "LGPL-3.0-or-later"),
                            ("GNU Affero General Public License v3.0", "AGPL-3.0-or-later"),
                            ("Mozilla Public License Version 2.0", "MPL-2.0")):
            with self.subTest(first):
                got = self.run_spdx(first)
                self.assertEqual((got.returncode, got.stdout.strip()), (0, want),
                                 got.stderr)

    def test_the_gnu_texts_are_told_apart_by_their_second_line(self):
        """The FSF's own files are the shape that breaks a one-line table:
        the family is on line 1 in upper case ("GNU GENERAL PUBLIC LICENSE")
        and the version is on line 2 ("Version 3, 29 June 2007"), so one line
        cannot tell GPL-2 from GPL-3 and a case-sensitive match answers
        neither.  GitHub's picker writes the title-case display name on one
        line instead, which is what the case above plants."""
        for text, want in (
                ("                    GNU GENERAL PUBLIC LICENSE\n"
                 "                       Version 3, 29 June 2007\n", "GPL-3.0-or-later"),
                ("                    GNU GENERAL PUBLIC LICENSE\n"
                 "                       Version 2, June 1991\n", "GPL-2.0-or-later"),
                ("                   GNU LESSER GENERAL PUBLIC LICENSE\n"
                 "                       Version 3, 29 June 2007\n", "LGPL-3.0-or-later"),
                ("                    GNU AFFERO GENERAL PUBLIC LICENSE\n"
                 "                       Version 3, 19 November 2007\n",
                 "AGPL-3.0-or-later")):
            with self.subTest(text.split("\n")[0].strip()):
                got = self.run_spdx(text=text)
                self.assertEqual((got.returncode, got.stdout.strip()), (0, want),
                                 got.stderr)

    def test_the_tree_own_license_is_read_as_bsd_2_clause(self):
        """The file the build actually reads, planted verbatim: its first line
        is the SPDX header, its "BSD 2-Clause License" heading is on line 3,
        and a reader that consulted only the heading table would stop the
        build here (measured: "names no SPDX id this table knows")."""
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            got = self.run_spdx(text=fh.read())
        self.assertEqual((got.returncode, got.stdout.strip()), (0, "BSD-2-Clause"),
                         got.stderr)

    def test_a_first_line_the_table_does_not_know_stops_the_build(self):
        """Not a fallback to the LicenseRef: a package that ships a licence
        file under the wrong SPDX expression is worse than one that will not
        build, and the message names the line and where to add a row."""
        got = self.run_spdx("Copyright (c) 2026 somebody, all rights reserved")
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("names no SPDX id this table knows", got.stderr)
        self.assertIn("all rights reserved", got.stderr)
        self.assertIn("add a row to spdx_id()", got.stderr)

    def test_the_build_script_refuses_a_spec_that_disagrees_with_the_license(self):
        """The gate, not a rewrite.  While there was no LICENSE the script sed
        the id into the scratch copy of the spec, which meant the shipped file
        could say anything; now the tag is right in the spec and the script's
        job is to refuse a build where the two have drifted apart.  The spec is
        copied unchanged, so `git status` after a build stays clean."""
        with open(BUILD_RPM, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("stag=$(sed -n 's/^License: *\\([^ ].*\\)$/\\1/p' \"$SPEC\"", src)
        self.assertIn("build-rpm.sh: licence mismatch", src)
        self.assertIn('cp "$SPEC" "$top/SPECS/w11.spec"', src)
        self.assertNotIn('sed "s|^License: .*', src)


class TheRpmlintrc(unittest.TestCase):
    """The analogue of debian/w11.lintian-overrides: exactly the
    accepted findings, each with the sentence saying why."""

    def setUp(self):
        with open(RPMLINTRC, encoding="utf-8") as fh:
            self.text = fh.read()
        self.filters = re.findall(r"^addFilter\(r?\"(.*)\"\)$", self.text, re.M)

    def test_it_names_exactly_the_accepted_findings_and_nothing_else(self):
        """Both directions.  A filter nobody needs any more is a finding of its
        own -- the rule debian/w11.lintian-overrides is held to -- and
        an accepted finding with no filter is a red CI job."""
        self.maxDiff = None
        named = []
        for f in self.filters:
            hit = [name for name in ACCEPTED_FINDINGS if name in f]
            self.assertEqual(len(hit), 1, "%r names %r" % (f, hit))
            named.append(hit[0])
        self.assertEqual(sorted(named), sorted(ACCEPTED_FINDINGS))

    def test_every_filter_compiles_as_a_regular_expression(self):
        """rpmlint matches these against the whole finding line; one that does
        not compile is silently no filter at all."""
        for f in self.filters:
            with self.subTest(f):
                re.compile(f)

    def test_every_filter_carries_a_reason_above_it(self):
        """A filter with no sentence is an excuse.  The comment block directly
        above each addFilter is what makes the file readable as a list of
        decisions rather than a list of shut-ups."""
        lines = self.text.splitlines()
        for i, line in enumerate(lines):
            if not line.startswith("addFilter("):
                continue
            above = [ln for ln in lines[:i][::-1]]
            reason = []
            for ln in above:
                if ln.startswith("#"):
                    reason.append(ln)
                else:
                    break
            with self.subTest(line):
                self.assertGreaterEqual(len(reason), 2, line)

    def test_zero_perms_ghost_is_fixed_rather_than_filtered(self):
        """rpmlint suggested `%ghost %attr(0644,root,root)` and the spec does
        exactly that, so the finding cannot occur and there is no filter for
        it.  This is the test that keeps somebody from filtering it back."""
        self.assertNotIn("zero-perms-ghost", self.text)
        self.assertIn("%attr(0644,root,root)", spec_text())

    def test_only_the_readme_of_packaging_rpm_is_in_the_payload(self):
        """An rpmlint configuration lives in the build, not on the user's
        machine -- unlike debian/w11.lintian-overrides, which dpkg
        ships under /usr/share/lintian/overrides.  The way it would get in is
        a %doc naming the directory rather than the one file in it, which is
        what this reads: the %doc line's packaging/ entries, not the absence
        of a word nobody typed."""
        doc = [ln for ln in main_files().splitlines() if ln.startswith("%doc ")]
        self.assertEqual(len(doc), 1, doc)
        named = [w for w in doc[0].split()[1:] if w.startswith("packaging")]
        self.assertEqual(named, ["packaging/rpm/README.Fedora"])
        self.assertNotIn("rpmlintrc", section("install"))


if __name__ == "__main__":
    unittest.main()
