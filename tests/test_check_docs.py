#!/usr/bin/env python3
"""scripts/check-docs.py, run by the suite, and its two blind spots mutated.

The script is the project's guard against a document that disagrees with a
fact, and it was itself unguarded: nobody ran it except by hand, and a check
that stops catching things is indistinguishable from a tree with nothing wrong
in it.  So this file runs it -- `main([]) == 0` over the real tree, which is
the same six seconds a maintainer spends -- and then breaks the tree on purpose
in the two ways it used to miss.

Both were measured by patching the script's own module-level functions and
watching a planted contradiction go unreported:

* `where = [n for n, t in docs.items() if opt in t]` was a substring test, so
  `--backen` counted as documented because `--backend` is written down, and
  `--persisten` because `--persistent` is.  Every option of `wxrandr` whose
  name is a prefix of another was un-checkable.  Fix 52, first half: a
  word-boundary match, with `-` treated as a word character.
* `accepted = code | options_in_help(tool) | SILENT` for the four hand-written
  parsers folded the help text into the set of options the tool accepts, so
  `helped - accepted` was empty by construction and the last loop -- the one
  that reports an option a help text prints and no parser takes -- could never
  fire.  A typo in a usage string was invisible.  Fix 52, second half.

The second half needed one more thing to be true: `options_in_code` could not
see the four getopt-shaped parsers at all, because their long options are bare
names in `(name, takes_arg)` tables and never appear as `"--..."` literals.
Reading those tables put 32 real `wdotool` options back into `accepted`
(`--sync`, `--terminator`, `--repeat-delay`, ...) and `wdotool --version`,
which xdotool accepts and leaves out of its help, into SILENT with the reason.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SCRIPT = os.path.join(ROOT, "scripts", "check-docs.py")


def load():
    """The script as a module.  Its name has a dash in it, so it cannot be
    imported: `spec_from_file_location` is the only way in, and it is also how
    the script would want to be tested -- the tables are module-level data."""
    spec = importlib.util.spec_from_file_location("check_docs", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Harness(unittest.TestCase):
    """One module instance for the file (loading it is cheap; running it is
    not), and one cached copy of the slow call."""

    @classmethod
    def setUpClass(cls):
        cls.cd = load()
        cls.real_help = {t: cls.cd.options_in_help(t) for t in ("wxrandr", "wmirror")}

    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.cd.main(argv)
        return rc, out.getvalue()

    def planted(self, extra, tool="wxrandr"):
        """`extra` added to what the tool's source spells AND to what its help
        prints, for one tool.  Both, so that the only thing the run can report
        about them is where they are documented: an option in `accepted` and
        not in the help text is a second, different report, and it would keep
        the exit status at 1 no matter what the document matcher did."""
        real_code, real_help = self.cd.options_in_code, self.cd.options_in_help

        def fake_code(name):
            got = real_code(name)
            return (got | set(extra)) if name == tool else got

        def fake_help(name):
            if name == tool:
                return set(self.real_help[tool]) | set(extra)
            return real_help(name)
        return (mock.patch.object(self.cd, "options_in_code", fake_code),
                mock.patch.object(self.cd, "options_in_help", fake_help))

    def with_help(self, extra, tool="wxrandr"):
        """`options_in_help` with `extra` added for one tool and untouched for
        the rest -- the mutation, planted in the one place the script reads the
        world through."""
        real = self.cd.options_in_help

        def fake(name):
            if name == tool:
                return set(self.real_help[tool]) | set(extra)
            return real(name)
        return mock.patch.object(self.cd, "options_in_help", fake)


class TheTreeItself(Harness):

    def test_the_whole_tree_is_clean(self):
        """The check a maintainer runs, run here.  It spawns each of the six
        tools for its help text, so it is slow, and it is the reason nobody was
        running it."""
        rc, out = self.run_main([])
        self.assertEqual(rc, 0, out)
        self.assertIn("0 disagreement(s)", out)

    def test_one_tool_alone_is_clean_too(self):
        """The positive control for every mutation below: unpatched, wxrandr
        reports nothing, so a report in one of those tests is the mutation and
        not the tree."""
        rc, out = self.run_main(["--tool", "wxrandr"])
        self.assertEqual(rc, 0, out)

    def test_the_script_is_a_script(self):
        """It is also run as `python3 scripts/check-docs.py` by hand and by
        vm/selftest.sh, so the entry point has to keep working."""
        got = subprocess.run([sys.executable, SCRIPT, "--tool", "wmirror"],
                             cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)


class ASubstringIsNotAnOption(Harness):
    """Fix 52, first half, finding F6.5.

    The matcher under test is `check_docs.documented_in(opt, docs)`, which the
    DOCUMENTED NOWHERE loop calls once per option the tool accepts.  So the
    mutation has to be planted in what the tool *accepts* -- `options_in_code`
    -- and not only in what its help prints: an option the help prints and no
    parser takes never reaches this loop at all, it is reported by the last
    loop instead, and a test that planted it there passed with the substring
    matcher still in place.  Verified by mutation: with line 264's
    `documented_in` call put back to `if opt in t`, both tests below fail and
    every other test in this file still passes.
    """

    PLANTED = ("--persisten", "--backen")

    def test_an_option_that_is_a_prefix_of_a_documented_one_is_reported(self):
        """`--backen` and `--persisten` are documented nowhere; `--backend` and
        `--persistent` are documented in docs/WXRANDR.md.  Under `opt in t` the
        first two read as documented and nothing was reported at all -- which
        made the whole "DOCUMENTED NOWHERE" column blind for exactly the
        options whose names this project extends (`--backend`/`--backends`,
        `--q1`/`--q12`, `--unsafe-gnome-overlap`/`-unmeasured`)."""
        code, help_ = self.planted(self.PLANTED)
        with code, help_:
            rc, out = self.run_main(["--tool", "wxrandr"])
        self.assertEqual(rc, 1, out)
        for opt in self.PLANTED:
            line = [ln for ln in out.splitlines() if ln.split()[:1] == [opt]]
            self.assertTrue(line, "%s not reported at all:\n%s" % (opt, out))
            self.assertEqual(line[0].split(None, 1)[1], "DOCUMENTED NOWHERE",
                             "%s was reported, but not for being undocumented" % opt)

    def test_the_documented_names_they_are_prefixes_of_are_still_found(self):
        """The other side of the boundary, asked of the script's own matcher:
        a word-boundary match that was too strict would report every option in
        the project.  `--backend` is written in the documents surrounded by
        spaces, backticks, commas and end-of-line, and all of those still
        count -- and each of these four is the longer name that made its own
        prefix invisible above."""
        docs = self.cd.documents()
        for opt in ("--backend", "--persistent", "--q1", "--unsafe-gnome-overlap"):
            with self.subTest(opt):
                self.assertTrue(self.cd.documented_in(opt, docs), opt)

    def test_the_matcher_does_not_read_a_longer_option_as_a_shorter_one(self):
        """`documented_in` over a document that names only the longer option.
        This is the substring bug in one line, with no tool and no subprocess
        in the way: `--backends` on the page is not `--backend` documented."""
        docs = {"fake.md": "The only option here is `--backends`, and `--q12`.\n"}
        self.assertEqual(self.cd.documented_in("--backends", docs), ["fake.md"])
        self.assertEqual(self.cd.documented_in("--backend", docs), [])
        self.assertEqual(self.cd.documented_in("--q1", docs), [])


class AnOptionIsDocumentedInItsOwnDocument(Harness):
    """Another program's flag cannot stand in for one of ours.

    `documented_in` searched every .md under ROOT, so `wxrandr --size` -- which wxrandr really accepts
    (wxrandr/cli.py:256, acted on by `_do_1_0`) and no wxrandr document spelled -- read as documented on
    the strength of `vmctl build <flavor> [--size 30G]` in vm/README.md and vm/SETUP.md.  The gate now
    asks only the tool's own documents, `OWN_DOCS[tool]` = docs/<TOOL>.md and the top-level README.md.
    Verified by mutation: with the `tool` argument dropped from the call at the DOCUMENTED NOWHERE loop,
    the first test below fails and every other test in this file still passes.
    """

    PLANTED = "--zzz-planted"

    def with_docs(self, name):
        """`documents()` with the planted option appended to one document's text."""
        real = self.cd.documents()
        self.assertIn(name, real, "the document this test plants in has moved")
        patched = dict(real)
        patched[name] = real[name] + "\n" + self.PLANTED + "\n"
        return mock.patch.object(self.cd, "documents", lambda: patched)

    def report_for(self, name):
        """What the run says about the planted option, with it written into `name` and nowhere else."""
        code, help_ = self.planted([self.PLANTED])
        with code, help_, self.with_docs(name):
            rc, out = self.run_main(["--tool", "wxrandr"])
        line = [ln for ln in out.splitlines() if ln.split()[:1] == [self.PLANTED]]
        return rc, out, line

    def test_another_programs_document_does_not_document_our_option(self):
        """vm/README.md is vmctl's, not wxrandr's: the option is still documented nowhere."""
        rc, out, line = self.report_for("vm/README.md")
        self.assertEqual(rc, 1, out)
        self.assertTrue(line, "%s not reported at all:\n%s" % (self.PLANTED, out))
        self.assertEqual(line[0].split(None, 1)[1], "DOCUMENTED NOWHERE",
                         "%s was reported, but not for being undocumented" % self.PLANTED)

    def test_the_tools_own_document_does(self):
        """The control: the same word in docs/WXRANDR.md and the run is clean, so the report above is
        the ownership rule and not the planting."""
        rc, out, line = self.report_for("docs/WXRANDR.md")
        self.assertEqual(rc, 0, out)
        self.assertEqual(line, [], out)

    def test_the_top_level_readme_counts_for_every_tool(self):
        """README.md is the one document that introduces all seven, so an option documented only there
        is documented.  Asserted as a table entry and then driven through a run."""
        for tool in self.cd.TOOLS:
            with self.subTest(tool):
                self.assertEqual(self.cd.OWN_DOCS[tool],
                                 ("docs/%s.md" % tool.upper(), "README.md"))
        rc, out, line = self.report_for("README.md")
        self.assertEqual(rc, 0, out)
        self.assertEqual(line, [], out)

    def test_the_matcher_without_a_tool_still_reads_the_whole_tree(self):
        """`documented_in(opt, docs)` with no tool is unchanged -- the scoping is one argument at one
        call site, and the SILENT, helped-not-accepted and EMITS loops never consult the documents."""
        docs = {"vm/README.md": "vmctl build <flavor> [--size 30G]\n",
                "docs/WXRANDR.md": "nothing of the sort here\n"}
        self.assertEqual(self.cd.documented_in("--size", docs), ["vm/README.md"])
        self.assertEqual(self.cd.documented_in("--size", docs, "wxrandr"), [])

    def test_the_option_that_found_this_is_documented_now(self):
        """`wxrandr --size` is the one option in the tree the old gate let through, so the tree has to
        answer for it: it is accepted, and docs/WXRANDR.md names it."""
        self.assertIn("--size", self.cd.options_in_code("wxrandr"))
        self.assertEqual(self.cd.documented_in("--size", self.cd.documents(), "wxrandr"),
                         ["docs/WXRANDR.md"])


