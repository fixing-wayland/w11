#!/bin/sh
# Compile the w11 labwc geometry shim against the distro's libwlroots-0.19 dev
# headers.  AGENTS.md route 6, our code, unmodified labwc: the .so is loaded
# into the stock compositor with LD_PRELOAD (see w11-labwc-session).
#
#     sh build-shim.sh [SRC.c] [OUT.so]
#
# Defaults: the w11-labwc-shim.c beside this script -> w11-labwc-shim.so beside
# it.  The w11 package builds it against whatever libwlroots-0.19 the target
# actually runs, which is the only version the .so can correctly read (the
# struct offsets are that soname's ABI), so this is a build-on-the-target step
# and not something baked into an Architecture: all .deb.
#
# Needs: a C compiler, pkg-config and wayland-scanner, plus the -dev packages of
# the wlroots the session runs -- on Ubuntu 26.04: gcc, pkgconf, libwlroots-0.19-dev,
# libwayland-dev, wayland-protocols.  (wlr_xdg_shell.h pulls in the generated
# xdg-shell-protocol.h, which is not installed anywhere, so we scan it here from
# the wayland-protocols XML into a scratch dir.)  Exits non-zero (and prints why)
# if any is missing; the session wrapper treats that as "run labwc without the shim".
set -eu

here=$(cd "$(dirname "$0")" && pwd)
src=${1:-$here/w11-labwc-shim.c}
out=${2:-$here/w11-labwc-shim.so}

CC=${CC:-cc}
command -v "$CC" >/dev/null 2>&1 || CC=gcc
if ! command -v "$CC" >/dev/null 2>&1; then
    echo "build-shim.sh: no C compiler (install gcc)" >&2
    exit 1
fi

PKG=pkg-config
command -v "$PKG" >/dev/null 2>&1 || PKG=pkgconf
if ! command -v "$PKG" >/dev/null 2>&1; then
    echo "build-shim.sh: no pkg-config (install pkgconf)" >&2
    exit 1
fi

# The wlroots the session runs.  0.19 is what Ubuntu 26.04 ships and labwc 0.9.3
# links; a later release with a newer soname names its own module here.
WLR=${W11_WLROOTS_PC:-wlroots-0.19}
if ! "$PKG" --exists "$WLR"; then
    echo "build-shim.sh: $WLR dev headers not found (install lib${WLR}-dev)" >&2
    exit 1
fi
if ! "$PKG" --exists wayland-server; then
    echo "build-shim.sh: wayland-server dev headers not found (install libwayland-dev)" >&2
    exit 1
fi

SCANNER=${WAYLAND_SCANNER:-wayland-scanner}
if ! command -v "$SCANNER" >/dev/null 2>&1; then
    echo "build-shim.sh: no wayland-scanner (install libwayland-dev / wayland-bin)" >&2
    exit 1
fi
protodir=$("$PKG" --variable=pkgdatadir wayland-protocols 2>/dev/null || true)
xdgxml=$protodir/stable/xdg-shell/xdg-shell.xml
if [ ! -f "$xdgxml" ]; then
    echo "build-shim.sh: xdg-shell.xml not found (install wayland-protocols)" >&2
    exit 1
fi

# wlr_xdg_shell.h #includes the generated xdg-shell-protocol.h; scan it into a
# scratch dir on the include path.
gen=$(mktemp -d)
trap 'rm -rf "$gen"' EXIT
"$SCANNER" server-header "$xdgxml" "$gen/xdg-shell-protocol.h"

set -x
# shellcheck disable=SC2046
"$CC" -O2 -fPIC -shared -Wall -o "$out" "$src" \
    -I"$gen" $("$PKG" --cflags "$WLR" wayland-server) -ldl
