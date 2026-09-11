#!/usr/bin/env python3
"""flake.nix and nix/: what the outputs claim, and (with nix) what they are.

The flake used to be two attributes -- `packages.<sys>.{wdotool,default}` and
a devShell -- and one derivation that shipped the six tools, five shadow
symlinks and no `share/` at all.  `nix flake show` said so, `nix eval
.#nixosModules` answered "does not provide attribute", and `nix flake check`
printed "all checks passed!" after running zero tests [recon2/pkg-nix §1].
Four separate claims in the tree were false against it: `nix run .` died
(`unable to execute .../bin/w11`, no meta.mainProgram),
scripts/parity-oracle.sh told the reader to `nix build .#xdotool` (no such
attribute), the postInstall comment said "No --prefix PATH here" while the
built wrapper's own makeCWrapper line prefixed PATH with six store bins, and
README's install section offered a route to a package with no udev rule, no
extension and no .desktop file in it.

So most of this file is text over nix source, in the shape of
tests/test_release_deb.py: the .deb's payload list is the specification, and
every path in it has to be named by some nix output.  The half that needs a
nix on the box is gated on W11_NIX_LIVE=1 (the `nix` CI job sets it): it builds
every package, counts the two bin/ directories, runs `nix run`, lists the
checks, and provokes the module's one assertion.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import w11common                                                   # noqa: E402

VERSION = w11common.VERSION
NIX = os.path.join(ROOT, "nix")
FLAKE = os.path.join(ROOT, "flake.nix")
MODULE = os.path.join(NIX, "module.nix")
PACKAGE = os.path.join(NIX, "package.nix")

#: Every output `packages.<system>` must declare, and what each one is for.
#: The .deb ships all of it in one binary package; nix cannot, because the
#: five shadow names collide with xdotool/xorg.xprop/xorg.xrandr/arandr in
#: environment.systemPackages and the GTK stack is 330.9 MiB nobody using
#: wdotool needs.
PACKAGES = ("w11", "warandr", "gnome-bridge", "gnome-overlap",
            "udev-rules", "x11-shadows")

#: The seven console scripts of pyproject.toml [project.scripts].  `xw11` is
#: the X11 proxy: not a clone of anything, and the program the other four run
#: the ORIGINAL xdotool/wmctrl/xprop/xrandr against on a Wayland session.
TOOLS = ("wdotool", "wwmctl", "wxprop", "wxrandr", "warandr", "wmirror", "xw11")

#: What packages.w11 ships of them: six.  `warandr` is removed in its
#: postInstall, because packages.warandr installs a bin/warandr of its own and
#: two packages with the same name in bin/ collide -- silently in
#: environment.systemPackages (buildEnv, ignoreCollisions = true, first one
#: wins) and fatally in home-manager's home.path (ignoreCollisions = false).
CLI_TOOLS = tuple(t for t in TOOLS if t != "warandr")

#: The five names the README calls "installing over the originals"; wmirror
#: gets none because there is no X11 original to shadow.
SHADOWS = ("xdotool", "wmctrl", "xprop", "xrandr", "arandr")

CHECKS = ("module-eval", "nixos-gnome", "nixos-kde", "nixos-sway", "tools")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def nix_files():
    """flake.nix and every file under nix/, as {relative path: text}."""
    out = {"flake.nix": read(FLAKE)}
    for dirpath, _dirs, names in os.walk(NIX):
        for name in sorted(names):
            if name.endswith(".nix"):
                full = os.path.join(dirpath, name)
                out[os.path.relpath(full, ROOT)] = read(full)
    return out


def nix(subcommand, *args, **kw):
    """`nix <subcommand>` against the flake as a path, not as a git tree.

    `path:` copies the directory as it is; the default git fetcher sees only
    files git knows about, so a test of the working tree has to say path: or
    it would test the last commit.  --no-write-lock-file goes AFTER the
    subcommand (nix rejects it as a global flag) and is there because a test
    may not edit flake.lock -- but `nix derivation show` is not a flake
    command and answers `unrecognised flag '--no-write-lock-file'`, so it is
    passed only to the ones that take it.
    """
    lock = [] if subcommand == "derivation" else ["--no-write-lock-file"]
    cmd = ["nix", "--extra-experimental-features", "nix-command flakes",
           subcommand, *lock, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 3600), **kw)


def flake_ref(attr=""):
    return "path:%s%s" % (ROOT, ("#" + attr) if attr else "")


class Version(unittest.TestCase):
    """R20.  One number in eight files, and two of them are nix."""

    def test_every_version_string_in_the_nix_sources_is_the_version(self):
        """tests/test_release_deb.py:OneVersionEverywhere reads flake.nix
        alone.  The package set moved into nix/package.nix, so a `version =`
        left behind in there would be just as wrong and nothing was looking
        at it."""
        for name, text in sorted(nix_files().items()):
            with self.subTest(name):
                found = set(re.findall(r'version\s*=\s*"([0-9][^"]*)"', text))
                self.assertTrue(found <= {VERSION}, (name, found))
        self.assertIn(VERSION, read(FLAKE))

    def test_the_version_is_declared_once_and_passed_down(self):
        """nix/package.nix takes `version` as an argument: if it carried the
        literal too, a release bump would have two places here instead of
        one and this file would still be green."""
        self.assertNotIn('version = "', read(PACKAGE))


class Outputs(unittest.TestCase):
    """R20, text: the attribute names other files in the tree promise."""

    @classmethod
    def setUpClass(cls):
        cls.flake = read(FLAKE)

    def test_every_package_is_declared_by_name(self):
        """Declared and exported: `w11` and `warandr` are `let`
        bindings (x11-shadows links into both), so a package can exist in the
        file and be in no attribute set."""
        pkg = read(PACKAGE)
        for name in PACKAGES:
            with self.subTest(name):
                self.assertRegex(pkg, r"(?m)^\s*%s = " % re.escape(name))
        self.assertRegex(pkg, r"(?m)^\s*inherit w11 warandr;")
        self.assertRegex(self.flake, r"default = w11pkgs\.w11;")

    def test_the_two_oracle_attributes_the_parity_script_names_exist(self):
        """scripts/parity-oracle.sh has documented `nix build .#xdotool` /
        `.#wmctrl` since it was written and neither existed: the flake
        answered `Did you mean wdotool?` [recon2/pkg-nix §1].  Both halves
        are pinned here so the sentence and the flake cannot drift apart
        again."""
        script = read(os.path.join(ROOT, "scripts", "parity-oracle.sh"))
        named = set()
        # Every `.#attr` in the file, wherever it is written.  An earlier
        # version only read lines containing "nix build", with a second clause
        # for a W11_ORACLE_PATH continuation line -- which was dead, because
        # the W11_ORACLE_PATH line in that script contains "nix build" itself
        # and the first clause always fired.  A continuation line that names
        # a third attribute has to be read too.
        for line in script.splitlines():
            named |= set(re.findall(r"\.#([a-z0-9-]+)", line))
        self.assertTrue(named, "the parity script no longer names an attribute")
        declared = set(re.findall(r"inherit \(pkgs\) ([a-z0-9 ]+);", self.flake))
        declared = {w for group in declared for w in group.split()}
        declared |= set(PACKAGES) | {"default"}
        self.assertEqual(named - declared, set(), sorted(named - declared))
        self.assertIn("xdotool", named)
        self.assertIn("wmctrl", named)

    def test_the_two_installable_packages_share_no_path(self):
        """The split exists so that installing both cannot go wrong, and
        there are two ways they would have overlapped: bin/warandr, which
        packages.w11 removes, and the Python tree, which only ONE of
        the two carries -- packages.warandr is a runCommand holding a symlink
        and a .desktop file, and the buildPythonApplication behind it
        (warandrApp) is not an output at all.  Live builds both and looks;
        these are the three lines that say it was meant."""
        pkg = read(PACKAGE)
        self.assertRegex(pkg, r'(?m)^\s*rm "\$out"/bin/warandr$')
        self.assertRegex(pkg, r'rm "\$out"/bin/wdotool "\$out"/bin/wwmctl')
        self.assertRegex(pkg, r'(?m)^  warandr = runCommand "w11-warandr-\$\{version\}"')

    def test_the_license_is_the_one_in_the_license_file(self):
        """meta.license was lib.licenses.unfree while there was no LICENSE,
        which nix enforces by refusing to EVALUATE the package -- a
        nixosSystem carrying the module answered `Refusing to evaluate
        package 'w11-0.4.0'`, so the flake carried an
        allowUnfreePredicate to get its own outputs back.  LICENSE exists
        now; both halves of that arrangement have to go together, and a
        package still marked unfree with no predicate would be a flake
        nobody can evaluate."""
        spdx = read(os.path.join(ROOT, "LICENSE")).splitlines()[0]
        self.assertEqual(spdx, "SPDX-License-Identifier: BSD-2-Clause")
        self.assertRegex(read(PACKAGE), r"license = lib\.licenses\.bsd2;")
        # Comments stripped: flake.nix explains at length why the predicate
        # was there and why it is gone, so a plain substring search over the
        # file would fail on the explanation.
        code = "\n".join(ln for ln in self.flake.splitlines()
                         if not ln.lstrip().startswith("#"))
        self.assertNotIn("allowUnfreePredicate", code)

    def test_both_module_outputs_are_declared(self):
        self.assertRegex(self.flake, r"nixosModules\.default = import \./nix/module\.nix")
        self.assertRegex(self.flake, r"homeManagerModules\.default = import \./nix/home-manager\.nix")

    def test_the_checks_are_declared_by_name(self):
        """`nix flake check` ran zero tests and said "all checks passed!"
        [recon2/pkg-nix §1 defect 5]."""
        for name in CHECKS:
            with self.subTest(name):
                self.assertRegex(self.flake, r"(?m)^\s*%s = import \./nix/checks/%s\.nix"
                                 % (re.escape(name), re.escape(name)))
                self.assertTrue(os.path.exists(os.path.join(NIX, "checks", name + ".nix")))

    def test_every_option_the_module_promises_is_declared(self):
        """Ten options, and each one is a thing the .deb does or the proxy
        needs: the tools, the GUI, the udev rule, the bridge, the overlap
        extension, the X11 originals the handover needs, the shadow names,
        wl-mirror, and the proxy as a user service.  A misspelt option name is
        not an error in nix -- it is an option nobody sets."""
        module = read(MODULE)
        for name in ("enable", "package", "warandr.enable", "uinput.enable",
                     "gnomeBridge.enable", "gnomeOverlap.enable", "x11Tools.enable",
                     "shadowOriginals", "wlMirror.enable", "proxy.enable"):
            with self.subTest(name):
                self.assertRegex(module, r"(?m)^    %s = lib\.mk" % re.escape(name))
        self.assertRegex(module, r"(?m)^\s*\+\+ lib\.optional cfg\.warandr\.enable w11pkgs\.warandr")

    def test_the_proxy_option_is_off_and_runs_the_packages_own_xw11(self):
        """`programs.w11.proxy.enable` is the one route to shadow ids that
        outlive the proxy's fifteen-minute idle exit (design section 2.6): a
        user service that never idles.  Off by default -- the wrapper spawns
        one on demand and nothing is owed to a session that never runs an X
        tool -- and `--foreground`, because systemd owns the lifetime here and
        the on-demand double fork would leave it nothing to supervise."""
        module = read(MODULE)
        self.assertRegex(module, r"(?m)^    proxy\.enable = lib\.mkOption \{")
        body = module.split("proxy.enable = lib.mkOption {", 1)[1].split("};", 1)[0]
        self.assertIn("default = false;", body)
        self.assertRegex(module, r'ExecStart = "\$\{cfg\.package\}/bin/xw11 --foreground";')
        self.assertIn("systemd.user.services.xw11", module)
        self.assertIn('wantedBy = [ "graphical-session.target" ]', module)

    def test_the_service_waits_for_xwayland_instead_of_failing_five_times(self):
        """The unit's restart numbers, which are not decoration: `serve()` exits
        1 when nothing answers $DISPLAY and no Xwayland is running
        (xw11/cli.py:_refusal), and on GNOME and KWin -- both start Xwayland on
        demand -- that is the ordinary state at graphical-session.target.  With
        systemd's default rate limit (5 starts in 10 s) the unit would be
        `failed` before the session's first X tool ran, and the option's whole
        promise (ids that last the session) would be quietly unmet.  Five
        seconds apart and no limit on the number is what makes it a wait."""
        module = read(MODULE)
        body = module.split("systemd.user.services.xw11", 1)[1]
        self.assertIn('Restart = "on-failure";', body)
        self.assertRegex(body, r"(?m)^\s*RestartSec = 5;")
        # Unit-level: systemd took the start-limit settings out of [Service] in
        # v229, and NixOS spells the unit's own as startLimitIntervalSec.
        self.assertRegex(body, r"(?m)^\s*startLimitIntervalSec = 0;")

    def test_main_program_is_declared_so_nix_run_works(self):
        """Without it `nix run .` looked for $out/bin/w11, which is
        the pname and not a script: "unable to execute ...: No such file or
        directory" [recon2/pkg-nix §1 defect 1]."""
        self.assertRegex(read(PACKAGE), r'mainProgram = "wdotool";')

    def test_the_platforms_claim_is_linux(self):
        """The old meta claimed nothing at all, so nixpkgs' default let the
        package advertise darwin and freebsd.  Every backend here is a
        Wayland protocol, an X11 connection or /dev/uinput."""
        self.assertRegex(read(PACKAGE), r"platforms = lib\.platforms\.linux;")

    def test_the_postinstall_comment_no_longer_denies_the_path_prefix(self):
        """buildPythonApplication prefixes PATH whatever the comment said,
        and the built wrapper's makeCWrapper line proved it [recon2/pkg-nix
        §1 defect 3].  The comment now says why the handover survives it."""
        pkg = read(PACKAGE)
        self.assertNotIn("No --prefix PATH here", pkg)
        self.assertIn("--prefix 'PATH'", pkg)
        self.assertIn("is_us()", pkg)


