"""The agreement to `--unsafe-gnome-overlap`: where it lives, what it records,
what it silences, and -- at length -- everything it cannot do.

The feature is a paragraph that stops being printed.  That is all it is, and
most of this file exists to hold that down, because the tempting version of this
feature (a remembered yes that lets the tool skip ahead) is the one that would
eventually cost somebody their session.  So:

* an agreement is recorded only against a build the six checks have just passed
  on, and it records what they measured -- the Shell version, libmutter's
  generation and the size this build's `MetaMonitorsConfig` turned out to be;
* it stops applying the moment any of that changes, which is what a distribution
  upgrade does;
* it is never consulted for whether to check anything.  Every refusal in
  tests/test_gnome_overlap.py is re-run here *with* an agreement recorded, and
  every one of them still refuses;
* and it is not the flag: an overlapping layout with no `--unsafe-gnome-overlap`
  is still GNOME's refusal, agreement or no agreement.

The harness is tests/test_gnome_overlap.py's -- a mock DisplayConfig, a mock
org.gnome.Shell and a mock extension on a bus in this process, with
XDG_CONFIG_HOME pointed at a temporary directory, which is where the agreement
then lands.
"""

import io
import json
import os
import re
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_gnome_overlap import WARNING, Case, _redirect
from warandr import randr as wrandr
from wxrandr import cli
from hacks.display import gnome_overlap
from hacks.display.core import Fatal

os.environ["W11_PASSTHROUGH"] = "never"

FLAG = gnome_overlap.FLAG
ALLOW = gnome_overlap.ALLOW_FLAG
FORGET = gnome_overlap.FORGET_FLAG
STATUS = gnome_overlap.STATUS_FLAG

#: the overlapping move every test in here applies: Virtual-2 from +1920+0 to
#: +960+0, half on top of Virtual-1
MOVE = ("--output", "Virtual-2", "--pos", "960x0")


class ConsentCase(Case):
    """A `Case` with the agreement file in reach."""

    def path(self):
        return gnome_overlap.consent_path()

    def record(self, **over):
        """Write an agreement by hand -- for the builds `--gnome-overlap-allow`
        would refuse to record one for, which is where the interesting questions
        are."""
        rec = {"format": gnome_overlap.CONSENT_FORMAT, "shell": "50.1",
               "libmutter": 18, "struct_size": 80,
               "agreed": "2026-01-02T03:04:05Z", "how": "by hand"}
        rec.update(over)
        os.makedirs(os.path.dirname(self.path()), exist_ok=True)
        with open(self.path(), "w") as fh:
            fh.write(json.dumps(rec))
        return rec

    def agree(self):
        """The supported way: run the command and check it worked."""
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 0, err)
        return out

    def stderr_lines(self, err):
        return [ln for ln in err.splitlines() if ln.strip()]

    def run_cli_no_session(self, *argv, how=None):
        """`cli.main` with a Session that cannot be built at all: a text console,
        or a session that will not start.

        `how` is which of the two ways that happens: `Fatal` for a backend that
        was named and is not there, and by default the real `_cant_open()`, which
        is what a machine with no compositor does -- it writes xrandr's own line
        to stderr and raises SystemExit, and answering anyway is the whole point
        of this command."""
        def boom(_sess, forced=None):
            if how is Fatal:
                raise Fatal("Can't open display\n")
            cli.Session._cant_open()
        orig = cli.Session.__init__
        cli.Session.__init__ = boom
        out, err = io.StringIO(), io.StringIO()
        try:
            with _redirect(out, err):
                code = cli.main(list(argv))
        finally:
            cli.Session.__init__ = orig
        return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------- the recording

