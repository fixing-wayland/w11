"""wxrandr's own sway IPC client against a compositor that is wedged, dying or
lying.

wdotool's sway backend was hardened first (tests/test_wire_hardening.py:
SwayWireGuards) and wxrandr's was not, although it speaks the same i3-ipc
protocol on the same socket and is the one that applies layouts.  The two are
separate code -- `hacks/window/backend_sway.py` against `hacks/display/core.py`'s
`SwayIPC` -- so nothing proved about one holds for the other, and this is the
other.

The double is `support.FakeSway`, the same one wdotool's guards use, in the six
shapes a real sway can present: answering, going away mid-chain, framing a reply
whose body is not JSON, accepting the connection and never answering again,
refusing an output command in words, and describing an output without the field
that says where it is.  Everything here goes through `cli.main` with `--backend
sway`, because that is what a user runs and because the answer that matters is
what reaches stderr and what the exit status is -- one line, never a traceback,
and never an unbounded wait.

sway lives on this host (live-measurements.md: test_windows_sway,
test_input_geometry_sway and test_wxrandr_live all pass against a real headless
sway 1.11 here), and tests/test_wxrandr_live.py is where the healthy path is
measured.  Nothing here needs a compositor.
"""

import contextlib
import gc
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
import warnings
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

import support
from wxrandr import cli
from hacks.display import core

#: what `SwayIPC.__init__` arms the command socket with (hacks/display/core.py, a
#: literal rather than a module constant -- wdotool's twin is
#: `backend_sway.IPC_TIMEOUT` and is patchable).  A wedged sway is answered by
#: this and by nothing else, so the tests below replace the class with one that
#: shortens it rather than waiting ten seconds a piece.
SOCKET_TIMEOUT = 10.0

#: long enough that a real wait would show, short enough that the file stays
#: fast: every bounded assertion below is against this
SHORT = 0.5


class ShortIPC(core.SwayIPC):
    """`SwayIPC` with a deadline a test can wait for.

    Not a stub: it is the real client on the real socket, with one line of the
    constructor's effect changed.  Everything the assertions are about --
    framing, `_read_exact`, the JSON, the RUN_COMMAND result list -- is the
    shipped code."""

    def __init__(self, sockpath=None):
        super().__init__(sockpath)
        self.sock.settimeout(SHORT)


class _Wire(unittest.TestCase):
    """One FakeSway per test, reached as the session's own compositor.

    No tests of its own: `SwayWire` below is the hostile-compositor half and `I3Dialect` the i3 half, and
    both want this harness and neither wants the other's cases."""

    def sway(self, mode, **kw):
        srv = support.FakeSway(mode, **kw)
        self.addCleanup(srv.close)
        self.env("SWAYSOCK", srv.path)
        return srv

    def env(self, name, value):
        old = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        self.addCleanup(self._restore, name, old)

    def _restore(self, name, old):
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-sway-wire-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # the state file is a cache and belongs in this test's directory, never
        # in the runtime directory of whoever is running the suite
        self.env("XDG_RUNTIME_DIR", self.tmp)
        # there is no compositor here beyond the fake socket: the wlr
        # enrichment connection must not be attempted against a real one
        self.env("WAYLAND_DISPLAY", None)
        self.env("WXRANDR_BACKEND", None)

    def run_cli(self, *argv, short=True):
        """`(code, stdout, stderr, seconds)` for one `cli.main`."""
        out, err = io.StringIO(), io.StringIO()
        started = time.monotonic()
        with mock.patch.object(core, "SwayIPC", ShortIPC if short else core.SwayIPC):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(list(argv))
                except SystemExit as e:
                    code = e.code if isinstance(e.code, int) else 0
        return code, out.getvalue(), err.getvalue(), time.monotonic() - started

    def one_line(self, err):
        """The single `xrandr:` line, asserting that it is single."""
        lines = [ln for ln in err.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("xrandr: "), lines[0])
        return lines[0]


