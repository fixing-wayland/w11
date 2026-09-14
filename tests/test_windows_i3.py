#!/usr/bin/env python3
"""The sway backend's i3 dialect, against the recordings of a live i3 4.25.1.

i3 speaks the same i3-ipc protocol sway does, so `W11_PASSTHROUGH=never` (and `wxrandr --backend
sway`) land on `wdotool/backend_sway.py` on an i3 box -- the default path there is the X11 handover and is
right [M recon2/i3.md §2a].  Forced onto our own code it half-worked and half-lied, and this file is the four
lies, one class each [M recon2/i3.md §2b, §2c]:

* ids: a con id is a pointer (47 bits, `97479943571072`), every X-shaped consumer takes the low 32
  (`0x5168c680`), and `wxprop -id` answered `BadWindow` on it;
* floating: i3 wraps a floating view in a `floating_con`, the walk reset the flag on the way down, and
  `windowmove` refused every window with "cannot move a tiled window";
* pid: i3's tree carries none at all, so `getwindowpid` failed on every window;
* visible: i3's tree carries none either, so `search --onlyvisible` matched nothing.

Doubles only: `support.FakeSway(dialect="i3")` replays `tests/fixtures/i3/*.json` (the tree, the outputs with
their `xroot-0` pseudo-output, the workspaces, GET_VERSION major 4) and `test_wxprop_x11.FakeXServerExt` is the X
plane beside it -- i3 is X11, which is the whole reason the ids and the pids can be had at all."""