class DebPayloadIsCovered(unittest.TestCase):
    """R20: the .deb <-> flake coherence gate this repo did not have.

    debian/w11.install is the list of everything the Debian package
    ships beyond the Python modules.  Not one of those paths was in the nix
    output: `find $out -maxdepth 4 -type d` named only site-packages, and
    there was no `share/` at all [recon2/pkg-nix §1].  A file added to the
    .deb and forgotten here is the same bug again."""

    @classmethod
    def setUpClass(cls):
        cls.rows = []
        for line in read(os.path.join(ROOT, "debian", "w11.install")).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                cls.rows.append(line.split()[0])
        # Comments stripped, or this test would pass on a nix file that only
        # TALKS about warandr.desktop -- which the header comment does, and
        # which is exactly the mutation that got through the first draft.
        cls.pkg = "\n".join(ln for ln in read(PACKAGE).splitlines()
                            if not ln.lstrip().startswith("#"))

    def test_the_install_file_still_has_rows(self):
        self.assertGreater(len(self.rows), 5, self.rows)

    def test_every_installed_path_is_named_by_some_nix_output(self):
        """A source path is covered when nix/package.nix names it or names a
        directory above it -- the extension packages copy a directory whole,
        which is how the three typelibs and rules.js get in.  The FIRST
        component is not a directory that may cover anything: `gnome/` is
        every row's parent and naming it would cover the lot."""
        for src in self.rows:
            with self.subTest(src):
                parts = src.split("/")[1:] if "/" in src else [src]
                covered = [p for p in parts if p and p != "*" and p in self.pkg]
                self.assertTrue(covered, "%s is in the .deb and in no nix output" % src)

    def test_the_modules_load_file_debian_rules_installs_is_covered_too(self):
        """It is not in the .install file -- debian/rules renames it on the
        way in (modules-load.d wants the file named for what installs it) --
        and it is half of what makes /dev/uinput carry its ACL from the
        first login."""
        self.assertIn("modules-load-uinput.conf", self.pkg)
        self.assertIn("lib/modules-load.d/w11-uinput.conf", self.pkg)


