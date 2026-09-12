#!/usr/bin/env python3
"""vm/*.sh: the parts that can be run without a rig, and the two that were wrong.

Nothing in this file boots a virtual machine.  `vm/` is five bash scripts and a
Python `vmctl`, and the interesting failures in them are not "the image did not
come up" -- they are a status that was thrown away, a parser that counted a head
that was not there, and (since the rig grew a second and a third distro) a
package-manager line that only one distribution would have accepted.  All of
those are reachable by slicing the shipped script and feeding it text, which is
what happens here; anything that needs the rig itself is gated on `VMCTL_LIVE`.

* `vm/build-iso-golden.sh` ran stage 2 down a pipeline (`$SSH ... | tee | sed`)
  and then read `$?`, which is `sed`'s and is 0 whatever ssh did.  Every stage-2
  failure -- ssh dropped, sudo refused the password, the guest rebooted under
  it -- was reported as `stage 2 did not finish (rc 0)`, and the real status was
  gone.  Fix 53: `rc=${PIPESTATUS[0]}`.
* `vm/selftest.sh` had two parsers for the same tool.  The `kde-x11)` one reads
  `kscreen-doctor -o` per output BLOCK, because Plasma 5.27 puts the state on
  the `Output:` line and 6.x on the indented lines under it; the `kde)` one was
  a `grep` over the `Output:` line alone, so on Wayland it counted a *disabled*
  head as present -- and the rig test that unplugs a head and counts what is
  left was the one asking.  Fix 54: one parser for both.
* `vm/build-image.sh` was apt from top to bottom (DEBIAN_FRONTEND, debconf,
  netplan, snap, `dpkg-query -W`) with one `case` arm per desktop that also knew
  that desktop's display manager.  It is now three layers -- `pkg_*` per package
  manager, `dm_*` per display manager, `desktop_*` per desktop -- with a
  dispatch table naming every `DESKTOPS` key of `vm/vmctl` and nothing else.
  Each layer is sliced and run here against stubs.

The kscreen fixtures are synthetic, in the two shapes the comment in the script
describes.  The wlr-randr, hyprctl and cosmic-randr captures are real output;
tests/fixtures/vm/README.md says which tool, which version and where each was
recorded.
"""

import json
import os
import pathlib
import re
import shlex
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

VM = os.path.join(ROOT, "vm")
KSCREEN = os.path.join(ROOT, "tests", "fixtures", "kscreen")
VMFIX = os.path.join(ROOT, "tests", "fixtures", "vm")
SELFTEST = os.path.join(VM, "selftest.sh")
ISO_GOLDEN = os.path.join(VM, "build-iso-golden.sh")
BUILD = os.path.join(VM, "build-image.sh")
VMCTL = os.path.join(VM, "vmctl")
FLAVORS = os.path.join(VM, "flavors")

#: The five bash scripts under vm/.  vmctl is Python and is covered elsewhere.
SCRIPTS = ("build-image.sh", "build-iso-golden.sh", "build-iso-image.sh",
           "selftest.sh", "setup-host.sh")

LIVE = unittest.skipUnless(os.environ.get("VMCTL_LIVE"),
                           "needs the QEMU rig: set VMCTL_LIVE=1")


def vmctl_module():
    """`vm/vmctl` imported as a module.

    It has no extension and is not on any path, so importlib does the work; the
    file has no import-time side effects beyond building Paths out of $VMDATA."""
    import importlib.util
    spec = importlib.util.spec_from_loader(
        "vmctl_under_test",
        importlib.machinery.SourceFileLoader("vmctl_under_test", VMCTL))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sh_layer(*names):
    """Several functions of vm/build-image.sh, concatenated in the order given.

    A layer function calls its neighbours (`pkg_install` calls `pkg_retry` calls
    `wait_net`), so a slice of one alone would not run.  Sliced rather than
    copied for the usual reason: what runs here is what ships."""
    return "".join(support.sh_function(BUILD, n) for n in names)


def stubs(tmp, names, log):
    """A directory of stub executables that append `$0 $*` to `log` and exit 0.

    `<name>=<n>` in `names` makes that stub exit n instead, which is how the
    retry loops are made to run.  A leading `+` makes it append its STDIN to the
    log as well (`+debconf-set-selections`): the answer a stub is piped is the
    whole point of pkg_seed_dm, and a stub that discards stdin cannot tell its
    three arms apart."""
    d = os.path.join(tmp, "stub")
    os.makedirs(d, exist_ok=True)
    for spec in names:
        stdin = spec.startswith("+")
        name, _, rc = spec.lstrip("+").partition("=")
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%%s\\n" "$(basename "$0") $*" >> %s\n' % log)
            if stdin:
                fh.write('sed "s/^/stdin: /" >> %s\n' % log)
            fh.write('exit %s\n' % (rc or "0"))
        os.chmod(path, 0o755)
    return d


PREAMBLE = """
set -u
say() { printf 'say: %s\\n' "$*" >> "$FAKE_LOG"; }
fail() { printf 'fail: %s\\n' "$*" >> "$FAKE_LOG"; exit 9; }
written() { printf '%s\\n' "$@" >> "$WRITTEN"; }
"""

# The display managers' unit files as the packages ship them.  Debian's gdm3.service
# is the odd one: no [Install] section, because the gdm3 postinst links
# display-manager.service itself from /etc/X11/default-display-manager -- which is
# why `systemctl enable gdm3` succeeds and enables nothing (CI run 34286867525).
_ALIASED = "[Unit]\nDescription=%s\n\n[Install]\nAlias=display-manager.service\n"
UNIT_FILES = {"gdm3": "[Unit]\nDescription=GNOME Display Manager\n",
              "gdm": _ALIASED % "GNOME Display Manager",
              "sddm": _ALIASED % "Simple Desktop Display Manager",
              "plasmalogin": _ALIASED % "Plasma Login Manager",
              "lightdm": _ALIASED % "Light Display Manager",
              "greetd": _ALIASED % "Greeter daemon"}

# systemd's enable/disable, as far as a display manager's Alias=display-manager.service
# goes (the double links the alias only when the unit's [Install] carries it, and
# says so when it does not, exit 0, as systemd does).
# goes: `enable` needs the unit file and refuses when the alias already points at
# another unit (systemd's "File display-manager.service already exists and is a
# symlink to ..."), `disable` drops the alias of the unit named.  A stub that only
# logs would let every dm_* pass with the display manager still disabled -- which
# is the golden image that booted to a text console on 2026-09-08.
SYSTEMCTL_DOUBLE = """#!/bin/sh
printf "%%s\\\\n" "systemctl $*" >> %s
root=${VMCTL_ROOT:-}; alias=$root/etc/systemd/system/display-manager.service
cmd=$1; shift
for u in "$@"; do
    unit=$root/usr/lib/systemd/system/${u%%.service}.service
    case $cmd in
    enable)
        [ -f "$unit" ] || { echo "Failed to enable unit: Unit file ${u%%.service}.service does not exist." >&2; exit 1; }
        grep -q "^Alias=display-manager.service" "$unit" || { echo "The unit files have no installation config (WantedBy=, RequiredBy=, Also=, Alias= settings in the [Install] section, and DefaultInstance= for template units)." >&2; continue; }
        if [ -L "$alias" ] && [ "$(readlink -f "$alias")" != "$unit" ]; then
            echo "Failed to enable unit: File display-manager.service already exists and is a symlink to $(readlink "$alias")." >&2
            exit 1
        fi
        mkdir -p "$(dirname "$alias")"; ln -sfn "$unit" "$alias" ;;
    disable)
        [ -L "$alias" ] && [ "$(readlink -f "$alias")" = "$unit" ] && rm "$alias" ;;
    esac
done
exit 0
"""


