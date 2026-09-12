#!/usr/bin/env python3
"""`.github/workflows/ci.yml` read as text, because the claims are about its
shape and the standard library has no YAML parser.

R25.  The workflow is the one file in the tree that nothing else can check: it
runs on GitHub and its mistakes are discovered by pushing.  Every failure this
file pins has happened to this project at least once --

* a test file added and no job running it (the matrix is a glob over
  `tests/test_*.py`, so the claim is that the glob is still the glob);
* a rig flavor added, `# vmctl-ci: push` in its yaml, and the plan step no
  longer reading that header, so the flavor is never built;
* the rig installing the COMMITTED release/*.deb instead of the one this push
  built -- every rig result before run 34340513060 measured the last release
  and nobody could tell from the log;
* `continue-on-error` spreading: one job whose distro really is rolling is a
  reason, five jobs that are merely flaky is a suite nobody reads.  On
  2026-09-12 the count went to ZERO and the table below is empty, which makes
  this the file that keeps it empty: every one of the nine was replaced by a
  pin (a digest-pinned base, a frozen pacman snapshot) or by a measurement
  somebody finally took (Arch's `wmctrl --help` is 6801 bytes, the same as
  nixpkgs' 1.07; the headless-sway probe passed in all four installer jobs on
  run 34628777544).

So the header comment at the top of the file is treated as the index it looks
like: every name it lists is a job, and every job is listed.  Somebody adding a
job and not the sentence is the common case, and the sentence is what a person
reads first.
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
# The tests directory too, so this file keeps working under all three of the
# documented invocation forms (docs/Technical.md section 9).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")
CI_GOLDEN = os.path.join(ROOT, "scripts", "ci-golden.sh")
FLAVORS = os.path.join(ROOT, "vm", "flavors")

#: job -> (its `continue-on-error` expression, why that job is allowed to fail).
#: EMPTY since 2026-09-12, and kept as a table rather than replaced by "no job
#: may carry the key" so that a job joining it has to arrive with its reason
#: written down.  Keyed by JOB and not by expression, for the day it is not
#: empty: two of the nine that were here were the literal `true`, and a set of
#: expressions would have swallowed a third job carrying `continue-on-error:
#: true` without changing.
#:
#: What each of the nine became, all measured on run 34628777544
#: [recon/gaps.md 4]:
#:
#:   unit-2610, deb-install  the development Ubuntu, now a digest-pinned
#:                           ubuntu:26.10 -- pinned in ci.yml's `plan` step,
#:                           which hashes it into the image key, and used by
#:                           digest in deb-install's own matrix; every 26.10
#:                           file was green on that run bar one that was red on
#:                           24.04 and 26.04 too
#:   unit-distro, pkgbuild   rolling Arch, now a dated base tag plus an
#:                           archive.archlinux.org snapshot of the same date
#:   parity-arch             its `wmctrl --help` size was uncounted; it is 6801
#:                           bytes, byte for byte nixpkgs' plain 1.07 (measured
#:                           2026-09-12 on the 1.07-6 package out of that same
#:                           snapshot), so no second size was ever needed
#:   three probe steps       "the first run here is the measurement": it passed,
#:                           in deb-install on all three releases, in rpm and in
#:                           nix, and inline in pkgbuild (job 103360336842)
#:   vm                      `stonking-* || arch-*`; stonking-gnome and
#:                           stonking-kde were green on two consecutive runs
#:                           (34628777544 and 34662004383), and the four red
#:                           arch flavors were fixed in the tree in this wave --
#:                           the push after 2026-09-12 is the run that measures
#:                           them, no arch rig having run since the fixes
ROLLING = {}


def text():
    with open(WORKFLOW, encoding="utf-8") as fh:
        return fh.read()


def header():
    """The comment block at the top of the file, up to `name: ci`."""
    return text().split("\nname: ci")[0]


def jobs():
    """job name -> its block, by indentation.

    A two-space key under `jobs:` starts a job and the next one ends it; every
    line between belongs to it.  That is enough structure for every claim here
    and it needs no parser."""
    body = text().split("\njobs:\n", 1)[1]
    starts = [(m.start(), m.group(1)) for m in
              re.finditer(r"^  ([a-z][a-z0-9-]*):$", body, re.M)]
    out = {}
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(body)
        out[name] = body[pos:end]
    return out


class TheHeaderIsTheIndex(unittest.TestCase):
    """The comment block lists the jobs.  Both directions."""

    def test_every_name_the_header_lists_is_a_job(self):
        listed = set(re.findall(r"^#   ([a-z][a-z0-9-]*) {2,}\S", header(), re.M))
        self.assertTrue(listed, "the header lists no jobs any more")
        self.assertEqual(listed - set(jobs()), set())

    def test_every_job_is_listed_in_the_header(self):
        listed = set(re.findall(r"^#   ([a-z][a-z0-9-]*) {2,}\S", header(), re.M))
        self.assertEqual(set(jobs()) - listed, set())

    def test_the_eighteen_jobs_are_the_ones_the_plan_names(self):
        """Named rather than counted: a job that disappears is the failure, and
        `len(jobs) == 18` would go green on a rename.  The unit tests are one
        job per release because a matrix caps at 256 jobs and three releases
        times the files in tests/ passed that (run 34393015905).  `recordings`
        is the eighteenth, added 2026-09-12 with the rig's `--record`."""
        self.assertEqual(sorted(jobs()),
                         ["deb", "deb-install", "image", "lint", "nix",
                          "nix-full", "parity", "parity-arch", "pkgbuild",
                          "plan", "recordings", "rpm", "rpm-install",
                          "unit-2404", "unit-2604", "unit-2610", "unit-distro",
                          "vm"])

    def test_no_matrix_can_reach_githubs_cap_of_256_jobs(self):
        """Each unit-* job has one matrix axis, the files; a second axis of
        releases is what tripped the cap, and GitHub reports it as a failed run
        with no job to show for it."""
        files = len([f for f in os.listdir(os.path.join(ROOT, "tests")) if re.match(r"test_.*\.py$", f)])
        for name in ("unit-2404", "unit-2604", "unit-2610"):
            with self.subTest(name):
                block = jobs()[name]
                self.assertIn("file: ${{ fromJSON(needs.plan.outputs.files) }}", block)
                self.assertNotIn("distro: [", block)
                self.assertLess(files, 256)


