#!/usr/bin/env python3
"""Do the documents, the help text and the code agree?

Prose read against prose catches nothing: every contradiction this project has
shipped was between a document and a *fact* -- an option parser, an install
file, an import.  So this reads the options out of the source, reads what each
tool prints when it is run, reads what the documents claim, and reports where
the three disagree.

Everything it reports is meant to be a real disagreement.  A finding that is
deliberate belongs in one of the two tables below, with the reason written
down, and the tables are themselves checked: an entry that has stopped being
true is a finding too, so a table cannot quietly rot into an excuse.

  python3 scripts/check-docs.py            # everything
  python3 scripts/check-docs.py --tool wxrandr
  python3 scripts/check-docs.py --grep overlap    # one feature across everything
  python3 scripts/check-docs.py --list           # every option, and where it is documented

Exit status is 1 when anything is reported.
"""
import argparse
import importlib
import os
import re
import subprocess
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = ["wdotool", "wwmctl", "wxprop", "wxrandr", "warandr", "wmirror", "xw11"]
OPT = re.compile(r"""["'](--[a-z][a-z0-9-]+)["']""")
IN_TEXT = re.compile(r"(--[a-z][a-z0-9-]+)")

#: `--help` is exempt in both directions and never reported.  argparse adds it
#: to `warandr` and `wmirror` without either of them naming it; `wwmctl`
#: special-cases it the way wmctrl does; and `wxprop`, like the real xprop,
#: answers it with `unrecognized argument --help` -- which puts the string in
#: the tool's own output without it being an option at all.
EXEMPT = {"--help"}

#: Options the tool really accepts and deliberately does **not** print, with
#: the reason.  Checked both ways: an entry that has left the source, or that
#: the help now prints after all, is reported.
SILENT = {
    "wdotool": {
        "--version": "xdotool 3.20160805.1 and 4.20260303.1 both accept `--version` "
                     "(and `-v`, and the `version` command) and both leave it out of "
                     "the command list `--help` prints.  wdotool's help is that list "
                     "byte for byte, so printing it would cost the parity "
                     "tests/test_cli_parity.py measures",
    },
    "wwmctl": {
        "--help": "",       # never reached: EXEMPT
        "--version": "wmctrl 1.07 special-cases exactly `wmctrl --version`, and its "
                     "usage text lists neither it nor --help.  Printing them would "
                     "cost the byte parity that is the whole point (docs/WWMCTL.md)",
    },
    # `wxrandr --help` is xrandr 1.5.4's own usage text, byte for byte, so
    # every option of ours is missing from it on purpose.  They are documented
    # in docs/WXRANDR.md, "Command surface".
    # `xw11 --help` prints all six of its own options.  What this table holds
    # is the other thirteen strings `options_in_code` finds under `xw11/`: the
    # word the daemonised child is re-exec'd with, and the twelve clone-only
    # tokens of `xw11/wrap.py:CLONE_ONLY`, which are not options of `xw11` at
    # all -- they are the argv words that make a wrapped tool run OUR code
    # instead of the original, and each is an option of the clone that owns it.
    # Measured 2026-09-11: without these thirteen entries this script reports
    # thirteen disagreements for a tree in which nothing disagrees.
    "xw11": {
        "__serve": "not an option: the argv the daemonised child is re-exec'd "
                   "with, the way `wdotool __daemon` is.  It is deliberately "
                   "absent from --help -- nobody types it",
        "--layout": "wdotool's, read here to recognise it: xw11/wrap.py's "
                    "CLONE_ONLY is the table of argv words that keep the clone",
        "--vkbd": "wdotool's, the same",
        "--true-geometry": "wwmctl's own flag (a flag wmctrl never had, batch 17), read here "
                           "to recognise it: xw11/wrap.py's CLONE_ONLY[\"wmctrl\"] keeps the "
                           "wmctrl clone on our code for it, and it is documented in docs/WWMCTL.md",
        "--backend": "wxrandr's, the same",
        "--backends": "wxrandr's, the same",
        "--print-backend": "wxrandr's, the same",
        "--persistent": "wxrandr's, the same",
        "--gnome-overlap-status": "wxrandr's, the same",
        "--gnome-overlap-allow": "wxrandr's, the same",
        "--gnome-overlap-forget": "wxrandr's, the same",
        "--unsafe-gnome-overlap": "wxrandr's, the same",
        "--unsafe-gnome-overlap-unmeasured": "wxrandr's, the same",
    },
    "wxrandr": {
        "--backend": "ours, not xrandr's",
        "--backends": "ours, not xrandr's",
        "--print-backend": "ours, not xrandr's",
        "--persistent": "ours, not xrandr's",
        "--gnome-overlap-status": "ours, not xrandr's",
        "--gnome-overlap-allow": "ours, not xrandr's",
        "--gnome-overlap-forget": "ours, not xrandr's",
        "--unsafe-gnome-overlap": "ours, not xrandr's",
        "--unsafe-gnome-overlap-unmeasured": "ours, not xrandr's",
        "--q1": "xrandr's own compatibility token, accepted and ignored, and "
                "absent from xrandr's usage text as well",
        "--q12": "the same",
    },
}

