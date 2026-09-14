#!/usr/bin/env bash
# rig-recordings.sh -- the recordings a CI rig run left behind, turned into fixtures.
#
#   scripts/rig-recordings.sh <run-id> [flavor ...]
#
# `vm/live-smoke.sh --record` writes every guest command of a run and the bytes it
# answered next to the log, as <flavor>-<stamp>-capture.txt, and the vm job's
# `live-smoke-<flavor>` artifact ships the whole of vm/live-smoke.out/ -- so every push
# already carries a recording of every phase on every flavor, and until this script
# existed CI threw all 38 of them away (recon/recordings.md 1.1, measured: nothing in
# ci.yml set CAPLOG, and a plain log cannot be converted -- of the 47 guest commands in
# the committed sway fixture, 5 appear anywhere in the artifact's log, none with its
# output bytes or its status).
#
# Per flavor this downloads that artifact, names the file the way
# vm/live-smoke.d/selftest-offline.sh's `replay_spec` reads a name back --
# <flavor>-<version>-<phase>-<phase>...-replay.txt, the phases out of the log's own
# `phases:` line and the version out of the run's `w11-desktop-version:` note -- replays
# it through vm/live-smoke.d/fake-vmctl with FAKE_VMCTL_STRICT=1 IN THE MODE THE RUN WAS
# MADE IN, and keeps it only if that replay reproduces the tally the log recorded.  Then
# it deletes the two classes of line in tests/fixtures/live/NOT-YET-RUN that the
# recording retires: the flavor's `# vmctl-desktop:` token and, where the run really ran
# the proxy phase, `proxy:<flavor>`.
#
# The acceptance rule is the log's own `done: N pass, M fail`.  It is the strongest one
# available and it was measured: the resolute-sway golden recorded live on 2026-09-11 ran
# 56 pass / 0 fail, and replaying that capture in the run's own mode (`--pkg --remove`)
# gave 56 pass / 0 fail, check for check, in 48.4 s -- while the same capture replayed
# the way pass 6 used to (`--reuse`) gave 42, because `phase_install` returns early under
# --reuse and phase_proxy's wrapper half is `[ "$MODE" = tree ]` (recon/recordings.md
# 1.5, 1.6.2).  A recording that does not replay is not written.
#
#   RIG_REC_LOCAL=<dir>   take the artifacts from <dir>/<flavor>/ instead of `gh run
#                         download` -- how this is tested with no run in hand
#   RIG_REC_OUT=<dir>     where the fixtures are written (default tests/fixtures/live)
#   RIG_REC_NYR=<file>    the NOT-YET-RUN to edit (default tests/fixtures/live/NOT-YET-RUN)
#   RIG_REC_WORK=<dir>    the scratch directory (default a mktemp -d)
#   RIG_REC_GH_REPO=o/r   the repository to download from (default: the origin remote)
#
# Exit status is 1 when ANY flavor had a problem and 0 when every one asked for was
# either written or simply not in that run's matrix (the count is on the last line).
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
REPO=$(dirname "$HERE")
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
case ${1:-} in
    -h|--help) usage; exit 0 ;;
    "")        usage; exit 2 ;;
esac

RUN=$1; shift
LIVEFIX=$REPO/tests/fixtures/live
OUT=${RIG_REC_OUT:-$LIVEFIX}
NYR=${RIG_REC_NYR:-$LIVEFIX/NOT-YET-RUN}
WORK=${RIG_REC_WORK:-$(mktemp -d -t rig-recordings-XXXXXX)}
# `|| true`: a failed command substitution inside a ${X:-$(...)} default aborts
# under `set -euo pipefail` even though the value would just be empty -- and the
# nix sandbox copies the source with no .git, so `git config` here exits 128
# (test_live_smoke.py::TheRecordingsPipeline, the nix check).  RIG_REC_LOCAL runs
# never reach gh, so an empty GH_REPO is harmless there; a `gh run download` run
# without it dies later with a clear message of its own.
GH_REPO=${RIG_REC_GH_REPO:-$(git -C "$REPO" config --get remote.origin.url 2>/dev/null \
    | sed 's|.*[:/]\([^/]*\)/\([^/]*\)$|\1/\2|; s|\.git$||' || true)}
mkdir -p "$WORK" "$OUT"

flavors=${*:-}
if [ -z "$flavors" ]; then
    flavors=$(for f in "$REPO"/vm/flavors/*.yaml; do basename "$f" .yaml; done)
fi

hdr() {   # hdr <flavor> <key> -- one of the flavor yaml's `# vmctl-<key>:` headers
    sed -n "s/^#[[:space:]]*vmctl-$2:[[:space:]]*//p" "$REPO/vm/flavors/$1.yaml" | head -1
}

VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$REPO/w11common/__init__.py" | head -1)

