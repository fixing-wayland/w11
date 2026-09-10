"""xw11 -- an X11 protocol proxy that speaks for the compositor.

A display of its own (`:20`..`:69`, both sockets, its own lock file and xauth
entry) in front of the session's real X server. Every byte a client sends is
forwarded upstream and every byte the server answers comes back, so the tools X
users already have -- xdotool, wmctrl, xprop, xrandr and everything else built
on libX11 -- keep working exactly as they do on X. What the compositor knows and
Xwayland does not (native toplevels, their titles, geometry, the client list,
input) is answered by the proxy in later stages; this stage is the pass-through
that everything after it must never regress.

AGENTS.md's rule is the whole design: if X supports it, we support it. The
proxy exists because that answer has to be given in X11 bytes.
"""

__all__ = ["cli", "client", "display", "policy", "server", "wire"]