#: Long options a package writes into **another program's** command line, or
#: parses out of one.  They are not options of the tool that spells them, so
#: they are not looked for in its help -- but each one is checked against the
#: program it is aimed at, so a rename over there is still caught here.
EMITS = {
    "warandr": ("wxrandr", "warandr drives wxrandr (or the real xrandr) and reads "
                           "arandr's layout scripts, so xrandr's whole vocabulary "
                           "appears in warandr/model.py and warandr/randr.py"),
    "wmirror": ("wl-mirror", "wmirror runs wl-mirror and owns its lifetime "
                             "(wmirror/core.py builds its command line)"),
}

#: wl-mirror's own options, which is the only external program this tree spells
#: options for that is not one of the six.  From its manual page; the shipped
#: ones are exercised live by tests/test_wmirror_cli.py against a stub helper.
EXTERNAL = {
    "wl-mirror": {"--fullscreen", "--fullscreen-output", "--no-show-cursor",
                  "--show-cursor", "--region", "--scaling", "--stream",
                  "--transform", "--verbose", "--version", "--help"},
}


def code_only(path):
    """The file with comments and docstrings removed.

    Without this a getopt token quoted in a comment (`"--anything"`, in
    wwmctl/cli.py, explaining what glibc prints for an unknown long option)
    reads as an option the tool accepts and is documented nowhere.
    """
    try:
        with open(path, "rb") as f:
            toks = list(tokenize.tokenize(f.readline))
    except (OSError, SyntaxError, tokenize.TokenError, UnicodeDecodeError):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    out = []
    for t in toks:
        if t.type == tokenize.COMMENT:
            continue
        if t.type == tokenize.STRING and t.line.lstrip().startswith(('"""', "'''")):
            continue        # a docstring: prose, not a parser
        out.append(t.string)
    return "\n".join(out)


#: The four hand-written parsers are getopt-shaped: a long option is a bare
#: name in a table of `(name, takes_an_argument)` pairs -- `("sync", False)`,
#: `("repeat-delay", True)` -- and the `--` is put back on by the parser, so
#: the option never appears as a `"--..."` literal anywhere in the source.
#: Reading only the literals made 32 real options of `wdotool` invisible to
#: this script.
#: The lookbehind is what keeps `d.get("focused", False)` out of it: an entry
#: in one of these tables is preceded by the `[` that opens the list or by the
#: `,` after the previous entry, never by a function name.
LONGOPT = re.compile(r'(?<=[\[,)])\s*\(\s*"([a-z][a-z0-9-]*)"\s*,\s*(?:True|False)\s*\)')


def options_in_code(tool):
    """Every long option spelled in the package's own source, code only."""
    found = set()
    d = os.path.join(ROOT, tool)
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if name.endswith(".py"):
            src = code_only(os.path.join(d, name))
            found |= set(OPT.findall(src))
            found |= {"--" + n for n in LONGOPT.findall(src)}
    return found


def _run(args, timeout=60):
    env = dict(os.environ, PYTHONPATH=ROOT, W11_PASSTHROUGH="never")
    try:
        p = subprocess.run([sys.executable, "-m"] + args, env=env, cwd=ROOT,
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return p.stdout + p.stderr


def options_in_help(tool):
    """Every long option the tool prints when asked for help -- including the
    help of every subcommand, which for `wdotool` is where nearly all of them
    are.  A tool whose top-level help is one screen of command names is not a
    tool with no options."""
    out = _run([tool, "--help"]) + _run([tool, "-h"])
    if tool == "wdotool":
        # xdotool's own shape: `wdotool --help` lists the 48 commands, and each
        # command has its own usage.  Plus the two of ours that are not
        # xdotool's: `keys` and the hidden `__keymap` diagnostic.
        cmds = [ln.strip() for ln in _run([tool, "--help"]).splitlines()
                if ln.startswith("  ") and ln.strip() and " " not in ln.strip()]
        for c in cmds + ["keys", "keys watch", "keys explain", "__keymap"]:
            out += _run([tool] + c.split() + ["--help"])
    return set(IN_TEXT.findall(out))


def options_from_parser(tool):
    """The exact set an argparse-driven tool accepts, out of the parser itself
    rather than out of a regex.  None for the four hand-written parsers."""
    factory = {"warandr": ("warandr.cli", "_parser"),
               "wmirror": ("wmirror.cli", "parser")}.get(tool)
    if not factory:
        return None
    mod, fn = factory
    sys.path.insert(0, ROOT)
    try:
        p = getattr(importlib.import_module(mod), fn)()
    except Exception:                                   # noqa: BLE001 -- report, never crash
        return None
    finally:
        sys.path.pop(0)
    return {s for a in p._actions for s in a.option_strings if s.startswith("--")}


#: A word-boundary match, not a substring one: `--persistent` is a substring of
#: nothing, but `--backend` is a substring of `--backends` and
#: `--print-backend`, and `--q1` of `--q12`.  With `opt in t` an option nobody
#: had ever documented read as documented because a longer option that shares
#: its spelling was, and every option of this project whose name extends
#: another's was un-checkable.  `-` counts as a word character here: these are
#: option names, and `--backend`/`--backend-` are two different things.
def documented_in(opt, docs):
    """The documents that name `opt` as a whole option name, sorted."""
    pat = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(opt))
    return sorted(n for n, t in docs.items() if pat.search(t))