class ThePlanJob(unittest.TestCase):
    """`plan` is where every matrix comes from, so every matrix is only as good
    as what it reads."""

    def setUp(self):
        self.plan = jobs()["plan"]

    def test_the_file_matrix_is_a_glob_over_every_test_file(self):
        """One job per test file per release: a file added to tests/ gets a job
        with no edit here, which is why the glob may not become a list."""
        self.assertIn('for f in tests/test_*.py; do basename "$f" .py; done',
                      self.plan)

    def test_the_flavor_split_reads_the_vmctl_ci_header(self):
        """The header is `# vmctl-ci: push|on-demand`, written by vm/vmctl's
        own `flavor_ci()`; the plan step greps for it and nothing else."""
        self.assertIn("vmctl-ci", self.plan)
        self.assertIn("grep -q '^# vmctl-ci: push'", self.plan)

    def test_the_scope_input_and_the_label_both_widen_the_matrix(self):
        """`workflow_dispatch` with `scope: all`, or the `ci:all-flavors` label
        on a pull request: the on-demand flavors have two ways in and neither
        is a file edit."""
        self.assertIn('if [ "$LABELLED" = true ]; then scope=all; fi', self.plan)
        self.assertIn('if [ "$scope" = all ]; then flavors=$all; else flavors=$push; fi',
                      self.plan)
        self.assertIn("ci:all-flavors", self.plan)
        self.assertIn("scope:", text().split("\njobs:")[0])

    def test_there_is_one_image_key_per_dockerfile(self):
        """The container tag is `<distro>-<12 hex>`, so a change to one image
        rebuilds one image.  The hash is over that Dockerfile AND the base
        references it is built from -- tests/test_ci_images.py::TheBasePins is
        the other half of that -- which is why the file name is matched inside
        a `sha256sum ...;` rather than at the end of the line."""
        keyed = set(re.findall(r"sha256sum (\.github/ci/[^;|\s]+)", self.plan))
        present = {"/".join([".github/ci", n])
                   for n in os.listdir(os.path.join(ROOT, ".github", "ci"))}
        self.assertEqual(keyed, present)

    def test_it_emits_nothing_no_other_job_reads(self):
        """The design sketched a `nix_flavors` output for the runner's nix
        install; the vm job reads `# vmctl-nix:` out of its own flavor's yaml
        instead, in the same step that reads `# vmctl-distro:`, so the output
        would have been computed on every run and read by nobody.  Every output
        declared here is consumed by name somewhere below."""
        declared = re.findall(r"^      ([a-z_]+): \$\{\{ steps\.list\.outputs\.",
                              self.plan, re.M)
        self.assertTrue(declared)
        body = text().split("\n  image:", 1)[1]
        for name in declared:
            with self.subTest(name):
                self.assertIn("needs.plan.outputs.%s" % name, body)


class TheImagesAndTheContainers(unittest.TestCase):
    """Five images, and every `container:` in the file is one of them."""

    def test_the_image_matrix_builds_every_dockerfile(self):
        image = jobs()["image"]
        named = set(re.findall(r"^            dockerfile: (\S+)$", image, re.M))
        self.assertEqual(named, set(os.listdir(os.path.join(ROOT, ".github", "ci"))))

    def test_the_image_matrix_is_the_five_tags(self):
        image = jobs()["image"]
        self.assertEqual(re.findall(r"^          - distro: \"?([^\"\n]+)\"?$",
                                    image, re.M),
                         ["24.04", "26.04", "26.10", "fedora44", "arch"])

    def test_every_container_names_a_distro_the_image_job_builds(self):
        """A container image is `w11-ci:<distro>-<key>`; the distro is
        either a literal or `${{ matrix.distro }}`/`${{ matrix.image }}`, and
        each of those matrices is checked here too."""
        built = set(re.findall(r"^          - distro: \"?([^\"\n]+)\"?$",
                               jobs()["image"], re.M))
        literals = re.findall(r"w11-ci:([a-z0-9.]+)-\$\{\{", text())
        self.assertTrue(literals)
        for distro in literals:
            with self.subTest(distro):
                self.assertIn(distro, built)
        for name in ("unit-distro",):
            with self.subTest(name):
                matrix = set(re.findall(r"^        distro: \[([^\]]+)\]$",
                                        jobs()[name], re.M))
                matrix |= set(re.findall(r"^          - distro: (\S+)$",
                                         jobs()[name], re.M))
                for row in matrix:
                    for got in row.replace('"', "").split(", "):
                        self.assertIn(got, built)

    def test_the_foreign_suite_job_runs_one_process_per_file(self):
        """Not `unittest discover`.  One interpreter across 105 files is a
        state channel between them, and this batch closed two leaks that only
        that channel could produce (an `os.environ` rebound to a plain dict in
        tests/test_live_smoke.py, and a `gi` cleanup registered in the wrong
        LIFO order in the same file); three more tests still flake that way on
        the author's box and pass run alone.  `unit` already runs one process
        per file on every Ubuntu release, and this job says the same thing on
        Fedora and Arch -- in one job, because 105 files times two distros is
        not worth the runner minutes.  The loop keeps going after a failure and
        prints the list, so one bad file does not hide the other 104."""
        block = jobs()["unit-distro"]
        self.assertNotIn("unittest discover", block)
        self.assertIn("for f in tests/test_*.py; do", block)
        self.assertIn('python3 "$f" || failed="$failed $f"', block)
        self.assertIn('[ -z "$failed" ] || { echo "FAILED:$failed"; exit 1; }',
                      block)

    def test_the_two_foreign_containers_run_the_jobs_that_need_them(self):
        """Fedora builds the rpm and Arch the pkg, and each also runs the whole
        suite once: the point of a distro image is that both happen in it."""
        for name, distro in (("rpm", "fedora44"), ("pkgbuild", "arch"),
                             ("parity-arch", "arch")):
            with self.subTest(name):
                self.assertIn("w11-ci:%s-" % distro, jobs()[name])


