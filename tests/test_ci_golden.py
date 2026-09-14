#!/usr/bin/env python3
"""scripts/ci-golden.sh: where a base image comes from, and what a cached
golden is keyed by.

R08 and R09.  Two claims, both of which were false in the tree the day this
file was written and both of which cost a whole CI job to discover:

* **The download rules.**  CI run 34340513060 was green on 25 rig jobs and red
  on five -- fedora44-gnome, fedora44-sway, arch-sway, arch-hypr, nixos-sway --
  and every one of them failed the same way: `ci-golden: no download rule for
  Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2`, forty seconds into a job that
  had already installed QEMU.  A rule is one `case` arm, and the thing that
  makes it worth a test is that it is unreachable from any unit run: the arm is
  chosen by a file name written in a flavor's yaml, and nothing but a rig job
  ever puts the two together.  `TheFlavorsAllHaveARule` is that join, done
  here in a tenth of a second.
* **The cache key.**  A golden is pulled by `<flavor>:<key>` and the key is a
  sha256 over the files that decide the image's contents.  Two failure modes,
  opposite and both silent: a file missing from the list means a recipe change
  that never rebuilds (CI keeps pulling last week's image and reports it green),
  and a file added to the list -- or the same files hashed in another order --
  invalidates every golden in GHCR at once and costs an hour of builds.  So the
  six-file list is asserted verbatim, in order, and the NixOS list beside it.

The measured URLs behind the two new rules: Fedora 44's cloud image answered at
dl.fedoraproject.org on 2026-09-08, 583 729 152 bytes, sha256 28680fe5...b7f
matching the published CHECKSUM [recon2/fedora.md 2], and the Arch cloud image
at geo.mirror.pkgbuild.com, 559 223 808 bytes, sha256 e3e688f9...930 matching
its published `.SHA256` [recon2/arch.md 1].
"""

import hashlib
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
# `import support` is a bare import: it resolves only with the tests directory
# on sys.path, which `python3 tests/<file>.py` gives for free and
# `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                    # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "ci-golden.sh")
FLAVORS = os.path.join(ROOT, "vm", "flavors")

#: The stub `curl`.  Two shapes are used by fetch(): `-o <file> <url>`, which
#: downloads, and `-fsSL <url>`, which prints the body -- here the `.SHA256`
#: sibling of an Arch image.  Every URL asked for is appended to $CURL_LOG,
#: which is the whole point of the stub.
CURL = r"""#!/bin/sh
out=; url=
while [ $# -gt 0 ]; do
    case $1 in
        -o) out=$2; shift 2 ;;
        --retry) shift 2 ;;
        -*) shift ;;
        *) url=$1; shift ;;
    esac
done
echo "$url" >> "$CURL_LOG"
if [ -n "$CURL_FAIL" ]; then
    # what a transfer cut off part way leaves behind, which is the state the
    # `rm -f` in fetch() is written for: real curl has already written what it
    # got to -o when the connection dies.  A stub that wrote nothing would make
    # `no half file behind` a property of the stub.
    [ -z "$out" ] || printf 'half a cloud image' > "$out"
    exit 22
fi
if [ -n "$out" ]; then printf '%s' "$CURL_BODY" > "$out"
else printf '%s  the-file\n' "$CURL_SHA"
fi
"""


def harness(body, extra=""):
    """A bash script that is ci-golden.sh's helpers and nothing else.

    `say` is redefined to print rather than to the pretty two-line form, so a
    test reads one line per event."""
    return "\n".join([
        "set -eu",
        'say() { echo "say: $*"; }',
        support.sh_function(SCRIPT, "check_sha256"),
        support.sh_function(SCRIPT, "fetch"),
        extra,
        body,
    ])