class HelpIsNotAParser(Harness):
    """Fix 52, second half, finding F7.2."""

    def test_an_option_only_the_help_prints_is_reported(self):
        """A typo in a usage string is the failure this catches: the tool
        prints `--zorp-typo`, somebody copies it out of the help, and the
        parser says "unrecognized option".  With the help text folded into
        `accepted` the script could not tell the two sets apart at all."""
        with self.with_help(["--zorp-typo"]):
            rc, out = self.run_main(["--tool", "wxrandr"])
        self.assertEqual(rc, 1, out)
        line = [ln for ln in out.splitlines() if "--zorp-typo" in ln]
        self.assertTrue(line, out)
        self.assertIn("printed by the help, not accepted by the parser", line[0])

    def test_the_four_hand_written_parsers_are_read_out_of_their_tables(self):
        """The premise the half above needs.  `wdotool`'s long options are
        `("sync", False)`-shaped entries in getopt tables, not `"--sync"`
        literals, so before the tables were read `options_in_code("wdotool")`
        missed 32 options the tool really accepts -- and dropping the help text
        out of `accepted` would have reported every one of them."""
        code = self.cd.options_in_code("wdotool")
        for opt in ("--sync", "--terminator", "--repeat-delay", "--clearmodifiers",
                    "--window", "--args", "--maxdepth"):
            self.assertIn(opt, code)

    def test_a_dict_lookup_is_not_an_option_table(self):
        """The lookbehind in LONGOPT.  `d.get("focused", False)` and
        `mp.get("is-preferred", False)` are the same three tokens as a table
        entry; without it `--focused`, `--decorated`, `--minimized`,
        `--is-preferred` and eight more became options of tools that have never
        heard of them."""
        for tool, ghost in (("wdotool", "--focused"), ("wdotool", "--decorated"),
                            ("wxrandr", "--is-preferred"), ("wxrandr", "--none")):
            with self.subTest(ghost):
                self.assertNotIn(ghost, self.cd.options_in_code(tool))


