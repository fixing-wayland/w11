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
RIG_RECORDINGS = os.path.join(ROOT, "scripts", "rig-recordings.sh")
COMMON_SH = os.path.join(STEPS, "common.sh")
CAPTURE_FROM_RUN = os.path.join(STEPS, "capture-from-run")
FAKE_VMCTL = os.path.join(STEPS, "fake-vmctl")
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


class TheCosmicKdlPoller(unittest.TestCase):
    """cosmic.sh's `cosmic_kdl`, which `cosmic_transform`/`cosmic_mode` read
    through.  cosmic-randr answers zero `output` rows for a second or so right
    after a modeset (MEASURED ~4 s once on the arch-cosmic golden, oracle_outputs'
    paragraph), and a single read in that window asserts an empty '' against the
    resting transform -- that is what reddened fedora44-cosmic in CI run
    34682383044 (`--rotate normal ... want 'normal' got ''`).  So the read polls
    the document until it carries an `output` row, bounded 10 x 1 s -- the same
    wait oracle_outputs uses.  This sources the shipped cosmic.sh the way the
    driver does (R13's stubs) with `guest` answering an empty document N times
    then the real KDL and `sleep` counted instead of taken, so the poll's shape
    is checked in a second where the golden costs a boot."""

    KDL = os.path.join(VMFIX, "cosmic-randr-2heads-one-disabled.kdl")

    def _poll(self, empties):
        """Source cosmic.sh, make `guest` emit an empty KDL `empties` times then
        the fixture, count guest+sleep calls, read Virtual-2's transform through
        cosmic_transform.  Returns (transform, guest_calls, sleeps)."""
        tmp = tempfile.mkdtemp()
        cnt, scnt = os.path.join(tmp, "n"), os.path.join(tmp, "s")
        for path in (cnt, scnt):
            with open(path, "w") as fh:
                fh.write("0")
        pre = "set -u\n"
        pre += 'STEPS=%s\nDESKTOP=cosmic\nDISTRO=ubuntu\nMODE=pkg\nREUSE=0\n' % STEPS
        pre += 'VM=/nonexistent/vmctl\nNAME=t\nFLAVOR=t\nREPO=%s\nHEADS=2\nSCALE=0\n' % ROOT
        for name in TheStepFiles.HELPERS.split():
            pre += '%s() { :; }\n' % name
        pre += '. "$STEPS/common.sh"\n. "$STEPS/cosmic.sh"\n'
        # Redefine AFTER sourcing so these win: `guest` (the KDL source) counts
        # itself and answers empty until the (empties+1)-th read; `sleep` is
        # counted, never taken, so an always-empty run does not wait 10 real s.
        tail = ('guest() { local n; n=$(cat "$CNT"); n=$((n+1)); printf %s "$n" > "$CNT";\n'
                '  if [ "$n" -gt "$EMPTIES" ]; then cat "$KDL"; fi; }\n'
                'sleep() { local s; s=$(cat "$SCNT"); printf %s "$((s+1))" > "$SCNT"; }\n'
                't=$(cosmic_transform Virtual-2)\n'
                'printf "T:[%s]\\n" "$t"\n'
                'printf "G:%s\\n" "$(cat "$CNT")"\n'
                'printf "S:%s\\n" "$(cat "$SCNT")"\n')
        env = dict(os.environ, CNT=cnt, SCNT=scnt, KDL=self.KDL, EMPTIES=str(empties))
        got = subprocess.run(["bash", "-c", pre + tail],
                             capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(got.returncode, 0, got.stderr)
        out = dict(ln.split(":", 1) for ln in got.stdout.splitlines() if ":" in ln)
        return out["T"], int(out["G"]), int(out["S"])

    def test_a_healthy_read_returns_on_the_first_guest_call(self):
        """When cosmic-randr answers rows straight away there is no added
        latency: one guest call, no sleep, the resting transform."""
        transform, calls, sleeps = self._poll(0)
        self.assertEqual((transform, calls, sleeps), ("[normal]", 1, 0))

    def test_an_empty_document_is_reread_past_the_settle_until_rows_arrive(self):
        """Two empty documents then rows: the poll re-reads past both (three
        guest calls, two sleeps) and still returns `normal`, where a single read
        would have asserted ''."""
        transform, calls, sleeps = self._poll(2)
        self.assertEqual((transform, calls, sleeps), ("[normal]", 3, 2))

    def test_an_always_empty_oracle_gives_up_bounded_at_ten_reads(self):
        """An oracle that never answers rows does not hang: the poll stops at
        ten reads (ten counted 1 s sleeps) and hands back the empty document, so
        the check FAILs rather than blocking the whole smoke."""
        transform, calls, sleeps = self._poll(99)
        self.assertEqual((transform, calls, sleeps), ("[]", 10, 10))


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


class TheRecordingsPipeline(unittest.TestCase):
    """`vm/live-smoke.sh --record`, the version note common.sh asks the guest for, and
    scripts/rig-recordings.sh -- the three pieces that turn what a rig run already
    measures into a fixture instead of throwing it away.

    CI runs every phase a recording would record, on 38 flavors, on every push, and
    before this nothing set CAPLOG: of the 47 guest commands in the committed sway
    fixture, 5 appear anywhere in an artifact log, none of them with its output bytes or
    its status (recon/recordings.md 1.1-1.2, measured on run 34571549808)."""

    #: The version line the driver asks for, as the transcript records it.  `sway
    #: --version` printed exactly this through the capture wrapper on the resolute-sway
    #: golden, 2026-09-11, and 1.11 is the token the committed fixture name carries.
    VERSION_SECTION = ("### printf 'w11-desktop-version: '; sway --version\n"
                       "w11-desktop-version: sway version 1.11\n"
                       "### rc=0\n")
    #: The guard `phase_proxy` asks before it measures anything (common.sh), which the
    #: committed sway recording predates -- it was cut one commit before the guard landed.
    XPROP_SECTION = "### xprop -root\n### rc=0\n"

    SWAY_FIXTURE = os.path.join(LIVEFIX, "resolute-sway-1.11-windows-wm-proxy-replay.txt")

    @property
    def PLANTED_NYR(self):
        return read(NOT_YET_RUN) + "sway\nproxy:resolute-sway\n"

    def driver(self, args, env=None, cwd=ROOT, timeout=300):
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run(["bash", DRIVER] + args, capture_output=True, text=True,
                              timeout=timeout, cwd=cwd, env=e)

    def record_a_run(self, work, override="", extra=(), name="rec"):
        """A recorded run with no VM: `--record` puts capture-from-run in front of
        whatever LIVE_SMOKE_VMCTL names, so pointing that at fake-vmctl replays the
        committed sway recording and captures the run of it.  That round trip is what
        recon/recordings.md 1.5(i) measured (27 pass, 0 fail, and a capture that names
        itself back to the same file name).

        `extra` are driver flags -- `--pkg` for a recording of the shape every CI run
        makes, which is the one the mode header exists for."""
        out = os.path.join(work, "out")
        os.makedirs(os.path.join(work, "state"), exist_ok=True)
        got = self.driver(
            ["resolute-sway", "--name", name, "--reuse", "--keep", "--record",
             "--phases", "windows,wm,proxy"] + list(extra),
            env={"LIVE_SMOKE_VMCTL": FAKE_VMCTL,
                 "FAKE_VMCTL_TRANSCRIPT": self.SWAY_FIXTURE,
                 "FAKE_VMCTL_OVERRIDE": override,
                 "FAKE_VMCTL_STATE": os.path.join(work, "state"),
                 "LIVE_SMOKE_OUT": out,
                 "LIVE_SMOKE_SLEEP": "0"})
        caps = [n for n in os.listdir(out) if n.endswith("-capture.txt")]
        logs = [n for n in os.listdir(out) if n.endswith(".log")]
        return got, out, caps, logs

    def test_record_puts_the_capture_wrapper_in_front_of_vmctl_and_pairs_it_with_the_log(self):
        """The flag is wired where $OUTDIR and $STAMP are known and not at the option
        loop, because the capture is paired with the log BY STAMP -- a capture whose log
        is a different run would name the fixture after the wrong phase list."""
        with tempfile.TemporaryDirectory() as work:
            got, out, caps, logs = self.record_a_run(work)
            self.assertEqual(got.returncode, 0, got.stdout[-3000:] + got.stderr[-2000:])
            self.assertEqual(len(caps), 1, caps)
            self.assertEqual(len(logs), 1, logs)
            self.assertEqual(caps[0], logs[0][:-len(".log")] + "-capture.txt")
            body = read(out, caps[0])
            self.assertIn("### wwmctl -l\n", body)
            self.assertIn("### rc=", body)
            # the header the replay reads the run's own mode out of
            self.assertRegex(body.splitlines()[0],
                             r"^# live-smoke recording: flavor=resolute-sway desktop=sway "
                             r"distro=ubuntu mode=tree remove=0 ")

    def test_without_record_the_run_captures_nothing(self):
        """The control: the same run without the flag leaves a log and no capture, which
        is what every CI run did until the vm job passed --record."""
        with tempfile.TemporaryDirectory() as work:
            out = os.path.join(work, "out")
            os.makedirs(os.path.join(work, "state"))
            got = self.driver(
                ["resolute-sway", "--name", "norec", "--reuse", "--keep",
                 "--phases", "windows"],
                env={"LIVE_SMOKE_VMCTL": FAKE_VMCTL,
                     "FAKE_VMCTL_TRANSCRIPT": self.SWAY_FIXTURE,
                     "FAKE_VMCTL_STATE": os.path.join(work, "state"),
                     "LIVE_SMOKE_OUT": out, "LIVE_SMOKE_SLEEP": "0"})
            self.assertEqual(got.returncode, 0, got.stderr[-2000:])
            self.assertEqual([n for n in os.listdir(out) if n.endswith("-capture.txt")], [])

    def test_the_sleep_knob_takes_the_phases_sleeps_out(self):
        """Every `sleep` in a phase is there for a real compositor; a transcript has no
        frames to wait for.  One full-phase replay cost 48.4 s, most of it literal sleep,
        and 38 of those is 20-30 minutes (recon/recordings.md 1.6.5) -- so the knob is
        what makes a replay job possible at all.  It is a shell FUNCTION in the driver
        because the sleeps are spread over common.sh and every step file."""
        block = support.sh_block(DRIVER, "sleep()   {", "command sleep \"$@\"; }")
        script = block + "\nt0=$SECONDS\nsleep 3\necho \"elapsed=$((SECONDS - t0))\"\n"
        fast = subprocess.run(["bash", "-c", "LIVE_SMOKE_SLEEP=0\n" + script],
                              capture_output=True, text=True, timeout=60)
        slow = subprocess.run(["bash", "-c", "LIVE_SMOKE_SLEEP=1\n" + script],
                              capture_output=True, text=True, timeout=60)
        self.assertIn("elapsed=0", fast.stdout, fast.stdout + fast.stderr)
        self.assertNotIn("elapsed=0", slow.stdout, slow.stdout + slow.stderr)

    def test_every_desktop_a_flavor_names_has_a_version_command_of_its_own(self):
        """The token in a recording's name is a MEASUREMENT: `replay_spec` drops exactly
        one token after the flavor, and until this hook those tokens were a human reading
        vm/README.md (recon/recordings.md 1.3).  A desktop that falls through to `true`
        prints no version, which makes rig-recordings.sh refuse to name its file -- so a
        new desktop has to bring its version command with it."""
        pre = ("set -u\n" + support.sh_function(COMMON_SH, "desktop_version_cmd")
               + '\nfor d in %s; do DESKTOP=$d; echo "$d $(desktop_version_cmd)"; done\n'
               % " ".join(sorted({d for d in flavor_desktops().values() if d})))
        got = subprocess.run(["bash", "-c", pre], capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        fell_through = [ln for ln in got.stdout.splitlines() if ln.split(" ", 1)[1] == "true"]
        self.assertEqual(fell_through, [], got.stdout)

    def test_the_version_note_marks_the_line_the_naming_reads(self):
        """The marker is in the recorded OUTPUT and not in the command, so the bytes the
        compositor printed about itself are a recorded section like any other -- and the
        reader that names the file takes the first version-shaped word of that line.  Both
        halves here: the note the driver asks for, and `version_of` over the bytes four
        real desktops answer with (the three shapes rig-recordings.sh's comment names, and
        the shape a desktop whose binary is not installed answers with)."""
        pre = ("set -u\nDESKTOP=sway\n"
               'guest() { printf "%s\\n" "GUEST[$1]"; }\nnote() { echo "NOTE $*"; }\n'
               + support.sh_function(COMMON_SH, "desktop_version_cmd")
               + support.sh_function(COMMON_SH, "desktop_version_note")
               + "\ndesktop_version_note\n")
        got = subprocess.run(["bash", "-c", pre], capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("NOTE GUEST[printf 'w11-desktop-version: '; sway --version]", got.stdout)
        # ...and the bytes themselves, through the function that turns them into the token
        # in the file name.  `sh: 1: cosmic-comp: not found` is what an arm whose binary is
        # not on the guest prints, and an empty token is what makes the naming refuse.
        reader = support.sh_function(RIG_RECORDINGS, "version_of")
        with tempfile.TemporaryDirectory() as work:
            for line, token in (("sway version 1.11", "1.11"),
                                ("GNOME Shell 50.1", "50.1"),
                                ("labwc 0.9.3 (wlroots 0.19.2)", "0.9.3"),
                                ("sh: 1: cosmic-comp: not found", "")):
                with self.subTest(line):
                    cap = os.path.join(work, "cap.txt")
                    with open(cap, "w", encoding="utf-8") as fh:
                        fh.write("### printf 'w11-desktop-version: '; true\n"
                                 "w11-desktop-version: %s\n### rc=0\n" % line)
                    out = subprocess.run(
                        ["bash", "-c", "set -u\n%s\nprintf '[%%s]' \"$(version_of %s)\"\n"
                         % (reader, shlex.quote(cap))],
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(out.stdout, "[%s]" % token, out.stderr)

    def test_the_driver_asks_for_the_version_once_beside_the_session_banner(self):
        """Once per run and after the session is up: it is a guest command, and a guest
        command before `vmctl session` has no session to ask."""
        src = read(VM, "live-smoke.sh")
        self.assertEqual(src.count("\ndesktop_version_note\n"), 1)
        self.assertLess(src.index("wait_session ||"), src.index("\ndesktop_version_note\n"))

    # -- scripts/rig-recordings.sh ------------------------------------------

    def rig(self, work, artifacts, args=(), env=None):
        """rig-recordings.sh against a directory of artifacts instead of `gh run
        download`, writing into a copy of everything it edits.

        The NOT-YET-RUN copy carries the two lines this flavor's recording retires,
        planted: the committed sway fixture already retired them in the tree, and a test
        that asserted their removal from a file that has not got them would pass on
        nothing."""
        out = os.path.join(work, "fixtures")
        os.makedirs(out, exist_ok=True)
        nyr = os.path.join(work, "NOT-YET-RUN")
        with open(nyr, "w", encoding="utf-8") as fh:
            fh.write(self.PLANTED_NYR)
        e = dict(os.environ)
        e.update({"RIG_REC_LOCAL": artifacts, "RIG_REC_OUT": out, "RIG_REC_NYR": nyr,
                  "RIG_REC_WORK": os.path.join(work, "rigwork")})
        e.update(env or {})
        got = subprocess.run(["bash", RIG_RECORDINGS, "9999", "resolute-sway"] + list(args),
                             capture_output=True, text=True, timeout=600, cwd=ROOT, env=e)
        return got, out, nyr

    def artifact_dir(self, work, version=True, tally=None):
        """One `live-smoke-resolute-sway` artifact, made by recording a replay of the
        committed sway recording -- the same round trip recon/recordings.md 1.5(i) took."""
        override = os.path.join(work, "override.txt")
        with open(override, "w", encoding="utf-8") as fh:
            fh.write((self.VERSION_SECTION if version else "") + self.XPROP_SECTION)
        got, out, caps, logs = self.record_a_run(work, override)
        self.assertEqual(got.returncode, 0, got.stdout[-3000:])
        art = os.path.join(work, "artifacts", "resolute-sway")
        os.makedirs(art)
        for name in caps + logs:
            with open(os.path.join(out, name), encoding="utf-8") as fh:
                body = fh.read()
            if tally is not None and name.endswith(".log"):
                body = re.sub(r"(?m)^(== \[\d+s\] done: )\d+( pass, )\d+( fail)$",
                              r"\g<1>%d\g<2>%d\g<3>" % tally, body)
            with open(os.path.join(art, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        return os.path.join(work, "artifacts")

    def test_a_recording_is_named_from_the_logs_phases_and_the_captures_version(self):
        """The name IS the mapping selftest-offline.sh's `replay_spec` reads back, and
        both halves of it come out of the run: the phase list out of the log's `phases:`
        line, the version out of the `w11-desktop-version:` section of the capture.  The
        name this produces for the sway round trip is byte-identical to the one the
        committed fixture carries."""
        with tempfile.TemporaryDirectory() as work:
            art = self.artifact_dir(work)
            got, out, nyr = self.rig(work, art)
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            self.assertEqual(os.listdir(out),
                             ["resolute-sway-1.11-windows-wm-proxy-replay.txt"])
            kept = read(out, "resolute-sway-1.11-windows-wm-proxy-replay.txt")
            self.assertIn("w11-desktop-version: sway version 1.11", kept)
            self.assertIn("recording written", got.stdout.replace("recording(s)", "recording"))

    def test_the_recording_retires_its_two_not_yet_run_lines(self):
        """The step-file token (`sway`) and `proxy:<flavor>`, the latter only because
        this run really ran the proxy phase.  Nothing else in the file may move."""
        with tempfile.TemporaryDirectory() as work:
            art = self.artifact_dir(work)
            got, out, nyr = self.rig(work, art)
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            before = self.PLANTED_NYR.splitlines()
            after = read(nyr).splitlines()
            self.assertEqual(sorted(set(before) - set(after)),
                             ["proxy:resolute-sway", "sway"])
            self.assertEqual(set(after) - set(before), set())
            self.assertIn("NOT-YET-RUN: 2 line(s) retired", got.stdout)

    def test_a_capture_with_no_version_note_is_refused_rather_than_named(self):
        """An arm of `desktop_version_cmd` that prints nothing lands here as an empty
        token, and a guessed version in a file name is a measurement nobody took.  This
        is the case that makes the note self-checking on the eighteen desktops whose arm
        has not been run yet."""
        with tempfile.TemporaryDirectory() as work:
            art = self.artifact_dir(work, version=False)
            got, out, nyr = self.rig(work, art)
            self.assertNotEqual(got.returncode, 0, got.stdout)
            self.assertIn("nothing to name the file after", got.stdout)
            self.assertEqual(os.listdir(out), [])
            self.assertEqual(self.PLANTED_NYR, read(nyr))

    def test_a_replay_that_does_not_reproduce_the_runs_own_tally_is_not_kept(self):
        """The acceptance rule, and the reason the replay runs at all: a recording nobody
        has replayed is a claim.  The log says `done: N pass, M fail`; the replay of the
        recording has to say the same thing, which the live resolute-sway run of
        2026-09-11 did check for check (56/0 live, 56/0 replayed in the run's own mode --
        and 42 replayed the way pass 6 used to, with --reuse)."""
        with tempfile.TemporaryDirectory() as work:
            art = self.artifact_dir(work, tally=(999, 0))
            got, out, nyr = self.rig(work, art)
            self.assertNotEqual(got.returncode, 0, got.stdout)
            self.assertIn("NOT kept", got.stdout)
            self.assertEqual(os.listdir(out), [])
            self.assertEqual(self.PLANTED_NYR, read(nyr))

    def test_the_kept_recording_carries_the_tally_that_accepted_it(self):
        """selftest-offline.sh's pass 6 has no log beside a fixture, so without this line
        its whole acceptance rule is ">= 5 checks and none red" -- which a replay that
        quietly loses half its checks satisfies.  The number the rig measured travels with
        the recording instead, in a comment fake-vmctl's parser drops."""
        with tempfile.TemporaryDirectory() as work:
            art = self.artifact_dir(work)
            got, out, nyr = self.rig(work, art)
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            kept = read(out, "resolute-sway-1.11-windows-wm-proxy-replay.txt").splitlines()
            self.assertTrue(kept[0].startswith("# live-smoke recording: "), kept[0])
            self.assertRegex(kept[1], r"^# replay: \d+ pass, 0 fail "
                                      r"\(mode tree, remove 0, phases windows,wm,proxy, "
                                      r"\d{4}-\d\d-\d\d\)$")
            # and it is the tally the replay actually produced, not a constant
            self.assertRegex(got.stdout, r"replay %s pass, 0 fail" % kept[1].split()[2])

    def test_a_fresh_recording_retires_the_flavors_older_one(self):
        """One recording per flavor.  An older `<flavor>-*-replay.txt` was cut from an
        older step file and is drifted by construction, so left beside the new one pass 6
        replays it forever, the drift inventory never empties and SELFTEST_STRICT=1 can
        never be turned on.  The NOT-YET-RUN edit already assumes one per flavor."""
        with tempfile.TemporaryDirectory() as work:
            art = self.artifact_dir(work)
            out = os.path.join(work, "fixtures")
            os.makedirs(out, exist_ok=True)
            stale = "resolute-sway-1.9-windows-replay.txt"
            with open(os.path.join(out, stale), "w", encoding="utf-8") as fh:
                fh.write("### true\n### rc=0\n")
            got, out, nyr = self.rig(work, art)
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            self.assertEqual(os.listdir(out), ["resolute-sway-1.11-windows-wm-proxy-replay.txt"])
            self.assertIn("superseded: " + stale, got.stdout)

    #: The two checks phase_proxy runs only where MODE is not `tree` (common.sh:627): the
    #: committed recording was cut from a tree run, so it has nothing recorded for them and
    #: this answers them -- 4194305 is the shadow id its proxy section carries, 7 the id the
    #: clone answers with in the same section.
    WRAPPER_SECTIONS = ("### W11_PROXY=always wdotool search --class foot | head -1\n"
                        "4194305\n### rc=0\n"
                        "### W11_PROXY=never wdotool search --class foot | head -1\n"
                        "7\n### rc=0\n")

    def test_pass_six_replays_a_pkg_recording_in_the_mode_the_run_was_made_in(self):
        """Every fixture the rig harvests is a `--pkg --remove` run, and one of those
        replayed the way pass 6 replayed everything before -- `--reuse`, tree mode -- loses
        14 of its 59 checks with no red line anywhere: 3 install, 9 remove and the 2 the
        proxy phase skips where MODE is tree (measured on the resolute-sway artifact of
        2026-09-12, 59 pass in the run's own mode against 45 in tree mode).  So the mode
        comes out of the capture's own header, and the tree replay below is the control
        that shows what that is worth."""
        with tempfile.TemporaryDirectory() as work:
            override = os.path.join(work, "override.txt")
            with open(override, "w", encoding="utf-8") as fh:
                fh.write(self.VERSION_SECTION + self.XPROP_SECTION + self.WRAPPER_SECTIONS)
            got, out, caps, logs = self.record_a_run(work, override, extra=["--pkg"])
            self.assertEqual(got.returncode, 0, got.stdout[-3000:])
            full = len(re.findall(r"(?m)^PASS ", got.stdout))
            cap = os.path.join(work, "resolute-sway-1.11-windows-wm-proxy-replay.txt")
            with open(cap, "w", encoding="utf-8") as fh:
                fh.write(read(out, caps[0]))
            self.assertIn("mode=pkg", read(cap).splitlines()[0])
            st = subprocess.run(["bash", SELFTEST_OFFLINE, cap], capture_output=True,
                                text=True, timeout=600, cwd=ROOT)
            self.assertEqual(st.returncode, 0, st.stdout[-3000:] + st.stderr[-1000:])
            line = [ln for ln in st.stdout.splitlines() if "resolute-sway [" in ln]
            self.assertEqual(len(line), 1, st.stdout)
            self.assertIn("--pkg", line[0])
            self.assertIn("%d pass, 0 fail" % full, line[0])
            # the control: the same capture replayed the old way runs FEWER checks, and
            # nothing in its output says so -- which is the whole hazard
            os.makedirs(os.path.join(work, "ctl"))
            tree = self.driver(
                ["resolute-sway", "--name", "ctl", "--reuse", "--keep",
                 "--phases", "windows,wm,proxy"],
                env={"LIVE_SMOKE_VMCTL": FAKE_VMCTL, "FAKE_VMCTL_TRANSCRIPT": cap,
                     "FAKE_VMCTL_STATE": os.path.join(work, "ctl"),
                     "LIVE_SMOKE_OUT": os.path.join(work, "ctlout"),
                     "LIVE_SMOKE_SLEEP": "0"})
            self.assertLess(len(re.findall(r"(?m)^PASS ", tree.stdout)), full)
            self.assertNotIn("FAIL ", tree.stdout)

    def test_the_preflight_replay_is_strict_and_names_what_is_missing(self):
        """Strict is what makes drift visible: fake-vmctl answers an unrecorded command
        with empty output and status 0, so a guard takes its has-an-X-server branch by
        luck and the checks behind it pass on nothing -- the committed sway recording
        replays 27 checks lenient and 11 strict, 0 fail either way (1.6.1).  A capture
        taken FROM a run answers everything that run asked, by construction, so the
        section a later step file would ask for is cut out here to make the case."""
        with tempfile.TemporaryDirectory() as work:
            art = self.artifact_dir(work)
            cap = [os.path.join(art, "resolute-sway", n)
                   for n in os.listdir(os.path.join(art, "resolute-sway"))
                   if n.endswith("-capture.txt")][0]
            body = read(cap)
            cut = body.replace("### xprop -root >/dev/null 2>&1\n### rc=0\n", "")
            self.assertNotEqual(cut, body, "the capture has no xprop -root guard in it")
            with open(cap, "w", encoding="utf-8") as fh:
                fh.write(cut)
            got, out, nyr = self.rig(work, art)
            self.assertNotEqual(got.returncode, 0, got.stdout)
            self.assertIn("nothing recorded for 'xprop -root'", got.stdout)
            self.assertIn("NOT kept", got.stdout)
            self.assertEqual(os.listdir(out), [])


class ThePromotedChecks(unittest.TestCase):
    """The xwants the first all-38 run answered, and the guard that made one of them
    assert nonsense.  Each of these was an `xwant` at af59ab3 and is a plain check now."""

    def setUp(self):
        self.common = read(STEPS, "common.sh")

    def labels(self, token):
        """Every `want`/`xwant` label in one step file, as {label: the helper called}."""
        out = {}
        for line in read(STEPS, token + ".sh").splitlines():
            m = re.match(r'\s*(xwant|want|same|xsame) "([^"]*)"', line)
            if m:
                out[m.group(2)] = m.group(1)
        return out

    def test_r9_typing_into_a_native_window_is_a_plain_check(self):
        """XPASS on all nine GNOME/KDE flavors of run 34571549808, with the registry read
        at 16-29 ms (recon/recordings.md 1.7)."""
        labels = self.labels("common")
        r9 = [k for k in labels if k.startswith("R9:")]
        self.assertEqual(len(r9), 1, sorted(labels))
        self.assertEqual(labels[r9[0]], "want")
        self.assertNotIn("(until", r9[0])

    def test_r2_primary_read_back_is_a_plain_check(self):
        """XFAIL on all nine until batch 7: `xrandr --output Virtual-2 --primary` read
        back Virtual-1 on Mutter and KWin, and reads back Virtual-2 now (measured on
        resolute-kde, Plasma 6.5 / KWin 6.5, requests-batch-7.md)."""
        labels = self.labels("common")
        r2 = [k for k in labels if k.startswith("R2:")]
        self.assertEqual(len(r2), 1, sorted(labels))
        self.assertEqual(labels[r2[0]], "want")

    def test_the_r2_anchor_has_to_look_like_an_output_name(self):
        """On nixos-gnome `$anchor` came back as the literal `sh:` -- the first token of
        a shell error out of the oracle -- and the regex became `^sh:$`, which asserts
        nonsense (run 34628777544, job 103360601082).  `[ -n "$anchor" ]` cannot tell
        those apart; the shape of the name can."""
        guard = [ln.strip() for ln in self.common.splitlines()
                 if "A-Za-z0-9-" in ln and "anchor" in ln]
        self.assertEqual(len(guard), 1, guard)
        for value, kept in (("Virtual-1", True), ("sh:", False), ("", False),
                            ("HEADLESS-2", True), ("2Virtual", False)):
            with self.subTest(value):
                got = subprocess.run(
                    ["bash", "-c", "set -u\nanchor=%s\n%s\nprintf '[%%s]' \"$anchor\"\n"
                     % (shlex.quote(value), guard[0])],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(got.stdout, "[%s]" % (value if kept else ""), got.stderr)

    def test_the_mate_replug_and_the_wayfire_key_source_are_plain_checks(self):
        """Both were waiting on a key in the golden, and vm/build-image.sh carries both:
        MATE's `turn-on-external-monitors-at-startup=true` gschema override and wayfire's
        `xkb_layout = us,de`."""
        mate = [k for k, v in self.labels("mate").items() if "plugged in again" in k]
        self.assertEqual([self.labels("mate")[k] for k in mate], ["want"], mate)
        wf = [k for k, v in self.labels("wayfire").items() if "group's source" in k]
        self.assertEqual([self.labels("wayfire")[k] for k in wf], ["want"], wf)
        self.assertIn("xkb_layout = us,de", read(VM, "build-image.sh"))
        self.assertIn("turn-on-external-monitors-at-startup=true", read(VM, "build-image.sh"))

    #: The wrapper's rule 5, verbatim from w11common/passthrough.py: a flavor with no
    #: original wmctrl installed answers 127 and says which package would provide it.
    NO_REAL = ("wmctrl: this is w11's clone and a handover to the real tool was asked for, "
               "but no real wmctrl was found on PATH -- install it (apt install wmctrl) or "
               "set W11_WMCTRL=/path/to/wmctrl")

    def phase_root(self, mode, root_out=""):
        """phase_root sliced out and run against stubbed guest()/root(), which echo the
        command (with the W11_PROXY the wrapper arm pins) to stderr and answer `root_out`
        on stdout.  The checks land on stdout as their labels."""
        pre = ("set -u\nDESKTOP=sway\nWIN=1\nMODE=%s\nROOT_OUT=%s\n"
               % (mode, shlex.quote(root_out))
               + 'guest() { echo "GUEST[${SMOKE_PROXY:-never}] $1" >&2; }\n'
               + 'root() { echo "ROOT[${SMOKE_PROXY:-never}] $1" >&2; printf "%s" "$ROOT_OUT"; }\n'
               + 'same() { echo "SAME $1"; }\nok() { :; }\nnote() { echo "NOTE $*"; }\n'
               + 'ev() { printf "%s" "$*"; }\n'
               + support.sh_function(COMMON_SH, "phase_root") + "\nphase_root\n")
        got = subprocess.run(["bash", "-c", pre], capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        checks = [ln[5:] for ln in got.stdout.splitlines() if ln.startswith("SAME ")]
        notes = [ln[5:] for ln in got.stdout.splitlines() if ln.startswith("NOTE ")]
        return got, checks, notes

    #: The two labels the wrapper half of phase_root adds, and the one it asserts in
    #: every mode.
    WRAPPED = ["wwmctl -l as root through the proxy is the seated user's own list",
               "wxrandr --query as root through the proxy is the seated user's own, byte for byte"]
    NO_OWN_PROXY = "root started no proxy of its own: neither root runtime display file exists"

    def test_phase_root_measures_the_wrapper_as_well_as_the_clones(self):
        """Gap (d): as root the two lookups used to take the CALLING process's uid and
        find /root/.Xauthority and /tmp/wdotool-0.  `root()` pins W11_PROXY from
        $SMOKE_PROXY, so the phase runs its first half with the clones and its second
        with the wrapper -- and asserts that root started no proxy of its own."""
        got, checks, _ = self.phase_root("pkg")
        self.assertIn("wwmctl -l as root sees the seated user's windows", checks)
        for label in self.WRAPPED:
            self.assertIn(label, checks)
        self.assertIn(self.NO_OWN_PROXY, checks)
        # the clone half runs with the wrapper off and the proxy half with it on; the
        # commands themselves go to stderr, because the phase reads each one's OUTPUT
        self.assertIn("ROOT[never] wwmctl -l | wc -l", got.stderr)
        self.assertIn("ROOT[always] wwmctl -l", got.stderr)
        self.assertIn("GUEST[always] wwmctl -l", got.stderr)
        self.assertIn("ROOT[always] wxrandr --query", got.stderr)
        self.assertIn("ROOT[always] ls /run/user/0/xw11/display", got.stderr)

    def test_phase_root_does_not_claim_the_wrapper_in_tree_mode(self):
        """The same claim phase_proxy makes at its own wrapper half (common.sh:627): the
        four zipapps a tree run drops in /usr/local/bin carry no xw11, so W11_PROXY=always
        hands over to nothing and both sides of the comparison would be the clone -- a
        PASS under a label saying the wrapper was measured.  The `ls` check stays: a tree
        run that SPAWNED a proxy under /run/user/0 is the same regression either way."""
        got, checks, notes = self.phase_root("tree")
        for label in self.WRAPPED:
            self.assertNotIn(label, checks)
        self.assertIn(self.NO_OWN_PROXY, checks)
        self.assertTrue([n for n in notes if "tree mode" in n and "package's" in n], notes)
        self.assertNotIn("GUEST[always] wwmctl -l", got.stderr)

    def test_phase_root_reads_the_wrappers_rule_five_as_a_note_and_not_a_failure(self):
        """A flavor with no original wmctrl installed: the wrapper exits 127 with the line
        that names the package, which is rule 5 working and not a regression -- there is
        nothing to hand over to, so the two comparisons have nothing to compare and the
        phase says so instead of running them."""
        got, checks, notes = self.phase_root("pkg", self.NO_REAL)
        for label in self.WRAPPED:
            self.assertNotIn(label, checks)
        self.assertIn(self.NO_OWN_PROXY, checks)
        self.assertTrue([n for n in notes if "no original wmctrl on this flavor" in n], notes)


class TheOffHeadHook(unittest.TestCase):
    """The per-flavor `OFF_HEAD_STAYS_OFF` hook (batch 18 item 2): a compositor that keeps an --off head
    off declares it, and common_display_phase's three re-enable checks become route-6 xwants there and stay
    plain everywhere else.  river.sh sets it (river 0.4.8 refuses wlr-randr --on); cosmic.sh does NOT --
    MEASURED on the arch-cosmic 1.8.0 golden (instance acos-b18r, 2026-09-12): a `--off` head comes back
    with `--auto` across three cycles, so cosmic's path is the plain one."""

    def setUp(self):
        self.common = read(STEPS, "common.sh")

    def _sets_hook(self, token):
        # a real assignment `OFF_HEAD_STAYS_OFF=...` at the top of a line, not a comment mentioning it
        return [ln for ln in read(STEPS, token + ".sh").splitlines()
                if re.match(r'\s*OFF_HEAD_STAYS_OFF=', ln)]

    def test_river_sets_the_hook_and_cosmic_does_not(self):
        river = self._sets_hook("river")
        self.assertEqual(len(river), 1, river)
        self.assertIn("river 0.4.8", river[0])
        # cosmic only NAMES the variable in prose (why it is not set); it must not assign it
        self.assertEqual(self._sets_hook("cosmic"), [],
                         "cosmic re-enables an --off head on 1.8.0, so it must not set OFF_HEAD_STAYS_OFF")

    def test_the_route6_path_has_exactly_the_three_re_enable_xwants(self):
        """off_head_route6_xwants turns the same three re-enable checks into xwants naming route 6, each
        with the measured refusal in the label -- so a patched compositor XPASSes them the day it lands."""
        body = support.sh_function(COMMON_SH, "off_head_route6_xwants")
        route6 = re.findall(r'xwant "([^"]*\(until route 6, a patched \$DESKTOP[^"]*)"', body)
        self.assertEqual(len(route6), 3, route6)
        self.assertTrue(any("--auto brings it back" in k for k in route6), route6)
        self.assertTrue(any("--right-of" in k for k in route6), route6)
        self.assertTrue(any("--below" in k for k in route6), route6)

    def test_the_plain_path_keeps_its_three_checks(self):
        """The everywhere-else path: common_display_phase's `same`/`beside` re-enable checks stay plain,
        and the hook is what routes between them (`if [ -n "${OFF_HEAD_STAYS_OFF:-}" ]`)."""
        phase = support.sh_function(COMMON_SH, "common_display_phase")
        self.assertIn('same "--output $second --auto brings it back"', phase)
        self.assertIn('beside right "$first" "$second"', phase)
        self.assertIn('beside below "$first" "$second"', phase)
        self.assertIn('if [ -n "${OFF_HEAD_STAYS_OFF:-}" ]; then', phase)
        self.assertIn("off_head_route6_xwants", phase)

if __name__ == "__main__":
    unittest.main()
