#!/usr/bin/env python3
"""wxrandr's backend selection: `--backend NAME`, `--print-backend`,
`--backends`.

Everything here is hermetic — the probes are replaced by a table, so no
compositor, bus or socket is touched.  The one thing that cannot be faked
in-process is the handover itself (`execve`), which lives with the rest of
it in tests/test_passthrough_exec.py against the fake install tree.
"""

import contextlib
import io
import os
import shutil
import socket
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# and the tests directory, so `import support` / `import wl_fake` resolve under
# every invocation form (SuiteGuard in tests/test_passthrough.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

from w11common import dbus_mini, distro, passthrough, session
import wl_fake
from support import env as support_env
from test_dbus_mini import MockBus
from wxrandr import cli
from hacks.display import gnome_overlap, mutter as mutter_mod

#: a GNOME session, as the probes would find it
GNOME = {
    "sway": (False, "no sway or i3 IPC socket ($SWAYSOCK)"),
    "hypr": (False, "no Hyprland IPC socket ($HYPRLAND_INSTANCE_SIGNATURE)"),
    "kwin": (False, "the compositor does not advertise "
                    "kde_output_management_v2"),
    "mutter": (True, "org.gnome.Mutter.DisplayConfig on the session bus"),
    "cinnamon": (False, "org.cinnamon.Muffin.DisplayConfig is not on the "
                        "session bus"),
    "wlr": (False, "the compositor does not advertise "
                   "zwlr_output_manager_v1"),
    "x11": (True, "/usr/bin/xrandr"),
}
COMPOSITOR = {"sway": "sway 1.9", "hypr": "Hyprland 0.53.3", "kwin": "KWin",
              "mutter": "Mutter", "cinnamon": "Muffin", "wlr": "wlroots",
              "x11": "X server (RandR)"}
PROTOCOL = {"sway": "sway IPC (i3-ipc)",
            "hypr": "Hyprland IPC (hyprctl)",
            "kwin": "kde_output_management_v2 version 12",
            "mutter": "org.gnome.Mutter.DisplayConfig (D-Bus)",
            "cinnamon": "org.cinnamon.Muffin.DisplayConfig (D-Bus)",
            "wlr": "zwlr_output_manager_v1 version 4"}


class ProbeStub:
    """Stands in for cli.probe_backend and records the order it was asked
    in — which is how "the detection order is unchanged" is checked."""

    def __init__(self, table):
        self.table = dict(table)
        self.calls = []

    def __call__(self, name, env=None, verbose=False):
        self.calls.append(name)
        ok, why = self.table.get(name, (False, "unknown backend"))
        return cli.Probe(name, ok, reason="" if ok else why,
                         detail=why if ok else "",
                         compositor=COMPOSITOR.get(name) if ok else None,
                         protocol=PROTOCOL.get(name) if ok else None)


class Stubbed(unittest.TestCase):
    """A test case whose probes are a table, not a session."""

    TABLE = GNOME

    def setUp(self):
        self.probe = ProbeStub(self.TABLE)
        self.orig = cli.probe_backend
        cli.probe_backend = self.probe
        self.addCleanup(setattr, cli, "probe_backend", self.orig)
        self.saved = os.environ.get("WXRANDR_BACKEND")
        os.environ.pop("WXRANDR_BACKEND", None)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        if self.saved is None:
            os.environ.pop("WXRANDR_BACKEND", None)
        else:
            os.environ["WXRANDR_BACKEND"] = self.saved

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
        return code, out.getvalue(), err.getvalue()


class Names(unittest.TestCase):
    def test_canonical_and_aliases(self):
        for spelling, want in (("auto", "auto"), ("x11", "x11"),
                               ("sway", "sway"), ("WLR", "wlr"),
                               (" mutter ", "mutter"), ("gnome", "mutter"),
                               ("GNOME", "mutter"), ("kde", "kwin"),
                               ("kwin", "kwin"),
                               # the two the second design adds, and the name
                               # each desktop's own users would type first
                               ("hypr", "hypr"), ("hyprland", "hypr"),
                               ("HYPRLAND", "hypr"), (" cinnamon ", "cinnamon"),
                               ("muffin", "cinnamon"), ("Muffin", "cinnamon")):
            self.assertEqual(cli.canonical_backend(spelling), want, spelling)
        for bad in ("", None, "wayland", "xrandr", "wlroots", "sway2",
                    "wayfire", "cosmic"):
            self.assertIsNone(cli.canonical_backend(bad), bad)

    def test_the_wayland_names_are_the_display_side_ones_only(self):
        """wdotool's `--backend` set and this one are not the same list and must not be made to look like it:
        Wayfire's IPC publishes no modes and no physical size, so it gets a window backend and no display one
        [M recon2/wayfire.md §3.4], and COSMIC's displays go over plain wlr-output-management, which the
        `wlr` token already names [M recon2/cosmic.md §3].

        Read off wdotool's own table rather than a second copy of this one, so that adding a backend on one
        side and forgetting the other is what fails here."""
        from hacks.window import backend_detect
        # wdotool spells Mutter's backend `gnome` and KWin's `kwin`; both are names this side accepts too, so
        # the comparison is on canonical tokens and the two that stay unknown here are the finding.
        window_side = {cli.canonical_backend(n) or n
                       for n in set(backend_detect._MAKERS) - {"i3"}}   # i3 is an alias of sway
        display_side = set(cli.WAYLAND_BACKENDS)
        self.assertEqual(sorted(window_side - display_side), ["cosmic", "wayfire"])
        self.assertEqual(sorted(display_side - window_side), [])
        for name in display_side:
            self.assertEqual(cli.canonical_backend(name), name, name)
            self.assertNotEqual(cli.probe_backend(name, {}).reason, "unknown backend", name)
        # the three tables are three views of one list: the flag's, the detection order's, and what is left
        self.assertEqual(cli.BACKEND_NAMES, ("auto", "x11") + cli.WAYLAND_BACKENDS)
        self.assertEqual(sorted(cli.AUTO_ORDER) + [cli.AUTO_FALLBACK],
                         sorted(display_side - {cli.AUTO_FALLBACK}) + [cli.AUTO_FALLBACK])
        self.assertNotIn(cli.AUTO_FALLBACK, cli.AUTO_ORDER)   # it is what is left, never probed for it

    def test_the_options_are_not_in_xrandrs_usage(self):
        """A byte-parity clone may only add options the real one has none
        of, and must not mention them where it prints the original's text."""
        for opt in ("--backend", "--print-backend", "--backends"):
            self.assertNotIn(opt, cli.USAGE, opt)