class UdevRule(unittest.TestCase):
    """R20: one rule, one copy of it, and never nixpkgs' uinput module."""

    def test_the_rule_is_read_from_gnome_and_not_retyped(self):
        self.assertIn('builtins.readFile (gnome "60-w11-uinput.rules")', read(PACKAGE))

    def test_the_rule_text_appears_in_no_nix_file(self):
        """The rule's comment IS the threat model, so a second copy under
        nix/ would be the copy that rots.  The payload line is what is
        searched for -- if someone pastes the rule in, that is the line they
        paste."""
        for name, text in sorted(nix_files().items()):
            with self.subTest(name):
                self.assertNotIn('KERNEL=="uinput"', text)

    def test_the_module_never_turns_on_nixpkgs_uinput_module(self):
        """R20 says "the module never mentions hardware.uinput"; it has to
        mention it, because the plan also asks for an assertion that refuses
        the combination, and an assertion has to read the option and name it
        in its message.  The claim that matters is narrower and is the one
        made here: nowhere does this module SET it.  nixpkgs'
        nixos/modules/hardware/uinput.nix is verbatim MODE="0660"
        GROUP="uinput" plus a uinput group -- the standing input channel the
        shipped rule's own comment rejects, and the opposite of the
        root:root 0600 + uaccess ACL measured on the NixOS GNOME guest
        [recon2/pkg-nix §7]."""
        text = read(MODULE)
        self.assertNotRegex(text, r"(?m)^\s*hardware\.uinput[.a-z]*\s*=")
        # ... and it does refuse the combination, by reading it.  The
        # assertion is named exactly: `assertIn("assertion", text)` was
        # satisfied by any module with any assertion in it, and by the word in
        # a comment.
        self.assertRegex(text, r"assertion = !\(cfg\.uinput\.enable "
                               r"&& config\.hardware\.uinput\.enable\);")

    def test_the_module_ships_the_rule_through_services_udev_packages(self):
        """The mechanism, named: services.udev.packages reads
        lib/udev/rules.d out of a package (udev.nix), which is why
        packages.udev-rules puts the file exactly there.  Measured end to
        end on the GNOME guest: crw-rw----+ root root with user:alice:rw-
        and group::--- [recon2/pkg-nix §7]."""
        self.assertRegex(read(MODULE), r"services\.udev\.packages = \[ w11pkgs\.udev-rules \];")
        self.assertIn("lib/udev/rules.d/60-w11-uinput.rules", read(PACKAGE))


