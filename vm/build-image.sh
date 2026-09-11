#!/bin/bash
# vmctl guest-side golden-image build.  NOT run on the host: vmctl embeds this
# file into the flavor's cloud-init user-data (write_files -> /usr/local/sbin/
# vmctl-build) and cloud-init runs it once, as root, inside the build VM.
# Progress lines ("vmctl-build: ...") and the final markers go to the serial
# console so `vmctl build` can follow them and decide success.
#
# Three layers, because the rig is no longer Ubuntu-only:
#   pkg_*      one implementation per package manager, chosen from /etc/os-release
#              ID (ID_LIKE as the fallback).  The yaml never says which: the guest
#              knows what it is, so a flavor header cannot lie to it.
#   dm_*       one per display manager, taking the session name or the command.
#   desktop_*  the quiet-desktop settings, first-run suppression and the per-head
#              layout contract (head 0 = Virtual-1 at (0,0)) of one desktop.
# The `case "$DESKTOP"` at the bottom is a dispatch table with one line per
# `# vmctl-desktop:` token of vm/vmctl's DESKTOPS, and nothing else
# (tests/test_vm_scripts.py R04 pins the two against each other).
#
# Every file the dm_*/desktop_* layers write is remembered for relabel(): by wcat
# (which writes and records in one) or, for the three configs built out of a group
# redirection, by a written() call beside it.  On Fedora SELinux is Enforcing and a
# file written by cloud-init's runcmd carries whatever label that process had
# [recon2/fedora, recon2/pkg-rpm].
# $VMCTL_ROOT prefixes every path a dm_*/desktop_* touches; it is empty in the
# guest and a temp tree in the tests (R03).
#
# Inputs (written by the flavor yaml to /etc/vmctl-build.env):
#   FLAVOR       flavor name (also the hostname)
#   DESKTOP      a DESKTOPS key of vm/vmctl (default gnome): which display manager
#                and desktop set-up below applies
#   DESKTOP_PKG  the desktop metapackage(s), e.g. ubuntu-desktop, or
#                "kubuntu-desktop plasma-workspace-wayland"; on dnf a word ending
#                in -environment is a comps environment id, not a package
#   EXTRA_PKGS   test-support packages (real X tools for parity oracles)

. /etc/vmctl-build.env
: "${DESKTOP:=gnome}"
: "${VMCTL_ROOT:=}"
LOG=/var/log/vmctl-build.log
exec >>"$LOG" 2>&1
# The serial console is the channel to `vmctl build` on the host.  A getty runs
# on ttyS0 and hangs the line up when it (re)starts, which kills every
# long-lived fd on it (a tee to /dev/ttyS0 died with EIO), so: stop the getty
# and open /dev/ttyS0 afresh for every write.  Everything else goes to $LOG.
systemctl stop serial-getty@ttyS0.service 2>/dev/null || true
con() { printf '%s\n' "$*" > /dev/ttyS0 2>/dev/null || true; }
say() { echo "vmctl-build: $*"; con "vmctl-build: $*"; }
fail() {
    say "FAILED: $*"
    { echo "vmctl-build: last lines of $LOG:"; tail -n 30 "$LOG"; echo VMCTL-BUILD-FAIL; } > /dev/ttyS0 2>/dev/null || true
    exit 1
}
trap 'fail "line $LINENO: $BASH_COMMAND"' ERR
set -eE -o pipefail

TESTHOME=$VMCTL_ROOT/home/test
WRITTEN=${WRITTEN:-$VMCTL_ROOT/var/lib/vmctl/written.list}

