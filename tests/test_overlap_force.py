"""`--unsafe-gnome-overlap-unmeasured`, and the table it exists to be the escape
hatch from.

Two features, one file, because neither makes sense without the other: the table
is what a GNOME release is added to, and the flag is what somebody does on the
GNOME nobody has added yet.

What is held down here:

* **the flag cannot be reached by accident.**  It is not a default in `Opts`, in
  `Session` or in any method signature; no environment variable sets it; it is a
  usage error without `--unsafe-gnome-overlap`, so it can never be the thing that
  turns the feature on; and its required argument is the GNOME Shell major of
  the machine in front of you, so a command line pasted out of a forum is refused
  by number on anybody else's;
* **it is never remembered.**  A forced run neither reads nor writes the
  agreement, `--gnome-overlap-allow` refuses to be typed with it, and
  `save_consent()` refuses a forced reply even if something ever called it;
* **it forces exactly one refusal.**  Every refusal in this feature is either
  *cautious* -- "this is a build nobody here has measured" -- or *certain* --
  a symbol that is not there, an extension that is not there, a compositor that
  is not GNOME, a struct nothing describes, a read that disagrees with Mutter.
  Only the first kind is forceable, and each of the others is re-run here with
  the flag typed and still refuses;
* **the table is one table.**  The extension's `generations.json` and wxrandr's
  `GENERATIONS` are proved identical, `metadata.json` is proved derived from
  them, and nothing anywhere composes a library name out of a number -- which is
  the thing that stopped being true when mutter 51 renumbered libmutter's API
  version to the GNOME major.  The stand-in for an unmeasured GNOME is one
  past the newest record rather than a number written down, because writing
  one down is how these tests came to be testing a build that had since been
  measured;
* **what a refusal prints** is a golden, because a message a maintainer cannot
  act on is the same as no message.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gnome_overlap import Case, EXT_DIR, GIR_DIR, FakeOverlap, load_gen_gir
from wxrandr import cli, gnome_overlap

os.environ["W11_PASSTHROUGH"] = "never"

FLAG = gnome_overlap.FLAG
FORCE = gnome_overlap.FORCE_FLAG
MOVE = ("--output", "Virtual-2", "--pos", "960x0")

TABLE_JSON = os.path.join(EXT_DIR, "generations.json")


def documents_text(relative):
    """One of the repository's markdown files, whole."""
    with open(os.path.join(ROOT, relative), encoding="utf-8") as fh:
        return fh.read()


def markdown_section(text, heading):
    """One `#### ...` section of a markdown document, up to the next heading of
    the same depth or shallower -- "" when the heading is not there, so a
    renamed section is a readable failure rather than a ValueError from
    str.index()."""
    at = text.find("\n" + heading)
    if at < 0:
        return ""
    body = text[at + 1:]
    hashes = heading.split(" ", 1)[0]
    end = re.search(r"\n#{1,%d} " % len(hashes), body)
    return body[:end.start()] if end else body

#: A GNOME nobody in this tree has measured: ONE PAST the newest record, worked
#: out from the table rather than written down here.  It used to be written
#: down, as 51, and then GNOME 51 was measured on Ubuntu 26.10 and every
#: assertion about "the build nobody has measured" was quietly about a build
#: that had been -- so the number moves with the table now, and adding a
#: generation cannot leave this file testing the wrong thing.
UNMEASURED_MAJOR = max(gnome_overlap.SUPPORTED_MAJORS) + 1
UM = str(UNMEASURED_MAJOR)                      # what gets typed after the flag
UMV = "%d.0" % UNMEASURED_MAJOR                 # what such a shell reports

#: the same build as a table record, for the tests about what the table can
#: express.  The names follow no scheme on purpose: mutter 51 renumbered
#: libmutter's API version to the GNOME major, so a record has to be able to say
#: anything.
NEXT = {"shell_major": UNMEASURED_MAJOR, "libmutter": UM,
        "soname": "libmutter-%s.so.0" % UM,
        "meta_typelib": UM, "namespace": "W11Overlap%s" % UM,
        "struct_size": 80, "tail_slots": 3,
        "measured_on": "nowhere: this record is a test fixture"}

#: which shipped description a forced run on such a build would be given: the
#: newest whose declared size matches, which is what rules.js does.
def _by_size(size=80):
    hits = [g for g in gnome_overlap.GENERATIONS if g["struct_size"] == size]
    return hits[-1]["namespace"]


def unmeasured(mock, shell=UMV):
    """Point the mock extension at a GNOME nobody has measured, with the
    library and typelib names that release really carries."""
    ov = mock.overlap = FakeOverlap(shell=shell)
    ov.sonames = ["libmutter-%s.so.0" % shell.split(".")[0]]
    ov.meta_typelib = shell.split(".")[0]
    ov.libmutter = shell.split(".")[0]
    ov.instance_size = 80
    return ov


# --------------------------------------------------------------- reachability

class Reachability(Case):
    """Every way in, and the far larger number of ways that are not one."""

    def record_agreement(self, **over):
        """An agreement for the build this Case's mock reports, written by hand
        -- the supported route (`--gnome-overlap-allow`) cannot be typed with
        the force flag, which is the whole point of the test that uses this."""
        rec = {"format": gnome_overlap.CONSENT_FORMAT, "shell": "50.1",
               "libmutter": 18, "struct_size": 80,
               "agreed": "2026-01-02T03:04:05Z", "how": "by hand"}
        rec.update(over)
        path = gnome_overlap.consent_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec))
        return rec

    def test_the_flag_alone_does_nothing_and_says_so(self):
        """It is a modifier, never an entry point.  Forgetting the dangerous
        flag and typing this one has to be a usage error, or there would be a
        command line in which this is what turns the feature on."""
        unmeasured(self.mock)
        code, out, err = self.run_cli(FORCE, UM, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("only means something together with %s" % FLAG, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])

    def test_it_needs_a_whole_gnome_major_and_not_a_version_string(self):
        for bad in ("", UMV, "fifty-one", "-1", UM + "x", "٥١"):
            code, out, err = self.run_cli(FLAG, FORCE, bad, *MOVE)
            self.assertEqual(code, 1, bad)
            self.assertIn("takes the GNOME Shell major version", err)
            self.assertEqual(self.ext_calls(), [], bad)

    def test_a_missing_argument_is_a_usage_error(self):
        code, out, err = self.run_cli(FLAG, FORCE)
        self.assertEqual(code, 1)
        self.assertEqual(self.ext_calls(), [])

    def test_it_has_to_name_the_gnome_in_front_of_you(self):
        """The property a bare --force cannot have: a line copied from a forum
        names the GNOME that person had, and is refused here by number."""
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, "49", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("names GNOME Shell 49; this session is GNOME Shell %s" % UMV,
                      err)
        self.assertIn("copied from somewhere else", err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])

    def test_it_applies_when_it_names_this_gnome(self):
        ov = unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        # the request carries the force, explicitly, on the call itself
        self.assertEqual(ov.calls[0][1]["force"], {"shell_major": UNMEASURED_MAJOR})

    def test_a_measured_gnome_does_not_need_it_and_is_unchanged_by_it(self):
        """Typing it where it is not needed changes nothing at all: the version
        gate passes on its own, and the extension is asked the same question."""
        code, out, err = self.run_cli(FLAG, FORCE, "50", *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])

    @unittest.expectedFailure
    def test_forcing_a_measured_gnome_is_a_no_op_and_says_so(self):
        """Fix 21 (F2.5): when `generation_for(version)` is not None the force
        skips nothing, because the check it exists to skip passes on its own.

        Measured at HEAD on this harness, with an agreement recorded for GNOME
        Shell 50.1: `--unsafe-gnome-overlap --unsafe-gnome-overlap-unmeasured 50
        --output Virtual-2 --pos 960x0` printed the whole forcing paragraph
        ("forcing past the one check that says this GNOME has been measured.
        This session may end."), threw the agreement away because the applying
        path does not read one while forcing, and put `force: {"shell_major":
        50}` in the request -- so a user on a measured GNOME who typed the flag
        out of caution got a louder, less-checked-looking run than one who did
        not, describing a risk that is not being taken.

        What the fix does: drop `force` for a major the table already has, warn
        once that the flag changed nothing, and let the run be the ordinary
        agreed one."""
        self.record_agreement()
        code, out, err = self.run_cli(FLAG, FORCE, "50", *MOVE)
        self.assertEqual(code, 0, err)
        # the *last* call and not the whole list: fix 17 (F1.3, pinned in
        # tests/test_overlap_consent.py ADifferentBuild) puts one Probe in front
        # of every apply that has an agreement to check, and the two fixes have
        # to be able to land in either order.
        self.assertEqual(self.ext_calls()[-1], "ApplyOverlap")
        self.assertNotIn("forcing past the one check", err)
        self.assertIn("as agreed on", err)
        self.assertIn("GNOME Shell 50 is measured", err)
        for _member, req in self.mock.overlap.calls:
            self.assertNotIn("force", req)
        # the control, in the same test: the refusal is dropped for a major the
        # table has, and for no other.  A fix that stopped forcing altogether
        # would satisfy every line above and break the feature.
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("forcing past the one check", err)

    @unittest.expectedFailure
    def test_a_dryrun_of_a_measured_gnome_is_not_the_refused_rehearsal(self):
        """Fix 21, the other half.  `--dryrun` with the force flag is refused
        because reaching an *unmeasured* build means loading a description built
        for another one, and gjs aborts rather than raising -- measured on a
        real GNOME 51, where the first forced run ever attempted was a --dryrun
        and it ended the session.  None of that applies to GNOME 50, whose
        description is the one shipped for it: there is nothing to rehearse
        dangerously, because there is nothing being forced.  The refusal stays
        exactly as it is for an unmeasured major (ADryRunCannotBeForced)."""
        code, out, err = self.run_cli("--dryrun", FLAG, FORCE, "50", *MOVE)
        self.assertEqual(code, 0, err)
        self.assertNotIn("cannot be rehearsed with --dryrun", err)
        self.assertEqual(self.ext_calls(), ["Probe"])

    def test_a_layout_gnome_accepts_never_reaches_any_of_it(self):
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, UM, "--output", "Virtual-2",
                                      "--pos", "1920x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertNotIn(FORCE, err)


