#!/usr/bin/env python3
"""The registry: who gets a shadow id, which id, and what two lists differ by.

Three measurements decide this file.

* **The id range is the server's own promise.** `rid_base | (n & rid_mask)`,
  mask 0x1fffff = 21 bits, bases stepping by mask + 1 in connection order
  [recon/wire.md 7.1]. The server allocated that range exclusively to the
  proxy's own connection, so a shadow cannot collide with a real Xwayland
  window and `BadIDChoice` for one is impossible. Exhaustion is 2^21 windows,
  which is 24 days at one window a second -- R7 is the row and `Exhausted`
  below is the answer.
* **The X plane's four internals stay unpaired.** With no managed X window at
  all, sway's root has no `_NET_CLIENT_LIST` and `X11Conn.client_list()` falls
  back to `QueryTree`, which answers `[2097153, 2097154, 2097155, 2097156]` --
  wlroots' own xwm windows, with empty WM_CLASS and no pid [recon/seams.md 3].
  `match_xids`' "must agree on something" rule keeps them out of every pairing,
  measured, and `EmptyXPlane` pins that the proxy does not undo it.
* **sway's list is not a stacking order.** `_NET_CLIENT_LIST_STACKING` is a copy
  of `_NET_CLIENT_LIST` because no backend in the tree reports one (R13). The
  copy is pinned so that a backend which starts reporting stacking changes a
  test rather than sliding a wrong order past.
"""

import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                      # noqa: E402
from support import (FakeBackend, FakeUpstream, ProxyRig, _recvn,   # noqa: E402
                     fake_view, fake_window)
from wdotool import backend_detect, x11_mini                        # noqa: E402
from wdotool.ctx import NoSessionError                              # noqa: E402
from xw11 import policy, wire                                       # noqa: E402
from xw11 import shadow as shadow_mod                               # noqa: E402
from xw11 import upstream as upstream_mod                           # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

#: The four wlroots xwm internals a bare X plane answers with
#: [recon/seams.md 3, measured on this box with zero X clients].
XWM_INTERNALS = [2097153, 2097154, 2097155, 2097156]


class LockSpy:
    """The server's `block`, with a record of whether it was held while
    something else ran. `acquire(blocking=False)` from a test cannot answer
    "was the lock held there" without taking it away when the answer is no."""

    def __init__(self):
        self.lock = threading.Lock()
        self.taken = 0
        self.held = False

    def __enter__(self):
        self.lock.acquire()
        self.taken += 1
        self.held = True
        return self

    def __exit__(self, *_exc):
        self.held = False
        self.lock.release()


class RegistryCase(unittest.TestCase):
    """A real `OwnConn` in front of a `FakeUpstream`, because the ids under test
    come out of the setup reply and nowhere else. A double that minted them
    would be the thing being measured."""

    def setUp(self):
        self.lines = []

    def own(self, num=51):
        d = tempfile.mkdtemp(prefix="xw11shadow-")
        self.addCleanup(shutil.rmtree, d, True)
        old = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = d
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", old)
        up = FakeUpstream(d, num=num)
        self.addCleanup(up.stop)
        self.upstream = up
        conn = upstream_mod.OwnConn(":%d" % num, log=self.lines.append)
        self.addCleanup(conn.close)
        self.assertTrue(conn.connect())
        return conn

    def registry(self, backend, own=None, **kw):
        return shadow_mod.Shadows(own or self.own(), backend,
                                  log=self.lines.append, **kw)

    def natives(self, *ids):
        wins = [fake_window(i, "w%d" % i) for i in ids]
        return FakeBackend(windows=wins,
                           views=[fake_view(w) for w in wins])


