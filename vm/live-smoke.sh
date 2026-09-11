#!/usr/bin/env bash
# live-smoke.sh -- the hand smoke of 2026-09-08, scripted, one flavor per run.
#
# Everything this checks was measured by hand first, on this rig, on
# noble-gnome (GNOME Shell 46.0), resolute-gnome (50.1), stonking-gnome
# (51.beta / libmutter-51.so.0), resolute-kde (Plasma 6.6 / KWin 6.6.6) and
# resolute-sway (sway 1.11); the write-up of that session is the oracle every
# step here replays.  A step that disagrees with it prints `FAIL <what>` with
# the bytes it actually saw, and the script's exit status is the number of
# FAILs (capped at 125, since a shell status is a byte and 126/127/128+ mean
# other things).  A run that cannot even get a desktop exits 2.
#
#   usage: vm/live-smoke.sh <flavor> [options]
#
#     --name NAME     instance name (default <flavor>-smoke)
#     --reuse         reuse the existing instance and whatever is installed in
#                     it; skips start --fresh and the install phase.  This is
#                     the iteration mode -- gnome46/gnome50/kde66/sway1 already
#                     carry a package -- and NOT the mode a release is measured
#                     in
#     --pkg           install the DISTRIBUTION PACKAGE and nothing else (the
#                     packaging axis).  Which package is decided by the flavor's
#                     `# vmctl-distro:` header, never by this script's own idea
#                     of what the guest is:
#                       ubuntu  release/w11_<ver>_all.deb
#                       fedora  $LIVE_SMOKE_RPMS/*.rpm -- default dist/, where
#                               scripts/build-rpm.sh leaves the three noarch
#                               rpms, and where the CI `rpm` job's artifact is
#                               unpacked
#                       arch    $LIVE_SMOKE_PKG -- default
#                               dist/w11-<ver>-1-any.pkg.tar.zst
#                       nixos   nothing is copied in: the package IS the image,
#                               and phase install asserts the store path and the
#                               specialisation instead
#     --deb           the older spelling of --pkg.  Kept because every note,
#                     log and habit in this tree predates Fedora and Arch
#     --heads N       monitors (default 2; the display and overlap phases need
#                     two, the layout phases do not care)
#     --cpus N        vCPUs (default 2)   } this host runs seven other agents
#     --mem M         guest RAM (default 3G)
#     --boot-timeout S  seconds to wait for the guest's ssh (default 240, or $LIVE_SMOKE_BOOT_TIMEOUT)
#     --phases a,b,c  run only these, in this order (see --list-phases)
#     --skip a,b      run the default list without these
#     --list-phases   print the flavor's default phase list and exit
#     --scale         re-run the display phase with scaling-factor 2 (the
#                     scale axis: 1.0 first, then 2 on the same instance)
#     --keep          leave the instance running at the end (default: stop it,
#                     because the memory rule on this host is ONE VM at a time)
#     --remove        after the phases, remove the package with the flavor's own
#                     package manager and check that every piece of it went: the
#                     files, the autostart symlink, the stamp, both extensions,
#                     and the grant on /dev/uinput.  Only meaningful with --pkg:
#                     the default mode installs the working tree over the
#                     package into /usr/local/bin, which no package manager owns
#
# The default mode (neither --pkg nor --deb) deploys the WORKING TREE over the
# package: scripts/build-pyz.sh into /usr/local/bin the way repro/deploy-to-vm.sh
# does, plus gnome/w11-bridge@w11 over the package's copy,
# because the tree carries fixes the shipped package does not (b7a60f0's
# maximize pair among them).
#
# Results land in vm/live-smoke.out/<flavor>-<date>.log with a screenshot of
# every head per phase next to it in <flavor>-<date>-shots/.
set -euo pipefail