class TheyAllParse(unittest.TestCase):
    """`bash -n` over every script in vm/.

    They are run by hand, months apart, against a build that takes forty
    minutes, so a syntax error in a branch nobody took that day is expensive in
    a way an ordinary script's is not."""

    def test_every_script_parses(self):
        for name in SCRIPTS:
            with self.subTest(name):
                got = subprocess.run(["bash", "-n", os.path.join(VM, name)],
                                     capture_output=True, text=True, timeout=60)
                self.assertEqual((got.returncode, got.stderr), (0, ""))

    def test_vmctl_compiles(self):
        got = subprocess.run([sys.executable, "-m", "py_compile",
                              os.path.join(VM, "vmctl")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(got.returncode, 0, got.stderr)


class StageTwoReportsSshsStatus(unittest.TestCase):
    """Fix 53, finding F7.6.  The shipped pipeline, sliced, with a stand-in ssh
    that fails."""

    def block(self, rc):
        """The pipeline out of build-iso-golden.sh, with `$SSH` bound to a
        function that produces output and then fails with `rc`."""
        body = ("set -eu\n"
                "B=$(mktemp -d)\n"
                "trap 'rm -rf \"$B\"' EXIT\n"
                "ssh() { echo 'stage 2 output'; return %d; }\n"
                "SSH=ssh\n"
                % rc)
        # The slice ends at the `set -e` after the assignment, not at the
        # assignment itself: `rc=${PIPESTATUS[0]}` is the fix under test, and a
        # marker that quotes it turns a regression to `rc=$?` into a missing
        # marker inside sh_block -- an error about slicing, not the RC=0 this
        # test exists to print.
        body += support.sh_block(ISO_GOLDEN,
                                 "set +e\n$SSH 'sudo -S -p \"\" bash /tmp/build-iso-image.sh'",
                                 "\nset -e\n")
        body += "\nprintf 'RC=%s\\n' \"$rc\"\n"
        return body

    def run_block(self, rc):
        got = subprocess.run(["bash", "-c", self.block(rc)],
                             capture_output=True, text=True, timeout=120,
                             env=dict(os.environ, LC_ALL="C"))
        self.assertEqual(got.returncode, 0, got.stderr)
        return got.stdout.strip()

    def test_a_failing_ssh_is_reported_with_its_own_status(self):
        """3 is what ssh exits with when the remote command exits 3; the
        message the script then prints -- `stage 2 did not finish (rc N)` -- is
        the only trace of a forty-minute build that stopped for a reason worth
        knowing."""
        self.assertEqual(self.run_block(3), "RC=3")

    def test_a_succeeding_ssh_is_still_zero(self):
        """The positive control: PIPESTATUS[0] of a pipeline whose first
        command succeeded is 0, and the script has to carry on."""
        self.assertEqual(self.run_block(0), "RC=0")

    def test_the_script_does_not_read_the_bare_dollar_question_there(self):
        """Read out of the source as well, because the slice above only proves
        the one pipeline: a second `rc=$?` after a pipeline would be the same
        bug in a new place."""
        with open(ISO_GOLDEN, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "rc=$?" and i and "|" in lines[i - 1]:
                self.fail("%s:%d: rc=$? after a pipeline" % (ISO_GOLDEN, i + 1))


# -- vm/selftest.sh: one parser per native display tool -----------------------

def selftest_arms():
    """The top-level `case $desktop in` of selftest.sh as
    {token: {"tool": ..., "oracle": ..., "sockets": ..., "heads_differ": ...}}."""
    with open(SELFTEST, encoding="utf-8") as fh:
        text = fh.read()
    body = text.split("case $desktop in", 1)[1].split("\nesac", 1)[0]
    arms, cur = {}, None
    for line in body.splitlines():
        m = re.match(r"^    ([a-z0-9|*-]+)\)(.*)$", line)
        if m:
            cur = m.group(1)
            if cur == "*":
                cur = None
                continue
            arms[cur] = {}
            line = m.group(2)
        if cur is None:
            continue
        for key, q, bare in re.findall(
                r'\b(tool|oracle|sockets|logind_type|session_type|heads_differ)='
                r'(?:"([^"]*)"|([^\s;]+))', line):
            arms[cur][key] = q if q else bare
    return arms


def selftest_parser(oracle):
    """The embedded `python3 -c` program of one arm of selftest.sh's heads()."""
    body = support.sh_function(SELFTEST, "heads")
    arm, keep = [], False
    for line in body.splitlines():
        m = re.match(r"^    ([a-z|]+)\)", line)
        if m:
            keep = oracle in m.group(1).split("|")
        if keep:
            arm.append(line)
            if line.rstrip().endswith(";;"):
                break
    m = re.search(r"\| python3 -c '\n(.*?)'", "\n".join(arm), re.S)
    assert m, "no python parser in selftest.sh heads() arm %s)" % oracle
    return m.group(1)


def run_parser(src, text):
    got = subprocess.run([sys.executable, "-c", src], input=text,
                         capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stderr
    return got.stdout.splitlines()


def fixture(name):
    with open(os.path.join(VMFIX, name), encoding="utf-8") as fh:
        return fh.read()


class TheKscreenParser(unittest.TestCase):
    """Fix 54, finding F7.7.  One parser, two Plasma generations, three
    captures.

    `kscreen-doctor -o` is the only way to ask KWin what heads it has, and the
    rig test that matters plugs and unplugs them: a parser that counts a
    disabled head reports three when the test has just made it two, and the
    selftest passes on a rig that is not doing what it claims."""

    def outputs(self, fixture_name):
        path = os.path.join(KSCREEN, fixture_name)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        return sorted({ln.split()[0] for ln in run_parser(selftest_parser("kscreen"), text)
                       if ln.startswith("Virtual-")})

    def test_plasma_5_27_one_line_format(self):
        """5.27 writes the whole state on the `Output:` line: `Output: 70
        Virtual-3 disabled connected priority 0 ...`."""
        self.assertEqual(self.outputs("plasma-5.27-wayland-3heads-one-disabled.txt"),
                         ["Virtual-1", "Virtual-2"])

    def test_plasma_6_x_block_format(self):
        """6.x writes `Output: 3 Virtual-3` and then one indented line per
        property, so `disabled` is nowhere near the name."""
        self.assertEqual(self.outputs("plasma-6.6-wayland-3heads-one-disabled.txt"),
                         ["Virtual-1", "Virtual-2"])

    def test_the_x11_backend_lists_connectors_that_are_not_plugged(self):
        """libkscreen's XRandR backend reports every CONNECTOR the X server
        has, disconnected ones included, where KWin on Wayland exports only the
        plugged ones -- which is why the condition is enabled AND connected and
        not enabled alone."""
        self.assertEqual(self.outputs("plasma-6.6-x11-3heads-one-disconnected.txt"),
                         ["Virtual-1", "Virtual-2"])

    def test_wayland_and_x11_share_the_one_parser(self):
        """The fix itself: inside heads(), `kde` and `kde-x11` reach one arm --
        they both set `oracle=kscreen`.  Two parsers is two chances for the next
        Plasma format change to be handled in only one of them, and the one that
        was wrong was the Wayland arm, which is the flavor every Plasma
        measurement in this repository was taken on."""
        arms = selftest_arms()
        self.assertEqual(arms["kde"]["oracle"], "kscreen")
        self.assertEqual(arms["kde-x11"]["oracle"], "kscreen")
        body = support.sh_function(SELFTEST, "heads")
        self.assertEqual(len(re.findall(r"re\.split\(r\"\(\?m\)\^Output: \"", body)), 1)


class TheSelftestParsers(unittest.TestCase):
    """R27.  The three parsers the wlroots, Hyprland and COSMIC flavors added to
    selftest.sh's heads(), each sliced out of the shipped file and run over the
    recorded output of the tool it reads.

    They answer the two questions `expect_monitors` and `layout` ask -- which
    heads are enabled, and where head 0 is -- so a parser that miscounts turns
    the hot-plug half of the selftest into a test that passes on a rig doing
    nothing."""

    def test_wlr_randr_reads_position_per_enabled_head(self):
        """wlr-randr 0.4.1 prints a block per output; the position is a
        `Position: x,y` line inside it."""
        self.assertEqual(run_parser(selftest_parser("wlr"),
                                    fixture("wlr-randr-0.4.1-3heads.txt")),
                         ["HEADLESS-3 2560,0", "HEADLESS-2 1280,0", "HEADLESS-1 0,0"])

    def test_wlr_randr_skips_a_disabled_head(self):
        """The case the parser exists for: a disabled head prints `Enabled: no`
        and then stops -- no Modes, no Position -- so a parser keyed on the name
        line alone would report a head the rig has just switched off."""
        self.assertEqual(run_parser(selftest_parser("wlr"),
                                    fixture("wlr-randr-0.4.1-3heads-one-disabled.txt")),
                         ["HEADLESS-2 1280,0", "HEADLESS-1 0,0"])

    def test_wlr_randr_on_the_rigs_connector_names(self):
        """The same capture with virtio-vga's names: what `monitors()` filters on
        is `^Virtual-`, so the substitution is the only difference that matters."""
        self.assertEqual(run_parser(selftest_parser("wlr"),
                                    fixture("wlr-randr-0.4.1-virtual-3heads-one-disabled.txt")),
                         ["Virtual-2 1280,0", "Virtual-1 0,0"])

    def test_hyprctl_monitors(self):
        """`hyprctl -j monitors` from the Hyprland recon's VM: Virtual-1 at 0,0
        and a runtime headless head at 1920,0 that came up scale 2.00 -- the
        measurement the flavor's `monitor = , preferred, auto, 1` answers."""
        got = run_parser(selftest_parser("hypr"),
                         fixture("hyprctl-monitors-2heads.json"))
        self.assertEqual(got, ["Virtual-1 0,0", "HEADLESS-2 1920,0"])

    def test_hyprctl_skips_a_disabled_monitor(self):
        """`disabled` is the field that says a head is off; it is false on both
        recorded heads, so the negative case is made by flipping it."""
        doc = json.loads(fixture("hyprctl-monitors-2heads.json"))
        doc[1]["disabled"] = True
        self.assertEqual(run_parser(selftest_parser("hypr"), json.dumps(doc)),
                         ["Virtual-1 0,0"])

    def test_cosmic_randr_kdl(self):
        """`cosmic-randr list --kdl`: `output "NAME" enabled=#true {` and an
        indented `position X Y`."""
        self.assertEqual(run_parser(selftest_parser("cosmic"),
                                    fixture("cosmic-randr-winit-1head.kdl")),
                         ["WINIT-0 0,0"])

    def test_cosmic_randr_reads_position_and_not_the_physical_size(self):
        """Two heads and a disabled one, with a physical size that is not the
        position: `physical` comes first inside the node, so a parser matching
        the wrong key answers 0,0 for everything and the layout check passes on
        a rig whose heads are stacked."""
        self.assertEqual(run_parser(selftest_parser("cosmic"),
                                    fixture("cosmic-randr-2heads-one-disabled.kdl")),
                         ["Virtual-1 0,0", "Virtual-2 1920,0"])

    def test_cosmic_randr_skips_a_disabled_output(self):
        self.assertEqual(run_parser(selftest_parser("cosmic"),
                                    fixture("cosmic-randr-winit-1head.kdl")
                                    .replace("enabled=#true", "enabled=#false")),
                         [])

    def test_every_desktops_key_has_an_arm(self):
        """selftest.sh keys off `# vmctl-desktop:`, so a DESKTOPS row with no arm
        here is a flavor that boots and then dies at `unknown desktop`."""
        self.assertEqual(sorted(selftest_arms()), sorted(vmctl_module().DESKTOPS))

    def test_every_arm_names_a_parser_that_exists(self):
        body = support.sh_function(SELFTEST, "heads")
        labels = set()
        for m in re.finditer(r"^    ([a-z|]+)\)", body, re.M):
            labels.update(m.group(1).split("|"))
        for token, arm in selftest_arms().items():
            with self.subTest(token):
                self.assertIn(arm["oracle"], labels)

    def test_the_session_type_agrees_with_vmctls_table(self):
        """Two tables, one fact: selftest.sh asserts `XDG_SESSION_TYPE` and
        `loginctl Type` per desktop, and vmctl decides which sockets to wait for
        from the same row.  Indexed and not `.get`-ed: an arm that forgets the
        key is the failure this is named for, and a default that fell back to
        the DESKTOPS value would have compared that value to itself."""
        desktops = vmctl_module().DESKTOPS
        for token, arm in selftest_arms().items():
            with self.subTest(token):
                self.assertEqual(arm["session_type"], desktops[token]["session"])
                self.assertIn(arm["logind_type"], desktops[token]["logind"])

    def test_heads_differ_is_set_exactly_where_a_panel_sits_on_one_head(self):
        """`heads_differ` is what turns "head 0 and head 1 are byte-identical"
        from a warning into a failure, so it may only be non-empty where the
        desktop really does draw them differently.  sway and i3 put a bar on
        every output, Hyprland, labwc, river and (as configured here) Wayfire,
        Budgie and COSMIC draw the same wallpaper on every head -- the last
        three unmeasured, which is why they are in the empty list and not the
        other one."""
        same = {"sway", "hypr", "labwc", "river", "wayfire", "budgie", "cosmic", "i3"}
        for token, arm in selftest_arms().items():
            with self.subTest(token):
                if token in same:
                    self.assertEqual(arm["heads_differ"], "")
                else:
                    self.assertTrue(arm["heads_differ"], token)

    def test_the_first_run_check_is_not_a_literal_none_on_a_full_desktop(self):
        """`layout` ends every arm with an `initial-setup:` line, and the run
        asserts it says `none`; an arm that PRINTS the literal makes that
        assertion vacuous.  That is honest for the bare compositors -- labwc,
        river, Hyprland and COSMIC ship no first-run dialog -- but oracle=wlr
        also serves xfce-wayland, lxqt-wayland and budgie, three full desktops
        whose X11 twins are exactly why the xrandr arm greps wmctrl for a
        display/welcome window (Xfce's is the reason the rig writes displays.xml
        with Notify=3)."""
        body = support.sh_function(SELFTEST, "layout")
        arm = body.split("wlr|hypr|cosmic)", 1)[1]
        self.assertIn("xfce-wayland|lxqt-wayland|budgie)", arm)
        for name in ("xfce4-display-settings", "lxqt-config-monitor",
                     "budgie-welcome", "update-notifier"):
            with self.subTest(name):
                self.assertIn(name, arm)
        # -x and not -f: the pattern travels in that sh's own argv, and `pgrep -f`
        # would match the shell running it and report a first-run window forever.
        self.assertNotIn("pgrep -a -f", arm)

    def test_the_muffin_arm_asks_cinnamons_own_bus_name(self):
        """Muffin's DisplayConfig carries byte-identical signatures to Mutter's
        under its own name, which is why one arm serves both and only the
        destination and the object path change [recon2/cinnamon]."""
        with open(SELFTEST, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("bus_dest=org.cinnamon.Muffin.DisplayConfig", text)
        self.assertIn("bus_path=/org/cinnamon/Muffin/DisplayConfig", text)
        self.assertEqual(selftest_arms()["cinnamon-wayland"]["oracle"], "muffin")


class TheDeviationLists(unittest.TestCase):
    """The ISO flavors claim "every deviation from an untouched install is
    listed here": four in the flavor yaml, four in the stage-2 script, eight in
    vm/README's table.  Three lists, one set of numbers."""

    @classmethod
    def setUpClass(cls):
        cls.docs = support.documents()
        with open(os.path.join(VM, "build-iso-image.sh"), encoding="utf-8") as fh:
            cls.stage2 = fh.read()

    @staticmethod
    def numbers(text):
        return sorted({int(n) for n in re.findall(r"DEVIATION (\d+)", text)})

    def test_the_flavor_yaml_carries_one_to_four(self):
        for name in ("resolute-gnome-iso.yaml", "noble-gnome-iso.yaml"):
            with self.subTest(name):
                with open(os.path.join(VM, "flavors", name), encoding="utf-8") as fh:
                    self.assertEqual(self.numbers(fh.read()), [1, 2, 3, 4])

    def test_the_stage_two_script_carries_five_to_eight(self):
        self.assertEqual(self.numbers(self.stage2), [5, 6, 7, 8])

    def test_the_readme_table_carries_all_eight(self):
        section = self.docs["vm/README.md"].split(
            "*Every deviation from an untouched install, and why.*")[1]
        table = section.split("\n\n")[1]
        rows = [ln for ln in table.splitlines() if ln.startswith("| ")]
        numbered = [int(m.group(1)) for ln in rows
                    for m in [re.match(r"\| (\d+) \|", ln)] if m]
        self.assertEqual(numbered, [1, 2, 3, 4, 5, 6, 7, 8])

    def test_the_prose_counts_them_the_same_way(self):
        section = self.docs["vm/README.md"]
        self.assertIn("There are eight, four in the flavor\nyaml and four in the "
                      "stage-2 script", section)


class TheFlavorHeaders(unittest.TestCase):
    """R01.  A flavor yaml is a cloud-config with four to seven `# vmctl-`
    headers in its comments, and those headers are the whole interface between
    the yaml and every script that reads it: which builder owns it, which
    desktop it runs, which distro the host should expect, whether CI builds it
    on every push."""

    @classmethod
    def setUpClass(cls):
        cls.mod = vmctl_module()
        cls.names = sorted(n[:-5] for n in os.listdir(FLAVORS) if n.endswith(".yaml"))
        cls.text = {}
        for n in cls.names:
            with open(os.path.join(FLAVORS, n + ".yaml"), encoding="utf-8") as fh:
                cls.text[n] = fh.read()

    def test_exactly_one_build_kind_per_flavor(self):
        """`vmctl build` (a cloud image), vm/build-iso-golden.sh (the Ubuntu
        installer) and vm/build-nixos-golden.sh (a nix build) are three
        different scripts; the header is how each recognises its own.  A yaml
        with two, or none, is one no builder will touch."""
        for name in self.names:
            with self.subTest(name):
                kinds = [k for k in ("base", "iso", "nix")
                         if re.search(r"^#\s*vmctl-%s:" % k, self.text[name], re.M)]
                self.assertEqual(len(kinds), 1, kinds)

    def test_every_flavor_names_a_desktop_vmctl_knows(self):
        for name in self.names:
            with self.subTest(name):
                found = re.findall(r"^#\s*vmctl-desktop:\s*(\S+)", self.text[name], re.M)
                self.assertEqual(len(found), 1, found)
                self.assertIn(found[0], self.mod.DESKTOPS)

    def test_the_distro_header_follows_the_flavor_name(self):
        """Both readers refuse a value outside their set, so `assertIn(...,
        DISTROS)` could only ever have failed by the call raising.  The claim
        worth pinning is the naming convention every yaml follows: a flavor
        called fedora*/arch-*/nixos-* says that distro in its header, and every
        other one is an Ubuntu image (noble-, resolute-, stonking-).  A header
        that disagrees with the name is the one drift a reader of `vmctl
        flavors` cannot see."""
        for name in self.names:
            with self.subTest(name):
                want = "ubuntu"
                for prefix in ("fedora", "arch", "nixos"):
                    if name.startswith(prefix):
                        want = prefix
                self.assertEqual(self.mod.flavor_distro(name), want)
                self.assertIn(self.mod.flavor_ci(name), ("push", "on-demand"))

    def test_the_ubuntu_cloud_image_flavors_are_built_on_every_push(self):
        """The Ubuntu cloud-image flavors are the ones that build in minutes on
        a runner; promoting one of them to `on-demand` is a deliberate one-word
        edit, not a drift.  The ISO installs and the foreign distros pick their
        own gate (the CI plan reads it)."""
        for name in self.names:
            if self.mod.flavor_distro(name) == "ubuntu" and "vmctl-iso:" not in self.text[name]:
                with self.subTest(name):
                    self.assertEqual(self.mod.flavor_ci(name), "push")

    def flavors_dir(self, tmp):
        """Point the module at `tmp` and put it back afterwards -- through
        addCleanup, because a failing assertion between the two would otherwise
        leave every later test in this class reading a deleted directory and
        failing for a reason that is not its own."""
        self.addCleanup(setattr, self.mod, "FLAVORS", pathlib.Path(FLAVORS))
        self.mod.FLAVORS = pathlib.Path(tmp)
        return self.mod.FLAVORS

    def test_an_unknown_distro_is_refused_and_names_the_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.flavors_dir(tmp)
            (self.mod.FLAVORS / "bogus.yaml").write_text(
                "#cloud-config\n# vmctl-base: x.img\n# vmctl-distro: gentoo\n")
            with self.assertRaises(self.mod.Fail) as e:
                self.mod.flavor_distro("bogus")
            self.assertIn("gentoo", str(e.exception))
            self.assertIn("ubuntu|fedora|arch|nixos", str(e.exception))
            (self.mod.FLAVORS / "sha.yaml").write_text(
                "#cloud-config\n# vmctl-base: x.img\n# vmctl-base-sha256: nothex\n")
            with self.assertRaises(self.mod.Fail):
                self.mod.flavor_base_sha256("sha")
            (self.mod.FLAVORS / "ok.yaml").write_text(
                "#cloud-config\n# vmctl-base: x.img\n# vmctl-base-sha256: %s\n" % ("a1" * 32))
            self.assertEqual(self.mod.flavor_base_sha256("ok"), "a1" * 32)

    def test_a_nix_flavor_sends_vmctl_build_to_its_own_builder(self):
        """The same refusal an ISO flavor gets, for the same reason: NixOS
        publishes no cloud image at all, so there is nothing for `vmctl build`
        to overlay [recon2/nixos]."""
        with tempfile.TemporaryDirectory() as tmp:
            self.flavors_dir(tmp)
            (self.mod.FLAVORS / "nixos-sway.yaml").write_text(
                "#cloud-config\n# vmctl-nix: nixos-sway\n# vmctl-distro: nixos\n")
            with self.assertRaises(self.mod.Fail) as e:
                self.mod.flavor_base("nixos-sway")
            self.assertIn("build-nixos-golden.sh nixos-sway", str(e.exception))

    def test_no_write_files_entry_carries_an_owner_key(self):
        """The measured cloud-init trap: a `write_files` entry with
        `owner: test:test` runs before the user exists and aborts the whole
        module (`OSError: Unknown user or group ... 'test'`, cloud-init status:
        error) [recon2/hyprland].  Write without `owner:`, chown in the build
        script -- which is what build-image.sh does everywhere."""
        for name in self.names:
            with self.subTest(name):
                block = self.text[name].split("write_files:", 1)
                if len(block) == 1:
                    continue
                for line in block[1].splitlines():
                    if re.match(r"^\s*runcmd:|^\s*power_state:", line):
                        break
                    self.assertNotRegex(line, r"^\s*(- )?owner:")


#: A `curl` that writes bytes instead of fetching them, so the Xwayland hook's sha256
#: gate can be RUN and not just read.  `CURL_FAIL_URLS` (space-separated) is the set of
#: URLs it refuses with curl's own exit 22 -- which is how the snapshot.debian.org arm is
#: made to run -- and `CURL_BODY` is what it writes for every other one.
CURL_DOUBLE = """#!/bin/sh
printf "%%s\\n" "curl $*" >> %s
out=; url=
while [ $# -gt 0 ]; do
    case $1 in
        -o) out=$2; shift 2 ;;
        -*) shift ;;
        *)  url=$1; shift ;;
    esac
done
for bad in ${CURL_FAIL_URLS:-}; do
    [ "$url" = "$bad" ] && exit 22
done
if [ -n "${CURL_FILE:-}" ]; then cp "$CURL_FILE" "$out"; else printf '%%s' "${CURL_BODY:-}" > "$out"; fi
exit 0
"""

#: `dpkg -i` is the only thing that moves the version `dpkg-query` answers, here as in the
#: guest: the pair share a state file, so a hook that read the version back before it
#: installed, or never installed at all, fails the gate instead of passing it by accident.
DPKG_DOUBLE = """#!/bin/sh
printf "%%s\\n" "dpkg $*" >> %s
[ "${1:-}" = "-i" ] && printf '%%s' "${FAKE_XW_AFTER:-}" > "$FAKE_XW_STATE"
exit 0
"""

#: `dpkg-query -W -f=${Version} xwayland`, answering what the state file holds and exiting
#: 1 when it holds nothing -- dpkg-query's own answer for a package that is not installed,
#: which is the arm the hook's `|| echo nothing` covers.
DPKG_QUERY_DOUBLE = """#!/bin/sh
printf "%%s\\n" "dpkg-query $*" >> %s
[ -s "$FAKE_XW_STATE" ] || exit 1
cat "$FAKE_XW_STATE"
"""

#: A `sha256sum` that answers `$FAKE_SHA`, used ONLY to reach the install arm without the
#: 992244 bytes themselves; the two mismatch tests run the real sha256sum over real bytes,
#: and W11_XWAYLAND_DEB runs the whole thing over the real .deb when the box has it.
SHA256SUM_DOUBLE = """#!/bin/sh
printf "%%s\\n" "sha256sum $*" >> %s
printf '%%s  %%s\\n' "$FAKE_SHA" "$1"
"""

#: The pinned .deb, its digest and the two URLs, as goal2/recon/gl.md 5 measured them
#: (992244 bytes, both URLs serving the same bytes on 2026-09-12).
XW_DEB = "xwayland_24.1.13-1_amd64.deb"
XW_SHA = "a0633569cf2b65d5d4902a2b6213d59c7db7973faef0233df9ace48f334ba6c2"
XW_URL = "http://deb.debian.org/debian/pool/main/x/xwayland/" + XW_DEB
XW_FALLBACK = ("https://snapshot.debian.org/archive/debian/20260901T000000Z"
               "/pool/main/x/xwayland/" + XW_DEB)


def flavor_write_file(flavor, path):
    """The `content: |` block one flavor yaml writes to `path`, dedented.

    Read out of the yaml with no yaml parser, the way every other reader in this
    tree does it: cloud-init's `write_files` entries here are all `- path:` /
    `permissions:` / `content: |` with a six-space body, and the block ends at the
    first line that is neither blank nor indented that far."""
    with open(os.path.join(FLAVORS, flavor + ".yaml"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "- path: " + path:
            continue
        for j in range(i + 1, len(lines)):
            if lines[j].strip() == "content: |":
                body = []
                for k in range(j + 1, len(lines)):
                    if lines[k] and not lines[k].startswith("      "):
                        break
                    body.append(lines[k][6:])
                return "\n".join(body) + "\n"
    raise AssertionError("%s.yaml writes no %s" % (flavor, path))


class TheXwaylandHold(unittest.TestCase):
    """R01b.  resolute-cinnamon-wayland's Xwayland hold: AGENTS.md route 5 at its
    cheapest end, a released fix installed into one golden.

    Ubuntu 26.04 ships xwayland `2:24.1.10-1`, the one release carrying the
    `damage_report()` NULL dereference that kills muffin's Xwayland (guest objdump,
    offset 0x5be4a; fixed upstream in 24.1.11) [goal2/recon/gl.md 3].  The flavor
    yaml writes /usr/local/sbin/vmctl-build-hook and vm/build-image.sh's `build_hook`
    runs it after the desktop install, so the fix lands in the golden and a failed
    download stops the BUILD instead of caching an image whose session cannot hold a
    compositor (vm/vmctl:779 decides a build on VMCTL-BUILD-OK alone, which is
    printed after this).

    The two run tests below execute the shipped hook with a `curl` that writes
    bytes: the digest gate is real sha256 over those bytes, so the mismatch arm is
    measured and not read."""

    HOOK = "/usr/local/sbin/vmctl-build-hook"

    @classmethod
    def setUpClass(cls):
        cls.hook = flavor_write_file("resolute-cinnamon-wayland", cls.HOOK)
        with open(BUILD, encoding="utf-8") as fh:
            cls.build = fh.read()

    def run_hook(self, body="not the 992244 bytes", fail_urls=(), sha=None, deb=None,
                 installed="2:24.1.10-1", after="2:24.1.13-1", timeout=60):
        """The shipped hook, with curl/dpkg/dpkg-query stubbed and sha256sum real.

        `deb` is a real file for curl to serve, `sha` replaces sha256sum with one
        answering that digest -- the two ways to reach the arm past the gate, the
        first with the real 992244 bytes and the second without them.  `installed`
        is what dpkg-query answers before the install and `after` what the dpkg
        double makes it answer once `dpkg -i` has run."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "log")
        open(log, "w").close()
        state = os.path.join(tmp, "xwayland-version")
        with open(state, "w") as fh:
            fh.write(installed)
        d = stubs(tmp, [], log)
        doubles = {"curl": CURL_DOUBLE, "dpkg": DPKG_DOUBLE, "dpkg-query": DPKG_QUERY_DOUBLE}
        if sha is not None:
            doubles["sha256sum"] = SHA256SUM_DOUBLE
        for name, text in doubles.items():
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text % log)
            os.chmod(os.path.join(d, name), 0o755)
        script = os.path.join(tmp, "hook")
        with open(script, "w") as fh:
            fh.write(self.hook)
        os.chmod(script, 0o755)
        env = dict(os.environ, PATH=d + ":" + os.environ["PATH"], FAKE_LOG=log,
                   CURL_BODY=body, CURL_FILE=deb or "", CURL_FAIL_URLS=" ".join(fail_urls),
                   FAKE_SHA=sha or "", FAKE_XW_STATE=state, FAKE_XW_AFTER=after)
        got = subprocess.run([script], capture_output=True, text=True,
                             timeout=timeout, env=env)
        with open(log) as fh:
            return got, [ln for ln in fh.read().splitlines() if ln]

    def test_the_hook_installs_the_pinned_deb_and_nothing_else(self):
        """The name and the digest are the pin; `dpkg -i` takes the file the
        download left, never a URL and never an apt package."""
        self.assertIn(XW_DEB, self.hook)
        self.assertIn(XW_SHA, self.hook)
        # written with `$deb` in them, so the pin is the name, once
        self.assertIn(XW_URL[:-len(XW_DEB)] + "$deb", self.hook)
        self.assertIn(XW_FALLBACK[:-len(XW_DEB)] + "$deb", self.hook)
        self.assertIn('dpkg -i "$tmp/$deb"', self.hook)
        self.assertNotIn("apt-get install xwayland", self.hook)

    def test_bytes_that_are_not_the_pinned_deb_stop_the_build_before_dpkg(self):
        """The gate that makes a pinned URL safe.  A mirror serving something
        else -- sid moving past 24.1.13-1, a captive portal's HTML -- must not
        reach `dpkg -i`, and must say both digests so the next reader knows
        which one moved."""
        got, log = self.run_hook(body="HTTP 403, not a .deb")
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        self.assertIn("sha256 mismatch", got.stdout)
        self.assertIn(XW_SHA, got.stdout)
        self.assertEqual([ln for ln in log if ln.startswith("dpkg ")], [])

    def test_the_snapshot_url_is_tried_when_the_debian_pool_answers_nothing(self):
        """sid moves past 24.1.13-1 in its own time and the pool drops the file
        when it does; snapshot.debian.org's 20260901T000000Z keeps serving those
        992244 bytes, and the digest below is what makes a second URL safe rather
        than a second version.  Both were measured answering on 2026-09-12."""
        got, log = self.run_hook(fail_urls=[XW_URL])
        curls = [ln for ln in log if ln.startswith("curl ")]
        self.assertEqual(len(curls), 2, curls)
        self.assertIn(XW_URL, curls[0])
        self.assertIn(XW_FALLBACK, curls[1])
        # and the first URL has to be able to GIVE UP: snapshot.debian.org rate-limits,
        # and a stalled connection with no cap holds the build until vmctl's own build
        # timeout kills it instead of falling through to the line below
        for curl in curls:
            self.assertIn("--max-time", curl)
            self.assertIn("--connect-timeout", curl)
        # it got bytes from the fallback and then stopped on the digest, which is
        # the proof the second download was really read and not merely attempted
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        self.assertIn("sha256 mismatch", got.stdout)

    def test_a_matching_digest_installs_the_deb_and_then_reads_the_version_back(self):
        """The arm the golden takes, run end to end.  What it pins is what happens
        AFTER the gate, which no mismatch test can reach: `dpkg -i` on the file the
        download left (not a URL, not an apt name), the version read back from dpkg
        AFTERWARDS, and 2:24.1.13-1 spelled the way Debian spells it -- an epoch
        dropped here is a hook that fails every build on the version it has just
        installed.  The digest is the stub's in this one; the two tests above run
        the real sha256sum over real bytes, and the one below over the real .deb."""
        got, log = self.run_hook(sha=XW_SHA)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertNotIn("sha256 mismatch", got.stdout)
        self.assertIn("installing " + XW_DEB + " over 2:24.1.10-1", got.stdout)
        installs = [ln for ln in log if ln.startswith("dpkg -i")]
        self.assertEqual(len(installs), 1, log)
        self.assertTrue(installs[0].endswith("/" + XW_DEB), installs)
        # the version the gate reads is the one dpkg answers AFTER that install
        self.assertTrue(log[log.index(installs[0]) + 1].startswith("dpkg-query "), log)
        self.assertIn("xwayland 2:24.1.13-1", got.stdout)

    def test_an_install_that_left_the_old_xwayland_stops_the_build(self):
        """`dpkg -i` exits 0 on a package that is held, diverted or already newer,
        and the golden would then be cached carrying the 2:24.1.10-1 whose
        damage_report() NULL kills muffin's Xwayland (goal2/recon/gl.md 3) -- with
        nothing in the build log saying so.  The gate is what turns that into a
        failed build, and it names both versions so the next reader knows which."""
        got, log = self.run_hook(sha=XW_SHA, after="2:24.1.10-1")
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        self.assertIn("xwayland is 2:24.1.10-1 after the install, not 2:24.1.13-1", got.stdout)
        self.assertTrue([ln for ln in log if ln.startswith("dpkg -i")], log)

    @unittest.skipUnless(os.path.exists(os.environ.get("W11_XWAYLAND_DEB", "")),
                         "W11_XWAYLAND_DEB is not a file on this box")
    def test_the_real_deb_passes_the_gate_this_hook_puts_in_front_of_it(self):
        """The same install arm with nothing stubbed but the network: the real
        sha256sum over the real 992244 bytes, which is the one thing the two
        mismatch tests cannot show -- that the digest written into the yaml is the
        digest of the file those URLs serve.  Opt-in because the bytes are 992 KB
        and belong in no fixture: point W11_XWAYLAND_DEB at a copy (goal2/gl/ has
        one; `curl -fLO` off the pool URL above fetched them in 5.0 s on this box,
        2026-09-12)."""
        got, log = self.run_hook(deb=os.environ["W11_XWAYLAND_DEB"])
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertNotIn("sha256 mismatch", got.stdout)
        self.assertEqual(len([ln for ln in log if ln.startswith("dpkg -i")]), 1, log)

    def test_only_this_flavor_carries_a_build_hook_and_it_is_an_ubuntu_one(self):
        """Per-flavor is the whole design: a Debian .deb belongs on no Arch,
        Fedora or NixOS image, and a hook in one yaml moves only that golden's
        cache key (scripts/ci-golden.sh hashes the yaml).  The shared build
        script names no flavor and no package."""
        hooked = []
        for name in sorted(os.listdir(FLAVORS)):
            if not name.endswith(".yaml"):
                continue
            with open(os.path.join(FLAVORS, name), encoding="utf-8") as fh:
                text = fh.read()
            if self.HOOK in text:
                hooked.append(name[:-5])
            else:
                self.assertNotIn(XW_DEB, text, name)
                self.assertNotIn(XW_SHA, text, name)
        self.assertEqual(hooked, ["resolute-cinnamon-wayland"])
        self.assertEqual(vmctl_module().flavor_distro(hooked[0]), "ubuntu")
        # the shared script may EXPLAIN the hook in a comment -- what it must not do is
        # act on it, so the tokens are looked for in what bash will run
        code = "\n".join(ln for ln in self.build.splitlines() if not ln.lstrip().startswith("#"))
        for token in (XW_DEB, XW_SHA, "deb.debian.org", "resolute-cinnamon-wayland", "dpkg -i"):
            self.assertNotIn(token, code, token)

    def test_a_hook_that_fails_takes_the_build_down_with_it(self):
        """The reason the hook is a seam in build-image.sh and not a second
        `runcmd:` entry: `vm/vmctl` decides a build on the VMCTL-BUILD-OK marker
        alone, so a runcmd failing after that marker would cache a broken golden
        as a good one.  Here the failure goes through fail(), which the PREAMBLE
        makes exit 9."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "log")
        open(log, "w").close()
        d = stubs(tmp, ["getent"], log)
        hook = os.path.join(tmp, "usr", "local", "sbin", "vmctl-build-hook")
        os.makedirs(os.path.dirname(hook))
        env = dict(os.environ, PATH=d + ":" + os.environ["PATH"], FAKE_LOG=log,
                   VMCTL_ROOT=tmp, NET_HOST="example.invalid", NET_FIX="true")
        run = lambda: subprocess.run(                                   # noqa: E731
            ["bash", "-c", PREAMBLE + sh_layer("wait_net", "build_hook") + "\nbuild_hook"],
            capture_output=True, text=True, timeout=60, env=env)
        # no hook: the build carries on, and nothing is said about a flavor arm
        got = run()
        self.assertEqual(got.returncode, 0, got.stderr)
        with open(log) as fh:
            self.assertNotIn("flavor hook", fh.read())
        for rc, want_exit in ((0, 0), (3, 9)):
            with self.subTest(rc=rc):
                with open(hook, "w") as fh:
                    fh.write('#!/bin/sh\necho "hook ran" >> %s\nexit %d\n' % (log, rc))
                os.chmod(hook, 0o755)
                open(log, "w").close()
                got = run()
                self.assertEqual(got.returncode, want_exit, got.stderr)
                with open(log) as fh:
                    text = fh.read()
                self.assertIn("hook ran", text)
                self.assertEqual(
                    "fail: the flavor hook /usr/local/sbin/vmctl-build-hook failed" in text,
                    rc != 0)

    def test_the_hook_runs_after_the_desktop_and_before_the_golden_markers(self):
        """Order is the whole safety of it: the .deb goes over a package the
        desktop install put there, and the build's OK marker is printed after the
        hook has had its say."""
        call = self.build.index("\nbuild_hook\n")
        self.assertLess(self.build.index("\npkg_desktop $DESKTOP_PKG\n"), call)
        self.assertLess(self.build.index("\n$DESK\n"), call)
        self.assertLess(call, self.build.index("con VMCTL-BUILD-OK"))
        self.assertLess(call, self.build.index("VMCTL-PACKAGES-BEGIN"))


class ThePackageLayer(unittest.TestCase):
    """R02.  `pkg_*` sliced out of build-image.sh and run with stub package
    managers, once per manager.  These are the lines that reach the network on a
    machine nobody is watching; the retry loop around them is the reason a
    golden build survives the networkd/NetworkManager hand-over, and the exact
    argv is what makes the difference between an unattended install and a
    prompt nobody answers."""

    LAYER = ("detect_pkg", "wait_net", "pkg_retry", "pkg_update", "pkg_install",
             "pkg_desktop", "pkg_seed_dm", "pkg_manifest")

    def run_layer(self, pkg, script, extra_stubs=(), timeout=60):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "log")
        open(log, "w").close()
        names = ["apt-get", "dnf", "pacman", "sleep", "getent", "netplan",
                 "+debconf-set-selections", "dpkg-query", "rpm", "sort",
                 "systemctl", "snap"] + list(extra_stubs)
        d = stubs(tmp, names, log)
        env = dict(os.environ, PATH=d + ":" + os.environ["PATH"], FAKE_LOG=log,
                   WRITTEN=os.path.join(tmp, "written"), PKG=pkg,
                   APT="apt-get -y -q -o Dpkg::Options::=--force-confdef "
                       "-o Dpkg::Options::=--force-confold",
                   NET_HOST="example.invalid", NET_FIX="true")
        got = subprocess.run(["bash", "-c", PREAMBLE + sh_layer(*self.LAYER) + "\n" + script],
                             capture_output=True, text=True, timeout=timeout, env=env)
        with open(log) as fh:
            return got, [ln for ln in fh.read().splitlines() if ln]

    def test_apt_install_is_todays_command_line(self):
        """The five `-o` options are not decoration: --force-confold is what
        keeps dpkg from stopping on a conffile prompt in a VM with no tty."""
        _, log = self.run_layer("apt", 'pkg_install foo bar')
        self.assertIn("apt-get -y -q -o Dpkg::Options::=--force-confdef "
                      "-o Dpkg::Options::=--force-confold install foo bar", log)

    def test_dnf_and_pacman_install_lines(self):
        _, log = self.run_layer("dnf", 'pkg_install foo')
        self.assertIn("dnf -y install foo", log)
        _, log = self.run_layer("pacman", 'pkg_install foo')
        self.assertIn("pacman -S --noconfirm --needed foo", log)

    def test_pacman_syncs_the_keyring_before_anything_else(self):
        """A cloud image weeks old carries keys older than the packages it is
        about to fetch, so the keyring is its own `-Sy` and it is first
        [recon2/arch]."""
        _, log = self.run_layer("pacman", 'pkg_update\npkg_install foo')
        self.assertEqual(log[0], "pacman -Sy --noconfirm --needed archlinux-keyring")
        self.assertTrue(log.index("pacman -Sy --noconfirm --needed archlinux-keyring")
                        < log.index("pacman -S --noconfirm --needed foo"))

    def test_a_failing_install_is_retried_five_times(self):
        """Five attempts, 15 s apart, each preceded by the network check and
        followed by a metadata refresh: the failure this loop was written for is
        an apt-get that ran while NetworkManager was taking the NIC over."""
        got, log = self.run_layer("apt", 'pkg_install foo || echo GAVEUP',
                                  extra_stubs=["apt-get=1"])
        self.assertEqual(len([ln for ln in log if ln.startswith("apt-get") and "install" in ln]), 5)
        self.assertEqual(len([ln for ln in log if ln == "sleep 15"]), 5)
        self.assertIn("GAVEUP", got.stdout)

    def test_wait_net_asks_getent_for_the_host_it_was_given(self):
        """The network can vanish mid-build (the networkd/NetworkManager
        hand-over left a 24.04 guest with no IP at all), so every package
        command is preceded by a name lookup of the archive it is about to
        talk to."""
        _, log = self.run_layer("apt", 'NET_HOST=archive.ubuntu.com; wait_net && echo UP')
        self.assertEqual(log, ["getent hosts archive.ubuntu.com"])

    def test_each_manager_waits_for_its_own_archive(self):
        """detect_pkg reads THIS machine's /etc/os-release, so the mapping is
        asserted where it is written: running it would only ever measure the
        distro the tests happen to run on."""
        table = dict((pkg, host) for _id, pkg, host in re.findall(
            r'\*" (\w+) "\*[^)]*\)\s+PKG=(\w+);\s+NET_HOST=(\S+);',
            sh_layer("detect_pkg")))
        self.assertEqual(table, {"apt": "archive.ubuntu.com",
                                 "dnf": "mirrors.fedoraproject.org",
                                 "pacman": "geo.mirror.pkgbuild.com"})

    def test_wait_net_gives_up_and_says_so(self):
        """90 tries, two seconds apart, is a minute and a half; the caller
        (`pkg_retry`) carries on regardless and says `does not resolve`, because
        a broken resolver is not always a broken network."""
        got, log = self.run_layer("apt", 'wait_net || echo DOWN', extra_stubs=["getent=2"])
        self.assertIn("DOWN", got.stdout)
        self.assertEqual(len([ln for ln in log if ln.startswith("getent")]), 90)
        self.assertEqual(len([ln for ln in log if ln == "say: network is down; true"]), 2)

    def test_dnf_installs_an_environment_as_a_group(self):
        """Fedora's desktops are comps environments, not packages:
        `workstation-product-environment`, `kde-desktop-environment`,
        `sway-desktop-environment` [recon2/pkg-rpm, recon2/fedora].  A word
        ending in -environment goes to `dnf group install`, the rest to
        `dnf install`, in that order."""
        _, log = self.run_layer("dnf", 'pkg_desktop workstation-product-environment cosmic-randr foot')
        self.assertIn("dnf -y group install workstation-product-environment", log)
        self.assertIn("dnf -y install cosmic-randr foot", log)
        self.assertLess(log.index("dnf -y group install workstation-product-environment"),
                        log.index("dnf -y install cosmic-randr foot"))

    def test_apt_does_not_invent_a_group_command(self):
        """The same DESKTOP_PKG on Ubuntu is just packages: `-environment` is a
        Fedora comps suffix and means nothing to apt."""
        _, log = self.run_layer("apt", 'pkg_desktop ubuntu-desktop')
        self.assertTrue(any("install ubuntu-desktop" in ln for ln in log))
        self.assertFalse([ln for ln in log if "group install" in ln])

    def test_the_manifest_is_names_only_per_manager(self):
        """`vmctl build` parses this list off the serial console and refuses a
        build whose count does not match, so its shape is a contract: names, one
        per line, sorted, with no versions.  `rpm -qa` and `pacman -Q` both
        print versions by default, which is what the two format flags are
        for."""
        for pkg, want in (("apt", "dpkg-query -W -f=${binary:Package}\\n"),
                          ("dnf", "rpm -qa --qf %{NAME}\\n"),
                          ("pacman", "pacman -Qq")):
            with self.subTest(pkg):
                _, log = self.run_layer(pkg, 'pkg_manifest >/dev/null')
                self.assertEqual([ln for ln in log if not ln.startswith("sort")], [want])

    def test_the_display_manager_question_is_answered_before_the_install(self):
        """debconf is apt's alone, and the first display manager to configure
        itself becomes THE display manager -- which is how a flavor that asked
        for LightDM ends up on gdm3 because the desktop metapackage pulled
        gnome-shell.  The ANSWER is the point, not the call: the three arms are
        indistinguishable unless the line piped in is read back."""
        for dm, want in (("dm_lightdm", "lightdm"), ("dm_sddm", "sddm"),
                         ("dm_plasma", "sddm"), ("dm_gdm", "gdm3")):
            with self.subTest(dm):
                _, log = self.run_layer("apt", 'pkg_seed_dm %s' % dm)
                self.assertIn("debconf-set-selections ", log[0])
                self.assertIn("stdin: %s shared/default-x-display-manager select %s"
                              % (want, want), log)
        _, log = self.run_layer("dnf", 'pkg_seed_dm dm_lightdm; echo done')
        self.assertEqual(log, [])


class TheDisplayManagers(unittest.TestCase):
    """R03.  Each `dm_*` sliced and run into a temp tree through the `VMCTL_ROOT`
    prefix seam.  Autologin is the one thing every flavor depends on and the one
    thing that cannot be checked without booting -- unless the files it writes
    are checked here."""

    LAYER = ("written", "wcat", "wdir", "accountsservice", "no_session_ambiguity", "dm_enable",
             "dm_gdm", "dm_sddm", "dm_plasmalogin", "dm_plasma", "dm_greetd", "dm_lightdm")
    DM_UNITS = ("gdm3", "gdm", "sddm", "plasmalogin", "lightdm", "greetd")

    def run_dm(self, script, tree=(), links=(), exe=(), units=DM_UNITS):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "root")
        for rel in tuple(tree) + tuple(exe):
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path) if not rel.endswith("/") else path, exist_ok=True)
            if not rel.endswith("/"):
                open(path, "w").close()
        for rel in exe:   # dm_greetd tests -x, not -f
            os.chmod(os.path.join(root, rel), 0o755)
        for src, dst in links:
            os.makedirs(os.path.dirname(os.path.join(root, src)), exist_ok=True)
            os.symlink(os.path.join(root, dst), os.path.join(root, src))
        for u in units:   # the unit files the display-manager packages installed
            path = os.path.join(root, "usr/lib/systemd/system", u + ".service")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(UNIT_FILES[u])
        log = os.path.join(tmp, "log")
        open(log, "w").close()
        d = stubs(tmp, ["systemctl", "usermod", "getent=2"], log)
        with open(os.path.join(d, "systemctl"), "w") as fh:
            fh.write(SYSTEMCTL_DOUBLE % log)
        env = dict(os.environ, PATH=d + ":" + os.environ["PATH"], FAKE_LOG=log,
                   WRITTEN=os.path.join(tmp, "written"), VMCTL_ROOT=root, PKG="apt")
        body = (PREAMBLE.replace('written() {', 'unused_written() {')
                + sh_layer(*self.LAYER) + "\n" + script)
        got = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                             timeout=60, env=env)
        return got, root, log

    def read(self, root, rel):
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()

    WL = "usr/share/wayland-sessions/"
    XS = "usr/share/xsessions/"

    def test_gdm_writes_gdm3_on_ubuntu_and_gdm_on_fedora(self):
        """The directory the package created is the fact; the distro name is
        not.  Ubuntu's gdm3 owns /etc/gdm3, Fedora's and Arch's gdm owns
        /etc/gdm [recon2/fedora, recon2/arch, recon2/gnome-xorg]."""
        got, root, _ = self.run_dm("dm_gdm wayland",
                                   tree=["etc/gdm3/", self.WL + "ubuntu.desktop"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("AutomaticLogin=test", self.read(root, "etc/gdm3/custom.conf"))
        got, root, _ = self.run_dm("dm_gdm wayland",
                                   tree=["etc/gdm/", self.WL + "gnome.desktop"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("AutomaticLogin=test", self.read(root, "etc/gdm/custom.conf"))
        self.assertFalse(os.path.exists(os.path.join(root, "etc/gdm3/custom.conf")))

    def test_gdm_takes_the_session_name_off_the_disk_and_not_the_flavor(self):
        """Ubuntu's gnome-session ships ubuntu.desktop / ubuntu-xorg.desktop;
        Fedora 44's /usr/share/wayland-sessions holds exactly gnome.desktop,
        cosmic.desktop and sway.desktop and its measured autologin row says
        `Session=gnome` [recon2/fedora], and Arch's gnome-session ships
        gnome.desktop too.  A hard-coded `ubuntu` would write an AccountsService
        row naming a session that does not exist on two of the three distros and
        lean on GDM's silent fallback -- so the name is resolved the way
        dm_plasma resolves its four Plasma names: off the image being built."""
        for tree, want in ((["etc/gdm3/", self.WL + "ubuntu.desktop",
                             self.WL + "gnome.desktop"], "ubuntu"),
                           (["etc/gdm/", self.WL + "gnome.desktop"], "gnome")):
            with self.subTest(want):
                got, root, _ = self.run_dm("dm_gdm wayland", tree=tree)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertIn("Session=%s\n" % want,
                              self.read(root, "var/lib/AccountsService/users/test"))
        for tree, want in ((["etc/gdm3/", self.XS + "ubuntu-xorg.desktop"], "ubuntu-xorg"),
                           (["etc/gdm/", self.XS + "gnome-xorg.desktop"], "gnome-xorg")):
            with self.subTest(want):
                got, root, _ = self.run_dm("dm_gdm x11", tree=tree)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertIn("XSession=%s\n" % want,
                              self.read(root, "var/lib/AccountsService/users/test"))

    def test_gdm_refuses_and_lists_the_directory_when_no_session_is_there(self):
        """A GNOME image with no GNOME session file is a build that would
        otherwise finish and boot to a login loop forty minutes later; the
        listing is what says which name it should have been looking for."""
        got, _, log = self.run_dm("dm_gdm wayland",
                                  tree=["etc/gdm3/", self.WL + "cosmic.desktop"])
        self.assertEqual(got.returncode, 9)
        with open(log, encoding="utf-8") as fh:
            self.assertIn("cosmic.desktop", fh.read())

    def test_wayland_enable_is_false_only_for_the_xorg_session(self):
        """gdm3 46.2 reads daemon/WaylandEnable and 50.1 no longer does (both
        read out of the debs [recon2/gnome-xorg]); false is the only way to
        reach an Xorg session on the release that honours it, and true keeps GDM
        off Xorg on the release that does not."""
        _, root, _ = self.run_dm("dm_gdm wayland",
                                  tree=["etc/gdm3/", self.WL + "ubuntu.desktop"])
        self.assertIn("WaylandEnable=true", self.read(root, "etc/gdm3/custom.conf"))
        _, root, _ = self.run_dm("dm_gdm x11",
                                 tree=["etc/gdm3/", self.XS + "ubuntu-xorg.desktop"])
        self.assertIn("WaylandEnable=false", self.read(root, "etc/gdm3/custom.conf"))
        self.assertIn("XSession=ubuntu-xorg",
                      self.read(root, "var/lib/AccountsService/users/test"))

    def test_gdm_on_x11_refuses_when_the_session_file_is_missing(self):
        got, _, _ = self.run_dm("dm_gdm x11", tree=["etc/gdm3/"])
        self.assertEqual(got.returncode, 9)

    def test_sddm_writes_its_autologin_file(self):
        got, root, _ = self.run_dm("dm_sddm plasma.desktop",
                                   tree=["usr/share/wayland-sessions/plasma.desktop"])
        self.assertEqual(got.returncode, 0, got.stderr)
        conf = self.read(root, "etc/sddm.conf.d/autologin.conf")
        self.assertIn("User=test", conf)
        self.assertIn("Session=plasma.desktop", conf)
        self.assertIn("Relogin=false", conf)
        self.assertIn("Session=plasma", self.read(root, "var/lib/AccountsService/users/test"))

    def test_a_session_name_in_both_directories_is_refused(self):
        """SDDM resolves the autologin session NAME against
        /usr/share/wayland-sessions first (Display::attemptAutologin), so a name
        in both directories would quietly start the other session type -- the
        one thing a kde-x11 flavor must never do.  ubuntu.desktop really is in
        both on noble [recon2/gnome-xorg]."""
        got, _, _ = self.run_dm("dm_sddm plasma.desktop",
                                tree=["usr/share/wayland-sessions/plasma.desktop",
                                      "usr/share/xsessions/plasma.desktop"])
        self.assertEqual(got.returncode, 9)

    def test_plasmalogin_writes_the_three_keys_the_template_has(self):
        """Fedora 44 replaced SDDM with plasma-login-manager in every KDE
        variant; the keys are the rpm's own template's [recon2/fedora]."""
        got, root, _ = self.run_dm("dm_plasmalogin plasma.desktop",
                                   tree=["usr/share/wayland-sessions/plasma.desktop"])
        self.assertEqual(got.returncode, 0, got.stderr)
        conf = self.read(root, "etc/plasmalogin.conf.d/autologin.conf")
        self.assertIn("[Autologin]", conf)
        self.assertIn("User=test", conf)
        self.assertIn("Session=plasma.desktop", conf)
        self.assertIn("Relogin=false", conf)

    def test_the_plasma_arm_picks_the_display_manager_that_is_installed(self):
        """One DESKTOPS row, two display managers: SDDM on Ubuntu and Arch,
        plasma-login-manager on Fedora 44, whose kde-desktop comps group carries
        it and no sddm at all [recon2/fedora].  Which one is decided by what the
        packages put on disk, never by the flavor's distro header."""
        _, root, _ = self.run_dm("dm_plasma wayland",
                                 tree=["usr/share/wayland-sessions/plasma.desktop"])
        self.assertTrue(os.path.exists(os.path.join(root, "etc/sddm.conf.d/autologin.conf")))
        self.assertFalse(os.path.exists(os.path.join(root, "etc/plasmalogin.conf.d/autologin.conf")))
        _, root, _ = self.run_dm("dm_plasma wayland",
                                 tree=["usr/share/wayland-sessions/plasma.desktop",
                                       "etc/plasmalogin.conf.d/"])
        self.assertTrue(os.path.exists(os.path.join(root, "etc/plasmalogin.conf.d/autologin.conf")))
        self.assertFalse(os.path.exists(os.path.join(root, "etc/sddm.conf.d/autologin.conf")))

    def test_the_plasma_arm_finds_five_twenty_seven_and_six_session_files(self):
        """Plasma 5.27's Wayland session is plasmawayland.desktop and Plasma
        6's is plasma.desktop; the X11 pair is plasma.desktop and
        plasmax11.desktop.  Four names, one arm."""
        _, root, _ = self.run_dm("dm_plasma wayland",
                                 tree=["usr/share/wayland-sessions/plasmawayland.desktop"])
        self.assertIn("Session=plasmawayland.desktop",
                      self.read(root, "etc/sddm.conf.d/autologin.conf"))
        _, root, _ = self.run_dm("dm_plasma x11",
                                 tree=["usr/share/xsessions/plasmax11.desktop"])
        self.assertIn("Session=plasmax11.desktop",
                      self.read(root, "etc/sddm.conf.d/autologin.conf"))
        got, _, _ = self.run_dm("dm_plasma x11", tree=["usr/share/xsessions/"])
        self.assertEqual(got.returncode, 9)

    def test_greetd_refuses_when_greetd_is_not_installed(self):
        """The flavor names greetd in DESKTOP_PKG; if the install silently did
        not happen, the image would boot to a text console and the failure
        would be read forty minutes later, off a serial log."""
        got, _, _ = self.run_dm('dm_greetd sway')
        self.assertEqual(got.returncode, 9)
        got, _, _ = self.run_dm('dm_greetd sway', tree=["usr/sbin/greetd"])
        self.assertEqual(got.returncode, 9, "a greetd that is not executable is not greetd")

    def test_greetd_writes_the_toml_and_the_getty_drop_in(self):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "root")
        os.makedirs(os.path.join(root, "usr/sbin"))
        with open(os.path.join(root, "usr/sbin/greetd"), "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(os.path.join(root, "usr/sbin/greetd"), 0o755)
        log = os.path.join(tmp, "log")
        open(log, "w").close()
        d = stubs(tmp, ["systemctl"], log)
        # dm_greetd ends in dm_enable like every arm, so the double and greetd's unit
        with open(os.path.join(d, "systemctl"), "w") as fh:
            fh.write(SYSTEMCTL_DOUBLE % log)
        os.makedirs(os.path.join(root, "usr/lib/systemd/system"), exist_ok=True)
        with open(os.path.join(root, "usr/lib/systemd/system/greetd.service"), "w") as fh:
            fh.write(UNIT_FILES["greetd"])
        body = (PREAMBLE.replace('written() {', 'unused_written() {')
                + sh_layer(*self.LAYER) + '\ndm_greetd "labwc"\n')
        got = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, PATH=d + ":" + os.environ["PATH"],
                                      FAKE_LOG=log, WRITTEN=os.path.join(tmp, "w"),
                                      VMCTL_ROOT=root, PKG="apt"))
        self.assertEqual(got.returncode, 0, got.stderr)
        toml = self.read(root, "etc/greetd/config.toml")
        self.assertEqual(toml.count('command = "labwc"'), 2)
        self.assertIn("vt = 1", toml)
        drop = self.read(root, "etc/systemd/system/greetd.service.d/vmctl-vt1.conf")
        self.assertIn("Conflicts=getty@tty1.service", drop)
        with open(log) as fh:
            lines = fh.read().splitlines()
        self.assertIn("systemctl disable getty@tty1.service", lines)
        self.assertIn("systemctl enable greetd", lines)
        self.assertEqual(os.path.basename(os.path.realpath(
            os.path.join(root, "etc/systemd/system/display-manager.service"))), "greetd.service")

    def test_greetd_takes_the_whole_multi_word_command_of_the_dispatch_table(self):
        """The one multi-word command in select_desktop is river's, and it is
        the one that was broken: `$DM $DM_ARGS` at the bottom of build-image.sh
        is UNQUOTED (dm_gdm needs the split -- its argument is a session kind),
        so `local cmd=$1` in dm_greetd saw `river` alone and wrote
        `command = "river"` into both stanzas.  river then starts with no `-c`:
        no tinyrwm, no dbus-update-activation-environment, no layout helper --
        the session the file's own comment says must not happen.  Driven
        through the shipped dispatch line rather than a hand-typed command, so
        that a table edit is covered too."""
        got, root, _ = self.run_dm(
            support.sh_function(BUILD, "select_desktop")
            + "\nDESKTOP=river\nselect_desktop\n$DM $DM_ARGS\n",
            exe=["usr/sbin/greetd"])
        self.assertEqual(got.returncode, 0, got.stderr)
        toml = self.read(root, "etc/greetd/config.toml")
        self.assertEqual(toml.count('command = "river -c /usr/local/bin/vmctl-river-init"'), 2,
                         toml)

    def test_lightdm_writes_the_session_name_it_was_given(self):
        """LightDM 1.32's sessions-directory already includes
        /usr/share/wayland-sessions (read out of the shipped binary
        [recon2/cinnamon, recon2/xfce-wayland]), so `autologin-session=
        cinnamon-wayland` needs no extra key -- and AccountsService's XSession=
        is meaningless for a Wayland session, so it is left out there."""
        got, root, _ = self.run_dm("dm_lightdm cinnamon-wayland",
                                   tree=["usr/share/wayland-sessions/cinnamon-wayland.desktop"],
                                   links=[("etc/systemd/system/display-manager.service",
                                           "usr/lib/systemd/system/lightdm.service")])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("autologin-session=cinnamon-wayland",
                      self.read(root, "etc/lightdm/lightdm.conf.d/50-autologin.conf"))
        user = self.read(root, "var/lib/AccountsService/users/test")
        self.assertIn("Session=cinnamon-wayland", user)
        self.assertNotIn("XSession=", user)

    def test_lightdm_takes_display_manager_service_back_from_gdm3(self):
        """The debconf race: gdm3 configured itself first and
        display-manager.service points at it, which is a golden image that
        boots into the wrong desktop.  dm_enable drops the other DMs' alias
        before enabling ours, so the race is undone rather than merely caught."""
        got, root, _ = self.run_dm("dm_lightdm xubuntu",
                                   tree=["usr/share/xsessions/xubuntu.desktop"],
                                   links=[("etc/systemd/system/display-manager.service",
                                           "usr/lib/systemd/system/gdm3.service")])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.alias(root), "lightdm.service")

    def test_lightdm_refuses_when_display_manager_service_is_not_lightdm(self):
        """No lightdm.service on the disk: systemctl enable fails, the alias
        never appears, and the build stops rather than shipping an image whose
        display manager is whatever won."""
        got, _, log = self.run_dm("dm_lightdm xubuntu",
                                  tree=["usr/share/xsessions/xubuntu.desktop"], units=())
        self.assertEqual(got.returncode, 9)
        with open(log) as fh:
            self.assertRegex(fh.read(), r"fail: (systemctl enable lightdm failed|no unit file for lightdm)")

    def alias(self, root):
        return os.path.basename(os.readlink(os.path.join(root, "etc/systemd/system/display-manager.service")))

    def test_every_display_manager_arm_enables_its_unit(self):
        """The regression of 2026-09-08: the rewrite into layers dropped the
        step the old script never needed (gdm3's postinst used to enable
        itself), and every GNOME flavor booted to a text console with gdm3
        installed, configured and disabled.  Each arm now ends in dm_enable,
        and the alias is checked on the tree it wrote into -- gdm3 on an Ubuntu
        tree (/etc/gdm3), gdm on a Fedora or Arch one (/etc/gdm)."""
        cases = (("dm_gdm wayland", ["etc/gdm3/", self.WL + "ubuntu.desktop"], "gdm3"),
                 ("dm_gdm wayland", ["etc/gdm/", self.WL + "gnome.desktop"], "gdm"),
                 ("dm_gdm x11", ["etc/gdm3/", self.XS + "ubuntu-xorg.desktop"], "gdm3"),
                 ("dm_sddm plasma.desktop", [self.WL + "plasma.desktop"], "sddm"),
                 ("dm_plasmalogin plasma.desktop", [self.WL + "plasma.desktop"], "plasmalogin"),
                 ("dm_lightdm xubuntu", [self.XS + "xubuntu.desktop"], "lightdm"))
        for script, tree, unit in cases:
            with self.subTest(script=script, unit=unit):
                got, root, log = self.run_dm(script, tree=tree)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual(self.alias(root), unit + ".service")
                with open(log) as fh:
                    lines = fh.read().splitlines()
                self.assertIn("systemctl enable " + unit, lines)
                for other in ("gdm3", "gdm", "sddm", "plasmalogin", "lightdm"):
                    if other != unit:
                        self.assertIn("systemctl disable " + other, lines)

    def test_debians_gdm3_unit_has_no_install_section_so_the_alias_is_linked_by_hand(self):
        """The second half of the 2026-09-08 regression, found by the CI rigs
        after the first half was fixed: `systemctl enable gdm3` exits 0 and
        links nothing, because Debian's unit carries no [Install] and the
        postinst that would have linked display-manager.service ran before
        /etc/X11/default-display-manager named gdm3.  dm_enable makes the link
        the postinst makes, and only then: Fedora's gdm.service has the Alias
        and is linked by systemd itself."""
        got, root, log = self.run_dm("dm_gdm wayland", tree=["etc/gdm3/", self.WL + "ubuntu.desktop"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.alias(root), "gdm3.service")
        with open(log) as fh:
            text = fh.read()
        self.assertIn("gdm3.service has no [Install] section; linking display-manager.service", text)
        self.assertIn("systemctl daemon-reload", text)
        got, root, log = self.run_dm("dm_gdm wayland", tree=["etc/gdm/", self.WL + "gnome.desktop"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.alias(root), "gdm.service")
        with open(log) as fh:
            self.assertNotIn("linking display-manager.service", fh.read())

    def test_ubuntus_gdm3_unit_is_a_symlink_to_gdm_service_and_the_check_follows_it(self):
        """Measured on the third build of 2026-09-09: Ubuntu 24.04 ships
        gdm.service and gdm3.service as a symlink to it, so the alias resolves
        to gdm.service and a check on the unit's NAME refuses the image it just
        configured.  Both sides are compared resolved."""
        tree = ["etc/gdm3/", self.WL + "ubuntu.desktop"]
        got, root, _ = self.run_dm("dm_gdm wayland", tree=tree, units=("gdm",),
                                   links=[("usr/lib/systemd/system/gdm3.service",
                                           "usr/lib/systemd/system/gdm.service")])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(os.path.basename(os.path.realpath(
            os.path.join(root, "etc/systemd/system/display-manager.service"))), "gdm.service")

    def test_the_debian_pointer_file_is_written_only_where_debian_reads_one(self):
        """/etc/X11/default-display-manager is x11-common's; dnf and pacman
        systems have neither the file nor anything that reads it."""
        _, root, _ = self.run_dm("dm_gdm wayland", tree=["etc/gdm3/", self.WL + "ubuntu.desktop"])
        self.assertEqual(self.read(root, "etc/X11/default-display-manager").strip(), "/usr/sbin/gdm3")
        _, root, _ = self.run_dm("dm_sddm plasma.desktop", tree=[self.WL + "plasma.desktop"])
        self.assertEqual(self.read(root, "etc/X11/default-display-manager").strip(), "/usr/bin/sddm")
        _, root, _ = self.run_dm("PKG=dnf; dm_gdm wayland", tree=["etc/gdm/", self.WL + "gnome.desktop"])
        self.assertFalse(os.path.exists(os.path.join(root, "etc/X11/default-display-manager")))
        self.assertEqual(self.alias(root), "gdm.service")

    def test_gdm_refuses_when_the_alias_does_not_come_up(self):
        """gdm3 installed but its unit gone (or a systemd that did not link the
        alias): the build stops with the reason on the console instead of a
        golden image that boots to a login: prompt."""
        got, _, log = self.run_dm("dm_gdm wayland", tree=["etc/gdm3/", self.WL + "ubuntu.desktop"], units=())
        self.assertEqual(got.returncode, 9)
        with open(log) as fh:
            self.assertRegex(fh.read(), r"fail: (systemctl enable gdm3 failed|no unit file for gdm3)")


class ThePlasmaWelcomeCentre(unittest.TestCase):
    """R03.  Plasma 6 does not autostart the welcome centre any more: kded's
    kded_plasma_welcome module opens it whenever plasma-welcomerc's
    LastSeenVersion is missing or older than the installed plasma-welcome, so
    hiding the autostart entry is not enough and the version has to be looked
    up.  selftest.sh's `pgrep -a -x plasma-welcome` is what fails when it is
    not: "a first-run window is running in the session"."""

    def run_kde(self, manager, version="6.4.0-1"):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "root")
        os.makedirs(os.path.join(root, "home/test"))
        log = os.path.join(tmp, "log")
        open(log, "w").close()
        # every package manager present, only the named one answering
        d = stubs(tmp, ["chown", "kwriteconfig6", "dpkg-query=1", "rpm=1", "pacman=1"], log)
        answer = {"apt": ("dpkg-query", version), "dnf": ("rpm", version.split("-")[0]),
                  "pacman": ("pacman", "plasma-welcome " + version)}[manager]
        with open(os.path.join(d, answer[0]), "w") as fh:
            fh.write('#!/bin/sh\nprintf "%%s\\n" "$(basename "$0") $*" >> %s\n'
                     'printf "%%s\\n" %s\n' % (log, shlex.quote(answer[1])))
        os.chmod(os.path.join(d, answer[0]), 0o755)
        with open(os.path.join(d, "install"), "w") as fh:
            fh.write('#!/bin/sh\nargs=""\nwhile [ $# -gt 0 ]; do case $1 in\n'
                     '  -o|-g) shift 2 ;;\n'
                     '  *) args="$args \'$1\'"; shift ;;\nesac; done\n'
                     'eval exec /usr/bin/install $args\n')
        os.chmod(os.path.join(d, "install"), 0o755)
        body = (PREAMBLE.replace("written() {", "unused_written() {")
                + "TESTHOME=$VMCTL_ROOT/home/test\n"
                + sh_layer("written", "wcat", "wdir", "hide_autostart", "desktop_kde")
                + "\ndesktop_kde\n")
        got = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, PATH=d + ":" + os.environ["PATH"],
                                      FAKE_LOG=log, WRITTEN=os.path.join(tmp, "w"),
                                      VMCTL_ROOT=root, PKG="apt"))
        with open(log, encoding="utf-8") as fh:
            return got, fh.read().splitlines()

    def test_the_version_is_looked_up_on_every_package_manager(self):
        """apt, dnf AND pacman: arch-kde is an on-demand flavor of this same
        rig, and with no `pacman -Q` branch it alone would boot into the welcome
        centre.  `pacman -Q` prints "name version", which is why its answer is
        cut down the way pkg_manifest normalises the same shape; dpkg's carries
        the Debian revision, which is why the tail is cut too."""
        for manager in ("apt", "dnf", "pacman"):
            with self.subTest(manager):
                got, log = self.run_kde(manager)
                self.assertEqual(got.returncode, 0, got.stderr)
                wrote = [ln for ln in log if "plasma-welcomerc" in ln and "kwriteconfig" in ln]
                self.assertEqual(len(wrote), 1, "\n".join(log))
                self.assertTrue(wrote[0].endswith("--group General --key LastSeenVersion 6.4.0"),
                                wrote[0])

    def test_an_epoch_and_a_revision_are_stripped_from_the_version(self):
        """kded compares LastSeenVersion with plasma-welcome's own version
        string, which carries neither: dpkg's `4:6.4.0-1ubuntu2` has to become
        6.4.0 or every boot decides the welcome centre has never been seen."""
        _, log = self.run_kde("apt", version="4:6.4.0-1ubuntu2")
        wrote = [ln for ln in log if "plasma-welcomerc" in ln][0]
        self.assertTrue(wrote.endswith("LastSeenVersion 6.4.0"), wrote)

    def test_a_package_manager_that_answers_nothing_writes_no_version(self):
        """Better no file than a version nobody can compare: the autostart entry
        is hidden either way, and the failure is then selftest's to report."""
        got, log = self.run_kde("apt", version="")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual([ln for ln in log if "kwriteconfig" in ln
                          and "plasma-welcomerc" in ln], [])
        self.assertIn("say: warning: no plasma-welcome version from dpkg/rpm/pacman; "
                      "plasma-welcomerc not written", log)

    def test_the_autostart_entry_is_hidden_as_well(self):
        """Plasma 5.27 still autostarts it; both mechanisms are covered."""
        got, _ = self.run_kde("apt")
        self.assertEqual(got.returncode, 0, got.stderr)


class TheWlrLayoutHelper(unittest.TestCase):
    """R03.  /usr/local/bin/vmctl-wlr-layout is vmctl-sway-layout for the
    compositors with no IPC (labwc and the three desktops on it, river, and
    whatever else wlroots names Virtual-N).  wlroots adds the initial outputs in
    REVERSE enumeration order -- measured on labwc, where HEADLESS-1 landed at
    +1280+0 [recon2/labwc, recon2/xfce-wayland] -- and the rig's contract is
    head 0 = Virtual-1 at (0,0), so this script is the whole of that contract on
    six flavors.  Sliced out of the heredoc and run against recorded wlr-randr
    output with a stub `wlr-randr` that records the argv it is called back
    with."""

    @staticmethod
    def script():
        body = support.sh_function(BUILD, "wlr_layout")
        return body.split("<<'EOF'\n", 1)[1].rsplit("\nEOF", 1)[0]

    def place(self, text):
        """Run the helper over `text` and return the argv of its second call."""
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        out = os.path.join(tmp, "wlr.txt")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text)
        log = os.path.join(tmp, "log")
        with open(os.path.join(tmp, "wlr-randr"), "w") as fh:
            # no arguments = the query; anything else is the placement being asked for
            fh.write('#!/bin/sh\n[ $# -eq 0 ] && exec cat %s\nprintf "%%s\\n" "$*" >> %s\n'
                     % (out, log))
        os.chmod(os.path.join(tmp, "wlr-randr"), 0o755)
        src = os.path.join(tmp, "layout.py")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(self.script())
        got = subprocess.run([sys.executable, src], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, PATH=tmp + ":" + os.environ["PATH"]))
        self.assertEqual(got.returncode, 0, got.stderr)
        if not os.path.exists(log):
            return ""
        with open(log, encoding="utf-8") as fh:
            return fh.read().strip()

    def test_the_heads_are_placed_in_connector_order_from_zero(self):
        """Connector order, not enumeration order: the fixture is what wlroots
        answered, Virtual-3 first and Virtual-1 last."""
        got = self.place(fixture("wlr-randr-0.4.1-virtual-3heads-one-disabled.txt"))
        self.assertEqual(got, "--output Virtual-1 --pos 0,0 --output Virtual-2 --pos 1280,0")

    def test_the_step_is_the_heads_own_width_and_not_a_constant(self):
        """`vmctl start --head-size` is free to ask for anything, and the
        recorded output above is 1280 wide: a hard-coded 1920 would leave a
        640-pixel gap between every pair of heads and put Virtual-2 somewhere
        wlr-randr never said a head was.  The width is on that head's own
        `(current)` mode line, three lines further up."""
        got = self.place(fixture("wlr-randr-0.4.1-virtual-3heads-one-disabled.txt"))
        self.assertNotIn("1920,0", got)
        self.assertIn("--pos 1280,0", got)
        wide = fixture("wlr-randr-0.4.1-virtual-3heads-one-disabled.txt").replace(
            "1280x720 px (current)", "1920x1080 px (current)")
        self.assertEqual(self.place(wide),
                         "--output Virtual-1 --pos 0,0 --output Virtual-2 --pos 1920,0")

    def test_a_disabled_head_is_not_placed(self):
        """Virtual-3 in the fixture is `Enabled: no` and has no mode line at
        all; placing it would turn it back on."""
        self.assertNotIn("Virtual-3",
                         self.place(fixture("wlr-randr-0.4.1-virtual-3heads-one-disabled.txt")))

    def test_nothing_is_asked_of_wlr_randr_when_there_are_no_heads(self):
        self.assertEqual(self.place(""), "")