written() {   # remember a path for relabel(); one per line, duplicates fine
    mkdir -p "$(dirname "$WRITTEN")"
    printf '%s\n' "$@" >> "$WRITTEN"
}
wcat() {   # `wcat PATH <<EOF` -- cat > PATH, remembered
    written "$1"; cat > "$1"
}
wdir() {   # `wdir [-o test -g test -m 0700] PATH...` -- install -d, remembered
    install -d "$@"
    local p
    for p in "$@"; do case "$p" in /*) written "$p" ;; esac; done
}
relabel() {   # SELinux, dnf only: restore the policy label on everything written above
    [ "$PKG" = dnf ] || return 0
    command -v restorecon >/dev/null 2>&1 || { say "no restorecon; skipping relabel"; return 0; }
    [ -f "$WRITTEN" ] || return 0
    local p
    sort -u "$WRITTEN" | while read -r p; do
        [ -e "$p" ] && restorecon -R "$p" 2>/dev/null || true
    done
    say "restorecon -R over $(sort -u "$WRITTEN" | wc -l) written paths"
}

# ---------------------------------------------------------------- layer 1: packages

# NEEDRESTART_MODE=l: needrestart must not restart services mid-build -- restarting
# systemd-networkd after the desktop pulled in NetworkManager left the guest with no
# IP at all (24.04).  The image is powered off right after the build anyway.
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l APT_LISTCHANGES_FRONTEND=none UCF_FORCE_CONFFOLD=1
APT="apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"

detect_pkg() {   # the package manager, from /etc/os-release ID, ID_LIKE as fallback
    local id like
    id=$(. /etc/os-release 2>/dev/null; echo "${ID:-}")
    like=$(. /etc/os-release 2>/dev/null; echo "${ID_LIKE:-}")
    case " $id $like " in
        *" ubuntu "*|*" debian "*) PKG=apt;    NET_HOST=archive.ubuntu.com;        NET_FIX="netplan apply" ;;
        *" fedora "*|*" rhel "*)   PKG=dnf;    NET_HOST=mirrors.fedoraproject.org; NET_FIX=true ;;
        *" arch "*)                PKG=pacman; NET_HOST=geo.mirror.pkgbuild.com;   NET_FIX=true ;;
        *) fail "unknown distro ID=$id ID_LIKE=$like (want ubuntu|debian|fedora|rhel|arch)" ;;
    esac
}
wait_net() {   # the network can vanish mid-build (networkd <-> NetworkManager hand-over)
    local i
    for i in $(seq 1 90); do
        getent hosts "$NET_HOST" >/dev/null 2>&1 && return 0
        if [ "$i" = 15 ] || [ "$i" = 45 ]; then
            say "network is down; $NET_FIX"
            $NET_FIX >/dev/null 2>&1 || true
        fi
        sleep 2
    done
    return 1
}
pkg_retry() {   # five attempts, 15 s apart, the network re-checked before each
    local n
    for n in 1 2 3 4 5; do
        wait_net || say "warning: $NET_HOST does not resolve"
        "$@" && return 0
        say "$* failed (attempt $n/5); retrying in 15 s"
        sleep 15
        pkg_update || true
    done
    return 1
}
pkg_update() {
    case "$PKG" in
    apt)    $APT update ;;
    dnf)    dnf -y makecache ;;
    # the keyring first, on its own -Sy: a cloud image weeks old has keys older
    # than the packages it is about to fetch [recon2/arch]
    pacman) pacman -Sy --noconfirm --needed archlinux-keyring ;;
    esac
}
pkg_install() {
    case "$PKG" in
    apt)    pkg_retry $APT install "$@" ;;
    dnf)    pkg_retry dnf -y install "$@" ;;
    pacman) pkg_retry pacman -S --noconfirm --needed "$@" ;;
    esac
}
pkg_desktop() {   # DESKTOP_PKG: on dnf a word ending in -environment is a comps id
    local g="" p=""
    if [ "$PKG" = dnf ]; then
        local w
        for w in "$@"; do
            case "$w" in *-environment) g="$g $w" ;; *) p="$p $w" ;; esac
        done
        [ -n "$g" ] && { say "dnf group install$g"; pkg_retry dnf -y group install $g; }
        [ -n "$p" ] && pkg_install $p
        return 0
    fi
    pkg_install "$@"
}
pkg_seed_dm() {   # answer the display manager question before any package asks it
    # A desktop metapackage drags in a second display manager (xubuntu-desktop pulls
    # gdm3 through gnome-shell on 26.04), and the first one to configure itself would
    # become THE display manager.  debconf is apt's alone; dnf and pacman have no
    # such question and pick the DM from the enabled unit.
    [ "$PKG" = apt ] || return 0
    case "$1" in
    dm_lightdm) echo "lightdm shared/default-x-display-manager select lightdm" | debconf-set-selections ;;
    dm_sddm|dm_plasma) echo "sddm shared/default-x-display-manager select sddm" | debconf-set-selections ;;
    dm_gdm)     echo "gdm3 shared/default-x-display-manager select gdm3" | debconf-set-selections ;;
    esac
}
pkg_manifest() {   # package NAMES only, sorted: what `vmctl build` reads off the serial
    case "$PKG" in
    apt)    dpkg-query -W -f='${binary:Package}\n' | sort ;;
    # Fedora names carry uppercase (NetworkManager, ModemManager) and pacman -Q prints
    # "name version", so both are normalised here and not on the host [recon2/pkg-rpm]
    dnf)    rpm -qa --qf '%{NAME}\n' | sort -u ;;
    pacman) pacman -Qq | sort ;;
    esac
}
pkg_quiet() {   # no automatic updates on a golden image
    case "$PKG" in
    apt)
        wcat "$VMCTL_ROOT/etc/apt/apt.conf.d/20auto-upgrades" <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
APT::Periodic::Unattended-Upgrade "0";
EOF
        systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
        systemctl mask apt-daily.service apt-daily-upgrade.service >/dev/null 2>&1 || true
        systemctl disable --now unattended-upgrades.service 2>/dev/null || true
        systemctl disable --now motd-news.timer 2>/dev/null || true
        snap refresh --hold >/dev/null 2>&1 || true ;;
    # dnf5-makecache.timer is the analogue: it wakes up and refreshes metadata on a
    # machine whose whole point is not to change [recon2/fedora]
    dnf)    systemctl mask dnf5-makecache.timer dnf-makecache.timer >/dev/null 2>&1 || true ;;
    # nothing to disable: an Arch cloud image has no unattended upgrades at all
    pacman) : ;;
    esac
}
pkg_cleanup() {
    case "$PKG" in
    apt)    $APT autoremove; apt-get clean ;;
    dnf)    dnf -y autoremove || true; dnf clean all ;;
    pacman) pacman -Sc --noconfirm >/dev/null 2>&1 || true ;;
    esac
}

# ---------------------------------------------------------------- layer 2: display managers

accountsservice() {   # `accountsservice <Session> [XSession]` for user test
    wdir -m 0755 "$VMCTL_ROOT/var/lib/AccountsService/users"
    { printf '[User]\nSession=%s\n' "$1"
      [ -n "${2:-}" ] && printf 'XSession=%s\n' "$2"
      printf 'SystemAccount=false\n'
    } > "$VMCTL_ROOT/var/lib/AccountsService/users/test"
    written "$VMCTL_ROOT/var/lib/AccountsService/users/test"
}
no_session_ambiguity() {   # SDDM resolves an autologin session NAME against
    # /usr/share/wayland-sessions first (Display::attemptAutologin), so a name in both
    # directories would quietly start the other session type.  Refuse to build that image.
    [ -f "$VMCTL_ROOT/usr/share/xsessions/$1" ] && [ -f "$VMCTL_ROOT/usr/share/wayland-sessions/$1" ] && \
        fail "$1 exists in /usr/share/xsessions AND /usr/share/wayland-sessions;" \
             "the display manager would pick one of them at random"
    return 0
}
dm_enable() {   # dm_enable <unit> <binary> -- make <unit> THE display manager of the image
    # The debconf answer (pkg_seed_dm) is a hint the postinsts may or may not act
    # on: gdm3 46's postinst leaves gdm3.service DISABLED when the desktop
    # metapackage pulls it in non-interactively (measured 2026-09-08: a
    # noble-gnome built by this script booted to a text console, no
    # /etc/systemd/system/display-manager.service, no /etc/X11/default-display-
    # manager; `systemctl enable gdm3` alone brought the autologin up), and dnf
    # and pacman have no such question at all -- their DM is whatever unit is
    # enabled.  So every dm_* ends here: the Debian pointer file where Debian
    # reads one, the other DMs' alias dropped, ours enabled, and the alias that
    # results checked, because an image that boots into the wrong desktop (or
    # none) is forty minutes nobody gets back.
    local unit=$1 bin=$2 other dmlink uf want
    # the unit file, wherever this distro keeps it; on Ubuntu gdm3.service is itself a
    # symlink to gdm.service, so what the alias resolves to is compared resolved
    for uf in usr/lib/systemd/system/$unit.service lib/systemd/system/$unit.service; do
        [ -f "$VMCTL_ROOT/$uf" ] && break
    done
    [ -f "$VMCTL_ROOT/$uf" ] || fail "no unit file for $unit under /usr/lib/systemd/system or /lib/systemd/system"
    want=$(readlink -f "$VMCTL_ROOT/$uf")
    if [ "$PKG" = apt ]; then
        wdir "$VMCTL_ROOT/etc/X11"
        echo "$bin" > "$VMCTL_ROOT/etc/X11/default-display-manager"
        written "$VMCTL_ROOT/etc/X11/default-display-manager"
    fi
    for other in gdm gdm3 sddm plasmalogin lightdm; do
        [ "$other" = "$unit" ] || systemctl disable "$other" 2>/dev/null || true   # drops its display-manager.service alias
    done
    # ...and whatever else already owns the link, by reading it rather than by guessing.  Measured on
    # fedora44-cosmic 2026-09-09: `dnf group install cosmic-desktop-environment` (1,147 packages) puts
    # cosmic-greeter -- which IS greetd, `ExecStart=greetd --config /etc/greetd/cosmic-greeter.toml` --
    # on the image and SELF-ENABLES it mid-transaction, and Fedora's greetd.service has no `WantedBy=`
    # at all, so `systemctl enable greetd` is exactly that one alias symlink and nothing else.  It then
    # fails: "File '/etc/systemd/system/display-manager.service' already exists and is a symlink to
    # /usr/lib/systemd/system/cosmic-greeter.service", and the whole flavor dies at line 410.  Verified
    # by hand in that guest: disable the incumbent, enable ours, and the reboot gave greetd active,
    # display-manager.service active, a seated `test seat0 tty1 Service=greetd` session, cosmic-comp
    # holding head 0's scanout and the head painting at stddev 0.131454 [requests-batch-2.md, batch 14].
    dmlink=$(readlink -f "$VMCTL_ROOT/etc/systemd/system/display-manager.service" 2>/dev/null || true)
    if [ -n "$dmlink" ] && [ "$dmlink" != "$want" ]; then
        other=$(basename "$dmlink" .service)
        say "display-manager.service is $other.service; disabling it so $unit can take the alias"
        systemctl disable "$other" 2>/dev/null || rm -f "$VMCTL_ROOT/etc/systemd/system/display-manager.service"
    fi
    systemctl enable "$unit" || fail "systemctl enable $unit failed"
    if [ ! -L "$VMCTL_ROOT/etc/systemd/system/display-manager.service" ]; then
        # Debian's gdm3.service has no [Install] section at all: `systemctl enable gdm3`
        # answers "has no installation config", exits 0 and links nothing (CI run
        # 34286867525, every GNOME rig), because on Debian the gdm3 postinst makes the
        # display-manager.service link itself from /etc/X11/default-display-manager.
        # lightdm, sddm, greetd and upstream gdm carry Alias=display-manager.service and
        # never get here.  Made the way the postinst makes it.
        say "$unit.service has no [Install] section; linking display-manager.service -> /$uf by hand"
        wdir "$VMCTL_ROOT/etc/systemd/system"
        ln -sfn "$VMCTL_ROOT/$uf" "$VMCTL_ROOT/etc/systemd/system/display-manager.service"
        systemctl daemon-reload 2>/dev/null || true
    fi
    dmlink=$(readlink -f "$VMCTL_ROOT/etc/systemd/system/display-manager.service")
    [ "$dmlink" = "$want" ] || fail "display-manager.service is not $unit: ${dmlink:-missing} (want $want)"
    say "$unit is the display manager (display-manager.service -> $dmlink)"
}
dm_gdm() {   # dm_gdm wayland|x11 -- resolve the GNOME session file, then the DM
    # The session NAME is Ubuntu's ubuntu/ubuntu-xorg (gnome-session's ubuntu-session
    # deb) and plain gnome/gnome-xorg everywhere else: Fedora 44 ships exactly
    # gnome.desktop, cosmic.desktop and sway.desktop in /usr/share/wayland-sessions and
    # its measured autologin row is `Session=gnome` [recon2/fedora], and Arch's
    # gnome-session ships gnome.desktop.  Resolved off the disk of the image being
    # built, the way dm_plasma resolves its four Plasma names -- the flavor's
    # `# vmctl-distro:` header is a HOST-side fact the guest never reads.
    local kind=${1:-wayland} sess="" dir=etc/gdm3 wl=true f
    local sdir=usr/share/wayland-sessions names="ubuntu gnome"
    if [ "$kind" = x11 ]; then
        wl=false; sdir=usr/share/xsessions; names="ubuntu-xorg gnome-xorg"
    fi
    for f in $names; do
        [ -f "$VMCTL_ROOT/$sdir/$f.desktop" ] && { sess=$f; break; }
    done
    [ -n "$sess" ] || fail "no GNOME $kind session file in /$sdir: $(ls "$VMCTL_ROOT/$sdir" 2>/dev/null | tr '\n' ' ')"
    [ -d "$VMCTL_ROOT/etc/gdm" ] && [ ! -d "$VMCTL_ROOT/etc/gdm3" ] && dir=etc/gdm
    say "GDM: autologin of user test into $sess (WaylandEnable=$wl), /$dir/custom.conf"
    wdir "$VMCTL_ROOT/$dir"
    wcat "$VMCTL_ROOT/$dir/custom.conf" <<EOF
# Written by vmctl (vm/build-image.sh): autologin the test user into the
# $kind session.  WaylandEnable=$wl: true keeps GDM off Xorg, false is the
# only way to reach an Xorg session at all (gdm3 46.2 honours it).
[daemon]
AutomaticLoginEnable=true
AutomaticLogin=test
WaylandEnable=$wl
InitialSetupEnable=false

[security]

[xdmcp]

[chooser]

[debug]
EOF
    [ "$kind" = x11 ] && no_session_ambiguity "$sess.desktop"
    accountsservice "$sess" "$sess"
    # Ubuntu's package is gdm3 (unit gdm3.service, /usr/sbin/gdm3); Fedora's and Arch's is gdm
    if [ "$dir" = etc/gdm3 ]; then dm_enable gdm3 /usr/sbin/gdm3; else dm_enable gdm /usr/sbin/gdm; fi
}
dm_sddm() {   # dm_sddm <session.desktop>
    local sess=$1
    no_session_ambiguity "$sess"
    say "SDDM: autologin of user test into $sess"
    wdir "$VMCTL_ROOT/etc/sddm.conf.d"
    wcat "$VMCTL_ROOT/etc/sddm.conf.d/autologin.conf" <<EOF
# Written by vmctl (vm/build-image.sh): autologin the test user into $sess.
# SDDM 0.20 still defaults DisplayServer=x11 for its greeter; the session itself
# is started from the directory the name was found in, Wayland first.
[Autologin]
User=test
Session=$sess
Relogin=false
EOF
    accountsservice "${sess%.desktop}"
    dm_enable sddm /usr/bin/sddm
}
dm_plasmalogin() {   # dm_plasmalogin <session.desktop>
    # Fedora 44 replaced SDDM with plasma-login-manager in every KDE variant (the
    # kde-desktop comps group carries plasma-login-manager and no sddm); the three
    # keys are the ones in the rpm's own /etc/plasmalogin.conf template, read from
    # the package and not measured live -- the first fedora44-kde build is the
    # measurement [recon2/fedora].
    local sess=$1
    no_session_ambiguity "$sess"
    say "plasma-login-manager: autologin of user test into $sess"
    wdir "$VMCTL_ROOT/etc/plasmalogin.conf.d"
    wcat "$VMCTL_ROOT/etc/plasmalogin.conf.d/autologin.conf" <<EOF
# Written by vmctl (vm/build-image.sh).
[Autologin]
User=test
Session=$sess
Relogin=false
EOF
    accountsservice "${sess%.desktop}"
    dm_enable plasmalogin /usr/bin/plasmalogin
}
dm_plasma() {   # dm_plasma wayland|x11 -- resolve the Plasma session file, then the DM
    # Plasma 5.27 (24.04): the Wayland session is plasmawayland.desktop from
    # plasma-workspace-wayland; Plasma 6 (26.04): plasma.desktop from
    # plasma-session-wayland.  The X11 session is plasma.desktop from plasma-workspace
    # on 5.27 and plasmax11.desktop from plasma-workspace-x11 on 6.
    local kind=$1 sess="" f
    if [ "$kind" = x11 ]; then
        for f in plasmax11.desktop plasma.desktop; do
            [ -f "$VMCTL_ROOT/usr/share/xsessions/$f" ] && { sess=$f; break; }
        done
        [ -n "$sess" ] || fail "no Plasma X11 session file in /usr/share/xsessions: $(ls "$VMCTL_ROOT/usr/share/xsessions" 2>/dev/null | tr '\n' ' ')"
    else
        for f in plasma.desktop plasmawayland.desktop; do
            [ -f "$VMCTL_ROOT/usr/share/wayland-sessions/$f" ] && { sess=$f; break; }
        done
        [ -n "$sess" ] || fail "no Plasma Wayland session file in /usr/share/wayland-sessions: $(ls "$VMCTL_ROOT/usr/share/wayland-sessions" 2>/dev/null | tr '\n' ' ')"
    fi
    if [ -d "$VMCTL_ROOT/etc/plasmalogin.conf.d" ] || [ -f "$VMCTL_ROOT/etc/plasmalogin.conf" ] \
       || [ -x "$VMCTL_ROOT/usr/bin/plasma-login-manager" ]; then
        dm_plasmalogin "$sess"
    else
        dm_sddm "$sess"
    fi
}
dm_greetd() {   # dm_greetd <command...>
    # $* and not $1: the dispatch table's river line is a THREE-word command
    # (`river -c /usr/local/bin/vmctl-river-init`) and reaches here word-split by
    # the unquoted `$DM $DM_ARGS` at the bottom of this file -- which dm_gdm needs,
    # its second word being the session KIND.  A `local cmd=$1` here truncated
    # river's command to `river`, i.e. a session with no init, no tinyrwm and no
    # layout helper.
    local cmd=$*
    say "greetd: autologin of user test into '$cmd' on vt 1 (logind seat session), graphical.target"
    [ -x "$VMCTL_ROOT/usr/sbin/greetd" ] || [ -x "$VMCTL_ROOT/usr/bin/greetd" ] || fail "greetd is not installed"
    wdir "$VMCTL_ROOT/etc/greetd"
    wcat "$VMCTL_ROOT/etc/greetd/config.toml" <<EOF
# Written by vmctl (vm/build-image.sh).  [initial_session] is greetd's autologin:
# on the first run after boot greetd starts the test user's compositor straight
# away, as a Class=user logind session on seat0 / vt 1 (pam_systemd; greetd exports
# XDG_SESSION_TYPE=tty, and libseat then switches the logind Type to wayland).
# Only when that session ends does [default_session] run -- here the same command
# again (as a greeter-class session), so a logout brings the desktop back on a VM
# that has no keyboard for a text greeter.  No seatd: the compositor takes the DRM
# device through libseat's logind backend.
[terminal]
vt = 1

[default_session]
command = "$cmd"
user = "test"

[initial_session]
command = "$cmd"
user = "test"
EOF
    wdir "$VMCTL_ROOT/etc/systemd/system/greetd.service.d"
    wcat "$VMCTL_ROOT/etc/systemd/system/greetd.service.d/vmctl-vt1.conf" <<'EOF'
# vmctl: the packaged unit only conflicts with getty@tty7 (its default vt);
# we run on vt 1, so keep the tty1 getty away from it.
[Unit]
Conflicts=getty@tty1.service
After=getty@tty1.service
EOF
    systemctl disable getty@tty1.service 2>/dev/null || true
    # through dm_enable like every other manager: Fedora's sway group brings SDDM
    # along and it self-enables mid-transaction, so a bare `systemctl enable greetd`
    # died on the alias it already held (fedora44-sway, CI run 34369384290)
    dm_enable greetd /usr/bin/greetd
}
dm_lightdm() {   # dm_lightdm <session>
    local sess=$1 dms
    # `ls` of a missing path fails; keep the pipeline (set -o pipefail, ERR trap) happy
    dms=$({ ls "$VMCTL_ROOT/usr/sbin/gdm3" "$VMCTL_ROOT/usr/bin/sddm" 2>/dev/null || true; } | tr '\n' ' ')
    say "LightDM is the display manager (installed alongside: ${dms:-none})"
    dm_enable lightdm /usr/sbin/lightdm
    # LightDM 1.32's built-in sessions-directory is /usr/share/lightdm/sessions:
    # /usr/share/xsessions:/usr/share/wayland-sessions (read out of the shipped
    # binary [recon2/cinnamon, recon2/xfce-wayland]), so one stanza selects either
    # session type and a Wayland session name needs no extra key.
    say "LightDM: autologin of user test into the $sess session, graphical.target"
    wdir "$VMCTL_ROOT/etc/lightdm/lightdm.conf.d"
    wcat "$VMCTL_ROOT/etc/lightdm/lightdm.conf.d/50-autologin.conf" <<EOF
# Written by vmctl (vm/build-image.sh): autologin the test user into $sess.
[Seat:*]
autologin-user=test
autologin-session=$sess
autologin-user-timeout=0
EOF
    # Ubuntu's lightdm-autologin PAM stack needs no group; add test to the ones
    # other distributions gate autologin on, if a package created them.
    local g
    for g in autologin nopasswdlogin; do
        getent group "$g" >/dev/null && usermod -aG "$g" test && say "user test added to group $g"
    done
    if [ -f "$VMCTL_ROOT/usr/share/wayland-sessions/$sess.desktop" ]; then
        accountsservice "$sess"          # XSession= is meaningless for a Wayland session
    else
        accountsservice "$sess" "$sess"
    fi
}

# ---------------------------------------------------------------- layer 3: desktops

hide_autostart() {   # a Hidden=true override in ~/.config/autostart for user test
    local d
    wdir -o test -g test -m 0700 "$TESTHOME/.config" "$TESTHOME/.config/autostart"
    for d in "$@"; do
        printf '[Desktop Entry]\nType=Application\nName=%s (disabled by vmctl)\nHidden=true\n' "$d" \
            > "$TESTHOME/.config/autostart/$d.desktop"
        written "$TESTHOME/.config/autostart/$d.desktop"
    done
}
gschema_quiet() {   # gschema_quiet gnome|cinnamon: no lock, no blank, no idle sleep
    # GNOME and Cinnamon carry the same schema tree under two ids; MATE's keys are
    # named differently and are written by desktop_mate itself.
    # No org.cinnamon block: an `enabled-applets` override would replace Cinnamon's
    # shipped panel (window list, clock, tray, ...) with whatever it named, and the
    # panel's strut is exactly what the X11 desktops' _NET_WORKAREA checks read.  The
    # recon measured no such key [recon2/cinnamon]; desktop_cinnamon hides mintwelcome
    # through XDG autostart instead.
    local ns=$1 shell_block=""
    [ "$ns" = gnome ] && shell_block="
[org.gnome.shell]
welcome-dialog-last-shown-version='999'"
    wdir "$VMCTL_ROOT/usr/share/glib-2.0/schemas"
    wcat "$VMCTL_ROOT/usr/share/glib-2.0/schemas/90_vmctl.gschema.override" <<EOF
# vmctl test rig: keep the desktop awake, unlocked and quiet.
[org.$ns.desktop.screensaver]
lock-enabled=false
idle-activation-enabled=false

[org.$ns.desktop.session]
idle-delay=uint32 0

[org.$ns.settings-daemon.plugins.power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
idle-dim=false$shell_block
EOF
    glib-compile-schemas "$VMCTL_ROOT/usr/share/glib-2.0/schemas"
    # verify the override took (glib-compile-schemas ignores a whole override file on an unknown key)
    [ "$(gsettings get org.$ns.desktop.session idle-delay 2>/dev/null)" = "uint32 0" ] \
        || fail "gschema override not applied (org.$ns.desktop.session idle-delay)"
}
wlr_layout() {   # /usr/local/bin/vmctl-wlr-layout: the vmctl-sway-layout of a compositor
    # with no IPC.  wlroots adds the initial outputs in REVERSE enumeration order --
    # measured on labwc, where HEADLESS-1 landed at +1280+0 [recon2/labwc,
    # recon2/xfce-wayland], and again here on sway 1.11 headless, where HEADLESS-2
    # took (0,0).  The rig's contract is head 0 = Virtual-1 at (0,0).
    wdir "$VMCTL_ROOT/usr/local/bin"
    wcat "$VMCTL_ROOT/usr/local/bin/vmctl-wlr-layout" <<'EOF'
#!/usr/bin/env python3
"""vmctl: put Virtual-1, Virtual-2, ... side by side at y=0 in connector order,
once, when the session starts, through wlr-randr (the compositors this runs on --
labwc, river, the Xfce/LXQt/Budgie Wayland sessions -- have no IPC of their own).
Nothing is watched afterwards: a test that moves outputs, or a hot-plugged head,
is left alone."""
import re, subprocess

def wlr(*args):
    return subprocess.run(["wlr-randr", *args], capture_output=True, text=True)

# One (name, width) per ENABLED head.  The width comes from that head's own
# "  1920x1080 px, 60.000 Hz (current)" line and not from a constant: `vmctl start
# --head-size` is free to ask for anything, and a hard-coded 1920 would overlap or
# gap the heads on every other size.
heads, name, wide, on = [], None, 0, False
def flush():
    if name and on:
        heads.append((name, wide or 1920))
for line in wlr().stdout.splitlines():
    m = re.match(r"^(\S+) ", line)
    if m:
        flush()
        name, wide, on = m.group(1), 0, False
    elif name and re.match(r"^\s+Enabled: yes", line):
        on = True
    elif name and not wide:
        m = re.match(r"^\s+(\d+)x(\d+) px.*\(current\)", line)
        if m:
            wide = int(m.group(1))
flush()   # the last head has no following header to flush it
def key(h):
    tail = h[0].rsplit("-", 1)[-1]
    return int(tail) if tail.isdigit() else 1 << 30
args, x = [], 0
for h, w in sorted(heads, key=key):
    args += ["--output", h, "--pos", "%d,0" % x]
    x += w
if args:
    wlr(*args)
EOF
    chmod 0755 "$VMCTL_ROOT/usr/local/bin/vmctl-wlr-layout"
}

desktop_gnome() {
    say "GNOME defaults: no screen lock/blank/idle sleep, no welcome tour (gschema override)"
    gschema_quiet gnome
    [ "$(gsettings get org.gnome.shell welcome-dialog-last-shown-version 2>/dev/null)" = "'999'" ] \
        || fail "gschema override (shell) not applied"
    say "user test: skip gnome-initial-setup (first login AND post-upgrade), hide update-notifier autostarts"
    wdir -o test -g test -m 0700 "$TESTHOME/.config" "$TESTHOME/.config/autostart" \
        "$TESTHOME/.config/gnome-initial-setup"
    echo yes > "$TESTHOME/.config/gnome-initial-setup-done"
    written "$TESTHOME/.config/gnome-initial-setup-done"
    # gnome-initial-setup >= 50 (26.04) also has gnome-initial-setup-upgrade-login.service
    # ("Welcome to Ubuntu 26.04 LTS!" / release notes dialog): it runs when the
    # -done marker above EXISTS and gnome-initial-setup/upgrade-<release>-done does
    # NOT, so the first-login marker alone turns that dialog on at every login.
    # Create the marker for this release plus whatever the installed unit names.
    local rel u m
    rel=$(. /etc/os-release; echo "$VERSION_ID")
    touch "$TESTHOME/.config/gnome-initial-setup/upgrade-${rel}-done"
    for u in "$VMCTL_ROOT/usr/lib/systemd/user/gnome-initial-setup-upgrade-login.service"; do
        [ -f "$u" ] || continue
        for m in $(sed -n 's/^ConditionPathExists=!%E\/\([^ ]*\)$/\1/p' "$u"); do
            wdir "$TESTHOME/.config/$(dirname "$m")"
            touch "$TESTHOME/.config/$m"
        done
        say "post-upgrade dialog markers: $(ls "$TESTHOME/.config/gnome-initial-setup" | tr '\n' ' ')"
    done
    hide_autostart gnome-initial-setup-first-login update-notifier ubuntu-report-on-upgrade
}
desktop_kde() {
    local kw cfg prof v
    kw=$(command -v kwriteconfig6 || command -v kwriteconfig5 || true)
    [ -n "$kw" ] || fail "kwriteconfig5/6 not found (libkf5config-bin / libkf6config-bin)"
    say "KDE: no screen lock / display power management for user test ($kw: kscreenlockerrc, powerdevilrc)"
    wdir -o test -g test -m 0700 "$TESTHOME/.config"
    cfg=$TESTHOME/.config
    $kw --file $cfg/kscreenlockerrc --group Daemon --key Autolock false
    $kw --file $cfg/kscreenlockerrc --group Daemon --key LockOnResume false
    written "$cfg/kscreenlockerrc"
    # the same as system-wide defaults (cascaded by KConfig; harmless duplicates)
    $kw --file "$VMCTL_ROOT/etc/xdg/kscreenlockerrc" --group Daemon --key Autolock false
    $kw --file "$VMCTL_ROOT/etc/xdg/kscreenlockerrc" --group Daemon --key LockOnResume false
    written "$VMCTL_ROOT/etc/xdg/kscreenlockerrc"
    if [ "${kw##*/}" = kwriteconfig6 ]; then
        # powerdevil 6: per-profile groups in powerdevilrc
        for prof in AC Battery LowBattery; do
            $kw --file $cfg/powerdevilrc --group $prof --group Display --key DimDisplayWhenIdle false
            $kw --file $cfg/powerdevilrc --group $prof --group Display --key TurnOffDisplayWhenIdle false
            $kw --file $cfg/powerdevilrc --group $prof --group SuspendAndShutdown --key AutoSuspendAction 0
        done
        written "$cfg/powerdevilrc"
    else
        # powerdevil 5: profiles live in powermanagementprofilesrc (SimpleConfig, no
        # /etc/xdg cascade; generated with idle actions only when the file is empty).
        # An action is disabled by its group being absent: keep just the button handling.
        wcat "$cfg/powermanagementprofilesrc" <<'EOF'
