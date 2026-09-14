#!/usr/bin/env python3
"""Cross-document links, and the rig's flavor images as three descriptions of
one directory.

Two kinds of claim, both of them the sort that decays without anybody noticing.

**Links.** Twenty-one markdown files link to each other and into their own
headings, 164 links in all, and a heading that is reworded takes every anchor
pointing at it with it -- silently, because a bad anchor renders as a link that
scrolls nowhere.  GitHub's slug rules are simple enough to reimplement exactly
(lowercase, backticks dropped, everything that is not a word character,
whitespace or `-` removed, whitespace to `-`, duplicates suffixed `-1`), so
every anchor is resolved against the headings of the file it points into.

**The rig's flavor images.** `vm/flavors/*.yaml` is the fact; `vm/README.md`'s
table and README.md's support matrix are two descriptions of it, and the counts
in the prose are a third.  Every number here is read back out of the directory
and the yaml headers, never asserted as a literal: the flavor list went from 13
to 38 over one wave of work, and a test carrying "thirteen" in its own source
would have had to be rewritten rather than re-run.  Each flavor's yaml carries
`# vmctl-desktop:`, which `vm/vmctl` looks up in its own `DESKTOPS` table, so a
flavor naming a desktop vmctl does not have is an image that cannot be built --
and would be discovered by building it, forty minutes later.  It also carries
`# vmctl-distro:` and `# vmctl-ci:`, which the table's own two rightmost columns
repeat for a reader; those are checked cell by cell (R28).
"""

import os
import re
import sys
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

FLAVORS = os.path.join(ROOT, "vm", "flavors")
VMCTL = os.path.join(ROOT, "vm", "vmctl")
#: One `<flavor>-packages.txt` per flavor whose golden was built and whose
#: package list was checked in.  Plan C1's rule for README's support matrix: a
#: version number may stand in the matrix header only where this file exists,
#: because that file is what the number is read back out of.
REFERENCE = os.path.join(ROOT, "vm", "reference")