class FetchHarness:
    """`fetch()` sliced out of the script and run with a stub curl on PATH.

    Shared by the two classes below: one asks what URL each name resolves to,
    the other asks only whether a name resolves at all."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="w11-cigolden-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.bin = os.path.join(self.dir, "bin")
        self.images = os.path.join(self.dir, "images")
        os.mkdir(self.bin)
        os.mkdir(self.images)
        curl = os.path.join(self.bin, "curl")
        with open(curl, "w", encoding="utf-8") as fh:
            fh.write(CURL)
        os.chmod(curl, 0o755)
        self.log = os.path.join(self.dir, "curl.log")

    def run_fetch(self, name, body="the base image", sha=None, fail=False):
        script = harness('fetch "$1"')
        env = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"],
                   VMIMAGES=self.images, CURL_LOG=self.log, CURL_BODY=body,
                   CURL_SHA=sha or hashlib.sha256(body.encode()).hexdigest(),
                   CURL_FAIL="1" if fail else "")
        got = subprocess.run(["bash", "-c", script, "harness", name],
                             capture_output=True, text=True, timeout=120, env=env)
        urls = []
        if os.path.exists(self.log):
            with open(self.log, encoding="utf-8") as fh:
                urls = fh.read().split()
        return got, urls


class FetchRules(FetchHarness, unittest.TestCase):
    """R08.  Every download rule, by the name a flavor's yaml writes."""

    def test_every_documented_name_resolves_to_its_url(self):
        """One row per download rule, including the two the five red jobs of
        run 34340513060 were missing."""
        cases = {
            "noble-server-cloudimg-amd64.img":
                "https://cloud-images.ubuntu.com/noble/current/",
            "ubuntu-26.04-server-cloudimg-amd64.img":
                "https://cloud-images.ubuntu.com/releases/26.04/release/",
            "stonking-server-cloudimg-amd64.img":
                "https://cloud-images.ubuntu.com/stonking/current/",
            "ubuntu-24.04.4-desktop-amd64.iso":
                "https://releases.ubuntu.com/24.04/",
            "ubuntu-26.04.1-desktop-amd64.iso":
                "https://releases.ubuntu.com/26.04/",
            "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2":
                "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/",
            "Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2":
                "https://dl.fedoraproject.org/pub/fedora/linux/releases/43/Cloud/x86_64/images/",
            "Arch-Linux-x86_64-cloudimg-20260901.583572.qcow2":
                "https://geo.mirror.pkgbuild.com/images/v20260901.583572/",
        }
        for name, prefix in sorted(cases.items()):
            with self.subTest(name):
                got, urls = self.run_fetch(name)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual(urls[0], prefix + name)
                self.assertTrue(os.path.exists(os.path.join(self.images, name)))
                os.unlink(os.path.join(self.images, name))
                os.unlink(self.log)

    def test_the_arch_image_also_fetches_its_published_sha256(self):
        """The dated arch-boxes build publishes `<file>.SHA256` beside itself;
        a rolling image that is not checked is a golden built from whatever the
        mirror had that minute."""
        got, urls = self.run_fetch("Arch-Linux-x86_64-cloudimg-20260901.583572.qcow2")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(len(urls), 2, urls)
        self.assertEqual(urls[1], urls[0] + ".SHA256")
        self.assertIn("say: sha256 ok", got.stdout)

    def test_a_wrong_sha256_on_the_arch_image_stops_the_build(self):
        """The premise of the check above: with the mirror's answer changed by
        one character the fetch fails and names both digests."""
        got, _ = self.run_fetch("Arch-Linux-x86_64-cloudimg-20260901.583572.qcow2",
                                sha="0" * 64)
        self.assertEqual(got.returncode, 1)
        self.assertIn("sha256 mismatch", got.stderr)
        self.assertIn("want " + "0" * 64, got.stderr)

    def test_a_name_with_no_rule_exits_2_and_says_so(self):
        """What the five red rig jobs printed, kept: an unknown image is a
        loud refusal naming the file, not a guessed URL."""
        got, urls = self.run_fetch("Debian-13-genericcloud-amd64.qcow2")
        self.assertEqual(got.returncode, 2)
        self.assertIn("no download rule for Debian-13-genericcloud-amd64.qcow2",
                      got.stderr)
        self.assertEqual(urls, [])

    def test_an_image_already_in_vmimages_is_not_downloaded_again(self):
        """A rig host with ~/images populated by hand -- and the second flavor
        of a run on the same base -- must not re-fetch 583 MB."""
        with open(os.path.join(self.images, "noble-server-cloudimg-amd64.img"),
                  "w", encoding="utf-8") as fh:
            fh.write("already here")
        got, urls = self.run_fetch("noble-server-cloudimg-amd64.img")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(urls, [])

    def test_a_failed_download_leaves_no_half_file_behind(self):
        """curl writes `<file>.part` and the move is what publishes it, so an
        interrupted job does not poison $VMIMAGES for the next one.

        The stub writes 18 bytes into the `.part` before exiting 22, which is
        what a cut-off transfer does: nothing in the tree ever removed that
        file (`[ -s "$VMIMAGES/$f" ]` guards the final name only), so it sat in
        the runner's image directory until the disk was full.  The old
        `curl ... && mv ...` shape did fail the run -- the && list was the
        function's last command and `set -e` fired at the call site -- but it
        failed it with curl's own message and left the part file."""
        got, urls = self.run_fetch("noble-server-cloudimg-amd64.img", fail=True)
        self.assertEqual(got.returncode, 1)
        self.assertIn("download failed: " + urls[0], got.stderr)
        self.assertEqual(os.listdir(self.images), [])