class Lookahead(unittest.TestCase):
    """The argv walk that happens before any parsing, so that main() knows
    whether an X11 session hands over."""

    def scan(self, *argv):
        return cli.scan_backend_argv(list(argv))

    def test_both_spellings(self):
        self.assertEqual(self.scan("--backend", "sway", "--query"),
                         ("sway", False, ["--query"]))
        self.assertEqual(self.scan("--backend=x11", "--query"),
                         ("x11", False, ["--query"]))
        self.assertEqual(self.scan("--query"), (None, False, ["--query"]))

    def test_an_output_named_like_the_flag_is_a_value(self):
        """`--output --backend` names an output `--backend`; the scan must
        read it as parse() does, or it would suppress a handover nobody
        asked to suppress."""
        for argv in (["--output", "--backend", "--off"],
                     ["--output", "--backend=x11", "--auto"],
                     ["--output", "DP-1", "--mode", "--backend"],
                     ["--set", "--backend", "sway"],
                     ["--addmode", "--backend", "sway"],
                     ["--setmonitor", "--backend", "sway", "none"],
                     ["--newmode", "--backend", "1", "2", "3", "4", "5",
                      "6", "7", "8", "9"],
                     ["-d", "--backend"],
                     ["--display", "--backends"]):
            self.assertEqual(cli.scan_backend_argv(list(argv)),
                             (None, False, list(argv)), argv)

    def test_after_a_stanza_it_is_still_the_flag(self):
        self.assertEqual(
            self.scan("--output", "DP-1", "--off", "--backend", "mutter"),
            ("mutter", False, ["--output", "DP-1", "--off"]))
        self.assertEqual(
            self.scan("--newmode", "m", "1", "2", "3", "4", "5", "6", "7",
                      "8", "9", "+hsync", "--backend", "kwin")[0], "kwin")

    def test_informational_options(self):
        self.assertEqual(self.scan("--print-backend"),
                         (None, True, ["--print-backend"]))
        self.assertEqual(self.scan("--backends"), (None, True, ["--backends"]))
        self.assertEqual(self.scan("--backend", "kde", "--print-backend"),
                         ("kde", True, ["--print-backend"]))

    def test_last_one_wins_like_parse(self):
        self.assertEqual(self.scan("--backend", "sway", "--backend=kwin")[0],
                         "kwin")

    def test_a_flag_with_no_value_is_still_the_flag(self):
        """It names nothing, but it *is* there.  Lose that and an X11
        session hands `--backend` to the original, and the user gets its
        `unrecognized option` where ours says what is missing."""
        self.assertEqual(self.scan("--backend"), ("", False, []))
        self.assertEqual(self.scan("--query", "--backend"),
                         ("", False, ["--query"]))
        self.assertIsNone(cli.canonical_backend(""))

    def test_the_walk_agrees_with_the_parser(self):
        """The look-ahead's whole job is to read argv exactly as parse()
        will, one step earlier -- for the flag, for the two informational
        options, and for the argv left over once the flag is removed."""
        cases = [
            ["--query"],
            ["--backend", "sway", "--query"],
            ["--backend=kde", "--print-backend"],
            ["--backends", "--verbose"],
            ["-d", ":0", "--backend", "wlr", "--dryrun"],
            ["--fb", "1920x1080", "--backend=auto", "--dpi", "96"],
            ["--output", "--backend", "--off"],
            ["--output", "DP-1", "--mode", "--backend", "--pos", "0x0"],
            ["--output", "DP-1", "--set", "--backend", "--print-backend"],
            ["--output", "DP-1", "--gamma", "1:1:1", "--backend", "mutter"],
            ["--addmode", "--backend", "--backends"],
            ["--rmmode", "--print-backend"],
            ["--newmode", "--backend", "1", "2", "3", "4", "5", "6", "7",
             "8", "9", "+hsync", "--backend", "kwin"],
            ["--setmonitor", "--backend", "x", "--backends", "--query"],
            ["--output", "DP-1", "--auto", "--backend", "sway",
             "--output", "HDMI-1", "--off"],
        ]
        for argv in cases:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):     # "not supported" warns
                opts = cli.parse(list(argv))
                flag, info, rest = cli.scan_backend_argv(list(argv))
                stripped = cli.parse(list(rest))
            self.assertEqual(cli.canonical_backend(flag), opts.backend, argv)
            self.assertEqual(info, opts.print_backend or opts.list_backends,
                             argv)
            self.assertIsNone(stripped.backend, argv)
            self.assertEqual([s.name for s in stripped.stanzas],
                             [s.name for s in opts.stanzas], argv)
            self.assertEqual(stripped.mode_ops, opts.mode_ops, argv)
            self.assertEqual(stripped.monitor_op, opts.monitor_op, argv)

    def test_an_unparseable_argv_does_not_raise(self):
        for argv in (["--backend"], ["--output"], ["--newmode", "m"],
                     ["--set"], ["--zorp", "--backend", "sway"]):
            cli.scan_backend_argv(list(argv))     # must simply not raise
        self.assertEqual(self.scan("--zorp", "--backend", "sway")[0], "sway")

    def test_our_own_apply_options_are_stripped_too(self):
        """The 0.4 retest, on both X11 images: `wxrandr --persistent --output
        Virtual-2 --below Virtual-1` came back as the real xrandr's
        `unrecognized option '--persistent'`, exit 1, layout unchanged, where
        the documents say an X11 apply works and saves nothing."""
        self.assertEqual(self.scan("--persistent", "--output", "DP-1", "--auto"),
                         (None, False, ["--output", "DP-1", "--auto"]))
        self.assertEqual(self.scan("--unsafe-gnome-overlap", "--output", "DP-1",
                                   "--pos", "960x0"),
                         (None, False, ["--output", "DP-1", "--pos", "960x0"]))
        self.assertEqual(self.scan("--backend", "x11", "--persistent",
                                   "--output", "DP-1", "--off"),
                         ("x11", False, ["--output", "DP-1", "--off"]))

    def test_which_of_our_options_this_argv_really_carries(self):
        self.assertEqual(cli.own_flags_in(["--persistent"]), {"--persistent"})
        self.assertEqual(cli.own_flags_in(["--query"]), set())
        # ...and an output *named* like one of them is a value, not a flag
        for argv in (["--output", "--persistent", "--off"],
                     ["--output", "--unsafe-gnome-overlap", "--auto"],
                     ["--mode", "--persistent"]):
            self.assertEqual(cli.own_flags_in(list(argv)), set(), argv)
        self.assertEqual(
            cli.own_flags_in(["--output", "--persistent", "--off",
                              "--persistent"]), {"--persistent"})

    def test_an_ordinary_argv_is_handed_on_untouched(self):
        argv = ["--output", "DP-1", "--primary", "--mode", "1920x1080",
                "--pos", "0x0", "--rotate", "normal", "--output", "HDMI-2",
                "--off"]
        self.assertEqual(cli.scan_backend_argv(list(argv)),
                         (None, False, argv))


