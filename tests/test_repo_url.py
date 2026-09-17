#!/usr/bin/env python3
"""One owner for the repository, in the prose as well as in the packaging.

The project moved to `github.com/fixing-wayland/w11` on 2026-09-11 and every
packaging file was moved with it -- `debian/control`'s `Homepage:`,
`debian/copyright`'s `Source:`, `packaging/rpm/w11.spec`'s `URL:`,
`packaging/arch/PKGBUILD`'s `url=` and `nix/package.nix`'s `meta.homepage`.  The
prose was not: README.md kept `github.com/zardus/w11` in the releases link and
in the `git clone` line, and `github:emolabs/w11` in the Nix bullet, where
`emolabs` names a repository this tree points at nowhere else.  Neither is a
dead link a reader recovers from by guessing: `nix run github:emolabs/w11` does
not resolve to a flake, and a clone URL that works only through a rename
redirect stops working the day somebody takes the old name.

Nothing pinned any of those strings, which is why three of them survived the
move, so this file is the pin: every `github.com/<owner>/w11` and
`github:<owner>/w11` spelling in the documents and the packaging has to name the
one owner, and the guard refuses to pass on a tree where the spellings have all
been deleted or reworded -- README.md, debian/control and nix/package.nix each
have to contribute at least one, which is what catches a rewrite that quietly
drops the URL instead of correcting it.

Not scanned: `tests/fixtures/live/`, where a `URL:` row is a byte a guest's
`dpkg -s`/`rpm -qi` printed on a dated run (11 recordings carry the old owner,
one of them twice, for 12 rows in all), and `LICENSE`/`debian/copyright`'s
author line, where `zardus` is Yan's own handle and not a repository at all.
"""

import os
import re
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py (which covers pytest) and
# tests/test_passthrough.py.  This line is what covers `python3 tests/<file>.py`, where conftest is not
# loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The owner the tree moved to.  A rename makes this one line the whole edit.
OWNER = "fixing-wayland"

#: The two spellings a repository is named in: an https URL and a flake
#: reference (`nix run github:<owner>/w11 -- --version`, README.md's Nix
#: bullet).  `\b` after `w11` keeps `w11.git` and `w11/releases` in and a
#: hypothetical `w11-something` out.
SPELLINGS = (re.compile(r"github\.com/([A-Za-z0-9_.-]+)/w11\b"),
             re.compile(r"github:([A-Za-z0-9_.-]+)/w11\b"))

#: The documents and the packaging files that name the repository to a reader or
#: to a package manager.  The three marked below are the ones the guard insists
#: actually carried a spelling when it ran.
NAMED = ("README.md", "docs/Technical.md", "docs/Blogpost.md", "debian/control",
         "debian/copyright", "packaging/rpm/w11.spec", "packaging/arch/PKGBUILD",
         "nix/package.nix")
MUST_CONTRIBUTE = ("README.md", "debian/control", "nix/package.nix")


def owners_in(path):
    """Every owner named in one file, in the order the file names them."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        text = fh.read()
    found = []
    for pattern in SPELLINGS:
        found.extend(pattern.findall(text))
    return found


def markdown_files():
    """The root's `*.md` and `docs/*.md` -- the prose a reader arrives at.

    `vm/README.md` and the packaging READMEs are left to the per-file list
    above; nothing under `tests/fixtures/` is read at all (see the module
    docstring).
    """
    names = [f for f in sorted(os.listdir(ROOT)) if f.endswith(".md")]
    docs = os.path.join(ROOT, "docs")
    names += [os.path.join("docs", f) for f in sorted(os.listdir(docs)) if f.endswith(".md")]
    return names


class TheRepositoryOwner(unittest.TestCase):

    def test_every_named_file_names_the_one_owner(self):
        """The packaging was unanimous before the move; now the prose is too."""
        seen = {}
        for path in NAMED:
            seen[path] = owners_in(path)
        flat = sorted({o for owners in seen.values() for o in owners})
        self.assertEqual(flat, [OWNER],
                         "a file names a repository owner the packaging does not: %r" % (seen,))

    def test_the_guard_is_not_passing_vacuously(self):
        """A file whose URL was deleted rather than corrected passes the test
        above by naming nobody, so the three files that certainly carry one --
        README's install section, `Homepage:` and `meta.homepage` -- have to
        produce a match here."""
        for path in MUST_CONTRIBUTE:
            with self.subTest(path):
                self.assertIn(OWNER, owners_in(path),
                              "%s no longer names github.com/%s/w11" % (path, OWNER))

    def test_no_markdown_anywhere_names_another_owner(self):
        """The per-file list is a list, and a document added later is not on
        it.  This walks the root and `docs/` instead, so the next `*.md` is
        covered the day it lands."""
        files = markdown_files()
        self.assertIn("README.md", files)
        self.assertIn(os.path.join("docs", "Technical.md"), files)
        for path in files:
            with self.subTest(path):
                self.assertEqual(sorted(set(owners_in(path))) or [OWNER], [OWNER])


if __name__ == "__main__":
    unittest.main()