class Recording(ConsentCase):
    def test_it_lands_where_xdg_says_and_nowhere_else(self):
        self.assertEqual(self.path(),
                         os.path.join(self.tmp, "w11", "overlap-consent.json"))
        self.agree()
        self.assertTrue(os.path.exists(self.path()))

    def test_the_default_is_under_dot_config(self):
        self.assertEqual(gnome_overlap.consent_path({"HOME": "/home/u"}),
                         "/home/u/.config/w11/overlap-consent.json")
        # the spec's rule, the same one monitors_xml.default_path follows
        self.assertEqual(gnome_overlap.consent_path({"XDG_CONFIG_HOME": "rel",
                                                     "HOME": "/home/u"}),
                         "/home/u/.config/w11/overlap-consent.json")

    def test_what_it_records_is_what_the_checks_measured(self):
        """Not a constant in this tree: the three numbers come out of the
        extension's answer, which got them out of the running compositor."""
        self.mock.overlap.shell = "46.0"
        self.mock.overlap.libmutter = 14
        self.mock.overlap.instance_size = 72
        self.mock.overlap.declared_size = 72
        self.agree()
        with open(self.path()) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["shell"], "46.0")
        self.assertEqual(rec["libmutter"], 14)
        self.assertEqual(rec["struct_size"], 72)
        self.assertEqual(rec["format"], gnome_overlap.CONSENT_FORMAT)
        self.assertEqual(rec["how"], "wxrandr " + ALLOW)
        self.assertRegex(rec["agreed"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_the_library_it_agrees_to_is_the_one_the_checks_ran_against(self):
        """Four facts, not three: the version string cannot see a libmutter
        swapped under it, and Ubuntu swaps one inside a stable release."""
        self.mock.overlap.build_id = "c0ffee" + "0" * 34
        out = self.agree()
        with open(self.path()) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["libmutter_build"], "c0ffee" + "0" * 34)
        # and it is in the words the user is shown before it is written
        self.assertIn("build c0ffee000000", out)

    def test_a_library_that_will_not_say_its_build_is_still_agreeable(self):
        """The build id is read out of an ELF note.  A library without one is
        odd, not dangerous, and this feature does not refuse on odd."""
        self.mock.overlap.build_id = None
        out = self.agree()
        with open(self.path()) as fh:
            rec = json.load(fh)
        self.assertIsNone(rec["libmutter_build"])
        self.assertIn("libmutter-18,", out)

    def test_the_size_is_read_back_from_the_check_when_the_field_is_missing(self):
        """An extension from before the field existed still reports the size in
        the check that measured it, and that is a number this may record."""
        self.mock.overlap.instance_size = None      # drop the field
        self.agree()
        with open(self.path()) as fh:
            self.assertEqual(json.load(fh)["struct_size"], 80)

    def test_allow_runs_every_check_and_writes_nothing_to_the_compositor(self):
        self.agree()
        self.assertEqual(self.ext_calls(), ["Probe"])
        self.assertEqual(self.applied(), [])

    def test_allow_says_what_is_being_agreed_to(self):
        out = self.agree()
        # the six checks, named
        for name in ("shell-version", "typelib", "sentinel", "pending-dialog",
                     "bounded-read", "public-view"):
            self.assertIn("check %s: " % name, out)
        self.assertIn("Agreeing to --unsafe-gnome-overlap on GNOME Shell 50.1 "
                      "(libmutter-18 build 0f3a1b2c3d4e, MetaMonitorsConfig 80 "
                      "bytes).", out)
        self.assertIn("What it risks:", out)
        self.assertIn("gnome-shell is the\n                        session", out)
        self.assertIn("What is agreed:", out)
        self.assertIn("this build and no other", out)
        self.assertIn("What is not agreed:", out)
        self.assertIn("Every check above runs again on", out)
        self.assertIn("To withdraw:          wxrandr --gnome-overlap-forget", out)
        self.assertIn("If the session dies:", out)
        self.assertIn("recorded in %s" % self.path(), out)

    def test_nothing_is_recorded_on_an_unmeasured_shell(self):
        self.mock.overlap.shell = "48.3"
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 1)
        self.assertIn("not a build this has been measured on", err)
        self.assertFalse(os.path.exists(self.path()))
        # one call, and it is the Probe that refuses at the first check: it is
        # what puts the size this build reports into the message a maintainer
        # reads (tests/test_overlap_force.py).  Nothing private was read.
        self.assertEqual(self.ext_calls(), ["Probe"])

    def test_nothing_is_recorded_when_the_extension_is_not_running(self):
        self.mock.overlap.present = False
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 1)
        self.assertIn("sh gnome/install-overlap.sh", err)
        self.assertFalse(os.path.exists(self.path()))

    def test_nothing_is_recorded_when_a_check_refuses(self):
        self.mock.overlap.reply = {"ok": False, "check": "struct-size",
                                   "reason": "this build's MetaMonitorsConfig is 88 bytes"}
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (struct-size)", err)
        self.assertFalse(os.path.exists(self.path()))

    def test_nothing_is_recorded_without_a_size_to_record(self):
        """A reply with no measurable struct size is not something to agree to:
        there would be nothing for a later run to compare against."""
        self.mock.overlap.instance_size = None
        self.mock.overlap.strip_typelib_check = True
        code, out, err = self.run_cli(ALLOW)
        self.assertEqual(code, 1)
        self.assertIn("did not say which build it verified", err)
        self.assertFalse(os.path.exists(self.path()))

    def test_there_is_nothing_to_agree_to_off_gnome(self):
        for backend in ("kwin", "sway", "wlr"):
            code, out, err = self.run_cli(ALLOW, backend=backend)
            self.assertEqual(code, 1, backend)
            self.assertIn("there is nothing to agree to", err)
            self.assertFalse(os.path.exists(self.path()))

    def test_an_agreement_is_recordable_on_every_measured_generation(self):
        """`--gnome-overlap-allow` over each record in GENERATIONS.

        The label is the point.  Through GNOME 50 libmutter's API version was a
        counter of its own -- 46 carried libmutter-14, 50 carried libmutter-18 --
        and code that spelled it `major - 32` worked by accident; mutter 51 set
        `libmutter_api_version = '51'` (meson.build line 10 of the 51~rc
        tarball) and the arithmetic is gone.  So what is recorded is the table's
        text label, and `"51"` must not arrive as the integer 51 -- a record
        written as an int reads back as one, and `consent_drift` would then be
        comparing 51 with "51".  Measured on the 46.0 golden: the file really
        written was {"libmutter": "14", ..., "shell": "46.0", "struct_size": 72}.
        """
        for g in gnome_overlap.GENERATIONS:
            with self.subTest(shell=g["shell_major"]):
                ov = self.mock.overlap
                ov.shell = "%d.0" % g["shell_major"]
                ov.libmutter = g["libmutter"]
                ov.instance_size = ov.declared_size = g["struct_size"]
                ov.sonames = [g["soname"]]
                ov.meta_typelib = g["meta_typelib"]
                gnome_overlap.forget_consent()
                out = self.agree()
                self.assertIn("GNOME Shell %d.0 (libmutter-%s build "
                              % (g["shell_major"], g["libmutter"]), out)
                self.assertIn("MetaMonitorsConfig %d bytes)" % g["struct_size"], out)
                with open(self.path(), encoding="utf-8") as fh:
                    rec = json.load(fh)
                self.assertEqual(rec["libmutter"], g["libmutter"])
                self.assertIsInstance(rec["libmutter"], str)
                self.assertEqual(rec["shell"], "%d.0" % g["shell_major"])
                self.assertEqual(rec["struct_size"], g["struct_size"])

    def test_the_generation_label_compares_as_text_however_it_was_written(self):
        """An agreement written by an older wxrandr holds the label as a JSON
        number; this one writes a string.  They are the same generation and
        `consent_drift` has to say so -- otherwise upgrading wxrandr would
        withdraw every agreement on disk."""
        for recorded, reported in ((51, "51"), ("51", 51), (18, "18"), ("18", 18)):
            self.assertIsNone(gnome_overlap.consent_drift(
                {"libmutter": recorded, "struct_size": 80},
                {"libmutter": reported, "struct_size": 80}), (recorded, reported))
        # ...and two different generations still differ, whichever way round
        self.assertIn("libmutter generation", gnome_overlap.consent_drift(
            {"libmutter": "18", "struct_size": 80},
            {"libmutter": 51, "struct_size": 80}))

    def test_an_agreement_from_one_measured_release_does_not_cover_the_next(self):
        """50.1 and 51.0 are both measured, and an agreement to one is not an
        agreement to the other: they carry different libmutters (18 and 51)
        even though MetaMonitorsConfig happens to be 80 bytes in both, so size
        alone would have said yes."""
        self.record(shell="50.1", libmutter="18", struct_size=80)
        self.mock.overlap.shell = "51.0"
        self.mock.overlap.libmutter = "51"
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertNotIn("as agreed on", err)
        self.assertTrue(err.startswith(WARNING.split("What it does:")[0]), err[:300])
        ok, why = gnome_overlap.consent_covers({"shell": "50.1"}, "51.0")
        self.assertFalse(ok)
        self.assertIn("given on GNOME Shell 50.1", why)
        self.assertIn("this session is GNOME Shell 51.0", why)

    def test_a_dryrun_of_the_agreement_records_nothing(self):
        """Everywhere else in this program `--dryrun` writes nothing, and this
        is the one command whose entire effect is a file.

        Measured at HEAD: `wxrandr --dryrun --gnome-overlap-allow` ran the
        probe, printed the agreement paragraph and then "recorded in
        <path>" -- a dry run that recorded a consent, which is the one thing
        somebody types a dry run to be sure of not doing.  The checks still run,
        because the paragraph names the build they measured and a rehearsal of
        this command that skipped them would be showing a made-up build."""
        code, out, err = self.run_cli("--dryrun", ALLOW)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["Probe"])
        self.assertEqual(self.applied(), [])
        self.assertFalse(os.path.exists(self.path()))
        self.assertIn("dryrun: nothing was recorded", out)
        self.assertIn(self.path(), out)
        self.assertNotIn("recorded in", out)
        # it still shows what would have been agreed to
        self.assertIn("Agreeing to --unsafe-gnome-overlap on GNOME Shell 50.1", out)

    def test_a_layout_typed_beside_the_agreement_is_a_usage_error(self):
        """`--gnome-overlap-allow` answers for itself and returns before any
        stanza is applied, so `--output ... --pos ...` beside it is a move that
        never happens.

        Measured at HEAD: `--gnome-overlap-allow --output Virtual-2 --pos 960x0`
        exited 0, probed, wrote the agreement and moved nothing, with no line
        anywhere saying so.  It is refused at parse time now, the way
        `--persistent` and the flag are: before a session is opened and before
        anything on the bus is asked."""
        code, out, err = self.run_cli(ALLOW, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("cannot both happen", err)
        self.assertIn("Virtual-2", err)
        self.assertIn("Record the agreement first, then apply the layout", err)
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual(self.applied(), [])
        self.assertFalse(os.path.exists(self.path()))

    def test_a_query_typed_beside_it_is_accepted_and_then_ignored(self):
        """The refusal above is about a *layout*, not about company: `--query`
        describes a session rather than asking for a change to one, so it is
        accepted -- and then it does not happen.

        Measured here: `wxrandr --gnome-overlap-allow --query` exits 0, probes,
        prints the agreement paragraph and `recorded in ...`, and prints no
        `Screen 0:` listing at all, because `_do_overlap_allow()` answers for
        the whole run and returns.  That is the same silent drop fix 23 refuses
        for a layout, in a much smaller size: nothing is lost but a listing the
        user can ask for again.  Pinned as it is rather than as it ought to be,
        so that a later fix which prints the listing has to come past this
        test."""
        code, out, err = self.run_cli(ALLOW, "--query")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["Probe"])
        self.assertTrue(os.path.exists(self.path()))
        self.assertIn("recorded in %s" % self.path(), out)
        self.assertNotIn("Screen 0:", out)
        self.assertNotIn("Virtual-1 connected", out)

    def test_a_file_that_is_not_an_agreement_is_not_one(self):
        for bad in ("", "{", "[]", '{"shell": "50.1"}',
                    '{"format": 99, "shell": "50.1"}',
                    '{"format": 1}'):
            os.makedirs(os.path.dirname(self.path()), exist_ok=True)
            with open(self.path(), "w") as fh:
                fh.write(bad)
            self.assertIsNone(gnome_overlap.load_consent(), bad)
            # ...and the tool is therefore loud, which is the safe direction
            code, out, err = self.run_cli(FLAG, *MOVE)
            self.assertEqual(code, 0, err)
            self.assertTrue(err.startswith(WARNING), bad)