class Mint(RegistryCase):
    def test_an_id_is_the_base_or_n_with_n_from_one(self):
        """`x11_mini._new_rid`'s allocator, reused rather than written a second
        time [recon/wire.md 7.1]: never the bare base, one id per handle."""
        own = self.own()
        shadows = self.registry(self.natives(1, 2, 3), own)
        got = [e.shadow for e in shadows.snapshot()]
        self.assertEqual(got, [own.rid_base | 1, own.rid_base | 2,
                               own.rid_base | 3])
        self.assertNotIn(own.rid_base, got)
        for xid in got:
            self.assertEqual(xid & ~own.rid_mask, own.rid_base)

    def test_the_base_is_the_one_the_server_handed_this_connection(self):
        own = self.own()
        self.assertEqual(own.rid_mask, support.RID_MASK)
        self.assertEqual(own.rid_base % (own.rid_mask + 1), 0)

    def test_one_id_per_handle_for_the_life_of_the_proxy(self):
        """A window that is merely re-listed keeps its id: `W=$(xdotool search
        ...)` has to still name it on the next invocation."""
        shadows = self.registry(self.natives(1, 2))
        first = {e.handle: e.shadow for e in shadows.snapshot()}
        shadows.invalidate()
        second = {e.handle: e.shadow for e in shadows.snapshot()}
        self.assertEqual(first, second)


class NeverReused(RegistryCase):
    def test_a_window_that_leaves_does_not_hand_its_id_to_the_next_one(self):
        """No free list (design section 4.3): 2^21 ids is 24 days at one window
        a second [recon/wire.md 7.1] and the proxy idles out fifteen minutes
        after its last client, so an id that named a window never names another
        one in any session that can exist."""
        backend = self.natives(1, 2)
        shadows = self.registry(backend)
        first = {e.handle: e.shadow for e in shadows.snapshot()}
        backend.close(1)
        shadows.invalidate()
        shadows.snapshot()
        new = fake_window(3, "w3")
        backend.windows.append(new)
        backend.views_.append(fake_view(new))
        shadows.invalidate()
        after = {e.handle: e.shadow for e in shadows.snapshot()}
        self.assertNotIn(1, after)
        self.assertEqual(after[2], first[2])
        self.assertNotIn(after[3], first.values())


class Exhausted(RegistryCase):
    """R7."""

    def test_the_mint_after_the_last_id_refuses_and_logs_exactly_once(self):
        own = self.own()
        own._rid_mask = 0x3                     # four ids, one of them the base
        shadows = self.registry(self.natives(1, 2, 3, 4), own)
        got = [e.shadow for e in shadows.snapshot()]
        self.assertEqual(got[:3], [own.rid_base | 1, own.rid_base | 2,
                                   own.rid_base | 3])
        self.assertEqual(got[3], 0)
        self.assertTrue(shadows.exhausted)
        self.assertEqual(shadows.mint(), 0)
        spent = [line for line in self.lines if "shadow ids are spent" in line]
        self.assertEqual(len(spent), 1)
        self.assertIn("not yet", spent[0])
        self.assertIn("AGENTS.md route 5", spent[0])

    def test_a_mint_of_zero_is_what_a_read_handler_turns_into_bad_alloc(self):
        """The seam, in the shape batch 3 will use it -- the `BadAlloc` bytes
        below are the test's own handler, and what the registry contributes is
        the 0 after the wrap and the stream order it comes back in. Batch 3 owns
        the real handler."""
        backend = FakeBackend(windows=[fake_window(i) for i in (1, 2, 3, 4)])
        rig = ProxyRig(num=52, upstream_num=53, passthrough=False,
                       backend=backend)
        self.addCleanup(rig.stop)

        def handler(server, conn, request):
            if server.shadows.mint() == 0:
                return wire.error(wire.ERR_ALLOC, conn.seq, 0, request.opcode, 0)
            return wire.reply(conn.seq, 0, b"\0" * 24)
        old = policy.POLICY[wire.OP_QUERY_TREE]
        self.addCleanup(policy.POLICY.__setitem__, wire.OP_QUERY_TREE, old)
        policy.POLICY[wire.OP_QUERY_TREE] = policy.Row(root=policy.ANSWER)
        rig.server.handlers[wire.OP_QUERY_TREE] = handler
        sock, _body = rig.raw()
        own = rig.wait_own()
        own._rid_mask = 0x3
        own._rid_next = 2                       # one id left, then the wrap
        root = support.FakeXServer.ROOTS[0]
        req = struct.pack("<BBHI", wire.OP_QUERY_TREE, 0, 2, root)
        sock.sendall(req * 2)
        first = _recvn(sock, 32)
        second = _recvn(sock, 32)
        self.assertEqual(first[0], 1)                       # the last id there was
        self.assertEqual(second[0], 0)                      # BadAlloc
        self.assertEqual(second[1], wire.ERR_ALLOC)
        self.assertEqual(struct.unpack_from("<H", second, 2)[0], 2)
        sock.close()
    def test_a_rebase_un_latches_it_because_the_latch_belongs_to_one_base(self):
        """An Xwayland restart hands the next connection a whole 2^21 range
        [recon/wire.md 7.1], so the refusal cannot outlive the base that earned
        it: a registry that kept answering 0 would never mint again for the rest
        of the proxy's life."""
        own = self.own(num=57)
        own._rid_mask = 0x3
        shadows = self.registry(self.natives(1, 2, 3, 4), own)
        shadows.snapshot()
        self.assertTrue(shadows.exhausted)
        own._rid_mask, own._rid_next = 0x1FFFFF, 0      # the next connection's
        shadows.rebase()
        self.assertFalse(shadows.exhausted)
        self.assertEqual(shadows.mint(), own.rid_base | 1)

    def test_a_connection_that_is_gone_mints_nothing_and_latches_nothing(self):
        """`Server.close_own` rebases the registry, and a read from a handler in
        the same selector pass -- a client whose own twin's EOF has not been
        serviced yet -- reaches `mint()` with the socket already shut. 0 is the
        answer there, not an exception through the loop; and it is not
        exhaustion, so the reopened connection mints from its own base."""
        own = self.own(num=58)
        shadows = self.registry(self.natives(1), own)
        shadows.snapshot()
        own.close()
        self.assertFalse(own.open)
        with self.assertRaises(upstream_mod.UpstreamGone):
            own.new_xid()                               # what mint() guards
        self.assertEqual(shadows.mint(), 0)
        self.assertFalse(shadows.exhausted)



