#!/usr/bin/env python3
"""wxprop's merged root (`-root` with a views() backend and Xwayland up) on the
backends that are not GNOME.

`resolve_root` (hacks/property/core.py:894-900) builds a `MergedRootTarget` for
*any* backend whose `views()` answers, not only the GNOME bridge: the wlr floor
(labwc, river, Wayfire, Budgie, Xfce- and LXQt-Wayland), COSMIC through
`XPlaneViews`, and Cinnamon all take this path whenever an X plane is joinable.
tests/test_wxprop_gnome.py covers it on the one backend whose `events()` happens
to take a `workspaces` keyword and whose synthesis always has a workspace
manager behind it, so two things went unproven here and were both wrong:

  * `-root -spy` called the event hook as `hook(None, workspaces=True)` with no
    fallback, so on `WlrBackend.events(self, timeout=None)`
    (hacks/window/backend_wlr.py:383, inherited by COSMIC) and
    `CinnamonBackend.events` (backend_cinnamon.py:464) the pump thread died at
    the call and the tool exited 1 with `bridge event stream failed: ... got an
    unexpected keyword argument 'workspaces'` instead of spying;
  * the bare `-root` dump advertised all six `_ROOT_OVERRIDES` whether the
    synthesis produced them or not, so a wlr session with no
    `ext_workspace_manager_v1` (backend_wlr.py:760-764) printed
    `_NET_DESKTOP_NAMES:  not found.` for a name real xprop's
    XListProperties-driven dump could never have listed in the first place.

The backends here are stubs rather than the real classes on purpose: what is
under test is core.py's two decisions, and the signature that trips them is one
line of each backend.
"""

import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` / `import test_wxprop_cli` resolve only with the tests
# directory itself on sys.path: running this file by path puts it there for
# free, `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support
from test_wxprop_cli import _CapStdout
from hacks.property.fmt import FatalError
from hacks.window import backend
from hacks.property import core

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


class _FloorBackend:
    """The wlr floor as `NativeRootTarget._props` sees it: `views()` answers (so
    the session is a views() session and the root goes through the merge), but
    the listing is empty and `workspaces()` is None -- backend_wlr.py:760-764,
    `if self.ws is None: return None`, which is every wlroots compositor with no
    `ext_workspace_manager_v1` global."""

    name = "wlr"

    def views(self):
        return []

    def workspaces(self):
        return None

    def num_desktops(self):
        return 1

    def get_desktop(self):
        return 0


class _NamedWorkspaces(support.FakeBackend):
    """The control: a backend that does list views and does name its
    workspaces, so the synthesis really produces all six overrides."""

    def workspaces(self):
        return [backend.Workspace(index=0, name="one", active=True),
                backend.Workspace(index=1, name="two")]


class _Session(core.Session):
    """A Session with the two plane handles pinned: the backend is the stub, and
    x11() is never consulted by the synthesis (the X side of the merge is the
    XTarget stub below)."""

    def __init__(self, b):
        super().__init__()
        self.b = b

    def backend(self):
        return self.b

    def x11(self):
        return None


class _XTarget:
    """The X root as this merge sees it: Mutter/xwm names `_NET_SUPPORTED` and
    nothing this test cares about (docs/XW11.md:326 measured wlroots' Xwayland
    root at 19 atoms, not one of them a desktop atom)."""

    plane = "x"
    conn = None
    win = 1

    def list_names(self):
        return [b"_NET_SUPPORTED"]

    def intern(self, name, create):
        return True

    def fetch(self, name):
        return None

    def atom_name(self, a):
        return None


def _merged(b):
    return core.MergedRootTarget(_XTarget(),
                                 core.NativeRootTarget(_Session(b), core.NativeAtoms()))


