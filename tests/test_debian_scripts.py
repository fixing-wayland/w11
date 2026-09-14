#!/usr/bin/env python3
"""dpkg's two maintainer scripts, run as processes, and the .deb into a scratch root.

`debian/w11.postinst` and `debian/w11.postrm` are the only code
in this project that runs as root on a stranger's machine, and they had no test
of any kind: what they do was read out of them, never executed.  Both are POSIX
sh under `set -e` and both take `DPKG_ROOT`, which is what makes them runnable
here -- every path they write is under it, and the udev half is skipped by
their own first condition when it is set (a chroot has no /run/udev).

One thing this found, and one half of the finding it leaves standing.  The
banner is printed by `cat <<'EOM'` with the stamp already written, so
`apt install ./w11.deb | tail -1` -- a pipe whose
reader leaves -- killed `cat` with SIGPIPE, `set -e` propagated 141, and dpkg
reported the package's configuration as failed over a banner nobody was
reading.  A retry then printed nothing, because the stamp was there, and the
failure looked arbitrary.  That is fix 55 (finding F6.4), applied here as
`cat <<'EOM' || true`.  The other half of F6.4 -- that the stamp is written
before the banner rather than after -- stands as it is: the stamp is the record
that this installation happened, and a banner that fails is not a reason to
say it did not.

The scratch-root install is the whole package through real dpkg: six scripts in
usr/bin, the autostart symlink, the stamp, the banner exactly once, and nothing
left under usr/ after `-r` but the empty directories dpkg does not remove.
"""

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

from w11common import VERSION                                      # noqa: E402

POSTINST = os.path.join(ROOT, "debian", "w11.postinst")
POSTRM = os.path.join(ROOT, "debian", "w11.postrm")
RELEASE = os.path.join(ROOT, "release")
STAMP = "var/lib/w11/installed"

#: Everything postinst and postrm may reach for on a real machine.  Each is a
#: stub that records and does nothing, so "it did not touch the node" is an
#: assertion about an empty log rather than about the absence of an error.
STUBBED = ("modprobe", "udevadm", "setfacl", "chown", "chmod")

STUB = "#!/bin/sh\nprintf '%s\\n' \"$(basename \"$0\") $*\" >> \"$FAKE_LOG\"\nexit 0\n"