class TheFlagOnAnXSession(unittest.TestCase):
    """The handover, as a real process: an X11 session hands wxrandr's argv to
    the distribution's xrandr, and our own options must not go with it.

    The tree is the one tests/test_passthrough_exec.py describes, reduced to
    what this needs: `WXRANDR_REAL_XRANDR` names the stand-in directly, so no
    PATH walk and no real xrandr on the developer's machine can take part, and
    `FAKE_REAL_LOG` is the proof of what the original was asked to do."""

    def run_wxrandr(self, *argv):
        tmp = tempfile.mkdtemp(prefix="overlap-x11-")
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "log")
        env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "DISPLAY": ":0",
               "PYTHONPATH": ROOT, "W11_PASSTHROUGH": "always",
               "WXRANDR_REAL_XRANDR": os.path.join(ROOT, "tests", "fixtures",
                                                   "fake_real_tool.py"),
               "FAKE_REAL_LOG": log}
        rc = subprocess.run([sys.executable, "-m", "wxrandr"] + list(argv),
                            capture_output=True, text=True, env=env, timeout=120)
        handed = []
        if os.path.exists(log):
            with open(log, encoding="utf-8") as fh:
                handed = [json.loads(ln) for ln in fh if ln.strip()]
        return rc, handed

    def test_the_flag_itself_is_our_usage_error_and_never_reaches_xrandr(self):
        """The control, and it passes today: `--unsafe-gnome-overlap` is in
        OWN_APPLY_FLAGS, so `_walk_argv` drops it and the pre-handover check
        answers in our own words."""
        rc, handed = self.run_wxrandr(FLAG, "--output", "X", "--pos", "960x0")
        self.assertEqual(rc.returncode, 1, rc.stdout + rc.stderr)
        self.assertIn("only means anything on GNOME", rc.stderr)
        self.assertEqual(handed, [])

    def test_the_force_flag_alone_is_our_usage_error_too(self):
        """Fix 22 (F1.9), landed as T26.  The force flag is now in
        `OWN_ARGV_FLAGS`, `own_flags_in` reports it, and main() keeps a
        command line carrying it alone rather than handing it over -- so
        `_check_force`'s sentence is what the user gets on an X11 session too.

        What it did before, measured as a real process: `python3 -m wxrandr
        --unsafe-gnome-overlap-unmeasured 52 --output X --auto` with
        `W11_PASSTHROUGH=always`, DISPLAY set and WAYLAND_DISPLAY unset
        exec'd the stand-in with argv `["--unsafe-gnome-overlap-unmeasured",
        "52", "--output", "X", "--auto"]` and exited 0; against a real xrandr
        that is "unrecognized option", exit 1 and the layout unapplied -- the
        same wrong answer `--persistent` used to give -- with `52` read as a
        positional.  The rig saw the same thing on all nine X11 flavors of run
        34628777544 [goal2/recon/gaps.md 1a #7], where the check at
        vm/live-smoke.d/xfce.sh:95 XPASSed on the ORIGINAL's message because
        its regex was the bare flag name, which that message contains."""
        rc, handed = self.run_wxrandr(FORCE, "52", "--output", "X", "--auto")
        self.assertEqual(rc.returncode, 1, rc.stdout + rc.stderr)
        self.assertIn("only means something together with %s" % FLAG, rc.stderr)
        self.assertNotIn("unrecognized option", rc.stderr)
        self.assertEqual(handed, [])

    def test_the_flag_and_its_value_both_stay_out_of_the_handover(self):
        """The other half of the pair, true on both sides of T26 and pinned
        here because T26 moved the code that decides it: with
        `--unsafe-gnome-overlap` typed beside it the session's own refusal
        comes first and is the whole answer, and neither the force flag nor its
        `52` reaches the original -- a stripped argv that kept the value would
        hand the original a screen size to apply."""
        rc, handed = self.run_wxrandr(FLAG, FORCE, "52", "--output", "X", "--auto")
        self.assertEqual(rc.returncode, 1, rc.stdout + rc.stderr)
        self.assertIn("only means anything on GNOME", rc.stderr)
        self.assertEqual(handed, [])

    def test_own_flags_in_reports_the_force_flag_and_swallows_its_value(self):
        """The seam main() asks, straight: `own_flags_in` names the flag, and
        `scan_backend_argv`'s stripped argv has lost the pair -- the value
        goes with the flag because `_ARITY` gives it one."""
        argv = [FORCE, "52", "--output", "X", "--auto"]
        self.assertEqual(cli.own_flags_in(list(argv)), {FORCE})
        self.assertEqual(cli.scan_backend_argv(list(argv)),
                         (None, False, ["--output", "X", "--auto"]))
        # ...and an output NAMED like it is a value, not a flag
        self.assertEqual(cli.own_flags_in(["--output", FORCE, "--auto"]), set())


class NotADefault(Case):
    def test_nothing_defaults_to_forcing(self):
        self.assertIsNone(cli.Opts().overlap_force)
        self.assertIsNone(cli.Session.overlap_force)

    def test_no_environment_variable_turns_it_on(self):
        unmeasured(self.mock, UMV)
        env = {"WXRANDR_UNSAFE_GNOME_OVERLAP_UNMEASURED": UM,
               "WXRANDR_OVERLAP_FORCE": "1", "WXRANDR_FORCE": UM,
               "WXRANDR_UNMEASURED": UM}
        code, out, err = self.run_cli(FLAG, *MOVE, env=env)
        self.assertEqual(code, 1)
        self.assertIn("is not a build this has been measured on", err)
        self.assertEqual(self.applied(), [])

    def test_the_word_force_appears_in_no_environment_lookup(self):
        """Not a behaviour test: a proof from the source that there is no
        variable to find.  Every os.environ read in the two modules is named."""
        for mod in ("wxrandr/cli.py", "wxrandr/mutter.py",
                    "wxrandr/gnome_overlap.py"):
            with open(os.path.join(ROOT, mod), encoding="utf-8") as fh:
                src = fh.read()
            for m in re.finditer(r"environ(?:\.get)?\(?\[?[\"']([A-Z_]+)[\"']", src):
                self.assertNotIn("OVERLAP", m.group(1), mod)
                self.assertNotIn("FORCE", m.group(1), mod)
                self.assertNotIn("UNSAFE", m.group(1), mod)


