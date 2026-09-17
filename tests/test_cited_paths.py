#!/usr/bin/env python3
"""Every `package/module.py` this tree points at is a file this tree has.

The engines moved out of the command packages and into `hacks/` in one
unreleased wave -- the Hyprland window backend from `wdotool/` to
`hacks/window/`, the property engine from `wxprop/` to `hacks/property/`, and so
on for thirty-one modules -- and the prose did not move with them.  199 citations across 96 files
named a path that no longer existed: the Unreleased CHANGELOG describing its own
wave, the comments that tell the next reader which file carries a constant, the
rig's shell phases, and the refusal `--unsafe-gnome-overlap` prints when it meets
a GNOME it has not been measured on, which sends a maintainer to a file to edit.
Nothing caught it.  `scripts/check-docs.py` compares option names and never the
paths the prose points at; `tests/test_docs_matrix.py`'s `EveryLinkResolves`
resolves markdown link targets and not inline code spans.

So the check is existence, not spelling.  `warandr/model.py` and everything under
`xw11/` are cited by the same shape and are perfectly alive, and a blanket rewrite
of the `w*/` prefix would have broken them; what is asserted here is only that the
file named is a file that is there.

Two things are deliberately out of scope.  A dotted module name
(`wxrandr.core.ROTATIONS`, xw11/randr.py:91) is not matched: the regex wants a
slash, and whether an import path should be renamed is a different question from
whether a citation resolves.  And line numbers riding along with a path
(`hacks/display/core.py:1261`) are not checked, because nothing can check them --
they drift on the next edit above them, which is why the two in xw11/randr.py
were rewritten to name `match_mode` and `resolve_real_mode` instead.
"""

import os
import re
import subprocess
import sys
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hacks.display import gnome_overlap                           # noqa: E402

#: The text files a citation can be written in.  `.json` is here for
#: gnome/w11-overlap@w11/generations.json's header comment field and `.nix` and
#: `.yaml` for the flake checks and the rig's flavour headers, which both carry
#: prose about which module does what.
SUFFIXES = (".py", ".md", ".sh", ".js", ".json", ".nix", ".yml", ".yaml")

#: Dated recordings of what a compositor said on a given day.  They are evidence,
#: not prose: a path inside one is what the tool printed then, and rewriting it
#: would be rewriting the measurement.
SKIP_PREFIXES = ("tests/fixtures/live/",)

#: The packages whose own files get cited.  Anything else with a slash and a
#: suffix -- `src/backends/meta-monitor-config-manager.h`, `debian/rules` -- is
#: somebody else's tree and cannot be checked from here.
CITATION = re.compile(
    r"\b(wdotool|wwmctl|wxprop|wxrandr|warandr|wmirror|xw11|hacks|w11common)"
    r"/[A-Za-z0-9_/]+\.(py|js|sh|c)\b")

#: Everything from the first released stanza down is history: it describes the
#: tree as it stood at 0.3.1, where `wdotool/cnum.py` (still here) and the
#: pre-`hacks/` names were both current.  Only the Unreleased section is a claim
#: about this tree.
CHANGELOG_CUT = "## Version"


#: What a walk of the tree must step over to give `git ls-files`'s answer: the
#: build droppings (dist/, debian/w11/, a nix `result` link), the bytecode a
#: test run leaves behind, and git's own store.
UNTRACKED_DIRS = ("dist", "debian/w11", ".git", "__pycache__")


def tracked_files():
    """`git ls-files`, filtered to the text this check can read.

    The file list comes from git rather than a walk so that dist/, debian/w11
    and the __pycache__ directories a test run leaves behind are never read.
    The nix sandbox check (nix/checks/tools.nix) runs this file from a source
    copy that is not a checkout -- no .git, no git on PATH -- so when git
    cannot answer, the same list is produced by walking the tree and stepping
    over what git would not have tracked.
    """
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                             ).stdout.decode("utf-8")
        rels = [r for r in out.split("\0") if r]
    except (OSError, subprocess.CalledProcessError):
        rels = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            rel_dir = os.path.relpath(dirpath, ROOT)
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = sorted(
                d for d in dirnames
                if os.path.join(rel_dir, d) not in UNTRACKED_DIRS
                and d != "__pycache__" and not d.startswith("result"))
            rels.extend(os.path.join(rel_dir, f) for f in sorted(filenames))
    return [r for r in rels
            if r.endswith(SUFFIXES) and not r.startswith(SKIP_PREFIXES)]


class EveryCitedPathExists(unittest.TestCase):
    """The sweep's own guard: it is what keeps the next rename from doing this
    again quietly."""

    def test_every_cited_module_is_a_file_in_the_tree(self):
        dead = []
        for rel in tracked_files():
            with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
                lines = fh.read().split("\n")
            if rel == "CHANGELOG.md":
                for i, line in enumerate(lines):
                    if line.startswith(CHANGELOG_CUT):
                        lines = lines[:i]
                        break
            for n, line in enumerate(lines, 1):
                for m in CITATION.finditer(line):
                    if not os.path.exists(os.path.join(ROOT, m.group(0))):
                        dead.append("%s:%d cites %s" % (rel, n, m.group(0)))
        self.assertEqual(dead, [], "\n".join([""] + dead))

    def test_the_two_table_files_exist(self):
        """`TABLE_FILES` is the pair of files the unmeasured-GNOME refusal tells a
        maintainer to edit, printed into a terminal where nobody can click it.  The
        second entry carries `  (GENERATIONS)` after the path, which is the name of
        the constant inside it and not part of the path, so the annotation comes off
        before the file is looked for -- and the lookup is anchored at ROOT, because
        a bare `os.path.exists` here would answer differently depending on the
        directory the suite was started from.
        """
        for f in gnome_overlap.TABLE_FILES:
            self.assertTrue(os.path.exists(os.path.join(ROOT, f.split()[0])), f)


if __name__ == "__main__":
    unittest.main()