class Pairing(RegistryCase):
    def test_a_view_with_an_xid_gets_no_shadow_and_one_without_gets_one(self):
        """The proxy trusts `View.xid` and repeats no matching: sway's tree node
        carries `"window"` directly (measured `window=4194316` on a live xterm),
        and the five backends that have to guess have already run `match_xids`
        [recon/seams.md 3]."""
        native = fake_window(1, "native")
        xwin = fake_window(2, "xterm")
        backend = FakeBackend(windows=[native, xwin],
                              views=[fake_view(native),
                                     fake_view(xwin, xid=0x40000C)])
        shadows = self.registry(backend)
        entries = {e.handle: e for e in shadows.snapshot()}
        self.assertEqual(entries[2].shadow, 0)
        self.assertEqual(entries[2].xid, 0x40000C)
        self.assertEqual(entries[2].id, 0x40000C)
        self.assertTrue(entries[1].shadow)
        self.assertEqual(entries[1].xid, 0)
        self.assertTrue(shadows.is_shadow(entries[1].shadow))
        self.assertFalse(shadows.is_shadow(0x40000C))

    def test_a_pairing_that_settles_late_destroys_the_shadow(self):
        """A title that arrives after the window does pairs a toplevel the
        backend could not pair a moment ago. The shadow is destroyed and the
        real id takes its place in the root lists; the reverse never happens
        (design section 4.2)."""
        win = fake_window(1, "")
        backend = FakeBackend(windows=[win], views=[fake_view(win)])
        shadows = self.registry(backend)
        entry = shadows.snapshot()[0]
        shadow = entry.shadow
        self.assertTrue(shadow)
        backend.views_ = [fake_view(win, xid=0x40000C)]
        changes = shadows.refresh()
        kinds = [c.kind for c in changes]
        self.assertEqual(kinds, [shadow_mod.GONE, shadow_mod.NEW])
        self.assertEqual(changes[0].entry.shadow, shadow)
        self.assertEqual(changes[1].entry.xid, 0x40000C)
        self.assertFalse(shadows.is_shadow(shadow))
        self.assertEqual(shadows.client_list(), [0x40000C])


