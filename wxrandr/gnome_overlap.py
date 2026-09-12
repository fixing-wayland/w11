"""`wxrandr --unsafe-gnome-overlap`: the one route to a GNOME layout in which two
monitors share screen area.

GNOME refuses such a layout, at one door and for no reason the rest of the
compositor needs -- `meta_verify_logical_monitor_config_list()` requires every
logical monitor to share an exact integer edge with another, and nothing else in
Mutter cares (docs/Technical.md section 6).  Every supported route in is closed:
`ApplyMonitorsConfig` validates on every method, and `~/.config/monitors.xml` is
worse than closed, because its reader runs the same validator and one failure
discards the whole file at every boot for ever.

What is left is a GNOME Shell extension that loads a type description of its own
for the symbols libmutter exports but does not introspect, and writes the new
positions into the configuration object before asking Mutter to apply it.  That
extension is `gnome/w11-overlap@w11`; this module is its client
and its gatekeeper.  The dangerous half is 8 bytes per monitor written inside
gnome-shell, and on Wayland a dead compositor is the whole session, so:

* **the flag is off, and its name is the warning.**  Nothing else in the CLI
  begins `--unsafe-`, and no xrandr manual page contains this string;
* **the ordinary layout never reaches any of it.**  The flag changes nothing
  unless the layout being applied is one Mutter's own validator refuses.  A
  request GNOME would take goes down the same DisplayConfig path it always did,
  and the extension is not even asked whether it is there;
* **it refuses on a compositor it does not recognise**, before it asks the
  extension anything, and the extension refuses again on its own account.  That
  one refusal, and no other, can be forced past by somebody who knows their
  machine, with a second flag that names the GNOME in front of them:
  `--unsafe-gnome-overlap-unmeasured 52`.  See "forcing" below for what it
  skips, what it cannot skip and why the argument is not decoration;
* **the warning is printed before the call, in full,** on every invocation until it
  is agreed to -- once, deliberately, and only for the build the checks passed on;
  see "the agreement" at the foot of this file for why that changes what is
  printed and nothing else;
* **it can never write ~/.config/monitors.xml.**  `--persistent` and this flag
  are mutually exclusive here; the extension's type description does not declare
  Mutter's writer at all, and it reports the file's digest from before and after
  its work so the tool can say so rather than assume it.

There is no confirmation prompt on the command line, deliberately.  See
`warning()`; warandr's dialog is the other half, and it too can only ever record
the agreement below.
"""

import json
import os
import re
import textwrap
import time

from w11common.dbus_mini import Bus, DBusError

FLAG = "--unsafe-gnome-overlap"
UUID = "w11-overlap@w11"
BUS_NAME = "org.w11.Overlap"
OBJECT_PATH = "/org/w11/Overlap"
IFACE = "org.w11.Overlap1"
SHELL_NAME = "org.gnome.Shell"
SHELL_PATH = "/org/gnome/Shell"
CALL_TIMEOUT = 30.0

#: THE TABLE.  One record per GNOME generation whose private
#: `MetaMonitorsConfig` layout has been measured (docs/Technical.md section 6),
#: and the only place in this file a version-specific fact lives.
#:
#: It is the same table as
#: `gnome/w11-overlap@w11/generations.json`, which is what the
#: extension itself reads; `tests/test_overlap_force.py` proves the two
#: identical, field for field, and names the file to fix when they are not.
#: Two copies rather than one because the extension is installed into
#: `~/.local/share/gnome-shell/extensions` and wxrandr is installed into a venv
#: or a .deb, and neither can read the other's files at runtime.
#:
#: EVERY NAME IS WRITTEN OUT, none is computed.  Through GNOME 50 libmutter's
#: API version was a counter of its own -- 46 carried libmutter-14, 50 carried
#: libmutter-18 -- and code that spelled `libmutter-%d.so.0 % (major - 32)`
#: worked by accident of that arithmetic.  mutter 51 sets
#: `libmutter_api_version = '51'` (meson.build, line 10 of the 51~rc tarball),
#: so the next Ubuntu ships `libmutter-51.so.0` with `Meta-51.typelib` beside it
#: and the arithmetic is gone.  A generation whose names follow no scheme at all
#: is still exactly one record.
GENERATIONS = (
    {"shell_major": 46,
     "libmutter": "14",
     "soname": "libmutter-14.so.0",
     "meta_typelib": "14",
     "namespace": "W11Overlap14",
     "struct_size": 72,
     "tail_slots": 1,
     "measured_on": "Ubuntu 24.04, GNOME Shell 46.0, mutter 46.0 and 46.2"},
    {"shell_major": 49,
     "libmutter": "17",
     "soname": "libmutter-17.so.0",
     "meta_typelib": "17",
     "namespace": "W11Overlap17",
     "struct_size": 80,
     "tail_slots": 3,
     "measured_on": "Fedora 43, GNOME Shell 49.9, mutter 49.7-1.fc43 "
                    "(libmutter-17.so.0 build c7c9add6935d)"},
    {"shell_major": 50,
     "libmutter": "18",
     "soname": "libmutter-18.so.0",
     "meta_typelib": "18",
     "namespace": "W11Overlap18",
     "struct_size": 80,
     "tail_slots": 3,
     "measured_on": "Ubuntu 26.04, GNOME Shell 50.1, mutter 50.1"},
    {"shell_major": 51,
     "libmutter": "51",
     "soname": "libmutter-51.so.0",
     "meta_typelib": "51",
     "namespace": "W11Overlap51",
     "struct_size": 80,
     "tail_slots": 3,
     "measured_on": "Ubuntu 26.10, GNOME Shell 51.beta, mutter 51~beta-1ubuntu2 "
                    "(libmutter-51.so.0 build e13468162ed2)"},
)