class NeverRemembered(Case):
    """A forced run leaves nothing behind, and cannot be covered by anything
    left behind earlier."""

    def consent_file(self):
        return gnome_overlap.consent_path()

    def test_a_forced_run_records_nothing(self):
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.consent_file()))
        self.assertIn("Nothing was recorded", err)

    def test_the_agreement_and_the_flag_cannot_be_typed_together(self):
        for argv in ((FLAG, FORCE, UM, gnome_overlap.ALLOW_FLAG),
                     (gnome_overlap.ALLOW_FLAG, FLAG, FORCE, UM)):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("cannot be used together", err)
            self.assertIn("forcing is what is done when they have not", err)

    def test_an_agreement_already_on_disk_does_not_quieten_a_forced_run(self):
        """Written by hand for exactly the build in the room, which is the only
        way such a file could exist at all -- `--gnome-overlap-allow` cannot
        produce one here.  The paragraph is printed anyway."""
        unmeasured(self.mock, UMV)
        path = self.consent_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"format": gnome_overlap.CONSENT_FORMAT, "shell": UMV,
                       "libmutter": UM, "struct_size": 80,
                       "agreed": "2026-01-01T00:00:00Z", "how": "by hand"}, fh)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("forcing past the one check", err)
        self.assertIn("What may happen:", err)
        self.assertNotIn("as agreed on", err)

    def test_save_consent_refuses_a_forced_reply_on_its_own(self):
        """Belt and braces, and from the *reply* rather than the caller's word
        for it: nothing calls this on the forced path, and if anything ever did
        it would raise rather than write."""
        facts = gnome_overlap.facts(
            {"shell": UMV, "libmutter": UM, "instance_size": 80,
             "forced": {"shell_major": UNMEASURED_MAJOR, "using": _by_size()}})
        self.assertTrue(facts["forced"])
        with self.assertRaises(ValueError) as e:
            gnome_overlap.save_consent(facts, "a test that should not have")
        self.assertIn("forced run is the one that did not", str(e.exception))
        self.assertFalse(os.path.exists(self.consent_file()))

    def test_the_applying_path_reads_the_agreement_only_when_not_forcing(self):
        """From the source, because this is a structural claim: there is one
        read of the agreement on the applying path and it is guarded."""
        with open(os.path.join(ROOT, "wxrandr", "mutter.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def apply_overlap"):]
        body = body[:body.index("\n    def ", 1)]
        self.assertIn("rec = None if force else gnome_overlap.load_consent()", body)
        self.assertEqual(body.count("load_consent"), 1)
        self.assertNotIn("save_consent", body)


# --------------------------------------------------- which refusals are which

class WhichRefusalsAreForceable(Case):
    """The classification, run rather than asserted about.

    A *cautious* refusal is one where the tool does not know enough: this is a
    build nobody here has measured.  That is what forcing is for.  A *certain*
    refusal is one where something is missing or wrong -- and forcing it would
    be pretending the missing thing is there.
    """

    def test_the_list_is_one_entry_and_both_halves_agree_on_it(self):
        self.assertEqual(gnome_overlap.FORCEABLE, ("shell-version",))
        self.assertTrue(gnome_overlap.is_forceable("shell-version"))
        for certain in ("symbols", "struct-size", "sentinel", "pending-dialog",
                        "bounded-read", "layout-mode", "public-view", "table",
                        "meta-typelib", "libmutter", "current", "connectors",
                        "maps", "request", "read-back", "positive-control",
                        "apply", "not-an-overlap", "write", "internal", None):
            self.assertFalse(gnome_overlap.is_forceable(certain), certain)

    def test_the_extension_decides_it_and_the_tool_only_reads_the_answer(self):
        """A reply says whether its own refusal is forceable; the tool falls
        back to the same list only for an extension too old to say."""
        self.assertTrue(gnome_overlap.refusal_is_forceable(
            {"check": "sentinel", "forceable": True}))
        self.assertFalse(gnome_overlap.refusal_is_forceable(
            {"check": "shell-version", "forceable": False}))
        self.assertTrue(gnome_overlap.refusal_is_forceable(
            {"check": "shell-version"}))

    # -- certain: forcing does not get past any of these ---------------------

    def test_a_compositor_that_is_not_gnome_is_certain(self):
        for backend in ("kwin", "sway", "wlr"):
            code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE, backend=backend)
            self.assertEqual(code, 1, backend)
            self.assertIn("only means anything on GNOME", err)
            self.assertEqual(self.ext_calls(), [], backend)

    def test_a_shell_that_will_not_say_its_version_is_certain(self):
        """Forcing is somebody vouching for the build in front of them by
        naming it.  A version string nothing can read is not a build anybody
        can name, so there is nothing for the flag to agree with."""
        self.mock.overlap.shell = None
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("does not report a version this can read", err)
        self.assertEqual(self.ext_calls(), [])

    def test_an_extension_that_is_not_there_is_certain(self):
        ov = unmeasured(self.mock, UMV)
        ov.present = False
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension is not running", err)
        self.assertEqual(self.applied(), [])

    def test_more_than_a_position_is_certain(self):
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE, "--mode", "1280x720")
        self.assertEqual(code, 1)
        self.assertIn("changes more than where the monitors are", err)
        self.assertEqual(self.ext_calls(), [])

    def test_persistent_is_certain(self):
        code, out, err = self.run_cli(FLAG, FORCE, UM, "--persistent", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("cannot be used together", err)
        self.assertEqual(self.ext_calls(), [])

    def test_a_symbol_that_is_absent_is_certain(self):
        ov = unmeasured(self.mock, UMV)
        ov.reply = {"ok": False, "check": "symbols", "forceable": False,
                    "reason": "%s.create_linear is not callable" % _by_size()}
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (symbols)", err)
        self.assertIn("create_linear is not callable", err)
        self.assertNotIn("To add this build", err)
        self.assertEqual(self.applied(), [])

    def test_a_struct_no_description_describes_is_certain(self):
        """Forcing picks a description by size; it cannot write one.  A build
        whose MetaMonitorsConfig is a size nothing here describes has nothing
        to force with, and says so."""
        ov = unmeasured(self.mock, UMV)
        ov.instance_size = 96
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (struct-size)", err)
        self.assertIn("96 bytes", err)
        self.assertEqual(self.applied(), [])

    def test_every_other_check_stays_refused_with_the_flag_typed(self):
        for check in ("sentinel", "pending-dialog", "bounded-read",
                      "public-view", "layout-mode", "read-back",
                      "positive-control", "table", "shared-library"):
            ov = unmeasured(self.mock, UMV)
            ov.reply = {"ok": False, "check": check, "forceable": False,
                        "reason": "made to fire for this test"}
            code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
            self.assertEqual(code, 1, check)
            self.assertIn("the overlap extension refused (%s)" % check, err)
            self.assertEqual(self.applied(), [], check)

    # -- cautious: the one that forcing is for -------------------------------

    def test_the_version_gate_is_the_only_one_that_changes_answer(self):
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("is not a build this has been measured on", err)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 0, err)


# ------------------------------------------------------------ what it prints

class WhatForcingPrints(Case):
    def test_the_paragraph_is_printed_before_the_call_every_time(self):
        unmeasured(self.mock, UMV)
        for _ in range(3):
            code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
            self.assertEqual(code, 0, err)
            head = err[:err.index("What it does:")]
            self.assertIn("forcing past the one check", head)
            for said in ("What is skipped:", "What is not skipped:",
                         "What may happen:", "What is recorded:",
                         "If it happens:"):
                self.assertIn(said, head)

    def test_it_says_what_is_skipped_and_what_cannot_be(self):
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertIn("one thing: that GNOME Shell %s is a build this project has"
                      % UMV,
                      err)
        self.assertIn("chosen instead by the size this build's own GType registry", err)
        for kept in ("sentinel", "modal-grab", "bounded read", "public",
                     "read-back", "positive control", "monitors.xml"):
            self.assertIn(kept, err)
        self.assertIn("none of it can be forced", err)

    def test_it_says_what_may_happen_and_what_to_do_if_it_does(self):
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertIn("gnome-shell crashes", err)
        self.assertIn("every program running in it goes", err)
        # the way back, in full, and it is the same one the ordinary warning gives
        self.assertIn("Ctrl+Alt+F3", err)
        self.assertIn("gnome-extensions disable w11-overlap@w11", err)
        self.assertIn("~/.local/share/gnome-shell/extensions/", err)
        # and the other place the files can be, which is where the .deb puts
        # them and therefore where almost every reader's copy actually is
        self.assertIn("/usr/share/gnome-shell/extensions/", err)

    def test_the_ordinary_warning_is_printed_as_well(self):
        """Forcing adds a paragraph; it never replaces the one that says what
        moves, what it risks, what it saves and how to undo it."""
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        for said in ("What it does:", "What it risks:", "What it saves:",
                     "To undo:"):
            self.assertIn(said, err)
        self.assertIn("move Virtual-2 from +1920+0 to +960+0", err)

    def test_the_apply_says_which_description_it_used(self):
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertIn("applied on an unmeasured GNOME through %s" % _by_size(), err)
        self.assertIn("MetaMonitorsConfig is 80 bytes here", err)

    def test_a_dryrun_is_refused_rather_than_rehearsed(self):
        """It used to Probe and print the paragraph.  It cannot: the checks a
        forced run makes happen inside gnome-shell, so a dry run of one is not
        dry -- it can end the session before anything of ours decides whether
        to write.  So nothing is asked of the extension at all, and the answer
        is one line naming the two honest alternatives.  The unit test of the
        rule itself is `ADryRunCannotBeForced` below."""
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli("--dryrun", FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 1, err)
        self.assertIn("cannot be rehearsed with --dryrun", err)
        self.assertIn("add the build first", err)
        self.assertNotIn("forcing past the one check", err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])