HERE=$(cd -- "$(dirname -- "$0")" && pwd)          # <repo>/vm
REPO=$(dirname "$HERE")
# The rig, or the recorded stand-in vm/live-smoke.d/selftest-offline.sh uses to
# exercise these phases with no VM at all.  Nothing but that self-test may set it.
VM=${LIVE_SMOKE_VMCTL:-$HERE/vmctl}
STEPS=$HERE/live-smoke.d
# One version, read where w11common keeps it, so that the .deb name and the
# NixOS store-path assertion cannot drift from the tree they are measuring.
VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$REPO/w11common/__init__.py" | head -1)
DEB=$REPO/release/w11_${VERSION}_all.deb
#: The two directories the non-Debian packages come out of.  dist/ is where
#: scripts/build-rpm.sh and scripts/build-pkgbuild.sh put them (their own
#: --help says so) and where CI's artifacts are unpacked.
LIVE_SMOKE_RPMS=${LIVE_SMOKE_RPMS:-$REPO/dist}
LIVE_SMOKE_PKG=${LIVE_SMOKE_PKG:-$REPO/dist/w11-${VERSION}-1-any.pkg.tar.zst}

# The usage block is the comment above, printed without its `# `.  Read by line
# CONTENT and not by line NUMBER: every previous edit to this header moved the
# `sed -n '2,47p'` that used to print it and nobody noticed until it printed
# half the code.
usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

# -h/--help is answered BEFORE the flavor is read: it is the one argument that
# is not a flavor, and taking $1 first made `live-smoke.sh --help` print "no
# flavor --help (vm/flavors/--help.yaml)" and exit 2, with the -h arm of the
# option loop below unreachable.
case ${1:-} in -h|--help) usage; exit 0 ;; esac
FLAVOR=${1:-}
[ -n "$FLAVOR" ] || { usage; exit 2; }
shift
NAME=""; MODE=tree; REUSE=0; HEADS=2; CPUS=2; MEM=3G; KEEP=0; SCALE=0; REMOVE=0
# seconds vmctl waits for the guest's ssh: an ISO-installed desktop on a loaded runner has
# taken more than the 240 s default (CI run 34388409584); --boot-timeout or the env knob
BOOT_TIMEOUT=${LIVE_SMOKE_BOOT_TIMEOUT:-240}
ONLY=""; SKIP=""; LIST=0
while [ $# -gt 0 ]; do
    case $1 in
        --name)   NAME=$2; shift 2 ;;
        --reuse)  REUSE=1; shift ;;
        # One mode, two spellings: --deb is what the hand smoke and every note
        # in vm/live-smoke.out/ says, and it now means "the package this
        # flavor's distro installs", which on every Ubuntu flavor is still a
        # .deb.  No count here on purpose -- vm/flavors/ grows every batch.
        --pkg|--deb) MODE=pkg; shift ;;
        --heads)  HEADS=$2; shift 2 ;;
        --cpus)   CPUS=$2; shift 2 ;;
        --mem)    MEM=$2; shift 2 ;;
        --boot-timeout) BOOT_TIMEOUT=$2; shift 2 ;;
        --phases) ONLY=$2; shift 2 ;;
        --skip)   SKIP=$2; shift 2 ;;
        --list-phases) LIST=1; shift ;;
        --scale)  SCALE=1; shift ;;
        --keep)   KEEP=1; shift ;;
        --remove) REMOVE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "live-smoke.sh: unknown option $1" >&2; exit 2 ;;
    esac