class TheFlavorsAllHaveARule(FetchHarness, unittest.TestCase):
    """The join nothing else makes: every image name any flavor names is a name
    `fetch()` knows.  This is the test the five red rig jobs of run
    34340513060 would have failed."""

    def bases(self):
        out = {}
        for name in sorted(os.listdir(FLAVORS)):
            if not name.endswith(".yaml"):
                continue
            with open(os.path.join(FLAVORS, name), encoding="utf-8") as fh:
                text = fh.read()
            for header in ("vmctl-base", "vmctl-iso"):
                m = re.search(r"^#\s*%s:\s*(\S+)$" % header, text, re.M)
                if m:
                    out.setdefault(m.group(1), []).append(name)
        return out

    def test_every_image_a_flavor_names_has_a_download_rule(self):
        found = self.bases()
        # the floor: 38 flavors today, of which only the two nixos ones name no
        # image, so a `bases()` that stopped parsing would be caught here
        self.assertGreaterEqual(len(found), 8, found)
        for image, flavors in sorted(found.items()):
            with self.subTest("%s (%s)" % (image, ", ".join(flavors))):
                got, urls = self.run_fetch(image)
                self.assertNotEqual(got.returncode, 2,
                                    "no download rule for %s, named by %s"
                                    % (image, ", ".join(flavors)))
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertTrue(urls[0].startswith("https://"), urls)
                os.unlink(os.path.join(self.images, image))
                os.unlink(self.log)

    def test_the_two_nixos_flavors_name_no_image_at_all(self):
        """The premise of the nix branch: a `# vmctl-nix:` flavor is built by
        nix out of vm/nixos/<flavor>.nix and downloads nothing."""
        nix = []
        for name in sorted(os.listdir(FLAVORS)):
            if not name.endswith(".yaml"):
                continue
            with open(os.path.join(FLAVORS, name), encoding="utf-8") as fh:
                if "# vmctl-nix:" in fh.read():
                    nix.append(name)
        self.assertEqual(nix, ["nixos-gnome.yaml", "nixos-sway.yaml"])
        for name in nix:
            with self.subTest(name):
                with open(os.path.join(FLAVORS, name), encoding="utf-8") as fh:
                    text = fh.read()
                self.assertNotRegex(text, r"(?m)^#\s*vmctl-base:")
                self.assertNotRegex(text, r"(?m)^#\s*vmctl-iso:")