[AC]
icon=battery-charging

[AC][HandleButtonEvents]
lidAction=1
powerButtonAction=16
powerDownAction=16
triggerLidActionWhenExternalMonitorPresent=false

[Battery]
icon=battery-060

[Battery][HandleButtonEvents]
lidAction=1
powerButtonAction=16
powerDownAction=16
triggerLidActionWhenExternalMonitorPresent=false

[LowBattery]
icon=battery-low

[LowBattery][HandleButtonEvents]
lidAction=1
powerButtonAction=16
powerDownAction=16
triggerLidActionWhenExternalMonitorPresent=false
EOF
    fi
    say "user test: hide the Plasma welcome centre / Discover update notifier autostarts"
    hide_autostart org.kde.plasma-welcome org.kde.discover.notifier
    # Plasma 6: the welcome centre is no autostart entry any more but launched by a
    # kded module (kded_plasma_welcome) whenever plasma-welcomerc's LastSeenVersion
    # is missing or older than plasma-welcome itself.  Mark this version as seen.
    # One query per package manager, in the order pkg_manifest uses; without the pacman
    # arm arch-kde would never get plasma-welcomerc and selftest.sh's `pgrep -x
    # plasma-welcome` check would fail with "a first-run window is running".
    # `pacman -Q` prints "name version", the same shape pkg_manifest normalises.
    if { v=$(dpkg-query -W -f='${Version}' plasma-welcome 2>/dev/null) \
         || v=$(rpm -q --qf '%{VERSION}' plasma-welcome 2>/dev/null) \
         || v=$(pacman -Q plasma-welcome 2>/dev/null | cut -d' ' -f2); } && [ -n "$v" ]; then
        # kded compares this against plasma-welcome's own version string, which carries
        # neither dpkg's epoch nor its revision.
        v=${v#*:}; v=${v%%-*}
        $kw --file $cfg/plasma-welcomerc --group General --key LastSeenVersion "$v"
        written "$cfg/plasma-welcomerc"
        say "plasma-welcome $v marked as seen (plasma-welcomerc)"
    else
        # An empty LastSeenVersion is worse than none: kded reads "" as older than every
        # version and opens the welcome centre on every boot, which is the failure this
        # block exists to prevent.  Say so on the serial console instead.
        say "warning: no plasma-welcome version from dpkg/rpm/pacman; plasma-welcomerc not written"
    fi
}
xfce_settings() {   # the xfconf channel files, shared by the X11 and the Wayland session
    local xc=$TESTHOME/.config/xfce4/xfconf/xfce-perchannel-xml
    wdir -o test -g test -m 0700 "$TESTHOME/.config" "$TESTHOME/.config/xfce4" \
        "$TESTHOME/.config/xfce4/xfconf" "$xc"
    wcat "$xc/xfce4-screensaver.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!-- vmctl test rig: never lock or blank -->
<channel name="xfce4-screensaver" version="1.0">
  <property name="saver" type="empty">
    <property name="enabled" type="bool" value="false"/>
    <property name="idle-activation" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
  </property>
  <property name="lock" type="empty">
    <property name="enabled" type="bool" value="false"/>
    <property name="saver-activation" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
    <property name="sleep-activation" type="empty">
      <property name="enabled" type="bool" value="false"/>
    </property>
  </property>
</channel>
EOF
    wcat "$xc/xfce4-power-manager.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!-- vmctl test rig: no DPMS, no blanking, no idle sleep, no lock on suspend -->
<channel name="xfce4-power-manager" version="1.0">
  <property name="xfce4-power-manager" type="empty">
    <property name="dpms-enabled" type="bool" value="false"/>
    <property name="blank-on-ac" type="int" value="0"/>
    <property name="blank-on-battery" type="int" value="0"/>
    <property name="dpms-on-ac-sleep" type="uint" value="0"/>
    <property name="dpms-on-ac-off" type="uint" value="0"/>
    <property name="dpms-on-battery-sleep" type="uint" value="0"/>
    <property name="dpms-on-battery-off" type="uint" value="0"/>
    <property name="inactivity-on-ac" type="uint" value="14"/>
    <property name="inactivity-on-battery" type="uint" value="14"/>
    <property name="lock-screen-suspend-hibernate" type="bool" value="false"/>
    <property name="presentation-mode" type="bool" value="true"/>
  </property>
</channel>
EOF
    wcat "$xc/xfce4-session.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-session" version="1.0">
  <property name="shutdown" type="empty">
    <property name="LockScreen" type="bool" value="false"/>
  </property>
</channel>
EOF
    wcat "$xc/displays.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!-- vmctl test rig: what xfsettingsd does with a hot-plugged output (/Notify:
     0 nothing, 1 the "new display" dialog = the default, 2 mirror, 3 extend):
     extend it, so a hot-plug shows up in xrandr - -listmonitors with no dialog -->
