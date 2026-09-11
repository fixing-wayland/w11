#!/bin/sh
# Build dist/<tool>: single-file executable zipapps (stdlib-only; warandr
# imports the system PyGObject/GTK 3 at run time).
set -eu
cd "$(dirname "$0")/.."

# Stamped into every zipapp so that a *copy* of one of these files installed
# under an original's name is recognised as ours by
# w11common.passthrough.is_us() (the head sniff, which is the only "not us"
# guard that survives two copies under two names in two PATH directories).
# zipapp stores members uncompressed and __main__.py sorts first, so the
# stamp lands in plain text in the first few hundred bytes -- which is what
# the guard reads. The guard matches the whole of "$STAMP", never the bare
# word: a third-party wrapper that merely mentions the project must not be
# mistaken for one of ours. Keep both in sync with
# w11common/passthrough.py:MARKER / STAMP.
MARKER=w11
STAMP="$MARKER-clone:"

build() { # name entry_module packages...
  name=$1; entry=$2; shift 2
  rm -rf dist/.stage
  mkdir -p dist/.stage
  for p in "$@"; do cp -r "$p" "dist/.stage/$p"; done
  find dist/.stage -name __pycache__ -type d -exec rm -rf {} +
  printf '# %s-clone: %s (X11 passthrough marker, see w11common/passthrough.py)\nimport sys\nfrom %s import main\n\nsys.exit(main())\n' \
    "$MARKER" "$name" "$entry" > dist/.stage/__main__.py
  printf '%s %s\n' "$STAMP" "$name" > "dist/.stage/_${MARKER}_marker"
  python3 -m zipapp dist/.stage -p "/usr/bin/env python3" -o "dist/$name"
  rm -rf dist/.stage
  chmod +x "dist/$name"
  # the guard reads the first 4 KiB: fail the build if the stamp is not there
  if ! head -c 4096 "dist/$name" | grep -aqF "$STAMP"; then
    echo "build-pyz: $name: stamp '$STAMP' missing from the first 4 KiB" >&2
    exit 1
  fi
  echo "built dist/$name ($(wc -c < "dist/$name") bytes)"
}

# w11common is in every bundle: every tool here finds its session, hands over
# to the original on an X11 one, raises the same exception when it fails and
# flushes through the same stdout on the way out.
#
# wwmctl/wxprop ride with the package they import for backends and the X
# wire client, wdotool. backend_kwin uses x11_mini to read the X plane: the
# XWayland ids KWin 6 does not export, and the WM_CLASS pair 5.27
# lower-cases; since it lives in wdotool the shipped wdotool answers like a
# source checkout with nothing else in the bundle.
build wdotool wdotool.cli w11common wdotool
build wwmctl  wwmctl.cli  w11common wdotool wwmctl
build wxprop  wxprop.cli  w11common wdotool wxprop
# The three display tools import no wdotool at all: what they used of it --
# CmdError, stdio and procs -- is in w11common, so a zipapp of one of them no
# longer drags in the keysym table, the input daemon and four window
# backends for three small files. That is 680 kB off each of the three.
build wxrandr wxrandr.cli w11common wxrandr
# warandr bundles wxrandr: on Wayland it runs the same interpreter with
# -m wxrandr, PYTHONPATH pointing at the pyz itself (zipimport)
build warandr warandr.cli w11common wxrandr warandr
# wmirror drives an external binary but reads the layout through wxrandr's
# own wlr client, and the detached supervisor is this same zipapp re-entered
# by fork, so it needs nothing else in the bundle.
build wmirror wmirror.cli w11common wxrandr wmirror
# xw11 is the widest bundle and the only one that needs to be: the proxy
# answers for windows with wdotool's backends (xw11/shadow.py), for properties
# with wxprop's synthesis (xw11/pump.py imports wxprop.core._events_hook), for
# RandR with wxrandr's model and appliers (xw11/randr.py, all of it inside
# functions -- `import xw11` still pulls none of it) and for input with
# wdotool's daemon client.  wwmctl and warandr are NOT in it: nothing under
# xw11/ imports either.
build xw11 xw11.cli w11common wdotool wxprop wxrandr xw11
