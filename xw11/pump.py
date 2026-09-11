"""One thread, one pipe: the compositor's changes, told to the selector loop.

Every `events()` in the tree is a **blocking Python generator, never a
selectable fd** [recon/seams.md 0.3]. Two backends have a pollable fd
underneath -- `dbus_mini.Bus.fileno()` and `WlConn.sock` -- but sway, Hyprland
and Wayfire open a second socket *inside* the generator and close it on exit,
and whether a `WlConn` survives a `select()` between `dispatch()` calls is
untested [recon/seams.md 0.3, 10.1]. So the shape is the one `wxprop` already
uses for exactly this (`spy_merged_root`, wxprop/core.py:1154): a daemon thread
steps the generator, puts a token on a deque under a lock and writes **one
byte** to a pipe the loop is selecting on.

Two backends have no `events()` at all -- wlr and COSMIC reach
`WindowBackend.NOT_YET_EVENTS` -- and are polled with `list()` every
`POLL = 0.25` s, which is Cinnamon's own rate for the same job
(backend_cinnamon.py:62) [recon/seams.md 2.3]. That poll is INSIDE the thread,
so it runs exactly as long as the pump does and not a tick longer.

**Nothing here starts itself.** `Server.arm_pump` calls `start()` when the
first client puts a non-zero mask on the root or a shadow and `stop()` when the
last such mask goes -- with the client, with a shadow that died, or with a
`ChangeWindowAttributes(0)` (design section 5.2's decision A). A proxy nobody
watches through therefore costs the compositor no sway subscription, no
Hyprland socket2 connection and no loaded KWin script, and the read side stays
fresh on the registry's own 20 ms TTL instead.

**The token is a hint; the diff is the truth.** What a client is told changed is
what two snapshots of `xw11/shadow.py` differ by, because a `move` token is a
bare word with no geometry in it [recon/seams.md 2.4] and sway's `new`, `title`
and `focus` for one `exec foot` all arrived within 75 ms of each other
[recon/seams.md 2.3] -- one re-list answers all three, which is why the loop
drains the deque completely and takes one snapshot.

Cinnamon is the one backend whose `events()` calls `list()` on the same Eval bus
from this thread [recon/seams.md 2.3], so its generator is stepped under the
server's `block` lock; every other backend's stream has a connection of its own.
That lock is held for the whole step and not only for the bus call inside it,
because the step is one `next()` on somebody else's generator and there is no
seam inside it -- so on Cinnamon, and only there, a read from the loop waits for
the pump's current step, which is that backend's own 0.25 s diff
(backend_cinnamon.py:437, `POLL = 0.25`). Holding it per bus call instead is not
yet done here: the route is a lock inside `backend_cinnamon`'s own `_call`
(this tree's code, no rung needed), at the cost of a backend that knows about a
lock the proxy owns. Two threads on one `dbus_mini.Bus` is two answers on one
socket, so the coarse lock is what ships until the fine one is written.
"""

import collections
import contextlib
import os
import threading

#: The poll cadence for a backend with no event stream: Cinnamon's own
#: (backend_cinnamon.py:62) [recon/seams.md 2.3].
POLL = 0.25

#: How long the pump waits before rebuilding a stream that died. A compositor
#: restart or KWin unloading the script is a generator that raises; the wait
#: keeps a compositor that is coming back from being hammered.
RESTART_SECONDS = 2.0

#: The one backend whose event generator shares a bus with the calls the loop
#: makes.
_LOCKED_BACKEND = "cinnamon"