#: what a record must have.  A record missing one of these is not a record.
TABLE_FIELDS = ("shell_major", "libmutter", "soname", "meta_typelib",
                "namespace", "struct_size", "tail_slots")

#: derived, never written down: the allowlist is the table's keys.
SUPPORTED_MAJORS = tuple(g["shell_major"] for g in GENERATIONS)

#: Both routes, because the message has to be right whichever way this was
#: installed: from a clone the extension has to be copied first, and from the
#: .deb its files are already in /usr/share/gnome-shell/extensions and only the
#: enable is missing.  Naming the clone's script alone sent package users to a
#: file they do not have.
#:
#: "if it does not come up" rather than a flat instruction to log out: measured
#: on default 26.04 and 24.04 desktops that had taken the .deb, `gnome-extensions
#: enable` brings it up at once, because gnome-shell scanned the directory at the
#: login the install itself asks for.  It is a fresh clone install, and enabling
#: it in the same session the package was installed in, that still need one.
INSTALL_HINT = (
    "the overlap extension is not running.  From the package:\n"
    "    gnome-extensions enable %s\n"
    "From a clone:\n"
    "    sh gnome/install-overlap.sh\n"
    "Either way, log out and back in once if it does not come up (gnome-shell "
    "reads extension directories only at login)\n" % UUID)

#: where a maintainer puts the answer, and what has to be run afterwards.  It is
#: in the refusal itself because somebody meeting this for the first time is
#: looking at a terminal, not at a document, and the document is one line down.
TABLE_FILES = ("gnome/w11-overlap@w11/generations.json",
               "wxrandr/gnome_overlap.py  (GENERATIONS)")
TABLE_DOC = 'docs/Technical.md section 6, "Adding a GNOME generation"'


# -- what the flag is allowed to do ------------------------------------------

def shell_major(version):
    """The major number of a GNOME Shell version string, or None."""
    try:
        return int(str(version).split(".")[0])
    except (ValueError, AttributeError, IndexError):
        return None


def generation_for(version):
    """The table record for a GNOME Shell version string, or None."""
    major = shell_major(version)
    for g in GENERATIONS:
        if g["shell_major"] == major:
            return g
    return None


#: Cinnamon's Muffin speaks the same DisplayConfig and enforces the same adjacency rule, so a Cinnamon user
#: meets exactly the refusal this flag exists for.  Overlapping monitors are an X feature, so this is a gap
#: of ours: what is missing is the OFFSETS.  The GNOME route reads a private `MetaMonitorsConfig` at offsets
#: chosen from GENERATIONS, and every record there is keyed on a version muffin does not have -- its GIR
#: namespace is `Meta-0` and its library `libmuffin.so.0` for every release ever made, where mutter numbers
#: both (`Meta-14`, `Meta-18`, `Meta-51`) [M recon2/cinnamon.md §2.3].  So the table would be keyed on the
#: Cinnamon release instead, which is a record measured per release rather than per Meta generation.
#:
#: The rung is 2 and not 3: `org.Cinnamon.Eval` reaches into muffin with nothing installed, and AGENTS.md
#: lists Eval under rung 2, so the extension GNOME needs (rung 3) is the alternative and not the route --
#: what it would buy is surviving a Cinnamon restart, which Eval does not.
CINNAMON_REASON = ("this is Cinnamon, whose Meta-0 typelib has no generation to check; not yet here, and "
                   "the route is org.Cinnamon.Eval reaching MetaMonitorsConfig inside muffin with nothing "
                   "installed (AGENTS.md route 2), at the cost of an offset record measured per Cinnamon "
                   "release instead of per Meta generation")

#: An X11 session reaches the same flag with the opposite problem: it does not need it.  The X server places
#: overlapping monitors natively, which is why `--unsafe-gnome-overlap` on a handed-over run is already
#: refused in these words -- but `--gnome-overlap-status` answers before any handover, and on GNOME-on-Xorg
#: (which owns org.gnome.Mutter.DisplayConfig too) it used to point at the extension instead
#: [M recon2/gnome-xorg.md §4 item 7].
#:
#: Byte-for-byte the handover branch's own sentence (wxrandr/cli.py:1807), because that is the whole point:
#: a user who asks the status and then types the flag on the same X11 box is told the same thing twice, in
#: the same words, and does not have to work out whether two sentences are two facts.  The generic branch
#: below keeps the status output's own wording ("without any of this"), where there is no flag named in the
#: line for an "it" to refer back to.
X11_REASON = ("this session is x11, which places overlapping monitors without it")


def not_gnome_reason(backend, session_kind=None):
    """Why the overlap route means nothing here, or None when this is a GNOME Wayland session it could mean
    something on.  `backend` is the wxrandr backend token, `session_kind` the session type when it is known.

    One function for the three callers that have to say it (the flag on a non-mutter backend, the status
    query, and the apply gate) so that a Cinnamon session cannot be told "which places overlapping monitors
    without it" -- Muffin refuses the very layout that sentence promises."""
    if session_kind == "x11":
        return X11_REASON
    if backend == "cinnamon":
        return CINNAMON_REASON
    if backend != "mutter":
        return ("this session is %s, which places overlapping monitors without any of this"
                % backend)
    return None