class TheDesktopsOnLabwc(unittest.TestCase):
    """R03.  Three of the nineteen desktops are not labwc -- they merely RUN on
    it: Budgie, `startxfce4 --wayland` and `startlxqtwayland`.  Each starts its
    own session manager (budgie-desktop, xfce4-session, lxqt-session) under
    labwc, and where the rig's head-layout helper is hooked decides whether the
    contract "head 0 = Virtual-1 at (0,0)" holds at all.

    ~/.config/labwc is the wrong place for all three.  `startxfce4 --wayland`
    runs `labwc --config-dir ~/.config/xfce4/labwc --config
    ~/.config/xfce4/labwc/rc.xml --session xfce4-session` (read verbatim out of
    /usr/bin/startxfce4 4.20.4-1 and measured as the process tree
    [recon2/xfce-wayland]), so nothing under ~/.config/labwc is read and
    wlroots' reverse enumeration leaves Virtual-1 at +3840; and labwc's
    `environment` file OVERRIDES a variable that is already set -- measured on
    labwc 0.9.3, where an environment carrying XDG_CURRENT_DESKTOP=LXQt:wlroots
    came out as labwc:wlroots -- so writing it under an LXQt session would have
    the rig rewrite the desktop identity the tools' own detection reads."""

    LAYER = ("written", "wcat", "wdir", "hide_autostart", "wlr_layout",
             "autostart_wlr_layout", "labwc_config", "xfce_settings",
             "desktop_xfce_wayland", "desktop_lxqt_wayland", "desktop_budgie",
             "desktop_labwc")

    def run_desktop(self, fn, tree=(), stub=("chown", "labwc")):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        root = os.path.join(tmp, "root")
        os.makedirs(os.path.join(root, "home/test"))
        for rel in tree:
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()
        log = os.path.join(tmp, "log")
        open(log, "w").close()
        d = stubs(tmp, list(stub), log)
        # `install -d -o test -g test` needs root and a `test` user; the directories
        # still have to be made for real, so the stub strips the ownership flags and
        # hands the rest to the real install.
        with open(os.path.join(d, "install"), "w") as fh:
            fh.write('#!/bin/sh\nargs=""\nwhile [ $# -gt 0 ]; do case $1 in\n'
                     '  -o|-g) shift 2 ;;\n'
                     '  *) args="$args \'$1\'"; shift ;;\nesac; done\n'
                     'eval exec /usr/bin/install $args\n')
        os.chmod(os.path.join(d, "install"), 0o755)
        body = (PREAMBLE.replace("written() {", "unused_written() {")
                + "TESTHOME=$VMCTL_ROOT/home/test\n" + sh_layer(*self.LAYER)
                + "\n%s\n" % fn)
        got = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, PATH=d + ":" + os.environ["PATH"],
                                      STUBS=d, FAKE_LOG=log, WRITTEN=os.path.join(tmp, "w"),
                                      VMCTL_ROOT=root, PKG="apt"))
        return got, os.path.join(root, "home/test")

    XFCE_TREE = ["usr/share/wayland-sessions/xfce-wayland.desktop"]

    def test_the_layout_helper_is_hooked_through_xdg_autostart(self):
        """xfce4-session, lxqt-session and budgie-desktop all run XDG autostart;
        it is the one hook all three share and the only one this rig can reach
        without owning the session script."""
        for fn, tree in (("desktop_xfce_wayland", self.XFCE_TREE),
                         ("desktop_lxqt_wayland", ()), ("desktop_budgie", ())):
            with self.subTest(fn):
                got, home = self.run_desktop(fn, tree)
                self.assertEqual(got.returncode, 0, got.stderr)
                entry = os.path.join(home, ".config/autostart/vmctl-wlr-layout.desktop")
                self.assertTrue(os.path.exists(entry), "no XDG autostart entry")
                with open(entry, encoding="utf-8") as fh:
                    self.assertIn("Exec=/usr/local/bin/vmctl-wlr-layout", fh.read())

    def test_a_desktop_on_labwc_writes_nothing_into_labwcs_own_config(self):
        """The two failures this prevents, one per file: an autostart under
        ~/.config/labwc that the session's labwc never reads (its --config-dir
        is elsewhere), and an `environment` that would overwrite the desktop's
        own XDG_CURRENT_DESKTOP if it did."""
        for fn, tree in (("desktop_xfce_wayland", self.XFCE_TREE),
                         ("desktop_lxqt_wayland", ()), ("desktop_budgie", ())):
            with self.subTest(fn):
                got, home = self.run_desktop(fn, tree)
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertFalse(os.path.exists(os.path.join(home, ".config/labwc")),
                                 "wrote into ~/.config/labwc")

    def test_the_bare_labwc_flavor_still_gets_its_own_config(self):
        """labwc alone IS the session, `labwc` with no --config-dir, and it
        draws nothing at all -- the swaybg line in that autostart is what keeps
        head 0 from being one flat colour and failing vmctl's paint check."""
        got, home = self.run_desktop("desktop_labwc")
        self.assertEqual(got.returncode, 0, got.stderr)
        with open(os.path.join(home, ".config/labwc/autostart"), encoding="utf-8") as fh:
            auto = fh.read()
        self.assertIn("/usr/local/bin/vmctl-wlr-layout &", auto)
        self.assertIn("swaybg -i", auto)
        with open(os.path.join(home, ".config/labwc/environment"), encoding="utf-8") as fh:
            self.assertIn("XDG_CURRENT_DESKTOP=labwc:wlroots", fh.read())

    def test_lxqt_wayland_names_the_compositor_where_lubuntu_cannot_outrank_it(self):
        """lubuntu-default-settings ships /etc/xdg/xdg-Lubuntu/lxqt/session.conf
        with window_manager=openbox and a Lubuntu session prepends xdg-Lubuntu to
        XDG_CONFIG_DIRS [recon2/openbox], so the rig's /etc/xdg copy can lose on
        the very key that decides whether startlxqtwayland takes its first-run
        branch (a different config directory, and lxqt-config-session instead of
        lxqt-session).  The user file outranks every system directory."""
        got, home = self.run_desktop("desktop_lxqt_wayland")
        self.assertEqual(got.returncode, 0, got.stderr)
        with open(os.path.join(home, ".config/lxqt/session.conf"), encoding="utf-8") as fh:
            self.assertIn("compositor=labwc", fh.read())

    def test_the_xfce_wayland_arm_refuses_without_the_session_file_or_labwc(self):
        """Both halves of "Xfce 4.20 on Wayland" have to be installed: the
        session file comes from xfce4-session 4.20 and the compositor is a
        separate package [recon2/xfce-wayland]."""
        got, _ = self.run_desktop("desktop_xfce_wayland")
        self.assertEqual(got.returncode, 9, "no session file, yet it built")
        # PATH is the stub directory prepended to the caller's, so a labwc on the
        # machine running the tests would answer `command -v`; the whole PATH goes.
        got, _ = self.run_desktop("PATH=$STUBS; desktop_xfce_wayland", self.XFCE_TREE,
                                  stub=("chown",))
        self.assertEqual(got.returncode, 9, "no labwc, yet it built")
        got, _ = self.run_desktop("desktop_xfce_wayland", self.XFCE_TREE)
        self.assertEqual(got.returncode, 0, got.stderr)