# Is the package this flavor's `# vmctl-distro:` installs on THIS host?  The same four
# arms vm/live-smoke.sh's pkg_files has, asked before the replay rather than during it:
# release/w11_<version>_all.deb is committed, dist/*.rpm and dist/*.pkg.tar.zst are build
# artifacts and are not, and on NixOS nothing is copied in at all -- the module is the
# image (recon/recordings.md 1.6.3).
have_package() {   # have_package <flavor>
    case $(hdr "$1" distro) in
        ubuntu) [ -f "$REPO/release/w11_${VERSION}_all.deb" ] ;;
        fedora) ls "${LIVE_SMOKE_RPMS:-$REPO/dist}"/*-"$VERSION"-*.rpm >/dev/null 2>&1 ;;
        arch)   [ -f "${LIVE_SMOKE_PKG:-$REPO/dist/w11-$VERSION-1-any.pkg.tar.zst}" ] ;;
        nixos)  return 0 ;;
        *)      return 1 ;;
    esac
}

# fake-vmctl with its stderr copied to a file.  FAKE_VMCTL_STRICT answers a command
# nobody recorded with a line on stderr and exit 127, and the driver's guest() folds
# stderr into the command's own output (live-smoke.sh's `2>&1`) -- so without this the
# refusal would end up as the evidence clause of some check and nowhere else
# (recon/recordings.md 1.6.1).  vm/live-smoke.d/selftest-offline.sh writes the same five
# lines for the same reason.
cat > "$WORK/strict-vmctl" <<'WRAP'
#!/bin/sh
err=$(mktemp) || exit 2
"$FAKE_VMCTL_REAL" "$@" 2>"$err"; st=$?
if [ -s "$err" ]; then cat "$err" >> "${FAKE_VMCTL_MISSES:-/dev/null}"; cat "$err" >&2; fi
rm -f "$err"
exit $st
WRAP
chmod +x "$WORK/strict-vmctl"

# The version token, out of the capture itself.  The marker is in the recorded OUTPUT and
# not in the command: the driver asks `printf 'w11-desktop-version: '; <the desktop's own
# version command>` once per run (common.sh, desktop_version_cmd), so what the compositor
# printed about itself is a recorded section like any other.  The token is the first
# version-shaped word of that line -- `sway version 1.11` -> 1.11, `GNOME Shell 50.1` ->
# 50.1, `labwc 0.9.3 (wlroots 0.19.2)` -> 0.9.3 -- and it may not contain a dash, because
# the naming rule drops exactly ONE token after the flavor name.  An arm of
# desktop_version_cmd that prints nothing lands here as an empty token and the file is
# NOT named: a guessed version in a file name is a measurement nobody took.
version_of() {   # version_of <capture> -> the token, or nothing
    # `|| true`: a capture with no note at all is the case this function exists to catch,
    # and under the script's own `set -euo pipefail` the grep that finds nothing would
    # otherwise abort the whole run with no message at all (measured here on a capture
    # with the note stripped out, 2026-09-12).
    # A compositor version is dotted (1.11, 6.7.5, 0.53.3, 46.0); require the dot so a
    # stray stderr integer that slipped into the note (a `QThreadStorage: entry 7` number)
    # can never be mistaken for one and name the fixture after it.
    sed -n 's/^w11-desktop-version: //p' "$1" | head -1 \
        | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9A-Za-z][0-9A-Za-z.+~-]*$' | head -1 || true
}

# The mode the run was made in, which is the mode it has to be replayed in.  `--record`
# writes it into the capture's first line; a capture taken before that line existed still
# answers, because the log's banner says the same thing in words ("<package> only" for
# --pkg, "working tree over the package" for the default).
mode_of() {   # mode_of <capture> <log> -> pkg|tree
    local m
    m=$(sed -n 's/^# live-smoke recording:.* mode=\([a-z]*\) .*/\1/p' "$1" | head -1)
    if [ -z "$m" ]; then
        if grep -q 'working tree over the package' "$2"; then m=tree; else m=pkg; fi
    fi
    printf '%s\n' "$m"
}
remove_of() {   # remove_of <capture> <log> -> 1 when the run ran the remove phase
    local r
    r=$(sed -n 's/^# live-smoke recording:.* remove=\([01]\) .*/\1/p' "$1" | head -1)
    if [ -z "$r" ]; then
        if grep -q '^== \[[0-9]*s\] phase remove$' "$2"; then r=1; else r=0; fi
    fi
    printf '%s\n' "$r"
}