class EmptyXPlane(RegistryCase):
    """A box with zero managed X windows, which is every sway session until an
    X client starts. The X plane still holds wlroots' four xwm internals and
    `views()` still answers `xid=0` for every toplevel, because `match_xids`'
    "must agree on something" rule left all four unpaired [recon/seams.md 3]."""

    def bare(self, titles=(("foot", 1), ("nautilus", 2))):
        """The wlr floor: the compositor's toplevels, every view unpaired, and
        an X server whose whole tree is the four internals."""
        own = self.own()
        self.upstream.children = list(XWM_INTERNALS)
        wins = [fake_window(wid, title) for title, wid in titles]
        backend = FakeBackend(windows=wins,
                              views=[fake_view(w, xid=0) for w in wins])
        return own, backend, self.registry(backend, own=own)

    def test_the_four_xwm_internals_are_in_no_list_and_shadow_nothing(self):
        """Measured with zero X clients on this box: `client_list()` answers
        `[2097153..2097156]`, all with empty WM_CLASS and no pid
        [recon/seams.md 3]. What the proxy must not do is invent a window for
        one of those ids."""
        _own, _backend, shadows = self.bare()
        entries = shadows.snapshot()
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e.shadow and not e.xid for e in entries))
        listed = shadows.client_list()
        self.assertEqual(len(listed), 2)
        for internal in XWM_INTERNALS:
            self.assertNotIn(internal, listed)
            self.assertIsNone(shadows.entry_for(internal))
            self.assertFalse(shadows.is_shadow(internal))

    def test_and_the_proxy_repeats_none_of_the_walk_that_found_them(self):
        """The falsifiable half, and the reason the registry can be trusted to
        stay out of the X plane's way: those four ids came out of
        `X11Conn.client_list()`'s fallback -- `QueryTree` on the root and then
        `GetWindowAttributes`/`GetProperty` per child [recon/seams.md 3] -- and
        the proxy owns BOTH ends, so it asks the X server nothing at all about
        any window. Every id it lists is one the compositor reported.

        The fake answers `QueryTree` with exactly those four and writes down
        being asked (`support.FakeUpstream._dispatch`), so a registry that ever
        walked the tree fails here."""
        own, _backend, shadows = self.bare()
        before = len(self.upstream.log)
        shadows.snapshot()
        shadows.invalidate()
        shadows.snapshot()
        # The barrier, and the reason this is not a race: a server answers one
        # connection's requests in the order they arrived [recon/wire.md 3.2],
        # so by the time this round trip is back, anything the registry had put
        # on the wire is already in the fake's log. (An id nobody interned, so
        # the answer is a `BadAtom` and the table stays as the open left it.)
        self.assertIsNone(own.atom_name(0xDEAD))
        self.assertEqual([row[0] for row in self.upstream.log[before:]],
                         ["GetAtomName"])
        walked = [row for row in self.upstream.log
                  if row[0] in ("QueryTree", "GetWindowAttributes")]
        self.assertEqual(walked, [])
        # The one property read on the whole connection is step 5 of the open.
        asked = [row for row in self.upstream.log if row[0] == "GetProperty"]
        self.assertEqual([row[2] for row in asked], ["_NET_SUPPORTED"])
        self.assertEqual([row[1] for row in asked],
                         [support.FakeXServer.ROOTS[0]])


