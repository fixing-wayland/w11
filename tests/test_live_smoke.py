#!/usr/bin/env python3
"""vm/live-smoke.sh and its step files, with no rig and no virtual machine.

The smoke is the only place several claims in this tree are checked at all, and
it costs a boot -- 407 s on noble-gnome, 526-591 s on noble-kde
(vm/live-smoke.out/*.log, 2026-09-08).  So the parts of it that are ordinary
text processing are checked here instead, in a second:

* `vm/live-smoke.d/oracle.py` -- the desktop's OWN display tool, which is the
  second opinion every wxrandr apply in the smoke is measured against.  It grew
  from five desktops to nineteen, and every new branch is parsed here against
  bytes a real tool printed (R12).  Two of its branches had never run: the
  `gdbus call` fallback of the Mutter reader asked for a fourth capture group
  from a pattern with three, and required a bare digit run where GLib's printer
  emits `uint32 0`.  Both are fixed and both are pinned below, as is the third
  way it could answer nothing: a tool that is not installed on the guest now
  exits 2 and names itself, where it used to hand back an empty string that
  `oracle_outputs` reads as "no enabled output".
* the step files -- that each one parses, defines the two hooks the driver
  needs, names only phases that exist, and writes every `xwant` label in the
  one convention the plan chose (R13).
* the driver's package axis -- `--pkg` with `--deb` as its alias, and the four
  arms (dpkg, rpm, pacman, a NixOS specialisation switch) that decide what gets
  copied in, what installs it, what removes it and what may be left behind
  (R14).  Sliced out of the shipped script, so a table that drifts fails here.
* `selftest-offline.sh` itself: the two-pass rule on every recording, and the
  bookkeeping that says which step files have no recording yet (R31).

Nothing here boots anything, and nothing here is a measurement: the recordings
under tests/fixtures/live/ and tests/fixtures/vm/ are, and each says where it
came from.
"""

import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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
STEPS = os.path.join(VM, "live-smoke.d")
DRIVER = os.path.join(VM, "live-smoke.sh")
ORACLE = os.path.join(STEPS, "oracle.py")
SELFTEST_OFFLINE = os.path.join(STEPS, "selftest-offline.sh")
FLAVORS = os.path.join(VM, "flavors")
VMCTL = os.path.join(VM, "vmctl")
LIVEFIX = os.path.join(ROOT, "tests", "fixtures", "live")
VMFIX = os.path.join(ROOT, "tests", "fixtures", "vm")
NOT_YET_RUN = os.path.join(LIVEFIX, "NOT-YET-RUN")