class Pump:
    """`backend.events()` on a thread, tokens on a deque, one byte per token."""

    def __init__(self, backend, wake_w, log=None, block=None, poll=POLL,
                 restart=RESTART_SECONDS, on_backend=None, debug=None):
        self.backend = backend
        #: Told, under the lock, when a restart detected a new backend. There is
        #: ONE backend in this process -- the loop reads through it too (design
        #: section 5.2's restart is the proxy's backend, not the pump's private
        #: copy) -- so a swap that only moved this reference would leave the
        #: registry calling a compositor that is gone.
        self.on_backend = on_backend
        self.wake_w = wake_w
        self.poll = poll
        self.restart = restart
        self._log = log
        #: The line nobody needs unless they are debugging this file: the stop
        #: path's "the generator was inside the compositor's read" is ONE line
        #: per client disconnect on a busy session, and it says nothing a user
        #: can act on.
        self._debug = debug
        self._block = block
        self._lock = threading.Lock()
        self._tokens = collections.deque()
        self._thread = None
        self._stop = threading.Event()
        self._gen = None
        self.started = False
        self.restarts = 0

    def say(self, text: str) -> None:
        if self._log is not None:
            self._log(text)

    def debug_say(self, text: str) -> None:
        if self._debug is not None:
            self._debug(text)

    # -- the deque ------------------------------------------------------------

    def put(self, token) -> None:
        """One token, and one byte on the pipe. The byte is what wakes the
        selector; the deque is what says why, and a wake that finds an empty
        deque is not an error -- the loop takes one snapshot either way."""
        with self._lock:
            self._tokens.append(token)
        try:
            os.write(self.wake_w, b"\1")
        except OSError:
            pass

    def drain(self):
        """Every token since the last drain, emptied in one go. The caller takes
        ONE snapshot for the whole list (design section 5.2)."""
        with self._lock:
            got = list(self._tokens)
            self._tokens.clear()
        return got

    # -- the thread -----------------------------------------------------------

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="xw11-pump")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """The stream let go and, where the caller waits, the thread joined. A
        stopped pump costs the compositor nothing: no sway subscription, no
        loaded KWin script (design section 5.2's decision).

        `timeout=0` does not wait for the thread at all, which is what the
        LOOP thread passes (`Server.arm_pump`, `Server.shutdown`): a client
        that disconnects must not freeze every other client for the length of a
        join, and the caller drops its reference so that the next arming builds
        a fresh pump rather than reviving this one's thread.

        A generator that is *inside* a blocking read cannot be closed from
        another thread -- CPython answers `ValueError: generator already
        executing` -- and every `events()` in this tree blocks on a socket or a
        bus (`recon/seams.md 0.3`). So the close is attempted, the failure is a
        log line, and the thread (a daemon) ends at the compositor's next event
        or with the process. Ending it on the spot needs a stream the loop can
        select on, which only `dbus_mini.Bus.fileno()` and `WlConn.sock` offer
        today and which sway, Hyprland and Wayfire hide inside the generator:
        that is not yet done here, and the route is those two fds plus a
        re-implementation of the other three streams over their own sockets
        (AGENTS.md route 1, the protocols the compositors already speak), at the
        cost of a second copy of each backend's event parsing."""
        if not self.started:
            return
        self.started = False
        self._stop.set()
        # The pipe is the server's and the server may close it (or the number
        # may be handed out again) while a thread that could not be closed is
        # still parked in the compositor's blocking read. -1 is what keeps that
        # thread's next `put()` out of somebody else's descriptor: the write
        # raises and is swallowed there.
        self.wake_w = -1
        gen, self._gen = self._gen, None
        if gen is not None:
            try:
                gen.close()
            except ValueError:
                # "generator already executing": the thread is parked in the
                # compositor's blocking read, which is where it is for all but
                # microseconds of its life. Expected, not a failure, and one
                # line per client disconnect if it were said out loud -- the
                # stream ends at the compositor's next event or with the
                # process, as the docstring above says.
                self.debug_say("the event stream was inside the compositor's "
                               "read: it ends at the next event")
            except Exception as e:                      # noqa: BLE001
                self.say("closing the event stream raised %s" % e)
        if self._thread is not None and timeout:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._one_stream()
            except Exception as e:                      # noqa: BLE001
                # A generator that dies is a compositor that restarted, or KWin
                # unloading the script under us. Neither is this proxy's to
                # fix, and neither may end the pump: the tools that come next
                # need the stream back.
                self.say("the compositor's event stream stopped (%s): "
                         "re-detecting in %gs" % (e, self.restart))
            if self._stop.is_set():
                break
            self._sleep(self.restart)
            if self._stop.is_set():
                break
            if not self._redetect():
                continue
            self.restarts += 1

    def _redetect(self) -> bool:
        """`backend_detect.reset()` and detect again: nothing in the tree
        re-detects on its own [recon/seams.md 2.2], and the cached bus, names
        and registry connection all belong to a compositor that is gone.

        Under the server's lock, because this runs on the pump's thread and
        `reset()` closes the very bus and registry connection the loop may be
        inside a call on [recon/seams.md 2.2] -- and the hand-off to everyone
        else holding the old backend happens under the same lock, so no read
        can land between the swap here and the swap there."""
        from wdotool import backend_detect

        from xw11 import shadow as shadow_mod
        with (self._block if self._block is not None
              else contextlib.nullcontext()):
            backend_detect.reset()
            got = shadow_mod.detect_backend(self._log)
            if got is None:
                return False
            self.backend = got
            if self.on_backend is not None:
                self.on_backend(got)
        return True

    def _one_stream(self) -> None:
        hook = self._events_hook()
        if hook is None:
            self._poll_forever()
            return
        try:
            self._gen = hook(None, workspaces=True)
        except TypeError:                    # a hook without the workspaces flag
            self._gen = hook(None)
        locked = getattr(self.backend, "name", "") == _LOCKED_BACKEND
        while not self._stop.is_set():
            if locked and self._block is not None:
                with self._block:
                    token = next(self._gen)
            else:
                token = next(self._gen)
            if isinstance(token, tuple) and token and token[0] == "error":
                raise RuntimeError(token[1] if len(token) > 1 else "error")
            self.put(token)

    def _events_hook(self):
        """`backend.events` where the backend really overrides it, None where it
        would only raise -- `wxprop.core._events_hook` (core.py:1072) is that
        test and is imported here rather than repeated."""
        from wxprop.core import _events_hook
        return _events_hook(self.backend)

    def _poll_forever(self) -> None:
        """wlr and COSMIC: `list()` every `POLL` seconds, and a token only when
        the answer changed. The pump is the only thing calling the compositor on
        this path, so a poll that finds nothing costs one `list()` -- 0.08 ms on
        sway [recon/seams.md 2.3] -- and wakes nobody.

        `previous` is the whole bookkeeping: the reduced listing of the last
        poll, compared field by field. The FIRST poll only records it -- there
        is nothing to have changed against -- so arming the pump never fires a
        burst of events for windows that were already there."""
        previous = None
        while not self._stop.is_set():
            now = self._poll_list()
            if now is not None and now != previous:
                if previous is not None:
                    self.put(("poll", 0))
                previous = now
            self._sleep(self.poll)

    def _poll_list(self):
        """One `list()` under the server's lock, reduced to what a diff would
        notice. Errors are swallowed here on purpose: the poll is a heartbeat,
        and a compositor that answers again next tick has cost one skipped
        beat."""
        try:
            if self._block is not None:
                with self._block:
                    wins = self.backend.list()
            else:
                wins = self.backend.list()
        except Exception as e:                          # noqa: BLE001
            self.say("polling the compositor raised %s" % e)
            return None
        return [(w.id, w.title, w.x, w.y, w.w, w.h, w.focused, w.visible,
                 w.desktop) for w in wins]

    def _sleep(self, seconds: float) -> None:
        """A sleep a `stop()` cuts short, so a pump on the poll path does not
        hold the shutdown for a quarter of a second."""
        self._stop.wait(seconds)