def describe_table():
    """One line per shipped generation, for the refusal a maintainer reads."""
    return ["GNOME %s -> %s, %s, Meta typelib %s, MetaMonitorsConfig %s bytes"
            % (g["shell_major"], g["soname"], g["namespace"],
               g["meta_typelib"], g["struct_size"])
            for g in GENERATIONS]


def unsupported_reason(version, force=None):
    """Why this compositor is not one to write into, or None if it is one.

    Deliberately a version *allowlist* rather than a blocklist.  The offsets
    this depends on are private, they already differ between the generations
    in the table, and a wrong one does not raise an error -- it writes into
    the compositor's heap.

    `force` is a `--unsafe-gnome-overlap-unmeasured` request, and it is the one
    thing that turns this particular refusal into a yes.  It cannot turn any
    other refusal into a yes: see `FORCEABLE` below.
    """
    if generation_for(version) is not None:
        return None
    if shell_major(version) is None:
        # Not forceable, and this is the reason: forcing is somebody vouching
        # for the build in front of them by naming it, and a version string
        # nothing can read is not a build anybody can name.
        return ("cannot tell which GNOME Shell this is (%r)" % (version,))
    if force is not None and force_reason(force, version) is None:
        return None
    return ("GNOME Shell %s is not a build this has been measured on "
            "(%s are)" % (version, " and ".join(str(m) for m in SUPPORTED_MAJORS)))


# -- forcing -----------------------------------------------------------------
#
# `--unsafe-gnome-overlap-unmeasured <major>`, and it is a second flag rather
# than a value of the first on purpose: `--unsafe-gnome-overlap` stays the only
# thing that turns the feature on at all, and this one only ever *widens* it.
# Typed alone it is a usage error, so there is no arrangement of the CLI in
# which forcing is the thing that enables anything.
#
# THE ARGUMENT IS THE POINT.  It is the GNOME Shell major of the machine in
# front of you, and it is compared with the one that is running, out here and
# again inside gnome-shell.  A command line pasted out of a forum names the
# GNOME that person had; on anybody else's it is refused by number, before a
# byte is read.  That is the property a bare `--force` cannot have.
#
# What it skips: the check that this GNOME is one of the builds in the table
# above, and with it the check that the mapped libmutter is the one that GNOME
# is supposed to carry -- because on a build nobody has measured there is no
# "supposed to".  The description to read through is then chosen by the size
# this build's own GType registry reports for MetaMonitorsConfig, which is a
# selection and not a relaxation: the size still has to be exactly a shipped
# description's, and the sentinel still has to round-trip at that description's
# offsets before anything is written.
#
# What it cannot skip: everything else, and the list is not negotiable because
# each entry refuses for a reason forcing does not answer.  See FORCEABLE.

FORCE_FLAG = "--unsafe-gnome-overlap-unmeasured"

#: The refusals forcing may get past, by the name the check reports.  One entry,
#: and the shape of the rule is what matters: a *cautious* refusal -- "this is a
#: build nobody here has measured" -- is what forcing is for.  A *certain*
#: refusal -- a symbol that is not there, an extension that is not there, a
#: compositor that is not GNOME, a struct nothing describes, a read that does
#: not agree with Mutter -- is not, because there is nothing to force: the code
#: could not run, or it just proved itself wrong.
FORCEABLE = ("shell-version",)


def is_forceable(check):
    """Is a refusal named `check` one that forcing is allowed to get past?"""
    return check in FORCEABLE


def parse_force(value):
    """`--unsafe-gnome-overlap-unmeasured 52` -> `{"shell_major": 52}`.

    Raises ValueError for anything that is not a whole GNOME Shell major.  A
    version string is refused rather than truncated: the flag asks what major
    this is, and somebody who types `51.0` has not read the sentence that told
    them what to type."""
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,3}", text):
        raise ValueError(
            "%s takes the GNOME Shell major version that is running here, as a "
            "whole number (for example: %s 52).  It got %r.\n"
            % (FORCE_FLAG, FORCE_FLAG, value))
    return {"shell_major": int(text)}


def force_reason(force, version):
    """Why this force request does not apply to this session, or None when it
    does.  Fails closed on everything, and the comparison is the whole guard."""
    if not force:
        return "not forced"
    asked = force.get("shell_major")
    # bool is an int in Python and is not a GNOME Shell major in any language
    if not isinstance(asked, int) or isinstance(asked, bool):
        return ("%s was given no whole GNOME Shell major to confirm" % FORCE_FLAG)
    running = shell_major(version)
    if running is None:
        return ("this shell does not report a version this can read (%r), so "
                "there is nothing for %s %s to agree with"
                % (version, FORCE_FLAG, asked))
    if asked != running:
        return ("%s %s names GNOME Shell %s; this session is GNOME Shell %s.  It "
                "has to name the GNOME in front of you, so that a command line "
                "copied from somewhere else is refused here rather than run"
                % (FORCE_FLAG, asked, asked, version))
    return None


def groups(plan):
    """A mutter plan (wxrandr.mutter.MutterOutputs.plan) as the extension's
    connector groups: one entry per logical monitor, mirrored outputs together."""
    out = []
    for p in plan:
        out.append({"connectors": sorted(c for c, _mid, _us in p["members"]),
                    "x": int(p["x"]), "y": int(p["y"])})
    return sorted(out, key=lambda g: (g["x"], g["y"], g["connectors"]))