done
NAME=${NAME:-$FLAVOR-smoke}
[ -f "$HERE/flavors/$FLAVOR.yaml" ] || { echo "no flavor $FLAVOR (vm/flavors/$FLAVOR.yaml)" >&2; exit 2; }
# The desktop and the distro, from the flavor's own headers -- the same two
# `sed -n 's/^#[[:space:]]*vmctl-<key>:...'` lookups vmctl, selftest.sh and
# ci-golden.sh do, so a new flavor needs no edit here.
DESKTOP=$(sed -n 's/^#[[:space:]]*vmctl-desktop:[[:space:]]*//p' "$HERE/flavors/$FLAVOR.yaml" | head -1)
DESKTOP=${DESKTOP:-gnome}
DISTRO=$(sed -n 's/^#[[:space:]]*vmctl-distro:[[:space:]]*//p' "$HERE/flavors/$FLAVOR.yaml" | head -1)
DISTRO=${DISTRO:-ubuntu}
[ -f "$STEPS/$DESKTOP.sh" ] || { echo "no step file $STEPS/$DESKTOP.sh for desktop $DESKTOP" >&2; exit 2; }

# ------------------------------------------------------- the package axis
# Four packagings, one procedure.  Each of the four functions below answers for
# the flavor's `# vmctl-distro:` and nothing else looks at $DISTRO, so adding a
# distro is four arms and no new branch anywhere in the phases.
# tests/test_live_smoke.py (R14) slices all four and runs them.