class Precedence(Stubbed):
    def test_flag_beats_environment_beats_detection(self):
        os.environ["WXRANDR_BACKEND"] = "kwin"
        self.assertEqual(cli.resolve_backend("sway"),
                         ("sway", "flag", "--backend sway"))
        self.assertEqual(cli.resolve_backend(None),
                         ("kwin", "environment", "WXRANDR_BACKEND=kwin"))
        self.assertEqual(cli.resolve_backend("gnome")[0], "mutter")
        os.environ.pop("WXRANDR_BACKEND")
        self.assertEqual(cli.resolve_backend(None), (None, "detection", None))

    def test_auto_is_not_a_forcing(self):
        os.environ["WXRANDR_BACKEND"] = "auto"
        self.assertEqual(cli.resolve_backend("auto"), (None, "detection", None))
        self.assertEqual(cli.resolve_backend(None), (None, "detection", None))

    def test_x11_is_a_value_the_environment_may_hold(self):
        """It was meaningless before the flag existed; now it means what the
        flag means, and main() reads it there (see test_passthrough_exec)."""
        os.environ["WXRANDR_BACKEND"] = "x11"
        self.assertEqual(cli.resolve_backend(None),
                         ("x11", "environment", "WXRANDR_BACKEND=x11"))
        self.assertEqual(cli.resolve_backend("mutter")[0], "mutter")
        self.assertEqual(cli.resolve_backend("auto")[0], "x11")

    def test_the_detection_order(self):
        """sway, then Hyprland, then the two bus protocols, then Muffin's copy of Mutter's -- and wlr as what
        is left, never probed for the decision.

        hypr is second because the wlr path is honest for reading a Hyprland layout and cannot apply one: the
        second apply of a session times out at 10 s with nothing changed, identically for `wlr-randr`, and
        with a second output present even the first one hangs [M recon2/hyprland.md §4]. Detection stops at
        the first available backend, so a probe below the answer is never even asked -- which is what keeps a
        Hyprland session from paying a KWin bus round trip."""
        name, _p = cli.detect_wayland()
        self.assertEqual(name, "mutter")
        self.assertEqual(self.probe.calls, ["sway", "hypr", "kwin", "mutter"])
        self.probe.calls = []
        self.probe.table["sway"] = (True, "IPC socket /run/sway.sock")
        self.assertEqual(cli.detect_wayland()[0], "sway")
        self.assertEqual(self.probe.calls, ["sway"])
        self.probe.calls = []
        self.probe.table["sway"] = (False, "no sway or i3 IPC socket")
        self.probe.table["hypr"] = (True, "IPC socket /run/user/1000/hypr/x/.socket.sock")
        self.assertEqual(cli.detect_wayland()[0], "hypr")
        self.assertEqual(self.probe.calls, ["sway", "hypr"])
        self.probe.calls = []
        self.probe.table["hypr"] = (False, "no Hyprland IPC socket")
        self.probe.table["kwin"] = (True, "kde_output_management_v2 version 12")
        self.assertEqual(cli.detect_wayland()[0], "kwin")
        self.assertEqual(self.probe.calls, ["sway", "hypr", "kwin"])
        self.probe.calls = []
        self.probe.table["kwin"] = (False, "no kde_output_management_v2")
        self.probe.table["mutter"] = (False, "no session bus")
        self.probe.table["cinnamon"] = (True, "org.cinnamon.Muffin.DisplayConfig on the session bus")
        self.assertEqual(cli.detect_wayland()[0], "cinnamon")
        self.assertEqual(self.probe.calls, ["sway", "hypr", "kwin", "mutter", "cinnamon"])

    def test_a_hypr_session_never_asks_the_bus(self):
        """The probe order is also a cost: `hypr` above the two D-Bus probes means a Hyprland box pays one
        socket stat and no bus connection at all. Hyprland owns neither bus name [M recon2/hyprland.md §2], so
        putting it below them would have been a round trip that can only ever answer no."""
        self.probe.table["hypr"] = (True, "IPC socket /run/user/1000/hypr/x/.socket.sock")
        self.assertEqual(cli.detect_wayland()[0], "hypr")
        self.assertNotIn("kwin", self.probe.calls)
        self.assertNotIn("mutter", self.probe.calls)

    def test_wlr_is_the_fallback_and_is_not_probed_for_it(self):
        self.probe.table["mutter"] = (False, "no session bus")
        self.assertEqual(cli.detect_wayland()[0], "wlr")
        self.assertNotIn("wlr", self.probe.calls)

    def test_chosen_backend_on_an_x11_session(self):
        """Detection asks the session kind first: X11 is where main() hands
        over, so that is the honest answer."""
        env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "PATH": "/nonexist"}
        self.assertEqual(cli.chosen_backend(None, env)[0], "x11")
        self.assertEqual(cli.chosen_backend("mutter", env)[0], "mutter")
        self.assertEqual(cli.chosen_backend("auto", env)[0], "x11")

    def test_the_flag_reaches_the_session(self):
        seen = []

        class FakeSession:
            def __init__(self, forced=None):
                seen.append(forced)
                raise cli.Fatal("stop here\n")
        orig, cli.Session = cli.Session, FakeSession
        try:
            self.assertEqual(self.run_cli("--query")[0], 1)
            self.assertEqual(self.run_cli("--backend", "kde", "--query")[0], 1)
        finally:
            cli.Session = orig
        self.assertEqual(seen, [None, "kwin"])


