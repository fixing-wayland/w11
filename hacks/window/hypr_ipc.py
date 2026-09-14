"""Hyprland's IPC socket, spoken directly (no `hyprctl`, no external tools).

Two sockets in one instance directory, `$XDG_RUNTIME_DIR/hypr/<signature>/`:

* `.socket.sock` -- request/response. Send the request as plain text, read the reply to EOF, and the
  connection is finished: one connection per request. That is the whole protocol, and it was proved against a
  live 0.53.3 with a ten-line AF_UNIX client before any of this was written -- `j/monitors` came back as the
  JSON `hyprctl -j monitors` prints [M recon2/hyprland.md §2].
* `.socket2.sock` -- events, a line stream of `name>>payload`. Five lines were recorded, in order, when a foot
  window opened: `windowtitle`, `windowtitlev2`, `openwindow`, `activewindow`, `activewindowv2`
  [M recon2/hyprland.md §2].

A request starting `j/` is answered with JSON; everything else with Hyprland's own words (`ok`, or a refusal
sentence). `dispatch` and `keyword` are the two mutating verbs the window and display backends need.

The failure modes are the sway backend's, because the situations are: a compositor that exited between
find_hypr_socket() and connect(), one wedged in its own event loop (the kernel accepts for it and nothing is
ever written), and one killed mid-write (a truncated reply). Each is one line in the tool's own voice, never a
traceback."""

import json
import os
import select
import socket
import time

from w11common import session
from w11common.errors import CmdError

#: Deadline on connect and on the reply. The same 10 s the sway backend arms, and for the same reason: every
#: answer is built in the compositor's own event loop, so silence means it is wedged rather than busy. A live
#: 0.53.3 answered `j/monitors` immediately [M recon2/hyprland.md §2].
IPC_TIMEOUT = 10.0


def _lost(e) -> CmdError:
    """Any wire-level failure of the Hyprland IPC socket, as one clear line."""
    return CmdError("hypr backend: lost the connection to the compositor (%s)" % e)


def _wedged(timeout: float) -> CmdError:
    """Connected, and not answering: a compositor stuck in its own event loop
    accepts on the IPC socket (the kernel does that) and then says nothing."""
    return CmdError("hypr backend: no answer from the compositor within %gs "
                    "(it is not responding)" % timeout)


def _no_socket() -> CmdError:
    return CmdError("hypr backend: no Hyprland IPC socket found "
                    "($HYPRLAND_INSTANCE_SIGNATURE unset and no hypr/*/.socket.sock in any runtime dir)")


class HyprIPC:
    """One instance directory's two sockets. Holds no connection: each request opens its own.

    That is not a simplification of a stream protocol, it IS the protocol -- the reply is delimited by EOF and
    by nothing else, so a client that kept the connection for a second request would block against a real
    Hyprland for ever (tests/test_support_helpers.py:HyprDouble pins that on the double). Nothing here has to
    be closed, which is why the display backend can hand this object to a probe and to a Session in turn."""

    def __init__(self, sockpath: "str | None" = None, timeout: float = IPC_TIMEOUT):
        self.sockpath = sockpath or session.find_hypr_socket()
        if not self.sockpath:
            raise _no_socket()
        self.timeout = timeout

    def close(self):
        """Nothing to close -- there is no held connection. It exists so this object can be a wxrandr probe's
        handle, which is closed once by the probe sweep and again by the Session that reused it."""

    @property
    def events_path(self) -> str:
        """`.socket2.sock` beside `.socket.sock`, which is where Hyprland puts it [M recon2/hyprland.md §2]."""
        return os.path.join(os.path.dirname(self.sockpath), ".socket2.sock")

    # -- wire ---------------------------------------------------------------

    def _connect(self, path: str, timeout: "float | None") -> socket.socket:
        """A connected socket, carrying `timeout` once it is up.

        The connect() itself always gets self.timeout, whatever the caller wants afterwards, for the reason
        SwayBackend._connect spells out: a compositor wedged inside its own event loop still has a *listening*
        socket, so connecting looks fine until the backlog fills and then blocks in the kernel for ever."""
        deadline = time.monotonic() + self.timeout
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                s.connect(path)
                break
            except (TimeoutError, BlockingIOError):
                # both are OSError subclasses: this arm has to come first
                s.close()
                if time.monotonic() >= deadline:
                    raise _wedged(self.timeout) from None
                time.sleep(0.01)
            except OSError as e:
                s.close()
                raise CmdError("hypr backend: cannot connect to %s: %s" % (path, e)) from None
        s.settimeout(timeout)
        return s

    def request(self, text: str) -> bytes:
        """Send `text`, read the reply to EOF, close. The reply's bytes, whatever they are."""
        s = self._connect(self.sockpath, self.timeout)
        try:
            try:
                s.sendall(text.encode())
            except TimeoutError:
                raise _wedged(self.timeout) from None
            except OSError as e:
                raise _lost(e) from None
            out = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except TimeoutError:
                    # TimeoutError is an OSError: this arm has to come first, or a compositor that is merely
                    # wedged reads as one that has gone.
                    raise _wedged(self.timeout) from None
                except OSError as e:
                    raise _lost(e) from None
                if not chunk:
                    break
                out += chunk
        finally:
            s.close()
        if not out:
            raise CmdError("hypr backend: the compositor closed the IPC socket without answering `%s`" % text)
        return out

    def json(self, name: str):
        """`j/<name>`, parsed. `name` is the bare word (`clients`, `monitors`, ...)."""
        raw = self.request("j/" + name)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as e:
            # a compositor killed mid-write leaves a truncated object, and one that does not know the request
            # answers a sentence: both are "this is not the JSON we asked for", and neither is a traceback
            raise CmdError("hypr backend: the compositor's answer to j/%s is not JSON (%s)" % (name, e)) from None

    def _ok(self, text: str) -> str:
        """Send a mutating verb and insist on `ok`. Anything else is Hyprland's own refusal, relayed whole and
        in its name -- `Invalid dispatcher` for a verb it does not know [R HyprCtl.cpp; no report recorded
        one]."""
        reply = self.request(text).decode("utf-8", "replace").strip()
        if reply != "ok":
            raise CmdError("hypr: %s" % reply)
        return reply

    def dispatch(self, args: str) -> str:
        """`dispatch <args>`. Every verb the window backend sends was measured `ok` and verified in
        `hyprctl clients` [M recon2/hyprland.md §2]."""
        return self._ok("dispatch " + args)

    def keyword(self, text: str) -> str:
        """`keyword <text>` -- a live config assignment, which is how the display backend applies a layout."""
        return self._ok("keyword " + text)

    # -- events -------------------------------------------------------------

    def events(self, timeout: "float | None" = None):
        """(name, payload) for every line on `.socket2.sock`, in Hyprland's own vocabulary.

        Stops after `timeout` seconds of silence (None waits for ever, which is what select_window() does). The
        connection is opened here and closed on the way out, so a caller that stops iterating early leaves
        nothing behind."""
        s = self._connect(self.events_path, None)
        buf = b""
        try:
            while True:
                if timeout is not None and not select.select([s], (), (), timeout)[0]:
                    return
                try:
                    chunk = s.recv(65536)
                except OSError as e:
                    raise _lost(e) from None
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace")
                    name, sep, payload = text.partition(">>")
                    if sep:
                        yield name, payload
        finally:
            s.close()