class TheTablesAreCheckedBothWays(Harness):
    """SILENT is a list of deliberate silences, so an entry that has stopped
    being true is itself a finding: an excuse cannot outlive its reason."""

    def test_an_option_the_tool_no_longer_accepts_is_reported(self):
        silent = dict(self.cd.SILENT)
        silent["wmirror"] = {"--never": "gone from wmirror/cli.py in 0.2"}
        with mock.patch.object(self.cd, "SILENT", silent):
            rc, out = self.run_main(["--tool", "wmirror"])
        self.assertEqual(rc, 1, out)
        self.assertIn("in SILENT but the tool no longer accepts it", out)

    def test_an_option_the_help_prints_after_all_is_reported(self):
        """The other direction: `--keep-layout` really is accepted by
        wmirror's parser and really is printed by its help, so an entry
        claiming it is deliberately unprinted is out of date."""
        silent = dict(self.cd.SILENT)
        silent["wmirror"] = {"--keep-layout": "claimed to be silent"}
        with mock.patch.object(self.cd, "SILENT", silent):
            rc, out = self.run_main(["--tool", "wmirror"])
        self.assertEqual(rc, 1, out)
        self.assertIn("in SILENT but the help prints it now", out)


class CommentsAreNotCode(Harness):
    """`code_only()` strips comments and docstrings before the option regex
    runs, because wwmctl/cli.py quotes `"--anything"` in a comment explaining
    what glibc prints for an unknown long option -- and that read as an option
    wwmctl accepts and nobody documents."""

    def test_an_option_quoted_in_a_comment_is_not_an_option(self):
        self.assertNotIn("--anything", self.cd.options_in_code("wwmctl"))
        with open(os.path.join(ROOT, "wwmctl", "cli.py"), encoding="utf-8") as fh:
            self.assertIn('"--anything"', fh.read(), "the premise has moved")

    def test_an_option_named_only_in_a_docstring_is_not_an_option(self):
        """Written into a scratch copy of a real package directory, because
        `options_in_code` walks a directory: a docstring and a comment naming
        `--not-a-real-option`, and nothing else."""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="w11-codeonly-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pkg = os.path.join(tmp, "wmirror")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "cli.py"), "w", encoding="utf-8") as fh:
            fh.write('"""Prose that names --not-a-real-option."""\n'
                     '# and a comment that names "--also-not-one"\n'
                     'REAL = "--fullscreen"\n')
        with mock.patch.object(self.cd, "ROOT", tmp):
            found = self.cd.options_in_code("wmirror")
        self.assertEqual(found, {"--fullscreen"}, found)