# --------------------------------------------------------------- withdrawing

class Withdrawing(ConsentCase):
    def test_forget_removes_it_and_says_so(self):
        self.agree()
        code, out, err = self.run_cli(FORGET)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "withdrawn: %s removed\n" % self.path())
        self.assertFalse(os.path.exists(self.path()))

    def test_forget_with_nothing_recorded_is_not_an_error(self):
        code, out, err = self.run_cli(FORGET)
        self.assertEqual(code, 0, err)
        self.assertIn("nothing to withdraw", out)

    def test_forget_opens_no_session_at_all(self):
        """The moment somebody most needs this is from a text console, with a
        session that will not start.  So it must not need one: Session is made
        to explode, and the withdrawal still happens."""
        self.agree()

        def boom(sess, forced=None):
            raise AssertionError("--gnome-overlap-forget built a Session")
        orig = cli.Session.__init__
        cli.Session.__init__ = boom
        try:
            code, out, err = self.run_cli(FORGET)
        finally:
            cli.Session.__init__ = orig
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(self.path()))

    def test_the_warning_is_back_after_a_withdrawal(self):
        self.agree()
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(len(self.stderr_lines(err)), 1, err)
        self.run_cli(FORGET)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertTrue(err.startswith(WARNING), err[:300])


# ----------------------------------------------------- the build it is scoped to