def documents():
    """Every markdown file in the tree, as relative name -> text."""
    out = {}
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "node_modules", "__pycache__")]
        for n in names:
            if n.endswith(".md"):
                path = os.path.join(base, n)
                with open(path, encoding="utf-8", errors="replace") as f:
                    out[os.path.relpath(path, ROOT)] = f.read()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tool", action="append", choices=TOOLS,
                    help="only this tool (repeatable)")
    ap.add_argument("--grep", metavar="WORD",
                    help="only options and passages containing WORD, for "
                         "checking one feature across everything")
    ap.add_argument("--list", action="store_true",
                    help="also print every option and which documents name it")
    args = ap.parse_args(argv)
    tools = args.tool or TOOLS
    docs = documents()
    problems = []

    # every option any of the six really accepts, for the cross-check below
    accepted_by = {}
    for tool in TOOLS:
        parsed = options_from_parser(tool)
        code = options_in_code(tool)
        # A hand-written parser has no list to ask, so what the tool accepts is
        # what its source spells, plus the deliberate silences.  The help text
        # is NOT part of that: those four help texts are byte parity with the
        # original's, so they print options this project has never implemented
        # -- and folding them in made "accepted" mean "accepted or merely
        # printed", which is the one thing the last loop below exists to tell
        # apart.  A typo in a help string was invisible: it went into accepted,
        # so `helped - accepted` was empty and nothing was reported.
        accepted_by[tool] = (parsed if parsed is not None
                             else code | set(SILENT.get(tool, {})))
    everything_ours = set().union(*accepted_by.values())

    for tool in tools:
        code = options_in_code(tool)
        helped = options_in_help(tool)
        parsed = options_from_parser(tool)
        silent = SILENT.get(tool, {})
        accepted = accepted_by[tool]
        # what this package spells for somebody else's command line
        emitted = (code - accepted) if parsed is not None else set()
        if args.grep:
            keep = lambda s: {o for o in s if args.grep in o}      # noqa: E731
            code, helped, accepted, emitted = (keep(code), keep(helped),
                                               keep(accepted), keep(emitted))

        print("\n== %s: %d options accepted, %d in its help, %d written for "
              "another program" % (tool, len(accepted), len(helped), len(emitted)))

        def report(opt, what):
            problems.append("%s %s: %s" % (tool, opt, what))
            print("  %-34s %s" % (opt, what))

        for opt in sorted(accepted - EXEMPT):
            where = documented_in(opt, docs)
            if not where:
                report(opt, "DOCUMENTED NOWHERE")
            if opt not in helped and opt not in silent:
                report(opt, "accepted but not in any help text, and not in SILENT")
            if args.list and where:
                print("  %-34s in %d documents: %s"
                      % (opt, len(where), ", ".join(sorted(where)[:4])
                         + (" ..." if len(where) > 4 else "")))
        for opt, why in sorted(silent.items()):
            if opt in EXEMPT:
                continue
            if opt not in accepted:
                report(opt, "in SILENT but the tool no longer accepts it")
            elif opt in helped:
                report(opt, "in SILENT but the help prints it now: %s" % why)
        whose, why = EMITS.get(tool, (None, None))
        elsewhere = set()
        if whose:
            elsewhere = (accepted_by.get(whose) or set()) | EXTERNAL.get(whose, set())
        for opt in sorted(helped - accepted - EXEMPT):
            if opt in emitted or opt in elsewhere or opt in everything_ours:
                continue        # the help quotes another program's command line
            report(opt, "printed by the help, not accepted by the parser")
        if emitted:
            print("  written for %s -- %s" % (whose or "?", why or "no EMITS entry"))
            for opt in sorted(emitted):
                if opt not in elsewhere and opt not in everything_ours:
                    report(opt, "written for %s, which has no such option"
                                % (whose or "another program"))

    if args.grep:
        print("\n== every passage mentioning %r" % args.grep)
        for name in sorted(docs):
            hits = [i + 1 for i, ln in enumerate(docs[name].splitlines())
                    if args.grep in ln]
            if hits:
                print("  %-28s %2d lines: %s"
                      % (name, len(hits), ", ".join(map(str, hits[:12]))))

    print("\n%d disagreement(s)" % len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