class ListNamesTests(unittest.TestCase):
    """The bare dump lists a name only if it can then print a value for it."""

    def test_list_names_advertises_only_what_the_synthesis_produces(self):
        m = _merged(_FloorBackend())
        names = m.list_names()
        # no workspace manager -> no names to publish; an empty listing is not
        # "rich", so there is no stacking order either (core.py:634, 643, 685)
        self.assertNotIn(b"_NET_DESKTOP_NAMES", names)
        self.assertNotIn(b"_NET_CLIENT_LIST_STACKING", names)
        for n in (b"_NET_CLIENT_LIST", b"_NET_ACTIVE_WINDOW",
                  b"_NET_NUMBER_OF_DESKTOPS", b"_NET_CURRENT_DESKTOP"):
            self.assertIn(n, names)
        # the point of the whole exercise: nothing listed fetches None, so
        # show_prop (core.py:996-998) cannot write ":  not found." for it
        for n in names:
            if n in core._ROOT_OVERRIDES:
                self.assertIsNotNone(m.fetch(n), "%s listed but not fetchable" % n)

    def test_a_backend_that_names_workspaces_and_lists_views_publishes_both(self):
        win = support.fake_window(11, title="T", class_="foot", instance="foot",
                                  pid=1, x=0, y=0, w=10, h=10, visible=True,
                                  focused=True, desktop=0)
        view = support.fake_view(win, xid=0, app_id="foot", instance="foot", cls="foot")
        m = _merged(_NamedWorkspaces(windows=[win], views=[view]))
        names = m.list_names()
        for n in core._ROOT_OVERRIDES:
            self.assertIn(n, names)
            self.assertIsNotNone(m.fetch(n))

    def test_a_written_override_is_still_read_from_the_x_root(self):
        """The `_written` half of the guard is unchanged: after a -set the name
        belongs to the X root, whatever the synthesis has (core.py:734-738)."""
        m = _merged(_FloorBackend())
        m._written.add(b"_NET_CURRENT_DESKTOP")
        self.assertNotIn(b"_NET_CURRENT_DESKTOP", m.list_names())


class _EventBackend:
    """A backend whose `events()` has the wlr/cosmic/cinnamon signature: one
    positional timeout, no `workspaces` keyword."""

    name = "wlr"

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def events(self, timeout=None):
        self.calls.append(timeout)
        if self.exc is not None:
            raise self.exc
        return iter(())


class _XConn:
    """Enough X connection for spy_merged_root's poll loop: `next_event` answers
    None until `fail_after` calls have gone by and then interrupts, which is how
    the real tool leaves -spy (SIGINT -> 130)."""

    def __init__(self, fail_after=3):
        self.fail_after = fail_after
        self.calls = 0
        self.selected = []

    def select_input(self, win, mask):
        self.selected.append((win, mask))

    def next_event(self, timeout):
        time.sleep(0.05)
        self.calls += 1
        if self.calls >= self.fail_after:
            raise KeyboardInterrupt
        return None

    def get_atom_name(self, a):
        return None


class _MergedStub:
    """The two attributes spy_merged_root reads off the target, plus the backend
    behind `native.sess`."""

    def __init__(self, conn, b):
        self.conn = conn
        self.win = 1
        self.native = type("N", (), {"sess": type("S", (), {"backend": lambda _s: b})()})()


class SpyTests(unittest.TestCase):
    """-root -spy on a backend whose event hook takes no `workspaces` flag."""

    def setUp(self):
        self.out = _CapStdout()
        real, sys.stdout = sys.stdout, self.out
        self.addCleanup(setattr, sys, "stdout", real)

    def test_spy_merged_root_falls_back_to_a_hook_without_the_workspaces_flag(self):
        b = _EventBackend()
        t = _MergedStub(_XConn(), b)
        with self.assertRaises(KeyboardInterrupt):
            core.spy_merged_root(None, t, None)
        time.sleep(0.1)  # the pump runs on a thread of its own
        # one positional call and no second kind of failure: the keyword call
        # raised TypeError before the body ran, so it left no record
        self.assertEqual(b.calls, [None])
        self.assertEqual(t.conn.selected, [(1, core.SPY_EVENT_MASK)])

    def test_a_failing_stream_is_still_fatal(self):
        b = _EventBackend(exc=RuntimeError("boom"))
        t = _MergedStub(_XConn(fail_after=20), b)
        with self.assertRaises(FatalError) as cm:
            core.spy_merged_root(None, t, None)
        self.assertEqual(str(cm.exception), "bridge event stream failed: boom")


if __name__ == "__main__":
    unittest.main()