class TheDispatchTable(unittest.TestCase):
    """R04.  build-image.sh's `select_desktop` and vmctl's DESKTOPS are one
    table written twice: the host reads the yaml's `# vmctl-desktop:` and looks
    it up in DESKTOPS, the guest takes the same token and picks a display
    manager and a desktop function.  A token in one and not the other is a
    flavor that builds and never logs in, or a flavor vmctl refuses to start."""

    @classmethod
    def setUpClass(cls):
        with open(BUILD, encoding="utf-8") as fh:
            cls.build = fh.read()
        cls.body = support.sh_function(BUILD, "select_desktop")
        cls.arms = {}
        for m in re.finditer(r"^    ([a-z0-9-]+)\)\s+DM=(\S+)\s+DM_ARGS=\"([^\"]*)\"\s+DESK=(\S+)",
                             cls.body, re.M):
            cls.arms[m.group(1)] = (m.group(2), m.group(3), m.group(4))

    def test_the_table_names_every_desktops_key_and_nothing_else(self):
        self.assertEqual(sorted(self.arms), sorted(vmctl_module().DESKTOPS))

    def test_every_function_the_table_names_exists(self):
        for token, (dm, _args, desk) in self.arms.items():
            with self.subTest(token):
                self.assertIn("\n%s() {" % dm, self.build)
                self.assertIn("\n%s() {" % desk, self.build)

    def test_the_display_manager_matches_the_desktops_row(self):
        """DESKTOPS' `dm` is what the failure hint tells a human to read the
        journal of; the dispatch table is what actually configured it.  They may
        not disagree."""
        want = {"dm_gdm": "gdm", "dm_sddm": "sddm", "dm_plasma": "sddm",
                "dm_greetd": "greetd", "dm_lightdm": "lightdm"}
        desktops = vmctl_module().DESKTOPS
        for token, (dm, _args, _desk) in self.arms.items():
            with self.subTest(token):
                self.assertIn(want[dm], desktops[token]["dm"])

    def test_an_unknown_desktop_is_refused_by_name(self):
        body = PREAMBLE + self.body + '\nDESKTOP=beos select_desktop\n'
        got = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, FAKE_LOG="/dev/stdout", WRITTEN="/dev/null"))
        self.assertEqual(got.returncode, 9)
        self.assertIn("unknown DESKTOP=beos", got.stdout)

    def test_the_wlroots_desktops_get_greetd_with_their_own_compositor(self):
        """The command greetd runs is the compositor's own name, and it is what
        `scanout_owner()` will look for in DRM debugfs -- `Hyprland` with a
        capital H, measured [recon2/hyprland]."""
        desktops = vmctl_module().DESKTOPS
        for token in ("sway", "hypr", "labwc", "wayfire", "river"):
            with self.subTest(token):
                dm, args, _ = self.arms[token]
                self.assertEqual(dm, "dm_greetd")
                self.assertEqual(args.split()[0], desktops[token]["compositor"])

    def test_the_only_multi_word_command_is_rivers_and_it_names_its_init(self):
        """river has no config file: `river -c <executable>` is the whole of its
        configuration [recon2/river], so the rest of the line is not decoration
        -- it is where tinyrwm, the D-Bus environment publish and the layout
        helper are started from.  Every other command is one word, which is why
        the greetd tests used to be able to miss the truncation."""
        multi = {t: a for t, (_dm, a, _d) in self.arms.items() if len(a.split()) > 1}
        self.assertEqual(multi, {"river": "river -c /usr/local/bin/vmctl-river-init"})
        self.assertIn('wcat "$VMCTL_ROOT/usr/local/bin/vmctl-river-init"', self.build)


