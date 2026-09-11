#!/usr/bin/env python3
"""vm/build-nixos-golden.sh: the third golden builder, sliced and run.

`vmctl build` overlays a cloud image and lets cloud-init dress it up;
`build-iso-golden.sh` drives the real Ubuntu installer off a release ISO.
Neither can make a NixOS golden: NixOS publishes no cloud image (the 26.05
release directory holds two ISOs, nixexprs.tar.xz and nothing else), runs no
cloud-init, and has no unattended installer to answer -- what it has instead
is a configuration [recon2/nixos §7.1].  So the flavor is
vm/nixos/{flake.nix,common.nix,<flavor>.nix} and the builder is `nix build`
plus three pieces of bookkeeping, and it is those three that are tested here:

* the root ssh key.  Every other flavor gets it through the cloud-init seed;
  this image has to have it baked in, so the build reads it from
  $VMDATA/keys/id_ed25519.pub and refuses in one line when it is not there.
  A 4.9 GiB image nobody can ssh into is the failure this prevents.
* the image's file name, which is `image.filePath` and carries the nixpkgs
  label -- the recon's was ...-26.05.20260907.93108a5-x86_64-linux.qcow2
  [recon2/nixos §7.4].  Hard-coding it is a builder that breaks on the next
  channel bump, so the script must read the attribute.
* the packages file.  vm/reference/<flavor>-packages.txt is a dpkg list for
  every other flavor and a store closure for this one; the conversion from
  `nix path-info -r` output to names is one sed and it has to be exact.

Nothing here builds an image or starts a VM (the nix build starts one of its
own inside the sandbox, which is why the script refuses to run beside a rig
instance -- also tested).
"""

import os
import re
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

import support                                                    # noqa: E402

VM = os.path.join(ROOT, "vm")
BUILDER = os.path.join(VM, "build-nixos-golden.sh")
FLAVORS = os.path.join(VM, "flavors")
NIXOS = os.path.join(VM, "nixos")
VMCTL = os.path.join(VM, "vmctl")

#: The flavors this builder owns.  Both are headers and prose only: a NixOS
#: yaml is not cloud-init user-data, because nothing in the guest reads it.
NIX_FLAVORS = ("nixos-sway", "nixos-gnome")


def run_sh(script, *args, **kw):
    """Run a slice of the builder under bash, with no repository around it."""
    return subprocess.run(["bash", "-c", script, "bash", *args],
                          capture_output=True, text=True, timeout=60, **kw)


def builder_text():
    with open(BUILDER, encoding="utf-8") as fh:
        return fh.read()