def rects(plan, dims):
    """(x, y, w, h) per logical monitor, for Mutter's verifier.  `dims` maps a
    connector to its pending logical size."""
    out = []
    for p in plan:
        first = p["members"][0][0]
        if first not in dims:
            return None
        out.append((int(p["x"]), int(p["y"])) + tuple(dims[first]))
    return out


def only_positions_differ(plan, current):
    """True when `plan` differs from the running configuration `current` (a
    `mutter._canon` list) in positions and nothing else.

    The extension writes two 32-bit words per logical monitor and nothing more:
    it cannot change a mode, a scale, a rotation, the primary or which outputs
    are mirrored together.  A request that changes any of those has to go
    through DisplayConfig first, and this is where that is noticed -- before
    anything is written -- instead of the extension quietly applying half of it.
    """
    from wxrandr import mutter as mutter_mod
    strip = lambda canon: sorted(entry[2:] for entry in canon)      # noqa: E731
    return strip(mutter_mod._canon(plan)) == strip(current or [])


def undo_command(current_groups):
    """The `wxrandr` line that puts the layout back, built from the layout that
    is running *now*, before anything is applied: an undo the user has to
    compose afterwards is not an undo.  It goes through DisplayConfig like any
    other invocation -- the way back does not depend on the dangerous half
    still working, or on the extension still being loaded."""
    parts = []
    for g in current_groups:
        parts.append("--output %s --pos %dx%d" % (g["connectors"][0], g["x"], g["y"]))
    return "wxrandr " + " ".join(parts)


# -- the warning -------------------------------------------------------------

RECOVERY = (
    "  If the session dies:  gnome-shell is the session on Wayland, so you land at\n"
    "                        the login screen.  Log in again -- nothing was saved,\n"
    "                        so the layout is the one you started with.\n"
    "                        If a session will not start at all, switch to a text\n"
    "                        console with Ctrl+Alt+F3, log in and run\n"
    "                            gnome-extensions disable %s\n"
    "                        (or delete whichever of these two is there:\n"
    "                            ~/.local/share/gnome-shell/extensions/%s\n"
    "                            /usr/share/gnome-shell/extensions/%s   <- from the .deb ),\n"
    "                        then Ctrl+Alt+F1 back to the login screen.\n" % (UUID, UUID, UUID))


def warning(shell, moves, undo):
    """Exactly what is about to happen, what it risks and how to get back.

    Printed to stderr, in full, before the extension is called, on every
    invocation until `--gnome-overlap-allow` has recorded an agreement for this
    build -- after which it is one line, and the checks are unchanged.

    **No confirmation prompt, on purpose.**  The decision is already made, once,
    by a flag that cannot be typed by accident and appears in no xrandr manual;
    a prompt would only ask the user to make it a second time.  Worse, a prompt
    has to be skippable to keep the tool usable from a script and from warandr,
    and a guard with a documented bypass protects nobody -- while the guards
    that do the work (the version gate here, and the struct-size, sentinel,
    bounded-read and public-view checks inside the extension) run whether or not
    a human answered a question.  GNOME's own answer to "are you sure" is the
    "Keep changes?" dialog with its 20-second revert, and this path cannot have
    that: it does not go through the D-Bus method that arms the timer.  So what
    is offered instead of a question is a printed undo command, and a printed
    way back from a session that will not start.
    """
    lines = ["xrandr: %s: GNOME will not place these monitors, so they are going to be\n"
             "  placed by writing into the running gnome-shell instead of asking it.\n"
             % FLAG]
    for i, m in enumerate(moves or ["nothing (no monitor changes position)"]):
        lines.append("  %s move %s\n"
                     % ("What it does:        " if i == 0 else " " * 21, m))
    lines.append(
        "                        by writing 8 bytes per monitor inside gnome-shell\n"
        "                        (GNOME Shell %s), through the %s\n"
        "                        extension, and then asking Mutter to apply the result.\n"
        % (shell, UUID))
    lines.append(
        "  What it risks:        those 8 bytes go where this build of libmutter keeps a\n"
        "                        logical monitor's position.  The extension re-checks\n"
        "                        that this really is such a build before every write --\n"
        "                        struct size against the GType registry, a sentinel\n"
        "                        through Mutter's own setter, and every value it reads\n"
        "                        against what Mutter reports publicly -- and refuses if\n"
        "                        anything disagrees.  If all of that is wrong anyway,\n"
        "                        gnome-shell crashes, and on Wayland gnome-shell is the\n"
        "                        session: every program running in it goes with it.\n")
    lines.append(
        "  What it saves:        nothing.  ~/.config/monitors.xml is never written on\n"
        "                        this path, so this layout does not survive a logout,\n"
        "                        and --persistent is refused together with this flag.\n")
    lines.append("  To undo:              %s\n" % undo)
    lines.append("                        or log out and back in.\n")
    lines.append(RECOVERY)
    return "".join(lines)



# -- the refusal a maintainer reads ------------------------------------------