class SwayWire(_Wire):
    """The five ways a compositor on the sway socket goes wrong, and the healthy control."""

    # -- the healthy control -------------------------------------------------

    def test_an_answering_sway_needs_no_wayland_display(self):
        """The control every test below is measured against, and one claim of
        its own: the sway backend is the IPC socket and the IPC socket only.
        `wlr_snapshot_safe()` returns None without `$WAYLAND_DISPLAY` and the
        query is answered out of GET_OUTPUTS alone."""
        srv = self.sway("ok")
        self.assertIsNone(os.environ.get("WAYLAND_DISPLAY"))
        code, out, err, _s = self.run_cli("--backend", "sway", "--query")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("HEADLESS-1 connected 1280x720+0+0", out)
        self.assertIn("HEADLESS-2 connected 1280x1024+1280+0", out)
        # GET_VERSION then GET_OUTPUTS, and nothing else. The version is the dialect (`SwayIPC.dialect()`,
        # cached for the connection): one round trip per session buys the i3 answers below.
        self.assertEqual([mt for mt, _p in srv.requests],
                         [support.GET_VERSION, support.GET_OUTPUTS])

    def test_the_command_socket_carries_a_deadline_at_all(self):
        """Which is the whole reason a wedged sway ends in a message rather than
        for ever.  The number is a literal in `SwayIPC.__init__`; what this
        pins is that there is one and that it is finite."""
        srv = self.sway("wedged")
        ipc = core.SwayIPC(srv.path)
        self.addCleanup(ipc.close)
        self.assertEqual(ipc.sock.gettimeout(), SOCKET_TIMEOUT)

    # -- the five ways it goes wrong -----------------------------------------

    def test_a_wedged_compositor_is_one_bounded_line(self):
        """Accepted, never answered.  The kernel completes the connect for a
        compositor stuck in its own event loop, so nothing before the first
        read can tell; only the socket's own deadline ends the wait."""
        srv = self.sway("wedged")
        code, out, err, secs = self.run_cli("--backend", "sway", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(self.one_line(err), "xrandr: timed out")
        self.assertGreater(secs, SHORT * 0.8, secs)
        self.assertLess(secs, 5.0, secs)
        self.assertEqual(srv.requests, [])          # it never read our frame

    def test_a_session_that_ends_mid_chain_is_one_line(self):
        """One answer, then the socket goes: ECONNRESET on the read, EPIPE on
        the next write.  An apply is two round trips (GET_OUTPUTS, then the
        `output ...` command), so it is the one that meets the second half.

        Either wording is accepted on purpose.  Which one the user gets is a
        race between the RST arriving and the read seeing a clean EOF, and both
        were seen on this host across repeated runs of this file -- so do not
        tighten it to one of them, it will be flaky."""
        self.sway("gone")
        code, out, err, secs = self.run_cli("--backend", "sway", "--output",
                                            "HEADLESS-2", "--pos", "0x720")
        self.assertEqual((code, out), (1, ""))
        line = self.one_line(err)
        self.assertTrue("Connection reset by peer" in line
                        or "compositor IPC connection closed" in line, line)
        self.assertLess(secs, 5.0, secs)

    def test_a_reply_that_is_not_json_is_one_line(self):
        """Well-framed, and the body is not JSON: the frame header is the
        compositor's word for how long the body is, and believing it is not the
        same as believing the body."""
        self.sway("badjson")
        code, out, err, secs = self.run_cli("--backend", "sway", "--query")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Expecting property name", self.one_line(err))
        self.assertLess(secs, 5.0, secs)

    def test_a_refusal_reaches_the_user_in_sways_words(self):
        """sway answers RUN_COMMAND with a result list, and a `success: false`
        entry carries its own sentence.  That sentence is the whole of what the
        user can act on, so it is quoted rather than summarised -- and the
        command it refused is named with it, because one `output ...` line can
        hold several sub-commands and sway runs them all."""
        self.sway("refuse")
        code, out, err, _s = self.run_cli("--backend", "sway", "--output",
                                          "HEADLESS-2", "--pos", "0x720")
        self.assertEqual((code, out), (1, ""))
        line = self.one_line(err)
        self.assertIn(support.FakeSway.ERROR, line)
        self.assertIn("output HEADLESS-2 position 0 720", line)

    def test_an_anisotropic_scale_warns_with_a_route_and_still_applies_one_axis(self):
        """`xrandr --scale 2x1` is a transform matrix the X server takes, and no output-management protocol
        carries one -- so this is a gap of ours and the warning owes the rung that would close it.  What must
        not change is the behaviour under it: the warning goes to stderr, the apply goes on with sx for both
        axes, and the exit status stays 0 (the shape wxrandr/cli.py:343's comment already relies on)."""
        srv = self.sway("ok")
        code, out, err, _s = self.run_cli("--backend", "sway", "--output", "HEADLESS-2", "--scale", "2x1")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("anisotropic scaling 2x1 is not done yet", err)
        self.assertIn("AGENTS.md route 6", err)
        self.assertNotIn("not supported on Wayland", err)
        cmds = [p for mt, p in srv.requests if mt == support.RUN_COMMAND]
        self.assertTrue(any("scale 2" in c for c in cmds), cmds)

    def test_outputs_with_no_rect_do_not_raise(self):
        """GET_OUTPUTS rows without `rect` at all.  A compositor that is not
        sway on the sway socket, or a sway too old or too new for the field:
        every position and size in the snapshot comes from it, and reading it
        as a plain subscript is the shape that used to be a KeyError somewhere
        above the user.

        The claim here is the query survives: no traceback, exit 0, both heads
        still listed.  0x0+0+0 is the fallback the missing field leaves them at,
        and it is the fallback that stays once the line the expectedFailure
        below asks for is added -- that fix adds a sentence to stderr, it does
        not invent geometry -- so stderr is only held to "no traceback, at most
        one line of ours", not to empty."""
        self.sway("partial")
        code, out, err, secs = self.run_cli("--backend", "sway", "--query")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertLessEqual(len([ln for ln in err.splitlines() if ln.strip()]), 1, err)
        # both heads are listed, and both are where a missing rect leaves them:
        # at 0,0 with no size, which is also what the screen line adds up to
        self.assertIn("HEADLESS-1 connected 0x0+0+0", out)
        self.assertIn("HEADLESS-2 connected 0x0+0+0", out)
        self.assertIn("current 0 x 0", out)
        self.assertLess(secs, 5.0, secs)

    @unittest.expectedFailure
    def test_outputs_with_no_rect_say_which_field_was_missing(self):
        """No fix number covers this one, so it is written to fail.

        T32 asks for one line naming the field.  Today the rows are folded in
        silently: every output is placed at 0,0 with no size, the screen line
        reads `current 0 x 0`, and nothing anywhere says that what the
        compositor sent had no geometry in it.  Exit 0 on a query that answered
        nothing is worse than one line."""
        self.sway("partial")
        _code, _out, err, _s = self.run_cli("--backend", "sway", "--query")
        self.assertIn("rect", self.one_line(err))

    # -- what the run leaves behind ------------------------------------------

    def test_no_socket_is_left_to_the_collector(self):
        """`cli.Session.close()` exists because nothing used to call any
        backend's close, and every run handed its socket to the garbage
        collector -- which reports it as a ResourceWarning at whatever moment it
        gets round to it.  Each of these paths leaves through a different exit,
        and every one of them has to close."""
        for mode, argv in (("ok", ("--query",)),
                           ("wedged", ("--query",)),
                           ("badjson", ("--query",)),
                           ("refuse", ("--output", "HEADLESS-2", "--pos", "0x720")),
                           ("gone", ("--output", "HEADLESS-2", "--pos", "0x720"))):
            with self.subTest(mode=mode):
                srv = support.FakeSway(mode)
                self.addCleanup(srv.close)
                self.env("SWAYSOCK", srv.path)
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    self.run_cli("--backend", "sway", *argv)
                    gc.collect()
                self.assertEqual([str(w.message) for w in caught
                                  if issubclass(w.category, ResourceWarning)], [])


class I3Dialect(_Wire):
    """U06: the same client against i3 4.25.1, which speaks this protocol and is not sway.

    `wxrandr --backend sway` is one of the two ways onto our own code on an i3 box (the other is
    `W11_PASSTHROUGH=never`), and what it did there was half-true [M recon2/i3.md §2b]: it called the
    compositor `sway 4.25.1 (2026-02-06)`, it listed i3's `xroot-0` pseudo-output as a connected output with
    no geometry beside the real one, and every apply died with i3's own 30-token parse error after the modes
    had already been recorded.

    The double is `support.FakeSway(dialect="i3")`, replaying tests/fixtures/i3/: GET_VERSION major 4,
    GET_OUTPUTS with `xroot-0` and `screen`, and `run_output_pos.json` -- the parse error -- to any
    `output ...`."""

    #: what the live i3 called itself, out of GET_VERSION's `human_readable`
    LABEL = "i3 4.25.1 (2026-02-06)"

    def test_print_backend_verbose_names_i3(self):
        self.sway("ok", dialect="i3")
        code, out, err = self.run_cli("--backend", "sway", "--print-backend", "--verbose")[:3]
        self.assertEqual((code, err), (0, ""))
        self.assertIn("compositor: %s\n" % self.LABEL, out)
        self.assertNotIn("compositor: sway", out)
        # the token is still `sway`: one backend, two dialects, and scripts read the first line
        self.assertEqual(out.splitlines()[0], "sway")

    def test_sway_is_still_called_sway(self):
        """The control for the line above: the same code path on the same double in its sway mood."""
        self.sway("ok")
        _code, out, _err, _s = self.run_cli("--backend", "sway", "--print-backend", "--verbose")
        self.assertIn("compositor: sway 1.11\n", out)

    def test_query_drops_the_xroot_pseudo_output(self):
        """i3 reports `xroot-0` (`active: false`, no modes, no current mode) covering the X screen; it is not
        a head and xrandr must not list one."""
        self.sway("ok", dialect="i3")
        code, out, err = self.run_cli("--backend", "sway", "--query")[:3]
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("xroot-0", out)
        self.assertIn("screen connected 1920x1080+0+0", out)

    def test_query_invents_no_modes(self):
        """i3's GET_OUTPUTS carries no `modes` and no `current_mode` at all, so the block under the header is
        empty rather than filled with a mode nobody reported."""
        self.sway("ok", dialect="i3")
        out = self.run_cli("--backend", "sway", "--query")[1]
        rows = [ln for ln in out.splitlines() if ln.startswith("   ")]
        self.assertEqual(rows, [], out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.strip()]), 2, out)

    def test_an_apply_is_one_line_naming_i3_and_sends_nothing(self):
        """The refusal is up front: i3 has no `output` command, so the only thing a two-phase apply could
        achieve is recording modes that were never applied and printing i3's parse error twice."""
        srv = self.sway("ok", dialect="i3")
        code, out, err, _s = self.run_cli("--backend", "sway", "--output", "screen", "--pos", "0x0")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            self.one_line(err),
            "xrandr: this is i3, which has no output command; the X server owns the layout here"
            " -- use xrandr (or drop --backend sway)")
        self.assertNotIn(support.RUN_COMMAND, [mt for mt, _p in srv.requests])

    def test_without_the_refusal_the_same_apply_reaches_i3s_parse_error(self):
        """So the test above cannot pass by accident.  The same command, the same double, the same product
        path, with only `dialect()` lying about which compositor this is: RUN_COMMAND goes out and what comes
        back is the 30-token parse error every apply on the live i3 died with, relayed as an `xrandr:` line
        [M recon2/i3.md §2b].  That is what the one-line refusal replaces, and it is a measurement of the
        code under test rather than of the double."""
        srv = self.sway("ok", dialect="i3")
        with mock.patch.object(core.SwayIPC, "dialect", lambda _self: "sway"):
            code, out, err, _s = self.run_cli("--backend", "sway", "--output", "screen", "--pos", "0x0")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(support.RUN_COMMAND, [mt for mt, _p in srv.requests])
        line = self.one_line(err)
        self.assertIn("compositor rejected `output screen position 0 0`", line)
        self.assertIn("Expected one of these tokens", line)

    def test_a_disconnected_sway_head_is_still_listed(self):
        """The drop is gated on the dialect as well as on the shape, which the plan did not ask for: on sway
        an output with `active: false` and no `current_mode` is a real head with nothing plugged into it, and
        `xrandr --query` has to keep printing it as `disconnected` -- dropping it would make outputs vanish
        from the listing rather than show as unplugged.  Only i3's `xroot-0` is not a head."""
        dark = dict(support.SWAY_OUTPUTS[1], active=False, modes=[])
        dark.pop("current_mode", None)
        self.sway("ok", outputs=[support.SWAY_OUTPUTS[0], dark])
        code, out, err = self.run_cli("--backend", "sway", "--query")[:3]
        self.assertEqual((code, err), (0, ""))
        self.assertIn(dark["name"], out)
        # the same shape on an i3 socket -- active false, no current mode, no modes -- is the pseudo-output
        # and is dropped, which is the only reason this row is interesting
        self.sway("ok", dialect="i3", outputs=[dict(dark, name="xroot-0")])
        out = self.run_cli("--backend", "sway", "--query")[1]
        self.assertNotIn("xroot-0", out)

    def test_an_apply_against_sway_still_applies(self):
        """The refusal is dialect-keyed and nothing else: sway takes the same command."""
        srv = self.sway("ok")
        code, _out, err, _s = self.run_cli("--backend", "sway", "--output",
                                           "HEADLESS-2", "--pos", "0x720")
        self.assertEqual((code, err), (0, ""))
        self.assertIn(support.RUN_COMMAND, [mt for mt, _p in srv.requests])


if __name__ == "__main__":
    unittest.main()