class ContinueOnError(unittest.TestCase):
    """The jobs allowed to fail, by name, and nothing outside them.  There are
    none."""

    def carriers(self):
        """job -> its `continue-on-error` expression, for the jobs that have
        one.  Job-level only: four spaces of indentation under a job's key."""
        out = {}
        for name, block in jobs().items():
            m = re.search(r"^    continue-on-error: (.*)$", block, re.M)
            if m:
                out[name] = m.group(1)
        return out

    def test_the_jobs_that_carry_it_are_exactly_the_rolling_ones(self):
        """ROLLING is empty, so this says no job carries the key at all.  By
        job and not by expression, so a job written `continue-on-error: true`
        is a new entry here and a failure; the reason column is the
        documentation the workflow's own comments repeat, and a job may not
        join the table without one being written."""
        want = {job: re.sub(r"\s+", " ", expr) for job, (expr, _) in ROLLING.items()}
        self.assertEqual(self.carriers(), want)

    def test_no_step_is_allowed_to_fail_either(self):
        """The three `xw11 --print-display` probes carried a STEP-level key
        (eight spaces), which the job-level regex above cannot see: a step that
        is allowed to fail is a check whose result nobody reads, exactly like a
        job that is.  Written as "the string is nowhere in the file" because
        with the table empty there is nothing left to enumerate."""
        self.assertEqual(re.findall(r"^\s*continue-on-error:.*$", text(), re.M), [])