def _found_lines(found):
    """What is running, out of the extension's `found` block -- the facts it
    measured before anything could refuse.  All of them are public: a version
    string, file names in /proc/self/maps, GIRepository, and
    `GObject.type_query()` on a type whose name is in a header.  Nothing private
    is read to produce them, which is why they are available on a build this has
    refused to write to."""
    found = found or {}
    out = ["GNOME Shell %s" % (found.get("shell") or "?")]
    for soname in found.get("sonames") or []:
        out.append(str(soname))
    if not found.get("sonames"):
        out.append("no libmutter found in gnome-shell's mappings")
    if found.get("meta_typelib"):
        out.append("Meta typelib %s" % found["meta_typelib"])
    if found.get("instance_size"):
        out.append("MetaMonitorsConfig %s bytes, from this build's GType registry"
                   % found["instance_size"])
    else:
        out.append("MetaMonitorsConfig size unknown (the GType registry does not "
                   "have the type)")
    return out


def unmeasured_refusal(version, found=None, force_hint=True):
    """The whole of what an unmeasured GNOME prints, and the one message in this
    file written for a maintainer rather than for a user.

    The brief it answers: somebody who has never seen this code has to be able
    to add a generation from this message and the document it names.  So it says
    the versions found, the structure size this build reports, what is shipped
    to compare that against, which two files the answer goes in, what to run
    afterwards, and where the procedure is written down.

    `found` is the extension's own measurements when it could be asked (it
    refuses at the same gate, and carries them out with the refusal); None when
    there was no extension to ask, and then the block is one line and says so.
    """
    lines = ["%s: GNOME Shell %s is not a build this has been measured on.\n"
             "  Nothing was read out of gnome-shell and nothing was written.\n\n"
             % (FLAG, version)]

    def block(title, items):
        for i, item in enumerate(items):
            lines.append("  %s %s\n"
                         % (("%-21s" % title) if i == 0 else " " * 21, item))

    if found:
        block("What is running:", _found_lines(found))
    else:
        block("What is running:",
              ["GNOME Shell %s" % version,
               "the rest needs the extension, which is not on the bus here:",
               "sh gnome/install-overlap.sh, log in again, and run this again"])
    block("What is shipped:", (found or {}).get("known") or describe_table())
    block("To add this build:",
          ["one record in each of these two, keyed by the GNOME major, and"]
          + ["nothing else anywhere:"]
          + ["    %s" % f for f in TABLE_FILES]
          + ["then"]
          + ["    python3 gnome/overlap-typelib/gen-gir.py --from-header \\",
             "        <mutter source>/src/backends/meta-monitor-config-manager.h",
             "    python3 gnome/overlap-typelib/gen-gir.py"]
          + ["which derives the offsets from that release's own header and then",
             "writes the description, its typelib and the extension's",
             "metadata.json out of the table.  The procedure, and the three-head",
             "measurement that has to follow it before anything ships, is",
             TABLE_DOC + "."])
    if force_hint:
        block("To try it here now:",
              ["wxrandr %s %s %s <the rest of the line>"
               % (FLAG, FORCE_FLAG, shell_major(version)),
               "which skips this one check and no other, records nothing, and",
               "may end this session.  It prints what it is doing first; read",
               "that before you type it."])
    return "".join(lines)


# -- forcing: what is printed before it is done ------------------------------

def forcing_warning(version, reply_or_forced=None):
    """Printed before the ordinary warning, on every forced invocation, with no
    way to silence it: a forced run never reads the agreement and never records
    one, so there is no "as agreed" line this can collapse into.

    It says the two things a warning has to say -- what may happen, and what to
    do if it does -- and one more that this particular warning owes the reader:
    exactly what is being skipped and exactly what is not, so that "I forced it"
    is not mistaken for "the checks are off"."""
    forced = reply_or_forced or {}
    if isinstance(forced, dict) and forced.get("forced"):
        forced = forced["forced"]
    picked = forced.get("because") if isinstance(forced, dict) else None
    lines = ["xrandr: %s %s: forcing past the one check that says this GNOME has\n"
             "  been measured.  This session may end.\n"
             % (FORCE_FLAG, shell_major(version))]

    def block(title, items):
        for i, item in enumerate(items):
            lines.append("  %s %s\n"
                         % (("%-21s" % title) if i == 0 else " " * 21, item))

    block("What is skipped:",
          ["one thing: that GNOME Shell %s is a build this project has" % version,
           "measured, and with it that the libmutter mapped into gnome-shell is",
           "the one this GNOME is supposed to carry -- on a build nobody has",
           "measured there is no supposed to.  The description to write through",
           "is chosen instead by the size this build's own GType registry",
           "reports for MetaMonitorsConfig."]
          + (textwrap.wrap(picked, 56) if picked else []))
    block("What is not skipped:",
          ["everything else, and none of it can be forced: exactly one libmutter",
           "mapped, the Meta typelib agreeing with it, the struct size equal to",
           "the description's, every symbol callable, the sentinel through",
           "Mutter's own setter, the modal-grab check, the bounded read, the",
           "layout mode, the field-by-field comparison against Mutter's public",
           "view, the read-back of the two words, Mutter's own validator as a",
           "positive control, and the digest of ~/.config/monitors.xml."])
    block("What may happen:",
          ["the two words go where this description believes a logical monitor's",
           "position is.  On a build nobody has measured that belief is the thing",
           "being forced past.  If it is wrong, gnome-shell crashes, and on",
           "Wayland gnome-shell is the session: every program running in it goes",
           "with it, unsaved work included."])
    block("What is recorded:",
          ["nothing.  A forced run neither reads nor writes the agreement, so",
           "this is printed every single time, and %s" % ALLOW_FLAG,
           "refuses while this flag is in use."])
    lines.append(RECOVERY.replace("  If the session dies:  ", "  If it happens:        ", 1))
    return "".join(lines)