class Info(Stubbed):
    def test_print_backend_is_one_token(self):
        code, out, err = self.run_cli("--print-backend")
        self.assertEqual((code, out, err), (0, "mutter\n", ""))
        code, out, err = self.run_cli("--backend", "sway", "--print-backend")
        self.assertEqual((code, out), (0, "sway\n"))

    def test_print_backend_verbose(self):
        code, out, err = self.run_cli("--print-backend", "--verbose")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "mutter\n"
                              "session: wayland\n"
                              "chosen by: detection\n"
                              "compositor: Mutter\n"
                              "protocol: org.gnome.Mutter.DisplayConfig "
                              "(D-Bus)\n"
                              "available: yes\n")

    def test_print_backend_verbose_says_why_and_what_is_missing(self):
        os.environ["WXRANDR_BACKEND"] = "kwin"
        code, out, _err = self.run_cli("--print-backend", "--verbose")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "kwin", "session: wayland",
            "chosen by: environment (WXRANDR_BACKEND=kwin)",
            "available: no (the compositor does not advertise "
            "kde_output_management_v2)"])
        code, out, _err = self.run_cli("--backend", "sway", "--print-backend",
                                       "--verbose")
        self.assertEqual(out.splitlines()[2],
                         "chosen by: flag (--backend sway)")

    def test_print_backend_for_x11_names_the_real_xrandr(self):
        env = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
        lines = cli.print_backend_lines(None, env, verbose=True)
        self.assertEqual(lines, ["x11", "session: x11", "chosen by: detection",
                                 "compositor: X server (RandR)",
                                 "real xrandr: /usr/bin/xrandr",
                                 "available: yes"])

    def test_backends_table(self):
        code, out, err = self.run_cli("--backends")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out,
                         "  sway      unavailable  no sway or i3 IPC socket "
                         "($SWAYSOCK)\n"
                         "  hypr      unavailable  no Hyprland IPC socket "
                         "($HYPRLAND_INSTANCE_SIGNATURE)\n"
                         "  kwin      unavailable  the compositor does not "
                         "advertise kde_output_management_v2\n"
                         "* mutter    available    "
                         "org.gnome.Mutter.DisplayConfig on the session bus\n"
                         "  cinnamon  unavailable  org.cinnamon.Muffin."
                         "DisplayConfig is not on the session bus\n"
                         "  wlr       unavailable  the compositor does not "
                         "advertise zwlr_output_manager_v1\n"
                         "  x11       available    /usr/bin/xrandr\n")

    def test_the_table_is_one_column_grid(self):
        """`cinnamon` is the longest token `--backend` takes; a name column narrower than that puts one row's
        state and reason out of line with every other's."""
        code, out, _err = self.run_cli("--backends")
        self.assertEqual(code, 0)
        starts = {ln.index("available") if "unavailable" not in ln
                  else ln.index("unavailable") for ln in out.splitlines()}
        self.assertEqual(len(starts), 1, out)

    def test_every_backend_the_flag_takes_has_a_row(self):
        """`--backends` is the answer to "why did it not pick the one I wanted", so a name `--backend` accepts
        and this table never mentions is a name with no explanation attached to it."""
        code, out, _err = self.run_cli("--backends")
        self.assertEqual(code, 0)
        # the row is "  name  state  reason", with `*` in column 0 on the one
        # auto would pick; the order is the detection order, not the flag's
        rows = [ln[2:].split()[0] for ln in out.splitlines()]
        self.assertEqual(sorted(rows),
                         sorted(n for n in cli.BACKEND_NAMES if n != "auto"))
        self.assertEqual(rows, list(cli.AUTO_ORDER) + [cli.AUTO_FALLBACK, "x11"])

    def test_backends_marks_what_auto_would_choose_not_what_is_forced(self):
        code, out, _err = self.run_cli("--backend", "sway", "--backends")
        self.assertEqual(code, 0)
        marked = [ln[2:10].strip() for ln in out.splitlines()
                  if ln.startswith("*")]
        self.assertEqual(marked, ["mutter"])

    def test_the_informational_options_never_open_a_session(self):
        class NoSession:
            def __init__(self, forced=None):
                raise AssertionError("--print-backend touched the layout")
        orig, cli.Session = cli.Session, NoSession
        try:
            self.assertEqual(self.run_cli("--print-backend")[0], 0)
            self.assertEqual(self.run_cli("--backends")[0], 0)
            self.assertEqual(self.run_cli("--backend", "kwin",
                                          "--print-backend")[0], 0)
        finally:
            cli.Session = orig