class TheDesktopsTable(unittest.TestCase):
    """R05.  vmctl's DESKTOPS is read by `vmctl session`, `vmctl user`,
    `wait_first_paint`, selftest.sh and the smoke; every one of them trusts the
    row's shape."""

    @classmethod
    def setUpClass(cls):
        cls.mod = vmctl_module()
        with open(VMCTL, encoding="utf-8") as fh:
            cls.text = fh.read()

    def test_every_row_has_the_seven_fields(self):
        for token, row in self.mod.DESKTOPS.items():
            with self.subTest(token):
                self.assertEqual(sorted(row),
                                 ["compositor", "dm", "label", "logind", "session",
                                  "shell", "splash"])

    def test_session_logind_and_dm_are_from_their_sets(self):
        for token, row in self.mod.DESKTOPS.items():
            with self.subTest(token):
                self.assertIn(row["session"], ("wayland", "x11"))
                self.assertTrue(set(row["logind"]) <= {"wayland", "x11", "tty"}, row["logind"])
                self.assertIsInstance(row["dm"], tuple)
                self.assertTrue(row["dm"])
                self.assertIsInstance(row["shell"], tuple)

    def test_an_x11_desktop_has_the_x_server_as_its_scanout_owner(self):
        """On X11 the compositor is a client: the X server owns head 0's
        framebuffer, so kwin_x11/cinnamon/marco are never what DRM debugfs
        names.  Measured on a live Lubuntu session, `allocated by = Xorg`
        [recon2/openbox]."""
        for token, row in self.mod.DESKTOPS.items():
            if row["session"] == "x11":
                with self.subTest(token):
                    self.assertEqual(row["compositor"], "Xorg")

    def test_a_greetd_desktop_may_report_a_tty_session(self):
        """greetd registers the session as a plain tty one and the compositor's
        libseat switches it to wayland, so both are legal for exactly the
        desktops greetd starts."""
        for token, row in self.mod.DESKTOPS.items():
            with self.subTest(token):
                self.assertEqual("tty" in row["logind"], "greetd" in row["dm"])

    def test_the_hint_names_every_candidate_and_display_manager_service(self):
        """One row may have two display managers (Fedora 44's KDE has
        plasma-login-manager where Ubuntu has SDDM), and NixOS drives
        display-manager.service directly [recon2/fedora, recon2/nixos].  The
        hint after a timeout is the only thing a human has to go on."""
        self.assertEqual(self.mod.dm_units("kde"), ("sddm", "plasmalogin", "display-manager"))
        self.assertEqual(self.mod.dm_units("sway"), ("greetd", "display-manager"))

    def test_the_paint_gate_covers_both_gnome_sessions(self):
        """`GNOME Shell started` is emitted on startup-complete with no backend
        condition, so it is the paint gate on Xorg too [recon2/gnome-xorg]; the
        scanout branch below it would look for `gnome-shell` and find `Xorg`."""
        self.assertIn('case "$DESKTOP" in gnome|gnome-x11)', self.mod.PAINT_SCRIPT)

    def test_no_process_match_is_a_bare_pgrep_x(self):
        """nixpkgs wraps GUI programs and a wrapped foo's comm is
        `.foo-wrappe`, so every match goes through pg() [recon2/nixos].  A bare
        `pgrep -x swaybg` would wait forever on a NixOS flavor."""
        for name, script in (("PAINT_SCRIPT", self.mod.PAINT_SCRIPT),
                             ("USER_WRAPPER", self.mod.USER_WRAPPER),
                             ("SESSION_SCRIPT", self.mod.SESSION_SCRIPT)):
            with self.subTest(name):
                without_pg = re.sub(r"pg\(\) \{.*?\n\}", "", script, flags=re.S)
                self.assertNotIn("pgrep", without_pg)
        self.assertIn(r'''pgrep -x "$1|\.$(printf '%.14s' "$1-wrapped")"''',
                      self.mod.PAINT_SCRIPT)

    def pg_says(self, script, want, *names):
        """Slice pg() out of `script`, run it under bash against the real
        `pgrep`, and return what it said about each name -- with one live
        process whose comm is `want`.

        Nothing is re-implemented here: a Python `re` over a pattern typed into
        the test would pass whatever pg() became, which is exactly how the
        truncation bug below survived."""
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        # comm is argv[0]'s basename, so a copy of bash named `.kwin_wayland-wrapped`
        # gives the kernel the same fifteen-character comm nixpkgs' wrapper does.
        m = re.search(r"^pg\(\) \{.*?\n\}", script, re.S | re.M)
        self.assertTrue(m, "no pg() in this script")
        # pg() reads the whole process table, so a real sway (a live session on the
        # developer's box, or a compositor another test file left running in the
        # same sandbox) answers for the name before this test's own process exists
        for n in names:
            pre = subprocess.run(["bash", "-c", m.group(0) + "\npg %s\n" % shlex.quote(n)],
                                 capture_output=True, timeout=60)
            if pre.returncode == 0:
                self.skipTest("a live process already answers for `pg %s` on this box" % n)
        exe = os.path.join(tmp, want)
        shutil.copy(shutil.which("bash"), exe)
        proc = subprocess.Popen([exe, "-c", "read x"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        out = {}
        for n in names:
            got = subprocess.run(["bash", "-c", m.group(0)
                                  + '\npg %s >/dev/null 2>&1 && echo YES || echo NO\n'
                                  % shlex.quote(n)],
                                 capture_output=True, text=True, timeout=60)
            out[n] = got.stdout.strip().splitlines()[-1] == "YES"
        return out

    def test_pg_matches_a_wrapped_name_whose_comm_was_truncated(self):
        """`kwin_wayland` is twelve characters, so its wrapped comm is
        `.kwin_wayland-w` -- fifteen characters with no `-wr` left in it, which
        is what a `-wr` suffix pattern needed and did not get.  Every 12- and
        13-character name in DESKTOPS (kwin_wayland, budgie-panel, cosmic-panel,
        mate-session, lxqt-session) was in that hole."""
        got = self.pg_says(self.mod.PAINT_SCRIPT, ".kwin_wayland-w",
                           "kwin_wayland", "kwin", "kwin_waylandd")
        self.assertTrue(got["kwin_wayland"], "the wrapped kwin_wayland was not found")
        self.assertFalse(got["kwin"], "a prefix of the name must not match")
        self.assertFalse(got["kwin_waylandd"], "a longer name must not match")

    def test_pg_matches_the_wrapped_name_and_not_a_neighbour(self):
        """The fifteen-character comm of a wrapped swaybg is `.swaybg-wrapped`
        exactly; sway's would be `.sway-wrapped`, and the pattern for `sway`
        must not swallow swaybg's.  Run against procps' own `pgrep -x`, whose
        whole-match ERE semantics are the half of this that is not vmctl's."""
        got = self.pg_says(self.mod.PAINT_SCRIPT, ".swaybg-wrapped",
                           "swaybg", "sway", "swaybgd", "myswaybg")
        self.assertTrue(got["swaybg"])
        self.assertFalse(got["sway"], "pg sway must not match .swaybg-wrapped")
        self.assertFalse(got["swaybgd"])
        self.assertFalse(got["myswaybg"])

    def test_the_user_wrappers_pg_is_the_same_rule_scoped_to_the_user(self):
        """USER_WRAPPER runs under /bin/sh (dash on Ubuntu), where `${v:0:14}`
        is not a substring; the shipped body uses `printf '%.14s'`, which is
        POSIX, and it is the pids it prints and not a status."""
        got = self.pg_says(self.mod.USER_WRAPPER, ".mate-session-w",
                           "mate-session", "mate")
        self.assertTrue(got["mate-session"])
        self.assertFalse(got["mate"])
        self.assertIn('pgrep -u "$(id -u)" -x', self.mod.USER_WRAPPER)


class TheUserWrapper(unittest.TestCase):
    """R06.  USER_WRAPPER is the shell `vmctl user` runs every command inside;
    it rebuilds the session environment from the systemd user manager first, the
    session leader's environ second and the sockets that are there third.  It is
    sliced out of vmctl and run here with a stand-in `systemctl --user
    show-environment`."""

    @classmethod
    def setUpClass(cls):
        cls.wrapper = vmctl_module().USER_WRAPPER

    def run_wrapper(self, desktop, session, showenv, extra_stubs=()):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        d = os.path.join(tmp, "stub")
        os.makedirs(d)
        with open(os.path.join(d, "systemctl"), "w") as fh:
            fh.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % showenv)
        with open(os.path.join(d, "loginctl"), "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        with open(os.path.join(d, "pgrep"), "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        for name, body in extra_stubs:
            with open(os.path.join(d, name), "w") as fh:
                fh.write(body)
        for f in os.listdir(d):
            os.chmod(os.path.join(d, f), 0o755)
        env = {"PATH": d + ":/usr/bin:/bin", "HOME": tmp, "XDG_RUNTIME_DIR": tmp,
               "VMCTL_DESKTOP": desktop, "VMCTL_SESSION": session}
        got = subprocess.run(["sh", "-c", self.wrapper, "vmctl-user", "env"],
                             capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(got.returncode, 0, got.stderr)
        return dict(ln.split("=", 1) for ln in got.stdout.splitlines() if "=" in ln)

    def test_the_three_new_socket_variables_are_imported(self):
        """Hyprland's own /proc/<pid>/environ carries XDG_SESSION_TYPE=tty and
        neither WAYLAND_DISPLAY nor the signature, while `systemctl --user
        show-environment` carries both, because Hyprland exports them itself
        [recon2/hyprland, recon2/arch].  The user manager is therefore the only
        place these three can come from."""
        env = self.run_wrapper("hypr", "wayland",
                               "HYPRLAND_INSTANCE_SIGNATURE=v0.53.3_1757000000\n"
                               "WAYFIRE_SOCKET=/run/user/1000/wayfire-wayland-1-.socket\n"
                               "I3SOCK=/run/user/1000/i3/ipc-socket.812\n"
                               "WAYLAND_DISPLAY=wayland-1")
        self.assertEqual(env["HYPRLAND_INSTANCE_SIGNATURE"], "v0.53.3_1757000000")
        self.assertEqual(env["WAYFIRE_SOCKET"], "/run/user/1000/wayfire-wayland-1-.socket")
        self.assertEqual(env["I3SOCK"], "/run/user/1000/i3/ipc-socket.812")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")

    def test_a_variable_outside_the_whitelist_is_not_imported(self):
        """imp() is a whitelist and not a filter: the user manager's environment
        carries whatever the session put there, and `vmctl user` is not a way to
        inherit it."""
        env = self.run_wrapper("sway", "wayland",
                               "VMCTL_NOT_WHITELISTED=x\nWAYLAND_DISPLAY=wayland-1")
        self.assertNotIn("VMCTL_NOT_WHITELISTED", env)

    def test_a_value_with_a_shell_character_is_refused(self):
        """The values are exported by a shell, so what may be in them is a
        closed set of characters -- this is the guard that keeps a session
        variable from becoming a command."""
        env = self.run_wrapper("sway", "wayland", "WAYLAND_DISPLAY=wayland-1;id")
        self.assertNotIn("WAYLAND_DISPLAY", env)

    def test_i3sock_is_asked_of_i3_itself(self):
        """i3 writes $XDG_RUNTIME_DIR/i3/ipc-socket.<pid> and exports nothing,
        so without this the tools forced onto our own code find no IPC socket at
        all [recon2/i3].  `i3 --get-socketpath` is i3's own answer."""
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        sock = os.path.join(tmp, "ipc-socket.812")
        import socket as socket_mod
        s = socket_mod.socket(socket_mod.AF_UNIX)
        s.bind(sock)
        self.addCleanup(s.close)
        env = self.run_wrapper("i3", "x11", "DISPLAY=:0",
                               extra_stubs=[("i3", "#!/bin/sh\necho %s\n" % sock)])
        self.assertEqual(env["I3SOCK"], sock)

    def test_display_is_defaulted_before_i3_is_asked_for_its_socket(self):
        """`i3 --get-socketpath` reads the I3_SOCKET_PATH property off the ROOT
        WINDOW [recon2/i3]: with no DISPLAY it cannot open the connection and
        answers nothing, and the find fallback -- which cannot tell two i3
        sessions apart -- is what answers instead.  Ordering is the whole fix,
        so ordering is what is asserted: the wrapper's DISPLAY default has to
        come first.  The functional test above passes DISPLAY in through the
        systemd environment and so cannot see this."""
        w = vmctl_module().USER_WRAPPER
        self.assertLess(w.index("export DISPLAY=:0"), w.index("i3 --get-socketpath"))

    def test_the_desktop_falls_back_to_a_name_the_session_would_have_set(self):
        """XDG_CURRENT_DESKTOP is what the tools' own detection reads when
        nothing else answers; river is deliberately absent, having no such name
        of its own."""
        for desktop, want in (("labwc", "labwc:wlroots"), ("cosmic", "COSMIC"),
                              ("hypr", "Hyprland"), ("cinnamon", "X-Cinnamon"),
                              ("mate", "MATE"), ("lxqt-wayland", "LXQt"),
                              ("gnome-x11", "ubuntu:GNOME"), ("budgie", "Budgie")):
            with self.subTest(desktop):
                env = self.run_wrapper(desktop, "wayland", "")
                self.assertEqual(env.get("XDG_CURRENT_DESKTOP"), want)
        self.assertNotIn("XDG_CURRENT_DESKTOP", self.run_wrapper("river", "wayland", ""))

    def test_the_session_leader_is_not_always_the_compositor(self):
        """On the Wayland Xfce and LXQt sessions labwc is the compositor, but
        xfce4-session and lxqt-session are what hold DISPLAY and
        XDG_CURRENT_DESKTOP in their environ [recon2/xfce-wayland,
        recon2/openbox]; on Budgie the environ that matters is labwc's."""
        block = self.wrapper.split('case "$VMCTL_DESKTOP" in', 1)[1].split("esac", 1)[0]
        leaders = {}
        for tokens, comp in re.findall(r"([a-z0-9|-]+)\) comp=(\S+)", block):
            for t in tokens.split("|"):
                leaders[t] = comp
        for desktop, leader in (("xfce-wayland", "xfce4-session"),
                                ("lxqt-wayland", "lxqt-session"),
                                ("budgie", "labwc"), ("cosmic", "cosmic-comp"),
                                ("hypr", "Hyprland"), ("i3", "i3")):
            with self.subTest(desktop):
                self.assertEqual(leaders.get(desktop), leader)


class ThePackageListParser(unittest.TestCase):
    """R07.  `vmctl build` reads the guest's package list off the serial console
    and refuses the build when what it parsed does not match what the guest
    said it wrote.  That check is the only proof the image finished; a name
    regex that drops Fedora's `NetworkManager` would fail every Fedora build
    with a count mismatch and no other clue."""

    @classmethod
    def setUpClass(cls):
        cls.mod = vmctl_module()
        # Sliced out of cmd_build rather than copied, for the reason every slice
        # in this file exists: a parser that drifts from what runs is what this
        # test cannot catch.
        src = support.sh_block(VMCTL, 'm = re.search(r"VMCTL-PACKAGES-BEGIN',
                               "guest reported {n[1] if n else '?'})\")")
        cls.src = re.sub(r"^        ", "", src, flags=re.M).replace("raise Fail(",
                                                                   "raise ValueError(")

    def parse(self, text):
        ns = {"re": re, "flavor": "x", "serial": "s", "text": text}
        exec(self.src, ns)
        return ns["pkgs"]

    def test_a_dpkg_list_parses(self):
        pkgs = self.parse(fixture("serial-packages-dpkg.txt"))
        self.assertIn("libgtk-3-0t64", pkgs)
        self.assertEqual(len(pkgs), 311)

    def test_an_rpm_list_keeps_the_uppercase_names(self):
        """Fedora has exactly two names in a desktop install that a lowercase-
        only regex would drop, and dropping them is a count mismatch, which is
        the message `build ... looks wrong` [recon2/pkg-rpm]."""
        pkgs = self.parse(fixture("serial-packages-rpm.txt"))
        self.assertIn("NetworkManager", pkgs)
        self.assertIn("ModemManager", pkgs)
        self.assertIn("xorg-x11-server-Xwayland", pkgs)

    def test_a_pacman_list_parses_including_an_underscore(self):
        """`pacman -Qq` prints names only (the guest's pkg_manifest does the
        normalising), and Arch has package names with an underscore that
        Debian's regex would have dropped."""
        pkgs = self.parse(fixture("serial-packages-pacman.txt"))
        self.assertIn("linux_api_headers", pkgs)
        self.assertIn("archlinux-keyring", pkgs)

    def test_a_short_list_fails_the_sanity_floor(self):
        """Under 200 names is not a desktop image: the smallest measured one is
        an Arch sway image at ~450 packages.  The floor catches a serial log
        that was truncated, which looks exactly like a successful build."""
        with self.assertRaises(ValueError) as e:
            self.parse(fixture("serial-packages-short.txt"))
        self.assertIn("150 entries parsed", str(e.exception))

    def test_a_count_the_guest_disagrees_with_fails(self):
        """The guest counts with `wc -l` and the host counts what it parsed;
        the two numbers are the check."""
        text = fixture("serial-packages-dpkg.txt").replace(
            "311 packages installed", "312 packages installed")
        with self.assertRaises(ValueError):
            self.parse(text)


class TheSELinuxRelabel(unittest.TestCase):
    """R33.  On Fedora SELinux is Enforcing and every file cloud-init's runcmd
    wrote carries whatever label that process had [recon2/fedora,
    recon2/pkg-rpm].  Nothing tripped an AVC in the recon, but nothing in the
    recon logged in either: the fix is cheap and the failure it prevents (a
    display manager that cannot read its own autologin file) is not."""

    @staticmethod
    def layers():
        """The dm_* and desktop_* half of build-image.sh: everything between the
        layer-2 banner and the build itself."""
        with open(BUILD, encoding="utf-8") as fh:
            text = fh.read()
        bar = "# ---------------------------------------------------------------- "
        return text.split(bar + "layer 2")[1].split(bar + "the build itself")[0]

    def test_every_write_in_the_layers_is_recorded(self):
        """Recorded either by `wcat` (which writes and remembers in one) or by a
        `written` call beside the redirection, for the three places that build a
        file out of a group -- the sway, Hyprland and i3 configs, each of which is
        the packaged default plus the rig's additions.  An unrecorded write is a
        file `relabel` never sees, which on Fedora is a display manager that
        cannot read its own autologin file."""
        lines = self.layers().splitlines()
        for i, line in enumerate(lines):
            if not re.search(r'> "\$(VMCTL_ROOT|TESTHOME)', line):
                continue
            if line.lstrip().startswith("wcat "):
                continue
            near = "\n".join(lines[i:i + 12])
            with self.subTest(line.strip()[:60]):
                self.assertRegex(near, r'written "\$(VMCTL_ROOT|TESTHOME)',
                                 "a write with no written() beside it")

    def test_the_relabel_runs_after_the_desktop_function(self):
        with open(BUILD, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("$DM $DM_ARGS\n$DESK\nrelabel\n", text)

    def test_restorecon_is_run_over_the_recorded_paths_on_dnf_only(self):
        tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "log")
        open(log, "w").close()
        d = stubs(tmp, ["restorecon"], log)
        listing = os.path.join(tmp, "written")
        with open(listing, "w") as fh:
            fh.write("%s/etc\n%s/etc\n%s/gone\n" % (tmp, tmp, tmp))
        os.makedirs(os.path.join(tmp, "etc"))
        body = (PREAMBLE + sh_layer("relabel")
                + '\nWRITTEN=%s\nrelabel\n' % listing)
        for pkg, want in (("dnf", ["restorecon -R %s/etc" % tmp]), ("apt", [])):
            with self.subTest(pkg):
                open(log, "w").close()
                got = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                                     timeout=60,
                                     env=dict(os.environ, PATH=d + ":" + os.environ["PATH"],
                                              FAKE_LOG=log, PKG=pkg))
                self.assertEqual(got.returncode, 0, got.stderr)
                with open(log) as fh:
                    lines = [ln for ln in fh.read().splitlines()
                             if ln.startswith("restorecon")]
                self.assertEqual(lines, want)


@LIVE
class TheRigItself(unittest.TestCase):
    """The only tests here that need a machine with ~/vm-data and QEMU.  Off by
    default: a unit test that boots a virtual machine is not a unit test, and
    the goldens are not in the repository."""

    def test_vmctl_lists_the_flavors(self):
        got = subprocess.run([sys.executable, os.path.join(VM, "vmctl"), "flavors"],
                             capture_output=True, text=True, timeout=300)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("resolute-gnome", got.stdout)


if __name__ == "__main__":
    unittest.main()