def moves_text(before, after):
    """"Virtual-2 from +1920+0 to +960+0" per monitor that actually moves."""
    was = {tuple(g["connectors"]): (g["x"], g["y"]) for g in before}
    out = []
    for g in after:
        key = tuple(g["connectors"])
        old = was.get(key)
        if old is None or old == (g["x"], g["y"]):
            continue
        out.append("%s from +%d+%d to +%d+%d"
                   % ("+".join(key), old[0], old[1], g["x"], g["y"]))
    return out


# -- the client --------------------------------------------------------------

class OverlapError(Exception):
    """A refusal, already worded for the user."""


class Overlap:
    """Client of org.w11.Overlap1.  Every call is a JSON string in and a
    JSON string out; the extension answers with `ok: false` and a named check
    rather than a D-Bus error, so a refusal reads the same wherever it came
    from."""

    def __init__(self, bus: Bus):
        self.bus = bus

    def running(self) -> bool:
        try:
            return self.bus.name_has_owner(BUS_NAME)
        except DBusError:
            return False

    def shell_version(self):
        """The running shell's own version string, from its public property."""
        try:
            (v,) = self.bus.call(SHELL_NAME, SHELL_PATH,
                                 "org.freedesktop.DBus.Properties", "Get", "ss",
                                 (SHELL_NAME, "ShellVersion"))
        except (DBusError, ValueError):
            return None
        return getattr(v, "value", v)

    def _call(self, member, request):
        try:
            (raw,) = self.bus.call(BUS_NAME, OBJECT_PATH, IFACE, member, "s",
                                   (json.dumps(request),), timeout=CALL_TIMEOUT)
        except DBusError as e:
            raise OverlapError("the overlap extension failed: %s\n"
                               % (e.message or e.name))
        try:
            return json.loads(raw)
        except ValueError:
            raise OverlapError("the overlap extension answered something that "
                               "is not JSON\n")

    def probe(self, layout_mode=None, expect=None, force=None):
        return self._call("Probe", _request(layout_mode, expect, force))

    def apply(self, layout_mode, expect, want, force=None):
        req = _request(layout_mode, expect, force)
        req["want"] = want
        return self._call("ApplyOverlap", req)


def _request(layout_mode, expect, force=None):
    req = {}
    if layout_mode:
        req["layout_mode"] = int(layout_mode)
    if expect is not None:
        req["expect"] = expect
    # Never present unless this invocation typed the flag.  The extension
    # refuses on an unmeasured build when the key is absent, so the default is
    # the safe one at both ends and forcing has to be asked for on every call.
    if force:
        req["force"] = dict(force)
    return req


def refusal_text(reply):
    """One line for a reply with `ok: false`."""
    check = reply.get("check") or "?"
    return ("the overlap extension refused (%s): %s\n"
            % (check, reply.get("reason") or "no reason given"))


def refusal_is_forceable(reply):
    """Does this refusal name a check that `--unsafe-gnome-overlap-unmeasured`
    could get past?  The extension answers for itself (`forceable`), and this
    falls back to the same list here for an extension too old to say.

    It decides what is *offered*, never what is done: a forced run still asks
    for the whole thing again and the extension decides again."""
    if "forceable" in reply:
        return bool(reply.get("forceable"))
    return is_forceable(reply.get("check"))


def notes_text(reply):
    """Anything the extension noticed that decides nothing.

    There is exactly one today and it is worth its own line: libmutter replaced
    on disk under a live session, which `apt upgrade` does routinely and which
    nothing in a session can otherwise see.  Notes are printed whether or not an
    agreement is recorded, because unlike the paragraph they are news rather
    than reassurance."""
    return "".join("note: %s\n" % n for n in (reply.get("notes") or []) if n)


def applied_text(reply, quiet=False):
    """What to print after a successful apply: what Mutter's validator said (a
    positive control that the write landed on the field it reads), and the proof
    that the saved configuration file was not touched.

    `quiet` (an agreement covers this build) drops both -- they are reassurance,
    and reassurance is exactly what somebody who has agreed does not need every
    time.  What survives `quiet` is the one line that is not reassurance: a
    saved configuration file that moved when it cannot have."""
    out = []
    forced = reply.get("forced")
    if forced:
        # Never suppressed by `quiet`, because a forced run never has one -- and
        # because "this was applied on a build nobody measured, through a
        # description picked by size" is news on every single invocation.
        out.append("applied on an unmeasured GNOME through %s: %s.  "
                   "Nothing was recorded; the next run asks in full again.\n"
                   % (forced.get("using") or "a shipped description",
                      forced.get("because") or "picked by MetaMonitorsConfig size"))
    verify = reply.get("verify")
    if verify and not quiet:
        out.append("mutter's own validator on the result: %s\n" % verify)
    saved = reply.get("saved_config") or {}
    if saved:
        if saved.get("unchanged"):
            if not quiet:
                out.append("%s: unchanged (%s)\n"
                           % (saved.get("path", "monitors.xml"),
                              saved.get("before", "?")))
        else:
            out.append("%s CHANGED across this call (%s -> %s); that should be "
                       "impossible, please report it\n"
                       % (saved.get("path", "monitors.xml"),
                          saved.get("before"), saved.get("after")))
    return "".join(out)