problems=0
written=0
for fl in $flavors; do
    if [ ! -f "$REPO/vm/flavors/$fl.yaml" ]; then
        echo "$fl: no such flavor"; problems=$((problems + 1)); continue
    fi
    dir=$WORK/$fl
    rm -rf "$dir"
    if [ -n "${RIG_REC_LOCAL:-}" ]; then
        [ -d "$RIG_REC_LOCAL/$fl" ] || { echo "$fl: no artifact directory under $RIG_REC_LOCAL"; continue; }
        cp -r "$RIG_REC_LOCAL/$fl" "$dir"
    else
        # A flavor that was not in that run's matrix is not a problem: it has nothing to
        # say about this run and the next one will carry it (all 38 are `# vmctl-ci: push`).
        # Everything ELSE gh can fail with -- an expired token, a repository that is not
        # this one, no network -- is a problem and is printed: swallowing those turned a
        # harvest that downloaded nothing at all into "0 recording(s) written, 0 problem(s)"
        # and exit 0, which is the one answer that must not be silent.
        if ! err=$(gh run download "$RUN" -R "$GH_REPO" -n "live-smoke-$fl" -D "$dir" 2>&1 >/dev/null); then
            case $err in
                *"no artifact"*|*"No artifacts found"*|*"not found"*)
                    echo "$fl: no live-smoke artifact on run $RUN"; continue ;;
                *)  echo "$fl: gh run download failed: $(printf '%s' "$err" | tr '\n' ' ' | cut -c1-160)"
                    problems=$((problems + 1)); continue ;;
            esac
        fi
    fi
    cap=$(ls "$dir"/*-capture.txt 2>/dev/null | tail -1 || true)
    if [ -z "$cap" ]; then
        echo "$fl: the artifact has no capture -- that run did not pass --record to vm/live-smoke.sh"
        problems=$((problems + 1)); continue
    fi
    log=${cap%-capture.txt}.log
    if [ ! -f "$log" ]; then
        echo "$fl: $(basename "$cap") has no log of the same stamp:" \
             "its phase list and its tally are both in that log"
        problems=$((problems + 1)); continue
    fi
    phases=$(sed -n 's/^phases: //p' "$log" | head -1 | tr -s ' ' | sed 's/ *$//' | tr ' ' '-')
    ver=$(version_of "$cap")
    if [ -z "$phases" ] || [ -z "$ver" ]; then
        echo "$fl: nothing to name the file after -- phases '$phases', version '$ver'" \
             "(an empty version is desktop_version_cmd's arm for $(hdr "$fl" desktop) printing nothing)"
        problems=$((problems + 1)); continue
    fi
    name=$fl-$ver-$phases-replay.txt
    mode=$(mode_of "$cap" "$log")
    remove=$(remove_of "$cap" "$log")
    want_pass=$(sed -n 's/^== \[[0-9]*s\] done: \([0-9]*\) pass, \([0-9]*\) fail$/\1/p' "$log" | tail -1)
    want_fail=$(sed -n 's/^== \[[0-9]*s\] done: \([0-9]*\) pass, \([0-9]*\) fail$/\2/p' "$log" | tail -1)

    # The replay, in the run's own mode.  The package halves need the package ON THIS HOST
    # (pkg_deploy copies it in): release/w11_<version>_all.deb is committed, dist/*.rpm and
    # dist/*.pkg.tar.zst are build artifacts and are not.  So on a flavor whose package is
    # not here the install and remove halves are dropped from the replay and the tally rule
    # gives way to "nothing failed" -- with the reason printed, and the route out of it
    # named: point LIVE_SMOKE_RPMS / LIVE_SMOKE_PKG at the `rpm` or `pkgbuild` job's
    # artifact and the full tally comes back.
    ph_csv=$(printf '%s' "$phases" | tr - ,)
    set -- --phases "$ph_csv"
    reduced=""
    if [ "$mode" = pkg ]; then
        if have_package "$fl"; then
            set -- "$@" --pkg
            if [ "$remove" = 1 ]; then set -- "$@" --remove; fi
        else
            reduced="the $(hdr "$fl" distro) package this run installed is not on this host"
            set -- --phases "$(printf '%s' "$ph_csv" | tr ',' '\n' | grep -vx install \
                                 | tr '\n' ',' | sed 's/,$//')"
        fi
    fi
    st=$WORK/state-$fl; rm -rf "$st"; mkdir -p "$st"
    o=$WORK/out-$fl; rm -rf "$o"; mkdir -p "$o"
    : > "$WORK/$fl.misses"
    LIVE_SMOKE_VMCTL=$WORK/strict-vmctl \
    FAKE_VMCTL_REAL=$REPO/vm/live-smoke.d/fake-vmctl \
    FAKE_VMCTL_MISSES=$WORK/$fl.misses \
    FAKE_VMCTL_TRANSCRIPT=$cap \
    FAKE_VMCTL_STRICT=1 \
    FAKE_VMCTL_STATE=$st \
    LIVE_SMOKE_OUT=$o \
    LIVE_SMOKE_SLEEP=0 \
        "$REPO/vm/live-smoke.sh" "$fl" --name replay-$fl --keep "$@" \
            > "$WORK/$fl.replay" 2>&1 || true
    p=$(grep -c '^PASS ' "$WORK/$fl.replay" || true)
    f=$(grep -c '^FAIL ' "$WORK/$fl.replay" || true)
    echo "$fl: $name -- replay $p pass, $f fail (log: ${want_pass:-?} pass, ${want_fail:-?} fail, mode $mode)"
    if [ -n "$reduced" ]; then
        echo "    reduced: $reduced, so install/remove were not replayed" \
             "(LIVE_SMOKE_RPMS / LIVE_SMOKE_PKG are where it would look for one)"
    fi
    ok=1
    if [ "$f" != "${want_fail:-0}" ] || [ "$p" -lt 5 ]; then ok=0; fi
    if [ -z "$reduced" ] && [ -n "$want_pass" ] && [ "$p" != "$want_pass" ]; then ok=0; fi
    if [ "$ok" != 1 ]; then
        # `|| true` on both: a tally that is short by a PASS has no FAIL line to print and
        # an empty grep is exit 1, which under `set -euo pipefail` would end the run here
        # with the reason unprinted (measured 2026-09-12 on a log whose tally was edited).
        { grep '^FAIL ' "$WORK/$fl.replay" || true; } | sed 's/^/    /'
        { sort -u "$WORK/$fl.misses" || true; } | sed 's/^/    /'
        echo "    NOT kept: the replay does not reproduce what the run's own log recorded"
        problems=$((problems + 1)); continue
    fi
    # The tally this replay accepted, stamped into the fixture as a second comment line
    # (fake-vmctl's parser keeps nothing before the first `### `, and neither does
    # selftest-offline.sh's `replay_spec`, which reads the NAME).  Pass 6 of the selftest
    # has no log beside the fixture, so without this line its only acceptance rule is
    # ">= 5 checks and none red" -- and a replay that quietly loses half its checks
    # satisfies that.  With it, the number the rig measured travels with the recording.
    awk -v ln="# replay: $p pass, $f fail (mode $mode, remove $remove, phases $ph_csv, $(date -u +%Y-%m-%d))" \
        'NR == 1 && /^# live-smoke recording:/ { print; print ln; next }
         NR == 1 { print ln } { print }' "$cap" > "$OUT/$name"
    written=$((written + 1))
    # One recording per flavor: an older <flavor>-*-replay.txt is drifted by construction
    # (it was cut from an older step file) and pass 6 would go on replaying it forever, so
    # the drift inventory could never empty and SELFTEST_STRICT=1 could never be turned on.
    # The NOT-YET-RUN edit below already assumes one per flavor.
    # `-[0-9]*` not `-*`: the token after the flavor is the VERSION, always a digit, so
    # this matches only THIS flavor's older fixtures and never a longer-prefix sibling
    # (resolute-cinnamon must not delete resolute-cinnamon-wayland; noble-gnome must not
    # delete noble-gnome-x11 or the curated noble-gnome-46.0 baseline).  Run 34688228778
    # showed the `-*` glob clobbering five siblings and two curated test baselines.
    for old in "$OUT/$fl"-[0-9]*-replay.txt; do
        [ -e "$old" ] || continue
        [ "$(basename "$old")" = "$name" ] && continue
        echo "    superseded: $(basename "$old") (cut from an older step file; removed)"
        rm -f "$old"
    done
    # The two classes of NOT-YET-RUN line this recording retires.  `proxy:<flavor>` only
    # when the run really ran the proxy phase: a `--skip proxy` run says nothing about it.
    tok=$(hdr "$fl" desktop)
    tmp=$WORK/nyr
    cp "$NYR" "$tmp"
    grep -vx "${tok:-__none__}" "$tmp" > "$tmp.1" || true; mv "$tmp.1" "$tmp"
    case $phases in *proxy*) grep -vx "proxy:$fl" "$tmp" > "$tmp.1" || true; mv "$tmp.1" "$tmp" ;; esac
    if ! cmp -s "$tmp" "$NYR"; then
        echo "    NOT-YET-RUN: $(($(wc -l < "$NYR") - $(wc -l < "$tmp"))) line(s) retired"
    fi
    cp "$tmp" "$NYR"
done
echo "rig-recordings: $written recording(s) written, $problems problem(s)"
if [ "$written" = 0 ] && [ "$problems" = 0 ] && [ -n "$flavors" ]; then
    echo "rig-recordings: nothing was written and nothing failed -- run $RUN carried no" \
         "live-smoke artifact for any of the flavors asked for.  Check the run id and" \
         "RIG_REC_GH_REPO (${GH_REPO:-unset})."
fi
[ "$problems" = 0 ]
