"""Shared command context: the object every cmd_* function is handed.

Command function contract (all *_cmds.py modules):

    def cmd_foo(ctx: Context, args: list[str]) -> int

The return value is the number of argv tokens the command consumed (NOT counting
the command name itself). Raise errors.CmdError(msg) on failure: the driver
prints the message to stderr, aborts the rest of the chain, and exits 1. That
class lives in w11common/errors.py, because the display tools catch it too.
"""

from w11common import errors


class SoftCmdError(errors.CmdError):
    """"The compositor cannot do this to *this* window" -- not a failure of the request, a shape of the desktop.
    sway refusing to move or resize a tiled container is the case that exists.

    A command that swallows one warns on stderr and carries on with rc 0; every other CmdError is a real failure
    and must reach the driver, which is the difference between "sway tiles this window" and "there is no such
    window" (both used to exit 0 out of windowmove)."""


class NoSessionError(errors.CmdError):
    """No Wayland session / window-management backend could be found at all (B5). Distinct from "the session is
    fine but nothing matched", which stays rc 1, so a script can tell "not logged in yet / no bridge" from "no
    such window" -- see "Session readiness and exit codes" in docs/WDOTOOL.md."""

    exit_code = 2


# Highest valid X/Mutter window id: both are 64-bit unsigned in our wire
# formats, and anything outside the range cannot name a window.
_MAX_WINDOW_ID = 2 ** 64 - 1


class Context:
    def __init__(self):
        self.stack: list[int] = []
        self._backend = None
        self._daemon = None
        # Non-fatal failure marker (e.g. `search` with no results): the chain
        # continues/completes but the process exits with this code.
        self.exit_code = 0
        # --layout (ours, not xdotool's): "us"/"fixed" forces the built-in US character table without reading
        # the compositor's keymap at all, "xkb" forces the reverse map, None leaves the choice to detection.
        self.layout_mode: str | None = None

    def backend(self):
        if self._backend is None:
            from hacks.window import backend_detect

            self._backend = backend_detect.detect()
        return self._backend

    def daemon(self):
        if self._daemon is None:
            from wdotool.daemon import DaemonClient

            self._daemon = DaemonClient.connect_or_spawn()
        return self._daemon

    def _no_stack(self, ref: str) -> errors.CmdError:
        """xdotool's empty-stack refusal, all three lines of it: the message, the reference that could not be
        resolved, and the command's own usage (window_get_arg() records it in `cmd_usage`). Scripts grep for
        this."""
        msg = "There are no windows in the stack\nInvalid window '%s'" % ref
        usage = getattr(self, "cmd_usage", None)
        if usage:
            msg += "\n" + usage.rstrip("\n")
        return errors.CmdError(msg)

    def _resolve_one(self, arg: str) -> int:
        if arg.startswith("%"):
            ref = arg[1:]
            if not self.stack:
                raise self._no_stack(arg)
            if ref == "@":
                return self.stack[0]
            try:
                n = int(ref)
            except ValueError:
                raise errors.CmdError(f"Invalid window stack reference '{arg}'") from None
            # Negative refs count from the end, like xdotool's window_list():
            # index = len(stack) + n, valid when it lands in [1, len(stack)].
            idx = len(self.stack) + n if n < 0 else n
            if idx <= 0 or idx > len(self.stack):
                raise errors.CmdError(
                    f"Invalid window stack reference '{arg}' (stack has {len(self.stack)} windows)"
                )
            return self.stack[idx - 1]
        try:
            wid = int(arg, 0)
        except ValueError:
            raise errors.CmdError(f"Invalid window id '{arg}'") from None
        # B8: a negative or out-of-range id used to reach the bridge and blow up in the D-Bus marshaller
        # ("cannot marshal -5 as 't'"); one line and rc 1 instead.
        if not 0 <= wid <= _MAX_WINDOW_ID:
            raise errors.CmdError(f"Invalid window id '{arg}'")
        return wid

    def resolve_window(self, arg: str | None = None) -> int:
        """Resolve an optional window argument like xdotool: explicit arg (decimal, 0x-hex, or %N/%@ stack ref),
        else %1. Like xdotool, an omitted window argument with an empty window stack is an error (the real tool
        validates the implicit "%1" against the stack and refuses; being lenient here made `wdotool windowclose`
        close the focused window where xdotool errors)."""
        if arg is not None:
            return self._resolve_one(arg)
        if self.stack:
            return self.stack[0]
        raise self._no_stack("%1")

    def resolve_windows(self, arg: str | None = None) -> list[int]:
        """Like resolve_window, but %@ expands to the entire stack."""
        if arg == "%@":
            if not self.stack:
                raise self._no_stack("%@")
            return list(self.stack)
        return [self.resolve_window(arg)]