# -- the agreement -----------------------------------------------------------
#
# The paragraph above is right the first time somebody does this and wrong the
# fiftieth, and a warning nobody reads is not a warning.  So it can be agreed to
# once -- but the agreement is *scoped to the build the checks were run on*,
# because agreeing is agreeing to a measured risk, and a compositor nobody has
# measured is not that risk.
#
# What the agreement does and does not do:
#
# * it silences the paragraph, and nothing else.  Every check in the list still
#   runs on every call, inside gnome-shell, whether or not anything is recorded
#   here: the applying path in wxrandr/mutter.py asks this module only what to
#   *print*, and the reply's `ok` is what decides whether anything is written.
#   There is no code path in which a recorded yes reaches a check.
# * it never turns the feature on.  `--unsafe-gnome-overlap` is still the only
#   thing that does, and it is still typed on every invocation.
#
# It is a file rather than GSettings or dconf: wxrandr must be able to read it
# with no GLib bindings and no schema installed, a user has to be able to see
# what they agreed to, and withdrawing it has to be possible with `rm` from a
# console when the session will not start.

CONSENT_DIR = "w11"
CONSENT_NAME = "overlap-consent.json"
CONSENT_FORMAT = 1
ALLOW_FLAG = "--gnome-overlap-allow"
FORGET_FLAG = "--gnome-overlap-forget"
STATUS_FLAG = "--gnome-overlap-status"


def consent_path(env=None):
    """`$XDG_CONFIG_HOME/w11/overlap-consent.json`, else
    `~/.config/w11/overlap-consent.json`.

    Per user, never per system: it is the user's own session that a wrong offset
    ends, so root's answer must not stand in for anybody else's.  The XDG rule is
    the spec's, relative `XDG_CONFIG_HOME` ignored, the same one
    `monitors_xml.default_path()` follows."""
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or ""
    if not base.startswith("/"):
        base = os.path.join(env.get("HOME", ""), ".config")
    return os.path.join(base, CONSENT_DIR, CONSENT_NAME)


def facts(reply):
    """The three things the checks actually verified, out of an extension reply:
    the GNOME Shell version string, libmutter's generation, and the size this
    build's `MetaMonitorsConfig` was found to be.

    They are what an agreement is recorded against.  The size comes from the
    reply rather than from anything here, because the check that matters read it
    out of the running GType registry -- a number written in this tree could go
    stale against the compositor, which is the whole failure being guarded."""
    size = reply.get("instance_size")
    if size is None:
        # an extension from before the field existed: the same number, from the
        # check that reported it
        for check in reply.get("checks") or []:
            if check.get("name") == "typelib":
                m = re.search(r"MetaMonitorsConfig (\d+) bytes", check.get("detail") or "")
                if m:
                    size = int(m.group(1))
    return {"shell": str(reply.get("shell") or ""),
            "libmutter": reply.get("libmutter"),
            "struct_size": int(size) if size else None,
            # True when the version gate was forced past on this call.  It is
            # here so that `save_consent()` can refuse rather than trust its
            # caller: an agreement is an agreement to a *measured* risk, and a
            # forced run is the definition of the unmeasured one.
            "forced": bool(reply.get("forced")),
            # The GNU build id of the libmutter this session has mapped, as the
            # extension read it out of the file the mapping came from.  It is
            # here because the GNOME Shell version string demonstrably cannot
            # see a library change: Ubuntu 24.04 carries mutter 46.2 under shell
            # 46.0, and 46.0 -> 46.2 under one unchanged shell version was
            # measured applying with every check green.  An agreement that names
            # only the version string therefore outlives the build it was given
            # for.  None on an extension too old to report it, or on a library
            # that would not say -- and then it is simply not compared.
            "libmutter_build": reply.get("libmutter_build") or None}


def describe_build(f):
    """"GNOME Shell 50.1 (libmutter-18 build 0f3a…, MetaMonitorsConfig 80 bytes)".

    The build id is in there because it is the only one of the four that moves
    when a distribution replaces libmutter without touching the shell, which is
    a thing that happens inside a stable release."""
    build = f.get("libmutter_build")
    return ("GNOME Shell %s (libmutter-%s%s, MetaMonitorsConfig %s bytes)"
            % (f.get("shell") or "?", f.get("libmutter") if f.get("libmutter") is not None else "?",
               (" build %s" % str(build)[:12]) if build else "",
               f.get("struct_size") if f.get("struct_size") is not None else "?"))


def load_consent(env=None):
    """The recorded agreement, or None.  Anything unreadable, malformed or in a
    format this build does not know is *not* an agreement: the file only ever
    makes the tool quieter, so failing to read it can only make it louder."""
    path = consent_path(env)
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.loads(fh.read(1 << 16))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or rec.get("format") != CONSENT_FORMAT:
        return None
    if not rec.get("shell"):
        return None
    return rec


def save_consent(f, how, env=None):
    """Write the agreement.  Returns the path.

    `f` is a `facts()` dict that came back from a *successful* probe or apply, so
    nothing can be recorded for a build the six checks have not just passed on;
    `how` is the words for how it was given, kept so that `--gnome-overlap-status`
    can say it back.

    A forced run can never reach here -- `--gnome-overlap-allow` and
    `--unsafe-gnome-overlap-unmeasured` are refused together at parse time, and
    the applying path in wxrandr/mutter.py does not read or write the
    agreement at all when it is forcing -- and this refuses anyway, from the reply rather than from the
    caller's word for it.  A yes that was only ever given on a build nobody
    measured is not a yes anybody can be held to."""
    if f.get("forced"):
        raise ValueError(
            "refusing to record an agreement for a run that used %s: an "
            "agreement names a build the checks passed on, and a forced run is "
            "the one that did not\n" % FORCE_FLAG)
    path = consent_path(env)
    rec = dict(f)
    rec.pop("forced", None)
    rec.update({"format": CONSENT_FORMAT,
                "agreed": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "how": how})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return path