class HomeManagerModule(unittest.TestCase):
    """R20: the half a user can do alone says which half it cannot.

    Comments stripped, and the assertions are on attribute lines.  Every
    string these tests used to look for -- "warnings", "/dev/uinput",
    "install-bridge.sh --udev", "programs.gnome-shell.extensions" -- is in
    this file's header comment or in an option description, so the first
    draft passed on a file that had deleted the whole `config` block and kept
    talking about it.  Same rule as DebPayloadIsCovered above."""

    @classmethod
    def setUpClass(cls):
        cls.code = "\n".join(ln for ln in read(os.path.join(NIX, "home-manager.nix")).splitlines()
                             if not ln.lstrip().startswith("#"))

    def test_it_hands_the_extension_over_by_package_not_by_path(self):
        """home-manager's programs.gnome-shell.extensions defaults an
        entry's id to package.passthru.extensionUuid and expects the
        extension at $out/share/gnome-shell/extensions/<id>, which is why
        the extension packages set that passthru [recon2/pkg-nix §3]."""
        self.assertRegex(self.code, r"(?m)^\s*programs\.gnome-shell\.extensions =")
        self.assertIn("package = w11pkgs.gnome-bridge", self.code)
        self.assertIn("passthru.extensionUuid", read(PACKAGE))

    def test_it_turns_the_gnome_shell_module_on_or_the_extension_is_inert(self):
        """home-manager's modules/programs/gnome-shell.nix wraps its whole
        `config` -- the extension packages AND the dconf enable -- in
        `mkIf cfg.enable`, and nothing else sets that option.  Measured
        against home-manager 2c0350c on 2026-09-08, with the line deleted
        and gnomeBridge.enable = true: `programs.gnome-shell.enable` false,
        no `dconf.settings."org/gnome/shell"` at all, and a home.packages
        with no extension in it.  With the line: true,
        `['w11-bridge@w11']`, and
        w11-gnome-bridge-0.4.0 in home.packages.  A text test cannot
        see any of that, which is why it pins the line itself."""
        self.assertRegex(self.code, r"(?m)^\s*programs\.gnome-shell\.enable = "
                                    r"lib\.mkIf cfg\.gnomeBridge\.enable")

    def test_it_warns_that_it_cannot_grant_uinput(self):
        """The udev rule is system state.  Without this sentence the next
        step a user takes is gnome/install-bridge.sh --udev, which writes
        /etc/udev/rules.d and /etc/modules-load.d -- both generated from the
        store on NixOS [recon2/pkg-nix §3]."""
        self.assertRegex(self.code, r"(?m)^\s*warnings = ")
        self.assertIn("/dev/uinput", self.code)
        self.assertIn("install-bridge.sh --udev", self.code)