class ItParses(unittest.TestCase):

    def test_bash_n(self):
        r = subprocess.run(["bash", "-n", BUILDER], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_it_is_executable_and_has_the_usage_header(self):
        self.assertTrue(os.access(BUILDER, os.X_OK))
        self.assertIn("usage: vm/build-nixos-golden.sh [flavor]", builder_text())


class TheImageName(unittest.TestCase):
    """R10: read `filePath`, never a literal."""

    def test_the_image_file_name_is_never_written_down(self):
        """`image.filePath` is
        nixos-image-<label>-<system>.<ext>, and the label carries the nixpkgs
        rev -- so the name changes every time vm/nixos/flake.lock moves.  A
        script with the name in it works exactly once
        (nixos/modules/image/file-options.nix; measured [recon2/nixos §7.4])."""
        self.assertNotIn("nixos-image-qcow2-", builder_text())
        self.assertNotIn("nixos.qcow2", builder_text())

    def test_it_reads_the_attribute_that_says_the_name(self):
        """`passthru.filePath`, not `filePath`: images.nix merges the
        attribute in with lib.recursiveUpdate and nothing hoists it onto the
        derivation, so `#images.<flavor>.filePath` answers "does not provide
        attribute"."""
        text = builder_text()
        self.assertRegex(text, r'nix eval --impure --raw "\$FLAKE#images\.\$ATTR\.passthru\.filePath"')

    def test_every_nix_command_is_impure(self):
        """vm/nixos/common.nix reads the root key with builtins.getEnv, which
        is only allowed under --impure; a pure command would fail with
        `access to absolute path ... is forbidden` or, worse, silently read
        an empty string.

        Every line that runs one, not just the three that assign to out/rel/
        top: the first draft matched `^\\s*(out|rel|top)=\\$\\(nix `, so a
        fourth variable, or a bare `nix build` in a branch, would have walked
        past it while the docstring said "every".  `nix path-info` is exempt
        and named as such -- it reads the store, evaluates nothing, and
        --impure would be noise; `command -v nix` and `nix config show` are
        the two probes that run before any evaluation."""
        seen = 0
        for line in builder_text().splitlines():
            bare = line.strip()
            if bare.startswith("#") or "command -v nix" in bare or "nix config show" in bare:
                continue
            m = re.search(r"\bnix (build|eval|path-info)\b", bare)
            if not m:
                continue
            seen += 1
            with self.subTest(bare):
                if m.group(1) == "path-info":
                    self.assertNotIn("--impure", bare)
                else:
                    self.assertIn("--impure", bare)
        self.assertGreaterEqual(seen, 4, "the builder no longer runs nix")


class RootPubkey(unittest.TestCase):
    """R10: the refusal when the key cannot be derived from $VMDATA/keys."""

    def setUp(self):
        self.fn = support.sh_function(BUILDER, "root_pubkey")

    def test_a_missing_key_is_one_line_naming_the_file_and_a_way_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_sh(self.fn + '\nroot_pubkey "$1"\n', tmp)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertEqual(r.stdout, "")
            lines = r.stderr.strip().splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertIn(os.path.join(tmp, "keys", "id_ed25519.pub"), lines[0])
            self.assertIn("ssh-keygen", lines[0])

    def test_a_key_that_is_there_comes_back_as_one_line(self):
        """The value goes into VMCTL_ROOT_PUBKEY and from there into
        users.users.root.openssh.authorizedKeys.keys, a list of strings: a
        trailing newline in it would be a key nobody can use."""
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "keys"))
            key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPUOmQDG0Hh53no vmctl\n"
            with open(os.path.join(tmp, "keys", "id_ed25519.pub"), "w", encoding="utf-8") as fh:
                fh.write(key)
            r = run_sh(self.fn + '\nroot_pubkey "$1"\n', tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, key.strip())


class StoreNames(unittest.TestCase):
    """R10: `nix path-info -r` output -> the packages file."""

    #: Real lines, from `nix path-info -r` on this box.  The last two are the
    #: point: `acl-2.3.2` starts with a letter run, and `sway-1.12` is the
    #: name the file is read for.
    SAMPLE = """\
/nix/store/1zpfzc9dhlnnpn9ba4zc4l6l3xxhvnnj-acl-2.3.2
/nix/store/3nsm1mngr20mnb200sg6qyjncvxcr6bd-source
/nix/store/81ij0n00byd8y1acw516bk70n0cmspix-w11-0.4.0
/nix/store/na148lk31f9sc7s35myp7kvhlr9p9gvp-w11-udev-rules-0.4.0
/nix/store/x5v1n1gyni695jy7pqfc0l2br4kfm2ch-sway-1.12
/nix/store/1zpfzc9dhlnnpn9ba4zc4l6l3xxhvnnj-acl-2.3.2
"""

    def setUp(self):
        self.fn = support.sh_function(BUILDER, "store_names")

    def names(self, text):
        r = run_sh(self.fn + "\nstore_names\n", input=text)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_the_hash_and_the_prefix_go_and_the_rest_stays(self):
        self.assertEqual(self.names(self.SAMPLE),
                         ["acl-2.3.2", "source", "sway-1.12",
                          "w11-0.4.0", "w11-udev-rules-0.4.0"])

    def test_a_duplicate_path_is_listed_once(self):
        """A closure lists the same store path once per reference; the file
        is read by eye and by grep."""
        self.assertEqual(self.names(self.SAMPLE).count("acl-2.3.2"), 1)

    def test_the_names_come_back_sorted(self):
        """`nix path-info -r` walks the closure in reference order, which is
        neither stable across builds nor readable; the file is read by eye
        and diffed against the previous golden's."""
        self.assertEqual(self.names(self.SAMPLE), sorted(self.names(self.SAMPLE)))

    def test_a_store_path_with_no_version_keeps_its_whole_name(self):
        """`source` is the flake's own source derivation and it has no
        version at all -- a rule that assumed `<name>-<version>` would drop
        it or truncate something else."""
        self.assertIn("source", self.names(self.SAMPLE))