class TheCacheKey(unittest.TestCase):
    """R09.  `key_inputs()` over a fake tree: what a golden's tag is a hash of.

    The nix list is a key that is never used as one, and the distinction
    matters if anybody turns that cache on: vm/nixos/flake.nix takes the
    repository as `inputs.w11.url = "path:../.."` and the module
    installs the package built from it, so a NixOS image's contents move with
    any file of the tree and not only with these eleven.  Nothing serves a
    stale one today because a `# vmctl-nix:` golden is neither pulled nor
    pushed; caching them would need the package source in the key as well."""

    def inputs(self, nix):
        # nix/checks is a DIRECTORY in the repository, and the fake tree has one
        # too: with `ls nix/*` in key_inputs() this test passed on a tree the
        # repository does not have, while the real run printed a `nix/checks:`
        # header, six `cat: ... No such file` lines, and a key that left every
        # file of nix/checks out.
        tree = tempfile.mkdtemp(prefix="w11-keytree-")
        self.addCleanup(shutil.rmtree, tree, ignore_errors=True)
        for path in ("vm/flavors", "vm/nixos", "nix/checks"):
            os.makedirs(os.path.join(tree, path))
        for path in ("vm/vmctl", "vm/build-image.sh", "vm/build-iso-golden.sh",
                     "vm/build-iso-image.sh", "vm/build-nixos-golden.sh",
                     "vm/golden-epoch", "flake.nix", "flake.lock",
                     "vm/nixos/common.nix", "vm/nixos/nixos-sway.nix",
                     "nix/package.nix", "nix/module.nix",
                     "nix/checks/tools.nix", "nix/checks/nixos-sway.nix",
                     "vm/flavors/a-flavor.yaml"):
            with open(os.path.join(tree, path), "w", encoding="utf-8") as fh:
                fh.write(path + "\n")
        script = "\n".join(["set -eu", "yaml=vm/flavors/a-flavor.yaml",
                            "nix=%s" % ("nixos-sway" if nix else ""),
                            support.sh_function(SCRIPT, "key_inputs"),
                            "key_inputs"])
        got = subprocess.run(["bash", "-c", script], cwd=tree, timeout=60,
                             capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stderr)
        return got.stdout.split()

    def test_a_cloud_image_flavor_hashes_exactly_todays_six_files(self):
        """Verbatim and in order.  Every golden in GHCR carries a tag computed
        over these six files concatenated in this sequence, so a reorder is not
        a refactor -- it is an hour of rebuilds."""
        self.assertEqual(self.inputs(nix=False),
                         ["vm/flavors/a-flavor.yaml", "vm/vmctl",
                          "vm/build-image.sh", "vm/build-iso-golden.sh",
                          "vm/build-iso-image.sh", "vm/golden-epoch"])

    def test_a_nixos_flavor_hashes_the_flake_and_the_modules_instead(self):
        """vm/build-image.sh is never run for one of these, and vm/nixos/*,
        nix/** and the lock file are what its contents come from -- `**`, since
        nix/checks/*.nix is where the three NixOS VM checks live and a change
        to one of those is a change to the image it boots."""
        got = self.inputs(nix=True)
        self.assertEqual(sorted(got),
                         sorted(["vm/flavors/a-flavor.yaml",
                                 "vm/build-nixos-golden.sh",
                                 "vm/nixos/common.nix", "vm/nixos/nixos-sway.nix",
                                 "nix/module.nix", "nix/package.nix",
                                 "nix/checks/tools.nix", "nix/checks/nixos-sway.nix",
                                 "flake.nix", "flake.lock", "vm/golden-epoch"]))

    def test_the_two_lists_differ_and_both_carry_the_epoch(self):
        """vm/golden-epoch is the hand-bumped file that says "the upstream
        moved under an unchanged recipe"; a list without it cannot be bumped at
        all."""
        cloud, nix = self.inputs(nix=False), self.inputs(nix=True)
        self.assertNotEqual(set(cloud), set(nix))
        self.assertIn("vm/golden-epoch", cloud)
        self.assertIn("vm/golden-epoch", nix)
        self.assertNotIn("vm/build-image.sh", nix)

    def test_a_real_flavors_key_is_the_sha256_of_those_six_files(self):
        """The tag itself, computed here the way the script computes it, so a
        change to either side is caught: `cat` of the six, sha256, first 16."""
        want = hashlib.sha256()
        for path in ("vm/flavors/resolute-sway.yaml", "vm/vmctl",
                     "vm/build-image.sh", "vm/build-iso-golden.sh",
                     "vm/build-iso-image.sh", "vm/golden-epoch"):
            with open(os.path.join(ROOT, path), "rb") as fh:
                want.update(fh.read())
        script = "\n".join(["set -eu", "yaml=vm/flavors/resolute-sway.yaml",
                            "nix=", support.sh_function(SCRIPT, "key_inputs"),
                            "key_inputs | xargs cat | sha256sum | cut -c1-16"])
        got = subprocess.run(["bash", "-c", script], cwd=ROOT, timeout=120,
                             capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip(), want.hexdigest()[:16])