class Diff(RegistryCase):
    def setUp(self):
        super().setUp()
        self.a = fake_window(1, "a", x=0, y=0, w=100, h=50)
        self.b = fake_window(2, "b", x=10, y=10, w=100, h=50)
        self.backend = FakeBackend(windows=[self.a, self.b],
                                   views=[fake_view(self.a), fake_view(self.b)])
        self.shadows = self.registry(self.backend)
        self.shadows.snapshot()                 # the first list is all new

    def kinds(self):
        return [(c.kind, c.handle) for c in self.shadows.refresh()]

    def test_an_unchanged_list_produces_nothing(self):
        self.assertEqual(self.kinds(), [])

    def test_geometry(self):
        self.a.x, self.a.w = 20, 300
        self.assertEqual(self.kinds(), [(shadow_mod.GEOMETRY, 1)])

    def test_title(self):
        self.a.title = "a - edited"
        self.assertEqual(self.kinds(), [(shadow_mod.TITLE, 1)])

    def test_focus_moves_as_a_change_on_each_window(self):
        """Two changes, because design section 5.3 writes a `FocusOut` on the
        one that lost it and a `FocusIn` on the one that took it."""
        self.a.focused = True
        self.assertEqual(self.kinds(), [(shadow_mod.FOCUS, 1)])
        self.a.focused, self.b.focused = False, True
        self.assertEqual(self.kinds(), [(shadow_mod.FOCUS, 1),
                                        (shadow_mod.FOCUS, 2)])

    def test_visibility(self):
        self.b.visible = False
        self.assertEqual(self.kinds(), [(shadow_mod.VISIBLE, 2)])

    def test_order_without_membership(self):
        """A raise: the same windows in another order. sway's `list()` is bottom
        to top (backend.py:295)."""
        self.backend.raise_(1)
        self.assertEqual(self.kinds(), [(shadow_mod.ORDER, 0)])

    def test_membership(self):
        new = fake_window(3, "c")
        self.backend.windows.append(new)
        self.backend.views_.append(fake_view(new))
        self.assertEqual(self.kinds(), [(shadow_mod.NEW, 3)])
        self.backend.close(2)
        self.assertEqual(self.kinds(), [(shadow_mod.GONE, 2)])

    def test_a_state_change_names_the_field_that_moved(self):
        """Batch 5 maps the field onto a property atom through wxprop's own
        tables (`_NATIVE_EVENT_PROPS`, wxprop/core.py:1031)."""
        self.backend.views_[0] = fake_view(self.a, fullscreen=True)
        changes = self.shadows.refresh()
        self.assertEqual([c.kind for c in changes], [shadow_mod.PROPS])
        self.assertEqual(changes[0].names, ("fullscreen",))

    def test_a_title_change_drops_the_cached_props(self):
        """`props` is rebuilt on change (design section 4.1), and `_NET_WM_NAME`
        and `WM_NAME` ARE the title while `_NET_WM_STATE` carries FOCUSED and
        HIDDEN and `WM_STATE` is the visibility. A cache kept across a rename
        would serve batch 3's `build_props` the old name."""
        entry = {e.handle: e for e in self.shadows.snapshot()}[1]
        entry.props = {"WM_NAME": b"a"}
        self.a.title = "a - edited"
        self.assertEqual(self.kinds(), [(shadow_mod.TITLE, 1)])
        self.assertEqual(entry.props, {})

    def test_and_so_does_a_focus_change_or_a_visibility_one(self):
        entries = {e.handle: e for e in self.shadows.snapshot()}
        entries[1].props = {"_NET_WM_STATE": b""}
        entries[2].props = {"WM_STATE": b""}
        self.a.focused = True
        self.b.visible = False
        self.assertEqual(sorted(self.kinds()),
                         sorted([(shadow_mod.VISIBLE, 2), (shadow_mod.FOCUS, 1)]))
        self.assertEqual(entries[1].props, {})
        self.assertEqual(entries[2].props, {})

    def test_and_an_entry_nothing_happened_to_keeps_it(self):
        """The cache is worth having: two reads inside one 20 ms window build
        the property set once."""
        entry = {e.handle: e for e in self.shadows.snapshot()}[1]
        entry.props = {"WM_NAME": b"a"}
        self.assertEqual(self.kinds(), [])
        self.assertEqual(entry.props, {"WM_NAME": b"a"})

    def test_the_desktop_pair_is_a_change_of_its_own(self):
        self.backend.desktop_ = 2
        changes = self.shadows.refresh()
        self.assertEqual([c.kind for c in changes], [shadow_mod.DESKTOP])
        self.assertEqual(self.shadows.current_desktop, 2)
        self.assertEqual(self.shadows.num_desktops, 3)