class ADifferentBuild(ConsentCase):
    def test_a_changed_shell_version_asks_again(self):
        self.record(shell="50.1")
        self.mock.overlap.shell = "50.2"
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        # the whole paragraph, for the build that was not agreed to
        self.assertTrue(err.startswith(WARNING.split("What it does:")[0]), err[:300])
        self.assertIn("(GNOME Shell 50.2)", err)
        for part in ("What it risks:", "What it saves:", "To undo:",
                     "If the session dies:"):
            self.assertIn(part, err)
        self.assertNotIn("as agreed on", err)

    def test_consent_covers_names_both_builds(self):
        ok, why = gnome_overlap.consent_covers({"shell": "46.0"}, "50.1")
        self.assertFalse(ok)
        self.assertIn("given on GNOME Shell 46.0", why)
        self.assertIn("this session is GNOME Shell 50.1", why)
        self.assertEqual(gnome_overlap.consent_covers({"shell": "50.1"}, "50.1"),
                         (True, None))
        self.assertFalse(gnome_overlap.consent_covers(None, "50.1")[0])

    def test_a_moved_struct_withdraws_the_agreement_after_the_apply(self):
        """The version string alone cannot see this: a build that kept its name
        and moved its private layout.  The extension's own struct-size check
        makes it a refusal rather than a bad write, so what is left to do here is
        bookkeeping -- and the bookkeeping is to stop being quiet."""
        self.record(shell="50.1", struct_size=80)
        self.mock.overlap.instance_size = self.mock.overlap.declared_size = 96
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("applying a layout GNOME refuses", err)
        self.assertIn("not the one that was agreed to", err)
        self.assertIn("MetaMonitorsConfig size 96, not 80", err)
        self.assertIn("the next run will ask again", err)
        self.assertFalse(os.path.exists(self.path()))
        # ...and it does
        self.mock.overlap.instance_size = self.mock.overlap.declared_size = 96
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertTrue(err.startswith(WARNING), err[:300])

    def test_a_new_libmutter_under_the_same_shell_withdraws_it(self):
        """The update a user actually receives.

        `ShellVersion` cannot see this: Ubuntu 24.04 ships mutter 46.2 under
        GNOME Shell 46.0, and 46.0 -> 46.2 under one unchanged shell version was
        measured applying with all six checks green.  Nothing about the layout
        moved -- four libmutter builds on each release, one layout -- so this is
        not a danger signal and does not refuse anything.  What it is is the end
        of what was agreed to, and the agreement text promises exactly this:
        "stops applying the moment any of that changes"."""
        self.record(shell="50.1", libmutter=18, libmutter_build="a" * 40)
        self.mock.overlap.build_id = "b" * 40
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("libmutter build bbbbbbbbbbbb, not aaaaaaaaaaaa", err)
        self.assertIn("the next run will ask again", err)
        self.assertFalse(os.path.exists(self.path()))
        # and the next run does ask, in full
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertTrue(err.startswith(WARNING), err[:300])

    @unittest.expectedFailure
    def test_a_new_libmutter_is_asked_about_before_the_write_and_not_after(self):
        """Fix 17 (F1.3): when a record exists, one Probe before the apply, and
        `consent_covers(rec, facts(probe))` -- not `consent_covers(rec, version)`
        -- decides whether the paragraph is printed.

        Today the only thing compared before the write is the GNOME Shell
        version string, and that string demonstrably cannot see a libmutter
        swapped under it: Ubuntu 24.04 carries mutter 46.2 under GNOME Shell
        46.0, and 46.0 -> 46.2 under one unchanged shell version was measured
        applying with all six checks green.  So an agreement given for build
        aaaa.. is *spent* quietly on build bbbb.., and the disagreement is only
        noticed by `consent_drift()` after the eight bytes are already in.  The
        agreement text promises the opposite -- "stops applying the moment any
        of that changes" -- and withdrawing an agreement after acting on it is
        not stopping.

        wxrandr/gnome_overlap.py:834-849 plus `_overlap_client` in
        wxrandr/mutter.py is where the extra Probe goes; the same Probe is what
        `--gnome-overlap-status` should be answering from.
        """
        self.record(shell="50.1", libmutter=18, libmutter_build="a" * 40)
        self.mock.overlap.build_id = "b" * 40
        code, out, err = self.run_cli(FLAG, *MOVE)
        # the check runs before the write, so the extension is asked twice
        self.assertEqual(self.ext_calls(), ["Probe", "ApplyOverlap"])
        # ...and the user reads the paragraph, not a line saying they agreed
        self.assertTrue(err.startswith(WARNING.split("What it does:")[0]), err[:300])
        self.assertNotIn("as agreed on", err)
        self.assertEqual(code, 0, err)

    @unittest.expectedFailure
    def test_the_status_asks_the_same_question_the_apply_would(self):
        """Fix 17 (F1.3), the reporting half: `--gnome-overlap-status` answers
        from `overlap_available()`, which reads the Shell's version property and
        whether the extension owns its bus name -- and nothing else.  With an
        agreement recorded against a libmutter build that is no longer the one
        mapped, it therefore says `agreed` about an agreement the next apply
        will withdraw.  It has to say `available`, and say what it would ask
        about."""
        self.record(shell="50.1", libmutter=18, libmutter_build="a" * 40)
        self.mock.overlap.build_id = "b" * 40
        lines = self.lines_of(STATUS)
        self.assertEqual(lines[0], "available")
        self.assertTrue(any("libmutter build" in ln for ln in lines), lines)

    def lines_of(self, *argv):
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return [ln for ln in out.splitlines() if ln.strip()]

    def test_the_same_library_is_not_a_difference(self):
        self.record(shell="50.1", libmutter=18,
                    libmutter_build=self.mock.overlap.build_id)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertNotIn("not the one that was agreed to", err)
        self.assertTrue(os.path.exists(self.path()))

    def test_an_agreement_from_before_the_build_was_recorded_still_stands(self):
        """A file written by an older wxrandr names no build.  That is not a
        difference, because it is not a disagreement: only two things that both
        say something can differ."""
        self.record(shell="50.1", libmutter=18, struct_size=80)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertNotIn("not the one that was agreed to", err)
        self.assertTrue(os.path.exists(self.path()))

    def test_a_moved_generation_withdraws_it_too(self):
        self.record(shell="50.1", libmutter=18)
        self.mock.overlap.libmutter = 19
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("libmutter generation 19, not 18", err)
        self.assertFalse(os.path.exists(self.path()))

    def test_drift_is_silent_when_the_build_is_the_one_agreed_to(self):
        f = {"shell": "50.1", "libmutter": 18, "struct_size": 80}
        self.assertIsNone(gnome_overlap.consent_drift(dict(f), f))
        self.assertIsNone(gnome_overlap.consent_drift(None, f))
        # a fact the reply did not carry is not a difference
        self.assertIsNone(gnome_overlap.consent_drift(
            dict(f), {"shell": "50.1", "libmutter": None, "struct_size": None}))


class RootsAgreementIsNotTheUsers(ConsentCase):
    """Fix 18 (F7.4): `consent_path()` is `$XDG_CONFIG_HOME` or `$HOME/.config`,
    read out of the environment of whoever is running the command.

    `sudo wxrandr` keeps the caller's HOME on Ubuntu (sudo's default
    `env_keep` does not include HOME, but `always_set_home` is off in
    /etc/sudoers there, so HOME survives), so root reads and writes the
    invoking user's agreement file -- and, the other way round, a root shell
    with its own HOME reads root's, agrees on root's behalf, and the session
    that a wrong offset would end belongs to uid 1000.  The module's own
    docstring says the opposite: "Per user, never per system: it is the user's
    own session that a wrong offset ends, so root's answer must not stand in
    for anybody else's."

    What the fix pins: when `os.geteuid()` and `w11common.session.session_uid()`
    disagree, the agreement is keyed on the *session* user's home
    (`pwd.getpwuid(session_uid()).pw_dir`), and if that cannot be worked out
    the paragraph is printed regardless.  The control below is the same run
    with the two uids agreeing, which must keep today's quiet behaviour
    exactly."""

    def setUp(self):
        super().setUp()
        self.record(shell="50.1", libmutter=18, struct_size=80)

    @unittest.expectedFailure
    def test_roots_run_does_not_spend_the_session_users_agreement(self):
        with mock.patch("os.geteuid", return_value=0), \
                mock.patch("w11common.session.session_uid", return_value=1000):
            code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertNotIn("as agreed on", err)
        self.assertTrue(err.startswith(WARNING.split("What it does:")[0]), err[:300])

    @unittest.expectedFailure
    def test_the_status_root_reads_is_not_the_session_users(self):
        with mock.patch("os.geteuid", return_value=0), \
                mock.patch("w11common.session.session_uid", return_value=1000):
            code, out, err = self.run_cli(STATUS)
        self.assertEqual(code, 0, err)
        self.assertEqual([ln for ln in out.splitlines() if ln.strip()][0], "available")

    def test_the_same_user_keeps_the_quiet_line(self):
        """The control: two uids that agree are the ordinary case, and nothing
        about it may change."""
        with mock.patch("os.geteuid", return_value=1000), \
                mock.patch("w11common.session.session_uid", return_value=1000):
            code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertIn("as agreed on 2026-01-02", err)
        self.assertNotIn("What it risks:", err)