class Lifecycle(unittest.TestCase):
    """postinst and postrm through one install / remove / install / purge, as
    dpkg would run them: same argument vectors, same DPKG_ROOT."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11-deb-scripts-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "root")
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "calls.log")
        os.makedirs(self.root)
        os.makedirs(self.bin)
        for name in STUBBED:
            path = os.path.join(self.bin, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(STUB)
            os.chmod(path, 0o755)

    def env(self):
        return {"PATH": self.bin + ":/usr/bin:/bin", "DPKG_ROOT": self.root,
                "FAKE_LOG": self.log, "LC_ALL": "C"}

    def run_script(self, script, *args, stdout=subprocess.PIPE):
        return subprocess.run(["sh", script] + list(args), env=self.env(),
                              cwd=self.tmp, stdout=stdout,
                              stderr=subprocess.PIPE, text=True, timeout=60)

    def stamp(self):
        return os.path.join(self.root, STAMP)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    def test_the_first_configure_stamps_and_says_its_piece(self):
        """A first install: the stamp is the record that this machine has been
        told, and the banner is the telling.  All three sentences of it are
        load-bearing -- the relogin is the whole manual procedure, /dev/uinput
        is the thing a user has just opened to themselves, and the X11 handover
        needs tools this package does not depend on."""
        got = self.run_script(POSTINST, "configure")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(os.path.exists(self.stamp()))
        self.assertIn("LOG OUT AND BACK IN ONCE", got.stdout)
        self.assertIn("/dev/uinput", got.stdout)
        self.assertIn("apt install xdotool wmctrl", got.stdout)

    def test_nothing_touches_the_node_under_dpkg_root(self):
        """An image build (`--root=`, a chroot, a container) has no /run/udev
        and no /dev/uinput of its own; the rule and the modules-load file are
        on disk and apply at that image's first boot.  So: no modprobe, no
        udevadm, no chmod of anybody's node."""
        self.run_script(POSTINST, "configure")
        self.assertEqual(self.calls(), [])

    def test_a_second_configure_is_silent(self):
        """An upgrade, where dpkg passes the old version: the user has already
        been told, and the extension and rule never went away."""
        self.run_script(POSTINST, "configure")
        got = self.run_script(POSTINST, "configure", VERSION)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout, "")

    def test_remove_drops_the_stamp_and_the_directory(self):
        self.run_script(POSTINST, "configure")
        got = self.run_script(POSTRM, "remove")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertFalse(os.path.exists(self.stamp()))
        self.assertFalse(os.path.isdir(os.path.dirname(self.stamp())))

    def test_a_reinstall_says_its_piece_again(self):
        """Remove-then-install is a first install: the extension and the udev
        rule went with the package, so the relogin is due again.  `[ -z "$2" ]`
        would not do this -- dpkg passes the old version for a package left in
        `rc` state -- which is why the state is a stamp of our own."""
        self.run_script(POSTINST, "configure")
        self.run_script(POSTRM, "remove")
        got = self.run_script(POSTINST, "configure", VERSION)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("LOG OUT AND BACK IN ONCE", got.stdout)

    def test_purge_on_a_state_that_is_already_gone_exits_zero(self):
        """`apt purge` after `apt remove` reaches a postrm whose files postrm
        already deleted.  Under `set -e` every rm and rmdir there has to be
        `|| true`, or purge fails on a package that is correctly removed."""
        got = self.run_script(POSTRM, "purge")
        self.assertEqual(got.returncode, 0, got.stderr)

    def test_the_banner_survives_a_reader_that_has_gone(self):
        """Fix 55, finding F6.4.  `apt install ./x.deb | head -1`, or any log
        filter that exits early: the pipe's read end is closed, `cat` dies of
        SIGPIPE, and without `|| true` `set -e` turns that into 141 -- dpkg
        then reports the configuration as failed.  The stamp is already
        written at that point, so the retry prints nothing and the failure has
        no visible cause at all.  Same broken-reader double as
        test_stdout_gone.BrokenPipe: a pipe whose reader is gone before the
        first write, rather than the timing luck of a real `| head -1`."""
        r, w = os.pipe()
        os.close(r)
        try:
            got = subprocess.run(["sh", POSTINST, "configure"], env=self.env(),
                                 cwd=self.tmp, stdout=w, stderr=subprocess.PIPE,
                                 text=True, timeout=60)
        finally:
            os.close(w)
        self.assertEqual(got.returncode, 0, (got.returncode, got.stderr))
        self.assertTrue(os.path.exists(self.stamp()))

    def test_both_scripts_parse(self):
        for path in (POSTINST, POSTRM):
            with self.subTest(os.path.basename(path)):
                got = subprocess.run(["sh", "-n", path], capture_output=True,
                                     text=True, timeout=60)
                self.assertEqual((got.returncode, got.stderr), (0, ""))