class TheExternalTable(Harness):
    """`EXTERNAL["wl-mirror"]` is wl-mirror's own option list, copied out of its
    manual page, and it is what stops `wmirror` being reported for spelling
    options that are not its own.  A copy of somebody else's interface is the
    kind of table that rots quietly."""

    def test_every_option_wmirror_writes_is_one_wl_mirror_has(self):
        """The check the script already makes, asserted here so that it is a
        test failure and not a line of output: `hacks/mirror/core.py` builds
        wl-mirror's command line, and an option that program dropped would
        make every mirror fail at run time."""
        rc, out = self.run_main(["--tool", "wmirror"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("which has no such option", out)

    @unittest.skipUnless(shutil.which("wl-mirror"), "no wl-mirror on this host")
    def test_the_table_is_a_subset_of_the_real_programs_help(self):
        """When the real thing is installed, the table is checked against it
        rather than against the manual page it was copied from.  Absent on this
        host and on the rig images: this runs where somebody has it."""
        got = subprocess.run([shutil.which("wl-mirror"), "--help"],
                             capture_output=True, text=True, timeout=60)
        real = set(self.cd.IN_TEXT.findall(got.stdout + got.stderr))
        self.assertTrue(real, "wl-mirror --help printed no long options")
        missing = self.cd.EXTERNAL["wl-mirror"] - real
        self.assertEqual(missing, set(), missing)


if __name__ == "__main__":
    unittest.main()