# ------------------------------------- what no agreement can do: skip a check

class NoAgreementSkipsAnything(ConsentCase):
    """Every refusal in tests/test_gnome_overlap.py, re-run with an agreement
    recorded for exactly the build in the room."""

    def setUp(self):
        super().setUp()
        self.record(shell="50.1", libmutter=18, struct_size=80)

    def test_an_unmeasured_shell_is_still_refused(self):
        # the agreement even names that shell: it is still refused
        self.record(shell="48.3", libmutter=18, struct_size=80)
        self.mock.overlap.shell = "48.3"
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("not a build this has been measured on", err)
        # the Probe that refuses at the first check, for the maintainer message
        self.assertEqual(self.ext_calls(), ["Probe"])
        self.assertEqual(self.applied(), [])

    def test_a_shell_that_will_not_say_its_version_is_still_refused(self):
        self.mock.overlap.shell = None
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("cannot tell which GNOME Shell this is", err)
        self.assertEqual(self.ext_calls(), [])

    def test_a_missing_extension_is_still_refused(self):
        self.mock.overlap.present = False
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("sh gnome/install-overlap.sh", err)
        self.assertEqual(self.applied(), [])

    def test_more_than_a_position_is_still_refused(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2",
                                      "--pos", "960x0", "--mode", "1280x720")
        self.assertEqual(code, 1)
        self.assertIn("changes more than where the monitors are", err)
        self.assertEqual(self.ext_calls(), [])

    def test_a_refusal_from_the_extension_is_still_a_refusal(self):
        self.mock.overlap.reply = {"ok": False, "check": "public-view",
                                   "reason": "monitor 0: x reads 4919, Mutter says 0"}
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("the overlap extension refused (public-view)", err)
        self.assertIn("4919", err)
        self.assertEqual(self.applied(), [])

    def test_the_checks_still_run_inside_the_shell(self):
        """The point of the whole design: an agreed apply is the same call, with
        the same checks behind it, as an unagreed one."""
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.run_cli(FORGET)
        before = len(self.mock.overlap.calls)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual([c[0] for c in self.mock.overlap.calls[before:]],
                         ["ApplyOverlap"])

    def test_persistent_is_still_refused(self):
        code, out, err = self.run_cli(FLAG, "--persistent", *MOVE)
        self.assertEqual(code, 1)
        self.assertIn("cannot be used together", err)

    def test_it_is_still_nothing_off_gnome(self):
        code, out, err = self.run_cli(FLAG, *MOVE, backend="kwin")
        self.assertEqual(code, 1)
        self.assertIn("only means anything on GNOME", err)

    def test_an_agreement_is_not_the_flag(self):
        """The loudest one.  An overlapping layout with no --unsafe-gnome-overlap
        is GNOME's refusal, however many times the user has agreed to anything."""
        code, out, err = self.run_cli(*MOVE)
        self.assertEqual(code, 1)
        self.assertIn("GNOME's Mutter refused this layout", err)
        # it went to DisplayConfig, which said no; the extension was never asked
        self.assertEqual(self.ext_calls(), [])
        self.assertEqual([c[1] for c in self.applied()], [1])

    def test_an_agreed_layout_gnome_accepts_never_reaches_the_extension(self):
        code, out, err = self.run_cli(FLAG, "--output", "Virtual-2", "--pos", "1920x0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ext_calls(), [])
        self.assertNotIn("agreed", err)

    def test_the_agreement_is_read_after_the_last_refusal_and_used_only_to_print(self):
        """A source-level fence.  Everything that can refuse an apply happens
        before the agreement is even read, and the value it produces reaches
        nothing but the two functions that write to stderr."""
        src = open(os.path.join(ROOT, "hacks", "display", "mutter.py"), encoding="utf-8").read()
        body = src[src.index("    def apply_overlap(self"):]
        body = body[:body.index("\n    def ", 10)]
        read = body.index("load_consent()")
        # every guard in the apply path is above it
        for guard in ("route = self.overlap_route(state, targets)",
                      "if route is None:", "self._overlap_client(plan, force)"):
            self.assertLess(body.index(guard), read, guard)
        # and a forced run does not read it at all: there is no agreement that
        # can make one of those quieter (tests/test_overlap_force.py)
        self.assertIn("rec = None if force else gnome_overlap.load_consent()", body)
        # ...and the extension's own answer is still what decides, below it
        self.assertLess(read, body.index('if not reply.get("ok"):'))
        # `quiet` reaches warn(), warn_bare() and applied_text(quiet=...), and
        # nothing else at all
        uses = [ln.strip() for ln in body.splitlines()
                if re.search(r"\bquiet\b", ln) and not ln.lstrip().startswith("#")]
        self.assertTrue(uses)
        for line in uses:
            self.assertTrue(line == "if quiet:"
                            or line.startswith("quiet, _why = gnome_overlap.consent_covers(")
                            or "warn(" in line or "warn_bare(" in line
                            or "applied_text(" in line, line)


# ------------------------------------------------------------- the quiet path

class Quiet(ConsentCase):
    def setUp(self):
        super().setUp()
        self.record(shell="50.1", libmutter=18, struct_size=80)

    def test_an_agreed_apply_says_one_line(self):
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            err,
            'xrandr: --unsafe-gnome-overlap: applying a layout GNOME refuses '
            '("logical monitors not adjacent (an overlap counts, and so does a '
            'gap)"), as agreed on 2026-01-02\n')
        # and it really applied, through the extension
        self.assertEqual(self.ext_calls(), ["ApplyOverlap"])
        self.assertEqual(self.applied(), [])

    def test_none_of_the_paragraph_survives(self):
        code, out, err = self.run_cli(FLAG, *MOVE)
        for gone in ("What it does:", "What it risks:", "To undo:",
                     "If the session dies:", "GNOME's rule this breaks:",
                     "mutter's own validator on the result:",
                     "monitors.xml: unchanged"):
            self.assertNotIn(gone, err)

    def test_a_saved_file_that_moved_is_still_shouted_about(self):
        """What survives `quiet` is the one line that is not reassurance -- and
        the audit after it, which an agreed run owes whatever the reply said.

        The reply is `FakeOverlap.saved_config_moved`, the shape
        extension.js:1100-1108 really produces: `applied: true`, `ok: false`, no
        `check`, no `reason`.  Before wxrandr/mutter.py:851 grew `and not
        reply.get("applied")` this went down the refusal path, so an agreed run
        printed "refused (?): no reason given", skipped `applied_text()`
        entirely and never reached `consent_drift()` -- an agreement left
        standing for a build it had just stopped matching.

        The build the extension reports is the build that was agreed to, so the
        audit finds nothing and the agreement survives; that it *ran* is proved
        by a spy, and by where in the run it ran (after the write, with the
        apply already in the extension's call log).  Deliberately not proved by
        moving the build id under a standing agreement: an agreement spent on a
        build the checks never passed on and withdrawn only afterwards is
        finding F1.3, which fix 17 removes by probing before the write -- a
        test that demanded that ordering would go red the day the bug is
        fixed."""
        self.mock.overlap.saved_config_moved = True
        self.record(shell="50.1", libmutter=18, struct_size=80,
                    libmutter_build=self.mock.overlap.build_id)
        seen = []
        real = gnome_overlap.consent_drift

        def spy(rec, facts):
            seen.append((len(self.mock.overlap.calls), rec, facts))
            return real(rec, facts)
        gnome_overlap.consent_drift = spy
        self.addCleanup(setattr, gnome_overlap, "consent_drift", real)
        code, out, err = self.run_cli(FLAG, *MOVE)
        self.assertIn("CHANGED across this call", err)
        self.assertIn("please report it", err)
        self.assertNotIn("no reason given", err)
        # the quiet line is still the first thing said: this was an agreed run
        self.assertTrue(err.startswith("xrandr: --unsafe-gnome-overlap: applying "
                                       "a layout GNOME refuses"), err[:200])
        # the audit ran, once, on the reply of the call that had just been made
        self.assertEqual(len(seen), 1, seen)
        at, rec, facts = seen[0]
        self.assertEqual(at, 1)                      # the ApplyOverlap has happened
        self.assertEqual(facts["libmutter_build"], self.mock.overlap.build_id)
        self.assertEqual(rec["libmutter_build"], self.mock.overlap.build_id)
        # ...found nothing, so the agreement stands
        self.assertNotIn("the agreement has been withdrawn", err)
        self.assertTrue(os.path.exists(self.path()))
        # ...and only then the status the `ok: false` decides
        self.assertEqual(code, 1)

    def test_the_dryrun_is_never_quiet(self):
        """--dryrun exists to be read; it is what somebody runs to find out what
        would happen, so it says all of it whatever is recorded."""
        code, out, err = self.run_cli(FLAG, "--dryrun", *MOVE)
        self.assertEqual(code, 0, err)
        self.assertTrue(err.startswith(WARNING), err[:300])
        self.assertIn("dryrun: nothing was written", err)
        self.assertEqual(self.ext_calls(), ["Probe"])

    def test_the_quiet_line_names_the_rule_and_the_day(self):
        line = gnome_overlap.quiet_line({"agreed": "2026-01-02T03:04:05Z"},
                                        "logical monitors overlap")
        self.assertEqual(line, '--unsafe-gnome-overlap: applying a layout GNOME '
                               'refuses ("logical monitors overlap"), as agreed '
                               'on 2026-01-02\n')
        self.assertLess(len(line), 120)