class ThePackagingJobs(unittest.TestCase):
    """deb, rpm and pkgbuild each build from the tree and hand the artifact on."""

    def test_each_builder_uploads_the_artifact_its_installer_downloads(self):
        pairs = (("deb", "deb-install", "deb"), ("rpm", "rpm-install", "rpms"))
        for builder, installer, artifact in pairs:
            with self.subTest(artifact):
                self.assertIn("name: %s\n          path:" % artifact, jobs()[builder])
                self.assertIn("name: %s" % artifact, jobs()[installer])

    def test_each_builder_uploads_before_it_runs_its_release_test(self):
        """Run 34662004383: a stale test-count pin in test_release_rpm and
        test_release_pkgbuild failed those two jobs, both of which ran their
        release test BEFORE `upload-artifact` -- so nothing was uploaded, and
        all twelve fedora-*/arch-* rig jobs died at `download-artifact` with
        "Artifact not found for name: rpms|pkg".  Twelve desktops measured
        nothing because of a number in a unit test.  The package job still goes
        red on its own test; what this pins is the order, so the rigs get the
        package that was built either way."""
        for builder, release in (("deb", "tests/test_release_deb.py"),
                                 ("rpm", "tests/test_release_rpm.py"),
                                 ("pkgbuild", "tests/test_release_pkgbuild.py")):
            with self.subTest(builder):
                block = jobs()[builder]
                self.assertIn(release, block)
                self.assertLess(block.index("uses: actions/upload-artifact@v4"),
                                block.index(release), block)

    def test_rpm_install_covers_the_two_released_fedora_tags(self):
        """43 and 44 are released and 44 is what the spec was written on.
        rawhide is deliberately not here: a noarch rpm built on 44 requires
        python(abi) = 3.14 and rawhide has moved on, so installing there means
        building there (run 34390127394).

        By tag, because the tag is what the job's name and the rawhide
        exclusion are about; the digest each tag is pinned to is
        tests/test_ci_images.py's half."""
        block = jobs()["rpm-install"]
        self.assertEqual(re.findall(r"^          - tag: \"(\d+)\"$", block, re.M),
                         ["43", "44"])
        self.assertNotIn("rawhide", "\n".join(
            ln for ln in block.splitlines() if not ln.lstrip().startswith("#")))
        self.assertIn("image: ${{ matrix.image }}", block)

    def test_every_installer_makes_all_six_tools_answer_and_then_removes(self):
        """The install half of each packaging is only proved by a tool
        answering and by the removal leaving nothing on PATH."""
        for name in ("deb-install", "rpm-install", "pkgbuild"):
            with self.subTest(name):
                block = jobs()[name]
                self.assertIn("for t in wdotool wwmctl wxprop wxrandr warandr wmirror",
                              block)
                self.assertIn("! command -v wdotool", block)

    def test_each_installer_verifies_the_installed_files_against_the_package(self):
        """Plan B 3.4 asks for `rpm -V` and `pacman -Qkk` here and not only in
        the guest's `pkgverify` smoke phase: this is the cheap place for it,
        one container that has just installed the package.  Both compare mode,
        size and digest of every packaged file with what is on disk, so a
        scriptlet that edits a file it does not own shows up as a job."""
        rpm = jobs()["rpm-install"]
        self.assertIn('rpm -V "$p"', rpm)
        for package in ("w11", "gnome-shell-extension-w11-bridge",
                        "gnome-shell-extension-w11-overlap"):
            with self.subTest(package):
                self.assertIn(package, rpm)
        self.assertIn("pacman -Qkk w11", jobs()["pkgbuild"])

    def test_each_installer_lists_the_four_paths_the_package_owns(self):
        """The extension directory, the udev rule, the autostart entry and the
        enabler, in all three installers.  The enabler's directory is the one
        that differs: rpm puts it under %{_libexecdir}, and debian/rules and
        the PKGBUILD under /usr/lib."""
        common = ("/usr/share/gnome-shell/extensions/",
                  "/usr/lib/udev/rules.d/60-w11-uinput.rules",
                  "/etc/xdg/autostart/w11-enable-bridge.desktop")
        for name, enabler in (("rpm-install", "/usr/libexec/w11/enable-bridge"),
                              ("pkgbuild", "/usr/lib/w11/enable-bridge"),
                              ("deb-install", "/usr/lib/w11/enable-bridge")):
            block = jobs()[name]
            for path in common + (enabler,):
                with self.subTest("%s: %s" % (name, path)):
                    self.assertIn(path, block)

    def test_no_step_configures_git_for_a_user_it_does_not_run_as(self):
        """`runuser -u ci -- git config --global ...` keeps the CALLER's
        environment (no `-l`), so it writes root's ~/.gitconfig and the
        `env HOME=/home/ci` commands after it read a file that does not exist.
        It was there for git's dubious-ownership check, which does not fire
        anyway: the step chowns the checkout to `ci` first, and the check
        compares the owner with the user."""
        self.assertNotIn("runuser -u ci -- git config", text())
        for name in ("rpm", "pkgbuild"):
            with self.subTest(name):
                self.assertIn('chown -R ci "$PWD"', jobs()[name])

    def test_pkgbuild_builds_as_the_ci_user(self):
        """makepkg refuses to run as root and says so; the job runs it through
        `runuser -u ci`, which is why the Arch image has a NOPASSWD sudoers
        line for that user."""
        block = jobs()["pkgbuild"]
        self.assertIn("runuser -u ci -- env HOME=/home/ci", block)
        self.assertIn("sh scripts/build-pkgbuild.sh --no-deps --lint", block)

    def test_the_nix_job_runs_the_live_half_of_test_flake(self):
        """tests/test_flake.py skips its build cases unless W11_NIX_LIVE=1, so
        the job that has nix is the only place they ever run."""
        block = jobs()["nix"]
        self.assertIn("W11_NIX_LIVE=1", block)
        self.assertIn("python3 tests/test_flake.py", block)
        self.assertIn("checks.x86_64-linux.nixos-sway", block)

    def test_both_nix_jobs_ask_for_the_same_system_features(self):
        """`nixos-sway`, `nixos-gnome` and `nixos-kde` are NixOS VM tests: they
        want the `nixos-test` and `kvm` features.  nix adds kvm by itself when
        its daemon can open /dev/kvm, which an ubuntu-24.04 runner can today --
        the point of passing it is that the two installers agree and the
        dependence is written down [recon2/pkg-nix.md 4 measured the checks
        with these features set, and the vm job passes the same line]."""
        want = "extra-conf: system-features = nixos-test benchmark big-parallel kvm"
        for name in ("nix", "nix-full", "vm"):
            with self.subTest(name):
                self.assertIn(want, jobs()[name])

    def test_the_two_expensive_nixos_checks_are_scope_all_only(self):
        """The recon measured the GNOME VM check at about fifty minutes of wall
        clock, nearly all of it substitution [recon2/pkg-nix.md]."""
        block = jobs()["nix-full"]
        self.assertIn("needs.plan.outputs.scope == 'all'", block)
        self.assertIn("checks.x86_64-linux.nixos-gnome", block)
        self.assertIn("checks.x86_64-linux.nixos-kde", block)
        self.assertNotIn("nixos-gnome", jobs()["nix"])