class TheKeyOnTheRealTree(unittest.TestCase):
    """`key_inputs()` run in the repository itself, for the one failure the
    fake tree above cannot have: a path the function prints and `xargs cat`
    cannot open."""

    def key_inputs(self, yaml, nix):
        script = "\n".join(["set -eu", "yaml=%s" % yaml, "nix=%s" % nix,
                            support.sh_function(SCRIPT, "key_inputs"),
                            "key_inputs"])
        got = subprocess.run(["bash", "-c", script], cwd=ROOT, timeout=120,
                             capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stderr, "", got.stderr)
        return got.stdout.split()

    def test_every_path_a_nixos_flavor_hashes_is_a_file_that_exists(self):
        """The `ls vm/nixos/* nix/*` this replaced printed a `nix/checks:`
        header and five bare names on this tree, so six of the paths did not
        exist: `cat` said so six times per nixos rig job and the key was
        computed without nix/checks/*.  Every line has to be openable."""
        got = self.key_inputs("vm/flavors/nixos-sway.yaml", "nixos-sway")
        self.assertIn("nix/checks/nixos-sway.nix", got)
        self.assertEqual(len(got), len(set(got)), got)
        for path in got:
            with self.subTest(path):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, path)), path)

    def test_the_cloud_image_list_still_names_six_existing_files(self):
        got = self.key_inputs("vm/flavors/resolute-sway.yaml", "")
        self.assertEqual(len(got), 6, got)
        for path in got:
            with self.subTest(path):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, path)), path)