#: Step files that are not a desktop token: the shared body, the self-test and
#: the three scripts that are copied into a guest and run there.
NOT_A_DESKTOP = ("common", "selftest-offline")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def oracle_module():
    """vm/live-smoke.d/oracle.py imported as a module.

    It runs inside a guest, so it is not importable by name from anywhere; the
    loader is how the shipped file itself -- not a copy of it -- is what these
    tests parse with."""
    spec = importlib.util.spec_from_loader(
        "w11_oracle_under_test",
        importlib.machinery.SourceFileLoader("w11_oracle_under_test", ORACLE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def step_tokens():
    """The desktop token of every vm/live-smoke.d/*.sh that is one."""
    return sorted(n[:-3] for n in os.listdir(STEPS)
                  if n.endswith(".sh") and not n.startswith("guest-")
                  and n[:-3] not in NOT_A_DESKTOP)


def flavor_desktops():
    """{flavor: its `# vmctl-desktop:`} over vm/flavors/."""
    out = {}
    for name in sorted(os.listdir(FLAVORS)):
        if not name.endswith(".yaml"):
            continue
        found = re.findall(r"^#\s*vmctl-desktop:\s*(\S+)", read(FLAVORS, name), re.M)
        out[name[:-5]] = found[0] if found else None
    return out


def recorded_tokens():
    """{token: [recording file names]} for the replay transcripts.

    A recording is named after the flavor it was taken on, and the flavor's own
    header says which desktop that is -- so nothing here hard-codes a mapping.
    Longest flavor name wins, or `noble-gnome` would claim a
    `noble-gnome-iso-...` recording."""
    flavors = flavor_desktops()
    out = {}
    for name in sorted(os.listdir(LIVEFIX)):
        if not name.endswith("-replay.txt"):
            continue
        best = ""
        for flavor in flavors:
            if name.startswith(flavor) and len(flavor) > len(best):
                best = flavor
        out.setdefault(flavors.get(best), []).append(name)
    return out


#: The second class of line in NOT-YET-RUN, which arrived with the `proxy`
#: phase: `proxy:<flavor>`, one per FLAVOR rather than one per step file.  The
#: shell half of the same bookkeeping (`selftest-offline.sh` pass 5) matches its
#: tokens with `grep -cx`, so a line of this shape is invisible to it, which is
#: what lets the two classes share one file.
PROXY_LINE = re.compile(r"^proxy:(\S+)$")


def not_yet_run():
    """The step-file tokens declared not yet recorded -- the `proxy:` lines are
    a different claim and `not_yet_run_proxy` is where they are read."""
    return [ln.strip() for ln in read(NOT_YET_RUN).splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
            and not PROXY_LINE.match(ln.strip())]


def not_yet_run_proxy():
    """The flavors whose `proxy` phase has never been run."""
    return [PROXY_LINE.match(ln.strip()).group(1)
            for ln in read(NOT_YET_RUN).splitlines()
            if PROXY_LINE.match(ln.strip())]


#: Every Wayland step file, which is every one whose SMOKE_PHASES runs `windows`
#: -- the X11 files run `passthrough` and stop, because the handover has already
#: replaced the process by the time a window command could run.  Read out of the
#: files rather than listed here: a step file that changes sides has to change
#: this test's answer with it.
def phases_of(token):
    """`SMOKE_PHASES` as the driver sees it, inheritance resolved.

    Three step files are three assignments and a source (lxqt-wayland sources
    labwc, i3 and kde-x11 source xfce), so the list has to come from bash and
    not from a regex over the file."""
    pre = "set -u\nSTEPS=%s\nDESKTOP=%s\nDISTRO=ubuntu\nMODE=pkg\nREUSE=0\n" % (STEPS, token)
    pre += "VM=/nonexistent/vmctl\nNAME=t\nFLAVOR=t\nREPO=%s\nHEADS=2\nSCALE=0\n" % ROOT
    for name in TheStepFiles.HELPERS.split():
        pre += "%s() { :; }\n" % name
    pre += '. "$STEPS/common.sh"\n. "$STEPS/%s.sh"\necho "P:$SMOKE_PHASES"\n' % token
    got = subprocess.run(["bash", "-c", pre], capture_output=True, text=True, timeout=60)
    line = [ln[2:] for ln in got.stdout.splitlines() if ln.startswith("P:")]
    if not line:
        raise AssertionError("%s printed no SMOKE_PHASES:\n%s" % (token, got.stderr[-2000:]))
    return line[0].split()


class TheOracle(unittest.TestCase):
    """R12.  vm/live-smoke.d/oracle.py, one branch at a time, over the bytes
    the real tool printed.  This is the file the whole display half of the
    smoke rests on: if it answers wrong, every `--output X --pos ...` check
    agrees with it and the run is green on nothing."""

    @classmethod
    def setUpClass(cls):
        cls.mod = oracle_module()

    def answering(self, text):
        """Point the module's one subprocess seam at recorded bytes.

        The seam is `run()`, which is the whole of what the module does with the
        outside world; everything below it is the parser under test."""
        self.mod.run = lambda *argv: text

    def without_gi(self):
        """Take python3-gi away for one test.

        The Mutter/Muffin reader tries the bindings first and falls back to
        `gdbus call` text; this host HAS the bindings, so the text branch would
        never be reached -- and it is the branch the KDE, sway and wlroots
        goldens, which carry no gi, actually run.  `sys.modules['gi'] = None`
        makes `import gi` raise, which is what "not installed" looks like.

        Exactly ONE cleanup, and which one depends on what was there.  Two of
        them looked safer and were not: cleanups run last-in-first-out, so a
        `pop` registered first ran AFTER the `__setitem__` that put the real
        module back and took it away again.  A re-imported `gi` is not the same
        object -- Debian's gi/__init__.py refuses to load a second time beside
        the static bindings and raises "you must not import static modules like
        gobject" -- so under `unittest discover` this left
        tests/test_overlap_consent.py's `test_it_records_no_agreement` with an
        ImportError on `from warandr import gui`, a file that is green run
        alone."""
        missing = object()
        had = sys.modules.get("gi", missing)
        if had is missing:
            self.addCleanup(sys.modules.pop, "gi", None)
        else:
            self.addCleanup(sys.modules.__setitem__, "gi", had)
        sys.modules["gi"] = None

    # -- wlr-randr, five desktops' only reader ------------------------------

    def test_wlr_randr_lists_every_enabled_head_with_its_position(self):
        self.answering(read(VMFIX, "wlr-randr-0.4.1-virtual-3heads-one-disabled.txt"))
        self.assertEqual(self.mod.wlr(), ["Virtual-2 1280,0", "Virtual-1 0,0"])

    def test_a_disabled_head_is_left_out_and_does_not_take_the_next_ones_position(self):
        """0.4.1 prints `Enabled: no` and then NO `Position:` line at all, so a
        head-by-head grep would give the disabled Virtual-3 the position of the
        head printed after it.  The three heads here are listed 3, 2, 1 --
        wlroots' own enumeration order -- and Virtual-3 is the disabled one, so
        that mistake is visible as `Virtual-3 1280,0`."""
        self.answering(read(VMFIX, "wlr-randr-0.4.1-virtual-3heads-one-disabled.txt"))
        got = self.mod.wlr()
        self.assertNotIn("Virtual-3", " ".join(got))
        self.assertEqual(len(got), 2, got)

    def test_all_three_heads_answer_when_none_is_disabled(self):
        self.answering(read(VMFIX, "wlr-randr-0.4.1-3heads.txt"))
        self.assertEqual(sorted(self.mod.wlr()),
                         ["HEADLESS-1 0,0", "HEADLESS-2 1280,0", "HEADLESS-3 2560,0"])

    # -- Hyprland ------------------------------------------------------------

    def test_hyprctl_monitors_answers_name_and_top_level_x_y(self):
        """Hyprland puts the position at the top level of the monitor object
        and not in a `rect`, which is the one thing a reader written from
        sway's JSON gets wrong."""
        self.answering(read(VMFIX, "hyprctl-monitors-2heads.json"))
        self.assertEqual(self.mod.hypr(), ["Virtual-1 0,0", "HEADLESS-2 1920,0"])

    def test_a_disabled_hypr_monitor_is_not_an_enabled_one(self):
        """Derived from the recording above by setting `"disabled": true` on the
        second monitor: hyprctl still lists it, and an oracle that says a head
        is at 1920,0 while the compositor has it off would make every
        `--output ... --off` check pass on the head that stayed."""
        data = json.loads(read(VMFIX, "hyprctl-monitors-2heads.json"))
        data[1]["disabled"] = True
        self.answering(json.dumps(data))
        self.assertEqual(self.mod.hypr(), ["Virtual-1 0,0"])

    # -- COSMIC --------------------------------------------------------------

    def test_cosmic_randr_kdl_reads_position_and_not_physical(self):
        """The fixture's second head has `physical 480 270` and `position 1920
        0`, in that order, so a parser matching the first pair of integers in
        the block answers 480,270."""
        self.answering(read(VMFIX, "cosmic-randr-2heads-one-disabled.kdl"))
        self.assertEqual(self.mod.cosmic(), ["Virtual-1 0,0", "Virtual-2 1920,0"])

    def test_a_cosmic_output_with_enabled_false_has_no_position_and_no_line(self):
        text = read(VMFIX, "cosmic-randr-2heads-one-disabled.kdl")
        self.assertIn('output "Virtual-3" enabled=#false', text)
        self.answering(text)
        self.assertNotIn("Virtual-3", " ".join(self.mod.cosmic()))

    def test_the_recorded_winit_document_is_one_line(self):
        self.answering(read(VMFIX, "cosmic-randr-winit-1head.kdl"))
        self.assertEqual(self.mod.cosmic(), ["WINIT-0 0,0"])

    # -- Cinnamon / Muffin ---------------------------------------------------

    def test_muffin_getcurrentstate_is_read_out_of_the_gdbus_text_form(self):
        """The branch that runs where python3-gi is not installed.  The recorded
        shape carries `uint32 0` for the transform and `@a{sv} {}` for an empty
        dictionary, because gdbus prints with type_annotate=TRUE -- measured on
        this host, tests/fixtures/live/README.md says how."""
        self.without_gi()
        self.answering(read(LIVEFIX, "oracle-muffin-getcurrentstate.txt"))
        self.assertEqual(self.mod.muffin(), ["LVDS1 0,0"])

    def test_muffin_and_gnome_are_one_reader_under_two_sets_of_names(self):
        """`org.cinnamon.Muffin.DisplayConfig`'s GetCurrentState signature is
        byte-for-byte Mutter's [recon2/cinnamon 3.1], so the same text parses
        the same way and only the bus name, path and interface differ."""
        self.without_gi()
        text = read(LIVEFIX, "oracle-muffin-getcurrentstate.txt")
        self.answering(text)
        self.assertEqual(self.mod.gnome(), self.mod.muffin())
        asked = []
        self.mod.run = lambda *argv: asked.append(argv) or text
        self.mod.muffin()
        self.assertIn("org.cinnamon.Muffin.DisplayConfig", asked[-1])
        self.assertIn("/org/cinnamon/Muffin/DisplayConfig", asked[-1])
        self.assertIn("org.cinnamon.Muffin.DisplayConfig.GetCurrentState", asked[-1])

    # -- Wayfire -------------------------------------------------------------

    def test_wayfire_outputs_come_from_geometry_and_not_from_workarea(self):
        """Both recorded outputs have `workarea` at 0,0; only `geometry` carries
        the layout position, so a reader on the wrong key says both heads are at
        the origin and every `--right-of` check in the smoke passes on it."""
        data = json.loads(read(LIVEFIX, "oracle-wayfire-list-outputs.json"))
        self.assertEqual(self.mod.wayfire_lines(data),
                         ["HEADLESS-1 0,0", "HEADLESS-2 1280,0"])

    def test_the_wayfire_client_frames_a_request_as_int32_le_length_then_json(self):
        """Wayfire's IPC is a 4-byte little-endian length and that many bytes of
        JSON, both ways [recon2/wayfire 1.1].  A socketpair is the server here,
        so what is exercised is the framing itself and not a stub of it."""
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        body = json.dumps([{"name": "Virtual-1", "geometry": {"x": 7, "y": 9}}]).encode()
        theirs.sendall(len(body).to_bytes(4, "little") + body)
        got = self.mod.wayfire_call(ours, "window-rules/list-outputs")
        self.assertEqual(self.mod.wayfire_lines(got), ["Virtual-1 7,9"])
        head = theirs.recv(4)
        asked = json.loads(theirs.recv(int.from_bytes(head, "little")).decode())
        self.assertEqual(int.from_bytes(head, "little"),
                         len(json.dumps(asked).encode()))
        self.assertEqual(asked["method"], "window-rules/list-outputs")

    def test_the_wayfire_socket_is_looked_for_where_the_tools_look(self):
        """$WAYFIRE_SOCKET first, then the `wayfire-*.socket` glob -- the order
        w11common.session.find_wayfire_socket uses.  The measured name has an
        empty pid field (`wayfire-wayland-1-.socket`), so nothing may match on
        one [recon2/wayfire 1.2]."""
        # `mock.patch.dict` and not `self.mod.os.environ = env`: oracle.py's `os`
        # IS the interpreter's `os`, so that assignment replaced os.environ for
        # the whole process with a plain dict -- and the cleanup that put it back
        # read `os.environ` AFTER the assignment, so what it restored was the
        # plain dict.  A plain dict never reaches `putenv`, so from that point on
        # every child spawned with the inherited environment saw a stale one:
        # under `unittest discover` this file left tests/test_wmirror_lifetime.py
        # with four failures and tests/test_overlap_consent.py with two errors,
        # both of them green run alone.  patch.dict restores the object it
        # replaced values in, so there is nothing to get wrong.
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env.pop("WAYFIRE_SOCKET", None)
            env.pop("_WAYFIRE_SOCKET", None)
            env["XDG_RUNTIME_DIR"] = tmp
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertIsNone(self.mod.wayfire_socket())
                path = os.path.join(tmp, "wayfire-wayland-1-.socket")
                open(path, "w").close()
                self.assertEqual(self.mod.wayfire_socket(), path)
                other = os.path.join(tmp, "named-by-the-variable")
                open(other, "w").close()
                os.environ["WAYFIRE_SOCKET"] = other
                self.assertEqual(self.mod.wayfire_socket(), other)

    # -- the table itself ----------------------------------------------------

    def test_every_desktop_token_vmctl_knows_has_an_oracle(self):
        """A token with no branch exits 2 (below), which is the right answer --
        but a token vmctl will happily build a flavor for and this file has no
        reader for is a flavor whose display phases cannot run at all."""
        block = read(VMCTL).split("DESKTOPS = {", 1)[1].split("\n}", 1)[0]
        tokens = re.findall(r'^\s*"([a-z0-9-]+)":\s*dict\(', block, re.M)
        self.assertGreater(len(tokens), 5, tokens)
        self.assertEqual([t for t in tokens if t not in self.mod.DESKTOPS], [])

    def test_an_unknown_token_exits_2_and_names_the_set(self):
        """Not "prints nothing and exits 0": `oracle_outputs` pipes this through
        a grep, so a silent empty answer would make display_pair() report one
        head and every position check pass by default."""
        got = subprocess.run([sys.executable, ORACLE, "plan9"],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 2, got.stderr)
        self.assertIn("no oracle for plan9", got.stderr)
        self.assertIn("sway", got.stderr)

    def test_a_tool_that_is_not_installed_exits_2_and_names_itself(self):
        """The other silent-empty-answer, and the one a new flavor actually
        hits: wlr-randr is only *Suggested* by labwc [recon2/labwc] and
        hyprctl and cosmic-randr reach a guest through its flavor's EXTRA_PKGS.
        Before this, a missing binary was caught as an OSError and answered
        with "", which `oracle_outputs` reads as "no enabled output" -- so the
        display phase would pass against nothing on exactly the guest where the
        reader is absent.  Run with a PATH that holds no binaries at all."""
        env = dict(os.environ, PATH=os.path.join(ROOT, "nonexistent-bin"))
        got = subprocess.run([sys.executable, ORACLE, "river"],
                             capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(got.returncode, 2, (got.stdout, got.stderr))
        self.assertEqual(got.stdout, "")
        self.assertIn("wlr-randr", got.stderr)
        self.assertIn("not installed", got.stderr)

    def test_the_x11_desktops_all_answer_through_xrandr(self):
        """gnome-x11, cinnamon, mate, i3 and lxqt are five different desktops
        and one X server; `xrandr --query` is the only reader any of them has
        that is not the tool under test."""
        for token in ("gnome-x11", "cinnamon", "mate", "i3", "lxqt", "xfce"):
            with self.subTest(token):
                self.assertIs(self.mod.DESKTOPS[token], self.mod.x11)

    def test_xrandr_reads_the_geometry_word_and_not_the_word_after_connected(self):
        """`primary` sits between `connected` and the geometry on one output and
        not on the others, which is why the parser scans for the first
        WxH+X+Y-shaped word rather than counting fields."""
        self.answering(
            "Screen 0: minimum 16 x 16, current 3200 x 1080, maximum 32767 x 32767\n"
            "Virtual-1 connected primary 1920x1080+0+0 (normal left inverted) 0mm x 0mm\n"
            "   1920x1080     60.00*+\n"
            "Virtual-2 connected 1280x720+1920+0 (normal left inverted) 0mm x 0mm\n"
            "Virtual-3 disconnected (normal left inverted right x axis y axis)\n")
        self.assertEqual(self.mod.x11(), ["Virtual-1 0,0", "Virtual-2 1920,0"])


class TheStepFiles(unittest.TestCase):
    """R13.  Every vm/live-smoke.d/<token>.sh, sourced the way the driver
    sources it, with the driver's own helpers stubbed out."""

    #: What live-smoke.sh defines and a step file may call at source time or
    #: inside a phase.  Stubbed to nothing: what is being read here is the
    #: SHAPE of the file, not what its checks decide.
    HELPERS = ("pass fail note step want wantnot same ok xwant ev guest guestq root shot "
               "await win_geom oracle_outputs opos display_pair beside sq editor_text "
               "head_dark plasma_major install_oracle arm_busrec after_reboot "
               "wait_session pkg_files pkg_deploy pkg_install_cmd pkg_remove_cmd "
               "pkg_left_behind pkg_banner_re")

    def source(self, token, tail):
        """Source common.sh and the step file in a bash with the driver stubbed,
        then run `tail`.  Sliced from the shipped tree, never copied."""
        pre = "set -u\n"
        pre += 'STEPS=%s\nDESKTOP=%s\nDISTRO=ubuntu\nMODE=pkg\nREUSE=0\n' % (STEPS, token)
        pre += 'VM=/nonexistent/vmctl\nNAME=t\nFLAVOR=t\nREPO=%s\nHEADS=2\nSCALE=0\n' % ROOT
        for name in self.HELPERS.split():
            pre += '%s() { :; }\n' % name
        pre += '. "$STEPS/common.sh"\n. "$STEPS/%s.sh"\n' % token
        return subprocess.run(["bash", "-c", pre + tail],
                              capture_output=True, text=True, timeout=60)

    def test_every_step_file_parses(self):
        for token in step_tokens() + list(NOT_A_DESKTOP):
            with self.subTest(token):
                got = subprocess.run(["bash", "-n", os.path.join(STEPS, token + ".sh")],
                                     capture_output=True, text=True, timeout=60)
                self.assertEqual(got.returncode, 0, got.stderr)

    def test_every_step_file_defines_the_two_hooks_the_driver_needs(self):
        """live-smoke.sh reads SMOKE_PHASES to know what to run and
        EDITOR_CLASS to find the window every window phase acts on.  A file
        missing either fails at the first phase with `set -u`, half a boot in."""
        for token in step_tokens():
            with self.subTest(token):
                got = self.source(token, 'echo "P:$SMOKE_PHASES"\necho "E:$EDITOR_CLASS"\n')
                self.assertEqual(got.returncode, 0, got.stderr)
                phases = [ln[2:] for ln in got.stdout.splitlines() if ln.startswith("P:")]
                editor = [ln[2:] for ln in got.stdout.splitlines() if ln.startswith("E:")]
                self.assertTrue(phases and phases[0].split(), got.stdout)
                self.assertTrue(editor and editor[0].strip(), got.stdout)

    def test_every_phase_a_step_file_names_exists_as_a_function(self):
        """The driver's own answer to a phase it cannot find is one line on
        stderr and a FAIL, minutes into a run that has already booted a guest.
        This is that check, before the boot."""
        for token in step_tokens():
            with self.subTest(token):
                got = self.source(token, 'for p in $SMOKE_PHASES; do\n'
                                         '  declare -F "phase_$p" >/dev/null || echo "MISSING $p"\n'
                                         'done\n')
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual([ln for ln in got.stdout.splitlines()
                                  if ln.startswith("MISSING")], [], got.stdout)

    def test_the_two_distro_phases_the_driver_appends_exist(self):
        """`distro_phases` in live-smoke.sh adds `selinux` and `pkgverify` to
        whatever the step file asked for, so those two have to exist for every
        desktop and not only for the one that made them necessary."""
        for token in step_tokens():
            with self.subTest(token):
                got = self.source(token, 'for p in selinux pkgverify; do\n'
                                         '  declare -F "phase_$p" >/dev/null || echo "MISSING $p"\n'
                                         'done\n')
                self.assertEqual(got.stdout.strip(), "", got.stdout)

    def test_every_xwant_label_names_the_fix_or_the_event_it_waits_for(self):
        """The convention the plan pins (C2): `xwant "<check> (fix Uxx)"` for a
        code fix in flight and `xwant "<check> (until <event>)"` for a
        measurement nobody has taken.  An `xwant` is neither a pass nor a fail,
        so a label that does not say what it is waiting for is a check that can
        sit XFAIL forever with nobody able to tell whether it still should."""
        pattern = re.compile(r'^\s*xwant "[^"]*\((fix|until) ')
        bad = []
        for name in sorted(os.listdir(STEPS)):
            if not name.endswith(".sh"):
                continue
            for n, line in enumerate(read(STEPS, name).splitlines(), 1):
                if re.match(r"\s*xwant ", line) and not pattern.match(line):
                    bad.append("%s:%d %s" % (name, n, line.strip()))
        self.assertEqual(bad, [])

    def test_every_desktop_a_flavor_names_has_a_step_file(self):
        """The driver refuses a flavor whose desktop has no step file, which is
        the right refusal and comes minutes late.  A DESKTOPS row with no flavor
        yet is legal -- batch 2 landed all nineteen before the flavors -- so the
        rule is stated over the desktops some yaml actually names."""
        want = sorted({d for d in flavor_desktops().values() if d})
        self.assertEqual([d for d in want if d not in step_tokens()], [])


class TheDriversPackageAxis(unittest.TestCase):
    """R14.  `--pkg` and its `--deb` alias, and the four per-distro tables, run
    as the shell functions they are."""

    def run_slice(self, body, distro=None, env=None):
        pre = "set -u\n"
        if distro:
            pre += 'DISTRO=%s\n' % distro
        pre += 'DEB=/repo/release/w11_0.4.0_all.deb\nVERSION=0.4.0\n'
        pre += 'LIVE_SMOKE_RPMS=/rpms\nLIVE_SMOKE_PKG=/pkgs/w11-0.4.0-1-any.pkg.tar.zst\n'
        for name in ("pkg_files", "pkg_install_cmd", "pkg_remove_cmd",
                     "pkg_left_behind", "pkg_banner_re", "distro_phases"):
            pre += support.sh_function(DRIVER, name)
        run_env = dict(os.environ)
        run_env.update(env or {})
        return subprocess.run(["bash", "-c", pre + body], capture_output=True,
                              text=True, timeout=60, env=run_env)

    def out(self, body, distro=None, env=None):
        got = self.run_slice(body, distro, env)
        self.assertEqual(got.returncode, 0, got.stderr)
        return got.stdout.strip()

    # -- the option, two spellings -------------------------------------------

    def option_loop(self, argv):
        """The driver's own option loop, sliced from the shipped file and given
        `argv`.  A copy of it here would pass while the script did anything."""
        block = support.sh_block(DRIVER, 'NAME=""; MODE=tree;', "\ndone\n")
        body = ("set -u\nusage() { :; }\n" + block
                + '\necho "MODE=$MODE REMOVE=$REMOVE"\n')
        return subprocess.run(["bash", "-c", body, "x"] + argv,
                              capture_output=True, text=True, timeout=60)

    def test_pkg_and_deb_are_one_mode(self):
        """--deb is what every note in vm/live-smoke.out/ and every habit says;
        --pkg is what it means now that three of the four packagings are not
        Debian's.  Keeping both is one `|` in the case arm, and dropping either
        would silently change what a recorded invocation measured."""
        for spelling in ("--pkg", "--deb"):
            with self.subTest(spelling):
                got = self.option_loop([spelling])
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertIn("MODE=pkg", got.stdout)
        got = self.option_loop([])
        self.assertIn("MODE=tree", got.stdout)

    def test_help_is_answered_before_the_flavor_is_looked_up(self):
        """$1 is the flavor, which made `--help` print "no flavor --help
        (vm/flavors/--help.yaml)" and exit 2, with the `-h|--help` arm of the
        option loop unreachable.  Both spellings now print the header comment
        and exit 0; no flavor at all still prints it and exits 2, which is what
        tells a script the invocation was wrong."""
        for argv, rc in ((["--help"], 0), (["-h"], 0), ([], 2)):
            with self.subTest(argv):
                got = subprocess.run(["bash", DRIVER] + argv, capture_output=True,
                                     text=True, timeout=60)
                self.assertEqual(got.returncode, rc, got.stdout[-500:] + got.stderr[-500:])
                self.assertIn("live-smoke.sh -- the hand smoke", got.stdout)
                self.assertNotIn("no flavor", got.stderr)

    def test_an_unknown_option_is_refused_with_status_2(self):
        got = self.option_loop(["--rpm"])
        self.assertEqual(got.returncode, 2, got.stdout)
        self.assertIn("unknown option --rpm", got.stderr)

    # -- the distro header ---------------------------------------------------

    def test_the_distro_comes_from_the_flavors_own_header(self):
        """The same `sed -n 's/^#[[:space:]]*vmctl-<key>:...'` idiom vmctl,
        selftest.sh and ci-golden.sh share, sliced out of the driver and run
        over a temp yaml -- so a flavor that says `fedora` gets dnf and one that
        says nothing at all still gets apt."""
        block = support.sh_block(DRIVER, "DISTRO=$(sed -n", "DISTRO=${DISTRO:-ubuntu}")
        with tempfile.TemporaryDirectory() as tmp:
            for name, text, want in (
                    ("f.yaml", "#cloud-config\n# vmctl-distro: fedora\n", "fedora"),
                    ("a.yaml", "#cloud-config\n#  vmctl-distro:   arch\n", "arch"),
                    ("u.yaml", "#cloud-config\n# vmctl-desktop: gnome\n", "ubuntu")):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
                body = ('set -u\nHERE=%s\nFLAVOR=%s\n' % (tmp, name[:-5])
                        + block.replace('"$HERE/flavors/$FLAVOR.yaml"',
                                        '"$HERE/$FLAVOR.yaml"')
                        + '\necho "$DISTRO"\n')
                got = subprocess.run(["bash", "-c", body], capture_output=True,
                                     text=True, timeout=60)
                with self.subTest(name):
                    self.assertEqual(got.stdout.strip(), want, got.stderr)

    def test_every_flavor_in_the_tree_names_a_distro_the_driver_has_an_arm_for(self):
        known = {"ubuntu", "fedora", "arch", "nixos"}
        for name in sorted(os.listdir(FLAVORS)):
            if not name.endswith(".yaml"):
                continue
            found = re.findall(r"^#\s*vmctl-distro:\s*(\S+)", read(FLAVORS, name), re.M)
            with self.subTest(name):
                self.assertTrue(set(found) <= known, found)

    # -- the four tables -----------------------------------------------------

    def test_the_package_resolves_per_distro(self):
        self.assertEqual(self.out("pkg_files", "ubuntu"),
                         "/repo/release/w11_0.4.0_all.deb")
        self.assertEqual(self.out("pkg_files", "arch"),
                         "/pkgs/w11-0.4.0-1-any.pkg.tar.zst")
        self.assertEqual(self.out("pkg_files", "nixos"), "")

    def test_the_rpms_are_the_ones_of_this_version_and_nothing_else(self):
        """Three noarch rpms come out of one build, named after the subpackages,
        so the NAME half has to be a glob -- but the VERSION half must not be.
        scripts/build-rpm.sh copies every RPMS/noarch/*.rpm into dist/ and never
        clears it, so a directory that still holds 0.3.9's three rpms would hand
        `dnf install` two versions of each name and fail.  The deb and pkg arms
        are pinned to $VERSION already; this is that same pin."""
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("w11-0.4.0-1.fc44.noarch.rpm",
                      "gnome-shell-extension-w11-bridge-0.4.0-1.fc44.noarch.rpm",
                      "w11-0.3.9-1.fc44.noarch.rpm",
                      "not-a-package.txt"):
                open(os.path.join(tmp, n), "w").close()
            got = self.out("LIVE_SMOKE_RPMS=%s; pkg_files" % tmp, "fedora")
            self.assertIn("w11-0.4.0-1.fc44.noarch.rpm", got)
            self.assertIn("gnome-shell-extension-w11-bridge-0.4.0-1.fc44.noarch.rpm", got)
            self.assertNotIn("0.3.9", got)
            self.assertNotIn("not-a-package.txt", got)
        self.assertEqual(self.out("LIVE_SMOKE_RPMS=/nowhere; pkg_files", "fedora"), "")

    def test_each_manager_installs_with_its_own_command(self):
        self.assertIn("apt-get install -y ./w11.deb", self.out("pkg_install_cmd", "ubuntu"))
        self.assertIn("dnf install -y", self.out("pkg_install_cmd", "fedora"))
        self.assertIn("pacman -U --noconfirm", self.out("pkg_install_cmd", "arch"))
        self.assertEqual(self.out("pkg_install_cmd", "nixos"), "")

    def test_the_bridge_subpackage_is_named_and_not_left_to_supplements(self):
        """`Supplements: gnome-shell` only fires where gnome-shell is installed,
        and fedora44-sway has none -- so a `dnf install ./w11-*.rpm`
        alone would leave the bridge out of exactly the image whose smoke does
        not need it and, worse, out of any image dnf decides differently about."""
        got = self.out("pkg_install_cmd", "fedora")
        self.assertIn("gnome-shell-extension-w11-bridge-", got)
        self.assertIn("gnome-shell-extension-w11-overlap-", got)

    def test_each_manager_removes_with_its_own_command(self):
        self.assertIn("apt-get remove -y w11", self.out("pkg_remove_cmd", "ubuntu"))
        self.assertIn("dnf remove -y w11", self.out("pkg_remove_cmd", "fedora"))
        self.assertIn("pacman -R --noconfirm w11", self.out("pkg_remove_cmd", "arch"))
        self.assertIn("switch-to-configuration test", self.out("pkg_remove_cmd", "nixos"))
        self.assertIn("without-w11", self.out("pkg_remove_cmd", "nixos"))

    def test_the_left_behind_table_names_each_packagings_own_libexec_path(self):
        """The one path that differs between packagings.  Checking the rpm's
        removal against `/usr/lib/w11` would pass on every rpm ever
        built, because nothing has ever put a file there."""
        self.assertIn("/usr/lib/w11", self.out("pkg_left_behind", "ubuntu").split())
        self.assertIn("/usr/lib/w11", self.out("pkg_left_behind", "arch").split())
        self.assertIn("/usr/libexec/w11", self.out("pkg_left_behind", "fedora").split())
        self.assertNotIn("/usr/lib/w11", self.out("pkg_left_behind", "fedora").split())

    def test_the_left_behind_table_names_both_extensions_everywhere(self):
        for distro in ("ubuntu", "fedora", "arch", "nixos"):
            with self.subTest(distro):
                got = self.out("pkg_left_behind", distro)
                self.assertIn("w11-bridge@w11", got)
                self.assertIn("w11-overlap@w11", got)

    def test_nixos_owns_no_path_under_usr_at_all(self):
        """There is no /usr on NixOS; the extensions live in the system profile
        and the stamp and the autostart entry belong to a package manager NixOS
        does not have."""
        got = self.out("pkg_left_behind", "nixos")
        self.assertNotIn("/usr/", got)
        self.assertNotIn("/var/lib/w11", got)
        self.assertIn("/run/current-system/sw/share/gnome-shell/extensions", got)

    def test_only_the_packagings_that_print_a_banner_are_checked_for_one(self):
        """dpkg's postinst and pacman's .install carry the same paragraph; the
        rpm carries none, because Fedora discourages chatty scriptlets and the
        spec ships README.Fedora instead [recon2/pkg-rpm].  A banner regex
        applied to the rpm would fail every Fedora run for a sentence nobody
        decided to print."""
        self.assertEqual(self.out("pkg_banner_re", "ubuntu"), "LOG OUT AND BACK IN ONCE")
        self.assertEqual(self.out("pkg_banner_re", "arch"), "LOG OUT AND BACK IN ONCE")
        self.assertEqual(self.out("pkg_banner_re", "fedora"), "")
        self.assertEqual(self.out("pkg_banner_re", "nixos"), "")

    def test_the_banner_string_is_the_one_the_scriptlets_print(self):
        """The regex is taken from the DRIVER and looked for in the scriptlets,
        rather than named a second time here: the pair of tests then says both
        halves of the claim.  The one above pins what the driver answers, this
        one pins that a scriptlet still prints it -- so rewording the paragraph
        in debian/w11.postinst or packaging/arch/w11.install
        turns this red instead of leaving the smoke waiting for a sentence
        nobody prints any more."""
        for distro, path in (("ubuntu", "debian/w11.postinst"),
                             ("arch", "packaging/arch/w11.install")):
            with self.subTest(path):
                banner = self.out("pkg_banner_re", distro)
                self.assertTrue(banner, "the driver answers no banner for %s" % distro)
                self.assertIn(banner, read(ROOT, path))

    def test_the_distro_phases_are_appended_only_where_they_mean_something(self):
        self.assertEqual(self.out("distro_phases", "fedora").split(), ["selinux", "pkgverify"])
        self.assertEqual(self.out("distro_phases", "arch").split(), ["pkgverify"])
        self.assertEqual(self.out("distro_phases", "ubuntu"), "")
        self.assertEqual(self.out("distro_phases", "nixos"), "")

    def nixos_remove(self):
        """phase_remove on a NixOS flavor, with `root` echoing every command it
        is handed and `guest` answering the two questions the arm asks: the
        default system's store path (before the switch) and where wdotool is."""
        pre = "set -u\nDISTRO=nixos\nMODE=pkg\nDEB=/x.deb\nVERSION=0.4.0\n"
        pre += "LIVE_SMOKE_RPMS=/rpms\nLIVE_SMOKE_PKG=/p.pkg.tar.zst\n"
        for name in ("pkg_files", "pkg_install_cmd", "pkg_remove_cmd",
                     "pkg_left_behind", "pkg_banner_re"):
            pre += support.sh_function(DRIVER, name)
        pre += ('root() { echo "ROOT[$1]" >&2; }\n'
                'guest() { case "$1" in *readlink*) echo "%s" ;; '
                '*) echo "/run/current-system/sw/bin/wdotool" ;; esac; }\n'
                'pkg_deploy() { echo "DEPLOYED" >&2; }\n'
                'ok() { :; }\nwant() { :; }\nwantnot() { :; }\nsame() { :; }\n'
                'note() { echo "NOTE[$1]" >&2; }\npass() { :; }\nfail() { :; }\n'
                'ev() { printf "%%s" "$*"; }\n'
                'VM=/nonexistent\nNAME=t\n') % self.DEFSYS
        pre += support.sh_function(DRIVER, "phase_remove") + "\nphase_remove\n"
        got = subprocess.run(["bash", "-c", pre], capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        return got.stderr

    #: What `readlink -f /run/current-system` answers on a NixOS guest before
    #: anything is switched: the DEFAULT system's toplevel, the one that carries
    #: `specialisation/without-w11`.
    DEFSYS = "/nix/store/0000000000000000000000000000000-nixos-system-w11-26.05"

    def test_phase_remove_never_runs_an_empty_command_on_nixos(self):
        """There is nothing to install on NixOS, so `pkg_install_cmd` is empty
        -- and a phase that ran it anyway would hand the guest an empty string,
        get status 0 back and report a re-install that never happened.  The arm
        switches back into the default specialisation instead, which is what
        putting the package back actually is there."""
        err = self.nixos_remove()
        self.assertNotIn("ROOT[]", err)
        self.assertIn("without-w11/bin/switch-to-configuration test", err)
        self.assertNotIn("DEPLOYED", err)

    def test_the_nixos_switch_back_goes_through_the_path_captured_before_the_switch(self):
        """The bug this pins: NixOS activation ends in `ln -sfn "$(readlink -f
        "$systemConfig")" /run/current-system` and runs that for `test` exactly
        as for `switch` [nixpkgs nixos/modules/system/activation/
        activation-script.nix:79, read in the local store 2026-09-08].  So the
        moment the removal switches into without-w11, /run/current-system
        IS that child -- which has no `specialisation/` of its own, the fact
        phase_install asserts -- and a "switch back" spelled
        /run/current-system/bin/switch-to-configuration would re-activate the
        child and leave the tools off PATH on every nixos flavor."""
        err = self.nixos_remove()
        self.assertIn("ROOT[%s/bin/switch-to-configuration test 2>&1]" % self.DEFSYS, err)
        self.assertNotIn("ROOT[/run/current-system/bin/switch-to-configuration", err)
        # the capture happens BEFORE the removal command is handed to root
        self.assertLess(err.index("NOTE[the default system to switch back into"),
                        err.index("ROOT[/run/current-system/specialisation/"), err)

    def test_phase_remove_installs_the_package_again_on_a_package_manager(self):
        """The other side of the same arm: on Ubuntu the phase copies the
        package back in and runs the install, because a removal drops the stamp
        and the banner is owed again."""
        pre = "set -u\nDISTRO=ubuntu\nMODE=pkg\nDEB=/x.deb\nVERSION=0.4.0\n"
        pre += "LIVE_SMOKE_RPMS=/rpms\nLIVE_SMOKE_PKG=/p.pkg.tar.zst\n"
        for name in ("pkg_files", "pkg_install_cmd", "pkg_remove_cmd",
                     "pkg_left_behind", "pkg_banner_re"):
            pre += support.sh_function(DRIVER, name)
        pre += ('root() { echo "ROOT[$1]" >&2; }\nguest() { :; }\n'
                'pkg_deploy() { echo "DEPLOYED" >&2; }\n'
                'ok() { :; }\nwant() { echo "WANT[$1]" >&2; }\nwantnot() { :; }\nsame() { :; }\n'
                'note() { :; }\npass() { :; }\nfail() { :; }\nev() { printf "%s" "$*"; }\n'
                'VM=/nonexistent\nNAME=t\n')
        pre += support.sh_function(DRIVER, "phase_remove") + "\nphase_remove\n"
        got = subprocess.run(["bash", "-c", pre], capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("DEPLOYED", got.stderr)
        self.assertIn("apt-get remove -y w11", got.stderr)
        self.assertIn("apt-get install -y ./w11.deb", got.stderr)
        self.assertIn("WANT[installing again prints the first-install banner again]", got.stderr)

    def remove_under_errexit(self, distro, rc):
        """phase_remove, sliced, run under the driver's own `set -euo pipefail`
        with every guest command exiting `rc`.  What is collected is which
        checks actually printed."""
        pre = "set -euo pipefail\nDISTRO=%s\nMODE=pkg\nDEB=/x.deb\nVERSION=0.4.0\n" % distro
        pre += "LIVE_SMOKE_RPMS=/rpms\nLIVE_SMOKE_PKG=/p.pkg.tar.zst\n"
        for name in ("pkg_files", "pkg_install_cmd", "pkg_remove_cmd",
                     "pkg_left_behind", "pkg_banner_re"):
            pre += support.sh_function(DRIVER, name)
        pre += ('root() { return %d; }\nguest() { return %d; }\n' % (rc, rc)
                + 'pkg_deploy() { :; }\nok() { echo "CHECK $1"; }\nwant() { echo "CHECK $1"; }\n'
                'wantnot() { echo "CHECK $1"; }\nsame() { echo "CHECK $1"; }\n'
                'note() { :; }\npass() { :; }\nfail() { :; }\nev() { printf "%s" "$*"; }\n'
                'VM=/nonexistent\nNAME=t\n')
        pre += support.sh_function(DRIVER, "phase_remove")
        pre += '\nphase_remove\necho "REACHED THE END"\n'
        got = subprocess.run(["bash", "-c", pre], capture_output=True, text=True, timeout=60)
        return got

    def test_a_clean_removal_still_runs_every_check_in_the_phase(self):
        """The empty answer is the PASSING one for four of these checks, and the
        commands that produce it exit nonzero: `ls -d` with nothing to list is
        2, `grep -l` with no match is 1, `command -v` for a name that is gone is
        1.  Under the driver's `set -euo pipefail` an unguarded `$( ... )` makes
        the phase abort at the first of them -- so a package that removed itself
        perfectly would print one check and then nothing, and the driver's
        `(phase remove returned 1 -- checks above stand)` line would be the only
        sign.  Every substitution in the phase ends in `|| true` for that."""
        got = self.remove_under_errexit("ubuntu", 1)
        checks = [ln[6:] for ln in got.stdout.splitlines() if ln.startswith("CHECK ")]
        self.assertIn("REACHED THE END", got.stdout, got.stdout + got.stderr)
        self.assertIn("every path the package owned is gone", checks)
        self.assertIn("no tool of ours is on PATH any more", checks)
        self.assertIn("no ACL entry is left on /dev/uinput", checks)
        self.assertIn("no uaccess tag is left for the node", checks)
        self.assertIn("byte-compilation left no __pycache__ outside dist-packages", checks)
        self.assertGreaterEqual(len(checks), 7, checks)

    def test_the_same_checks_run_when_every_command_succeeds(self):
        """The control: nothing about the guarding may depend on the status."""
        quiet = self.remove_under_errexit("ubuntu", 0)
        noisy = self.remove_under_errexit("ubuntu", 1)
        self.assertEqual([ln for ln in quiet.stdout.splitlines() if ln.startswith("CHECK ")],
                         [ln for ln in noisy.stdout.splitlines() if ln.startswith("CHECK ")])

    # -- the guard on --remove -----------------------------------------------

    def test_remove_without_pkg_prints_the_skip_note_and_runs_nothing(self):
        """The default mode puts the tree's zipapps in /usr/local/bin, which no
        package manager owns, so a removal there would report files left behind
        that were never the package's."""
        block = support.sh_block(DRIVER, 'if [ "$REMOVE" = 1 ]; then', "\nfi\n")
        body = ('set -u\nREMOVE=1\nMODE=tree\nDISTRO=ubuntu\n'
                'note() { echo "NOTE $*"; }\nstep() { echo "STEP $*"; }\n'
                'shot() { :; }\nphase_remove() { echo "RAN phase_remove"; }\n' + block)
        got = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("--remove needs --pkg", got.stdout)
        self.assertNotIn("RAN phase_remove", got.stdout)
        body = body.replace("MODE=tree", "MODE=pkg")
        got = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=60)
        self.assertIn("RAN phase_remove", got.stdout)


class TheDistroPhases(unittest.TestCase):
    """The two phases live-smoke.sh appends for a distro rather than a desktop,
    sliced out of common.sh and run under the driver's own shell options."""

    COMMON = os.path.join(STEPS, "common.sh")

    def phase(self, name, distro, guest_out, guest_rc=0, mode="pkg"):
        """`guest_out` is one answer for every `root` call, or a tuple of
        `(glob, answer)` rules matched against the command `root` was handed --
        which is how a two-check phase is told apart from a phase that ran one
        check twice.  A counter would not do it: every call site here is a
        `$( ... )` substitution, so each `root` runs in its own subshell and
        nothing it assigns survives.  SELinux's mode and its AVC count are
        different questions and must not be answered with the same string."""
        # The globs go into a `case` unquoted, so they carry no spaces: match on
        # a word that only one of the phase's commands has (`AVC`, `getenforce`).
        rules = ((("*", guest_out),) if isinstance(guest_out, str)
                 else tuple(guest_out) + (("*", ""),))
        pre = "set -euo pipefail\nDISTRO=%s\nMODE=%s\n" % (distro, mode)
        pre += "root() { case \"$1\" in\n"
        for pat, ans in rules:
            pre += "  %s) printf '%%s\\n' %s ;;\n" % (pat, shlex.quote(ans))
        pre += "  esac\n  return %d; }\n" % guest_rc
        pre += ('same() { echo "SAME|$1|$2|$3"; }\nnote() { :; }\n'
                'pass() { echo "PASS|$1"; }\nfail() { echo "FAIL|$1"; }\n'
                'ev() { printf "%s" "$*"; }\n')
        pre += support.sh_function(self.COMMON, name) + "\n%s\necho DONE\n" % name
        got = subprocess.run(["bash", "-c", pre], capture_output=True, text=True, timeout=60)
        self.assertIn("DONE", got.stdout, got.stdout + got.stderr)
        return got.stdout

    def test_selinux_runs_both_checks_when_the_avc_count_is_zero(self):
        """`grep -c` exits 1 when it counts zero, which is the passing case, and
        an unguarded substitution would abort the phase there -- so the phase
        would print `Enforcing` and then say nothing about denials at all.  The
        two answers are the two real ones, in order, so the second `same` is
        checked to be comparing "0" against "0" and not against the mode."""
        out = self.phase("phase_selinux", "fedora",
                         (("*getenforce*", "Enforcing"), ("*AVC*", "0"), ("*wc*", "37")),
                         guest_rc=1)
        lines = [ln for ln in out.splitlines() if ln.startswith("SAME|")]
        self.assertEqual(len(lines), 2, out)
        self.assertEqual(lines[0], "SAME|SELinux is Enforcing (the state every Fedora "
                                   "measurement was taken in)|Enforcing|Enforcing")
        self.assertTrue(lines[1].endswith("|0|0"), lines[1])
        self.assertIn("no SELinux denial", lines[1])

    def test_selinux_compares_getenforce_against_enforcing(self):
        out = self.phase("phase_selinux", "fedora", "Permissive")
        first = [ln for ln in out.splitlines() if ln.startswith("SAME|")][0]
        self.assertTrue(first.endswith("|Enforcing|Permissive"), first)

    def test_pkgverify_passes_on_silence_and_on_pacmans_summary_line(self):
        """`rpm -V` prints nothing when every file agrees; `pacman -Qkk` prints
        one summary line.  Two shapes, one meaning, and neither may be
        confused with a real difference."""
        for distro, answer in (("fedora", ""),
                               ("arch", "w11: 61 total files, 0 altered files")):
            with self.subTest(distro):
                out = self.phase("phase_pkgverify", distro, answer)
                self.assertTrue(any(ln.startswith("PASS|") for ln in out.splitlines()), out)
                self.assertFalse(any(ln.startswith("FAIL|") for ln in out.splitlines()), out)

    def test_pkgverify_fails_on_a_difference_and_on_a_package_that_is_not_installed(self):
        """The second half is what makes the first half worth anything: `rpm -V`
        on a package rpm has never heard of prints one line and exits 1, and a
        phase that called that "clean" would report a verified package on an
        image where the install never happened."""
        for distro, answer in (
                ("fedora", "S.5....T.  /usr/bin/wdotool"),
                ("fedora", "package w11 is not installed"),
                ("arch", "w11: 61 total files, 1 altered file")):
            with self.subTest((distro, answer)):
                out = self.phase("phase_pkgverify", distro, answer)
                self.assertTrue(any(ln.startswith("FAIL|") for ln in out.splitlines()), out)

    def test_pkgverify_has_nothing_to_say_on_a_distro_with_no_verifier(self):
        out = self.phase("phase_pkgverify", "ubuntu", "")
        self.assertNotIn("PASS|", out)
        self.assertNotIn("FAIL|", out)

    def test_pkgverify_does_not_run_the_verifier_over_a_tree_deploy(self):
        """In tree mode phase_install puts the tree's extension.js, metadata.json
        and org.w11.Bridge1.xml over the package's copies under
        /usr/share/gnome-shell/extensions (gnome.sh install_extra), and the
        single Arch package OWNS those three files -- so `pacman -Qkk` would
        report altered files for a difference the smoke itself made, and the
        phase would be red by construction on arch-gnome.  It says what it did
        not check and stops."""
        out = self.phase("phase_pkgverify", "arch",
                         "w11: 61 total files, 3 altered files", mode="tree")
        self.assertNotIn("PASS|", out)
        self.assertNotIn("FAIL|", out)


class TheProxyPhase(unittest.TestCase):
    """The `proxy` phase: where it runs, where it must not, and the bookkeeping
    that says which flavors have run it.

    It is the only place in the tree where the ORIGINAL xdotool, wmctrl, xprop
    and xrandr are driven through `xw11` against a real compositor, so a
    Wayland step file that quietly lost the phase would take the whole claim
    with it and nothing else would notice.  The mirror claim matters as much:
    on an X11 session the handover has already replaced the process one line
    above the wrapper's call, so no proxy may be started there at all -- and
    that is asserted by the pid file the proxy writes, not by a process
    pattern.
    """

    @classmethod
    def setUpClass(cls):
        cls.common = read(STEPS, "common.sh")
        cls.phases = {t: phases_of(t) for t in step_tokens()}

    def wayland_tokens(self):
        """A step file is a Wayland one when it runs `windows`; the X11 ones
        run `passthrough` and stop, because after the handover there is no
        clone left in the process to ask about a window."""
        return sorted(t for t, p in self.phases.items() if "windows" in p)

    def x11_tokens(self):
        return sorted(t for t, p in self.phases.items() if "windows" not in p)

    def test_common_sh_defines_phase_proxy(self):
        """The premise: every list below names a function, and this is where it
        lives -- no desktop file carries a copy."""
        self.assertIn("\nphase_proxy() {", self.common)
        self.assertEqual(sum(1 for t in step_tokens()
                             if "phase_proxy()" in read(STEPS, t + ".sh")), 0)

    def test_the_split_is_not_empty_on_either_side(self):
        """The premise of the two tests below: a `phases_of` that stopped
        resolving would make both of them pass over nothing."""
        self.assertGreaterEqual(len(self.wayland_tokens()), 10, self.phases)
        self.assertGreaterEqual(len(self.x11_tokens()), 5, self.phases)

    def test_every_wayland_step_file_runs_proxy_right_after_wm(self):
        """Right after `wm` and not anywhere: the phase re-runs the `windows`
        and `wm` assertions with the originals and compares them with what the
        clones answered, so it needs $WIN and those answers to be the freshest
        thing that happened."""
        for token in self.wayland_tokens():
            with self.subTest(token):
                phases = self.phases[token]
                self.assertIn("proxy", phases, phases)
                self.assertIn("wm", phases, phases)
                self.assertEqual(phases.index("proxy"), phases.index("wm") + 1, phases)

    def test_no_x11_step_file_runs_proxy(self):
        for token in self.x11_tokens():
            with self.subTest(token):
                self.assertNotIn("proxy", self.phases[token], self.phases[token])

    def test_every_x11_step_file_asserts_that_no_proxy_was_started(self):
        """One `want`, in the `passthrough` phase, on the display file --
        `xfce.sh` defines that phase and the other six X11 files source it, so
        the claim reaches all seven."""
        want = ("want \"the X11 handover starts no proxy: "
                "there is no xw11 display file\"")
        self.assertIn(want, read(STEPS, "xfce.sh"))
        for token in self.x11_tokens():
            with self.subTest(token):
                got = subprocess.run(
                    ["bash", "-c",
                     'STEPS=%s; DESKTOP=%s; . "$STEPS/common.sh" >/dev/null 2>&1;'
                     ' . "$STEPS/%s.sh" >/dev/null 2>&1;'
                     ' declare -f phase_passthrough' % (STEPS, token, token)],
                    capture_output=True, text=True, timeout=60)
                self.assertIn("xw11/display", got.stdout,
                              "%s: phase_passthrough does not check for a proxy" % token)

    def test_the_phase_runs_the_originals_and_not_the_clones(self):
        """What separates this phase from `windows`: the binaries it names.  A
        phase that called `wdotool` everywhere would be measuring our code
        twice and calling the second run a proxy test."""
        body = self.common.split("phase_proxy() {", 1)[1].split("\nphase_input", 1)[0]
        for original in ("xdotool", "wmctrl", "xprop", "xrandr"):
            with self.subTest(original):
                self.assertIn("DISPLAY=$disp %s" % original, body)
        self.assertIn("xw11 --print-display", body)
        self.assertIn("xw11 --stop", body)

    def test_the_proxy_lines_are_one_per_wayland_flavor_minus_the_recorded_ones(self):
        """Per flavor and not per step file, because what the phase measures is
        per compositor AND per distribution: the original xdotool is
        3.20211022.1 on Ubuntu and Fedora and 4.20260303.1 on Arch, and a
        recording on one says nothing about the other."""
        way = set(self.wayland_tokens())
        flavors = {f for f, d in flavor_desktops().items() if d in way}
        self.assertTrue(flavors, "no flavor names a Wayland step file")
        recorded = {f for f in flavors
                    for name in os.listdir(LIVEFIX)
                    if name.startswith(f + "-") and name.endswith("-replay.txt")
                    and "-proxy" in name}
        self.assertEqual(sorted(set(not_yet_run_proxy())),
                         sorted(flavors - recorded))

    def test_no_proxy_line_names_a_flavor_that_does_not_exist(self):
        self.assertEqual([f for f in not_yet_run_proxy()
                          if f not in flavor_desktops()], [])

    def test_the_step_file_list_and_the_proxy_list_do_not_overlap(self):
        """The two classes of line share one file and are read by two different
        functions; a token that parsed as both would be a line one of them is
        silently skipping."""
        self.assertEqual(sorted(set(not_yet_run()) & set(not_yet_run_proxy())), [])


class TheOfflineSelfTest(unittest.TestCase):
    """R31.  vm/live-smoke.d/selftest-offline.sh, and the bookkeeping that says
    which step files it can and cannot cover."""

    def test_every_step_file_is_recorded_or_declared_not_yet_run(self):
        """A step file with no recording and no entry is one whose checks
        nobody has ever run -- not against a guest, and not against bytes."""
        recorded = {t for t in recorded_tokens() if t}
        declared = set(not_yet_run())
        missing = [t for t in step_tokens() if t not in recorded and t not in declared]
        self.assertEqual(missing, [])

    def test_nothing_is_both_recorded_and_declared_not_yet_run(self):
        """The list is deleted from as recordings arrive.  A token in both
        places means somebody added a recording and left the line, and the next
        reader cannot tell which of the two is the truth."""
        recorded = {t for t in recorded_tokens() if t}
        self.assertEqual(sorted(recorded & set(not_yet_run())), [])

    def test_the_not_yet_run_list_names_only_step_files_that_exist(self):
        self.assertEqual([t for t in not_yet_run() if t not in step_tokens()], [])

    def test_every_recording_resolves_to_a_flavor_and_a_desktop(self):
        """The name is the mapping: `<flavor>-<whatever>-replay.txt`.  A
        recording whose flavor was renamed or dropped resolves to None and
        covers nothing, silently."""
        self.assertNotIn(None, recorded_tokens())

    #: The self-test costs about nineteen seconds -- it runs the real driver
    #: against the real step files through fake-vmctl, twice -- so it runs ONCE
    #: for this class and both tests read the same stdout.  Two separate
    #: subprocess runs made this file cost 38 s of which 38 s was this script.
    SELFTEST = None

    @classmethod
    def setUpClass(cls):
        cls.SELFTEST = subprocess.run(["bash", SELFTEST_OFFLINE], capture_output=True,
                                      text=True, timeout=600, cwd=ROOT)

    def test_the_two_pass_rule_holds_on_every_recording(self):
        """pass 1 is every check green on the recording, pass 2 is exactly one
        red on one deliberately mutated answer.  It is the only thing in the
        tree that proves the smoke's own checks can fail."""
        got = self.SELFTEST
        self.assertEqual(got.returncode, 0, got.stdout[-4000:] + got.stderr[-2000:])
        self.assertIn("selftest-offline: OK", got.stdout)
        self.assertNotIn("SELFTEST FAIL", got.stdout)
        # and it really ran the recording, rather than finding none and saying OK
        self.assertRegex(got.stdout, r"pass 1: the recording as it was captured")
        self.assertRegex(got.stdout, r"\(the b7a60f0 check went red")

    def test_the_self_test_lists_every_step_file_in_its_coverage_pass(self):
        """Its pass 5 is the shell half of the first test in this class; a step
        file it does not mention is one it walked past."""
        got = self.SELFTEST
        tail = got.stdout.split("pass 5", 1)[-1]
        for token in step_tokens():
            with self.subTest(token):
                self.assertRegex(tail, r"(?m)^\s*%s: (recorded|not yet run)$" % re.escape(token))


if __name__ == "__main__":
    unittest.main()