class TheProxyInCI(unittest.TestCase):
    """What the X11 proxy added to this workflow, and what would quietly stop
    running if a line went.

    Two of these have already been the failure elsewhere in this tree: a second
    invocation added and the variable that makes it a second *thing* left off
    (the parity run through the proxy is byte-for-byte the direct run without
    `W11_PARITY_PROXY=1`, so a copy-paste that drops it is a job that passes
    twice as slowly and proves nothing), and a package installed in a container
    that has nothing to run it against."""

    #: The four jobs that run the proxy against a real compositor: three that
    #: have just installed or built a package, and the flake.
    PROBERS = ("deb-install", "rpm", "pkgbuild", "nix")

    def test_both_parity_jobs_run_the_oracle_direct_and_through_the_proxy(self):
        """Once direct and once through a pass-through xw11 on :98.  The
        second run is the framing's regression test: the two must differ only
        in their `Ran N tests in` lines."""
        for name in ("parity", "parity-arch"):
            with self.subTest(name):
                block = jobs()[name]
                # `sh scripts/...`, the invocations: the step's own name says
                # the script's path too and is not a run of it
                self.assertGreaterEqual(block.count("sh scripts/parity-oracle.sh"), 2, block)
                self.assertEqual(block.count("W11_PARITY_PROXY=1"), 1, block)

    def test_the_nix_parity_job_runs_a_third_pass_with_the_proxy_synthesizing(self):
        """The third mode is the one a real session is in: no `--passthrough`,
        so every request is answered out of the proxy's own shadow, and in
        front of a compositor's Xwayland rather than an Xvfb.  Nobody had run
        the byte-parity set through it before 2026-09-12; it is byte-identical
        and costs 5.3 s measured here, 6.4 s in the recon
        [recon/recordings.md 2.2].  `parity-arch` does NOT get it: it runs in a
        container whose sway is there for the live files, and one job proving
        the mode is what the mode costs.

        Both variables are asserted, because either alone is a third run of
        something already run: without `W11_PARITY_PROXY=native` it is the
        pass-through mode again, and without `W11_PARITY_SWAY=1` the upstream
        is the same Xvfb as the other two."""
        block = jobs()["parity"]
        self.assertEqual(block.count("sh scripts/parity-oracle.sh"), 3, block)
        self.assertIn("W11_PARITY_SWAY=1 W11_PARITY_PROXY=native "
                      "sh scripts/parity-oracle.sh", block)

    def test_the_bare_parity_runner_installs_what_native_parity_needs(self):
        """`parity` is the one job on a bare runner rather than in one of our
        images, so the packages are apt's and are listed by hand.
        tests/test_xw11_parity.py::NativeParity needs a compositor, an Xwayland
        and a native toplevel to compare against."""
        block = jobs()["parity"]
        for package in ("sway", "xwayland", "foot"):
            with self.subTest(package):
                self.assertRegex(block, r"install[^\n]*\b%s\b" % package)
        # and the four it always had, so this test cannot pass by replacing them
        for package in ("xvfb", "x11-utils", "xterm", "wmctrl"):
            with self.subTest(package):
                self.assertRegex(block, r"install[^\n]*\b%s\b" % re.escape(package))

    def test_every_installer_runs_xw11_print_display_against_a_real_compositor(self):
        """The one claim a container can make about the proxy: it starts, it
        prints a display, and the second call prints the SAME one because a
        session has one proxy.  A package that installs `xw11` and never runs
        it is a package whose seventh command nobody has executed."""
        for name in self.PROBERS:
            with self.subTest(name):
                block = jobs()[name]
                self.assertIn("xw11-probe.sh", block)
                self.assertIn("--print-display", block)
                self.assertIn("WLR_BACKENDS=headless", block)
                self.assertIn("xwayland enable", block)
                # the comparison itself, not merely two calls
                self.assertIn('[ "$a" = "$b" ]', block)

    def test_the_probe_is_the_same_script_in_all_four(self):
        """Four copies that have drifted apart are four different claims.  The
        script is written by a quoted heredoc, so the bytes between `<<'PROBE'`
        and the terminator are comparable directly."""
        bodies = []
        for name in self.PROBERS:
            body = re.split(r"\n\s*PROBE\b",
                            jobs()[name].split("<<'PROBE'", 1)[1], maxsplit=1)[0]
            bodies.append("\n".join(ln.strip() for ln in body.splitlines()))
        self.assertEqual(len(set(bodies)), 1, [b[:200] for b in bodies])
        self.assertIn("--print-display", bodies[0])

    def test_the_probe_step_gates_the_job_and_says_where_it_was_measured(self):
        """It carried `continue-on-error: true` until 2026-09-12 on the ground
        that the first run in a container WAS the measurement.  The run
        happened -- 34628777544, passing in deb-install on all three releases,
        in rpm and in nix [recon/gaps.md 4] -- so the marker is gone and a
        proxy that will not start in a container turns the push red.  The date
        of the measurement stays beside the step, because a step whose reason
        drifted off to an unrelated line of the job is a reason nobody reading
        the step will find."""
        step = "- name: xw11 --print-display under a headless sway"
        for name in ("deb-install", "rpm", "nix"):
            with self.subTest(name):
                head, probe = jobs()[name].split(step, 1)
                self.assertNotIn("continue-on-error", probe.split("run:", 1)[0])
                # the contiguous comment block immediately above the step plus
                # its own keys down to `run:`, and not the whole job
                comment, lines = [], head.splitlines()
                if lines and not lines[-1].strip():
                    lines.pop()                 # the step's own indentation
                for line in reversed(lines):
                    if not line.strip().startswith("#"):
                        break
                    comment.append(line)
                where = "\n".join(reversed(comment)) + probe.split("run:", 1)[0]
                self.assertIn("2026-09-11", where, where[-400:])
                self.assertIn("34628777544", where, where[-400:])
        # pkgbuild runs the same probe inline, because its `pacman -R` is a
        # second claim that has to be reached whatever sway does; the failure
        # is remembered in `probe` and asserted at the end of the step, so it
        # gates the job like the other three
        pkg = jobs()["pkgbuild"]
        # and it says where ITS measurement is, which is not in any recon: the
        # inline probe was behind `|| echo` on run 34628777544, so the only
        # record that it passed there is that job's own log
        self.assertIn("103360336842", pkg)
        self.assertIn(":20 then :20", pkg)
        self.assertIn("did not run in this container", pkg)
        self.assertIn("probe=0", pkg)
        self.assertIn('[ "$probe" = 0 ]', pkg)
        self.assertLess(pkg.index("pacman -R --noconfirm w11"),
                        pkg.index('[ "$probe" = 0 ]'), pkg)

    def test_lint_covers_the_whole_tree_and_xw11_is_not_excluded(self):
        """`uvx ruff check .` is every package; the only thing that could take
        xw11/ out of it is pyproject's own exclude list, so that is where this
        looks."""
        self.assertIn("uvx ruff check .", jobs()["lint"])
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
            self.assertNotIn("xw11", fh.read().split("[tool.ruff]", 1)[1]
                             .split("[tool.ruff.lint]", 1)[0])