def forget_consent(env=None):
    """Withdraw it.  `(path, removed)` -- `removed` False when there was
    nothing to remove, which is not an error: `--gnome-overlap-forget` on a
    machine that never agreed has done what it was asked."""
    path = consent_path(env)
    try:
        os.unlink(path)
        return path, True
    except OSError:
        return path, False


def consent_covers(rec, shell):
    """Does the recorded agreement cover the GNOME that is running *now*?

    Only the version string can be compared here, and that is deliberate: this
    question has to be answered before anything is read out of the compositor,
    because its answer decides whether the paragraph is printed *before* the
    write.  The other two recorded facts are audited afterwards, against the
    reply, by `consent_drift()`.

    Returns `(True, None)` or `(False, one sentence saying why not)`."""
    if not rec:
        return False, "nothing is recorded"
    if str(rec.get("shell")) != str(shell):
        return False, ("the agreement was given on GNOME Shell %s; this session "
                       "is GNOME Shell %s" % (rec.get("shell"), shell))
    return True, None


def consent_drift(rec, f):
    """The audit after the reply: everything the agreement recorded, against
    everything the checks just measured.

    Reaching this with a difference needs a build that kept its version string
    and changed its private layout -- which the extension's own struct-size check
    turns into a refusal, not a bad write -- so this is a record-keeping check
    rather than a guard.  When it fires the record is withdrawn, and the next run
    asks in full."""
    if not rec:
        return None
    bad = []
    # `libmutter` is compared as text since 0.4: it is the table's label for the
    # generation, and a label is not arithmetic (mutter 51's is "51", which is
    # also its GNOME major).  `str()` on both sides keeps records written when
    # it was a number reading identically.
    for k, word, same in (("libmutter", "libmutter generation",
                           lambda a, b: str(a).strip() == str(b).strip()),
                          ("struct_size", "MetaMonitorsConfig size",
                           lambda a, b: int(a) == int(b))):
        want, got = rec.get(k), f.get(k)
        if want is not None and got is not None and not same(want, got):
            bad.append("%s %s, not %s" % (word, got, want))
    # The one that a routine `apt upgrade` actually moves.  Measured: four
    # libmutter builds on 24.04 and four on 26.04, every one of them a different
    # binary and every one of them the same private layout -- so this is not a
    # danger signal, it is the end of what was agreed to.  The next run asks in
    # full about the build that is there now.
    want, got = rec.get("libmutter_build"), f.get("libmutter_build")
    if want and got and str(want) != str(got):
        bad.append("libmutter build %s, not %s" % (str(got)[:12], str(want)[:12]))
    if not bad:
        return None
    return ("this GNOME is not the one that was agreed to (%s); the agreement "
            "has been withdrawn and the next run will ask again\n" % "; ".join(bad))


def quiet_line(rec, fault):
    """The whole of what an agreed run says.  One line, and the last one: it
    names the rule GNOME would have refused this layout under, because a layout
    no other desktop would blink at is still an unusual thing to be doing, and
    the day it was agreed, because that is where `--gnome-overlap-status` and
    `--gnome-overlap-forget` are found from."""
    return ('%s: applying a layout GNOME refuses ("%s"), as agreed on %s\n'
            % (FLAG, fault, str((rec or {}).get("agreed", "?")).split("T")[0]))


def agreement_text(f):
    """What `--gnome-overlap-allow` prints before it writes anything: the risk
    being agreed to, and the two things the agreement is *not*.

    The checks have already run and passed at this point -- that is where the
    build in the first line comes from -- so this is a description of a measured
    thing rather than a guess about one."""
    return ("".join([
        "Agreeing to %s on %s.\n\n" % (FLAG, describe_build(f)),
        "  What it does:         places monitors GNOME's own validator refuses, by writing\n"
        "                        8 bytes per monitor inside gnome-shell through the\n"
        "                        %s extension, and then asking Mutter\n"
        "                        to apply the result.\n" % UUID,
        "  What it risks:        those 8 bytes go where this build of libmutter keeps a\n"
        "                        logical monitor's position.  If that is ever wrong,\n"
        "                        gnome-shell crashes, and on Wayland gnome-shell is the\n"
        "                        session: every program running in it goes with it.\n",
        "  What it saves:        nothing.  ~/.config/monitors.xml is never written on this\n"
        "                        path, so an overlapping layout is gone at the next login.\n",
        "  What is agreed:       this build and no other.  The agreement records\n"
        "                            %s\n"
        "                        and stops applying the moment any of that changes, which\n"
        "                        is what a distribution upgrade does.\n" % describe_build(f),
        "  What is not agreed:   any of the checking.  Every check above runs again on\n"
        "                        every single apply, agreed or not, and a build this does\n"
        "                        not recognise is refused however old the agreement is.\n",
        "  To withdraw:          wxrandr %s\n" % FORGET_FLAG,
        RECOVERY]))