class Errors(Stubbed):
    def test_an_unknown_name_lists_the_valid_ones(self):
        code, out, err = self.run_cli("--backend", "banana", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err,
                         "xrandr: --backend: invalid argument 'banana'; "
                         "valid: auto, x11, sway, hypr, wlr, mutter, cinnamon, kwin\n"
                         "Try 'xrandr --help' for more information.\n")
        code, _out, err = self.run_cli("--backend=nope")
        self.assertEqual(code, 1)
        self.assertIn("invalid argument 'nope'", err)

    def test_forcing_an_unavailable_backend_names_what_is_missing(self):
        code, out, err = self.run_cli("--backend", "sway", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: --backend sway is not available in "
                              "this session: no sway or i3 IPC socket "
                              "($SWAYSOCK)\n")
        code, _out, err = self.run_cli("--backend", "kde", "--output", "DP-1",
                                       "--off")
        self.assertEqual(code, 1)
        self.assertEqual(err, "xrandr: --backend kwin is not available in "
                              "this session: the compositor does not "
                              "advertise kde_output_management_v2\n")

    def test_no_silent_fallback(self):
        """The forced one fails; the one auto would have picked is not tried
        behind the user's back."""
        code, _out, err = self.run_cli("--backend", "wlr", "--query")
        self.assertEqual(code, 1)
        self.assertIn("--backend wlr is not available", err)
        self.assertNotIn("mutter", err)

    def test_the_environment_variable_keeps_its_own_behaviour(self):
        """WXRANDR_BACKEND is *not* pre-checked (that would change bytes
        every existing test pins): the backend itself says what is wrong."""
        os.environ["WXRANDR_BACKEND"] = "sway"
        code, _out, err = self.run_cli("--query")
        self.assertEqual(code, 1)
        self.assertNotIn("is not available in this session", err)
        self.assertIn("Can't open display", err)

    def test_x11_from_a_library_call_is_one_line(self):
        """At a command line `--backend x11` never reaches here: main() has
        exec'd the real xrandr.  Embedded, where a process may not be
        replaced, it is a fatal, not a traceback."""
        code, out, err = self.run_cli("--backend", "x11", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: --backend x11 hands over to the real "
                              "xrandr, which an embedded call cannot do\n")


class EnvironmentX11(Stubbed):
    """`WXRANDR_BACKEND=x11` is the same request as `--backend x11`.  At a
    command line main() hands over before parsing (proved with a real
    `execve` in tests/test_passthrough_exec.py); embedded, where a process
    may not be replaced, it is one fatal line -- naming the variable that
    asked, not a flag nobody typed."""

    def test_it_is_one_line_naming_the_variable(self):
        os.environ["WXRANDR_BACKEND"] = "x11"
        code, out, err = self.run_cli("--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "xrandr: WXRANDR_BACKEND=x11 hands over to the "
                              "real xrandr, which an embedded call cannot "
                              "do\n")

    def test_the_flag_still_beats_it(self):
        os.environ["WXRANDR_BACKEND"] = "x11"
        code, _out, err = self.run_cli("--backend", "sway", "--query")
        self.assertEqual(code, 1)
        self.assertIn("--backend sway is not available", err)
        self.assertNotIn("WXRANDR_BACKEND", err)

    def test_the_informational_options_answer_for_it(self):
        os.environ["WXRANDR_BACKEND"] = "x11"
        code, out, _err = self.run_cli("--print-backend")
        self.assertEqual((code, out), (0, "x11\n"))