class TheRigJob(unittest.TestCase):
    """The vm job: one per flavor, and the package it installs."""

    def setUp(self):
        self.vm = jobs()["vm"]

    def test_the_matrix_is_the_plans_flavor_list(self):
        self.assertIn("flavor: ${{ fromJSON(needs.plan.outputs.flavors) }}", self.vm)

    def test_it_waits_for_the_three_package_builds(self):
        """Finding of run 34319854037: the rig installed the committed
        release/*.deb, so every rig result until then measured the LAST RELEASE
        and not the push.  The deb job's artifact is downloaded into release/
        for exactly that, and the rpm and pkg arrive the same way."""
        self.assertIn("needs: [plan, deb, rpm, pkgbuild]", self.vm)
        self.assertIn("name: deb\n          path: release", self.vm)
        self.assertIn("name: rpms\n          path: dist", self.vm)
        self.assertIn("name: pkg\n          path: dist", self.vm)

    def test_one_distros_failed_package_build_does_not_skip_the_others_rigs(self):
        """`needs` alone skips a dependant when a needed job FAILS, and `rpm`
        is the one that could: it is not continue-on-error, and a Fedora build
        going wrong would take 25 Ubuntu rig jobs and 2 NixOS ones with it.
        `pkgbuild` could not -- a continue-on-error job reports `success` to
        its dependants and only leaves its artifact missing -- so this
        condition is written for `rpm`, and the fedora rigs then fail at their
        own download step, which is where a missing rpm belongs."""
        self.assertIn("if: ${{ !cancelled() && needs.plan.result == 'success' "
                      "&& needs.deb.result == 'success' }}", self.vm)
        # JOB level, four spaces: `rpm` carries a step-level `continue-on-error`
        # (eight spaces) on its headless-sway probe since the X11 proxy landed,
        # and a step that is allowed to fail says nothing about what the JOB
        # reports to its dependants, which is the whole of this claim.
        self.assertIsNone(re.search(r"^    continue-on-error:", jobs()["rpm"], re.M),
                          jobs()["rpm"])
        self.assertIn("needs.deb.result", self.vm)

    def test_each_download_is_guarded_by_the_flavors_own_distro(self):
        """A fedora flavor must not have a .deb dropped into release/: the
        smoke reads the distro out of the yaml and installs one package set."""
        for distro in ("ubuntu", "fedora", "arch"):
            with self.subTest(distro):
                self.assertIn("if: steps.flavor.outputs.distro == '%s'" % distro,
                              self.vm)
        self.assertIn("vmctl-distro", self.vm)

    def test_nix_is_installed_only_for_the_flavors_built_by_nix(self):
        """A NixOS golden is a nix build; every other flavor's is a cloud image
        and pays nothing for this step."""
        self.assertIn("if: steps.flavor.outputs.nix != ''", self.vm)
        self.assertIn("system-features = nixos-test benchmark big-parallel kvm", self.vm)

    def test_the_smoke_runs_in_package_mode_and_removes_afterwards(self):
        self.assertIn("vm/live-smoke.sh ${{ matrix.flavor }} --pkg --remove --record "
                      "--heads 3 --cpus 2 --mem 3G", self.vm)

    def test_the_smoke_records_and_the_upload_carries_the_capture(self):
        """`--record` is the whole of what CI had to add for the recordings:
        vm/live-smoke.sh pairs vm/live-smoke.d/capture-from-run with the log by
        stamp and writes <flavor>-<stamp>-capture.txt beside it, and the upload
        below already ships the whole of vm/live-smoke.out/.  Without the word,
        every push measures 38 desktops and deletes the evidence
        [recon/recordings.md 1.1: nothing in this file set CAPLOG].  24 KB per
        Wayland flavor, measured on resolute-sway 2026-09-12."""
        run = [ln for ln in self.vm.splitlines() if "vm/live-smoke.sh" in ln
               and ln.strip().startswith("run:")]
        self.assertEqual(len(run), 1, self.vm)
        self.assertIn("--record", run[0])
        self.assertIn("path: vm/live-smoke.out/", self.vm)
        self.assertIn("if: always()", self.vm)

    def test_the_dropped_marker_names_the_runs_that_measured_it(self):
        """`continue-on-error: ${{ startsWith(matrix.flavor, \'stonking-\') ||
        startsWith(matrix.flavor, \'arch-\') }}` came off on 2026-09-12, and the
        two halves came off on different evidence.  stonking had the two
        consecutive green runs that were asked for -- 34628777544 and
        34662004383 -- and both are named, because one run named is one run
        short of the condition.  The arch half came off on a fix in the TREE
        with no CI measurement behind it yet (all twelve fedora-*/arch-* rigs
        of 34662004383 died at download-artifact), and the comment has to say
        so rather than call those flavors measured."""
        # the stonking sentence itself, not the job: 34662004383 is named twice
        # more below it, for the twelve rigs that died at download-artifact, and
        # an assertion over the whole block would go green on those
        stonking = self.vm.split("stonking-gnome", 1)[1].split("asked for", 1)[0]
        for run in ("34628777544", "34662004383"):
            with self.subTest(run):
                self.assertIn(run, stonking)
        self.assertIn("IN THE TREE", self.vm)

    def test_the_golden_comes_from_ci_golden_sh(self):
        self.assertIn("scripts/ci-golden.sh ${{ matrix.flavor }}", self.vm)