class TheUdevSequence(unittest.TestCase):
    """The four commands postinst runs on a real system, in the only order
    that works, each of them unable to fail the install.

    Read out of the source rather than run: `udevadm control --reload-rules`
    needs root and a real udev, and a test that had one would be reloading the
    rules of the machine it runs on."""

    def setUp(self):
        with open(POSTINST, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_the_order_is_modprobe_reload_trigger_settle(self):
        """modprobe first, because the rule cannot match a node that does not
        exist; --reload-rules before the trigger, because the trigger is what
        re-runs the freshly-read rule; `--name-match=uinput` rather than a bare
        trigger, which would re-run every rule on the machine; settle last, so
        that the ACL is in place before dpkg says the package is configured."""
        block = self.src.split("if [ -z \"${DPKG_ROOT:-}\" ]")[1].split("fi\n")[0]
        order = [m for m in re.findall(
            r"modprobe uinput|control --reload-rules|trigger --name-match=uinput|settle",
            block)]
        self.assertEqual(order, ["modprobe uinput", "control --reload-rules",
                                 "trigger --name-match=uinput", "settle"])

    def test_none_of_them_can_fail_the_install(self):
        """A machine with no uinput module, or udev in a state this package
        knows nothing about, still gets six working tools and a bridge: the
        input path is one feature of six and is not worth a failed install."""
        block = self.src.split("if [ -z \"${DPKG_ROOT:-}\" ]")[1].split("fi\n")[0]
        for line in block.splitlines():
            line = line.strip()
            if line.startswith(("modprobe", "udevadm")):
                self.assertTrue(line.endswith("|| true"), line)


@unittest.skipUnless(shutil.which("dpkg"), "no dpkg")
class TheDebIntoAScratchRoot(unittest.TestCase):
    """`dpkg -i` into a directory, then `-r`, then `-P`.

    Not a chroot and not root: `--force-script-chrootless` runs the maintainer
    scripts on this machine with DPKG_ROOT pointing into the scratch tree,
    which is exactly the environment the tests above construct by hand -- so
    this is the same lifecycle with dpkg driving it, over the real payload.
    `py3compile`/`py3clean` are stubbed out because dh_python3's snippets call
    them and they would byte-compile into the scratch root for a Python this
    package does not target."""

    @classmethod
    def setUpClass(cls):
        debs = sorted(n for n in os.listdir(RELEASE) if n.endswith(".deb"))
        if len(debs) != 1:
            raise unittest.SkipTest("release/ holds %d packages" % len(debs))
        cls.deb = os.path.join(RELEASE, debs[0])

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11-dpkg-root-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "root")
        self.admin = os.path.join(self.root, "var/lib/dpkg")
        for sub in ("info", "updates", "triggers", "alternatives", "parts"):
            os.makedirs(os.path.join(self.admin, sub))
        with open(os.path.join(self.admin, "status"), "w"):
            pass
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name in ("py3compile", "py3clean"):
            path = os.path.join(self.bin, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nexit 0\n")
            os.chmod(path, 0o755)

    def dpkg(self, *args):
        cmd = ["dpkg", "--force-not-root", "--force-script-chrootless",
               "--force-bad-path", "--force-depends", "--root=" + self.root,
               "--admindir=" + self.admin, "--no-triggers",
               "--log=" + os.path.join(self.tmp, "dpkg.log")] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                              env=dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"],
                                       LC_ALL="C"))

    def install(self):
        got = self.dpkg("-i", self.deb)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        return got

    def r(self, *parts):
        return os.path.join(self.root, *parts)

    def test_the_six_tools_the_symlink_and_the_stamp_land(self):
        got = self.install()
        self.assertEqual(sorted(os.listdir(self.r("usr/bin"))),
                         ["warandr", "wdotool", "wmirror", "wwmctl", "wxprop", "wxrandr", "xw11"])
        link = self.r("etc/xdg/autostart/w11-enable-bridge.desktop")
        self.assertEqual(os.readlink(link), "/usr/lib/w11/enable-bridge.desktop")
        self.assertTrue(os.path.exists(self.r(STAMP)))
        self.assertEqual(got.stdout.count("LOG OUT AND BACK IN ONCE"), 1)

    def test_the_scripts_in_usr_bin_import_the_packaged_modules(self):
        """The .deb route is not the zipapp route: dh_python3 installs the six
        packages into /usr/lib/python3/dist-packages and usr/bin holds
        setuptools console-script shims, one per `[project.scripts]` entry.
        (`scripts/build-pyz.sh` makes the self-contained files instead, which
        is the other route and has its own test.)  The interpreter line is
        /usr/bin/python3, so the tools follow the system Python across a
        release upgrade -- 3.12 on noble, 3.14 on resolute."""
        self.install()
        for name, module in (("wdotool", "wdotool.cli"), ("wxrandr", "wxrandr.cli"),
                             ("wmirror", "wmirror.cli")):
            with open(self.r("usr/bin", name), encoding="utf-8") as fh:
                text = fh.read()
            # dh-python writes `#!/usr/bin/python3` on 24.04 and `#! /usr/bin/python3` (a space, which the
            # kernel ignores) on 26.04; the interpreter is what matters.
            self.assertRegex(text, r"\A#! ?/usr/bin/python3\n", name)
            self.assertIn("from %s import main" % module, text)
            self.assertTrue(os.path.isdir(
                self.r("usr/lib/python3/dist-packages", name)), name)

    def test_remove_leaves_nothing_under_usr_but_empty_directories(self):
        self.install()
        got = self.dpkg("-r", "w11")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        left = []
        for base, _dirs, names in os.walk(self.r("usr")):
            left += [os.path.relpath(os.path.join(base, n), self.root) for n in names]
        self.assertEqual(left, [], left)
        self.assertFalse(os.path.exists(self.r(STAMP)))
        self.assertFalse(os.path.lexists(
            self.r("etc/xdg/autostart/w11-enable-bridge.desktop")))

    def test_purge_after_remove_exits_zero(self):
        self.install()
        self.dpkg("-r", "w11")
        got = self.dpkg("-P", "w11")
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)

    def test_removing_and_installing_again_prints_the_banner_again(self):
        """The claim in postinst's own comment, end to end through dpkg: a
        remove takes the extension and the rule away, so the next install owes
        the user the relogin sentence a second time."""
        self.install()
        self.dpkg("-r", "w11")
        got = self.install()
        self.assertEqual(got.stdout.count("LOG OUT AND BACK IN ONCE"), 1)


if __name__ == "__main__":
    unittest.main()