# The host-side files to copy in, space separated; empty on NixOS, where there
# is nothing to copy: the module is already in the image (vm/nixos/common.nix).
pkg_files() {
    case "$DISTRO" in
    ubuntu) echo "$DEB" ;;
    # The three noarch rpms of one build, whichever names they carry: the
    # subpackages are named after the extensions, so the names are a glob --
    # but the VERSION is pinned the way the deb and the pkg arms pin theirs.
    # scripts/build-rpm.sh copies every RPMS/noarch/*.rpm into dist/ and never
    # clears it, so a dist/ that still holds the previous build would otherwise
    # hand `dnf install` two versions of each name and fail.
    fedora) ls "$LIVE_SMOKE_RPMS"/*-"$VERSION"-*.rpm 2>/dev/null | tr '\n' ' ' || true ;;
    arch)   echo "$LIVE_SMOKE_PKG" ;;
    nixos)  echo "" ;;
    esac
}

# The root command that installs what pkg_files copied into /tmp.
pkg_install_cmd() {
    case "$DISTRO" in
    ubuntu) echo "cd /tmp && DEBIAN_FRONTEND=noninteractive apt-get install -y ./w11.deb 2>&1" ;;
    # The bridge subpackage is named EXPLICITLY: its `Supplements: gnome-shell`
    # fires only where gnome-shell is installed, and fedora44-sway has none
    # [packaging/rpm/w11.spec].  The overlap subpackage rides along the
    # same way.
    fedora) echo "cd /tmp && dnf install -y ./w11-*.rpm \
./gnome-shell-extension-w11-bridge-*.rpm ./gnome-shell-extension-w11-overlap-*.rpm 2>&1" ;;
    arch)   echo "cd /tmp && pacman -U --noconfirm ./w11-*.pkg.tar.zst 2>&1" ;;
    # Nothing is installed on NixOS: switching INTO the default specialisation
    # is the install, and the image boots into it already.
    nixos)  echo "" ;;
    esac
}

# The root command that takes it away again.
pkg_remove_cmd() {
    case "$DISTRO" in
    ubuntu) echo "DEBIAN_FRONTEND=noninteractive apt-get remove -y w11 2>&1" ;;
    # The subpackages go with it through their `Requires: w11 = %{version}`.
    fedora) echo "dnf remove -y w11 2>&1" ;;
    arch)   echo "pacman -R --noconfirm w11 2>&1" ;;
    # The offline, in-guest analogue of a removal: the other specialisation is
    # the same system with programs.w11 off, and switch-to-configuration
    # reloads the udev rules and swaps the system path in one step -- no
    # network, no rebuild [vm/nixos/common.nix, plan B 1.4].
    nixos)  echo "/run/current-system/specialisation/without-w11/bin/switch-to-configuration test 2>&1" ;;
    esac
}

# Every path the packaging owned, one per line: what `ls -d` must find nothing
# of after the removal.  The libexec path is the one line that differs between
# packagings -- /usr/lib on dpkg and pacman, /usr/libexec on rpm (Fedora's
# guidelines), the system profile on NixOS -- and it is why this is a table.
pkg_left_behind() {
    local ext=/usr/share/gnome-shell/extensions
    case "$DISTRO" in
    ubuntu|arch) echo "/usr/lib/w11" ;;
    fedora)      echo "/usr/libexec/w11" ;;
    nixos)       ext=/run/current-system/sw/share/gnome-shell/extensions ;;
    esac
    case "$DISTRO" in
    nixos) : ;;
    *) echo "/var/lib/w11"
       echo "/etc/xdg/autostart/w11-enable-bridge.desktop" ;;
    esac
    echo "$ext/w11-bridge@w11"
    echo "$ext/w11-overlap@w11"
}

# Copy what pkg_files names into the guest's /tmp, under the names the install
# command above expects.  A FAIL and a nonzero status when a package that
# should be there is not, so the caller says it once and goes on.
pkg_deploy() {
    local f n=0
    for f in $(pkg_files); do
        [ -f "$f" ] || { fail "no package at $f (scripts/build-deb.sh, build-rpm.sh or build-pkgbuild.sh first)"
                         return 1; }
        case "$f" in
        *.deb) "$VM" scp "$NAME" "$f" "$NAME:/tmp/w11.deb" >/dev/null ;;
        *)     "$VM" scp "$NAME" "$f" "$NAME:/tmp/$(basename "$f")" >/dev/null ;;
        esac
        n=$((n + 1))
    done
    if [ "$DISTRO" != nixos ] && [ "$n" = 0 ]; then
        fail "no package for distro $DISTRO (LIVE_SMOKE_RPMS=$LIVE_SMOKE_RPMS, LIVE_SMOKE_PKG=$LIVE_SMOKE_PKG)"
        return 1
    fi
    return 0
}

# The first-install banner, as an extended regex, or empty where the packaging
# deliberately prints none.  dpkg's postinst and pacman's .install carry the
# same paragraph, byte for byte, and its loudest line is this one; the rpm
# carries no banner at all -- Fedora discourages chatty scriptlets and the spec
# ships README.Fedora in %doc instead [recon2/pkg-rpm 292, packaging/rpm/
# w11.spec], so phase_install checks for that file there.
pkg_banner_re() {
    case "$DISTRO" in
    ubuntu|arch) echo "LOG OUT AND BACK IN ONCE" ;;
    *)           echo "" ;;
    esac
}

# The two phases that belong to a DISTRO rather than to a desktop, appended to
# whatever the step file asked for so that no step file has to know what it is
# running on (plan B 2.1).  `--phases` and `--skip` still win over both.
distro_phases() {
    case "$DISTRO" in
    fedora) echo "selinux pkgverify" ;;
    arch)   echo "pkgverify" ;;
    *)      echo "" ;;
    esac
}

# ---------------------------------------------------------------- output
# `--list-phases` answers before any of this: it is a question about the step
# files, and a question should not leave a run directory behind.
if [ "$LIST" = 1 ]; then
    # shellcheck source=live-smoke.d/common.sh
    . "$STEPS/common.sh"
    # shellcheck source=/dev/null
    . "$STEPS/$DESKTOP.sh"
    echo "$FLAVOR ($DESKTOP, $DISTRO): $(echo "$SMOKE_PHASES $(distro_phases)" | xargs)"
    exit 0
fi
STAMP=$(date +%Y%m%d-%H%M%S)
# LIVE_SMOKE_OUT is for the offline self-test, which must not litter the
# real results directory with runs against a recording.
OUTDIR=${LIVE_SMOKE_OUT:-$HERE/live-smoke.out}
SHOTDIR=$OUTDIR/$FLAVOR-$STAMP-shots
LOG=$OUTDIR/$FLAVOR-$STAMP.log
mkdir -p "$SHOTDIR"
exec > >(tee -a "$LOG") 2>&1

T0=$(date +%s)
FAILS=0; PASSES=0; PHASE=-
elapsed() { echo $(( $(date +%s) - T0 )); }
step()  { echo; echo "== [$(elapsed)s] $*"; }
pass()  { PASSES=$((PASSES + 1)); echo "PASS $PHASE: $*"; }
fail()  { FAILS=$((FAILS + 1));  echo "FAIL $PHASE: $*"; }
note()  { echo "     $*"; }
# The evidence line for a failure: the bytes we saw, one line, clipped -- a
# GNOME extension error can be a screenful and the log is meant to be read.
ev()    { printf '%s' "$*" | tr '\n' '|' | cut -c1-400; }

# want <what> <extended-regex> <text...>  -- PASS when the text matches
want()    { local w=$1 re=$2; shift 2
            if printf '%s\n' "$*" | grep -Eq -- "$re"; then pass "$w"
            else fail "$w [want /$re/, got: $(ev "$*")]"; fi; }
# wantnot <what> <extended-regex> <text...>
wantnot() { local w=$1 re=$2; shift 2
            if printf '%s\n' "$*" | grep -Eq -- "$re"; then fail "$w [/$re/ present in: $(ev "$*")]"
            else pass "$w"; fi; }
# same <what> <expected> <actual>  -- byte equality, which is what most of the
# measurements are (`100,100 800x600`, `(true,)`, a typed string read back)
same()    { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 [want '$2', got '$(ev "$3")']"; fi; }
ok()      { if [ "$2" = 0 ]; then pass "$1"; else fail "$1 [exit $2]"; fi; }
# A check for behaviour a fix in flight is going to add: it counts as neither a
# pass nor a fail, so an unfinished fix cannot turn a smoke run red, and the day
# it lands the run says XPASS and the line here becomes a plain `want`.
xwant()   { local w=$1 re=$2; shift 2
            if printf '%s\n' "$*" | grep -Eq -- "$re"
            then echo "XPASS $PHASE: $w (the fix has landed: promote this to want)"
            else echo "XFAIL $PHASE: $w (not in the tree yet) [$(ev "$*")]"; fi; }

# ---------------------------------------------------------------- the guest
# Never let a guest command's own status abort the run: a FAIL is a result, not
# an accident.  Callers that care take the status from `$?` after `|| st=$?`.
# As user test, in the session's environment, with W11_PROXY pinned: the smoke's window
# and wm phases measure the CLONES, and on a Wayland flavor with the originals installed
# the wrapper would otherwise hand every one of them to the original through the proxy
# (measured by batch 9 of issue 1). phase_proxy names DISPLAY and W11_PROXY on its own
# commands, and SMOKE_PROXY=always is the knob for a phase that wants the wrapper.
guest()  { "$VM" user "$NAME" -- env W11_PROXY="${SMOKE_PROXY:-never}" sh -c "$1" 2>&1; }
# as root, no session env -- and W11_PROXY pinned for the same reason as guest(): the root
# phase measures the CLONES finding the seated session, not the wrapper (which, as root with
# no cookie of its own for the proxy, is a NOT YET of its own: docs/XW11.md, What differs)
root()   { "$VM" ssh  "$NAME" -- env W11_PROXY="${SMOKE_PROXY:-never}" sh -c "$1" 2>&1; }
guestq() { "$VM" user "$NAME" -- env W11_PROXY="${SMOKE_PROXY:-never}" sh -c "$1" >/dev/null 2>&1; }   # guest(), quiet
shot()   { "$VM" shot "$NAME" --all "$SHOTDIR/$1" >/dev/null 2>&1 && note "shots: $SHOTDIR/$1-*.png" || true; }

# ---------------------------------------------------------------- steps
# shellcheck source=live-smoke.d/common.sh
. "$STEPS/common.sh"
# shellcheck source=/dev/null
. "$STEPS/$DESKTOP.sh"

run_phases=$(echo "$SMOKE_PHASES $(distro_phases)" | xargs)
[ -n "$ONLY" ] && run_phases=$(echo "$ONLY" | tr ',' ' ')
for s in $(echo "$SKIP" | tr ',' ' '); do
    run_phases=$(echo "$run_phases" | tr ' ' '\n' | grep -vx "$s" | tr '\n' ' ')
done

echo "live-smoke $FLAVOR ($DESKTOP, $DISTRO) instance $NAME," \
     "$(if [ $REUSE = 1 ]; then echo reuse; else echo fresh; fi)," \
     "$(if [ $MODE = pkg ]; then echo "$(pkg_files) only"; else echo 'working tree over the package'; fi)"
echo "phases: $run_phases"
echo "log:    $LOG"

# ---------------------------------------------------------------- boot
# `vmctl start` gives up when ONE ssh probe takes longer than 20 s (vmctl's
# wait_ssh caps each probe there and turns the timeout into a Fail), which on
# this host -- seven agents, two vCPU for the guest -- happens on a cold boot
# without the guest being in any trouble.  So a nonzero start is not the end of
# it: if QEMU is up, wait for ssh here and go on.
# The same again for `vmctl session`, whose probe is a 30 s ssh: a timeout there
# is the host being busy, not the desktop being absent, and `vmctl user -- true`
# answering is the proof of that.
wait_session() {
    local i
    for i in 1 2 3 4; do
        if "$VM" session "$NAME" --timeout 240; then return 0; fi
        if "$VM" user "$NAME" -- true >/dev/null 2>&1; then
            echo "     (vmctl session's probe timed out under load; the session answers, going on)"
            return 0
        fi
    done
    return 1
}
wait_ssh() {
    local i
    for i in $(seq 1 72); do          # 6 minutes
        if "$VM" ssh "$NAME" -- true >/dev/null 2>&1; then return 0; fi
        sleep 5
    done
    return 1
}
if [ "$REUSE" = 1 ]; then
    step "vmctl start $NAME (reuse: the overlay and whatever is installed in it)"
    "$VM" start "$NAME" --cpus "$CPUS" --mem "$MEM" --timeout "$BOOT_TIMEOUT" >/dev/null || true
else
    step "vmctl start $NAME --flavor $FLAVOR --heads $HEADS --fresh"
    "$VM" start "$NAME" --flavor "$FLAVOR" --heads "$HEADS" --cpus "$CPUS" --mem "$MEM" --fresh \
        --timeout "$BOOT_TIMEOUT" >/dev/null || true
fi
wait_ssh || { echo "cannot reach $NAME over ssh"; exit 2; }
step "vmctl session $NAME (autologin; GDM's fires once per boot)"
wait_session || { echo "no graphical session"; exit 2; }
# The native display tool of this desktop, as a script the guest runs: every
# wxrandr apply below is checked against it and never against wxrandr itself.
# It is copied into the seated user's HOME (install_oracle, common.sh) rather
# than left in /tmp, because /tmp does not survive a reboot on 24.04 and this
# smoke reboots up to three times.
install_oracle
shot 00-session

cleanup() {
    if [ "$KEEP" = 1 ]; then
        echo "(--keep: $NAME left running -- vm/vmctl stop $NAME when you are done; ONE VM at a time on this host)"
    else
        "$VM" stop "$NAME" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

for p in $run_phases; do
    if ! declare -F "phase_$p" >/dev/null; then
        echo "live-smoke.sh: no phase_$p for $DESKTOP" >&2; FAILS=$((FAILS + 1)); continue
    fi
    PHASE=$p
    step "phase $p"
    "phase_$p" || note "(phase $p returned $? -- checks above stand)"
    shot "$p"
done
PHASE=-

# ------------------------------------------------------- the removal, last
# Removing the package is the other half of the packaging claim and the half
# nobody ever runs: README.Debian says removing it puts /dev/uinput back to
# root:root 0600 with no ACL, and until this existed that sentence had been
# checked by reading debian/w11.postrm.  It runs after every phase
# because it destroys the installation the phases measure, and only under
# --pkg: the default mode puts the tree's zipapps in /usr/local/bin, which no
# package manager owns and none will take away, so a removal there would report
# files left behind that were never the package's.
#
# What it asserts is what the hand smoke measured on resolute-gnome after
# `apt-get remove -y w11`: nothing of ours under the packaging's own
# libexec directory, no autostart entry, no /var/lib/w11 stamp, neither
# extension directory, /dev/uinput back to root:root 0600 with no ACL entry and
# no uaccess tag -- and, because the remove drops the stamp, a re-install
# printing the first-install banner all over again.  The four packagings differ
# in exactly two of those: which paths they owned (pkg_left_behind) and whether
# they print a banner at all (pkg_banner_re).  The /dev/uinput half is
# identical everywhere and is the point: the rpm's %postun and pacman's
# post_remove run debian's revoke sequence token for token
# [recon2/pkg-rpm, recon2/pkg-arch].
phase_remove() {
    local out st=0 cmd defsys=""
    # NixOS only: the system we are about to LEAVE, resolved before we leave it.
    # Activation ends in `ln -sfn "$(readlink -f "$systemConfig")"
    # /run/current-system` and runs that line for `test` exactly as for `switch`
    # [nixpkgs nixos/modules/system/activation/activation-script.nix:79, read in
    # the local store 2026-09-08], so the moment the switch into
    # without-w11 lands, /run/current-system IS that child -- and a
    # child carries no `specialisation/` of its own, which is the very fact
    # phase_install asserts.  Switching "back" through /run/current-system would
    # re-activate the child and leave the tools off PATH.  /run/booted-system is
    # the fallback because stage 2 sets it once and activation never touches it.
    if [ "$DISTRO" = nixos ]; then
        defsys=$(guest 'readlink -f /run/current-system' | tr -d ' \r' || true)
        [ -n "$defsys" ] || defsys=/run/booted-system
        note "the default system to switch back into is $defsys"
    fi
    cmd=$(pkg_remove_cmd)
    out=$(root "$cmd") || st=$?
    ok "$cmd" "$st"
    # Every `$( ... )` below ends in `|| true`, and that is not decoration:
    # `root` hands back the guest command's own status, `ls -d` with nothing to
    # list exits 2, `grep -l` with no match exits 1, and `command -v` exits 1
    # for a name that is gone -- which is the PASSING case for all three.  Under
    # the driver's `set -euo pipefail` the assignment would then abort the phase
    # before its own `same` ran, so a clean removal used to be reported as a
    # phase that "returned 1" with none of its six checks printed.
    local left
    left=$(root "ls -d $(pkg_left_behind | tr '\n' ' ') 2>/dev/null | tr '\n' ' '" || true)
    same "every path the package owned is gone" "" "$(printf '%s' "$left" | tr -d ' \r')"
    # The six names go with it: /usr/bin is the package manager's, so
    # `command -v` finding one afterwards is a leftover (or a pip install
    # nobody mentioned).
    same "no tool of ours is on PATH any more" "" \
         "$(guest 'for t in wdotool wwmctl wxprop wxrandr warandr wmirror; do command -v $t; done 2>/dev/null' \
              | tr -d ' \n\r' || true)"
    # The node: the removal scriptlet removes the ACL entry rather than masking
    # it, because udev's uaccess builtin only ever ADDS entries and a left-over
    # one would make the next install a no-op and leave input broken.
    local mode acls
    mode=$(root "stat -c '%U:%G %a' /dev/uinput 2>/dev/null" | tr -d '\r' || true)
    acls=$(root 'getfacl -p /dev/uinput 2>/dev/null' | grep -c '^user:[^:][^:]*:' || true)
    if [ "$DISTRO" = nixos ]; then
        # NixOS has no removal scriptlet: switching into a configuration without the
        # module drops the rule file and the uaccess tag, and nothing resets the node
        # (measured on nixos-sway, CI run 34360974165: root:root 660 and user:test:rw-
        # left behind until the next boot). udev's uaccess builtin only ever ADDS an
        # entry, so the route is AGENTS.md route 4, a rule in the base system that
        # resets a node whose uaccess tag went away, or a reboot.
        xwant "/dev/uinput is back to root:root 0600 (until NixOS resets a node whose module left: route 4, a base udev rule, or the next boot)" \
              "^root:root 600$" "$mode"
        xwant "no ACL entry is left on /dev/uinput (until NixOS resets a node whose module left: route 4, a base udev rule, or the next boot)" \
              "^0$" "$acls"
    else
        same "/dev/uinput is back to root:root 0600" "root:root 600" "$mode"
        same "no ACL entry is left on /dev/uinput" "0" "${acls:-0}"
    fi
    same "no uaccess tag is left for the node" "" \
         "$(root "grep -l uinput /run/udev/tags/uaccess/* 2>/dev/null" | tr -d ' \n\r' || true)"
    # dh_python3 byte-compiles into dist-packages and nowhere else; a
    # __pycache__ under the libexec directory or /usr/bin is py3compile having
    # been pointed at the wrong tree, and it outlives the remove because dpkg
    # does not own it.  %pyproject_save_files and the PKGBUILD's `python -m
    # installer` have the same failure mode, so the check is not Debian's alone.
    same "byte-compilation left no __pycache__ outside dist-packages" "" \
         "$(root "find $(pkg_left_behind | head -1) /usr/bin -name __pycache__ 2>/dev/null" \
              | tr -d ' \n\r' || true)"
    # Putting it back.  On NixOS that is the same switch in the other direction,
    # through the store path captured at the top of the phase and NOT through
    # /run/current-system, which the removal has just repointed at the child.
    # There is no banner and no package to copy either, so this arm returns
    # before pkg_deploy; without it the phase would run `root ""`, get 0, and
    # call that a re-install.
    if [ "$DISTRO" = nixos ]; then
        st=0; out=$(root "$defsys/bin/switch-to-configuration test 2>&1") || st=$?
        ok "switching back into the default specialisation" "$st"
        want "the tools are on PATH again" "/run/current-system/sw/bin/wdotool" \
             "$(guest 'command -v wdotool' | tr -d ' \r' || true)"
        return 0
    fi
    # A remove takes the stamp with it, so the next install is a first install
    # and owes the user the relogin sentence again -- where the packaging prints
    # one at all.
    pkg_deploy || return 0
    st=0; out=$(root "$(pkg_install_cmd)") || st=$?
    ok "installing again" "$st"
    local banner; banner=$(pkg_banner_re)
    if [ -n "$banner" ]; then
        want "installing again prints the first-install banner again" "$banner" "$out"
    else
        note "($DISTRO's packaging prints no banner; the rpm's advice went into README.Fedora, nothing to re-read)"
    fi
}

if [ "$REMOVE" = 1 ]; then
    if [ "$MODE" != pkg ]; then
        note "--remove needs --pkg (the tree's zipapps in /usr/local/bin belong to no package manager); skipped"
    else
        PHASE=remove
        step "phase remove"
        phase_remove || note "(phase remove returned $? -- checks above stand)"
        shot remove
        PHASE=-
    fi
fi

step "done: $PASSES pass, $FAILS fail"
echo "log:  $LOG"
echo "shots: $SHOTDIR"
if [ "$FAILS" -gt 125 ]; then exit 125; fi
exit "$FAILS"
