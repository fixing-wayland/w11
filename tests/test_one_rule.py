#!/usr/bin/env python3
"""AGENTS.md's one rule, as a gate the suite runs.

The rule is in the tree as prose (AGENTS.md, landed 2d06ae3) and it names four
sentences that must never appear: "Wayland forbids it, so we don't", "Not
possible on Wayland", "By design", "The compositor is right to refuse this".
Prose is not a gate -- batch 16 found two copies of "impossible on Wayland" in
its own step files by grepping for them by hand, batch 14 found a third in
river.sh, and batch 20's sweep of 2026-09-09 found the fourth in a place no
grep of the rig would ever reach: `wdotool windowreparent` printed
"reparenting is not possible on Wayland; ignoring" to a user's stderr.  A
sentence that only a hand-grep catches comes back.

So the three unambiguous phrasings are banned tree-wide here, and the fourth
("by design", which has honest uses about our OWN design and about a library's
API shape) is allow-listed line by line: a new one has to be written into
ALLOWED_BY_DESIGN with its reason, which is the review this rule wants.

The second half is the positive form of the rule.  A refusal is allowed to
name what a protocol lacks -- that is a fact -- but it owes the user the route
that would close it and what that route costs.  The three refusals reworded on
2026-09-09 are pinned here by the thing that makes them right: each carries a
NOT-YET and a rung of AGENTS.md's ladder.
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wdotool import (backend, backend_cosmic, backend_wayfire, backend_wlr,   # noqa: E402
                     daemon, window_cmds)
from wxrandr import gnome_overlap                               # noqa: E402

#: the file suffixes that carry sentences a human reads -- code, shell, the
#: extension's JS, the rig's yaml and nix, and every document
SUFFIXES = (".py", ".sh", ".js", ".md", ".yaml", ".yml", ".nix", ".spec")

#: AGENTS.md and its CLAUDE.md symlink QUOTE the banned sentences, which is the
#: one place they belong; this file quotes them too.
EXEMPT = {"AGENTS.md", "CLAUDE.md", "test_one_rule.py"}

#: The three with no honest use.  Written as regexes because the wrong answer
#: is the claim, not the spelling: "not possible on Wayland" and "impossible on
#: Wayland" are the same sentence, and batch 16 found both spellings.
BANNED = (
    re.compile(r"(?:not +|im)possible on Wayland", re.I),
    re.compile(r"Wayland (?:forbids|does not (?:allow|permit)|disallows)", re.I),
    re.compile(r"the compositor is right", re.I),
)

#: "by design" is the fourth banned sentence and the only one with honest uses:
#: every line below is about OUR design or about a third party's API shape, and
#: none of them is an answer to a feature we do not have.  Stripped text, so a
#: line that only moves does not fail this.
ALLOWED_BY_DESIGN = {
    # our own security posture, in the README's own section about it
    "**What the tools do by design.** `wdotool` injects keystrokes and pointer events as a",
    # our lock's own contract
    "the other's load. The lock is best effort by design: a runtime dir that",
    # our deliberate difference from XWayland's fabricated CVT list, which is not
    # a refusal: wxrandr prints MORE than the oracle here, never less
    "geometry/rotation words, monitor listings — the mode TABLES differ by design:",
    # KWin's own JS API, described
    "return w.captionNormal;                   /* 6.x: no suffix, by design */",
    "| `GetPointer` | `() → (iiu)` | the real pointer and Clutter modifier mask. Diagnostic only "
    "(`GnomeBackend.real_pointer()`, no command uses it): `getmouselocation` reports the daemon-tracked "
    "injected pointer by design, and this is how the two are checked against each other |",
    # wmirror's audience, which is a choice about the tool and not about a protocol
    "sway is the whole audience for this tool (wlroots only, by design), and",
}

BY_DESIGN = re.compile(r"by design", re.I)

#: A `want` pattern in a step file that pins one of these refusals, read out of
#: the file itself.  The three of them are the live oracle: `want` is `grep -Eq`
#: against the tool's own stderr.
STEP_REFUSAL = re.compile(r'"([^"\n]*is not supported by the (?:wlr|cosmic) backend[^"\n]*)"')

#: Lines that RESTATE the rule rather than lean on it.  Stripped text, same
#: shape as ALLOWED_BY_DESIGN.  It held one line until 2026-09-12: README.md's
#: Philosophy section used to quote AGENTS.md's second paragraph, the one place
#: outside AGENTS.md where a reader was shown the wrong answer in order to be
#: told it is wrong.  That paragraph now says a missing Wayland capability is an
#: implementation gap to investigate, which is the same rule with nothing
#: banned left in it, so the set is empty and the check below keeps it honest:
#: an exemption that outlives its line fails the suite.
QUOTING_THE_RULE = set()


def tracked_files():
    """Every file git tracks with one of the suffixes above.  git, not a walk,
    so a scratch copy or a build tree in the working directory cannot fail the
    suite for a sentence nobody is shipping."""
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    except (subprocess.CalledProcessError, FileNotFoundError):
        # not a checkout (the flake's check runs on a copy of the tree with no .git):
        # walk it, leaving out what git would not track
        skip = {".git", "__pycache__", "release", "dist", "result", "build", "live-smoke.out", ".venv"}
        names = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [x for x in dirnames if x not in skip and not x.startswith(".")]
            for f in filenames:
                names.append(os.path.relpath(os.path.join(dirpath, f), ROOT))
    return [n for n in names
            if n.endswith(SUFFIXES) and os.path.basename(n) not in EXEMPT]


class TheBannedSentences(unittest.TestCase):
    """The wrong answers, nowhere in the tree."""

    @classmethod
    def setUpClass(cls):
        cls.files = tracked_files()
        cls.assertTrue(cls.files, "git ls-files found nothing to scan")

    def hits(self, rx):
        found = []
        for name in self.files:
            with open(os.path.join(ROOT, name), encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if rx.search(line) and line.strip() not in QUOTING_THE_RULE:
                        found.append("%s:%d: %s" % (name, n, line.strip()))
        return found

    def test_no_file_says_a_thing_is_not_possible_on_wayland(self):
        """The sentence AGENTS.md exists to keep out.  It was in
        wdotool/window_cmds.py's windowreparent warning until 2026-09-09 --
        user-visible stderr, not a comment -- and no rig grep could see it."""
        self.assertEqual(self.hits(BANNED[0]), [])

    def test_no_file_says_wayland_forbids_or_disallows_it(self):
        """No exemptions left: the README's Philosophy section was reworded on
        2026-09-12 and no longer quotes the sentence.  QUOTING_THE_RULE holds
        any such quote by its bytes, and the second assertion fails when an
        entry outlives the line it exempts, which is how the rewrite was
        caught."""
        self.assertEqual(self.hits(BANNED[1]), [])
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = [ln.strip() for ln in f]
        self.assertEqual(QUOTING_THE_RULE - set(readme), set(),
                         "the exemption outlived the line it exempts")

    def test_no_file_says_the_compositor_is_right_to_refuse(self):
        self.assertEqual(self.hits(BANNED[2]), [])

    def test_every_by_design_is_one_of_the_six_that_earned_it(self):
        """Not a ban: a review gate.  Six lines in the tree say "by design"
        and every one is about our own design or a documented third-party API,
        which is the opposite of citing a compositor's design as our reason.  A
        seventh has to be written down here with its sentence.

        It was seven until 2026-09-12: the bridge's GetPointer comment said the
        daemon-tracked injected pointer was left out "by design"; the live-
        reload rewrite says "deliberately", which is the same fact in a word the
        grep never stops on.

        It was eight until 2026-09-09: wxrandr/core.py's `_container` docstring
        said the state file was "hand-editable by design", which is our own
        design and honest -- and still the one line of the eight that a reader
        grepping the tree for the banned phrase would have to stop and judge.
        It now reads "meant to be hand-edited", which says the same thing and
        needs no allow-list entry."""
        stripped = set()
        for hit in self.hits(BY_DESIGN):
            stripped.add(hit.split(": ", 1)[1])
        new = stripped - ALLOWED_BY_DESIGN
        self.assertEqual(new, set(), "a new 'by design': justify it in ALLOWED_BY_DESIGN or reword it")
        gone = ALLOWED_BY_DESIGN - stripped
        self.assertEqual(gone, set(), "ALLOWED_BY_DESIGN names a line that is no longer in the tree")


class ARefusalCarriesItsRoute(unittest.TestCase):
    """The positive half: naming what a protocol lacks is a fact and is
    allowed; stopping there is not.  Each of these three was a bare lack until
    2026-09-09 (requests-batch-8.md, batch 14's two items; the third found in
    the sweep), and each now ends in a rung of AGENTS.md's ladder."""

    def test_the_wlr_geometry_refusal_names_the_x_plane_and_a_patched_compositor(self):
        """Route 5 for an XWayland window (the X plane really does have a
        ConfigureWindow for it -- vm/live-smoke.d/labwc.sh:128 and river.sh:95
        already say so in their check names), route 6 for a native one."""
        s = backend_wlr.NO_GEOMETRY
        self.assertIn("zwlr_foreign_toplevel_management_v1 carries no geometry", s)
        self.assertIn("AGENTS.md route 5", s)
        self.assertIn("route 6", s)
        self.assertIn("not yet", s)

    def test_the_cosmic_geometry_refusal_names_a_patched_cosmic_comp(self):
        """cosmic-comp has no move request and no protocol above it that does,
        so route 6 is the lowest rung that reaches it."""
        s = backend_cosmic.NO_GEOMETRY
        self.assertIn("the COSMIC toplevel protocol has no move, resize, raise or lower", s)
        self.assertIn("AGENTS.md route 6", s)
        self.assertIn("not yet", s)

    def test_the_step_files_own_want_patterns_still_match_the_refusal(self):
        """The reason the route went at the END of each sentence and not after
        the colon: vm/live-smoke.d/labwc.sh and river.sh pin the protocol
        clause contiguous with `the wlr backend: `, and cosmic.sh pins the
        prefix alone.  `want` is `grep -Eq`, so a substring is the whole test
        -- and a reword that moved the clause would turn three live runs red
        without any unit test noticing.

        The patterns are READ out of the step files, not transcribed here: a
        transcription stays green on the day someone rewords labwc.sh's `want`
        line, which is the exact gap this test exists to close."""
        mods = {"wlr": backend_wlr, "cosmic": backend_cosmic}
        checked = []
        for name in ("labwc.sh", "river.sh", "cosmic.sh"):
            path = os.path.join(ROOT, "vm", "live-smoke.d", name)
            with open(path, encoding="utf-8") as f:
                pats = STEP_REFUSAL.findall(f.read())
            self.assertTrue(pats, "%s no longer pins one of these refusals; move this test with it" % name)
            for pat in pats:
                which = "cosmic" if "cosmic backend" in pat else "wlr"
                op = pat.split(" is not supported by the ", 1)[0]
                line = "%s is not supported by the %s backend: %s" % (op, which, mods[which].NO_GEOMETRY)
                self.assertTrue(re.search(pat, line),
                                "%s pins %r, which no longer matches %r" % (name, pat, line))
                checked.append((name, op))
        # labwc's two, river's one and cosmic's two -- so a step file that quietly lost its check
        # cannot leave this test asserting nothing
        self.assertEqual(len(checked), 5, checked)

    def test_the_sentences_outside_the_five_backends_carry_a_rung_too(self):
        """The ten wordings that lived in files batch 19 did not own, moved
        2026-09-09 (requests-unowned-batch-19.md, plus the four it declined).
        Each is read out of its own module rather than transcribed: a copy
        here would stay green on the day the product's sentence loses its
        rung, which is the whole failure this file exists to catch.

        `backend.WindowBackend`'s four are the ones a backend reaches by NOT
        overriding the method (set_num_desktops on six backends, select_window
        and events on wlr and cosmic, set_desktop_for_window on wlr), so the
        gap they describe is ours and not a compositor's."""
        owed = {
            "daemon.POINTER_UNKNOWN": daemon.POINTER_UNKNOWN,
            "daemon.VPTR_FORCED_ROUTE": daemon.VPTR_FORCED_ROUTE,
            "backend.NOT_YET_NUM_DESKTOPS": backend.WindowBackend.NOT_YET_NUM_DESKTOPS,
            "backend.NOT_YET_SELECT_WINDOW": backend.WindowBackend.NOT_YET_SELECT_WINDOW,
            "backend.NOT_YET_EVENTS": backend.WindowBackend.NOT_YET_EVENTS,
            "backend.NOT_YET_SET_WINDOW_DESKTOP": backend.WindowBackend.NOT_YET_SET_WINDOW_DESKTOP,
            "backend_wlr.NO_MANAGER": backend_wlr.NO_MANAGER,
            "backend_wlr.NO_SEAT": backend_wlr.NO_SEAT,
            "backend_cosmic.NO_SEAT": backend_cosmic.NO_SEAT,
            "gnome_overlap.CINNAMON_REASON": gnome_overlap.CINNAMON_REASON,
        }
        for name, sentence in owed.items():
            with self.subTest(name):
                self.assertIn("AGENTS.md route", sentence, name)
                self.assertIn("not yet", sentence.lower(), name)
        # The eleventh is Wayfire's api gate, and it is the one that must NOT say "not yet": the window half
        # is there and `plugins = ipc ipc-rules` switches it on, so the sentence names the rung it stands on
        # (2, Wayfire's own IPC) and what it costs the user rather than promising work of ours.
        self.assertIn("AGENTS.md route 2", backend_wayfire.NO_IPC_RULES)
        self.assertNotIn("not yet", backend_wayfire.NO_IPC_RULES.lower())
        self.assertIn("wayfire.ini", backend_wayfire.NO_IPC_RULES)

    def test_the_base_defaults_actually_print_the_rung_they_carry(self):
        """The constants above are what a reader greps; this is what a user
        reads.  `_unsupported` grew an optional `why` on 2026-09-09 and the
        four reachable defaults pass one, so the failure this catches is a
        constant that is right and a call site that forgot to hand it over.

        The bare form is still what a backend with no `why` prints, which is
        the fake backend in tests/test_windows_cmds.py and nothing shipped."""
        b = backend.WindowBackend()
        for call, op, owed in (
                (lambda: b.set_num_desktops(2), "set_num_desktops", b.NOT_YET_NUM_DESKTOPS),
                (lambda: b.select_window(), "selectwindow", b.NOT_YET_SELECT_WINDOW),
                (lambda: b.events(), "window events", b.NOT_YET_EVENTS),
                (lambda: b.set_window_desktop(1, 2), "set_desktop_for_window",
                 b.NOT_YET_SET_WINDOW_DESKTOP)):
            with self.subTest(op):
                with self.assertRaises(backend.CmdError) as cm:
                    call()
                self.assertEqual(str(cm.exception),
                                 "%s is not supported by the none backend: %s" % (op, owed))
                self.assertTrue(cm.exception.unsupported,
                                "a capability gap stays downgradable to a warning")
        with self.assertRaises(backend.CmdError) as cm:
            b.lower(1)
        self.assertEqual(str(cm.exception), "windowlower is not supported by the none backend")

    def test_the_heads_the_rest_of_the_tree_greps_survived_the_rewording(self):
        """Every sentence above kept its old opening clause contiguous.  Two of
        them are read from outside the unit suite: tests/test_wire_hardening greps
        `does not offer` out of the wlr constructor's refusal, and
        vm/live-smoke.d/cinnamon-wayland.sh:404 quotes the Cinnamon reason as it
        was measured on a live 6.4.13.  A reword that moved either head instead of
        appending to it would leave those two reading for text that is no longer
        first.

        The other four are kept contiguous for the reader rather than for a
        grepper (checked against vm/live-smoke.d/ on 2026-09-09: cosmic.sh:14
        quotes backend_detect's `no Wayland session found` sentence, not
        NO_MANAGER, and wayfire.sh:81 is that step's own sys.exit, not
        NO_IPC_RULES).  The head is the half that says what happened, and it stays
        the half a user reads first."""
        heads = (
            (backend_wlr.NO_MANAGER,
             "wlr backend: compositor does not offer zwlr_foreign_toplevel_management_unstable_v1"),
            (backend_wlr.NO_SEAT, "compositor offers no wl_seat; cannot activate windows"),
            (backend_cosmic.NO_SEAT, "compositor offers no wl_seat; cannot activate windows"),
            (gnome_overlap.CINNAMON_REASON,
             "this is Cinnamon, whose Meta-0 typelib has no generation to check"),
            (backend_wayfire.NO_IPC_RULES, "wayfire backend: this Wayfire's IPC has no %s"),
            (daemon.POINTER_UNKNOWN, "wdotool does not know where the pointer is:"),
        )
        for sentence, head in heads:
            with self.subTest(head):
                self.assertTrue(sentence.startswith(head), sentence)
        # and the forced-pointer rung is a TAIL, appended to whatever vptr.py raised
        self.assertTrue(daemon.VPTR_FORCED_ROUTE.startswith("; "), daemon.VPTR_FORCED_ROUTE)

    def test_the_wl_seat_refusals_do_not_borrow_the_sandbox_filters_rung(self):
        """Corrected 2026-09-09, in review of the batch above.  Both seat
        refusals offered "run it unsandboxed" as rung 1, on the strength of
        cosmic-comp's `client_not_sandboxed`, and that filter does not reach the
        seat: `SeatState::<Self>::new()` takes no filter [R
        recon2/cosmic/state.rs:694] while the toplevel info, the manager, the
        workspaces and the layer shell each take one [same file, 736-762].  A
        sandboxed COSMIC client therefore keeps its wl_seat and loses the
        manager: it dies at the constructor's precondition, which is a different
        sentence, and never reaches `activate` at all.  The route named a case
        that cannot arise.

        So the two are asserted apart rather than the sandbox rung being banned:
        the refusals that ARE the filter's still name it, and the seat's names
        route 6, because `activate` takes a wl_seat in the request itself and
        nothing below a patched cosmic-comp changes that."""
        seat = backend_cosmic.NO_SEAT
        self.assertIn("AGENTS.md route 6", seat)
        for wrong in ("sandbox", "route 1"):
            self.assertNotIn(wrong, seat, "the seat is not behind cosmic-comp's filter")
        self.assertNotIn("sandbox", backend_wlr.NO_SEAT)
        with open(os.path.join(ROOT, "wdotool", "backend_cosmic.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("unsandboxed run of the protocol", src,
                      "the manager refusals keep the sandbox rung -- this is a split, not a ban")

    def test_the_picker_refusal_does_not_deny_the_geometry_cosmic_sends(self):
        """Corrected 2026-09-09, same review.  The sentence told every backend
        that lands here that "this backend's protocols carry no pointer position
        and no window geometry", and the second half is true of wlr --
        `zwlr_foreign_toplevel_management_v1` has none, which is what its own
        NO_GEOMETRY says -- and false of cosmic: `zcosmic_toplevel_info_v1` has a
        `geometry` event and this tree parses it into `rec.geometry`
        (backend_cosmic._on_cosmic).  What is true on cosmic is a measurement,
        not a lack: the event is sent only alongside an output_enter or a change
        and never arrived in the nested rig.  A refusal that reports a protocol
        lack the protocol does not have is the same defect as one that reports a
        verdict, so it is pinned here."""
        s = backend.WindowBackend.NOT_YET_SELECT_WINDOW
        self.assertIn("no window geometry on wlr", s)
        self.assertIn("on cosmic", s)
        self.assertNotIn("no window geometry to put a click in", s)
        with open(os.path.join(ROOT, "wdotool", "backend_cosmic.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertTrue(re.search(r"_CH_EV_GEOMETRY[\s\S]{0,120}?rec\.geometry\s*=", src),
                        "cosmic parses the geometry event; a sentence saying it has none is wrong")

    def test_windowreparent_warns_with_a_route_instead_of_a_verdict(self):
        """xdotool's windowreparent is one XReparentWindow and it succeeds, so
        the command stays a warn-and-succeed here (docs/WDOTOOL.md's own `warn
        and succeed` column).  What changed is the sentence: the old one told
        the user what Wayland allows, the new one tells them what would make it
        work.  Read out of the module rather than asserted as a literal so the
        claim is the shape and not the wording."""
        with open(os.path.join(ROOT, "wdotool", "window_cmds.py"), encoding="utf-8") as f:
            src = f.read()
        i = src.index("windowreparent: ")
        warning = src[i:src.index('")', i)]
        self.assertIn("AGENTS.md route 5", warning)
        self.assertIn("route 6", warning)
        self.assertIn("ignoring", warning)
        self.assertTrue(hasattr(window_cmds, "cmd_windowreparent"))


if __name__ == "__main__":
    unittest.main()