# ------------------------------------------------------------------ the status

class Status(ConsentCase):
    def lines(self, *argv, **kw):
        code, out, err = self.run_cli(STATUS, *argv, **kw)
        self.assertEqual(code, 0, err)
        return out.splitlines()

    def test_available_when_the_route_is_there_and_nothing_is_agreed(self):
        lines = self.lines()
        self.assertEqual(lines[0], "available")
        self.assertIn("shell: 50.1", lines)
        self.assertIn("extension: running", lines)
        self.assertIn("asks first: nothing is recorded", lines)
        self.assertIn("file: %s" % self.path(), lines)

    def test_agreed_once_it_is(self):
        self.agree()
        lines = self.lines()
        self.assertEqual(lines[0], "agreed")
        self.assertIn("agreed for: GNOME Shell 50.1 (libmutter-18 build "
                      "0f3a1b2c3d4e, MetaMonitorsConfig 80 bytes)", lines)
        self.assertIn("agreed by: wxrandr --gnome-overlap-allow", lines)

    def test_available_again_when_the_agreement_is_for_another_build(self):
        self.record(shell="46.0")
        lines = self.lines()
        self.assertEqual(lines[0], "available")
        self.assertTrue(any("asks first:" in ln and "46.0" in ln for ln in lines), lines)

    def test_unavailable_on_an_unmeasured_shell(self):
        self.mock.overlap.shell = "48.3"
        lines = self.lines()
        self.assertEqual(lines[0], "unavailable")
        self.assertTrue(any("not a build this has been measured on" in ln
                            for ln in lines), lines)

    def test_unavailable_with_the_extension_absent(self):
        self.mock.overlap.present = False
        lines = self.lines()
        self.assertEqual(lines[0], "unavailable")
        self.assertTrue(any("install-overlap.sh" in ln for ln in lines), lines)

    def test_unavailable_off_gnome(self):
        for backend in ("kwin", "sway", "wlr"):
            lines = self.lines(backend=backend)
            self.assertEqual(lines[0], "unavailable")
            self.assertTrue(any(backend in ln for ln in lines), lines)

    def test_it_answers_with_no_session_at_all(self):
        """A status query has to be answerable from a text console with a
        session that will not start: the record is a file, and half of what this
        reports is in it.

        Both ways a session fails to build, because they are different
        exceptions: a named backend that is not there is a `Fatal`, and no
        compositor at all goes through `_cant_open()`, which is xrandr's own
        "Can't open display" and a `SystemExit`.  The second one is the text
        console this command is for, and it used to walk straight past the
        answer."""
        self.agree()
        for how in (Fatal, None):
            code, out, err = self.run_cli_no_session(STATUS, how=how)
            self.assertEqual(code, 0, err)
            lines = out.splitlines()
            self.assertEqual(lines[0], "unavailable", how)
            self.assertIn("reason: Can't open display", lines)
            self.assertIn("agreed by: wxrandr --gnome-overlap-allow", lines)
            self.assertIn("file: %s" % self.path(), lines)
            # and the stray line does not land on top of the answer
            self.assertNotIn("Can't open display", err)

    def test_it_reads_nothing_out_of_gnome_shell(self):
        """A GUI runs this at startup.  It must not cost a walk of Mutter's
        private structures to answer 'would this work?'."""
        self.agree()
        before = len(self.mock.overlap.calls)
        self.lines()
        self.assertEqual(self.mock.overlap.calls[before:], [])
        self.assertEqual(self.applied(), [])