#: `](path#anchor)`, `](path)` and `](#anchor)`, which are the three shapes in
#: these documents.  Reference-style links and bare autolinks are not used.
LINK = re.compile(r"\]\(([^)\s#]+)?(?:#([^)\s]+))?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.M)
#: ```-fenced blocks are dropped before HEADING runs: a console block in these
#: documents is full of shell comments, and thirteen `#` lines across the tree
#: read as headings that a rendered page does not offer.  They could only hide
#: a missing anchor, never invent one, but an anchor set that is not GitHub's
#: is not the thing this file claims to check.
FENCE = re.compile(r"^\s*(?:```|~~~)", re.M)

#: The word each `# vmctl-distro:` value opens vm/README.md's `release` column
#: with -- `Ubuntu 26.04 LTS`, `Arch, rolling (20260901)`.  That column is prose
#: for a reader; the `distro` column two to its right is the header verbatim, and
#: both have to agree with the yaml.
DISTRO_WORD = {"ubuntu": "Ubuntu", "fedora": "Fedora", "arch": "Arch",
               "nixos": "NixOS"}
#: The same for `# vmctl-ci:`, whose column is the last one in the table.
CI_WORD = {"push": "push", "on-demand": "on demand"}
#: A version number as these documents write one: a whole token, never the tail
#: of a word.  The lookbehind is what keeps `X11`'s 11 and `i3`'s 3 out.
VERSION = re.compile(r"(?<![\w.])\d+(?:\.\d+)*(?![\w.])")


def slugify(heading):
    """GitHub's anchor for a heading, without the duplicate suffix.

    The rules, in order, from GitHub's own `github-slugger`: strip, lowercase,
    remove everything that is not a word character, whitespace or a hyphen
    (which takes the backticks, the `*`, the `?` and the em dashes out), then
    whitespace to `-`.  Leading hyphens are kept, which is why
    `## \\`--dryrun\\`, and unrecognised builds` becomes
    `--dryrun-and-unrecognised-builds` and not `dryrun-and-unrecognised-builds`.
    """
    s = heading.strip().lower()
    s = s.replace("`", "")
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s+", "-", s)


def unfenced(text):
    """`text` with every fenced code block removed, lines kept in place.

    A fence is toggled by a line whose first non-blank token is ``` or ~~~;
    the closing fence toggles it back.  An unclosed fence swallows the rest of
    the file, which is what a renderer does too."""
    out, inside = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            out.append("")
            continue
        out.append("" if inside else line)
    return "\n".join(out)


def anchors(text):
    """Every anchor a rendered file offers, duplicates numbered as GitHub
    numbers them (`-1`, `-2`, ...)."""
    text = unfenced(text)
    out = set(re.findall(r"<a\s+(?:id|name)=[\"']([^\"']+)[\"']", text))
    seen = {}
    for _hashes, title in HEADING.findall(text):
        base = slugify(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.add(base if n == 0 else "%s-%d" % (base, n))
    return out


def header(text, key, default=None):
    """One `# vmctl-<key>:` value out of a flavor yaml, or `default`."""
    m = re.search(r"^#\s*vmctl-%s:\s*(\S+)" % key, text, re.M)
    return m.group(1) if m else default


class TheSlugRules(unittest.TestCase):
    """The reimplementation, checked against headings whose slugs are known
    because other files link to them."""

    def test_a_heading_that_begins_with_an_option_keeps_its_dashes(self):
        self.assertEqual(slugify("`--dryrun`, and unrecognised builds"),
                         "--dryrun-and-unrecognised-builds")

    def test_backticks_go_and_the_words_stay(self):
        self.assertEqual(slugify("`wxrandr --persistent`"), "wxrandr---persistent")

    def test_punctuation_goes_and_spaces_become_hyphens(self):
        self.assertEqual(slugify("No authorization dialog"), "no-authorization-dialog")
        self.assertEqual(slugify("What's in it? (and what is not)"),
                         "whats-in-it-and-what-is-not")

    def test_a_repeated_heading_gets_a_numbered_anchor(self):
        text = ('<a id="old-title"></a>\n\n## One\n\n## One\n\n## One\n'
                '```html\n<a id="example-only"></a>\n```\n')
        self.assertEqual(anchors(text), {"old-title", "one", "one-1", "one-2"})


class EveryLinkResolves(unittest.TestCase):
    """164 links today, none of them broken."""

    @classmethod
    def setUpClass(cls):
        cls.docs = support.documents()

    def links(self):
        for name, text in self.docs.items():
            base = os.path.dirname(name)
            for m in LINK.finditer(text):
                path, anchor = m.group(1), m.group(2)
                if path and re.match(r"[a-z]+:", path):
                    continue                    # http(s), mailto: not ours to check
                target = name if not path else os.path.normpath(
                    os.path.join(base, path))
                yield name, path, anchor, target

    def test_every_relative_path_names_a_file_that_exists(self):
        missing = [(n, p) for n, p, _a, t in self.links()
                   if p and not os.path.exists(os.path.join(ROOT, t))]
        self.assertEqual(missing, [])

    def test_every_anchor_names_a_heading_that_exists(self):
        """The half that rots: a heading is reworded and the link still
        renders, still looks like a link, and goes nowhere."""
        missing = []
        for name, path, anchor, target in self.links():
            if not anchor or target not in self.docs:
                continue
            if anchor not in anchors(self.docs[target]):
                missing.append("%s -> %s#%s" % (name, path or "(self)", anchor))
        self.assertEqual(missing, [])

    def test_there_are_links_to_check(self):
        """The premise: a regex that stopped matching would make both tests
        above pass over nothing at all."""
        found = list(self.links())
        self.assertGreater(len(found), 100, len(found))
        self.assertGreater(len([1 for _n, _p, a, _t in found if a]), 40)


class TheProxyDocument(unittest.TestCase):
    """docs/XW11.md, the one document AGENTS.md asks for by shape rather than
    by subject: a "What differs" table with a row per thing X does that we do
    not do yet, each naming the route that would close it and what that route
    costs.

    `EveryLinkResolves` above already resolves its anchors against its
    headings, along with every other document's.  What this class adds is the
    claims that are about THIS document: that the tools' own contracts and the
    README point at it (a contract that describes the clone's output without
    saying when the clone runs is a contract that is now wrong half the time),
    that its own table of contents resolves, and that the table has the shape
    the rule asks for rather than merely the heading."""

    DOC = "docs/XW11.md"

    @classmethod
    def setUpClass(cls):
        cls.docs = support.documents()
        cls.text = cls.docs[cls.DOC]

    def what_differs(self):
        """Every row of every table under `## What differs`, as cell lists.
        The section runs to the end of the document bar the `See also`."""
        body = self.text.split("\n## What differs", 1)[1].split("\n## See also", 1)[0]
        rows = []
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("|") or set(line) <= set("|- :"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells and cells[0].lower().startswith("x behaviour"):
                continue                        # a header row
            rows.append(cells)
        return rows

    def test_the_document_exists_and_carries_the_table_the_rule_asks_for(self):
        self.assertIn("\n## What differs", self.text)
        self.assertGreaterEqual(len(self.what_differs()), 30, len(self.what_differs()))

    def test_every_row_of_it_has_the_four_columns_the_rule_names(self):
        """X behaviour | what xw11 does | route | cost.  A three-column row is
        a row that dropped one of them, and the one that gets dropped is the
        cost."""
        for row in self.what_differs():
            with self.subTest(row[0][:60]):
                self.assertEqual(len(row), 4, row)
                for cell in row:
                    self.assertTrue(cell.strip(), row)

    def test_every_not_yet_row_names_a_rung_of_the_ladder(self):
        """The positive half of AGENTS.md's rule, as this document's own gate:
        a row that says a thing is not yet done owes the route number that
        would do it.  A row whose route column is `--` is a CHOICE and not a
        gap, and it says so in its own cost cell -- so the two are told apart
        here by the column rather than by the prose."""
        rungs = {"1", "2", "3", "4", "5", "6"}
        choices, routed = 0, 0
        for row in self.what_differs():
            _x, ours, route, cost = row
            with self.subTest(row[0][:60]):
                if route.startswith("\u2014") or route.startswith("--"):
                    # a choice, not a gap: it owes a cost and it may NOT be a
                    # "not yet", because a not-yet is exactly the thing that
                    # owes a rung
                    choices += 1
                    self.assertTrue(cost.strip(), row)
                    self.assertNotIn("not yet", ours.lower(), row)
                    continue
                routed += 1
                # The rung is the LEADING token of the column and one of six,
                # not any digit anywhere in the cell: `route 10` and a cell
                # carrying a date would both have satisfied `\d`, and neither
                # names a rung of a six-rung ladder.  What follows the first one
                # is free -- `4, or 1` and `2 for five of the six, 3 on GNOME`
                # are both rows in the table.
                lead = re.match(r"(?:routes?|rungs?)?\s*([0-9]+)\b", route)
                self.assertTrue(lead, "route column names no rung: %r" % route)
                self.assertIn(lead.group(1), rungs, "route column names no rung: %r" % route)
                self.assertTrue(cost.strip(), row)
        # the premise, from both sides: a document of nothing but choices would
        # never check a rung, and one with no choice in it would never check
        # the clause above
        self.assertGreaterEqual(routed, 15, routed)
        self.assertGreaterEqual(choices, 5, choices)

    def test_its_own_contents_line_resolves(self):
        """The header carries a hand-written contents line; `EveryLinkResolves`
        checks it with every other link in the tree, and this is the premise --
        that there is one, and that it names the sections a reader comes for."""
        head = self.text.split("\n## ", 1)[0]
        for want in ("#how-it-starts", "#the-request-policy", "#what-differs",
                     "#the-wrapper-rules", "#randr"):
            with self.subTest(want):
                self.assertIn(want, head)
                self.assertIn(want.lstrip("#"), anchors(self.text))

    def test_every_document_that_owes_a_link_to_it_has_one(self):
        """The README lists it in its document table, Technical.md's proxy
        section points at it for the contract, and each of the four wrapped
        tools' contracts points at the wrapper rules -- because since the proxy
        landed, what those four documents describe is not always what runs."""
        for name in ("README.md", "docs/Technical.md", "docs/WDOTOOL.md",
                     "docs/WWMCTL.md", "docs/WXPROP.md", "docs/WXRANDR.md"):
            with self.subTest(name):
                self.assertIn("XW11.md", self.docs[name])
        self.assertIn("| [docs/XW11.md](docs/XW11.md) |", self.docs["README.md"])
        for name in ("docs/WDOTOOL.md", "docs/WWMCTL.md", "docs/WXPROP.md",
                     "docs/WXRANDR.md"):
            with self.subTest(name):
                self.assertIn("XW11.md#the-wrapper-rules", self.docs[name])

    def test_the_four_tool_documents_say_which_tool_runs_on_wayland(self):
        """The sentence that stops each contract being wrong: with the original
        installed, a Wayland session runs the ORIGINAL.  Named per tool, so a
        paragraph copied into three files and left naming the fourth's variable
        is a failure here."""
        for name, var in (("docs/WDOTOOL.md", "WDOTOOL_PROXY"),
                          ("docs/WWMCTL.md", "WWMCTL_PROXY"),
                          ("docs/WXPROP.md", "WXPROP_PROXY"),
                          ("docs/WXRANDR.md", "WXRANDR_PROXY")):
            with self.subTest(name):
                self.assertIn("W11_PROXY=never", self.docs[name])
                self.assertIn(var, self.docs[name])


class TheRigImages(unittest.TestCase):
    """vm/flavors/ is the fact; two documents and one Python table describe
    it.  Nothing below writes a count of its own down."""

    @classmethod
    def setUpClass(cls):
        cls.docs = support.documents()
        cls.names = sorted(n[:-5] for n in os.listdir(FLAVORS)
                           if n.endswith(".yaml"))
        cls.yaml = {}
        for name in cls.names:
            with open(os.path.join(FLAVORS, name + ".yaml"), encoding="utf-8") as fh:
                cls.yaml[name] = fh.read()
        with open(VMCTL, encoding="utf-8") as fh:
            vmctl = fh.read()
        block = vmctl.split("DESKTOPS = {", 1)[1].split("\n}", 1)[0]
        cls.desktops = re.findall(r'^\s*"([a-z0-9-]+)":\s*dict\(', block, re.M)
        cls.builder = {}
        for kind in ("base", "iso", "nix"):
            for name in cls.names:
                if re.search(r"^#\s*vmctl-%s:" % kind, cls.yaml[name], re.M):
                    cls.builder.setdefault(name, []).append(kind)
        cls.of_kind = {k: sorted(n for n in cls.names
                                 if cls.builder.get(n) == [k])
                       for k in ("base", "iso", "nix")}
        cls.distro = {n: header(cls.yaml[n], "distro", "ubuntu") for n in cls.names}
        cls.ci = {n: header(cls.yaml[n], "ci", "push") for n in cls.names}
        # `stonking` is Ubuntu 26.10, a development release: two probe images
        # that stand behind the measured matrix rather than in it.
        cls.probes = sorted(n for n in cls.names if n.startswith("stonking-"))

    def rows(self):
        """The flavor table of vm/README.md, one dict of cells per row.

        The table is recognised by its header line rather than by position:
        it is the only one in the file whose first column is `flavor`."""
        text = self.docs["vm/README.md"]
        # This exact string is also `tests/test_backend_gnome.py`'s anchor for
        # the same table (it pulls `GNOME Shell <major>` out of it), so the two
        # columns R28 asked for are `release` here and `distro` second from the
        # right rather than a rename of column two.
        head = "| flavor | release | desktop (as built) |"
        self.assertIn(head, text, "the flavor table's header has been reworded")
        body = text.split(head, 1)[1].split("\n\n", 1)[0].splitlines()
        out = {}
        for line in body[2:]:                   # [0] is the rest of the header
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            m = re.match(r"\**`([a-z0-9-]+)`\**$", cells[0])
            if m:
                out[m.group(1)] = cells
        return out

    def test_each_one_is_built_by_exactly_one_of_the_three_builders(self):
        """`vmctl build`, `vm/build-iso-golden.sh` and
        `vm/build-nixos-golden.sh` recognise their own flavors by the header,
        so a yaml carrying two, or none, is one no script will pick up."""
        for name in self.names:
            with self.subTest(name):
                self.assertEqual(self.builder.get(name, []) and
                                 len(self.builder[name]), 1,
                                 self.builder.get(name))
        self.assertEqual(sorted(sum(self.of_kind.values(), [])), self.names)
        self.assertEqual(self.of_kind["iso"], ["noble-gnome-iso",
                                               "resolute-gnome-iso"])
        self.assertEqual(self.of_kind["nix"], ["nixos-gnome", "nixos-sway"])

    def test_every_flavor_names_a_desktop_vmctl_knows(self):
        """`vmctl` raises `unknown '# vmctl-desktop: x'` for anything else, and
        the flavor's whole session type, display manager and screenshot
        arrangement come out of that table."""
        for name, text in self.yaml.items():
            with self.subTest(name):
                found = re.findall(r"^#\s*vmctl-desktop:\s*(\S+)", text, re.M)
                self.assertEqual(len(found), 1, found)
                self.assertIn(found[0], self.desktops)

    def test_every_distro_and_ci_value_is_one_the_rig_understands(self):
        """A typo in either header is a flavor CI plans wrongly or a guest
        whose package list is built the wrong shape, and both are silent."""
        for name in self.names:
            with self.subTest(name):
                self.assertIn(self.distro[name], DISTRO_WORD)
                self.assertIn(self.ci[name], CI_WORD)

    def test_vm_readme_has_one_table_row_per_flavor(self):
        rows = self.rows()
        self.assertEqual(sorted(rows), self.names)

    def test_vm_readme_names_no_flavor_that_does_not_exist(self):
        """The other direction: a flavor that was renamed or dropped leaves a
        row nobody can build."""
        text = self.docs["vm/README.md"]
        prefixes = sorted({n.split("-", 1)[0] for n in self.names})
        named = set(re.findall(r"`((?:%s)-[a-z0-9-]+)`" % "|".join(prefixes), text))
        self.assertEqual(sorted(named - set(self.names)), [])

    def test_the_table_repeats_each_flavors_distro_and_ci_header(self):
        """R28's release and distro columns, and the CI class after them.  The
        distro and CI cells are the yaml's own headers written out verbatim and
        the release cell opens with the distro's name, so all three go stale the
        same way: silently, one row at a time."""
        for name, cells in sorted(self.rows().items()):
            with self.subTest(name):
                self.assertTrue(cells[1].startswith(DISTRO_WORD[self.distro[name]]),
                                (cells[1], self.distro[name]))
                self.assertEqual(cells[-2].strip("`"), self.distro[name])
                self.assertEqual(cells[-1], CI_WORD[self.ci[name]])

    def test_the_prose_counts_agree_with_the_directory(self):
        """Seven numbers in vm/README.md's counting paragraph, every one of
        them read back out of vm/flavors/ rather than asserted as a string.
        Adding a flavor moves at least three of the seven, and this is what
        says so."""
        text = self.docs["vm/README.md"]
        m = re.search(r"\*\*(\d+) flavors\*\* across four distributions: "
                      r"(\d+) Ubuntu, (\d+) Arch, (\d+) Fedora and (\d+) NixOS", text)
        self.assertTrue(m, "the counting sentence has been reworded")
        counts = [int(g) for g in m.groups()]
        self.assertEqual(counts[0], len(self.names), self.names)
        for got, key in zip(counts[1:], ("ubuntu", "arch", "fedora", "nixos")):
            with self.subTest(key):
                self.assertEqual(got, sum(1 for n in self.names
                                          if self.distro[n] == key))
        m = re.search(r"(\d+) are a cloud image plus a desktop metapackage, "
                      r"(\d+) are an Ubuntu desktop ISO", text)
        self.assertTrue(m, "the builder sentence has been reworded")
        self.assertEqual(int(m.group(1)), len(self.of_kind["base"]))
        self.assertEqual(int(m.group(2)), len(self.of_kind["iso"]))
        m = re.search(r"(\d+) are NixOS configurations built\s*\n?\s*with `nix build`", text)
        self.assertTrue(m, "the NixOS sentence has been reworded")
        self.assertEqual(int(m.group(1)), len(self.of_kind["nix"]))
        m = re.search(r"CI builds (\d+) of them on every push and (\d+) on\s*\n?\s*demand", text)
        self.assertTrue(m, "the CI sentence has been reworded")
        self.assertEqual(int(m.group(1)),
                         sum(1 for n in self.names if self.ci[n] == "push"))
        self.assertEqual(int(m.group(2)),
                         sum(1 for n in self.names if self.ci[n] == "on-demand"))

    def test_the_desktop_token_count_is_vmctls_own(self):
        """Two sentences in vm/README.md and one in docs/Technical.md count the
        `DESKTOPS` table for a reader ("19 desktop tokens", "one script gets 19
        desktops out of them").  That table is `vm/vmctl`'s, it grew from 5 to 19
        over this wave of work (`5b6a0f3` carried gnome, kde, kde-x11, xfce
        and sway), and there is one `vm/live-smoke.d/<token>.sh` per
        key, so the number is load-bearing prose rather than a flourish.  All
        three said "twenty" until 2026-09-09, which is what a hand-written count
        does."""
        self.assertGreater(len(self.desktops), 10, self.desktops)
        for name, pattern in (("vm/README.md", r"(\d+) desktop tokens"),
                              ("vm/README.md", r"one script gets (\d+) desktops"),
                              ("docs/Technical.md", r"one script gets (\d+) desktops")):
            with self.subTest(name + " " + pattern):
                found = re.findall(pattern, self.docs[name])
                self.assertEqual(len(found), 1, found)
                self.assertEqual(int(found[0]), len(self.desktops))

    def test_the_prose_counts_what_the_support_matrix_rests_on(self):
        """README's *Desktop support* matrix is measured on the Ubuntu
        flavors and on nothing else, because those are the only goldens CI has
        built; the sentence saying so has to move with the directory too."""
        text = self.docs["vm/README.md"]
        m = re.search(r"The (\d+) Ubuntu flavors are what the \*Desktop support\*", text)
        self.assertTrue(m, "the matrix-count sentence has been reworded")
        self.assertEqual(int(m.group(1)),
                         sum(1 for n in self.names if self.distro[n] == "ubuntu"))
        m = re.search(r"The (\d+) Fedora, Arch and\s*\n?\s*NixOS flavors have no golden", text)
        self.assertTrue(m, "the not-built sentence has been reworded")
        self.assertEqual(int(m.group(1)),
                         sum(1 for n in self.names if self.distro[n] != "ubuntu"))
        # The number under the matrix itself: the Ubuntu flavors minus the two
        # ISO installs and the two `stonking-` probes, which both documents say
        # stand behind the table rather than in it.  Both have to write it, and
        # the arithmetic is what disagreed before (23 in one, 25 - 4 in the
        # other).
        under = [n for n in self.names if self.distro[n] == "ubuntu"
                 and n not in self.of_kind["iso"] and n not in self.probes]
        m = re.search(r"That\s*\n?\s*leaves (\d+) images under the matrix", text)
        self.assertTrue(m, "vm/README.md's matrix-image sentence has been reworded")
        self.assertEqual(int(m.group(1)), len(under), sorted(under))
        m = re.search(r"measured rather than assumed, on (\d+) (?:golden|prepared) VM\s*images",
                      self.docs["README.md"])
        self.assertTrue(m, "README.md's matrix-image sentence has been reworded")
        self.assertEqual(int(m.group(1)), len(under), sorted(under))

    def providers(self):
        """Which flavors' table rows write each version number.

        Tokens, not substrings: `1.26` is provided by a row saying `MATE 1.26`
        or `marco 1.26.2` and not by one saying `LXQt 12.3`, which contains it.
        A dotted header token also matches a longer row token it is a
        dot-boundary prefix of, because the header writes versions short --
        `Wayfire 0.10` for the row's `0.10.0`, `Cinnamon 6.4` for `6.4.13`."""
        out = {}
        for name, cells in self.rows().items():
            for tok in VERSION.findall(" | ".join(cells)):
                parts = tok.split(".")
                # `tok` itself, plus every dot-boundary prefix that still
                # carries a dot: `1.26.2` provides `1.26.2` and `1.26` and not
                # `1`, which would let a header say `Xfce 4`.
                for form in [tok] + [".".join(parts[:i])
                                     for i in range(2, len(parts))]:
                    out.setdefault(form, set()).add(name)
        return out

    def test_the_matrix_header_names_only_versions_a_flavor_provides(self):
        """README's support matrix is a summary of the flavor table, so every
        version number in its header has to be one some image actually runs.
        The header writes the pairs short -- `GNOME 46 / 50`, `Xfce 4.18 /
        4.20` -- so whole numeric tokens are compared and not phrases, against
        every cell of the table whose distro and CI columns the test above
        checks against the yamls.  Containment, which this was, does not do it:
        with `assertIn(token, table)` a header saying `sway 1.1` passes on the
        table's `1.11` and a header saying `GNOME 4` passes on anything at all.
        Both went green under the old assertion and red under this one
        (mutation run, 2026-09-09)."""
        head = [ln for ln in self.docs["README.md"].splitlines()
                if ln.startswith("| | GNOME 4")]
        self.assertEqual(len(head), 1, head)
        said = set(VERSION.findall(head[0]))
        self.assertGreater(len(said), 6, sorted(said))
        provided = self.providers()
        for token in sorted(said):
            with self.subTest(token):
                self.assertIn(token, provided,
                              "no flavor row runs %s" % token)

    def test_the_matrix_claims_a_version_only_where_the_package_list_is_in(self):
        """Plan C1's rule, the other half of the one above: a version number in
        the header rests on `vm/reference/<flavor>-packages.txt`, the golden's
        own package list, and not on prose.  MATE 1.26, LXQt 2.3 and labwc
        0.9.3 are measured and have no such file checked in yet, so the header
        names those three desktops without a number and the versions sit in
        footnotes (b) and (m) until the files land."""
        self.assertTrue(os.path.isdir(REFERENCE), REFERENCE)
        have = {n[:-len("-packages.txt")] for n in os.listdir(REFERENCE)
                if n.endswith("-packages.txt")}
        self.assertTrue(have & set(self.names), sorted(have))
        head = [ln for ln in self.docs["README.md"].splitlines()
                if ln.startswith("| | GNOME 4")][0]
        provided = self.providers()
        for token in sorted(set(VERSION.findall(head))):
            with self.subTest(token):
                self.assertTrue(provided.get(token, set()) & have,
                                "%s is claimed by %s, none of which has a "
                                "vm/reference/ package list"
                                % (token, sorted(provided.get(token, ()))))

    def test_the_two_newest_desktops_are_not_claimed_in_the_matrix(self):
        """Plasma 6.7 and GNOME Shell 51 are images that exist and are not what
        the matrix is measured on -- they stand behind it in the "Four more
        images" paragraph, which says so.  Promoting them into the header would
        be claiming the probes' worth of measurement."""
        head = [ln for ln in self.docs["README.md"].splitlines()
                if ln.startswith("| | GNOME 4")][0]
        self.assertNotIn("6.7", head)
        self.assertNotIn("51", head)
        self.assertEqual(len(self.probes), 2, self.probes)
        para = self.docs["README.md"].split("Four more images stand behind the table")[1]
        para = para.split("\n\n")[0]
        self.assertIn("Plasma 6.7", para)
        self.assertIn("GNOME 51", para)


if __name__ == "__main__":
    unittest.main()