class RootLists(RegistryCase):
    """R13."""

    def test_stacking_is_a_copy_of_the_client_list_in_list_order(self):
        """No backend in the tree reports a stacking order, so the two lists are
        the same list -- and this is where a backend that starts reporting one
        makes a test fail rather than a client read a wrong order (design
        section 4.6)."""
        wins = [fake_window(i, "w%d" % i) for i in (1, 2, 3)]
        backend = FakeBackend(windows=wins,
                              views=[fake_view(wins[0]),
                                     fake_view(wins[1], xid=0x40000C),
                                     fake_view(wins[2])])
        shadows = self.registry(backend)
        shadows.snapshot()
        listed = shadows.client_list()
        self.assertEqual(listed, shadows.stacking())
        self.assertEqual(listed[1], 0x40000C)
        self.assertEqual([e.id for e in shadows.snapshot()], listed)
        backend.raise_(1)
        shadows.invalidate()
        shadows.snapshot()
        self.assertEqual(shadows.client_list()[-1], listed[0])
        self.assertEqual(shadows.client_list(), shadows.stacking())

    def test_the_focused_entry_is_the_active_window(self):
        wins = [fake_window(1, "a"), fake_window(2, "b", focused=True)]
        backend = FakeBackend(windows=wins,
                              views=[fake_view(w) for w in wins])
        shadows = self.registry(backend)
        entries = shadows.snapshot()
        self.assertEqual(shadows.focused(), entries[1].id)
        wins[1].focused = False
        shadows.invalidate()
        shadows.snapshot()
        self.assertEqual(shadows.focused(), 0)


class Ttl(RegistryCase):
    def test_two_reads_inside_the_ttl_cost_one_list(self):
        """20 ms (design section 4.8): a `search` walk of five windows is served
        from one list, and `list()` is 0.08 ms on sway [recon/seams.md 2.3]."""
        backend = FakeBackend(windows=[fake_window(1)])
        shadows = self.registry(backend)
        shadows.snapshot()
        time.sleep(0.005)
        shadows.snapshot()
        self.assertEqual(backend.counts["list"], 1)

    def test_two_reads_past_the_ttl_cost_two(self):
        backend = FakeBackend(windows=[fake_window(1)])
        shadows = self.registry(backend)
        shadows.snapshot()
        time.sleep(0.030)
        shadows.snapshot()
        self.assertEqual(backend.counts["list"], 2)

    def test_an_invalidation_beats_the_clock(self):
        """What the pump does with a token: the next read re-lists whatever the
        clock says, because a read 75 ms behind a write is what two back-to-back
        invocations would see [recon/seams.md 2.3]."""
        backend = FakeBackend(windows=[fake_window(1)])
        shadows = self.registry(backend)
        shadows.snapshot()
        shadows.invalidate()
        shadows.snapshot()
        self.assertEqual(backend.counts["list"], 2)
        self.assertEqual(shadow_mod.TTL, 0.020)


class BackendTrouble(RegistryCase):
    def test_a_session_that_went_away_is_re_detected_once(self):
        """Nothing in the tree re-detects on its own [recon/seams.md 2.2], and a
        proxy is expected to outlive a compositor restart: it holds a display
        number, a lock and a client's connection, none of which the compositor
        going away invalidates."""
        gone = FakeBackend(windows=[fake_window(1)])
        gone.raise_on("list", NoSessionError("the compositor is gone"))
        fresh = FakeBackend(windows=[fake_window(9)])
        calls = {"reset": 0}
        old_reset, old_detect = backend_detect.reset, backend_detect.detect
        backend_detect.reset = lambda: calls.__setitem__("reset", calls["reset"] + 1)
        backend_detect.detect = lambda: fresh
        self.addCleanup(setattr, backend_detect, "reset", old_reset)
        self.addCleanup(setattr, backend_detect, "detect", old_detect)
        shadows = self.registry(gone)
        entries = shadows.snapshot()
        self.assertEqual(calls["reset"], 1)
        self.assertIs(shadows.backend, fresh)
        self.assertEqual([e.handle for e in entries], [9])

    def test_the_new_backend_is_handed_to_everyone_holding_the_old_one(self):
        """There is ONE backend in this process: the loop reads through this
        registry and the pump steps that same object's `events()`. A swap that
        moved only this reference would leave the pump calling a compositor that
        is gone."""
        gone = FakeBackend(windows=[fake_window(1)])
        gone.raise_on("list", NoSessionError("the compositor is gone"))
        fresh = FakeBackend(windows=[fake_window(9)])
        old_reset, old_detect = backend_detect.reset, backend_detect.detect
        backend_detect.reset = lambda: None
        backend_detect.detect = lambda: fresh
        self.addCleanup(setattr, backend_detect, "reset", old_reset)
        self.addCleanup(setattr, backend_detect, "detect", old_detect)
        told = []
        shadows = self.registry(gone, on_backend=told.append)
        shadows.snapshot()
        self.assertEqual(told, [fresh])
        self.assertIs(shadows.backend, fresh)

    def test_and_the_reset_and_the_hand_off_happen_under_the_servers_lock(self):
        """`backend_detect.reset()` closes the cached bus, the cached
        `ListNames` and the registry connection [recon/seams.md 2.2] -- the very
        things the pump's thread may be inside a call on."""
        gone = FakeBackend(windows=[fake_window(1)])
        gone.raise_on("list", NoSessionError("the compositor is gone"))
        fresh = FakeBackend(windows=[fake_window(9)])
        block = LockSpy()
        seen = []
        old_reset, old_detect = backend_detect.reset, backend_detect.detect
        backend_detect.reset = lambda: seen.append(("reset", block.held))
        backend_detect.detect = lambda: (seen.append(("detect", block.held)),
                                         fresh)[1]
        self.addCleanup(setattr, backend_detect, "reset", old_reset)
        self.addCleanup(setattr, backend_detect, "detect", old_detect)
        shadows = self.registry(gone, block=block,
                                on_backend=lambda b: seen.append(("told",
                                                                  block.held)))
        shadows.snapshot()
        self.assertEqual(seen, [("reset", True), ("detect", True),
                                ("told", True)])
        self.assertFalse(block.held)

    def test_a_backend_that_cannot_answer_is_a_log_line_and_not_an_error(self):
        """`CmdError` is this compositor not doing that thing. X gives a client
        no error when the window manager ignores a request, and neither does
        this (design section 3.3)."""
        from w11common.errors import CmdError
        backend = FakeBackend(windows=[fake_window(1)])
        backend.raise_on("get_desktop", CmdError("no workspaces here"))
        shadows = self.registry(backend)
        shadows.snapshot()
        self.assertEqual(shadows.current_desktop, -1)
        self.assertTrue([line for line in self.lines if "no workspaces here" in line])