import contextlib
import copy
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` / `import test_wwmctl_x11` resolve only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

import support
from test_wxprop_x11 import FakeXServerExt
from wdotool import cli
from hacks.window import x11_mini
from hacks.window.backend_sway import SwayBackend
from wdotool.ctx import Context

#: the w11smoke xterm, as the live i3 reported it [M recon2/i3.md §1, fixtures/i3/get_tree_floating.json]
CON_ID = 97479943571072                 # i3's own node id: a pointer, 47 bits wide
X_ID = 8388621                          # the same window's X id, which is what `wwmctl -l` printed
#: what `wxprop -id 97479943571072` actually asked the X server for, and the id in the BadWindow it got back
TRUNCATED = CON_ID & 0xFFFFFFFF         # 0x5168c680
#: i3bar's dock window, the other view in the same recording
BAR_X_ID = 6291462
#: the workspace the recorded GET_WORKSPACES calls visible and focused
VISIBLE_WS = "2"


def tree_with_w11smoke_on_ws2(floating: bool):
    """The recorded tree with the w11smoke con moved onto workspace 2, floating or tiled.

    The recording caught that xterm in the scratchpad (`__i3_scratch`), which is where §2c isolated the
    floating bug; a window on a live workspace is the ordinary case and the only place the derived `visible`
    can be read at all.  The con itself is the recorded one, byte for byte -- only its parent changes, and
    the `floating_con` wrapper travels with it when `floating` is asked for, because that wrapper is exactly
    what i3 puts around a floated view."""
    tree = support.fixture_json("i3", "get_tree_floating.json")
    scratch = _find(tree, lambda n: n.get("name") == "__i3_scratch")
    wrapper = scratch["floating_nodes"].pop()
    ws2 = _find(tree, lambda n: n.get("type") == "workspace" and n.get("name") == VISIBLE_WS)
    if floating:
        ws2.setdefault("floating_nodes", []).append(wrapper)
    else:
        ws2.setdefault("nodes", []).append(wrapper["nodes"][0])
    return tree


#: a second workspace, off the same output, that GET_WORKSPACES does not call visible
HIDDEN_WS = "3"
#: the copy of the w11smoke xterm that sits on it
HIDDEN_X_ID = 8388640
HIDDEN_CON_ID = 97479943571073


def tree_with_a_hidden_workspace():
    """(tree, workspaces) for two workspaces on one output: `2` visible, `3` not, one xterm on each.

    i3 shows one workspace per output at a time and keeps the others whole, so this is the ordinary two-
    workspace desktop -- and the only shape that tells "on a workspace GET_WORKSPACES calls visible" apart
    from "on a workspace at all", which is what the one-workspace recording cannot do.  The second xterm is
    the recorded con with new ids, so the two rows differ in nothing but where they hang."""
    tree = tree_with_w11smoke_on_ws2(floating=False)
    content = _find(tree, lambda n: n.get("type") == "con" and n.get("name") == "content"
                    and any(c.get("num") == 2 for c in n.get("nodes") or []))
    ws2 = _find(tree, lambda n: n.get("type") == "workspace" and n.get("name") == VISIBLE_WS)
    hidden = copy.deepcopy(ws2)
    hidden.update(id=97479943571649, num=int(HIDDEN_WS), name=HIDDEN_WS, focused=False)
    con = _find(hidden, lambda n: n.get("window") == X_ID)
    con.update(id=HIDDEN_CON_ID, window=HIDDEN_X_ID, focused=False)
    content["nodes"].append(hidden)
    live = support.fixture_json("i3", "get_workspaces.json")
    shape = copy.deepcopy(live[0])
    shape.update(id=hidden["id"], num=int(HIDDEN_WS), name=HIDDEN_WS, visible=False, focused=False)
    return tree, live + [shape]


def _find(node, pred):
    if pred(node):
        return node
    for child in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
        hit = _find(child, pred)
        if hit is not None:
            return hit
    return None


class I3Base(unittest.TestCase):
    """One FakeSway per test, answering as i3 4.25.1, plus the real backend on top of it."""

    def backend(self, **kw):
        srv = support.FakeSway("ok", dialect="i3", **kw)
        self.addCleanup(srv.close)
        self.srv = srv
        b = SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        self.addCleanup(self._close_x_plane, b)
        return b

    @staticmethod
    def _close_x_plane(b):
        """The i3 dialect opens an X connection for the pids and keeps it for the life of the backend (as
        the KWin backend does); a test that leaves it to the collector reports it as a ResourceWarning."""
        x = getattr(b, "_x", None)
        if x not in (None, "unset"):
            x.close()

    def commands(self):
        """Every RUN_COMMAND payload the double was sent, in order."""
        return [body for mtype, body in self.srv.requests if mtype == support.RUN_COMMAND]

    def run_chain(self, backend, argv):
        ctx = Context()
        ctx._backend = backend
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.run_chain(ctx, "wdotool", argv)
        return (rc if rc else ctx.exit_code), out.getvalue(), err.getvalue()


class XPlaneBase(I3Base):
    """...and an X server beside it, because i3 is X11 and half the answers live there."""

    DISPLAY = ":77"

    def setUp(self):
        self.xdir = tempfile.mkdtemp(prefix="i3-xplane-")
        self.addCleanup(shutil.rmtree, self.xdir, ignore_errors=True)
        p = mock.patch.object(x11_mini, "_SOCK_DIR", self.xdir)
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {
            "DISPLAY": self.DISPLAY,
            # never the developer's own cookie: the fake server accepts an empty one
            "XAUTHORITY": os.path.join(self.xdir, "no-such-authority")})
        env.start()
        self.addCleanup(env.stop)
        self.x = FakeXServerExt(self.xdir, num=int(self.DISPLAY[1:]))
        self.addCleanup(self.x.stop)
        self.x.set_prop(X_ID, "WM_CLASS", "STRING", 8, b"xterm\x00XTerm\x00")
        self.x.set_prop(X_ID, "_NET_WM_PID", "CARDINAL", 32, (54187).to_bytes(4, "little"))
        self.x.children = [X_ID]
        # the landmine: if the truncation ever comes back, the X server answers it the way the live one did
        self.x.error_windows.add(TRUNCATED)


class Ids(XPlaneBase):
    """U01: a con id above 2^32 never reaches an X consumer.

    Closes recon2/i3.md §2b: `wdotool search` printed `97479943571072` where the real xdotool printed
    `8388621`, and `wxprop -id` on that number sent `0x5168c680` to the X server and got BadWindow."""

    def test_the_ids_handed_out_are_the_x_ids(self):
        b = self.backend()
        ids = sorted(w.id for w in b.list())
        self.assertEqual(ids, sorted([X_ID, BAR_X_ID]))
        self.assertNotIn(CON_ID, ids)
        for wid in ids:
            self.assertLess(wid, 2 ** 32, "an id no X consumer can carry")

    def test_search_prints_what_the_real_xdotool_prints(self):
        b = self.backend()
        rc, out, err = self.run_chain(b, ["search", "--class", "XTerm"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "%d\n" % X_ID)

    def test_wwmctl_lists_the_window_under_its_x_id(self):
        """`wwmctl -l` already read `node["window"]` for the printed column, so this pins that the id
        column and the id every other tool hands out are now the same number."""
        from wwmctl import cli as wwmctl_cli
        from hacks.window import wmctl as wwmctl_core
        b = self.backend()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wwmctl_core, "_detect_backend", lambda: b), \
                mock.patch.object(wwmctl_core, "_x11_connect", lambda: None), \
                mock.patch.object(wwmctl_core, "hostname", lambda: "testhost"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wwmctl_cli.main(["-l"])
        self.assertEqual((rc, err.getvalue()), (0, ""))
        self.assertIn("0x0080000d", out.getvalue())
        self.assertNotIn("%d" % CON_ID, out.getvalue())

    def test_wxprop_reaches_the_x_plane_with_that_id(self):
        b = self.backend()
        printed = self.run_chain(b, ["search", "--class", "XTerm"])[1].strip()
        code, out, err = self.wxprop("-id", printed, "WM_CLASS")
        self.assertEqual(code, 0, err)
        self.assertNotIn("BadWindow", err)
        self.assertEqual(out, b'WM_CLASS(STRING) = "xterm", "XTerm"\n')

    def test_the_con_id_is_the_measured_badwindow(self):
        """The other half of the same claim, so the test above cannot pass by accident: hand `wxprop -id`
        the con id and the X server answers exactly what the live one answered.

        Not a tautology of the double: the only id the server refuses is `TRUNCATED`, and nothing in the
        argument list is that number.  Reaching the refusal at all is the product computing
        `97479943571072 & 0xFFFFFFFF` on the way to the wire, and the id in the message is what it put
        there."""
        code, _out, err = self.wxprop("-id", str(CON_ID), "WM_CLASS")
        self.assertEqual(code, 1)
        self.assertIn("BadWindow", err)
        self.assertIn("Resource id in failed request:  0x5168c680", err)

    def test_a_con_id_with_another_low_word_is_a_different_request(self):
        """The control for the control: the refusal is keyed on the truncated value and not on "big number".
        One bit further up leaves the low 32 alone -- still 0x5168c680, still refused -- and one bit lower
        changes them, and that request is not the one the server is armed against."""
        code, _out, err = self.wxprop("-id", str(CON_ID + 2 ** 40), "WM_CLASS")
        self.assertEqual((code, "Resource id in failed request:  0x5168c680" in err), (1, True))
        code, _out, err = self.wxprop("-id", str(CON_ID + 1), "WM_CLASS")
        self.assertNotIn("BadWindow", err)

    def wxprop(self, *argv):
        from wxprop import cli as wxprop_cli
        from hacks.property import core as wxprop_core

        class Cap:
            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, s):
                self.buffer.write(s.encode())

            def flush(self):
                pass

        real_connect = wxprop_core._x11_connect

        def connect_and_remember(*a, **kw):
            """wxprop leaves its X connection to process exit, which is right for a one-shot tool and wrong
            inside a suite: the socket is finalized whenever the collector gets to it, and the
            ResourceWarning then lands on stderr in whatever test is running at that moment (it failed
            `test_getwindowpid_answers_off_the_x_plane` roughly one run in two).  Closed here instead, so
            every `err` in this class is the tool's own output."""
            conn = real_connect(*a, **kw)
            if conn is not None:
                self.addCleanup(conn.close)
            return conn

        out, err = Cap(), io.StringIO()
        with mock.patch.object(wxprop_core, "_detect_backend", lambda: None), \
                mock.patch.object(wxprop_core, "_x11_connect", connect_and_remember), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", err):
            code = wxprop_cli.main(list(argv))
        return code, out.buffer.getvalue(), err.getvalue()


class Floating(I3Base):
    """U02: the `floating_con -> con` view is floating, and moves.

    Closes recon2/i3.md §2c: `_nodes()` reset the flag when it walked into the wrapper, so every floating i3
    window read as tiled and `windowmove` answered "cannot move a tiled window to an absolute position",
    while `i3-msg '[con_id=N] move absolute position 100 100'` did it."""

    def test_the_wrapped_view_is_reported_floating(self):
        b = self.backend()
        rows = {win.id: floating for _n, win, floating, _ws in b._nodes()}
        self.assertTrue(rows[X_ID])
        self.assertFalse(rows[BAR_X_ID])            # i3bar is a dock, tiled in a dockarea

    def test_windowmove_sends_i3s_own_command(self):
        b = self.backend()
        rc, out, err = self.run_chain(b, ["windowmove", str(X_ID), "100", "100"])
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertEqual(self.commands(),
                         ["[con_id=%d] move absolute position 100 100" % CON_ID])

    def test_the_command_names_the_con_id_and_never_the_x_id(self):
        """The two halves of the id split, in one assertion: the user says 8388621, i3 hears
        97479943571072, and `[con_id=8388621]` would match nothing at all."""
        b = self.backend()
        self.run_chain(b, ["windowmove", str(X_ID), "100", "100"])
        self.assertNotIn("[con_id=%d]" % X_ID, "".join(self.commands()))

    def test_a_tiled_view_is_still_refused(self):
        """The refusal is right when the window really is tiled -- the fix is a predicate, not its removal."""
        b = self.backend(tree=tree_with_w11smoke_on_ws2(floating=False))
        rc, _out, err = self.run_chain(b, ["windowmove", str(X_ID), "100", "100"])
        self.assertEqual(rc, 0)                     # SoftCmdError: a warning, not a failed chain
        self.assertIn("cannot move a tiled window", err)
        self.assertEqual(self.commands(), [])

    def test_the_refusal_opens_with_i3_and_not_with_sway(self):
        """`dialect()` answers i3 everywhere else in backend_sway.py -- the label, the ids, the floating
        flag, the pids -- and the two tiled refusals and the fullscreen one were the last strings that did
        not.  Measured verbatim on the first live resolute-i3 run, 2026-09-09:

            $ W11_PASSTHROUGH=never env -u I3SOCK wdotool windowsize 10485774 400 300
            sway: cannot resize a tiled window to an absolute size (floating enable it first)

        which names a program the user is not running [requests-batch-9.md, from batch 16].  Asserted on
        the PREFIX in both directions, because `vm/live-smoke.d/i3.sh:103` and `hypr.sh:155` match the
        middle of the same sentence and neither would notice the word in front of it."""
        b = self.backend(tree=tree_with_w11smoke_on_ws2(floating=False))
        _rc, _out, err = self.run_chain(b, ["windowmove", str(X_ID), "100", "100"])
        self.assertIn("i3: cannot move a tiled window", err)
        self.assertNotIn("sway:", err)
        b2 = self.backend(tree=tree_with_w11smoke_on_ws2(floating=False))
        _rc, _out, err2 = self.run_chain(b2, ["windowsize", str(X_ID), "400", "300"])
        self.assertIn("i3: cannot resize a tiled window", err2)
        self.assertNotIn("sway:", err2)
        # and the third of the three, which is why `_refuse_fullscreen` stopped being a staticmethod
        tree = tree_with_w11smoke_on_ws2(floating=True)
        _find(tree, lambda n: n.get("window") == X_ID)["fullscreen_mode"] = 1
        b3 = self.backend(tree=tree)
        _rc, _out, err3 = self.run_chain(b3, ["windowmove", str(X_ID), "100", "100"])
        self.assertIn("i3: cannot move a fullscreen window", err3)
        self.assertNotIn("sway:", err3)

    def test_i3s_own_floating_field_is_read(self):
        """i3 also states it on the con (`user_on` / `auto_on` / `user_off` / `auto_off`, a field sway does
        not send), and a tree fetched without the wrapper -- or a future i3 that drops it -- still answers."""
        tree = tree_with_w11smoke_on_ws2(floating=False)
        _find(tree, lambda n: n.get("window") == X_ID)["floating"] = "user_on"
        b = self.backend(tree=tree)
        rows = {win.id: floating for _n, win, floating, _ws in b._nodes()}
        self.assertTrue(rows[X_ID])


class PidAndVisible(XPlaneBase):
    """U03: the two fields i3's tree does not have at all.

    Closes recon2/i3.md §2b: `getwindowpid` was `window 97479943571072 has no pid associated with it` on
    every window, and `search --onlyvisible` returned nothing with rc 1."""

    def test_getwindowpid_answers_off_the_x_plane(self):
        b = self.backend(tree=tree_with_w11smoke_on_ws2(floating=True))
        rc, out, err = self.run_chain(b, ["getwindowpid", str(X_ID)])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "54187\n")

    def test_no_x_plane_leaves_the_pid_where_it_was(self):
        """One unreachable X server is not a failed listing: the pid goes back to 0 and everything else in
        the row still answers."""
        self.x.stop()
        b = self.backend(tree=tree_with_w11smoke_on_ws2(floating=True))
        rows = {w.id: w for w in b.list()}
        self.assertEqual(rows[X_ID].pid, 0)
        self.assertEqual(rows[X_ID].title, "w11smoke")

    def test_a_view_on_the_visible_workspace_is_visible(self):
        b = self.backend(tree=tree_with_w11smoke_on_ws2(floating=True))
        rows = {w.id: w for w in b.list()}
        self.assertTrue(rows[X_ID].visible)
        # i3bar hangs off a dockarea, outside every workspace, so it is not on a visible one
        self.assertFalse(rows[BAR_X_ID].visible)

    def test_onlyvisible_matches_it(self):
        b = self.backend(tree=tree_with_w11smoke_on_ws2(floating=True))
        rc, out, err = self.run_chain(b, ["search", "--onlyvisible", "--class", "XTerm"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "%d\n" % X_ID)

    def test_a_view_on_a_workspace_that_is_not_shown_is_not_visible(self):
        """The claim is `visible` in GET_WORKSPACES, not "on a workspace": i3 keeps every workspace in the
        tree whole and shows one per output, so the xterm on workspace 3 is as absent from the screen as the
        stashed one below.  With the recorded one-workspace tree the two rules are indistinguishable --
        dropping the `visible` filter passes every other test in this file."""
        tree, workspaces = tree_with_a_hidden_workspace()
        b = self.backend(tree=tree, workspaces=workspaces)
        rows = {w.id: w for w in b.list()}
        self.assertTrue(rows[X_ID].visible)
        self.assertFalse(rows[HIDDEN_X_ID].visible)

    def test_onlyvisible_prints_only_the_shown_workspaces_window(self):
        """The same claim through the command that reads it: both xterms are XTerm and only one is on
        screen, so `--onlyvisible` is the difference between one id and two."""
        tree, workspaces = tree_with_a_hidden_workspace()
        b = self.backend(tree=tree, workspaces=workspaces)
        rc, out, err = self.run_chain(b, ["search", "--onlyvisible", "--class", "XTerm"])
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out, "%d\n" % X_ID)
        # the control: without the filter the tree really does hold both of them
        rc, out, _err = self.run_chain(b, ["search", "--class", "XTerm"])
        self.assertEqual((rc, sorted(int(x) for x in out.split())),
                         (0, sorted([X_ID, HIDDEN_X_ID])))

    def test_the_scratchpad_is_not_visible(self):
        """The recorded tree, unchanged: the xterm is in `__i3_scratch`, which GET_WORKSPACES never lists,
        so the same rule that makes a live workspace visible hides a stashed window."""
        b = self.backend()
        rows = {w.id: w for w in b.list()}
        self.assertFalse(rows[X_ID].visible)
        rc, out, _err = self.run_chain(b, ["search", "--onlyvisible", "--class", "XTerm"])
        self.assertEqual((rc, out), (1, ""))


class Events(I3Base):
    """The window event stream speaks the same ids `list()` does.

    `select_window()` and `wxprop -root -spy` read `events()`, and an i3 event carries the container node --
    whose `id` is the con id.  Yielding that where every other command answers X ids would make the picker
    return a number no other tool accepts."""

    def test_an_event_carrying_the_window_id_yields_it(self):
        b = self.backend()
        self.assertEqual(b._event_wid({"id": CON_ID, "window": X_ID}), X_ID)

    def test_an_event_without_one_is_looked_up_in_the_tree(self):
        """A `close` event's container may no longer carry `window`; the tree still has the row."""
        b = self.backend()
        self.assertEqual(b._event_wid({"id": CON_ID}), X_ID)

    def test_an_id_the_tree_does_not_know_is_passed_through(self):
        """Better a con id than nothing: a window that is already gone is still an event a caller may want."""
        b = self.backend()
        self.assertEqual(b._event_wid({"id": 4242}), 4242)

    def test_on_sway_the_id_is_the_node_id_untouched(self):
        srv = support.FakeSway("ok")
        self.addCleanup(srv.close)
        b = SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        self.assertEqual(b._event_wid({"id": 7, "window": 99}), 7)


class RefusalsCarryTheirRoute(I3Base):
    """AGENTS.md: a gap is NOT YET plus the rung that would close it, never the compositor's lack alone.

    XLowerWindow and `_NET_WM_STATE_SHADED` are both things the X11 toolbox has always done, so neither
    `windowlower` nor an unknown `windowstate` is allowed to stop at "sway cannot".  What must NOT change is
    the mechanics either sentence carries: `windowlower` still warns and exits 0 (docs/WDOTOOL.md's `warn
    and succeed` column, and the live check at tests/test_windows_sway.py:239), and the state refusal is
    still a plain failure that wwmctl's X-plane retry does not treat as a capability gap."""

    def test_windowlower_warns_with_a_route_and_still_succeeds(self):
        b = self.backend(tree=tree_with_w11smoke_on_ws2(floating=True))
        rc, out, err = self.run_chain(b, ["windowlower", str(X_ID)])
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("i3 has no lower command", err)
        self.assertIn("AGENTS.md route 6", err)
        self.assertIn("not yet", err)
        self.assertTrue(err.rstrip("\n").endswith("ignoring"), err)
        self.assertNotIn("sway", err)
        self.assertEqual(self.commands(), [], "a warning is not a command")

    def test_raising_a_tiled_window_names_the_route_in_the_dialect(self):
        b = self.backend(tree=tree_with_w11smoke_on_ws2(floating=False))
        rc, _out, err = self.run_chain(b, ["windowraise", str(X_ID)])
        self.assertEqual(rc, 0)
        self.assertIn("tiled i3 windows have no stacking order", err)
        self.assertIn("AGENTS.md route 6", err)
        self.assertEqual(self.commands(), [])

    def test_a_state_neither_dialect_has_says_what_would_close_it(self):
        """The one byte pin for this sentence in the suite: `FakeSwayBackend` in tests/test_wwmctl_cli.py
        carries the head clause only, on purpose, so there is no second copy to keep in step. Also the
        mechanics: an `unsupported` state refusal has always been a FAILURE (wwmctl's X-plane retry keys
        off the flag), and rewording it did not make it a warning."""
        b = self.backend(tree=tree_with_w11smoke_on_ws2(floating=True))
        rc, _out, err = self.run_chain(b, ["windowstate", "--add", "SHADED", str(X_ID)])
        self.assertNotEqual(rc, 0, "a state refusal is a failure and stays one")
        self.assertIn("windowstate SHADED is not supported by the sway backend: i3 has no such window "
                      "state; not yet here, and the route is a patched compositor (AGENTS.md route 6)",
                      err)
        self.assertEqual(self.commands(), [])


class Dialect(I3Base):
    """What decides all of the above: one GET_VERSION, and sway's answers are untouched by it."""

    def test_major_four_is_i3(self):
        b = self.backend()
        self.assertEqual(b.dialect(), "i3")
        self.assertEqual([m for m, _p in self.srv.requests], [support.GET_VERSION])

    def test_it_is_asked_once_per_backend(self):
        b = self.backend()
        for _ in range(3):
            b.list()
        self.assertEqual([m for m, _p in self.srv.requests].count(support.GET_VERSION), 1)

    def test_sway_is_still_sway(self):
        """sway 1.11 answers major 1, and every i3 arm above is off in that case: the ids are the con ids
        the sway backend has always handed out."""
        srv = support.FakeSway("ok")
        self.addCleanup(srv.close)
        b = SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        self.assertEqual(b.dialect(), "sway")

    def test_a_reply_that_is_not_a_version_reads_as_sway(self):
        """The double answers its workspace list to anything it does not special-case, which is exactly the
        shape of an IPC peer that is not i3: no `major`, so no i3 behaviour."""
        srv = support.FakeSway("ok", dialect="i3")
        srv.version = [{"num": 1}]
        self.addCleanup(srv.close)
        b = SwayBackend(sockpath=srv.path)
        self.addCleanup(b.sock.close)
        self.assertEqual(b.dialect(), "sway")


if __name__ == "__main__":
    unittest.main()
