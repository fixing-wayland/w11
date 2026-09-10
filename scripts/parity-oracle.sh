#!/bin/sh
# Run the two byte-parity files against the oracles they were written against,
# under a display, and fail loudly when either oracle is missing.
#
#     sh scripts/parity-oracle.sh
#
# tests/test_cli_parity.py compares ~45 invocations byte-for-byte with the real
# xdotool. The version string is in `version`, in the help text and in every
# usage block, so the file only means anything against the pinned generation --
# 4.20260303.1, the one flake.nix builds. Against Ubuntu 26.04's apt xdotool
# (3.20160805.1) it used to abort on the first compare and take the other ~40
# with it; since the version skip landed in that file's prologue it bows out
# instead, which is honest but silent. This script is what makes it run.
#
# tests/test_wwmctl_cli.py is the mirror case: two wmctrl generations answer
# "1.07" to -V and differ only in --help (6801 bytes vs 7179; the newer carries
# "\n  -j "). Ubuntu 26.04 ships 1.07+git20240228, nixpkgs the plain 1.07, and
# the file has to be green against both -- so it is run once against each.
#
# Where the oracles come from, in order:
#   $W11_ORACLE_PATH    colon-separated PATH prefix, used as given
#   $W11_ORACLE_PATH_FILE / scripts/nixpath  a file holding one such prefix
#   /nix/store/*-xdotool-<pinned>/bin and /nix/store/*-wmctrl-1.07/bin
# On a host with neither, build them: `nix develop` (both on PATH), or
#   W11_ORACLE_PATH=$(nix build --no-link --print-out-paths .#xdotool .#wmctrl |
#                    sed 's|$|/bin|' | paste -sd:)
# `packages.xdotool` and `packages.wmctrl` are nixpkgs' own, re-exported by
# flake.nix so that this sentence is true.  Until 0.5 they were not there and
# this line named two attributes the flake did not have: `nix build .#xdotool`
# answered `does not provide attribute ... Did you mean wdotool?`, while CI did
# it the working way (`nix build --inputs-from . nixpkgs#xdotool`)
# [recon2/pkg-nix §1 defect 2].
#
#   W11_PARITY_DISPLAY   display to use for the Xvfb we start (default :99)
#   W11_PARITY_PROXY=1   run everything through `python3 -m xw11` on
#                        $W11_PARITY_PROXY_DISPLAY (default :98) instead of
#                        straight at $DISPLAY.  Stage 1 of the X11 proxy is
#                        "not one byte changes", and this is what says so: both
#                        runs must differ only in the `Ran N tests in` lines
#                        [recon/env.md 7, measured with a 61-line forwarder].
set -eu
cd "$(dirname "$0")/.."

XDO_PIN=$(python3 -c 'import sys; sys.path.insert(0, "."); from wdotool import cli; print(cli.XDO_VERSION)')
: "${W11_PARITY_DISPLAY:=:99}"
: "${W11_PARITY_PROXY_DISPLAY:=:98}"

die() { printf 'parity-oracle: %s\n' "$*" >&2; exit 2; }
say() { printf '\n== %s\n' "$*"; }

# -- find the pinned oracles --------------------------------------------------

prefix=""
if [ -n "${W11_ORACLE_PATH:-}" ]; then
  prefix=$W11_ORACLE_PATH
else
  for f in "${W11_ORACLE_PATH_FILE:-}" scripts/nixpath; do
    [ -n "$f" ] && [ -f "$f" ] || continue
    prefix=$(tr -d '\n' < "$f")
    break
  done