class ThePackagesFile(unittest.TestCase):
    """R10: vm/reference/<flavor>-packages.txt is a different thing here.

    Every other flavor's file is `dpkg-query -W -f='${binary:Package}\n'` --
    package names, one per line, which vm/README.md tells the reader to grep
    with `^name(:amd64)?$`.  A NixOS image has no dpkg at all; the closest
    thing is the store closure of /run/current-system, whose entries are
    `<name>-<version>` store path names and can be several per Debian package
    or none [recon2/nixos §7.3].  Reading one as the other is the mistake the
    header exists to prevent."""

    def test_the_file_says_in_its_first_line_that_it_is_not_a_dpkg_list(self):
        text = builder_text()
        block = text.split("} > \"$VMDATA/golden/$FLAVOR-packages.txt\"")[0]
        block = block.split("    {\n")[-1]
        heads = [ln.strip() for ln in block.splitlines() if ln.strip().startswith('echo "#')]
        self.assertGreaterEqual(len(heads), 1, block)
        self.assertIn("nix path-info -r /run/current-system", heads[0])
        self.assertRegex(" ".join(heads), r"NOT a dpkg list")

    def test_the_count_it_reports_excludes_those_header_lines(self):
        """`wc -l` of a file with a four-line header is four more than the
        closure, and the number in the build log is the one a person
        compares against the next build's."""
        text = builder_text()
        heads = len([ln for ln in text.splitlines() if ln.strip().startswith('echo "#')])
        self.assertRegex(text, r"wc -l < \"\$VMDATA/golden/\$FLAVOR-packages\.txt\"\) - %d" % heads)


class TheOneVmRule(unittest.TestCase):
    """R10: nixpkgs' make-disk-image boots a QEMU inside the nix sandbox.

    Measured: `sh -e /nix/store/...-vm-run` was the live process while
    nixos.raw grew under /nix/var/nix/builds/ [recon2/nixos §7.4].  On a host
    with a one-VM-at-a-time rule, that makes this build a VM start."""

    def setUp(self):
        self.fn = support.sh_function(BUILDER, "running_instances")

    def test_a_live_instance_is_reported_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "instances", "gnome-1")
            os.makedirs(d)
            # $$ is the bash running the slice: a pid that is certainly alive
            # and that this test does not have to start or stop.
            r = run_sh(self.fn + '\necho $$ > "$1/instances/gnome-1/qemu.pid"\n'
                       'running_instances "$1"\n', tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.split(), ["gnome-1"])

    def test_a_stale_pidfile_is_not_an_instance(self):
        """A crashed QEMU leaves its pidfile behind, and refusing to build
        for a machine that is not running would be its own bug."""
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "instances", "dead-1")
            os.makedirs(d)
            with open(os.path.join(d, "qemu.pid"), "w", encoding="utf-8") as fh:
                # A pid no process can have: the kernel rejects it as out of
                # range, so kill -0 fails without ever naming a real process.
                fh.write("4294967295\n")
            r = run_sh(self.fn + '\nrunning_instances "$1"\n', tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "")

    def test_no_instances_directory_at_all_is_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = run_sh(self.fn + '\nrunning_instances "$1"\n', tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "")