<channel name="displays" version="1.0">
  <property name="Notify" type="int" value="3"/>
</channel>
EOF
    chown -R test:test "$TESTHOME/.config/xfce4"
    hide_autostart xfce4-screensaver light-locker update-notifier
}
desktop_xfce() {
    [ -f "$VMCTL_ROOT/usr/share/xsessions/xubuntu.desktop" ] || \
        fail "no /usr/share/xsessions/xubuntu.desktop (xubuntu-default-settings)"
    say "Xfce: xfce4-screensaver/light-locker off, no blanking/DPMS/idle sleep (xfconf), autostarts hidden"
    xfce_settings
}
desktop_xfce_wayland() {
    # Xfce 4.20's Wayland session is startxfce4 --wayland with labwc as the
    # compositor; the session file is xfce-wayland.desktop and exists in neither
    # /usr/share/xsessions nor /usr/share/lightdm/sessions [recon2/xfce-wayland].
    [ -f "$VMCTL_ROOT/usr/share/wayland-sessions/xfce-wayland.desktop" ] || \
        fail "no /usr/share/wayland-sessions/xfce-wayland.desktop (xfce4-session 4.20 + labwc)"
    command -v labwc >/dev/null || fail "labwc is not installed (the compositor of the Xfce Wayland session)"
    say "Xfce on Wayland: the xfconf channels of the X11 session, plus the wlr layout helper"
    xfce_settings
    wlr_layout
    autostart_wlr_layout          # NOT labwc_config: see the comment on it
}
autostart_wlr_layout() {   # ~/.config/autostart/vmctl-wlr-layout.desktop for user test
    # The XDG autostart directory, not labwc's own, is where the layout helper goes for
    # every DESKTOP that merely happens to run on labwc (Budgie, the Xfce and LXQt
    # Wayland sessions): those sessions start their own session manager under labwc
    # (budgie-desktop, xfce4-session, lxqt-session) and every one of them runs XDG
    # autostart, while labwc's ~/.config/labwc is not even the directory their labwc
    # reads -- `startxfce4 --wayland` runs
    # `labwc --config-dir ~/.config/xfce4/labwc --config ~/.config/xfce4/labwc/rc.xml
    #  --session xfce4-session` (read verbatim out of /usr/bin/startxfce4 4.20.4-1 and
    # measured as the process tree [recon2/xfce-wayland]), Budgie's labwc config is
    # written into ~/.config/budgie-desktop/labwc/ by budgie's own labwc-bridge
    # [recon2/budgie], and startlxqtwayland's config dir was never measured at all.
    wdir -o test -g test -m 0700 "$TESTHOME/.config" "$TESTHOME/.config/autostart"
    wcat "$TESTHOME/.config/autostart/vmctl-wlr-layout.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=vmctl head layout
Exec=/usr/local/bin/vmctl-wlr-layout
X-GNOME-Autostart-enabled=true
EOF
}
labwc_config() {   # ~/.config/labwc/{rc.xml,environment,autostart}; extra autostart lines in $@
    # ONLY for the bare `labwc` flavor, whose session IS `labwc` with no --config-dir.
    # Do not call this for a desktop that runs on labwc: besides the config-dir above,
    # labwc's environment file OVERRIDES a variable that is already set -- measured here
    # on labwc 0.9.3 (the version resolute ships [recon2/openbox]), where an environment
    # already carrying XDG_CURRENT_DESKTOP=LXQt:wlroots came out of the startup command
    # as labwc:wlroots.  Writing it under an LXQt or Xfce session would have the rig
    # rewrite the very desktop identity the tools' detection reads, and would stop
    # LXQt's OnlyShowIn=LXQt autostarts from matching.
    #
    # labwc alone draws NOTHING -- no bar, no wallpaper -- so a correct boot would fail
    # vmctl's "a head that is one flat colour has not painted" check.  swaybg with the
    # sway wallpaper is what makes head 0 non-flat [recon2/labwc].
    local extra
    wdir -o test -g test -m 0700 "$TESTHOME/.config" "$TESTHOME/.config/labwc"
    wcat "$TESTHOME/.config/labwc/rc.xml" <<'EOF'
<?xml version="1.0"?>
<!-- vmctl test rig: three named desktops, so ext_workspace_manager_v1 has
     something to report, and no window-decoration surprises. -->
<labwc_config>
  <desktops>
    <popupTime>0</popupTime>
    <names>
      <name>1</name>
      <name>2</name>
      <name>3</name>
    </names>
  </desktops>
</labwc_config>
EOF
    wcat "$TESTHOME/.config/labwc/environment" <<'EOF'
# vmctl test rig: labwc reads key=value lines here into the session environment.
XDG_CURRENT_DESKTOP=labwc:wlroots
XDG_SESSION_TYPE=wayland
EOF
    {
        cat <<'EOF'
#!/bin/sh
# Written by vmctl (vm/build-image.sh): labwc's autostart is where the sway
# config's `exec` lines go.  There is no SWAYSOCK equivalent to export.
dbus-update-activation-environment --systemd WAYLAND_DISPLAY DISPLAY \
    XDG_CURRENT_DESKTOP=labwc:wlroots XDG_SESSION_TYPE=wayland &
[ -f /usr/share/backgrounds/sway/Sway_Wallpaper_Blue_1920x1080.png ] && \
    swaybg -i /usr/share/backgrounds/sway/Sway_Wallpaper_Blue_1920x1080.png &
EOF
        for extra in "$@"; do printf '/usr/local/bin/%s &\n' "$extra"; done
    } > "$TESTHOME/.config/labwc/autostart"
    written "$TESTHOME/.config/labwc/autostart"
    chmod 0755 "$TESTHOME/.config/labwc/autostart"
}
desktop_labwc() {
    say "labwc: rc.xml/environment/autostart for user test, swaybg wallpaper, wlr layout helper"
    wlr_layout
    labwc_config "vmctl-wlr-layout"
}
desktop_budgie() {
    # Ubuntu Budgie 26.04 is a labwc desktop: ubuntu-budgie-desktop-minimal Depends on
    # labwc and budgie-desktop Depends on xdotool, wlr-randr and wdisplays -- the
    # shipped default already carries the X11 tool this project replaces [recon2/budgie].
    # budgie-desktop's own session starts labwc; the rig only adds the layout helper.
    say "Budgie on labwc: the wlr layout helper, idle/lock autostarts hidden"
    wlr_layout
    autostart_wlr_layout
    # budgie-core pulls swayidle/swaylock/wlopm and budgie-daemon owns power; this is
    # the one part of the recipe the recon could not verify, and where a first build
    # will need one iteration [recon2/budgie].
    hide_autostart swayidle budgie-screensaver update-notifier
}
desktop_lxqt_wayland() {
    # startlxqtwayland has a first-run branch that picks a compositor into a DIFFERENT
    # config directory and starts lxqt-config-session instead of lxqt-session; naming
    # labwc in session.conf is what avoids it [recon2/openbox].
    say "LXQt on Wayland: compositor=labwc in session.conf, wlr layout helper on XDG autostart"
    # Both copies: lubuntu-default-settings ships /etc/xdg/xdg-Lubuntu/lxqt/session.conf
    # with window_manager=openbox and a Lubuntu session prepends xdg-Lubuntu to
    # XDG_CONFIG_DIRS [recon2/openbox], so the system file below can LOSE to Lubuntu's
    # on the one key that decides whether startlxqtwayland takes its first-run branch.
    # The user file beats every system directory, so that is where the answer really is.
    wdir "$VMCTL_ROOT/etc/xdg/lxqt"
    wcat "$VMCTL_ROOT/etc/xdg/lxqt/session.conf" <<'EOF'
# Written by vmctl (vm/build-image.sh).
[General]
window_manager=labwc
compositor=labwc
EOF
    wdir -o test -g test -m 0700 "$TESTHOME/.config" "$TESTHOME/.config/lxqt"
    wcat "$TESTHOME/.config/lxqt/session.conf" <<'EOF'
# Written by vmctl (vm/build-image.sh); this copy outranks /etc/xdg/xdg-Lubuntu.
[General]
window_manager=labwc
compositor=labwc
EOF
    chown -R test:test "$TESTHOME/.config/lxqt"
    lxqt_wayland_desktops
    wlr_layout
    autostart_wlr_layout          # NOT labwc_config: see the comment on it
    hide_autostart xscreensaver lxqt-powermanagement lubuntu-update-notifier
}
# Three workspaces for the LXQt Wayland session, written into LXQt's OWN labwc rc.xml.
#
# Not labwc_config's file and not a copy of it.  Measured on the resolute-lxqt-wayland golden
# 2026-09-09: `startlxqtwayland` copies /usr/share/lxqt/wayland/labwc to $XDG_CONFIG_HOME/labwc
# only when that directory does not exist yet, and then runs
# `labwc -C $XDG_CONFIG_HOME/labwc -S lxqt-session` -- so the config dir IS ~/.config/labwc, but
# pre-creating it at build time would stop LXQt's menu.xml, themerc and environment from ever
# being installed and would leave a desktop that is not Lubuntu's.  Patching the system copy
# gets the session the workspaces AND everything else LXQt ships.
#
# lxqt-wayland-session's rc.xml declares exactly one, `<name>Default</name>` at six spaces of
# indent; the four-name example above it in the same file is inside an XML comment and is
# indented eight, which is why the anchor is the whole line and not the tag.  With one desktop
# `wwmctl -d` printed one row on the first run of this flavor (CI 34308982263) and
# vm/live-smoke.d/labwc.sh reads that as the ext_workspace reader having nothing to read.
lxqt_wayland_desktops() {
    local rc=$VMCTL_ROOT/usr/share/lxqt/wayland/labwc/rc.xml
    [ -f "$rc" ] || fail "no $rc (lxqt-wayland-session): nothing to give three workspaces to"
    grep -q '^      <name>Default</name>$' "$rc" || \
        fail "$rc no longer has the one-line <name>Default</name> this patches"
    local two='      <name>Workspace 2</name>\n      <name>Workspace 3</name>'
    sed -i "s|^      <name>Default</name>\$|      <name>Default</name>\n$two|" "$rc"
    written "$rc"
    say "LXQt on Wayland: three workspaces in $rc (shipped: one), so ext_workspace_manager_v1 has \
something to report"
}
desktop_lxqt() {
    # Measured on a real Lubuntu session: without window_manager=openbox lxqt-session
    # comes up with no WM and no panel at all (`ps -u test` showed lxqt-session alone).
    # xscreensaver is what LXQt/Lubuntu ships, so ~/.xscreensaver is the xfconf
    # analogue here [recon2/openbox].
    say "LXQt on X11: window_manager=openbox, xscreensaver off, autostarts hidden"
    wdir "$VMCTL_ROOT/etc/xdg/lxqt"
    wcat "$VMCTL_ROOT/etc/xdg/lxqt/session.conf" <<'EOF'
# Written by vmctl (vm/build-image.sh).
[General]
window_manager=openbox
EOF
    wdir -o test -g test -m 0700 "$TESTHOME/.config"
    wcat "$TESTHOME/.xscreensaver" <<'EOF'
mode: off
lock: False
dpmsEnabled: False
EOF
    chown test:test "$TESTHOME/.xscreensaver"
    hide_autostart xscreensaver lxqt-powermanagement lubuntu-update-notifier
}
desktop_sway() {
    say "sway: config for user test = the packaged default + xwayland, Virtual-N left to right, session env export"
    wdir "$VMCTL_ROOT/usr/local/bin"
    wcat "$VMCTL_ROOT/usr/local/bin/vmctl-sway-layout" <<'EOF'
#!/usr/bin/env python3
"""vmctl: put sway's outputs Virtual-1, Virtual-2, ... side by side at y=0 in
connector order, once, when the session starts.  Stock sway/wlroots adds the
initial outputs to the layout in reverse enumeration order, which on a 3-head
virtio-vga puts Virtual-3 at (0,0) and Virtual-1 at x=3840; the rig's contract
is head 0 = Virtual-1 at (0,0).  Nothing is watched afterwards: a test that
moves outputs, or a hot-plugged head (wlroots appends it on the right), is
left alone."""
import json, subprocess

def sway(*args):
    return subprocess.run(["swaymsg", *args], capture_output=True, text=True)

def key(o):
    n = o["name"].rsplit("-", 1)[-1]
    return int(n) if n.isdigit() else 1 << 30

outs = sorted((o for o in json.loads(sway("-t", "get_outputs").stdout) if o.get("active")), key=key)
x = 0
for o in outs:
    sway("output", o["name"], "pos", str(x), "0")
    x += o["rect"]["width"]
if outs:
    sway("focus", "output", outs[0]["name"])
EOF
    chmod 0755 "$VMCTL_ROOT/usr/local/bin/vmctl-sway-layout"
    wdir -o test -g test -m 0700 "$TESTHOME/.config" "$TESTHOME/.config/sway"
    {
      cat "$VMCTL_ROOT/etc/sway/config"
      cat <<'EOF'

### vmctl additions (vm/build-image.sh) -- everything above is /etc/sway/config verbatim
# Xwayland for the X11-parity oracles (xdotool/wmctrl/xprop/xrandr through Xwayland);
# "enable" = lazy start on the first X client (sway's default when Xwayland is installed).
xwayland enable
# workspace N on Virtual-N (stock sway hands workspace 1 to whichever output it
# enabled first, which is Virtual-3 on a 3-head virtio-vga)
workspace 1 output Virtual-1
workspace 2 output Virtual-2
workspace 3 output Virtual-3
workspace 4 output Virtual-4
# Publish the session to the systemd user manager + D-Bus activation environment so
# user services and `vmctl user` can find it (greetd starts sway with a bare env).
exec dbus-update-activation-environment --systemd WAYLAND_DISPLAY SWAYSOCK DISPLAY XDG_CURRENT_DESKTOP=sway XDG_SESSION_TYPE=wayland
# Virtual-1 at (0,0), Virtual-2 to its right, ... (once; see the script)
exec /usr/local/bin/vmctl-sway-layout
EOF
    } > "$TESTHOME/.config/sway/config"
    written "$TESTHOME/.config/sway/config"
    chown test:test "$TESTHOME/.config/sway/config"
    sway -C -c "$TESTHOME/.config/sway/config" >/dev/null 2>&1 \
        || say "warning: sway -C could not validate the config here (no display); check in the guest"
}
desktop_hypr() {
    # Hyprland places outputs by config rule, so no exec helper is needed.  scale 1 is
    # pinned because Hyprland's default `auto` gave a runtime headless output scale 2.0
    # [recon2/hyprland]; the four monitor lines are the vmctl-sway-layout equivalent.
    say "Hyprland: hyprland.conf for user test (unscaled Virtual-N left to right, no logo, no splash)"
    wdir -o test -g test -m 0700 "$TESTHOME/.config" "$TESTHOME/.config/hypr"
    {
      [ -f "$VMCTL_ROOT/usr/share/hypr/hyprland.conf" ] && cat "$VMCTL_ROOT/usr/share/hypr/hyprland.conf"
      cat <<'EOF'

### vmctl additions (vm/build-image.sh) -- everything above is /usr/share/hypr/hyprland.conf
monitor = , preferred, auto, 1
monitor = Virtual-1,preferred,0x0,1
monitor = Virtual-2,preferred,1920x0,1
monitor = Virtual-3,preferred,3840x0,1
monitor = Virtual-4,preferred,5760x0,1
animations { enabled = false }
misc {
    disable_hyprland_logo = true
    disable_splash_rendering = true
    force_default_wallpaper = 0
}
debug { disable_logs = false }
EOF
    } > "$TESTHOME/.config/hypr/hyprland.conf"
    written "$TESTHOME/.config/hypr/hyprland.conf"
    chown test:test "$TESTHOME/.config/hypr/hyprland.conf"
}
desktop_wayfire() {
    # Wayfire starts with no config at all (default plugin list, one 1280x720 headless
    # output); the ini exists for the rig's contract alone.  It places outputs from the
    # ini directly, so no layout helper is needed either [recon2/wayfire].
    say "Wayfire: wayfire.ini for user test (ipc plugins, Virtual-N stanzas, wf-panel)"
    # [autostart] takes one command per key with no continuation, so the D-Bus
    # activation-environment publish (the sway config's `exec dbus-update-...` line)
    # is a two-line script rather than a 140-column ini value.
    wdir "$VMCTL_ROOT/usr/local/bin"
    wcat "$VMCTL_ROOT/usr/local/bin/vmctl-wayfire-env" <<'EOF'
#!/bin/sh
# Written by vmctl (vm/build-image.sh): wayfire.ini's [autostart] env= command.
exec dbus-update-activation-environment --systemd \
    WAYLAND_DISPLAY WAYFIRE_SOCKET DISPLAY \
    XDG_CURRENT_DESKTOP=wayfire XDG_SESSION_TYPE=wayland
EOF
    chmod 0755 "$VMCTL_ROOT/usr/local/bin/vmctl-wayfire-env"
    wdir -o test -g test -m 0700 "$TESTHOME/.config"
    wcat "$TESTHOME/.config/wayfire.ini" <<'EOF'
# Written by vmctl (vm/build-image.sh).
[core]
plugins = autostart command cube expo fast-switcher fisheye grid idle invert move \
          oswitch place resize switcher vswitch window-rules wm-actions wobbly \
          wrot zoom ipc ipc-rules stipc
xwayland = true

# Two groups, so `wdotool/xkbmap.py:WayfireLayouts` (U07) is reachable at all.  With one
# xkb_layout the compositor's keymap has one group, `choose_group()` is CERTAIN and `fetch()`
# never asks any desktop reader -- measured 2026-09-08 against wayfire 0.10.0-1, the package
# this flavor installs: `wdotool keys explain --chars z` answered `layout: English (US) --
# group 1 of 1, from wayland` and `wayfire/get-keyboard-state` reported one possible layout.
# It is an ini line and not a smoke step because there is no runtime route to a second layout:
# `wayfire/set-config-options {"input/xkb_layout": ...}` answers `{"result": "ok"}` and changes
# nothing, and `wayfire/set-keyboard-state` must NEVER be called -- it recompiles the keymap as
# the selected layout DUPLICATED and loses the other one until restart [M recon2/wayfire.md
# §2.7, requests-batch-2.md from batch 13].
[input]
xkb_layout = us,de

[autostart]
env = /usr/local/bin/vmctl-wayfire-env
panel = wf-panel
background = wf-background

[idle]
disable_on_fullscreen = true
dpms_timeout = -1
screensaver_timeout = -1

[output:Virtual-1]
position = 0,0
[output:Virtual-2]
position = 1920,0
[output:Virtual-3]
position = 3840,0
[output:Virtual-4]
position = 5760,0
EOF
    chown test:test "$TESTHOME/.config/wayfire.ini"
}
desktop_river() {
    # river has no config file: `river -c <init>` runs one executable, and the window
    # manager is a separate program -- none is packaged anywhere, so the flavor builds
    # upstream's own reference WM (tinyrwm) from source [recon2/river].  river/wlroots
    # adds the initial outputs in reverse enumeration order exactly as sway does.
    say "river: the layout helper on the session's init script (tinyrwm is the window manager)"
    [ -x "$VMCTL_ROOT/usr/local/bin/tinyrwm" ] || fail "tinyrwm is not built (river has no packaged window manager)"
    wlr_layout
    wdir "$VMCTL_ROOT/usr/local/bin"
    wcat "$VMCTL_ROOT/usr/local/bin/vmctl-river-init" <<'EOF'
#!/bin/sh
# Written by vmctl (vm/build-image.sh): river's -c executable.
dbus-update-activation-environment --systemd WAYLAND_DISPLAY DISPLAY XDG_SESSION_TYPE=wayland &
/usr/local/bin/vmctl-wlr-layout &
exec /usr/local/bin/tinyrwm
EOF
    chmod 0755 "$VMCTL_ROOT/usr/local/bin/vmctl-river-init"
}
desktop_cosmic() {
    # cosmic-comp panics with Io(Os { code: 13 }) at src/config/mod.rs:173 when
    # ~/.config is not the user's before the first login [recon2/arch].  Its KMS backend
    # PAINTS on virtio-vga with no 3D -- measured 2026-09-09 off the fedora44-cosmic disk:
    # /sys/kernel/debug/dri/0/state names cosmic-comp as head 0's allocator and a QMP
    # screendump of that head measures stddev 0.131454 against the rig's 0.02 flat-colour
    # line -- so there is no COSMIC_BACKEND=winit fallback here and none is needed
    # [vm/flavors/fedora44-cosmic.yaml carries the write-up].
    # Nothing is written into ~/.config/cosmic: the recon recorded no cosmic-idle
    # config path, key or value, and a guess here would be a rig setting no measurement
    # stands behind.  The first fedora44-cosmic build is where idle blanking gets
    # measured -- and whether it even fires inside the smoke's window.
    say "COSMIC: ~/.config owned by test before the first login"
    wdir -o test -g test -m 0700 "$TESTHOME/.config"
    chown -R test:test "$TESTHOME/.config"
}
desktop_cinnamon() {
    # Muffin is Mutter's fork, so the quiet-desktop keys are Mutter's under
    # org.cinnamon.* ids [recon2/cinnamon].  The same function serves the X11 and the
    # Wayland session: the two flavors differ only in LightDM's autologin-session.
    say "Cinnamon: no lock/blank/idle sleep (gschema override), welcome dialog hidden"
    gschema_quiet cinnamon
    hide_autostart mintwelcome update-notifier
}
desktop_mate() {
    # MATE's keys are org.mate.*, named differently from GNOME's, so this is its own
    # override rather than gschema_quiet's [recon2/mate].
    say "MATE: no lock/blank/idle sleep (gschema override), autostarts hidden"
    wdir "$VMCTL_ROOT/usr/share/glib-2.0/schemas"
    wcat "$VMCTL_ROOT/usr/share/glib-2.0/schemas/90_vmctl.gschema.override" <<'EOF'
# vmctl test rig: keep the desktop awake, unlocked and quiet.
[org.mate.screensaver]
idle-activation-enabled=false
lock-enabled=false

[org.mate.session]
idle-delay=0

[org.mate.power-manager]
sleep-display-ac=0
sleep-display-battery=0
sleep-computer-ac=0
sleep-computer-battery=0

# The key's own default is FALSE, and MATE is the only desktop in the rig where it is
# [recon2/mate.md 5, read out of the schema]: a head that `vmctl head <n> off` unplugs and
# `vmctl head <n> 1920x1080` plugs back in would stay disabled here where every other
# flavor's desktop turns it back on, which is exactly what mate.sh's `heads` phase measures
# [requests-batch-2.md, from batch 16].
[org.mate.SettingsDaemon.plugins.xrandr]
turn-on-external-monitors-at-startup=true
EOF
    glib-compile-schemas "$VMCTL_ROOT/usr/share/glib-2.0/schemas"
    [ "$(gsettings get org.mate.session idle-delay 2>/dev/null)" = "0" ] \
        || fail "gschema override not applied (org.mate.session idle-delay)"
    # glib-compile-schemas ignores a WHOLE override file on one unknown key, so the new
    # block is verified by name too rather than trusted to the line above
    [ "$(gsettings get org.mate.SettingsDaemon.plugins.xrandr \
            turn-on-external-monitors-at-startup 2>/dev/null)" = "true" ] \
        || fail "gschema override not applied (org.mate.SettingsDaemon.plugins.xrandr)"
    hide_autostart mate-screensaver update-notifier
}
desktop_i3() {
    # Two rig requirements, both measured: without a config i3-config-wizard opens a
    # first-run window, which selftest.sh asserts is absent; and i3's root window is
    # solid black, which fails vmctl's flat-head check -- xsetroot -mod 8 8 is the
    # smallest fix [recon2/i3].  i3 has no display daemon, so the heads are arranged
    # with xrandr from the config.
    say "i3: config for user test (xsetroot checkerboard, xrandr layout, i3bar), no config wizard"
    wdir -o test -g test -m 0700 "$TESTHOME/.config" "$TESTHOME/.config/i3"
    {
      [ -f "$VMCTL_ROOT/etc/i3/config" ] && cat "$VMCTL_ROOT/etc/i3/config"
      cat <<'EOF'

### vmctl additions (vm/build-image.sh) -- everything above is /etc/i3/config verbatim
# A black root window is one flat colour and vmctl's first-paint check fails a head
# that is: give every head a checkerboard.
exec --no-startup-id xsetroot -mod 8 8
# i3 has no display daemon: arrange Virtual-1..4 left to right once, at start.
exec --no-startup-id xrandr --output Virtual-1 --auto --pos 0x0 --primary
exec --no-startup-id sh -c 'xrandr --output Virtual-2 --auto --right-of Virtual-1; \
                             xrandr --output Virtual-3 --auto --right-of Virtual-2'
EOF
    } > "$TESTHOME/.config/i3/config"
    written "$TESTHOME/.config/i3/config"
    chown test:test "$TESTHOME/.config/i3/config"
}