# ------------------------------------------------------------------- warandr

class WarandrSide(unittest.TestCase):
    """The GUI's half: it never imports any of the above -- it runs wxrandr and
    reads the first line of `--gnome-overlap-status`."""

    def backend(self, name="mutter", state=None, wayland=True):
        b = wrandr.Backend(["wxrandr"], wayland, env={}, name=name)
        if state is not None:
            b.overlap_info = {"state": state}
        return b

    def layout(self, overlapping):
        class L:
            def overlaps(self):
                return [("DP-1", "DP-2")] if overlapping else []
        return L()

    def test_the_flag_is_added_only_for_an_overlap_on_gnome_with_a_route(self):
        for name, state, over, want in (
                ("mutter", "agreed", True, ["--unsafe-gnome-overlap"]),
                ("mutter", "available", True, ["--unsafe-gnome-overlap"]),
                ("mutter", "unavailable", True, []),
                ("mutter", None, True, []),
                ("mutter", "agreed", False, []),
                ("kwin", "agreed", True, []),
                ("x11", None, True, []),
                ("sway", None, True, [])):
            b = self.backend(name, state, wayland=name != "x11")
            self.assertEqual(b.overlap_flag(self.layout(over)), want,
                             (name, state, over))
        self.assertEqual(self.backend("mutter", "agreed").overlap_flag(None), [])

    def test_asking_stops_the_moment_it_is_agreed(self):
        self.assertTrue(self.backend("mutter", "available")
                        .overlap_needs_asking(self.layout(True)))
        self.assertFalse(self.backend("mutter", "agreed")
                         .overlap_needs_asking(self.layout(True)))
        self.assertFalse(self.backend("mutter", "available")
                         .overlap_needs_asking(self.layout(False)))
        self.assertFalse(self.backend("kwin", "available")
                         .overlap_needs_asking(self.layout(True)))

    def test_the_command_the_window_shows_is_the_command_it_runs(self):
        b = self.backend("mutter", "available")
        b.forced = "mutter"
        self.assertEqual(b.run_word_for(self.layout(True)),
                         "wxrandr --backend mutter --unsafe-gnome-overlap")
        self.assertEqual(b.run_word_for(self.layout(False)),
                         "wxrandr --backend mutter")

    def test_gnome_stops_refusing_an_overlap_once_there_is_a_route(self):
        self.assertIsNotNone(self.backend("mutter").overlap_refusal())
        self.assertIsNotNone(self.backend("mutter", "unavailable").overlap_refusal())
        for state in ("available", "agreed"):
            b = self.backend("mutter", state)
            self.assertIsNone(b.overlap_refusal(), state)
            self.assertIn("w11-overlap extension", b.overlap_note())
            self.assertIn("gone at the next login", b.overlap_note())

    def test_one_sentence_for_both_states(self):
        """It is also the comment a saved script carries; what a script says
        about a layout must not depend on who saved it."""
        self.assertEqual(self.backend("mutter", "available").overlap_note(),
                         self.backend("mutter", "agreed").overlap_note())

    def test_the_status_is_parsed_into_the_token_and_its_lines(self):
        b = self.backend("mutter")
        b.run = lambda args, timeout=30: (
            0, "agreed\nshell: 50.1\nagreed on: 2026-01-02T03:04:05Z\n", "")
        info = b.read_overlap_status()
        self.assertEqual(info["state"], "agreed")
        self.assertEqual(info["shell"], "50.1")
        self.assertEqual(info["agreed on"], "2026-01-02T03:04:05Z")
        self.assertIn("agreed on 2026-01-02T03:04:05Z", "\n".join(b.info_lines()))

    def test_a_wxrandr_too_old_for_the_option_is_simply_no_route(self):
        b = self.backend("mutter")
        b.run = lambda args, timeout=30: (1, "", "xrandr: unrecognized option\n")
        self.assertEqual(b.read_overlap_status()["state"], "unavailable")
        b.run = lambda args, timeout=30: (0, "some other tool's output\n", "")
        self.assertEqual(b.read_overlap_status()["state"], "unavailable")

    def test_a_backend_that_cannot_be_run_is_no_route_and_no_traceback(self):
        b = self.backend("mutter")

        def boom(args, timeout=30):
            raise wrandr.RandrError("cannot run wxrandr")
        b.run = boom
        self.assertEqual(b.read_overlap_status()["state"], "unavailable")

    def test_nothing_but_gnome_is_even_asked(self):
        for name in ("kwin", "sway", "wlr", "x11"):
            b = self.backend(name, wayland=name != "x11")
            b.run = lambda args, timeout=30: self.fail("%s was asked" % name)
            self.assertIsNone(b.read_overlap_status())

    def test_warandr_does_not_import_any_of_this(self):
        """warandr runs wxrandr; it never reaches into it.  The agreement is
        wxrandr's file, read and written by wxrandr, and warandr only ever sees
        the first line of a status command."""
        for name in sorted(os.listdir(os.path.join(ROOT, "warandr"))):
            if not name.endswith(".py"):
                continue
            src = open(os.path.join(ROOT, "warandr", name), encoding="utf-8").read()
            # Reaching in is what is forbidden, not saying the words: argparse turns
            # warandr's own --unsafe-gnome-overlap into the attribute
            # args.unsafe_gnome_overlap, which is warandr's flag, not wxrandr's module.
            self.assertNotIn("import wxrandr", src, name)
            self.assertNotIn("from wxrandr", src, name)
            self.assertNotIn("hacks.display.gnome_overlap", src, name)
            self.assertNotIn("import gnome_overlap", src, name)