class Live(unittest.TestCase):
    """R21: with a nix on the box, the outputs are built and looked at."""

    @classmethod
    def setUpClass(cls):
        if not os.environ.get("W11_NIX_LIVE"):
            raise unittest.SkipTest("needs nix: set W11_NIX_LIVE=1")
        if not shutil.which("nix"):
            raise unittest.SkipTest("W11_NIX_LIVE is set and there is no nix on PATH")
        args = [flake_ref(p) for p in PACKAGES]
        r = nix("build", "--no-link", "--print-out-paths", *args)
        # Not a skip: W11_NIX_LIVE is opt-in, and a build that fails on a box
        # that asked for the live half is the finding, not a reason to be
        # quiet.  The first version of this file skipped here and hid an
        # `unrecognised flag` for a whole run.
        assert r.returncode == 0, "nix build failed:\n%s" % r.stderr[-3000:]
        paths = r.stdout.split()
        assert len(paths) == len(PACKAGES), paths
        cls.out = dict(zip(PACKAGES, paths))

    def test_the_cli_package_carries_the_five_stdlib_tools_and_nothing_else(self):
        """The old single derivation had eleven names in bin/: the tools plus
        the five shadows, which is what collided in environment.systemPackages
        [recon2/nixos].  Every console script except `warandr`: that one
        belongs to packages.warandr alone, and the two packages are installed
        together.

        Beside each script is buildPythonApplication's `.NAME-wrapped`, and
        that is not noise -- it is the reason vm/vmctl's pg() matches
        `.NAME-wrapped` truncated to fifteen characters instead of a plain
        `pgrep -x name`: on NixOS the wrapper is what ends up in /proc/comm
        [recon2/nixos §6.1]."""
        names = sorted(os.listdir(os.path.join(self.out["w11"], "bin")))
        self.assertEqual([n for n in names if not n.startswith(".")], sorted(CLI_TOOLS))
        self.assertEqual([n for n in names if n.startswith(".")],
                         sorted(".%s-wrapped" % t for t in CLI_TOOLS))

    def test_the_two_packages_can_be_installed_together_in_either_order(self):
        """The collision this split exists to prevent, provoked from both
        sides.  environment.systemPackages is a buildEnv with
        ignoreCollisions = true, so a duplicate bin/warandr there is resolved
        silently in favour of whichever package the module list happens to
        put first; home.path's buildEnv has ignoreCollisions = false and
        fails the build outright.  So: buildEnv WITHOUT ignoreCollisions, in
        both orders -- if the two bin/ directories overlap at all this does
        not build, and if the link lands in the wrong package `warandr`
        cannot import gi."""
        expr = """
          let flake = builtins.getFlake "%s";
              pkgs = flake.inputs.nixpkgs.legacyPackages."x86_64-linux";
              p = flake.packages."x86_64-linux";
          in pkgs.buildEnv { name = "both"; paths = [ p.%s p.%s ]; }
        """
        for first, second in (("w11", "warandr"), ("warandr", "w11")):
            with self.subTest("%s then %s" % (first, second)):
                r = nix("build", "--impure", "--no-link", "--print-out-paths",
                        "--expr", expr % (flake_ref(), first, second))
                self.assertEqual(r.returncode, 0, r.stderr[-2000:])
                link = os.path.join(r.stdout.strip(), "bin", "warandr")
                self.assertTrue(os.path.islink(link) or os.path.exists(link), link)
                self.assertIn("-w11-warandr-", os.path.realpath(link))

    def test_the_shadow_package_is_the_five_links_alone(self):
        binned = os.path.join(self.out["x11-shadows"], "bin")
        self.assertEqual(sorted(os.listdir(binned)), sorted(SHADOWS))
        for name in SHADOWS:
            with self.subTest(name):
                link = os.path.join(binned, name)
                self.assertTrue(os.path.islink(link))
                self.assertTrue(os.path.exists(link), os.readlink(link))

    def test_the_gui_package_is_one_link_and_its_desktop_entry(self):
        """Two files, and no lib/ at all.  packages.warandr and
        packages.w11 are both installed on a machine that wants the
        GUI, so every path either one owns is a path the other must not:
        bin/warandr is a link into the private GTK build, and the Python tree
        that build carries stays there rather than colliding with this
        package's own site-packages.  The `.warandr-wrapped` BESIDE THE
        TARGET is makeWrapper's, and its presence is the proof that the GTK
        wrapping ran."""
        out = self.out["warandr"]
        self.assertEqual(sorted(os.listdir(os.path.join(out, "bin"))), ["warandr"])
        link = os.path.join(out, "bin", "warandr")
        self.assertTrue(os.path.islink(link))
        target = os.path.realpath(link)
        self.assertIn("-w11-warandr-app-", target)
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(target), ".warandr-wrapped")))
        self.assertEqual(sorted(os.listdir(out)), ["bin", "share"])
        self.assertTrue(os.path.exists(os.path.join(out, "share/applications/warandr.desktop")))

    def test_the_extension_packages_are_where_gnome_shell_looks(self):
        for pkg, uuid in (("gnome-bridge", "w11-bridge@w11"),
                          ("gnome-overlap", "w11-overlap@w11")):
            with self.subTest(pkg):
                d = os.path.join(self.out[pkg], "share/gnome-shell/extensions", uuid)
                self.assertTrue(os.path.isdir(d), d)
                self.assertIn("metadata.json", os.listdir(d))

    def test_the_udev_package_is_the_rule_and_the_modules_load_entry(self):
        out = self.out["udev-rules"]
        rule = os.path.join(out, "lib/udev/rules.d/60-w11-uinput.rules")
        self.assertTrue(os.path.exists(rule))
        self.assertEqual(read(rule), read(os.path.join(ROOT, "gnome", "60-w11-uinput.rules")))
        self.assertTrue(os.path.exists(os.path.join(out, "lib/modules-load.d/w11-uinput.conf")))

    def test_nix_run_prints_the_clone_version(self):
        """`nix run .` died before meta.mainProgram existed.  With the
        escape hatch set it prints our own string; without it, it would hand
        over to whatever xdotool is on this box, which is the behaviour the
        recon measured (3.20160805.1 from /usr/bin) [recon2/pkg-nix §2a]."""
        env = dict(os.environ, W11_PASSTHROUGH="never")
        r = nix("run", flake_ref(), "--", "--version", env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        from wdotool import cli
        self.assertEqual(r.stdout.strip(), "xdotool version %s" % cli.XDO_VERSION)

    def test_flake_check_has_five_checks_to_run(self):
        r = nix("eval", flake_ref("checks.x86_64-linux"), "--apply", "builtins.attrNames")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(sorted(re.findall(r'"([a-z-]+)"', r.stdout)), sorted(CHECKS))

    def test_the_module_eval_check_evaluates_a_system_without_building_one(self):
        """The check's whole reason to exist is that it costs seconds where
        the VM tests cost tens of minutes, and it did the opposite: written
        as a plain `${toplevel.drvPath}` the interpolation carried the
        drvPath's `=` string context, so nix added the system's whole .drv
        closure as inputs -- measured here on 2026-09-08 by putting the
        interpolation back: 3993 inputDrvs and 4614 inputSrcs, among them
        nixos-system-nixos-26.11.20260831.34ab990.drv, linux-6.5.6.tar.xz and
        the initrd units -- and `nix build` of it then went off realising
        that closure: one such run was still going 21 minutes later, when it
        was killed.  builtins.unsafeDiscardStringContext is the fix and this
        is the regression pin: two input derivations (bash and
        stdenv-linux-no-cc), and no NixOS system among them."""
        r = nix("derivation", "show", flake_ref("checks.x86_64-linux.module-eval"))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        # nix 2.34 answers `{"version": 4, "derivations": {...}}` and puts the
        # inputs under `inputs.drvs`; older nix answered the inner mapping
        # alone with `inputDrvs` at the top.  Both spellings are read, and the
        # first draft of this test read only the old one -- against which BOTH
        # forms of the check reported zero inputs, which is a test that cannot
        # fail.  KeyError is not the alternative: an unknown shape has to be
        # loud, so a derivation with neither key raises.
        payload = json.loads(r.stdout)
        (only,) = payload.get("derivations", payload).values()
        inputs = sorted(only["inputs"]["drvs"] if "inputs" in only else only["inputDrvs"])
        self.assertLess(len(inputs), 10, len(inputs))
        self.assertEqual([d for d in inputs if "nixos-system" in d], [])
        # ... and it does still evaluate the module, which is the other half:
        # a check that inputs nothing because it evaluates nothing would pass
        # this test too.
        b = nix("build", "--no-link", "--print-out-paths",
                flake_ref("checks.x86_64-linux.module-eval"))
        self.assertEqual(b.returncode, 0, b.stderr[-2000:])
        self.assertRegex(read(b.stdout.strip()), r"^/nix/store/[a-z0-9]{32}-nixos-system-.*\.drv$")

    def test_a_system_that_turns_on_both_uinput_grants_is_refused(self):
        """The assertion, provoked: a nixosSystem carrying the module and
        hardware.uinput.enable = true must not evaluate."""
        expr = """
          let flake = builtins.getFlake "%s";
          in (flake.inputs.nixpkgs.lib.nixosSystem {
            system = "x86_64-linux";
            modules = [ flake.nixosModules.default ({ ... }: {
              boot.loader.grub.device = "nodev";
              fileSystems."/" = { device = "/dev/vda1"; fsType = "ext4"; };
              system.stateVersion = "25.11";
              programs.w11.enable = true;
              hardware.uinput.enable = %s;
            }) ];
          }).config.system.build.toplevel.drvPath
        """
        bad = nix("eval", "--impure", "--expr", expr % (flake_ref(), "true"))
        self.assertNotEqual(bad.returncode, 0, bad.stdout)
        self.assertIn("Failed assertions", bad.stderr)
        self.assertIn("hardware.uinput.enable", bad.stderr)
        # ... and the same system without it evaluates, so the refusal is the
        # assertion and not a broken module.
        good = nix("eval", "--impure", "--expr", expr % (flake_ref(), "false"))
        self.assertEqual(good.returncode, 0, good.stderr[-2000:])
        self.assertIn(".drv", good.stdout)


if __name__ == "__main__":
    unittest.main()