class DroppedOnTheHandover(Stubbed):
    """What an X11 session does with the two options only we have.

    Measured on both X11 images in the 0.4 retest: `wxrandr --persistent
    --output Virtual-2 --below Virtual-1` came back as the real xrandr's
    `unrecognized option '--persistent'`, exit 1, layout unchanged -- so the
    option had to be stripped before the handover, which it now is
    (`Lookahead.test_our_own_apply_options_are_stripped_too`).  Stripping it
    silently is the other half of the same problem: a script that has been
    asking for a persistent layout on an X11 box has never been getting one and
    would never learn that from the output.

    `--unsafe-gnome-overlap` is the opposite case and stays refused: X11 places
    overlapping monitors by itself, so a user who typed it has misunderstood
    something rather than asked for something unavailable."""

    def handover(self, *argv):
        """`main()` with a real argv, so `entry` is true and the handover is the
        one a command line gets; `maybe_exec_real` is recorded rather than run,
        because the real one would `execve` this process."""
        seen = []

        def record(tool, args=None, **kw):
            seen.append((tool, list(args or []), kw))
            return 0

        out, err = io.StringIO(), io.StringIO()
        real, passthrough.maybe_exec_real = passthrough.maybe_exec_real, record
        saved_argv = list(sys.argv)
        sys.argv = ["wxrandr"] + list(argv)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main()
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 0
        finally:
            passthrough.maybe_exec_real = real
            sys.argv = saved_argv
        return code, out.getvalue(), err.getvalue(), seen

    def test_persistent_is_dropped_and_said_once(self):
        code, out, err, seen = self.handover("--backend", "x11", "--persistent",
                                             "--output", "DP-1", "--auto")
        self.assertEqual((code, out), (0, ""))
        # the real xrandr gets the command without our option, and nothing else
        # about it changed
        self.assertEqual(len(seen), 1, seen)
        tool, args, kw = seen[0]
        self.assertEqual((tool, args), ("xrandr", ["--output", "DP-1", "--auto"]))
        self.assertEqual((kw["entry"], kw["force"]), (True, True))
        # exactly one line, and it names the option and the session
        lines = [ln for ln in err.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertIn("--persistent", lines[0])
        self.assertIn("X11", lines[0])
        self.assertTrue(lines[0].startswith("xrandr: "), lines[0])

    def test_an_ordinary_handover_says_nothing(self):
        """The control: every command that does not carry one of our options
        hands over in silence, which is the whole promise of the clone."""
        code, out, err, seen = self.handover("--backend", "x11", "--output",
                                             "DP-1", "--auto")
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(seen[0][1], ["--output", "DP-1", "--auto"])

    def test_the_overlap_flag_is_still_refused_rather_than_dropped(self):
        code, out, err, seen = self.handover("--backend", "x11",
                                             "--unsafe-gnome-overlap",
                                             "--output", "DP-1", "--pos", "960x0")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(seen, [])              # nothing was handed over at all
        self.assertIn("--unsafe-gnome-overlap only means anything on GNOME", err)
        self.assertNotIn("--persistent", err)

    def test_both_at_once_is_the_refusal_alone(self):
        """The refusal is the whole answer, and the drop line is not said with
        it.  Measured here: stderr is exactly the one refusal line and `seen` is
        empty -- the refusal returns 1 before the PERSISTENT_FLAG branch is
        reached, and nothing was handed over, so nothing was dropped either.
        Saying both would name an option that never got as far as mattering."""
        code, _out, err, seen = self.handover(
            "--backend", "x11", "--persistent", "--unsafe-gnome-overlap",
            "--output", "DP-1", "--pos", "960x0")
        self.assertEqual(code, 1)
        self.assertEqual(seen, [])
        lines = [ln for ln in err.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertIn("--unsafe-gnome-overlap only means anything on GNOME", lines[0])
        self.assertNotIn("is dropped on X11", err)


class TheOverlapFlagOffGnome(unittest.TestCase):
    """The sentence a non-GNOME Wayland session gets for `--unsafe-gnome-overlap`, byte for byte.

    It moved on 2026-09-09.  The apply gate used to write its own "which places overlapping monitors
    without it", which is FALSE on Cinnamon -- Muffin refuses the very layout that sentence promises -- so
    all three callers now come off `gnome_overlap.not_gnome_reason()` and its generic arm reads "without any
    of this" (requests-batch-7.md, batch 7 -> batch 20).  Every existing test stopped at "only means
    anything on GNOME", so the half that changed was unpinned and the next reword would have been silent.

    `_run_session` is called directly because the refusal is its first statement, before a session is asked
    anything: a kwin or cinnamon session that could answer would be a fixture about the wrong thing."""

    class _Sess:
        """Only the fields `_run_session` reads before it refuses."""

        backend = "kwin"
        persistent = False
        overlap = False
        overlap_force = False

    def refusal(self, backend):
        sess = self._Sess()
        sess.backend = backend
        opts = cli.Opts()
        opts.overlap = True
        with self.assertRaises(cli.Fatal) as cm:
            cli._run_session(sess, opts)
        return cm.exception.args[0]

    def test_kwin_is_told_it_places_them_without_any_of_this(self):
        self.assertEqual(self.refusal("kwin"),
                         "--unsafe-gnome-overlap only means anything on GNOME; this session is kwin, "
                         "which places overlapping monitors without any of this\n")

    def test_sway_gets_the_same_sentence_with_its_own_name_in_it(self):
        self.assertEqual(self.refusal("sway"),
                         "--unsafe-gnome-overlap only means anything on GNOME; this session is sway, "
                         "which places overlapping monitors without any of this\n")

    def test_cinnamon_gets_muffins_own_reason_and_never_the_generic_one(self):
        """The whole point of routing the three callers through one function: a Cinnamon session may not be
        told that it places overlapping monitors without this, because it does not place them at all."""
        line = self.refusal("cinnamon")
        self.assertNotIn("without any of this", line)
        self.assertNotIn("without it", line)
        self.assertIn(gnome_overlap.CINNAMON_REASON, line)


class ReadmeOnTheHandover(unittest.TestCase):
    """The paragraph that promises the handover, against what it does.

    F7.5(a): README said `execve` and argv *untouched*, and argv is not
    untouched -- `scan_backend_argv` strips `--backend`, `--persistent` and
    `--unsafe-gnome-overlap` before the original ever sees them, because the
    original has never had any of the three and would answer `unrecognized
    option` to the whole command."""

    def readme(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            return f.read()

    def sentences(self):
        """Every sentence of README, unwrapped: the claim and the exception to
        it have to be in the same one, or a reader meets the claim alone."""
        out = []
        for para in self.readme().split("\n\n"):
            out.extend(" ".join(para.split()).split(". "))
        return out

    def test_untouched_argv_is_only_claimed_where_our_options_are_named(self):
        claims = [s for s in self.sentences() if "argv untouched" in s]
        self.assertTrue(claims, "the handover paragraph is gone from README")
        for s in claims:
            for flag in cli.OWN_APPLY_FLAGS:
                self.assertIn(flag, s, s)

    def test_the_readme_says_what_becomes_of_each_of_them(self):
        """Not just that they are stripped: dropped and refused are different
        outcomes and the two options get different ones."""
        text = " ".join(self.readme().split())
        self.assertIn("`--persistent` is dropped", text)
        self.assertIn("`--unsafe-gnome-overlap` is refused", text)


class WlrNaming(unittest.TestCase):
    """U30: what `--print-backend --verbose` calls the compositor behind zwlr_output_manager_v1.

    It said `wlroots` on every one of them, which is a word nobody's desktop is called: measured on COSMIC
    [M recon2/cosmic.md §3], on labwc, on Budgie and on Xfce-on-Wayland [M recon2/labwc.md, budgie.md,
    xfce-wayland.md]. Two answers replace it, and they are not the same kind of claim. COSMIC is read off the
    registry -- cosmic-comp advertises `zcosmic_output_manager_v1` beside the wlr protocol and nothing else
    does -- which is evidence. `$XDG_CURRENT_DESKTOP` is a hint: it decides no code path and gates nothing,
    the token stays `wlr` either way, and it is there so the line stops being useless.

    The registries are the recorded ones, replayed by `wl_fake.registry_server`."""

    def probe(self, fixture, **environ):
        fake = wl_fake.registry_server(fixture)
        self.addCleanup(fake.close)
        env_ = {"WAYLAND_DISPLAY": fake.path, "PATH": "/nonexistent"}
        env_.update({k: v for k, v in environ.items() if v is not None})
        with support_env(WAYLAND_DISPLAY=fake.path, XDG_RUNTIME_DIR=fake.dir,
                         XDG_CURRENT_DESKTOP=environ.get("XDG_CURRENT_DESKTOP")):
            p = cli.probe_backend("wlr", env_)
        self.addCleanup(p.close)
        return p

    def test_cosmic_is_named_from_the_protocol_it_adds(self):
        p = self.probe("cosmic")
        self.assertTrue(p.available, p.reason)
        self.assertEqual(p.compositor, "COSMIC (wlr-output-management)")
        # the token is unchanged: rotation and 1.5x scale matched cosmic-randr
        # over the plain wlr protocol, so there is no COSMIC display backend
        self.assertEqual(p.name, "wlr")
        self.assertEqual(p.protocol, "zwlr_output_manager_v1 version 4")

    def test_the_desktop_variable_is_appended_where_there_is_no_better_answer(self):
        for fixture, xdg, want in (
                ("labwc", "labwc:wlroots", "wlroots (XDG_CURRENT_DESKTOP=labwc:wlroots)"),
                ("budgie", "Budgie", "wlroots (XDG_CURRENT_DESKTOP=Budgie)"),
                ("xfce-labwc", "XFCE", "wlroots (XDG_CURRENT_DESKTOP=XFCE)"),
                ("labwc", "LXQt:labwc:wlroots",
                 "wlroots (XDG_CURRENT_DESKTOP=LXQt:labwc:wlroots)")):
            with self.subTest(xdg):
                p = self.probe(fixture, XDG_CURRENT_DESKTOP=xdg)
                self.assertTrue(p.available, p.reason)
                self.assertEqual(p.compositor, want)

    def test_a_desktop_that_names_its_own_backend_is_left_alone(self):
        """sway exports the variable too and is named by a backend of its own; appending it here would put
        `wlroots (XDG_CURRENT_DESKTOP=sway)` in front of somebody who forced `--backend wlr` on sway, which
        says less than `wlroots` did."""
        p = self.probe("labwc", XDG_CURRENT_DESKTOP="sway")
        self.assertEqual(p.compositor, "wlroots")

    def test_with_the_variable_unset_it_is_the_old_word(self):
        p = self.probe("labwc")
        self.assertEqual(p.compositor, "wlroots")

    def test_cosmic_wins_over_the_variable(self):
        p = self.probe("cosmic", XDG_CURRENT_DESKTOP="COSMIC")
        self.assertEqual(p.compositor, "COSMIC (wlr-output-management)")

    def test_a_registry_with_no_output_manager_is_still_unavailable(self):
        """Cinnamon's Muffin advertises 23 globals and no wlr output protocol at all [M recon2/cinnamon.md
        §2.1]: the naming change must not turn a refusal into an answer."""
        fake = wl_fake.registry_server("cinnamon")
        self.addCleanup(fake.close)
        with support_env(WAYLAND_DISPLAY=fake.path, XDG_RUNTIME_DIR=fake.dir):
            p = cli.probe_backend("wlr", {"WAYLAND_DISPLAY": fake.path})
        self.assertFalse(p.available)
        self.assertEqual(p.reason,
                         "the compositor does not advertise zwlr_output_manager_v1")
        self.assertIsNone(p.compositor)


class X11Probe(unittest.TestCase):
    """The one probe with no compositor in it: which real xrandr `x11` is."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxr-x11-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # The reason string goes through distro.hint('xrandr'), which reads /etc/os-release. Left alone this
        # class would assert the host's answer -- Debian's here, `dnf install xrandr` on the Fedora CI image
        # -- so the family is planted and the bytes below are Debian's on every box.
        rel = os.path.join(self.tmp, "os-release")
        with open(rel, "w", encoding="utf-8") as fh:
            fh.write('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n')
        for name, value in (("OS_RELEASE", rel),
                            ("NIXOS_MARKER", os.path.join(self.tmp, "NIXOS"))):
            old = getattr(distro, name)
            setattr(distro, name, value)
            self.addCleanup(setattr, distro, name, old)

    def test_finds_the_real_xrandr(self):
        p = cli.probe_backend("x11", {"PATH": self.tmp})
        self.assertFalse(p.available)
        self.assertEqual(p.reason,
                         "no real xrandr on PATH (apt install x11-xserver-utils)")
        real = os.path.join(self.tmp, "xrandr")
        with open(real, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(real, stat.S_IRWXU)
        passthrough.reset_cache()
        p = cli.probe_backend("x11", {"PATH": self.tmp})
        self.assertTrue(p.available, p.reason)
        self.assertEqual(p.detail, real)

    def test_an_unusable_override_is_the_reason(self):
        p = cli.probe_backend("x11", {"PATH": self.tmp,
                                      "WXRANDR_REAL_XRANDR": "/no/such/tool"})
        self.assertFalse(p.available)
        self.assertIn("WXRANDR_REAL_XRANDR", p.reason)
        self.assertNotIn("\n", p.reason)


class CinnamonProbe(unittest.TestCase):
    """`_probe_cinnamon()` against a real dbus_mini client and a real bus, because the only thing it does is
    ask one bus a question about one name. A Cinnamon session owns `org.cinnamon.Muffin.DisplayConfig` and
    never `org.gnome.Mutter.DisplayConfig` [M recon2/cinnamon.md §2.2], so the name is the whole decision and
    these are the bytes `--backends` prints under it."""

    DEST = "org.cinnamon.Muffin.DisplayConfig"

    @classmethod
    def setUpClass(cls):
        cls.mock = MockBus()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def setUp(self):
        # an empty runtime tree, so find_user_bus()'s scan cannot reach the host's own bus and the planted
        # address is the only one there is
        self.tmp = tempfile.mkdtemp(prefix="wxr-cinn-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name, value in (("RUN_USER_DIR", self.tmp), ("X11_SOCKET_DIR", self.tmp)):
            old = getattr(session, name)
            setattr(session, name, value)
            self.addCleanup(setattr, session, name, old)

    @contextlib.contextmanager
    def bus_env(self, address):
        with support_env(DBUS_SESSION_BUS_ADDRESS=address, WAYLAND_DISPLAY=None,
                         XDG_RUNTIME_DIR=self.tmp):
            yield

    def owner(self):
        """A second client of the mock bus holding Muffin's name, as a Cinnamon session's Muffin does."""
        held = dbus_mini.Bus(self.mock.address)
        self.addCleanup(held.close)
        self.assertEqual(held.request_name(self.DEST), 1)
        return held

    def test_no_name_on_the_bus_is_the_reason(self):
        with self.bus_env(self.mock.address):
            p = cli.probe_backend("cinnamon")
        self.assertFalse(p.available)
        self.assertEqual(p.reason, "org.cinnamon.Muffin.DisplayConfig is not on the session bus")
        self.assertIsNone(p.handle)

    def test_the_name_on_the_bus_is_the_backend(self):
        self.owner()
        with self.bus_env(self.mock.address):
            p = cli.probe_backend("cinnamon")
        self.addCleanup(p.handle.close)
        self.assertTrue(p.available, p.reason)
        self.assertEqual(p.compositor, "Muffin")
        self.assertEqual(p.protocol, "org.cinnamon.Muffin.DisplayConfig (D-Bus)")
        self.assertEqual(p.detail, "org.cinnamon.Muffin.DisplayConfig on the session bus")
        self.assertIsInstance(p.handle, dbus_mini.Bus)
        # the handle is live: the caller constructs MutterOutputs on it rather than dialling a second time
        self.assertIn("org.freedesktop.DBus", p.handle.list_names())

    def test_the_unavailable_probe_closes_the_bus_it_opened(self):
        """The available probe hands its connection on in `handle`; this one owns it to the end and closes it.
        Asserted on the call and not on the mock's connection list, because the dropped Probe holds no
        reference either way and CPython's refcounting would close the socket for us -- which would make the
        socket-count version of this test unable to fail.

        The name patched is `wxrandr.mutter.Bus`, not `w11common.dbus_mini.Bus`: since 2026-09-09 the two
        flavours share one probe (`mutter.probe(flavor=MUFFIN)`), which binds `Bus` at import time, and
        mutter.py is where the close now happens."""
        opened = []
        real = mutter_mod.Bus

        class Recording(real):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.closes = 0
                opened.append(self)

            def close(self):
                self.closes += 1
                super().close()

        mutter_mod.Bus = Recording
        self.addCleanup(setattr, mutter_mod, "Bus", real)
        with self.bus_env(self.mock.address):
            p = cli.probe_backend("cinnamon")
        self.assertFalse(p.available)
        self.assertEqual(len(opened), 1)       # one dial, not one per question asked
        self.assertEqual(opened[0].closes, 1, "the refusing probe left its connection open")

    def test_no_bus_at_all(self):
        with self.bus_env("unix:path=" + os.path.join(self.tmp, "nothing-here")):
            p = cli.probe_backend("cinnamon")
        self.assertEqual((p.available, p.reason), (False, "no session bus"))


class HyprProbe(unittest.TestCase):
    """`_probe_hypr()`'s two answers that need no Hyprland: no socket, and a socket with no
    hacks/display/hypr.py built into this install."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxr-hypr-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        old = session.RUN_USER_DIR
        session.RUN_USER_DIR = self.tmp
        self.addCleanup(setattr, session, "RUN_USER_DIR", old)

    def test_no_socket_is_the_reason(self):
        with support_env(HYPRLAND_INSTANCE_SIGNATURE=None, XDG_RUNTIME_DIR=self.tmp):
            p = cli.probe_backend("hypr")
        self.assertFalse(p.available)
        self.assertEqual(p.reason, "no Hyprland IPC socket ($HYPRLAND_INSTANCE_SIGNATURE)")

    def test_a_socket_with_no_backend_built_is_still_a_row_and_not_a_traceback(self):
        """`hypr` is second in AUTO_ORDER, so a probe that raises takes down every wxrandr invocation on a
        Hyprland desktop -- `--query` included, which reads honestly over the wlr floor
        [M recon2/hyprland.md §4]. Until hacks/display/hypr.py lands this asserts the guard; after it lands
        the import succeeds and the skip below fires instead."""
        rd = os.path.join(self.tmp, "user", "%d" % os.getuid())
        os.makedirs(os.path.join(rd, "hypr", "sig0"))
        sock = os.path.join(rd, "hypr", "sig0", ".socket.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(srv.close)
        srv.bind(sock)
        srv.listen(1)
        session.RUN_USER_DIR = os.path.join(self.tmp, "user")
        with support_env(HYPRLAND_INSTANCE_SIGNATURE=None, XDG_RUNTIME_DIR=rd):
            self.assertEqual(session.find_hypr_socket(), sock)
            p = cli.probe_backend("hypr")          # must not raise
        try:
            import hacks.display.hypr                     # noqa: F401
        except ImportError:
            self.assertFalse(p.available)
            self.assertEqual(p.reason,
                             "the hypr display backend is not built into this install")
            return
        self.skipTest("hacks/display/hypr.py is built here; tests/test_wxrandr_hypr.py owns this probe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
