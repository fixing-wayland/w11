#!/usr/bin/env python3
"""xw11/pump.py: the thread that turns a compositor's changes into one byte.

Every `events()` in this tree is a blocking Python generator and none of them is
a selectable fd [recon/seams.md 0.3], so the pump is the shape `wxprop` already
uses for the same job (`spy_merged_root`, wxprop/core.py:1154): a daemon thread
steps the generator, puts a token on a deque and writes one byte to a pipe the
selector loop is already watching.

Two facts it is built around:

* **the token is a hint, the diff is the truth.** sway answered `new`, `title`
  and `focus` for one `exec foot` within 75 ms of each other
  [recon/seams.md 2.3], and a `move` token carries no geometry at all
  [recon/seams.md 2.4]. So a wake drains every token and takes ONE snapshot --
  `Coalesce` is that claim.
* **two backends have no event stream at all.** wlr and COSMIC reach
  `WindowBackend.NOT_YET_EVENTS`, and `wxprop.core._events_hook` is what tells
  them apart from a backend that really has one (core.py:1072). They are polled
  at 0.25 s, which is Cinnamon's own rate for the same job
  (backend_cinnamon.py:62).
"""

import os
import sys
import threading
import time
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`, and
# it is set BEFORE the imports because a tool module reads it at import time.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                      # noqa: E402
from support import FakeBackend, FakeBackendEvents, fake_window     # noqa: E402
from wdotool import backend_detect                                  # noqa: E402
from wxprop.core import _events_hook                                # noqa: E402
from xw11 import pump as pump_mod                                   # noqa: E402



class RecordingLock:
    """A lock that says how it was used. `block.acquire(blocking=False)` from a
    test cannot answer "is the pump inside a step" without taking the lock away
    from the pump when the answer is no, which is a test that changes what it
    measures."""

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


class CinnamonBackend(FakeBackendEvents):
    """The one backend whose `events()` calls `list()` on the same Eval bus the
    loop's own calls go over [recon/seams.md 2.3]."""

    name = "cinnamon"


class PumpCase(unittest.TestCase):
    def pump(self, backend, **kw):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        self.pipe_r = r
        self.lines = []
        got = pump_mod.Pump(backend, w, log=self.lines.append, **kw)
        self.addCleanup(self.finish, got, backend)
        return got

    def finish(self, pump, backend):
        backend.feed(None)                      # the generator returns
        pump.stop(timeout=2.0)

    def wait(self, ready, timeout=5.0, what="the pump"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready():
                return True
            time.sleep(0.005)
        raise AssertionError("%s never happened in %gs" % (what, timeout))

    def bytes_ready(self, n, timeout=5.0):
        """One byte per token is what wakes the selector; this is the selector."""
        os.set_blocking(self.pipe_r, False)
        got = b""
        deadline = time.monotonic() + timeout
        while len(got) < n and time.monotonic() < deadline:
            try:
                got += os.read(self.pipe_r, n - len(got))
            except BlockingIOError:
                time.sleep(0.005)
        return got


class Coalesce(unittest.TestCase):
    """Through the real loop, because the coalescing is the LOOP's: the pump
    puts tokens on the deque and `Server.drain_pump` empties it into one
    invalidation, and a test that invalidated three times itself would be
    measuring nothing but its own for-loop."""

    def rig(self, backend, **kw):
        kw.setdefault("num", 64)
        kw.setdefault("upstream_num", 65)
        got = support.ProxyRig(passthrough=False, backend=backend, **kw)
        self.addCleanup(got.stop)
        return got

    def test_three_tokens_cost_one_list_and_not_three(self):
        """`new`, `title` and `focus` for one `exec foot` arrive together
        [recon/seams.md 2.3, +17.5 ms on this box 2026-09-10]; ONE re-list
        answers all three.

        The TTL is pushed out to ten seconds first, so the clock cannot explain
        a `list()` and every one of them is somebody's decision to re-list. With
        the shipped 20 ms a re-list per token would hide behind the next expiry,
        which is exactly how the first version of this test survived a
        `for _t in tokens: self.shadows.refresh()` mutation in `drain_pump`.

        Batch 5 moved the re-list itself INTO the drain (design section 5.2:
        the wake drains the deque, takes one snapshot and hands the diff to
        `xw11/events.py`), so what this pins is the count and no longer the
        laziness: three tokens, one `list()`."""
        backend = FakeBackend(windows=[fake_window(1, "foot")])
        rig = self.rig(backend)
        client = rig.conn()
        self.addCleanup(client.close)
        rig.wait_own()
        shadows = rig.server.shadows
        shadows.ttl = 10.0
        shadows.snapshot()
        self.assertFalse(shadows.stale())
        pump = rig.server.ensure_pump()
        self.assertIsNotNone(pump)
        listed = backend.counts["list"]
        for _i in range(20):
            for token in ((1, "new"), (1, "title"), (1, "focus")):
                pump.put(token)
        rig.wait(lambda: backend.counts["list"] > listed, what="the re-list")
        rig.wait(lambda: pump.drain() == [], what="the deque emptied")
        # One re-list per WAKE, never one per token. The loop can wake more
        # than once during a burst -- the pump writes a byte per token and the
        # selector is free to return between two of them -- so what is pinned
        # is the ratio: 60 tokens, single figures of `list()` on an idle box and
        # 15 on a loaded CI runner (run 34562929804), never the 60 that a
        # `for token in tokens: refresh()` inside `drain_pump` makes.
        # `test_one_drain_is_one_relist` in tests/test_xw11_events.py pins the
        # exact 1 on the deterministic seam.
        self.assertLessEqual(backend.counts["list"] - listed, 30)
        self.assertGreaterEqual(backend.counts["list"] - listed, 1)
        # And the reader after the last drain pays nothing: its snapshot is
        # fresh, because the drain took it.
        self.assertFalse(shadows.stale())
        after = backend.counts["list"]
        shadows.snapshot()
        self.assertEqual(backend.counts["list"], after)


class DequeShape(PumpCase):
    def test_a_drained_deque_is_empty_and_a_wake_with_nothing_in_it_is_no_error(self):
        backend = FakeBackend(windows=[fake_window(1)])
        pump = self.pump(backend)
        pump.start()
        backend.feed((1, "title"))
        self.assertEqual(len(self.bytes_ready(1)), 1)
        self.assertEqual(pump.drain(), [(1, "title")])
        self.assertEqual(pump.drain(), [])


class PollPath(PumpCase):
    def test_a_backend_with_no_event_stream_is_polled(self):
        """wlr and COSMIC have no `events()` at all, and this is the measurement
        the design leans on: `_events_hook` really answers None for a backend
        that never overrode it (wxprop/core.py:1072)."""
        backend = FakeBackend(has_events=False, windows=[fake_window(1)])
        self.assertIsNone(_events_hook(backend))
        pump = self.pump(backend, poll=0.05)
        pump.start()
        self.wait(lambda: backend.counts["list"] >= 3, what="three polls")
        pump.stop(timeout=2.0)
        settled = backend.counts["list"]
        time.sleep(0.15)
        self.assertEqual(backend.counts["list"], settled)

    def test_the_poll_only_wakes_the_loop_when_the_answer_changed(self):
        """A heartbeat that woke the selector four times a second for ever
        would cost every idle proxy on a wlr box a wake per quarter second."""
        backend = FakeBackend(has_events=False, windows=[fake_window(1, "a")])
        pump = self.pump(backend, poll=0.02)
        pump.start()
        self.wait(lambda: backend.counts["list"] >= 3, what="three polls")
        self.assertEqual(self.bytes_ready(1, timeout=0.1), b"")
        backend.windows[0].title = "b"
        self.assertEqual(len(self.bytes_ready(1)), 1)
        self.assertEqual([t[0] for t in pump.drain()], ["poll"])

    def test_the_cadence_is_cinnamons_own(self):
        self.assertEqual(pump_mod.POLL, 0.25)


class Restart(PumpCase):
    def test_a_stream_that_dies_is_re_detected_and_restarted(self):
        """A compositor restart, or KWin unloading the script under us. Nothing
        in the tree re-detects on its own [recon/seams.md 2.2], so the pump does
        it: `reset()` first, because the cached bus, the cached ListNames and
        the open registry connection all belong to the session that went."""
        dying = FakeBackend(windows=[fake_window(1)])
        fresh = FakeBackend(windows=[fake_window(2)])
        calls = {"reset": 0}
        old_reset, old_detect = backend_detect.reset, backend_detect.detect
        backend_detect.reset = lambda: calls.__setitem__("reset", calls["reset"] + 1)
        backend_detect.detect = lambda: fresh
        self.addCleanup(setattr, backend_detect, "reset", old_reset)
        self.addCleanup(setattr, backend_detect, "detect", old_detect)
        pump = self.pump(dying, restart=0.05)
        self.addCleanup(fresh.feed, None)
        pump.start()
        dying.feed((1, "title"))
        self.assertEqual(len(self.bytes_ready(1)), 1)
        dying.feed(RuntimeError("the compositor went away"))
        self.wait(lambda: pump.restarts == 1, what="the restart")
        self.assertEqual(calls["reset"], 1)
        self.assertIs(pump.backend, fresh)
        fresh.feed((2, "focus"))
        self.assertEqual(len(self.bytes_ready(1)), 1)
        self.assertIn((2, "focus"), pump.drain())
        self.assertTrue([line for line in self.lines
                         if "event stream stopped" in line])

    def test_an_error_token_is_a_dead_stream_too(self):
        """The backends that report their own trouble put `("error", text)` on
        the stream rather than raising."""
        dying = FakeBackend(windows=[fake_window(1)])
        fresh = FakeBackend(windows=[fake_window(2)])
        old_reset, old_detect = backend_detect.reset, backend_detect.detect
        backend_detect.reset = lambda: None
        backend_detect.detect = lambda: fresh
        self.addCleanup(setattr, backend_detect, "reset", old_reset)
        self.addCleanup(setattr, backend_detect, "detect", old_detect)
        pump = self.pump(dying, restart=0.05)
        self.addCleanup(fresh.feed, None)
        pump.start()
        dying.feed(("error", "sway hung up"))
        self.wait(lambda: pump.restarts == 1, what="the restart")
        self.assertTrue([line for line in self.lines if "sway hung up" in line])

    def test_the_new_backend_is_handed_to_the_rest_of_the_proxy(self):
        """Design section 5.2's restart is the PROXY's backend, not the pump's
        private copy: the loop reads through the same object, so a swap that
        moved only this thread's reference would leave every read calling a
        compositor that is gone."""
        dying = FakeBackend(windows=[fake_window(1)])
        fresh = FakeBackend(windows=[fake_window(2)])
        old_reset, old_detect = backend_detect.reset, backend_detect.detect
        backend_detect.reset = lambda: None
        backend_detect.detect = lambda: fresh
        self.addCleanup(setattr, backend_detect, "reset", old_reset)
        self.addCleanup(setattr, backend_detect, "detect", old_detect)
        told = []
        pump = self.pump(dying, restart=0.05, on_backend=told.append)
        self.addCleanup(fresh.feed, None)
        pump.start()
        dying.feed(RuntimeError("the compositor went away"))
        self.wait(lambda: pump.restarts == 1, what="the restart")
        self.assertEqual(told, [fresh])
        self.assertIs(pump.backend, fresh)

    def test_and_the_reset_and_the_hand_off_happen_under_the_servers_lock(self):
        """`reset()` closes the cached bus, the cached `ListNames` and the
        registry connection [recon/seams.md 2.2] -- and it runs on THIS thread
        while the loop may be inside a call on exactly those."""
        dying = FakeBackend(windows=[fake_window(1)])
        fresh = FakeBackend(windows=[fake_window(2)])
        block = RecordingLock()
        seen = []
        old_reset, old_detect = backend_detect.reset, backend_detect.detect
        backend_detect.reset = lambda: seen.append(("reset", block.held))
        backend_detect.detect = lambda: (seen.append(("detect", block.held)),
                                         fresh)[1]
        self.addCleanup(setattr, backend_detect, "reset", old_reset)
        self.addCleanup(setattr, backend_detect, "detect", old_detect)
        pump = self.pump(dying, restart=0.05, block=block,
                         on_backend=lambda b: seen.append(("told", block.held)))
        self.addCleanup(fresh.feed, None)
        pump.start()
        dying.feed(RuntimeError("the compositor went away"))
        self.wait(lambda: pump.restarts == 1, what="the restart")
        self.assertEqual(seen, [("reset", True), ("detect", True),
                                ("told", True)])

    def test_the_wait_before_a_restart_is_two_seconds(self):
        """Long enough that a compositor coming back is not hammered."""
        self.assertEqual(pump_mod.RESTART_SECONDS, 2.0)


class CinnamonLocked(PumpCase):
    def test_cinnamons_generator_is_stepped_under_the_servers_lock(self):
        """Its `events()` calls `list()` on the same Eval bus as the loop's own
        calls [recon/seams.md 2.3]; two threads on one bus is two answers on one
        socket."""
        backend = CinnamonBackend(windows=[fake_window(1)])
        block = RecordingLock()
        pump = self.pump(backend, block=block)
        pump.start()
        backend.feed((1, "title"))
        self.assertEqual(len(self.bytes_ready(1)), 1)
        self.assertGreaterEqual(block.taken, 1)

    def test_and_it_is_held_while_the_pump_waits_for_the_next_change(self):
        """The cost, written down: the lock is held across the whole step, so on
        Cinnamon -- and only there -- a read from the loop waits for that
        backend's own 0.25 s diff (backend_cinnamon.py:437). The finer lock,
        one per bus call inside the backend, is the module docstring's not-yet."""
        backend = CinnamonBackend(windows=[fake_window(1)])
        block = RecordingLock()
        pump = self.pump(backend, block=block)
        pump.start()
        backend.feed((1, "title"))
        self.assertEqual(len(self.bytes_ready(1)), 1)
        self.wait(lambda: block.held, what="the pump parked inside a step")

    def test_every_other_backend_has_a_connection_of_its_own_and_takes_no_lock(self):
        """sway, Hyprland, Wayfire, KWin and the GNOME bridge each open their
        own socket or bus inside the generator [recon/seams.md 2.3], so a pump
        parked in `next()` on one of them leaves the loop's lock free."""
        backend = FakeBackend(windows=[fake_window(1)])
        block = RecordingLock()
        pump = self.pump(backend, block=block)
        pump.start()
        backend.feed((1, "title"))
        self.assertEqual(len(self.bytes_ready(1)), 1)
        self.assertEqual(block.taken, 0)
        self.assertFalse(block.held)
        with block:
            backend.feed((1, "focus"))
            self.assertEqual(len(self.bytes_ready(1)), 1)


class StopWhileReading(PumpCase):
    """What `stop()` says when the generator is where it always is.

    A generator parked in `next()` cannot be closed from another thread --
    CPython answers `ValueError: generator already executing` -- and every
    `events()` in this tree blocks on a socket or a bus [recon/seams.md 0.3].
    That is the normal path, taken once per client disconnect that drops the
    last mask, so it belongs in the debug log and not in the one a user reads.
    """

    def test_the_expected_close_failure_is_a_debug_line_and_not_a_log_line(self):
        debug = []
        backend = FakeBackendEvents()
        got = self.pump(backend, debug=debug.append)
        got.start()
        backend.feed((11, "title"))
        self.wait(lambda: self.bytes_ready(1, timeout=1.0), what="a token")
        got.stop(timeout=0)
        self.assertEqual(self.lines, [], "nothing at say level: %r" % self.lines)
        self.assertEqual(len(debug), 1, "one debug line: %r" % debug)
        self.assertIn("the next event", debug[0])

    def test_a_generator_nobody_is_inside_closes_with_no_line_at_all(self):
        """The other half, so the test above is pinning the ValueError branch
        and not "stop() is quiet": a stream that already returned closes
        cleanly and says nothing anywhere."""
        debug = []
        backend = FakeBackendEvents()
        got = self.pump(backend, debug=debug.append)
        got.start()
        backend.feed(None)                      # the generator returns
        self.wait(lambda: got.restarts or got._gen is None
                  or self.lines, what="the stream ending")
        got.stop(timeout=2.0)
        # The restart line belongs to `_run` and is a stream that ended; what
        # must not be here is the close path saying anything at either level.
        self.assertEqual(debug, [])
        self.assertEqual([ln for ln in self.lines if "clos" in ln
                          or "inside the compositor" in ln], [])


class Wiring(unittest.TestCase):
    def test_the_server_drains_the_deque_into_one_invalidation(self):
        """What the loop does with the byte: the pump's tokens are drained in
        one go and the registry re-reads the compositor ONCE for the lot
        (design section 5.2). Batch 2 pinned the invalidation alone here,
        because nothing yet consumed the diff; batch 5's `Server.on_changes`
        is what consumes it, so the count of `list()` calls is the claim."""
        backend = FakeBackend(windows=[fake_window(1)])
        rig = support.ProxyRig(num=56, upstream_num=57, passthrough=False,
                               backend=backend)
        self.addCleanup(rig.stop)
        client = rig.conn()
        rig.wait_own()
        shadows = rig.server.shadows
        shadows.ttl = 10.0                  # so the clock explains no list()
        shadows.snapshot()
        self.assertFalse(shadows.stale())
        listed = backend.counts["list"]
        pump = rig.server.ensure_pump()
        self.assertIsNotNone(pump)
        for token in ((1, "new"), (1, "title")):
            pump.put(token)
        rig.wait(lambda: backend.counts["list"] > listed, what="the re-list")
        rig.wait(lambda: pump.drain() == [], what="the deque emptied")
        # At most one per wake, and the snapshot the diff came out of is the
        # one the next reader gets.
        self.assertLessEqual(backend.counts["list"] - listed, 2)
        self.assertFalse(shadows.stale())
        client.close()


class Armed(unittest.TestCase):
    """The pump's lifetime: the first non-zero mask on the root or a shadow
    starts it and the last one to go stops it (design section 5.2's decision
    A).

    What it buys is measured on the other side: sway's `events()` opens a
    SECOND IPC socket and subscribes inside the generator, KWin's loads a JS
    script that stays loaded for the whole iteration [recon/seams.md 2.3]. A
    proxy that ran one for a client which only ever reads would pay that for
    nothing, and the 20 ms registry TTL is what keeps reads fresh instead.
    """

    def rig(self, backend, **kw):
        kw.setdefault("num", 66)
        kw.setdefault("upstream_num", 67)
        got = support.ProxyRig(passthrough=False, backend=backend, **kw)
        self.addCleanup(got.stop)
        return got

    def armed(self, rig):
        return rig.server.events_pump is not None \
            and rig.server.events_pump.started

    def client(self, rig):
        conn = rig.conn()
        self.addCleanup(conn.close)
        rig.wait_own()
        return conn

    def sync(self, conn):
        return conn._wait_reply(conn._send(43, 0))      # GetInputFocus

    def shadow(self, rig):
        rig.server.shadows.refresh()
        got = [e for e in rig.server.shadows.snapshot() if e.shadow]
        self.assertTrue(got, "no shadow to select on")
        return got[0].shadow

    def test_no_event_stream_runs_until_a_client_selects_one(self):
        backend = FakeBackend(windows=[fake_window(1, "foot")])
        rig = self.rig(backend, num=68, upstream_num=69)
        conn = self.client(rig)
        self.sync(conn)
        self.assertFalse(self.armed(rig))
        self.assertEqual(backend.counts["events"], 0)
        conn.select_input(self.shadow(rig), 0x420000)
        self.sync(conn)
        rig.wait(lambda: self.armed(rig), what="the pump")
        rig.wait(lambda: backend.counts["events"] == 1,
                 what="the compositor's stream")

    def test_a_zero_mask_does_not_arm_it(self):
        """`ChangeWindowAttributes` with `CWEventMask = 0` is how a client
        stops watching; it must not be what starts the stream."""
        backend = FakeBackend(windows=[fake_window(1, "foot")])
        rig = self.rig(backend, num=70, upstream_num=71)
        conn = self.client(rig)
        conn.select_input(self.shadow(rig), 0)
        self.sync(conn)
        time.sleep(0.2)
        self.assertFalse(self.armed(rig))
        self.assertEqual(backend.counts["events"], 0)

    def test_the_last_mask_to_go_stops_it(self):
        backend = FakeBackend(windows=[fake_window(1, "foot")])
        rig = self.rig(backend, num=72, upstream_num=73)
        conn = self.client(rig)
        shadow = self.shadow(rig)
        conn.select_input(shadow, 0x420000)
        self.sync(conn)
        rig.wait(lambda: self.armed(rig), what="the pump")
        conn.select_input(shadow, 0)
        self.sync(conn)
        rig.wait(lambda: rig.server.events_pump is None,
                 what="the pump stopping")
        # And the compositor is not read again for a change nobody wants: a
        # token now reaches a deque nobody drains.
        listed = backend.counts["list"]
        backend.feed((1, "title"))
        time.sleep(0.2)
        self.assertEqual(backend.counts["list"], listed)

    def test_a_backend_with_no_events_is_polled_only_while_armed(self):
        """wlr and COSMIC reach `WindowBackend.NOT_YET_EVENTS`
        [recon/seams.md 2.3] and are polled instead -- and the poll is inside
        the pump's thread, so an unarmed proxy makes no call at all."""
        backend = FakeBackend(windows=[fake_window(1, "foot")],
                              has_events=False)
        rig = self.rig(backend, num=74, upstream_num=75)
        conn = self.client(rig)
        self.sync(conn)
        self.assertIsNone(_events_hook(backend))
        idle = backend.counts["list"]
        time.sleep(0.3)
        self.assertEqual(backend.counts["list"], idle)
        conn.select_input(self.shadow(rig), 0x420000)
        self.sync(conn)
        rig.wait(lambda: self.armed(rig), what="the pump")
        rig.server.events_pump.poll = 0.02
        rig.wait(lambda: backend.counts["list"] > idle + 1,
                 what="the poll")


if __name__ == "__main__":
    unittest.main()