class TheTwoFlavors(unittest.TestCase):
    """The yamls this builder owns, and what vmctl makes of them."""

    @classmethod
    def setUpClass(cls):
        # vm/vmctl has no extension and is on no path; it has no import-time
        # side effects beyond building Paths out of $VMDATA, which is what
        # makes this safe.  tests/test_vm_scripts.py imports it the same way.
        import importlib.machinery
        import importlib.util
        spec = importlib.util.spec_from_loader(
            "vmctl_under_test",
            importlib.machinery.SourceFileLoader("vmctl_under_test", VMCTL))
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.text = {}
        for name in NIX_FLAVORS:
            with open(os.path.join(FLAVORS, name + ".yaml"), encoding="utf-8") as fh:
                cls.text[name] = fh.read()

    def test_each_one_names_the_nix_attribute_and_no_other_build_kind(self):
        for name in NIX_FLAVORS:
            with self.subTest(name):
                self.assertEqual(self.mod.flavor_nix(name), name)
                self.assertIsNone(self.mod.flavor_iso(name))
                self.assertNotRegex(self.text[name], r"(?m)^#\s*vmctl-base:")

    def test_vmctl_build_refuses_them_and_points_at_this_builder(self):
        """The same refusal an ISO flavor gets, for a different reason: there
        is no cloud image to overlay because NixOS publishes none."""
        for name in NIX_FLAVORS:
            with self.subTest(name):
                with self.assertRaises(self.mod.Fail) as e:
                    self.mod.flavor_base(name)
                self.assertIn("vm/build-nixos-golden.sh %s" % name, str(e.exception))

    def test_the_distro_desktop_and_ci_headers_are_what_the_rig_reads(self):
        """nixos-gnome is on demand and nixos-sway per push: a GNOME image is
        ~15 min of build against sway's ~10, and the module's GNOME half
        already has a per-push proof in `nix flake check`'s nixos-gnome
        [recon2/pkg-nix §7]."""
        self.assertEqual(self.mod.flavor_distro("nixos-sway"), "nixos")
        self.assertEqual(self.mod.flavor_distro("nixos-gnome"), "nixos")
        self.assertEqual(self.mod.flavor_desktop("nixos-sway"), "sway")
        self.assertEqual(self.mod.flavor_desktop("nixos-gnome"), "gnome")
        self.assertEqual(self.mod.flavor_ci("nixos-sway"), "push")
        self.assertEqual(self.mod.flavor_ci("nixos-gnome"), "push")

    def test_each_flavor_has_a_module_beside_the_flake(self):
        for name in NIX_FLAVORS:
            with self.subTest(name):
                self.assertTrue(os.path.exists(os.path.join(NIXOS, name + ".nix")))

    def test_the_rig_flake_names_both_flavors_and_pins_the_release(self):
        """nixos-26.05 and not the repo flake's nixos-unstable: a rig image
        is a picture of what a NixOS user runs.  The two differ where it
        shows -- xdotool is 3.20211022.1 on 26.05 and 4.20260303.1 on
        unstable [recon2/pkg-nix, recon2/nixos]."""
        with open(os.path.join(NIXOS, "flake.nix"), encoding="utf-8") as fh:
            flake = fh.read()
        self.assertIn("nixos-26.05", flake)
        self.assertIn('inputs.w11.inputs.nixpkgs.follows = "nixpkgs"', flake)
        for name in NIX_FLAVORS:
            with self.subTest(name):
                self.assertIn('"%s"' % name, flake)

    def test_the_lock_pins_the_release_the_flake_asks_for(self):
        """A rig golden that is rebuilt from a moving channel is not a
        measurement of anything."""
        with open(os.path.join(NIXOS, "flake.lock"), encoding="utf-8") as fh:
            lock = fh.read()
        self.assertIn('"ref": "nixos-26.05"', lock)
        self.assertRegex(lock, r'"rev": "[0-9a-f]{40}"')
        self.assertIn('"path": "../.."', lock)

    def test_both_specialisations_are_in_the_image(self):
        """`switch-to-configuration test` into without-w11 is the
        offline analogue of `apt-get remove`: it reloads the udev rules and
        swaps the system path in one step, with no network and no rebuild.
        There is no other way to run the smoke's package axis on NixOS --
        the package is IN the image."""
        with open(os.path.join(NIXOS, "common.nix"), encoding="utf-8") as fh:
            common = fh.read()
        self.assertIn("specialisation.without-w11.configuration", common)
        self.assertIn("programs.w11.enable = lib.mkForce false", common)
        self.assertIn("programs.w11 = {", common)

    def test_the_root_key_comes_from_the_environment_and_is_asserted(self):
        with open(os.path.join(NIXOS, "common.nix"), encoding="utf-8") as fh:
            common = fh.read()
        self.assertIn('builtins.getEnv "VMCTL_ROOT_PUBKEY"', common)
        self.assertIn('rootKey != ""', common)


if __name__ == "__main__":
    unittest.main()