class PassthroughWithoutSession(unittest.TestCase):
    def test_no_session_leaves_every_request_passing_and_logs_once(self):
        """A box with no Wayland session is not a refusal: the proxy forwards
        every byte and the client talks to Xwayland exactly as it would have
        without one. One line says the shadow side is off."""
        old = backend_detect.detect

        def refuse():
            raise NoSessionError("no compositor here")
        backend_detect.detect = refuse
        self.addCleanup(setattr, backend_detect, "detect", old)
        rig = ProxyRig(num=54, upstream_num=55, passthrough=False, backend=None)
        self.addCleanup(rig.stop)
        lines = []
        rig.server._log = type("W", (), {"write": lambda _s, t: lines.append(t)})()
        rig.upstream.children = [0x111]
        called = []
        oldrow = policy.POLICY[wire.OP_QUERY_TREE]
        self.addCleanup(policy.POLICY.__setitem__, wire.OP_QUERY_TREE, oldrow)
        policy.POLICY[wire.OP_QUERY_TREE] = policy.Row(other=policy.ANSWER,
                                                       root=policy.ANSWER)
        sock, _body = rig.raw()
        rig.wait(lambda: rig.server.passthrough, what="the pass-through decision")
        rig.server.handlers[wire.OP_QUERY_TREE] = lambda *a: called.append(a)
        root = support.FakeXServer.ROOTS[0]
        sock.sendall(struct.pack("<BBHI", wire.OP_QUERY_TREE, 0, 2, root))
        head = _recvn(sock, 32)
        (words,) = struct.unpack_from("<I", head, 4)
        body = _recvn(sock, 4 * words)
        self.assertEqual(struct.unpack("<I", body)[0], 0x111)   # the fake's own
        self.assertEqual(called, [])
        self.assertIsNone(rig.server.shadows)
        # And no connection of its own: with nothing to shadow there are no ids
        # to mint and no atoms to intern, and one more connection on an X-only
        # box is one more thing holding Xwayland open [recon/env.md 6] and one
        # more resource-id base out of the pool [recon/wire.md 1.3].
        self.assertIsNone(rig.server.own)
        self.assertEqual(rig.upstream.accepted, 1)          # the client's twin
        said = [t for t in lines if "no Wayland session" in t]
        self.assertEqual(len(said), 1)
        self.assertIn("passes through untouched", said[0])
        sock.close()


if __name__ == "__main__":
    unittest.main()