class TheRecordingsJob(unittest.TestCase):
    """The other half of `--record`: one job per flavor that replays the
    capture that flavor's rig job just made.

    It proves the pipeline and lands nothing.  The 38 fixtures arrive in one
    commit of their own (scripts/rig-recordings.sh writes them), and until they
    do, `tests/fixtures/live/NOT-YET-RUN` is 59 lines long and every one of
    them is waiting on bytes CI throws away
    [recon/recordings.md 1.1, recon/gaps.md 2]."""

    def setUp(self):
        self.rec = jobs()["recordings"]

    def test_it_replays_every_flavor_the_rig_ran(self):
        """The same matrix as the rig job, from the same plan output: a flavor
        that is smoked and not replayed is a recording nobody has ever read
        back."""
        self.assertIn("flavor: ${{ fromJSON(needs.plan.outputs.flavors) }}", self.rec)
        self.assertIn("needs: [plan, vm]", self.rec)
        self.assertIn("scripts/rig-recordings.sh ${{ github.run_id }} "
                      "${{ matrix.flavor }}", self.rec)

    def test_it_runs_after_a_red_rig_too(self):
        """The rig uploads its artifact with `if: always()`, and a recording of
        a run with failures in it is exactly as replayable as one of a green
        run: rig-recordings.sh compares the replay against the tally that run's
        own log recorded, FAILs included.  Without the condition, one red
        flavor out of 38 would skip all 38 replays."""
        self.assertIn("if: ${{ !cancelled() && needs.plan.result == 'success' }}",
                      self.rec)

    def test_it_writes_no_fixture_into_the_tree(self):
        """The harvest is a commit somebody makes, not a job: RIG_REC_OUT and
        RIG_REC_NYR point at a scratch directory, and the step ends by asking
        git whether anything under tests/ moved."""
        self.assertIn("RIG_REC_OUT=/tmp/rec RIG_REC_NYR=/tmp/rec/NOT-YET-RUN", self.rec)
        self.assertIn("git diff --exit-code -- tests/", self.rec)

    def test_the_replay_is_cheap_enough_to_run_every_push(self):
        """`LIVE_SMOKE_SLEEP=0`: the phases sleep for a real compositor, and a
        transcript needs none of it -- nine recordings twice each went from
        2 m 50 s to 23 s with it (measured 2026-09-12).  Without it this job is
        38 jobs of sleeping.

        The variable is set by scripts/rig-recordings.sh, on the replay it
        runs, and that is the copy asserted here.  The job carried one of its
        own until the review of 2026-09-12 pointed out that the script's
        assignment overrides it, so the job env was a copy that could not
        act -- and a test on a line that cannot act is a test that goes green
        while the thing it names is broken."""
        with open(os.path.join(ROOT, "scripts", "rig-recordings.sh"),
                  encoding="utf-8") as fh:
            script = fh.read()
        self.assertRegex(script, r"(?m)^\s*LIVE_SMOKE_SLEEP=0")
        self.assertNotIn("LIVE_SMOKE_SLEEP", self.rec)

    def test_it_downloads_the_package_its_flavor_installed(self):
        """A `--pkg --remove` recording covers phase_install and phase_remove,
        and replaying those needs the package file on the runner:
        rig-recordings.sh drops both halves with a printed reason when it is
        not there, and the fedora and arch flavors -- 12 of the 38 -- would
        replay a third less than they recorded."""
        for distro in ("ubuntu", "fedora", "arch"):
            with self.subTest(distro):
                self.assertIn("if: steps.flavor.outputs.distro == '%s'" % distro,
                              self.rec)
        for artifact, path in (("deb", "release"), ("rpms", "dist"), ("pkg", "dist")):
            with self.subTest(artifact):
                self.assertIn("name: %s\n          path: %s" % (artifact, path),
                              self.rec)

    def test_the_script_it_calls_is_the_one_the_harvest_uses(self):
        """Same script, same acceptance rule, so the day the fixtures are cut
        the pipeline has already run 38 times on that very run."""
        script = os.path.join(ROOT, "scripts", "rig-recordings.sh")
        self.assertTrue(os.path.exists(script))
        with open(script, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("FAKE_VMCTL_STRICT=1", body)
        self.assertIn("RIG_REC_OUT", body)
        self.assertIn("RIG_REC_NYR", body)


class TheParityOracleSwayArm(unittest.TestCase):
    """`W11_PARITY_SWAY=1`, the mode the `parity` job's third line runs in.

    It lives beside the workflow tests because the workflow is what it
    protects: that third line is the only run of the byte-parity set against
    the proxy's own answers, and in front of a real compositor's Xwayland
    rather than an Xvfb.  A run of it that silently measured something else
    would be a green check standing for a comparison nobody made.

    That is exactly what it did until the review of 2026-09-12 found it: the
    arm exported the display sway named and handed it to the generic branch
    below, which starts an Xvfb on :99 when the display does not answer.  With
    a `sway` that wrote `:77` and exited, the script printed "upstream is
    sway's Xwayland on DISPLAY=:77", then "DISPLAY=:99 (1280 720)", then "all
    oracles ran", and exited 0.

    The doubles here are three shell shims in a temporary directory rather
    than anything out of tests/support.py: what is under test is a POSIX shell
    script and the things it looks for on PATH."""

    #: what the oracle gate at the top of the script demands before it reaches
    #: the display at all: the pinned xdotool generation, and a `wmctrl --help`
    #: of the 6801 bytes nixpkgs' 1.07 and Arch's 1.07-6 both print.
    SHIMS = {
        "xdotool": '#!/bin/sh\ncase "$1" in\nversion) echo "xdotool 4.20260303.1" ;;\n'
                   '*) exit 1 ;;\nesac\n',
        "wmctrl": '#!/bin/sh\n[ "$1" = --help ] || exit 1\nprintf \'%6800s\\n\' \'\'\n',
        # names a display and dies, which is what a lazily started Xwayland
        # that cannot start looks like from here
        "sway": '#!/bin/sh\nconf=""\nwhile [ $# -gt 0 ]; do [ "$1" = -c ] && conf=$2; shift; done\n'
                'echo ":77" > "$(dirname "$conf")/parity-display"\n',
    }

    def run_oracle(self, tmp, **extra):
        binv = os.path.join(tmp, "bin")
        os.makedirs(binv)
        for name, body in self.SHIMS.items():
            path = os.path.join(binv, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(path, 0o755)
        rt = os.path.join(tmp, "rt")
        os.makedirs(rt, mode=0o700)
        env = dict(os.environ, PATH=binv + os.pathsep + os.environ["PATH"],
                   W11_ORACLE_PATH=binv, XDG_RUNTIME_DIR=rt, W11_PARITY_SWAY="1")
        env.pop("DISPLAY", None)
        env.update(extra)
        proc = subprocess.run(["sh", os.path.join(ROOT, "scripts", "parity-oracle.sh")],
                              cwd=ROOT, env=env, capture_output=True, text=True,
                              timeout=120)
        return proc, rt

    def test_a_display_sway_names_but_never_answers_is_fatal(self):
        """Fatal, and fatal with the reason in it.  Not "the script exits
        non-zero": without the wait the script exits ZERO, having quietly put
        an Xvfb where the compositor was supposed to be."""
        with tempfile.TemporaryDirectory(prefix="parity-arm-") as tmp:
            proc, _ = self.run_oracle(tmp)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("upstream is sway's Xwayland on DISPLAY=:77", proc.stdout)
        self.assertIn("sway exited before its Xwayland answered", proc.stderr)
        # the two lines the silent fallback used to print, and the end of a run
        # that got all the way through on the wrong server
        self.assertNotIn("DISPLAY=:99", proc.stdout)
        self.assertNotIn("all oracles ran", proc.stdout)

    def test_it_leaves_no_file_in_the_runtime_directory_it_was_given(self):
        """The config and the display file are the script's, not the caller's:
        two `/tmp/xdg-parity-*` directories were still on the author's box
        after two runs on 2026-09-12.  A caller that HAD an XDG_RUNTIME_DIR
        keeps it; only the two files go."""
        with tempfile.TemporaryDirectory(prefix="parity-arm-") as tmp:
            proc, rt = self.run_oracle(tmp)
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(os.path.isdir(rt))
            self.assertEqual(sorted(os.listdir(rt)), [])

    def test_the_xvfb_branch_cannot_be_reached_under_it(self):
        """The second lock on the same door, and a source read because the
        first lock makes it unreachable from outside: the wait above dies
        before any display that does not answer can fall through.  If somebody
        ever moves or weakens that wait, this is what says the fallback is
        still fenced off."""
        with open(os.path.join(ROOT, "scripts", "parity-oracle.sh"),
                  encoding="utf-8") as fh:
            oracle = fh.read()
        branch = [ln for ln in oracle.splitlines() if "no usable DISPLAY" in ln]
        self.assertEqual(len(branch), 1, oracle)
        guard = oracle.split("command -v Xvfb", 1)[0].rstrip().splitlines()[-1]
        self.assertIn('[ -z "${W11_PARITY_SWAY:-}" ] &&', guard)


class TheGoldenRepository(unittest.TestCase):
    """The rig job logs into GHCR and ci-golden.sh pushes there: one name."""

    def test_the_workflow_and_the_script_agree_on_the_registry(self):
        with open(CI_GOLDEN, encoding="utf-8") as fh:
            script = fh.read()
        m = re.search(r"repo=\$\{GOLDEN_REPO:-(ghcr\.io)/\$owner/(\S+)\}", script)
        self.assertIsNotNone(m, "ci-golden.sh no longer defaults GOLDEN_REPO")
        self.assertEqual(m.group(2), "w11-golden")
        self.assertIn("oras login %s" % m.group(1), jobs()["vm"])

    def test_the_rig_job_installs_oras_because_the_script_needs_it(self):
        """`command -v oras` is what decides between a cache hit and a build,
        so a runner without it silently rebuilds every golden."""
        self.assertIn("oras_1.2.0_linux_amd64.tar.gz", jobs()["vm"])


class EveryPushFlavorGetsAJob(unittest.TestCase):
    """The directory and the workflow, joined the way the matrix joins them at
    run time."""

    def flavors_by_ci(self):
        """flavor -> its `# vmctl-ci:` header, defaulting to push the way
        vm/vmctl's own `flavor_ci()` defaults it."""
        out = {}
        for name in sorted(os.listdir(FLAVORS)):
            if not name.endswith(".yaml"):
                continue
            with open(os.path.join(FLAVORS, name), encoding="utf-8") as fh:
                m = re.search(r"^#\s*vmctl-ci:\s*(\S+)$", fh.read(), re.M)
            out[name[:-5]] = m.group(1) if m else "push"
        return out

    def push_flavors(self):
        return sorted(f for f, ci in self.flavors_by_ci().items() if ci == "push")

    def test_every_flavor_is_in_the_push_set(self):
        """Not a count: the numbers are derived and pinned once, in
        tests/test_docs_matrix.py.  What this asserts is that the set is
        total -- a yaml with a header the plan step's grep does not match is a
        flavor CI never builds and nobody is told about -- and that nothing is
        on demand."""
        by_ci = self.flavors_by_ci()
        self.assertEqual(set(by_ci.values()), {"push"}, "a flavor is on demand: the owner's rule "
                         "of 2026-09-11 is that every flavor runs on every push")
        self.assertEqual(len(self.push_flavors()), len(by_ci))

    def test_every_foreign_flavor_is_in_the_push_set(self):
        """Every flavor runs on every push (the owner's rule of 2026-09-11: nothing
        is on demand), so every foreign flavor is in the push set and builds
        on every run."""
        foreign = [f for f in self.push_flavors()
                   if f.startswith(("fedora", "arch-", "nixos-"))]
        self.assertEqual(sorted(foreign),
                         ["arch-cosmic", "arch-gnome", "arch-hypr", "arch-kde", "arch-river", "arch-sway",
                          "fedora43-gnome", "fedora44-cosmic", "fedora44-gnome", "fedora44-kde",
                          "fedora44-sway", "nixos-gnome", "nixos-sway"])


if __name__ == "__main__":
    unittest.main()
