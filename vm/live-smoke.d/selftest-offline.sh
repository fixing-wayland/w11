#!/usr/bin/env bash
# live-smoke's own regression: the phases, with no VM.
#
# It runs vm/live-smoke.sh against vm/live-smoke.d/fake-vmctl, which replays
# tests/fixtures/live/noble-gnome-46.0-windows-wm-replay.txt -- the `windows`
# and `wm` phases' guest commands, recorded off the real GNOME 46.0 guest on
# 2026-09-08 in the order the phases ask them.  Five passes:
#
#   1. the recording as it is: every check has to PASS.  A check that cannot
#      pass against the desktop it was written from is a broken check.
#   2. the same recording with ONE answer replaced -- the geometry after
#      `wwmctl -b remove,maximized_vert,maximized_horz` becomes the maximized
#      one, which is what the tools did before b7a60f0 -- and exactly one check
#      has to go red, the one named for that fix.  A check that cannot fail is
#      not a check.
#
# Passes 3 and 4 are the recorder check (phase busrec, T65), which no recording
# off the real guest covers because it is about a process and a growing file
# rather than about bytes a tool printed.  They are two hand-written
# transcripts: a session where `pgrep -x dbus-monitor` names a pid and the log
# grows on one bus call has to PASS, and one where the pid is missing and the
# log stands still has to FAIL.  The check this replaced could not do the
# second: it matched `pgrep -f 'dbus-monitor --session'`, which counts the
# `sh -c` wrapper carrying that very string, so it printed PASS with no
# recorder anywhere.
#
# Pass 5 is the bookkeeping that keeps the other four honest as step files
# arrive: every vm/live-smoke.d/<token>.sh is either covered by a recording here
# or named in tests/fixtures/live/NOT-YET-RUN, never both and never neither.
# Without it a new step file could ship with no recording, no entry, and nothing
# saying so -- and its checks would have been run by nobody.
#
# Pass 6 replays every OTHER recording in tests/fixtures/live/ against its own
# flavor's step file, IN THE MODE THE RUN THAT MADE IT WAS IN -- `vm/live-smoke.sh
# --record` writes that mode into the capture's first line, and a --pkg --remove
# run replayed with --reuse loses 14 of its checks with no red line anywhere (3
# install, 9 remove, 2 proxy: measured on the resolute-sway artifact of
# 2026-09-12, 59 pass in the run's own mode against 45 in tree mode).  It then
# replays it a second time with FAKE_VMCTL_STRICT to
# ask what the recording has NOTHING for.  Without that second pass the drift
# between a recording and the step file it was cut from is invisible: fake-vmctl
# answers an unrecorded command with empty output and status 0, a guard takes a
# branch by luck, and the checks behind it pass on nothing.  A recording that
# cannot answer something the run asks is named, with the commands, and is not a
# failure -- it is a recording waiting for the rig to cut a new one
# (scripts/rig-recordings.sh off a CI run).  SELFTEST_STRICT=1 makes it fatal.
#
# Takes about half a minute -- 23 s for nine recordings replayed twice each,
# measured 2026-09-12, where eight of them took 2 m 50 s before LIVE_SMOKE_SLEEP=0
# (the phases' sleeps are for a real compositor, not for a transcript) -- and needs
# nothing but bash and python3.
#   usage: vm/live-smoke.d/selftest-offline.sh [recording ...]
#
# With recordings named on the command line it runs pass 6 over those and nothing
# else, which is how one fresh fixture is checked before it is committed (and how
# tests/test_live_smoke.py checks the mode handling without a fixtures directory
# of its own).  SELFTEST_STRICT=1 makes drift fatal.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)          # <repo>/vm/live-smoke.d
REPO=$(dirname "$(dirname "$HERE")")
CAP=$REPO/tests/fixtures/live/noble-gnome-46.0-windows-wm-replay.txt
[ -f "$CAP" ] || { echo "no capture at $CAP"; exit 2; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
fails=0
drift=0
ONLY=$*          # recordings named on the command line: pass 6 over those, and no other pass
VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$REPO/w11common/__init__.py" | head -1)
say() { echo "$*"; }
bad() { fails=$((fails + 1)); echo "SELFTEST FAIL: $*"; }

# The three things a recording says about itself, all of them in comment lines that
# fake-vmctl's parser drops (it keeps nothing before the first `### `): the mode the run
# was made in and whether it removed the package, written by `vm/live-smoke.sh --record`,
# and the tally scripts/rig-recordings.sh accepted when it kept the file.  A capture cut
# before those lines existed answers empty, and the caller falls back to what it did then.
mode_of()     { sed -n 's/^# live-smoke recording:.* mode=\([a-z]*\) .*/\1/p' "$1" | head -1; }
remove_of()   { sed -n 's/^# live-smoke recording:.* remove=\([01]\) .*/\1/p' "$1" | head -1; }
accepted_of() { sed -n 's/^# replay: \([0-9]*\) pass.*/\1/p' "$1" | head -1; }
distro_of()   { sed -n 's/^#[[:space:]]*vmctl-distro:[[:space:]]*//p' "$REPO/vm/flavors/$1.yaml" | head -1; }

# Is the package a --pkg recording installed on THIS host?  The same four arms
# vm/live-smoke.sh's pkg_files has: release/w11_<version>_all.deb is committed, dist/*.rpm
# and dist/*.pkg.tar.zst are build artifacts and are not, and on NixOS nothing is copied in
# at all -- the module is the image (recon/recordings.md 1.6.3).  Without the package the
# install half cannot be replayed, so it comes out of the phase list and is said out loud.
have_package() {   # have_package <flavor>
    case $(distro_of "$1") in
        ubuntu) [ -f "$REPO/release/w11_${VERSION}_all.deb" ] ;;
        fedora) ls "${LIVE_SMOKE_RPMS:-$REPO/dist}"/*-"$VERSION"-*.rpm >/dev/null 2>&1 ;;
        arch)   [ -f "${LIVE_SMOKE_PKG:-$REPO/dist/w11-$VERSION-1-any.pkg.tar.zst}" ] ;;
        nixos)  return 0 ;;
        *)      return 1 ;;
    esac
}

# fake-vmctl in front of a tee for its own stderr.  With FAKE_VMCTL_STRICT set it answers a
# command nobody recorded with a line on stderr and exit 127 -- and the driver's guest() folds
# stderr into the command's OUTPUT (live-smoke.sh's `2>&1`), so that line ends up as the
# evidence clause of some check instead of anywhere a reader would look.  This copies it to a
# file as well, which is what pass 6 greps to say WHICH command a recording cannot answer
# (recon/recordings.md 1.6.1: "to a file the selftest greps").
cat > "$WORK/strict-vmctl" <<'WRAP'
#!/bin/sh
err=$(mktemp) || exit 2
"$FAKE_VMCTL_REAL" "$@" 2>"$err"; st=$?
if [ -s "$err" ]; then cat "$err" >> "${FAKE_VMCTL_MISSES:-/dev/null}"; cat "$err" >&2; fi
rm -f "$err"
exit $st
WRAP
chmod +x "$WORK/strict-vmctl"

# run_pass <label> <override-file-or-empty> [phases] [transcript] [flavor] [strict]
#          [driver flags] -> log on stdout
#
# LIVE_SMOKE_SLEEP=0 on every pass: every `sleep` in a phase is there for a real compositor --
# a modeset needs a frame, a hot-plugged head needs the next probe -- and a transcript has
# neither.  Measured on this box 2026-09-12: the nine recordings replayed in 2 s each with it
# and in 25-50 s each without (the eight-recording selftest took 2 m 50 s at load 52,
# recon/recordings.md 1.6.5).  The driver defines `sleep` as a function for exactly this.
run_pass() {
    local label=$1 override=$2 phases=${3:-windows,wm} cap=${4:-$CAP} flavor=${5:-noble-gnome}
    local strict=${6:-0} extra=${7:-}
    # `--reuse` goes away with the mode flags, and not because --pkg needs a fresh
    # instance: phase_install returns early under --reuse, so a --pkg recording replayed
    # with it loses the three checks the install phase is (recon/recordings.md 1.6.2).
    local reuse=--reuse
    if [ -n "$extra" ]; then reuse=""; fi
    rm -rf "$WORK/state-$label"; mkdir -p "$WORK/state-$label"
    : > "$WORK/$label.misses"
    FAKE_VMCTL_REAL=$HERE/fake-vmctl \
    FAKE_VMCTL_MISSES=$WORK/$label.misses \
    LIVE_SMOKE_VMCTL=$WORK/strict-vmctl \
    FAKE_VMCTL_TRANSCRIPT=$cap \
    FAKE_VMCTL_OVERRIDE=$override \
    FAKE_VMCTL_STRICT=$(if [ "$strict" = 1 ]; then echo 1; else echo ""; fi) \
    FAKE_VMCTL_STATE=$WORK/state-$label \
    LIVE_SMOKE_OUT=$WORK/out \
    LIVE_SMOKE_SLEEP=0 \
        "$REPO/vm/live-smoke.sh" "$flavor" --name fake-$label $reuse --keep $extra \
            --phases "$phases" > "$WORK/$label.log" 2>&1 || true
    cat "$WORK/$label.log"
}

# <flavor> <phases> for a recording, read out of its NAME: strip `-replay.txt`, strip the
# longest flavor prefix, drop the version token that follows it, and the rest is the phase
# list with `-` for `,`.  So `resolute-hypr-0.53.3-windows-wm-replay.txt` is
# `resolute-hypr windows,wm` and `resolute-i3-4.25.1-i3ipc-replay.txt` is `resolute-i3 i3ipc`.
# The name IS the mapping -- tests/test_live_smoke.py:test_every_recording_resolves_to_a_flavor
# says the same thing about the first half of it.
replay_spec() {   # replay_spec <path> -> "<flavor> <phases>" or nothing
    local base fl best rest
    base=$(basename "$1" -replay.txt); best=""
    for fl in "$REPO"/vm/flavors/*.yaml; do
        fl=$(basename "$fl" .yaml)
        case $base in "$fl"-*) [ ${#fl} -gt ${#best} ] && best=$fl ;; esac
    done
    [ -n "$best" ] || return 0
    rest=${base#"$best"-}          # <version>-<phase>[-<phase>...]
    rest=${rest#*-}                # drop the version token
    [ -n "$rest" ] || return 0
    printf '%s %s\n' "$best" "$(printf '%s' "$rest" | tr - ,)"
}

# replay_all <recording>... -- pass 6 over the recordings given, each in the mode the run
# that made it was in.
replay_all() {
    local cap spec flavor phases lbl p f n mode remove extra reduced want
    for cap in "$@"; do
        [ -e "$cap" ] || continue
        if [ -z "$ONLY" ] && [ "$cap" = "$CAP" ]; then continue; fi   # pass 1 and 2 are this one
        spec=$(replay_spec "$cap") || true
        if [ -z "$spec" ]; then
            bad "$(basename "$cap") does not resolve to a flavor and a phase list"
            continue
        fi
        flavor=${spec%% *}; phases=${spec#* }
        lbl=$(basename "$cap" -replay.txt)
        # The mode the capture's own header names.  A --pkg --remove run replayed with
        # --reuse in tree mode silently loses 14 of its 59 checks -- 3 install, 9 remove
        # and the 2 the proxy phase skips where MODE is tree -- and every one of the 38
        # fixtures the rig harvests is a --pkg --remove run (measured on the resolute-sway
        # artifact of 2026-09-12: 59 pass in the run's own mode, 45 in tree mode).
        mode=$(mode_of "$cap"); remove=$(remove_of "$cap"); extra=""; reduced=""
        if [ "$mode" = pkg ]; then
            if have_package "$flavor"; then
                extra="--pkg"
                if [ "$remove" = 1 ]; then extra="$extra --remove"; fi
            else
                reduced="the $(distro_of "$flavor") package that run installed is not on this host"
                phases=$(printf '%s' "$phases" | tr ',' '\n' | grep -vx install \
                           | tr '\n' ',' | sed 's/,$//')
            fi
        fi
        run_pass "$lbl" "" "$phases" "$cap" "$flavor" 0 "$extra" > "$WORK/$lbl.out"
        p=$(grep -c '^PASS ' "$WORK/$lbl.out" || true)
        f=$(grep -c '^FAIL ' "$WORK/$lbl.out" || true)
        say "   $flavor [$phases]${extra:+ $extra}: $p pass, $f fail"
        if [ -n "$reduced" ]; then
            say "      reduced: $reduced, so the install phase was not replayed" \
                "(LIVE_SMOKE_RPMS / LIVE_SMOKE_PKG are where it would look for one)"
        fi
        [ "$p" -ge 5 ] || bad "$(basename "$cap"): only $p checks ran against it"
        # The tally scripts/rig-recordings.sh accepted when it kept the file, which is the
        # run's own `done: N pass` -- there is no log beside a fixture, so this line is the
        # only thing that can catch a replay losing checks rather than failing them.  A
        # recording cut before the line existed has none, and then ">= 5 and none red" is
        # all there is to go on, which is what this pass had for all of them until now.
        want=$(accepted_of "$cap")
        if [ -n "$want" ] && [ -z "$reduced" ] && [ "$p" != "$want" ]; then
            bad "$(basename "$cap"): replayed $p checks where the rig accepted $want --" \
                "the replay is not running what the run ran"
        fi
        if [ "$f" != 0 ]; then
            grep '^FAIL ' "$WORK/$lbl.out" | sed 's/^/      /'
            bad "$(basename "$cap"): $f check(s) failed against the desktop they were recorded from"
        fi
        # ...and the SAME replay again with FAKE_VMCTL_STRICT, for the one question the run
        # above cannot answer: which commands the recording has nothing for.  fake-vmctl
        # answers those with empty output and status 0, so a guard takes its has-an-X-server
        # branch by luck and the checks behind it pass on nothing -- measured on the
        # committed sway recording, which was cut one commit before common.sh's `xprop -root`
        # guard landed and replays 27 checks lenient against 11 strict, 0 fail either way
        # (recon/recordings.md 1.6.1).  It is a second run and not the first one made strict,
        # because strict COSTS those 16 checks: the commands the recording does answer are
        # real bytes and the drift is one guard.
        #
        # Only for a recording that can answer the driver itself: `vm/live-smoke.sh --record`
        # wraps the whole run and its capture begins with wait_ssh's `true`, while an older
        # guest-capture.sh list has no preamble and strict there stops at `cannot reach over
        # ssh` with nothing replayed (measured 2026-09-12: 0 checks for noble-gnome-46.0 and
        # resolute-i3-4.25.1, against 39/35/42/32/31/30/11 for the seven that carry it).
        if grep -qx '### true' "$cap"; then
            run_pass "$lbl-strict" "" "$phases" "$cap" "$flavor" 1 "$extra" > /dev/null
            n=$(sort -u "$WORK/$lbl-strict.misses" | grep -c . || true)
        else
            say "      (a guest-capture.sh command list, with no ssh preamble: nothing to inventory)"
            n=0
        fi
        # NOT a failure: a recording is behind its step file until the rig cuts a new one
        # (scripts/rig-recordings.sh off a CI run), and this says how far behind, by command.
        # SELFTEST_STRICT=1 is the switch for the day they are all fresh.
        if [ "${n:-0}" != 0 ]; then
            drift=$((drift + 1))
            say "      behind its step file: $n command(s) the run asks and this recording does not answer"
            sort -u "$WORK/$lbl-strict.misses" | sed 's/fake-vmctl: nothing recorded for /         /'
            if [ "${SELFTEST_STRICT:-0}" = 1 ]; then
                bad "$(basename "$cap"): $n unrecorded command(s) and SELFTEST_STRICT=1"
            fi
        fi
    done
}

drift_summary() {
    say
    if [ "$drift" != 0 ]; then
        say "$drift recording(s) are behind their step files -- each is named above with the commands"
        say "it cannot answer.  scripts/rig-recordings.sh <run-id> cuts fresh ones from a CI run."
    fi
}

# Recordings named on the command line: pass 6 over exactly those.  One fresh fixture is
# checked this way before it is committed, and nothing else here has anything to say about
# a file that is not in tests/fixtures/live/ yet.
if [ -n "$ONLY" ]; then
    say "== the recording(s) named on the command line, replayed the way pass 6 does"
    replay_all "$@"
    drift_summary
    if [ "$fails" = 0 ]; then
        say "selftest-offline: OK ($# recording(s) replayed in the mode each was made in)"
        exit 0
    fi
    say "selftest-offline: $fails problem(s)"
    exit 1
fi

say "== pass 1: the recording as it was captured"
run_pass good "" > "$WORK/good.out"
grep -E '^(PASS|FAIL) ' "$WORK/good.out" | sed 's/^/   /'
n_pass=$(grep -c '^PASS ' "$WORK/good.out" || true)
n_fail=$(grep -c '^FAIL ' "$WORK/good.out" || true)
[ "$n_pass" -ge 15 ] || bad "only $n_pass checks ran against the recording (expected 15 or more)"
[ "$n_fail" = 0 ] || bad "$n_fail check(s) failed against the desktop they were recorded from"

# The pre-b7a60f0 behaviour, in one section: the geometry read AFTER the pair is
# removed answers with the maximized geometry instead of 100,100 800x600.  The
# override table wins over the transcript for that command, and its two entries
# put it back in the same asking order the phase uses.
cat > "$WORK/regress.txt" <<'BREAK'
### wdotool getwindowgeometry 1950993699
Window 1950993699
  Position: 100,100 (screen: 0)
  Geometry: 800x600
### rc=0
### wdotool getwindowgeometry 1950993699
Window 1950993699
  Position: 100,100 (screen: 0)
  Geometry: 800x600
### rc=0
### wdotool getwindowgeometry 1950993699
Window 1950993699
  Position: 66,32 (screen: 0)
  Geometry: 1854x1048
### rc=0
### wdotool getwindowgeometry 1950993699
Window 1950993699
  Position: 66,32 (screen: 0)
  Geometry: 1854x1048
### rc=0
BREAK

say
say "== pass 2: the same recording with the b7a60f0 regression put back in"
run_pass regress "$WORK/regress.txt" > "$WORK/regress.out"
grep -E '^(PASS|FAIL) ' "$WORK/regress.out" | sed 's/^/   /'
if grep -q '^FAIL wm: b7a60f0' "$WORK/regress.out"; then
    say "   (the b7a60f0 check went red, which is the point of this pass)"
else
    bad "the b7a60f0 check did not fail when the regression was put back"
fi
r_fail=$(grep -c '^FAIL ' "$WORK/regress.out" || true)
[ "$r_fail" = 1 ] || bad "expected exactly one failing check in pass 2, got $r_fail"

# ---- the recorder (T65's phase busrec) ---------------------------------------
# A session where the recorder is there: a pid whose process NAME is
# dbus-monitor, and a log that grew from 12 to 57 lines while one deliberate
# bus call was made.  (`wc -l < ... || echo 0` and `pgrep -x dbus-monitor` are
# the two commands the phase asks, as fake-vmctl normalises them.)
cat > "$WORK/busrec-ok.txt" <<'REC'
### pgrep -x dbus-monitor
4242
### rc=0
### wc -l < $HOME/w11-bus.log || echo 0
12
### rc=0
### wc -l < $HOME/w11-bus.log || echo 0
57
### rc=0
REC
# And the session the old check could not tell apart from it: nothing is
# recording, so there is no dbus-monitor process and the log stands at 0.
cat > "$WORK/busrec-none.txt" <<'REC'
### pgrep -x dbus-monitor
### rc=1
### wc -l < $HOME/w11-bus.log || echo 0
0
### rc=0
### wc -l < $HOME/w11-bus.log || echo 0
0
### rc=0
REC

say
say "== pass 3: phase busrec with a recorder running"
run_pass busrec-ok "" busrec "$WORK/busrec-ok.txt" > "$WORK/busrec-ok.out"
grep -E '^(PASS|FAIL) ' "$WORK/busrec-ok.out" | sed 's/^/   /'
grep -q '^PASS busrec: dbus-monitor (pid 4242) is recording' "$WORK/busrec-ok.out" \
    || bad "phase busrec did not pass with a recorder running"

say
say "== pass 4: phase busrec with nothing recording"
run_pass busrec-none "" busrec "$WORK/busrec-none.txt" > "$WORK/busrec-none.out"
grep -E '^(PASS|FAIL) ' "$WORK/busrec-none.out" | sed 's/^/   /'
grep -q '^FAIL busrec: no session-bus recording' "$WORK/busrec-none.out" \
    || bad "phase busrec passed with no recorder: the check is matching itself again"

# ---- pass 5: every step file is recorded or declared not yet run ------------
say
say "== pass 5: every step file is either recorded here or listed in NOT-YET-RUN"
NYR=$REPO/tests/fixtures/live/NOT-YET-RUN
[ -f "$NYR" ] || bad "no $NYR"
# A recording's token is the flavor its name starts with, resolved to that
# flavor's `# vmctl-desktop:`.  Longest match first, so noble-gnome-iso wins
# over noble-gnome for a name that starts with it.
recorded_tokens() {
    local cap base fl best
    for cap in "$REPO"/tests/fixtures/live/*-replay.txt; do
        [ -e "$cap" ] || continue
        base=$(basename "$cap"); best=""
        for fl in "$REPO"/vm/flavors/*.yaml; do
            fl=$(basename "$fl" .yaml)
            case $base in "$fl"*) [ ${#fl} -gt ${#best} ] && best=$fl ;; esac
        done
        [ -n "$best" ] || { echo "?$base"; continue; }
        sed -n 's/^#[[:space:]]*vmctl-desktop:[[:space:]]*//p' "$REPO/vm/flavors/$best.yaml" | head -1
    done
}
rec=$(recorded_tokens | sort -u)
# `|| true`: NOT-YET-RUN is empty once every flavor is recorded (the goal state),
# and `grep .` on no lines exits 1, which under `set -e` would abort pass 5 with its
# header printed and no token lines -- the whole point of pass 5 is to confirm exactly
# that empty state, so an empty list is success, not an error.
nyr=$(grep -v '^#' "$NYR" | grep . | sort -u || true)
for f in "$HERE"/*.sh; do
    tok=$(basename "$f" .sh)
    case $tok in common|selftest-offline|guest-*) continue ;; esac
    in_rec=$(printf '%s\n' "$rec" | grep -cx "$tok" || true)
    in_nyr=$(printf '%s\n' "$nyr" | grep -cx "$tok" || true)
    if [ "$in_rec" = 0 ] && [ "$in_nyr" = 0 ]; then
        bad "step file $tok.sh has no recording and is not in NOT-YET-RUN"
    elif [ "$in_rec" != 0 ] && [ "$in_nyr" != 0 ]; then
        bad "step file $tok.sh has a recording AND is still listed in NOT-YET-RUN"
    else
        say "   $tok: $(if [ "$in_rec" != 0 ]; then echo recorded; else echo 'not yet run'; fi)"
    fi
done

# ---- pass 6: every recording replays green against its own step file --------
# `CAP` above is one recording and its two phases; every OTHER recording in the
# directory used to be a file nothing ran.  Batch 16 checked its own by hand and
# said so in requests-batch-11.md item 3; this is that, in the tree, for all of
# them.  A recording is green or it is a claim about a desktop that nobody is
# checking any more.
say
say "== pass 6: every recording replays green against its own flavor's step file"
replay_all "$REPO"/tests/fixtures/live/*-replay.txt

drift_summary
if [ "$fails" = 0 ]; then
    say "selftest-offline: OK ($n_pass checks pass on the recording, 1 fails on the regression,"
    say "                  phase busrec passes with a recorder and fails without one, and every"
    say "                  step file is either recorded or declared not yet run)"
    exit 0
fi
say "selftest-offline: $fails problem(s)"
exit 1
