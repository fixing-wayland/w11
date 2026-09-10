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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                      # noqa: E402
from support import FakeBackend, FakeBackendEvents, fake_window     # noqa: E402
from wdotool import backend_detect                                  # noqa: E402
from wxprop.core import _events_hook                                # noqa: E402
from xw11 import pump as pump_mod                                   # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"


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
        [recon/seams.md 2.3]; one re-list answers all three.

        The TTL is pushed out to ten seconds first, so the clock cannot explain
        a `list()` and every one of them is somebody's decision to re-list. With
        the shipped 20 ms a re-list per token would hide behind the next expiry,
        which is exactly how the first version of this test survived a
        `for _t in tokens: self.shadows.refresh()` mutation in `drain_pump`."""
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
        for token in ((1, "new"), (1, "title"), (1, "focus")):
            pump.put(token)
        rig.wait(shadows.stale, what="the invalidation")
        rig.wait(lambda: pump.drain() == [], what="the deque emptied")
        # The loop invalidated and listed nothing: the next READ is what pays,
        # once, for all three tokens.
        self.assertEqual(backend.counts["list"], listed)
        shadows.snapshot()
        self.assertEqual(backend.counts["list"] - listed, 1)


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


class Wiring(unittest.TestCase):
    def test_the_server_drains_the_deque_into_one_invalidation(self):
        """What the loop does with the byte: the pump's tokens are drained in
        one go and the registry is told once that it is out of date -- TOLD,
        not re-listed."""
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
        rig.wait(lambda: shadows.stale(), what="the invalidation")
        rig.wait(lambda: pump.drain() == [], what="the deque emptied")
        # Told once, and not re-listed: the re-list is the next reader's, which
        # is what makes a burst on an idle proxy cost the compositor nothing.
        self.assertEqual(backend.counts["list"], listed)
        client.close()


if __name__ == "__main__":
    unittest.main()