fi
if [ -z "$prefix" ]; then
  # No glob-with-no-match nonsense: `set -- pattern` leaves the pattern itself
  # when nothing matches, so test the file rather than the word.
  for d in /nix/store/*-xdotool-"$XDO_PIN"/bin; do
    [ -x "$d/xdotool" ] && prefix="$d" && break
  done
  for d in /nix/store/*-wmctrl-1.07/bin; do
    [ -x "$d/wmctrl" ] && prefix="${prefix:+$prefix:}$d" && break
  done
fi
[ -n "$prefix" ] || die "no oracle path: set \$W11_ORACLE_PATH, or build them with nix"
PATH="$prefix:$PATH"
export PATH

command -v xdotool >/dev/null 2>&1 || die "no xdotool on PATH after adding $prefix"
command -v wmctrl  >/dev/null 2>&1 || die "no wmctrl on PATH after adding $prefix"
have=$(xdotool version | awk '{print $NF}')
[ "$have" = "$XDO_PIN" ] || die "xdotool on PATH is $have, the clone is written against $XDO_PIN"
whelp=$(wmctrl --help | wc -c)
[ "$whelp" = 6801 ] || die "wmctrl --help is $whelp bytes, the nix 1.07 oracle is 6801"
printf 'parity-oracle: xdotool %s at %s\n' "$have" "$(command -v xdotool)"
printf 'parity-oracle: wmctrl 1.07 at %s\n' "$(command -v wmctrl)"

# -- a display, because xdo_new() runs before xdotool dispatches --------------

xvfb_pid=""
proxy_pid=""
cleanup() {
  [ -n "$proxy_pid" ] && kill "$proxy_pid" 2>/dev/null
  [ -n "$xvfb_pid" ] && kill "$xvfb_pid" 2>/dev/null
  :
}
trap cleanup EXIT INT TERM HUP PIPE
if [ -z "${DISPLAY:-}" ] || ! xdotool getdisplaygeometry >/dev/null 2>&1; then
  command -v Xvfb >/dev/null 2>&1 || die "no usable DISPLAY and no Xvfb to start one"
  Xvfb "$W11_PARITY_DISPLAY" -screen 0 1280x720x24 >/dev/null 2>&1 &
  xvfb_pid=$!
  DISPLAY=$W11_PARITY_DISPLAY
  export DISPLAY
  i=0
  while [ "$i" -lt 100 ]; do
    xdotool getdisplaygeometry >/dev/null 2>&1 && break
    i=$((i + 1))
    sleep 0.1
  done
  [ "$i" -lt 100 ] || die "Xvfb on $W11_PARITY_DISPLAY never came up"
fi
printf 'parity-oracle: DISPLAY=%s (%s)\n' "$DISPLAY" "$(xdotool getdisplaygeometry | tr '\n' ' ')"

# -- and, when asked, the proxy in front of it --------------------------------
#
# The same wait the Xvfb above gets, for the same reason: the proxy is up when a
# client can talk through it, not when the process exists.

if [ -n "${W11_PARITY_PROXY:-}" ]; then
  python3 -m xw11 --upstream "$DISPLAY" --display "$W11_PARITY_PROXY_DISPLAY" \
      --passthrough --foreground &
  proxy_pid=$!
  DISPLAY=$W11_PARITY_PROXY_DISPLAY
  export DISPLAY
  i=0
  while [ "$i" -lt 100 ]; do
    xdotool getdisplaygeometry >/dev/null 2>&1 && break
    i=$((i + 1))
    sleep 0.1
  done
  [ "$i" -lt 100 ] || die "xw11 on $W11_PARITY_PROXY_DISPLAY never came up"
  printf 'parity-oracle: through xw11 on DISPLAY=%s (pid %s)\n' "$DISPLAY" "$proxy_pid"
fi

# -- the '#'-anywhere comment rule, straight off the oracle -------------------
#
# tests/test_cli_script.py pins this from a table. The rule itself came out of a
# real mismatch: 4.20260303.1 treats any unquoted token starting with '#' as the
# end of the line, 3.2016 only the first token, and this clone followed 3.2016
# until wdotool/cli.py:240 dropped `and first`. Compare the two binaries here so
# the claim rests on the oracle and not on our reading of it.

say "'#' comment rule against the oracle"
tmp=$(mktemp -d)
cat > "$tmp/comment.xdo" <<'EOF'
exec --sync echo before # trailing
EOF
cat > "$tmp/ours" <<EOF
#!$(command -v python3)
import sys
sys.path.insert(0, "$PWD")
from wdotool import cli
sys.exit(cli.main())
EOF
chmod 755 "$tmp/ours"
W11_PASSTHROUGH=never "$tmp/ours" "$tmp/comment.xdo" > "$tmp/out.ours" 2>&1 || true
xdotool "$tmp/comment.xdo" > "$tmp/out.real" 2>&1 || true
if ! cmp -s "$tmp/out.ours" "$tmp/out.real"; then
  printf 'ours: %s\nreal: %s\n' "$(cat "$tmp/out.ours")" "$(cat "$tmp/out.real")" >&2
  rm -rf "$tmp"
  die "'#' comment rule differs from xdotool $have"
fi
printf 'both print: %s\n' "$(cat "$tmp/out.real")"
[ "$(cat "$tmp/out.real")" = "before" ] || die "expected 'before' from both"
rm -rf "$tmp"

# -- the two unittest files ---------------------------------------------------
#
# test_cli_parity bows out with a printed SKIP rather than a failure when it
# cannot compare; here that is the one thing this script exists to prevent, so
# grep for it instead of trusting the exit status.

say "tests/test_cli_parity.py against xdotool $have"
out=$(python3 tests/test_cli_parity.py 2>&1) || { printf '%s\n' "$out"; die "test_cli_parity failed"; }
printf '%s\n' "$out"
case $out in
  *SKIP*) die "test_cli_parity skipped -- the oracle above did not reach it" ;;
esac

say "tests/test_wwmctl_cli.py against the nix wmctrl 1.07"
# Capture rather than pipe: this is /bin/sh with `set -eu` and dash has no
# pipefail, so `python3 ... | tail -3` would report tail's status and a red run
# would end in "all oracles ran" with rc 0 -- the exact silent pass the script
# exists to prevent.
out=$(WWMCTL_WMCTRL_GENERATION=1.07 python3 tests/test_wwmctl_cli.py 2>&1) ||
  { printf '%s\n' "$out"; die "test_wwmctl_cli failed against the nix wmctrl 1.07"; }
printf '%s\n' "$out" | tail -3

# The distro oracle is the other half of the pair, and it is only reachable by
# taking the nix prefix back off: /usr/bin/wmctrl on Ubuntu 26.04 is
# 1.07+git20240228, whose --help is the 7179-byte HELP_GIT.
say "tests/test_wwmctl_cli.py against the distro wmctrl"
distro=$(PATH=${PATH#"$prefix":} command -v wmctrl || true)
[ -n "$distro" ] || die "no second wmctrl outside $prefix to check the git generation against"
dhelp=$("$distro" --help | wc -c)
printf 'parity-oracle: %s --help is %s bytes\n' "$distro" "$dhelp"
# 26.04 ships the git generation (7179 bytes); 24.04 the plain 1.07 (6801), the same
# generation as the nix oracle, which is still a second binary worth the run.
case $dhelp in
    7179) gen=git ;;
    6801) gen=1.07; printf 'parity-oracle: %s is the plain 1.07 generation (24.04 ships that one)\n' "$distro" ;;
    *) die "$distro --help is $dhelp bytes, neither the 6801-byte 1.07 nor the 7179-byte git generation" ;;
esac
out=$(PATH=${PATH#"$prefix":} WWMCTL_WMCTRL_GENERATION=$gen python3 tests/test_wwmctl_cli.py 2>&1) ||
  { printf '%s\n' "$out"; die "test_wwmctl_cli failed against the distro wmctrl $distro"; }
printf '%s\n' "$out" | tail -3

say "parity-oracle: all oracles ran"