class WarandrNeverAsks(unittest.TestCase):
    """`warandr --unsafe-gnome-overlap`: somebody who has already decided,
    starting the window from a hotkey or a desktop entry where a dialog is the
    whole interaction. It waives being asked and nothing else."""

    def _backend(self, state, overlaps=True):
        from warandr import randr
        b = randr.Backend(["wxrandr"], True, env={}, name="mutter")
        b.overlap_state = lambda: state
        layout = types.SimpleNamespace(overlaps=lambda: overlaps)
        return b, layout

    def test_it_does_not_ask_with_no_agreement_recorded(self):
        b, layout = self._backend("available")
        self.assertTrue(b.overlap_needs_asking(layout))      # without the flag
        b.overlap_never_ask = True
        self.assertFalse(b.overlap_needs_asking(layout))

    def test_it_still_only_applies_where_the_route_is_really_there(self):
        # waiving the question must not invent the capability
        b, layout = self._backend("unavailable")
        b.overlap_never_ask = True
        self.assertEqual(b.overlap_flag(layout), [])
        self.assertFalse(b.overlap_needs_asking(layout))

    def test_it_changes_nothing_for_a_layout_that_does_not_overlap(self):
        b, layout = self._backend("available", overlaps=False)
        b.overlap_never_ask = True
        self.assertEqual(b.overlap_flag(layout), [])

    def test_it_is_off_unless_the_option_is_given(self):
        from warandr import randr
        self.assertFalse(randr.Backend(["wxrandr"], True, env={}).overlap_never_ask)

    def test_the_option_reaches_the_backend(self):
        from warandr import cli
        args = cli._parser().parse_args(["--unsafe-gnome-overlap"])
        self.assertTrue(args.unsafe_gnome_overlap)
        self.assertFalse(cli._parser().parse_args([]).unsafe_gnome_overlap)

    def test_it_records_no_agreement(self):
        # How a program was started must not change what it leaves on disk.
        # warandr does record one, but only from the dialog box the user
        # ticks, so the flag must not reach that path: it waives the question
        # and leaves the answer unwritten.
        import inspect
        from warandr import cli, gui, randr
        self.assertNotIn("allow_overlap", inspect.getsource(cli))
        recorded = [ln.strip() for ln in inspect.getsource(gui).splitlines()
                    if "allow_overlap" in ln]
        self.assertTrue(recorded, "the dialog should still be able to record")
        for ln in recorded:
            self.assertNotIn("never_ask", ln)
        # and the flag itself is nowhere near the recording method
        self.assertNotIn("never_ask", inspect.getsource(randr.Backend.allow_overlap))


# ---------------------------------------------------- the session, not the build

class OnXorg(ConsentCase):
    """U18.  GNOME-on-Xorg owns `org.gnome.Mutter.DisplayConfig` exactly as GNOME-on-Wayland does (measured
    against a live one: `GetCurrentState` there answers with `'renderer': <'xrandr'>`), so wxrandr's detection
    picks the `mutter` backend off the bus name and `--gnome-overlap-status` -- which answers before any
    handover -- ran the extension's own checks and reported `unavailable / shell: 46.0 / reason: the overlap
    extension is not running...` [M recon2/gnome-xorg.md §4 item 7].

    Every word of that is true and the advice is wrong: installing the extension changes nothing, because the
    X server has been placing overlapping monitors all along.  The status has to say which session this is,
    in the same words the flag itself uses on a handed-over X11 run (wxrandr/cli.py's handover branch)."""

    def session_env(self, kind):
        """A session that says what it is the way pam_systemd says it (`XDG_SESSION_TYPE`, step 3 of
        w11common/passthrough.py) -- and with the suite's `W11_PASSTHROUGH=never` lifted, since that
        variable's whole job is to answer this question first (step 1) and leaving it in would make every
        session below `wayland` whatever the type said.

        `WAYLAND_DISPLAY` is dropped for both kinds: step 2 wants a socket that exists, and this box has a
        dozen other agents' sockets in it. So the type is read from the one variable that differs, which is
        what makes the x11 and wayland cases a real pair."""
        from w11common import passthrough
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)
        env = dict(os.environ)
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("SUDO_UID", None)
        env.pop("PKEXEC_UID", None)
        env["W11_PASSTHROUGH"] = ""
        env["XDG_SESSION_TYPE"] = kind
        return mock.patch.dict(os.environ, env, clear=True)

    def x11_env(self):
        return self.session_env("x11")

    def status(self):
        code, out, err = self.run_cli(STATUS)
        self.assertEqual(code, 0, err)
        return [ln for ln in out.splitlines() if ln.strip()]

    def test_the_status_on_an_x11_session_says_the_session_is_x11(self):
        with self.x11_env():
            lines = self.status()
        self.assertEqual(lines[0], "unavailable")
        self.assertIn("reason: this session is x11, which places overlapping monitors "
                      "without it", lines)
        # ...which is the handover branch's own sentence, word for word: the plan asks the status to
        # answer in the words the flag already uses (A 1.7), and wxrandr/cli.py:1807 is where they are.
        self.assertIn(gnome_overlap.X11_REASON,
                      "%s only means anything on GNOME; this session is x11, which places "
                      "overlapping monitors without it\n" % gnome_overlap.FLAG)

    def test_it_does_not_send_the_reader_to_the_extension(self):
        """The extension IS on this mock bus and the shell IS a measured 50.1, so before the fix this session
        answered `available`.  Neither fact is an answer to "can I overlap here?" on X11."""
        with self.x11_env():
            lines = self.status()
        self.assertNotIn("extension: running", lines)
        for ln in lines:
            self.assertNotIn("install-overlap", ln)

    def test_the_shell_version_is_still_reported(self):
        """It is a GNOME session and the version is readable, so the machine-readable half keeps carrying it:
        the reason changed, not what is known."""
        with self.x11_env():
            self.assertIn("shell: 50.1", self.status())

    def test_a_wayland_session_is_untouched(self):
        """The control, and it is built through the same recipe with one variable changed: same bus, same
        extension, same recorded nothing, `W11_PASSTHROUGH` lifted here too. Keeping the suite's
        override in would have had `session_kind()` answer `wayland` without ever reading
        `XDG_SESSION_TYPE`, and an implementation that ignored that variable entirely would have passed
        both halves of this pair."""
        with self.session_env("wayland"):
            lines = self.status()
        self.assertEqual(lines[0], "available")
        self.assertIn("extension: running", lines)

    def test_the_recorded_agreement_is_still_read_back(self):
        """`--gnome-overlap-status` is also how somebody asks what they have agreed to, and that answer is a
        file: it must survive the session being one this cannot be used on."""
        self.record()
        with self.x11_env():
            lines = self.status()
        self.assertEqual(lines[0], "unavailable")
        self.assertIn("agreed on: 2026-01-02T03:04:05Z", lines)


if __name__ == "__main__":
    unittest.main()