# ---------------------------------------------------------------- the dispatch table

select_desktop() {
    # One line per `# vmctl-desktop:` token of vm/vmctl's DESKTOPS, and nothing else:
    # DM is the dm_* function and DM_ARGS its session name or command, DESK the
    # desktop_* function.  tests/test_vm_scripts.py R04 pins this against DESKTOPS.
    case "$DESKTOP" in
    gnome)            DM=dm_gdm     DM_ARGS="wayland"                           DESK=desktop_gnome ;;
    gnome-x11)        DM=dm_gdm     DM_ARGS="x11"                               DESK=desktop_gnome ;;
    kde)              DM=dm_plasma  DM_ARGS="wayland"                           DESK=desktop_kde ;;
    kde-x11)          DM=dm_plasma  DM_ARGS="x11"                               DESK=desktop_kde ;;
    xfce)             DM=dm_lightdm DM_ARGS="xubuntu"                           DESK=desktop_xfce ;;
    xfce-wayland)     DM=dm_lightdm DM_ARGS="xfce-wayland"                      DESK=desktop_xfce_wayland ;;
    sway)             DM=dm_greetd  DM_ARGS="sway"                              DESK=desktop_sway ;;
    hypr)             DM=dm_greetd  DM_ARGS="Hyprland"                          DESK=desktop_hypr ;;
    labwc)            DM=dm_greetd  DM_ARGS="labwc"                             DESK=desktop_labwc ;;
    wayfire)          DM=dm_greetd  DM_ARGS="wayfire"                           DESK=desktop_wayfire ;;
    river)            DM=dm_greetd  DM_ARGS="river -c /usr/local/bin/vmctl-river-init" DESK=desktop_river ;;
    cosmic)           DM=dm_greetd  DM_ARGS="start-cosmic"                      DESK=desktop_cosmic ;;
    budgie)           DM=dm_sddm    DM_ARGS="budgie-desktop.desktop"            DESK=desktop_budgie ;;
    lxqt)             DM=dm_sddm    DM_ARGS="lxqt.desktop"                      DESK=desktop_lxqt ;;
    lxqt-wayland)     DM=dm_sddm    DM_ARGS="lxqt-wayland.desktop"              DESK=desktop_lxqt_wayland ;;
    cinnamon)         DM=dm_lightdm DM_ARGS="cinnamon"                          DESK=desktop_cinnamon ;;
    cinnamon-wayland) DM=dm_lightdm DM_ARGS="cinnamon-wayland"                  DESK=desktop_cinnamon ;;
    mate)             DM=dm_lightdm DM_ARGS="mate"                              DESK=desktop_mate ;;
    i3)               DM=dm_lightdm DM_ARGS="i3"                                DESK=desktop_i3 ;;
    *) fail "unknown DESKTOP=$DESKTOP (not a key of vm/vmctl's DESKTOPS)" ;;
    esac
}