class TheThreeBuilders(unittest.TestCase):
    """Which builder each flavor kind reaches, read off the dispatch."""

    @classmethod
    def setUpClass(cls):
        with open(SCRIPT, encoding="utf-8") as fh:
            cls.text = fh.read()

    def dispatch(self):
        return support.sh_block(SCRIPT, "t0=$(date +%s)", 'say "built in')

    def test_nix_is_tried_first_then_iso_then_the_cloud_image(self):
        """Order matters: a NixOS flavor has neither `# vmctl-base:` nor
        `# vmctl-iso:`, so the `else` arm would run `vmctl build` on it and
        vmctl would refuse with the message it was given for exactly that."""
        block = self.dispatch()
        self.assertLess(block.index('if [ -n "$nix" ]'),
                        block.index('elif [ -n "$iso" ]'))
        self.assertIn("bash vm/build-nixos-golden.sh", block)
        self.assertIn("bash vm/build-iso-golden.sh", block)
        self.assertIn("vm/vmctl build", block)

    def test_only_the_cloud_image_arm_downloads_and_checks_a_base(self):
        """`# vmctl-base-sha256:` is the flavor's own pin, checked after the
        download; an ISO has `# vmctl-iso-sha256:`, checked by
        vm/build-iso-golden.sh, and a NixOS golden has no file to check."""
        block = self.dispatch()
        nix_arm, iso_arm, base_arm = (block.split('elif [ -n "$iso" ]')[0],
                                      block.split('elif [ -n "$iso" ]')[1].split("else")[0],
                                      block.split("\nelse\n")[1])
        self.assertNotIn("fetch", nix_arm)
        self.assertIn('fetch "$iso"', iso_arm)
        self.assertNotIn("base_sha", iso_arm)
        self.assertIn('fetch "$base"', base_arm)
        self.assertIn('check_sha256 "$base" "$base_sha"', base_arm)

    def test_a_nixos_golden_is_never_pulled_or_pushed(self):
        """The image carries the rig's root public key in its own /etc --
        vm/nixos/common.nix reads it with `builtins.getEnv
        "VMCTL_ROOT_PUBKEY"` and the build is `--impure` -- and NixOS runs no
        cloud-init, so the seed `vmctl start` attaches is read by nobody
        [recon2/nixos 7.1].  An image built on another machine therefore has no
        way in, which is what makes a shared cache of these wrong rather than
        merely stale: both the pull and the push are gated on `$cacheable`, and
        `$cacheable` is empty for exactly the `# vmctl-nix:` flavors."""
        self.assertIn('cacheable=1\nif [ -n "$nix" ]; then\n    cacheable=', self.text)
        for line in ("oras manifest fetch", "oras push"):
            with self.subTest(line):
                stanza = [ln for ln in self.text.splitlines() if line in ln]
                self.assertTrue(stanza, line)
        self.assertIn('if [ -n "$cacheable" ] && command -v oras', self.text)
        self.assertIn('if [ -n "$cacheable" ] && [ "${GOLDEN_PUSH:-1}" = 1 ]', self.text)

    def test_the_nix_branch_makes_the_root_key_the_build_needs(self):
        """build-nixos-golden.sh refuses with one line when
        $VMDATA/keys/id_ed25519.pub is not there, and a GitHub runner's
        $VMDATA is an empty directory this job just made.  `ssh-keygen` is what
        vmctl and build-iso-golden.sh do on first use; this does the same, so
        the nixos-sway rig job is not one refusal every push."""
        self.assertIn("ssh-keygen -t ed25519", self.text)
        self.assertIn("$VMDATA/keys/id_ed25519", self.text)
        with open(os.path.join(ROOT, "vm", "build-nixos-golden.sh"),
                  encoding="utf-8") as fh:
            self.assertIn("keys/id_ed25519.pub", fh.read())

    def test_every_fedora_flavor_pins_the_digest_of_the_image_it_names(self):
        """Every Fedora flavor carries the digest of ITS OWN base image, read
        off that release's published CHECKSUM.  The four fedora44-* ones share
        one image and one digest [recon2/fedora.md 2]; fedora43-gnome names a
        different image and had no digest at all until 2026-09-11, which made
        its 556 MB download the one base in the rig nothing verified -- the
        value below is Fedora's own, from
        `.../Fedora-Cloud-43-1.6-x86_64-CHECKSUM` [goal2/recon/flavors.md 1],
        re-read off that file on 2026-09-12.  The Arch flavors deliberately
        carry none and say why in their own yaml -- their pin is the mirror's
        `.SHA256`, fetched and checked at download time -- and the Ubuntu
        cloud images are `current/` names that move under one URL.

        Keyed off the image name and not off a hand-written list, so a fifth
        Fedora flavor on a fifth image cannot slip in unpinned."""
        pinned, bases = {}, {}
        for name in sorted(os.listdir(FLAVORS)):
            if not name.endswith(".yaml"):
                continue
            with open(os.path.join(FLAVORS, name), encoding="utf-8") as fh:
                text = fh.read()
            m = re.search(r"^#\s*vmctl-base-sha256:\s*([0-9a-f]{64})$", text, re.M)
            b = re.search(r"^#\s*vmctl-base:\s*(\S+)$", text, re.M)
            if b:
                bases[name[:-5]] = b.group(1)
            if m:
                pinned[name[:-5]] = m.group(1)
        fedora = sorted(f for f, b in bases.items() if b.startswith("Fedora-"))
        self.assertEqual(sorted(pinned), fedora)
        self.assertEqual(sorted(pinned), ["fedora43-gnome", "fedora44-cosmic",
                                          "fedora44-gnome", "fedora44-kde", "fedora44-sway"])
        # one digest per image, and the two images are not the same image
        by_image = {}
        for flavor, digest in pinned.items():
            by_image.setdefault(bases[flavor], set()).add(digest)
        self.assertEqual(
            {k: sorted(v) for k, v in sorted(by_image.items())},
            {"Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2":
                ["846574c8a97cd2d8dc1f231062d73107cc85cbbbda56335e264a46e3a6c8ab2f"],
             "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2":
                ["28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f"]})


if __name__ == "__main__":
    unittest.main()