class WhatARefusalPrints(Case):
    """The message somebody who has never seen this code has to add a
    generation from.  A golden, because the whole value of it is that every
    part is there."""

    def refusal(self):
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        return err

    def test_it_names_the_versions_found(self):
        err = self.refusal()
        self.assertIn("GNOME Shell %s" % UMV, err)
        self.assertIn("libmutter-%s.so.0" % UM, err)
        self.assertIn("Meta typelib %s" % UM, err)

    def test_it_names_the_structure_size_this_build_reports(self):
        self.assertIn("MetaMonitorsConfig 80 bytes, from this build's GType "
                      "registry", self.refusal())

    def test_it_names_what_was_expected(self):
        err = self.refusal()
        for line in gnome_overlap.describe_table():
            self.assertIn(line, err)

    def test_it_names_where_the_answer_goes_and_what_to_run(self):
        err = self.refusal()
        self.assertIn("gnome/w11-overlap@w11/generations.json", err)
        self.assertIn("wxrandr/gnome_overlap.py  (GENERATIONS)", err)
        self.assertIn("gen-gir.py --from-header", err)
        self.assertIn("meta-monitor-config-manager.h", err)
        self.assertIn('docs/Technical.md section 6, "Adding a GNOME generation"',
                      err)

    def test_it_offers_the_flag_with_this_machines_number_in_it(self):
        err = self.refusal()
        self.assertIn("%s %s %s" % (FLAG, FORCE, UM), err)
        self.assertIn("skips this one check and no other", err)
        self.assertIn("may end this session", err)

    def test_a_forced_run_is_not_told_how_to_force(self):
        """It is already forcing.  Repeating the offer would be noise on the one
        message that has to be read."""
        ov = unmeasured(self.mock, UMV)
        ov.reply = {"ok": False, "check": "sentinel", "forceable": False,
                    "reason": "the tail is not where it was measured"}
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertNotIn("To try it here now", err)

    def test_with_no_extension_it_says_the_numbers_need_one(self):
        ov = unmeasured(self.mock, UMV)
        ov.present = False
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the rest needs the extension", err)
        self.assertIn("sh gnome/install-overlap.sh", err)

    @unittest.expectedFailure
    def test_an_unmeasured_gnome_with_no_extension_names_the_package_step(self):
        """Fix 19 (F7.0): `INSTALL_HINT` is a constant, and a constant cannot
        say the one thing this reader needs.

        The reader here is on a GNOME nobody has measured -- `%s` -- with the
        package installed and the extension not on the bus.  gnome-shell will
        not load an extension whose metadata.json does not list the running
        major, so `gnome-extensions enable w11-overlap@w11`
        cannot bring it up on that release however many times it is typed:
        measured on the 26.10 stonking-gnome golden, where the shipped bridge
        was "OUT OF DATE" for exactly this reason and the advice printed
        ("reinstall a matching gnome/ from the repo") could not help, because
        the repo's copy carried the same list.

        `gnome/install-overlap.sh --system` is the step that does help -- it
        adds the running major to the installed copy's metadata (measured: one
        line appended to shell-version plus a reboot brought the bridge up on
        51.beta).  `INSTALL_HINT` becomes `install_hint(shell_major)`, and for
        a major the table does not have it says so.
        """ % UMV
        ov = unmeasured(self.mock, UMV)
        ov.present = False
        code, out, err = self.run_cli(FLAG, FORCE, UM, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("gnome-extensions enable w11-overlap@w11", err)
        self.assertIn("install-overlap.sh --system", err)
        self.assertIn("Shell %s" % UM, err)

    @unittest.expectedFailure
    def test_the_documents_name_the_package_step_too(self):
        """Fix 19, the documents half.  README's "#### Overlapping monitors on
        GNOME" tells a reader to run `sh gnome/install-overlap.sh`, which is the
        clone route; somebody who installed the .deb has the files already and
        needs `--system` (and, on an unmeasured major, needs it to rewrite the
        installed metadata).  WXRANDR.md's forcing section says nothing about
        either."""
        section = markdown_section(documents_text("README.md"),
                                   "#### Overlapping monitors on GNOME")
        self.assertTrue(section, "README lost that heading")
        self.assertIn("install-overlap.sh --system", section)
        wx = documents_text(os.path.join("docs", "WXRANDR.md"))
        at = wx.find(gnome_overlap.FORCE_FLAG)
        self.assertNotEqual(at, -1, "WXRANDR.md lost the forcing section")
        self.assertIn("install-overlap.sh --system", wx[at:at + 8000])

    def test_the_agreement_option_gives_the_same_message(self):
        """`--gnome-overlap-allow` on a new release is exactly where a
        maintainer lands, so it is the same message and not a shorter one."""
        unmeasured(self.mock, UMV)
        code, out, err = self.run_cli(gnome_overlap.ALLOW_FLAG)
        self.assertEqual(code, 1)
        self.assertIn("To add this build:", err)
        # ...without the offer to force: that is not what this option does
        self.assertNotIn("To try it here now", err)


# ------------------------------------------------------------------ the table

class TheTable(unittest.TestCase):
    def table(self):
        with open(TABLE_JSON, encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def read(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_the_two_copies_are_one_table(self):
        """The extension reads generations.json; wxrandr cannot (it is
        installed somewhere else entirely), so it carries the same records.
        They have to be identical, and this names the file to fix."""
        js = self.table()["generations"]
        py = list(gnome_overlap.GENERATIONS)
        self.assertEqual([g["shell_major"] for g in js],
                         [g["shell_major"] for g in py],
                         "generations.json and wxrandr/gnome_overlap.py list "
                         "different GNOME releases")
        for a, b in zip(js, py):
            self.assertEqual(a, b, "the GNOME %s records differ" % a["shell_major"])

    def test_every_record_is_complete(self):
        for g in self.table()["generations"]:
            for field in gnome_overlap.TABLE_FIELDS:
                self.assertIn(field, g)
                self.assertIsNotNone(g[field])

    def test_the_extension_metadata_is_derived_from_it(self):
        meta = json.loads(self.read(os.path.join(EXT_DIR, "metadata.json")))
        self.assertEqual(meta["shell-version"],
                         [str(g["shell_major"]) for g in self.table()["generations"]])

    def test_there_is_one_description_per_record_and_no_others(self):
        got = sorted(os.listdir(os.path.join(EXT_DIR, "typelib")))
        self.assertEqual(got, sorted("%s-1.0.typelib" % g["namespace"]
                                     for g in self.table()["generations"]))
        girs = sorted(n for n in os.listdir(GIR_DIR) if n.endswith(".gir"))
        self.assertEqual(girs, sorted("%s-1.0.gir" % g["namespace"]
                                      for g in self.table()["generations"]))

    def test_no_shipped_gir_names_a_shared_library(self):
        """The inverse of what this asserted until 0.4, and the reason is a
        dead session: a `shared-library` makes GIRepository dlopen exactly that
        file, and a forced run picks a description by size on a machine whose
        libmutter is by definition not the one the description was measured
        against.  The dlopen fails and gjs aborts gnome-shell on the first call
        through it -- measured on GNOME 51, on a `--dryrun` that writes
        nothing.  A description is a layout; which library is mapped is proved
        from /proc/self/maps."""
        for g in self.table()["generations"]:
            text = self.read(os.path.join(GIR_DIR, "%s-1.0.gir" % g["namespace"]))
            body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
            self.assertNotIn("shared-library", body)
            self.assertNotIn(g["soname"], body)
            self.assertIn('name="%s"' % g["namespace"], body)

    #: code that builds a *file name* or a *namespace* out of a substituted
    #: value.  Prose about the old scheme is fine and is everywhere; a format
    #: string that ends in `.so`, or a `W11Overlap` with a hole in it, is the
    #: thing mutter 51 made wrong, and there must be none left.
    COMPOSED = (re.compile(r"libmutter-(%[sd]|\$\{|\{|\" *\+|' *\+)[^\n]*\.so"),
                re.compile(r"W11Overlap(%[sd]|\$\{|\{|\" *\+|' *\+)"))

    def test_no_library_name_is_composed_out_of_a_number(self):
        """The rewrite in one assertion.  `libmutter-14` and `libmutter-18`
        appear in prose all over this tree; what must not appear anywhere is
        code that *builds* a soname or a namespace from an integer, because
        mutter 51 stopped following the arithmetic that would make it right."""
        roots = [os.path.join(ROOT, p) for p in
                 ("wxrandr", "warandr", "gnome", "w11common")]
        for root in roots:
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    if not name.endswith((".py", ".js", ".sh")):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                    for line in text.splitlines():
                        if line.lstrip().startswith(("#", "//", "*")):
                            continue        # prose about the old scheme is fine
                        for pat in self.COMPOSED:
                            self.assertIsNone(pat.search(line),
                                              "%s composes a name: %s"
                                              % (path, line.strip()))

    def test_it_can_express_the_next_ubuntu(self):
        """GNOME 51 really does break the old scheme: mutter sets
        `libmutter_api_version = '51'`, so the library is libmutter-51.so.0 and
        the typelib is Meta-51 -- 51 rather than the 19 the 46->14, 50->18
        counter would have produced.  Adding it must be one record, with no code
        anywhere having to learn a new rule.

        This does not make GNOME 51 supported.  Nothing here writes the record
        into the shipped table; it proves the table has room for it."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gen_gir_next", os.path.join(GIR_DIR, "gen-gir.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        table = self.table()
        table["generations"] = table["generations"] + [dict(NEXT)]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "generations.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(table, fh)
            records = gen.load_table(path)
        self.assertEqual([g["shell_major"] for g in records],
                         list(gnome_overlap.SUPPORTED_MAJORS) + [UNMEASURED_MAJOR])
        ns, text = gen.gir(records[-1])
        self.assertEqual(ns, NEXT["namespace"])
        self.assertIn('name="%s"' % NEXT["namespace"], text)
        self.assertEqual(gen.metadata_shell_versions(records),
                         [str(m) for m in gnome_overlap.SUPPORTED_MAJORS] + [UM])

    def test_it_can_express_a_name_that_follows_no_scheme_at_all(self):
        """Not a prediction, a property: the next rename does not have to be one
        anybody guessed."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gen_gir_odd", os.path.join(GIR_DIR, "gen-gir.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        odd = dict(NEXT, shell_major=99, libmutter="mainline",
                   soname="libmutter-mainline.so.0", meta_typelib="mainline",
                   namespace="W11OverlapMainline")
        ns, text = gen.gir(odd)
        self.assertEqual(ns, "W11OverlapMainline")
        self.assertIn('name="W11OverlapMainline"', text)

    def test_a_record_missing_a_field_is_an_error_naming_the_field(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gen_gir_bad", os.path.join(GIR_DIR, "gen-gir.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        table = self.table()
        broken = dict(NEXT)
        broken.pop("soname")
        table["generations"] = [broken]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "generations.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(table, fh)
            with self.assertRaises(SystemExit) as e:
                gen.load_table(path)
        self.assertIn("soname", str(e.exception))

    def test_the_header_deriver_reads_a_whole_real_header(self):
        """`struct _MetaMonitorsConfig` is a prefix of
        `struct _MetaMonitorsConfigKey`, which mutter's real header declares
        first.  A plain `find` therefore laid out the Key struct and reported
        that the head of the config had moved -- on the very file the
        documented procedure tells a maintainer to point this at."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gen_gir_hdr", os.path.join(GIR_DIR, "gen-gir.py"))
        gen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gen)
        header = """
typedef struct _MetaMonitorsConfigKey
{
  GList *monitor_specs;
  MetaLogicalMonitorLayoutMode layout_mode;
} MetaMonitorsConfigKey;

struct _MetaMonitorsConfig
{
  GObject parent;

  MetaMonitorsConfig *parent_config;
  MetaMonitorsConfigKey *key;
  GList *logical_monitor_configs;

  GList *disabled_monitor_specs;
  GList *for_lease_monitor_specs;

  MetaMonitorsConfigFlag flags;

  MetaLogicalMonitorLayoutMode layout_mode;

  MetaMonitorSwitchConfigType switch_config;
};
"""
        ints, rows, size = gen.build_from_header(header)
        self.assertEqual((ints, size), (3, 80))
        at = {f: off for off, _sz, _t, f in rows}
        self.assertEqual(at["logical_monitor_configs"], 40)
        self.assertEqual(at["switch_config"], 72)


class GenGirIsTheOnlyGenerator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = load_gen_gir()

    def test_the_generated_files_are_in_step_with_the_table(self):
        """The checked-in .gir and metadata.json against what the table
        generates, in this process and with no compiler involved.

        It used to run `gen-gir.py --check`, which compiled each description and
        compared the result with the shipped typelib byte for byte -- so on any
        machine whose g-ir-compiler is not the one that produced the checked-in
        files this went red about a description that is correct.  Measured on
        Ubuntu 26.04 and on nixpkgs, both carrying g-ir-compiler 1.86.0: 17
        four-byte words differ per typelib and nothing else does.  The typelibs
        are compared by meaning in tests/test_gnome_overlap.py ShippedExtension;
        what belongs here is the half that is pure text -- that no .gir was
        edited by hand and no record was added without regenerating."""
        for record in self.gen.load_table():
            ns, text = self.gen.gir(record)
            with self.subTest(ns=ns):
                path = os.path.join(GIR_DIR, "%s-1.0.gir" % ns)
                self.assertTrue(os.path.exists(path), path)
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), text)
        with open(os.path.join(EXT_DIR, "metadata.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        table = self.gen.load_table()
        self.assertEqual(meta["shell-version"],
                         self.gen.metadata_shell_versions(table))
        self.assertEqual(meta["description"],
                         self.gen.metadata_description(table, meta["description"]))

    def test_gen_is_gone_and_says_what_replaced_it(self):
        """`--gen 18` used to mean the libmutter generation.  The table is keyed
        by GNOME major now, and a script that takes one number when it means the
        other invites exactly the wrong answer, so the old spelling is an error
        rather than a silent reinterpretation."""
        rc = subprocess.run([sys.executable, os.path.join(GIR_DIR, "gen-gir.py"),
                             "--from-header", "/dev/null", "--gen", "18"],
                            capture_output=True, text=True)
        self.assertNotEqual(rc.returncode, 0)
        self.assertIn("--shell 50", rc.stdout + rc.stderr)


# -------------------------------------------------------------------- the GUI

class TheGuiCannotForce(unittest.TestCase):
    """warandr has no way to reach this, deliberately, and this is the test that
    keeps it that way.

    The reason is the flag's own argument.  What makes forcing defensible is
    that somebody typed the version of the GNOME in front of them, out of the
    refusal they had just read; a checkbox is a click, and a click carries none
    of that.  warandr's overlap route is also gated on
    `wxrandr --gnome-overlap-status`, which answers `unavailable` on an
    unmeasured build -- so the window reports GNOME's refusal there, as it did
    before any of this existed, and the way to overrule it is a terminal.
    """

    def test_no_spelling_of_the_flag_is_anywhere_in_warandr(self):
        for name in sorted(os.listdir(os.path.join(ROOT, "warandr"))):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(ROOT, "warandr", name),
                      encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn(FORCE, text, name)
            self.assertNotIn("unmeasured", text.lower(), name)

    def test_the_command_line_it_builds_can_never_contain_it(self):
        from warandr import randr as wrandr

        class L:
            def overlaps(self):
                return [("DP-1", "DP-2")]
        b = wrandr.Backend(["wxrandr"], True, name="mutter")
        for state in ("agreed", "available", "unavailable", None):
            b.overlap_info = None if state is None else {"state": state}
            self.assertNotIn(FORCE, b.overlap_flag(L()))
            self.assertNotIn(FORCE, b.run_word_for(L()))

    def test_its_own_unsafe_flag_waives_a_question_and_not_a_check(self):
        """`warandr --unsafe-gnome-overlap` exists and means something much
        smaller: do not put the dialog up.  It is not this flag and does not
        imply it."""
        from warandr import cli as wcli
        args = wcli._parser().parse_args(["--unsafe-gnome-overlap"])
        self.assertTrue(args.unsafe_gnome_overlap)
        self.assertFalse(hasattr(args, "unsafe_gnome_overlap_unmeasured"))


# ---------------------------------------- the same decisions, run under node

NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(NODE is None, "no node to run rules.js")
class RulesJSForce(unittest.TestCase):
    """The extension's half, run for real.

    Everything the force path decides lives in rules.js, which has no `gi`
    imports precisely so that it can be run here: the gate that compares the
    named major with the running one, the selection of a description by struct
    size, and the list of forceable checks.  Checking the Python and leaving the
    JavaScript to a code review would be checking the half that cannot kill a
    session.
    """

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="rules-force-")
        shutil.copy(os.path.join(EXT_DIR, "rules.js"),
                    os.path.join(cls.dir, "rules.mjs"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, True)

    def call(self, body, arg):
        src = ("import * as R from '%s/rules.mjs';\n"
               "const input = JSON.parse(process.argv[2] || '{}');\n"
               % self.dir) + body
        path = os.path.join(self.dir, "case.mjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        rc = subprocess.run([NODE, path, json.dumps(arg)],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        return json.loads(rc.stdout)

    def table(self):
        with open(TABLE_JSON, encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_gate_refuses_everything_but_this_gnome(self):
        cases = [[None, "51.0"],                       # not forced at all
                 [{}, "51.0"],                         # forced with nothing named
                 [{"shell_major": "51"}, "51.0"],      # a string, not a number
                 [{"shell_major": 51.5}, "51.0"],      # not a whole number
                 [{"shell_major": 49}, "51.0"],        # somebody else's machine
                 [{"shell_major": 51}, "banana"],      # a version nothing can read
                 [{"shell_major": 51}, "51.0"]]        # the one that passes
        got = self.call("console.log(JSON.stringify(input.map("
                        "c => R.forceGate(c[0], c[1]))));\n", cases)
        self.assertEqual(got[0], "not forced")
        for i in range(1, 6):
            self.assertIsNotNone(got[i], cases[i])
        self.assertIn("names GNOME Shell 49", got[4])
        self.assertIsNone(got[6])

    def test_the_same_gate_as_the_python_side(self):
        """Two implementations of one rule, and they answer the same on every
        case: the tool refuses out here, and the extension refuses again on its
        own account, so a disagreement would be a hole."""
        cases = [[{"shell_major": 51}, "51.0"], [{"shell_major": 49}, "51.0"],
                 [{"shell_major": 51}, "51"], [{"shell_major": 51}, ""],
                 [None, "51.0"]]
        got = self.call("console.log(JSON.stringify(input.map("
                        "c => R.forceGate(c[0], c[1]))));\n", cases)
        for js, (force, shell) in zip(got, cases):
            py = gnome_overlap.force_reason(force, shell)
            self.assertEqual(js is None, py is None, (force, shell, js, py))

    def test_a_description_is_selected_by_size_and_never_guessed(self):
        table = self.table()
        got = self.call("console.log(JSON.stringify(input.sizes.map("
                        "s => R.selectByStructSize(input.table, s))));\n",
                        {"table": table, "sizes": [72, 80, 96, 0, None, -8]})
        self.assertEqual(got[0]["generation"]["namespace"], _by_size(72))
        self.assertEqual(got[1]["generation"]["namespace"], _by_size(80))
        for i in (2, 3, 4, 5):
            self.assertNotIn("generation", got[i])
            self.assertIn("refusal", got[i])
        self.assertIn("no description shipped here describes", got[2]["refusal"])
        self.assertIn("Forcing cannot invent a description", got[2]["refusal"])

    def test_two_descriptions_that_disagree_are_a_refusal_not_a_coin_toss(self):
        """One size, two shapes: neither can be assumed and the size cannot
        choose, so it refuses."""
        table = self.table()
        table["generations"] = [dict(g, struct_size=80, tail_slots=1 + i)
                                for i, g in enumerate(table["generations"])]
        got = self.call("console.log(JSON.stringify("
                        "R.selectByStructSize(input, 80)));\n", table)
        self.assertNotIn("generation", got)
        self.assertIn("cannot say which one", got["refusal"])
        self.assertIn("do not agree on what is in them", got["refusal"])

    def test_two_descriptions_of_one_size_that_agree_are_one_description(self):
        """The shipped table is already this: GNOME 50 and GNOME 51 are both 80
        bytes with three tail slots, measured separately, and the two .girs
        differ only in their name.  Refusing that as "ambiguous" would take the
        escape hatch away from every release after the first repeat, so the
        newest is chosen and the answer says the others describe the same
        bytes."""
        table = self.table()
        table["generations"] = [dict(g, struct_size=80, tail_slots=3)
                                for g in table["generations"]]
        got = self.call("console.log(JSON.stringify("
                        "R.selectByStructSize(input, 80)));\n", table)
        self.assertNotIn("refusal", got)
        self.assertEqual(got["generation"]["namespace"],
                         table["generations"][-1]["namespace"])
        self.assertEqual(got["sameShape"],
                         [g["namespace"] for g in table["generations"][:-1]])

    def test_the_forceable_list_is_the_same_on_both_sides(self):
        checks = ["shell-version", "struct-size", "sentinel", "pending-dialog",
                  "bounded-read", "public-view", "symbols", "table", "libmutter",
                  "shared-library"]
        got = self.call("console.log(JSON.stringify(input.map("
                        "c => R.isForceable(c))));\n", checks)
        for check, js in zip(checks, got):
            self.assertEqual(js, gnome_overlap.is_forceable(check), check)

    def test_the_soname_scan_finds_the_library_and_not_its_relatives(self):
        """The next Ubuntu's real mapping lines, and the ones beside them.
        `libmutter-clutter-51.so.0` is mapped into the same process and is not
        the library this writes to; a scan that returned both would refuse every
        session on 51 for the wrong reason."""
        maps = "\n".join([
            "7f0000000000-7f0000200000 r--p 00000000 fd:02 100001   "
            "/usr/lib/x86_64-linux-gnu/libmutter-51.so.0.0.0",
            "7f0000200000-7f0000300000 r--p 00000000 fd:02 100002   "
            "/usr/lib/x86_64-linux-gnu/mutter-51/libmutter-clutter-51.so.0.0.0",
            "7f0000300000-7f0000400000 r--p 00000000 fd:02 100003   "
            "/usr/lib/x86_64-linux-gnu/mutter-51/libmutter-cogl-51.so.0.0.0",
            "7f0000400000-7f0000500000 r--p 00000000 fd:02 100004   "
            "/usr/lib/x86_64-linux-gnu/mutter-51/libmutter-mtk-51.so.0.0.0",
            ""])
        got = self.call("console.log(JSON.stringify(R.mutterSonames(input)));\n",
                        maps)
        self.assertEqual(got, ["libmutter-51.so.0"])

    def test_the_soname_token_is_read_and_not_computed(self):
        got = self.call("console.log(JSON.stringify(input.map("
                        "s => R.sonameToken(s))));\n",
                        ["libmutter-14.so.0", "libmutter-51.so.0",
                         "libmutter-mainline.so.0", "libmutter-clutter-51.so.0",
                         "libfoo.so.0", ""])
        self.assertEqual(got, ["14", "51", "mainline", None, None, None])

    def test_the_table_is_read_and_not_restated(self):
        table = self.table()
        got = self.call("console.log(JSON.stringify({"
                        "majors: R.knownMajors(input),"
                        "lines: R.describeTable(input)}));\n", table)
        self.assertEqual(got["majors"], list(gnome_overlap.SUPPORTED_MAJORS))
        self.assertEqual(got["lines"], gnome_overlap.describe_table())


class TheDescriptionNamesNoLibrary(unittest.TestCase):
    """What a forced run on GNOME 51 cost before it was fixed, held down.

    A `shared-library` in the description makes GIRepository dlopen that exact
    file.  A forced run picks its description by struct size, on a machine whose
    libmutter is by definition not the one the description was measured against
    -- so the dlopen fails, and gjs does not raise: it asserts and aborts the
    process, which on Wayland is the session.  It was measured doing exactly
    that on Ubuntu 26.10, on a `--dryrun` that writes nothing.
    """

    #: the C symbols the descriptions make callable -- 17 function entries over
    #: 13 distinct identifiers, because g_memdup2 is described five times, once
    #: per record shape it copies.  Written out, because "what this extension
    #: can reach inside gnome-shell" is exactly the list that must not grow by
    #: accident.
    SYMBOLS = {
        "g_memdup2", "g_strndup", "g_object_ref", "g_object_unref",
        "g_type_name_from_instance", "memcpy",
        "meta_monitor_manager_get_config_manager",
        "meta_monitor_config_manager_get_current",
        "meta_monitor_config_manager_create_linear",
        "meta_monitors_config_get_switch_config",
        "meta_monitors_config_set_switch_config",
        "meta_verify_monitors_config",
        "meta_monitor_manager_apply_monitors_config",
    }

    def girs(self):
        out = {}
        for name in sorted(os.listdir(GIR_DIR)):
            if name.endswith(".gir"):
                with open(os.path.join(GIR_DIR, name), encoding="utf-8") as fh:
                    out[name] = fh.read()
        self.assertTrue(out)
        return out

    def test_the_string_copy_is_owned_by_whoever_asked_for_it(self):
        """`g_strndup` returns freshly allocated memory, so the description has
        to say `transfer-ownership="full"` or gjs converts the bytes to a JS
        string and frees nothing.

        It said "none" until 0.4.1: one connector name leaked per monitor per
        read, inside gnome-shell, which lives for the session.  Small, and
        wrong, and the kind of wrong that a description is the only place to
        fix -- the extension has no address to free."""
        want = ('<function name="strn" c:identifier="g_strndup">\n'
                '      <return-value transfer-ownership="full">')
        for name, text in self.girs().items():
            self.assertIn(want, text, name)
        gen = load_gen_gir()
        self.assertIn(want, gen.BODY)
        # and in the artifact gnome-shell actually loads.  The .gir is a source
        # file; the typelib is what gjs reads, it is checked in compiled, and
        # the two have been out of step before -- the typelibs shipped up to
        # 0.4.1 say caller_owns 0 for exactly this function while their .gir
        # said "full".  2 is GI_TRANSFER_EVERYTHING.
        shipped = os.path.join(EXT_DIR, "typelib")
        for g in gnome_overlap.GENERATIONS:
            ns = g["namespace"]
            with self.subTest(ns=ns):
                summary, why = gen.typelib_summary(shipped, ns)
                if why == gen.NO_GIREPOSITORY:
                    self.skipTest("no GIRepository")
                self.assertIsNotNone(summary, "%s: %s" % (ns, why))
                self.assertEqual(summary["functions"]["strn"]["symbol"], "g_strndup")
                self.assertEqual(summary["functions"]["strn"]["transfer"], 2)

    @unittest.expectedFailure
    def test_every_bounded_copy_is_freed(self):
        """Fix 16 (F1.7), deferred: each `g_memdup2` wrapper returns
        `transfer-ownership="none"`, so every struct the extension walks is
        copied and never freed -- 72 or 80 bytes per struct, a few hundred bytes
        per Probe or ApplyOverlap, in gnome-shell.

        Either the return becomes `full`, or an `fr` (`g_free`, taking the
        guint64 the extension already holds) is described and `extension.js`
        calls `lib.fr(` after each copy.  Both are changes to the read path
        inside a live compositor, which is why this is written down rather than
        guessed at: gen-gir.py's `_memdup` docstring carries the same note."""
        with open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8") as fh:
            js = fh.read()
        for name, text in self.girs().items():
            for block in re.findall(r'<function name="dup_\w+".*?</function>',
                                    text, re.S):
                freed = ('transfer-ownership="full"' in block.split("</return-value>")[0]
                         or ('c:identifier="g_free"' in text and "lib.fr(" in js))
                self.assertTrue(freed, "%s: %s" % (name, block.splitlines()[0]))

    def test_nothing_else_is_callable_through_a_description(self):
        """The list of C symbols the extension can reach inside gnome-shell,
        whole.  It grows only on purpose."""
        for name, text in self.girs().items():
            got = set(re.findall(r'c:identifier="([^"]+)"', text))
            self.assertEqual(got - {"g_free"}, self.SYMBOLS, name)

    def test_the_generator_does_not_tell_a_maintainer_to_name_a_library(self):
        """gen-gir.py's TABLE comment said `soname` "goes into the .gir's
        `shared-library`" long after HEAD stopped putting it there and started
        explaining, at length, why nothing may.  A maintainer reading the
        comment beside the table -- which is where somebody adding a GNOME
        reads -- was being told to make the one edit that has actually ended a
        session here (measured, Ubuntu 26.10, a forced --dryrun)."""
        with open(os.path.join(GIR_DIR, "gen-gir.py"), encoding="utf-8") as fh:
            script = fh.read()
        table = script[script.index("#: THE TABLE"):script.index("TABLE_PATH =")]
        self.assertNotRegex(table, r"soname[\s\S]{0,200}shared-library")
        self.assertIn("/proc/self/maps", table)

    def test_the_soname_reaches_no_generated_byte(self):
        """The proof rather than the promise: change the record's soname to
        something absurd and the description that comes out is identical."""
        gen = load_gen_gir()
        record = gen.load_table()[0]
        self.assertEqual(gen.gir(record),
                         gen.gir(dict(record, soname="libfoo.so.9")))
        for name, text in self.girs().items():
            self.assertNotIn("shared-library=", text, name)
            self.assertNotIn("libmutter-", text.split("-->")[-1], name)

    def test_the_extension_refuses_a_description_naming_an_absent_library(self):
        """The guard for descriptions this project did not generate: one left
        behind by an older install, or built by hand.  It is a static read of
        the loaded namespace, before any call is made through it."""
        src = open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8").read()
        self.assertIn("sharedLibraries(", src)
        self.assertIn("refuse('shared-library'", src)
        # ...and it happens before anything is called through the description
        self.assertLess(src.index("refuse('shared-library'"),
                        src.index("lib.strn(0, 0)"))
        self.assertFalse(gnome_overlap.is_forceable("shared-library"))

    def test_nothing_is_called_through_a_description_of_the_wrong_size(self):
        """`struct-size` is documented as refusing having read nothing, so the
        one call that proves the description can reach the process has to come
        after the size comparison, not before it."""
        src = open(os.path.join(EXT_DIR, "extension.js"), encoding="utf-8").read()
        self.assertLess(src.index("this build's MetaMonitorsConfig is ${actual}"),
                        src.index("lib.strn(0, 0)"))


class TheInstallerSeesThePackagesCopy(unittest.TestCase):
    """`install-overlap.sh` writes into the user's extension directory, but the
    .deb puts the files in the system one and enables nothing -- so on the
    machine almost every reader has, the script was reporting on a directory
    that does not exist.  Measured on a default 26.04 desktop that had taken the
    package: `--check` said `files: not installed, table: MISSING` about an
    extension gnome-shell had loaded, and `--uninstall` said it had `removed` a
    path in ~/.local that was never created.  `install-bridge.sh --check`
    already looks in both directories; this is its sibling catching up.

    The two blocks are run as themselves, sliced out of the script, the way
    `name_this_shell` is above: the alternative is a test whose answer depends
    on what is installed on the machine running it."""

    SH = os.path.join(ROOT, "gnome", "install-overlap.sh")
    UUID = "w11-overlap@w11"

    def _slice(self, first, last):
        src = open(self.SH, encoding="utf-8").read()
        start = src.index(first)
        end = src.index(last, start) + len(last)
        return src[start:end]

    def run_block(self, block, dest, system_dir, system=0):
        script = ('UUID="%s"\nSYSTEM=%d\nDEST="%s"\nSYSTEM_DIR="%s"\n%s\n'
                  % (self.UUID, system, dest, system_dir, block))
        r = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def _tree(self, root, uuid_dir=True):
        """A directory holding an installed extension, as far as these blocks
        are concerned: `extension.js` is what both of them test for."""
        d = os.path.join(root, self.UUID) if uuid_dir else root
        os.makedirs(os.path.join(d, "typelib"), exist_ok=True)
        for n in ("extension.js", "generations.json"):
            with open(os.path.join(d, n), "w", encoding="utf-8") as fh:
                fh.write("{}\n")
        with open(os.path.join(d, "typelib", "W11Overlap18-1.0.typelib"), "w") as fh:
            fh.write("x")
        return d

    # -- where the files are found ------------------------------------------

    FOUND = ("FOUND=\n", 'FOUND=$SYSTEM_DIR/$UUID\nfi')

    def found(self, dest, system_dir):
        out = self.run_block(self._slice(*self.FOUND) + '\necho "FOUND=$FOUND"',
                             dest, system_dir)
        return out.strip().split("FOUND=", 1)[1].strip()

    def test_the_users_copy_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = self._tree(os.path.join(tmp, "user"), uuid_dir=False)
            self.assertEqual(self.found(user, os.path.join(tmp, "sys")), user)

    def test_the_packages_copy_is_found_when_there_is_no_users_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysdir = os.path.join(tmp, "sys")
            self._tree(sysdir)
            self.assertEqual(self.found(os.path.join(tmp, "nothing-here"), sysdir),
                             os.path.join(sysdir, self.UUID))

    def test_a_users_copy_shadows_the_packages_one(self):
        """gnome-shell prefers the user's, so the report has to name that one."""
        with tempfile.TemporaryDirectory() as tmp:
            user = self._tree(os.path.join(tmp, "user"), uuid_dir=False)
            sysdir = os.path.join(tmp, "sys")
            self._tree(sysdir)
            self.assertEqual(self.found(user, sysdir), user)

    def test_neither_is_a_blank_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.found(os.path.join(tmp, "a"),
                                        os.path.join(tmp, "b")), "")

    # -- what --uninstall says ----------------------------------------------

    UNINST = ('    if [ -e "$DEST" ]; then', '--system --uninstall)."\n    fi')

    def uninstall(self, dest, system_dir):
        return self.run_block(self._slice(*self.UNINST), dest, system_dir)

    def test_it_says_what_it_removed_when_it_removed_something(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = self._tree(os.path.join(tmp, "user"), uuid_dir=False)
            out = self.uninstall(user, os.path.join(tmp, "sys"))
            self.assertIn("removed %s" % user, out)
            self.assertFalse(os.path.exists(user))

    def test_it_claims_no_removal_when_there_was_nothing_there(self):
        """The line this whole class exists for."""
        with tempfile.TemporaryDirectory() as tmp:
            out = self.uninstall(os.path.join(tmp, "never-created"),
                                 os.path.join(tmp, "sys"))
            self.assertIn("nothing of this script's to remove", out)
            self.assertNotIn("removed", out)

    def test_it_names_the_packages_copy_and_leaves_it_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysdir = os.path.join(tmp, "sys")
            kept = self._tree(sysdir)
            out = self.uninstall(os.path.join(tmp, "never-created"), sysdir)
            self.assertIn(kept, out)
            self.assertIn("sudo apt remove w11", out)
            self.assertTrue(os.path.exists(os.path.join(kept, "extension.js")),
                            "another package's files are not this script's to delete")


class TheInstallerNamesTheRunningShell(unittest.TestCase):
    """gnome-shell will not load an extension whose metadata.json does not name
    the running Shell major -- so on exactly the builds
    `--unsafe-gnome-overlap-unmeasured` exists for, the extension was installed,
    enabled, OUT_OF_DATE, never on the bus, and the flag had nothing to reach.
    The installer names the running major in the INSTALLED copy, and this is
    that function, run.
    """

    SH = os.path.join(ROOT, "gnome", "install-overlap.sh")

    def run_it(self, metadata, major="51"):
        """Extract name_this_shell() and run it over one metadata.json."""
        src = open(self.SH, encoding="utf-8").read()
        start = src.index("name_this_shell() {")
        end = src.index("\n}\n", start) + 3
        func = src[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "metadata.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(metadata)
            script = ("SYSTEM=1\nME=1\nTARGET_UID=1\nTARGET_USER=x\n"
                      'DEST="%s"\n%s\nname_this_shell "%s"\n'
                      % (tmp, func, major))
            r = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), r.stderr

    def test_it_adds_the_major_to_an_array_written_on_one_line(self):
        meta, err = self.run_it('{"shell-version": ["46", "50"], "x": 1}\n')
        self.assertEqual(meta["shell-version"], ["46", "50", "51"])
        self.assertIn("added", err)

    def test_it_adds_the_major_to_an_array_written_over_several(self):
        """The format gen-gir.py writes as soon as there are three records, and
        the one a one-line rule silently did nothing to."""
        meta, err = self.run_it('{\n  "shell-version": [\n    "46",\n'
                                '    "50"\n  ],\n  "x": 1\n}\n')
        self.assertEqual(meta["shell-version"], ["46", "50", "51"])
        self.assertIn("added", err)

    def test_it_says_nothing_when_the_major_is_already_there(self):
        for text in ('{"shell-version": ["46", "51"]}\n',
                     '{\n  "shell-version": [\n    "46",\n    "51"\n  ]\n}\n'):
            meta, err = self.run_it(text)
            self.assertEqual(meta["shell-version"], ["46", "51"])
            self.assertEqual(err, "")

    def test_it_takes_the_major_out_of_a_full_version(self):
        meta, _err = self.run_it('{"shell-version": ["46"]}\n', major="51.beta")
        self.assertEqual(meta["shell-version"], ["46", "51"])


class ThePackageShipsWhatTheExtensionReads(unittest.TestCase):
    """The table is read at call time, out of the extension's own directory, so
    a package that installs the extension without it installs an extension that
    refuses.  It did: `generations.json` was missing from debian/*.install from
    the day the table was introduced until GNOME 51 was added, because nothing
    exercised the packaged copy."""

    def test_every_file_the_extension_reads_is_in_the_package(self):
        install = open(os.path.join(ROOT, "debian", "w11.install"),
                       encoding="utf-8").read()
        for name in ("extension.js", "rules.js", "metadata.json",
                     "generations.json", "org.w11.Overlap1.xml"):
            self.assertIn("%s/%s" % (gnome_overlap.UUID, name), install, name)
        self.assertIn("%s/typelib/*" % gnome_overlap.UUID, install)

    def test_the_directory_holds_nothing_the_package_leaves_out(self):
        """The other direction, so that a file added beside the extension is
        either packaged or deliberately not."""
        install = open(os.path.join(ROOT, "debian", "w11.install"),
                       encoding="utf-8").read()
        for name in sorted(os.listdir(EXT_DIR)):
            if name == "typelib":
                continue
            self.assertIn("%s/%s" % (gnome_overlap.UUID, name), install, name)


class TheMetadataDescriptionComesFromTheTable(unittest.TestCase):
    def test_it_names_every_measured_release(self):
        """It said "46 and 50" by hand, and stayed saying it when 51 was
        measured.  gen-gir.py writes that sentence now, and --check fails if it
        is stale."""
        meta = json.load(open(os.path.join(EXT_DIR, "metadata.json"),
                              encoding="utf-8"))
        majors = [str(g["shell_major"]) for g in gnome_overlap.GENERATIONS]
        listed = "%s and %s" % (", ".join(majors[:-1]), majors[-1])
        self.assertIn("Only GNOME Shell %s have been measured" % listed,
                      meta["description"])


class ADryRunCannotBeForced(unittest.TestCase):
    """Measured on a real GNOME 51: the first forced run ever attempted was a
    `--dryrun`, and it ended the session. Forcing picks a description by size,
    that description names the library it was built for, and the interpreter
    inside gnome-shell aborts rather than raising when it cannot be opened. So
    a dry run promises a safe look at an unmeasured build and cannot give one."""

    def _err(self, argv):
        from wxrandr import cli
        try:
            cli.parse(argv)
        except Exception as e:                      # ArgErr
            return str(e)
        return ""

    def test_it_is_refused(self):
        err = self._err(["--unsafe-gnome-overlap", "--unsafe-gnome-overlap-unmeasured",
                         "51", "--dryrun", "--output", "X", "--pos", "1x0"])
        self.assertIn("cannot be rehearsed with --dryrun", err)

    def test_the_message_says_where_to_look(self):
        err = self._err(["--unsafe-gnome-overlap", "--unsafe-gnome-overlap-unmeasured",
                         "51", "--dryrun", "--output", "X", "--pos", "1x0"])
        self.assertIn("add the build first", err)

    def test_a_dry_run_without_forcing_is_fine(self):
        # the ordinary flag rehearses safely: every check runs and refuses
        err = self._err(["--unsafe-gnome-overlap", "--dryrun",
                         "--output", "X", "--pos", "1x0"])
        self.assertNotIn("cannot be rehearsed", err)

    def test_forcing_without_a_dry_run_is_fine(self):
        err = self._err(["--unsafe-gnome-overlap", "--unsafe-gnome-overlap-unmeasured",
                         "51", "--output", "X", "--pos", "1x0"])
        self.assertNotIn("cannot be rehearsed", err)


if __name__ == "__main__":
    unittest.main()