# ---------------------------------------------------------------- the build itself

detect_pkg
select_desktop
say "flavor $FLAVOR ($DESKTOP, $PKG): $(. /etc/os-release; echo "$PRETTY_NAME"), kernel $(uname -r)"

say "waiting for network"
wait_net || fail "no network"

say "$PKG: refreshing package metadata"
pkg_update

# the small packages first: after the desktop install the network may be gone,
# and nothing that follows the desktop needs it
say "installing test-support packages: $EXTRA_PKGS"
pkg_install $EXTRA_PKGS

pkg_seed_dm "$DM"
say "installing $DESKTOP_PKG (expect 5-20 minutes)"
pkg_desktop $DESKTOP_PKG

$DM $DM_ARGS
$DESK
relabel

systemctl set-default graphical.target
# NetworkManager (from the desktop) now manages the NIC via netplan; networkd's
# wait-online would otherwise block network-online.target for its full 2 min
# timeout on every boot.  NetworkManager-wait-online covers the target.  (The
# wlroots flavors keep networkd: nothing installs NetworkManager there.)
if [ -x /usr/sbin/NetworkManager ]; then
  systemctl disable systemd-networkd-wait-online.service 2>/dev/null || true
fi
# a desktop arm that wrote nothing under ~/.config (river's config lives elsewhere) leaves
# no directory to chown, and the build died on it (arch-river, CI run 34628780504)
mkdir -p "$TESTHOME/.config"
chown -R test:test "$TESTHOME/.config"

say "disabling automatic updates ($PKG)"
pkg_quiet

say "cleanup"
pkg_cleanup
install -d /var/lib/vmctl
echo "$FLAVOR $(date -u +%FT%TZ)" > /var/lib/vmctl/golden
printf 'FLAVOR=%s\nDESKTOP=%s\nPKG=%s\n' "$FLAVOR" "$DESKTOP" "$PKG" > /var/lib/vmctl/flavor.env
pkg_manifest > /var/lib/vmctl/packages.txt
say "$(wc -l < /var/lib/vmctl/packages.txt) packages installed; dumping the list to the serial console"
sync
dmesg -n 1
{ echo VMCTL-PACKAGES-BEGIN; cat /var/lib/vmctl/packages.txt; echo VMCTL-PACKAGES-END; } > /dev/ttyS0
say "golden image build finished"
con VMCTL-BUILD-OK
