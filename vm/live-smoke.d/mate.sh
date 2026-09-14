# live-smoke.d/mate.sh -- MATE 1.26 on marco over Xorg (resolute-mate), the only session Ubuntu has.
#
# The handover body is Xfce's, unchanged, for the reason kde-x11.sh gives: handing our argv to the real
# xdotool/wmctrl/xprop/xrandr is a property of the SESSION TYPE and not of the desktop.  What this file
# adds is what marco is and nobody else is:
#
#   * `marco`.  marco is Metacity's fork and answers `Metacity (Marco)` on the _NET_SUPPORTING_WM_CHECK
#     window, with 68 atoms in _NET_SUPPORTED -- `_NET_WM_STATE_SHADED` among them, which mutter dropped
#     and KWin 6 refuses.  Its check window carries no _NET_WM_PID and no WM_CLASS, and
#     _NET_SHOWING_DESKTOP is absent right after start, so real `wmctrl -m` prints THREE N/A columns
#     here where xfwm4 prints a class, a pid and OFF.  That is what a MATE user sees, so it is asserted
#     as the answer and not as a defect [M recon2/mate.md 1, 3, against marco 1.26.2 under Xvfb].
#   * `heads`.  mate-settings-daemon's xrandr plugin is the one MATE-specific rig risk the recon could
#     not settle without a VM: `org.mate.SettingsDaemon.plugins.xrandr
#     turn-on-external-monitors-at-startup` is FALSE by default and vm/build-image.sh's desktop_mate
#     does not write it, so a head that is unplugged and plugged back in may stay disabled
#     [M recon2/mate.md 5].  This phase is that measurement.
#   * `x11root`.  common.sh's phase_root is a Wayland claim (root over ssh finds the seated user's
#     session), and xfce.sh's own comment says the X11 half is "a separate measurement nobody has made".
#     Here it is, on the desktop whose mechanism gap was measured: with the stock _SESSION_LEADERS
#     `_shell_environ(1000)` answered {} and with `mate-session` added it answered DISPLAY=:91
#     [M recon2/mate.md 4].  Whether LightDM's ~/.Xauthority masks the gap on a real box is exactly what
#     this phase settles.
#
# The window every check acts on is xfce.sh's `xterm -T w11smoke`: `wwmctl -lGpx` byte-identical to
# `wmctrl -lGpx` was measured on an xterm on a MATE session [M recon2/mate.md 3], and mate-terminal --
# which ubuntu-mate-desktop puts on the image -- has a VTE geometry that nothing here needs.
#
# shellcheck source=live-smoke.d/xfce.sh
. "$STEPS/xfce.sh"

SMOKE_PHASES="install passthrough marco heads x11root"

# The `-e` form of `editor_start` that this file used to override for itself is xfce.sh's own
# since 2026-09-09 (requests-batch-11.md item 1), so the override is gone and the reason lives
# where the function does.

# marco's own name for itself, the three N/A columns, and the state mutter dropped.
phase_marco() {
    editor_start
    local out win
    out=$(await 30 '[0-9]' "wdotool search --name w11smoke | head -1" || true)
    win=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$win" ]; then fail "no w11smoke xterm to work on [$(ev "$out")]"; return 1; fi
    pass "wdotool search --name w11smoke -> $win (through the real xdotool)"
    local m; m=$(guest 'wwmctl -m' || true)
    want "wwmctl -m names the window manager marco's check window names" "^Name: Metacity \(Marco\)$" "$m"
    # The three N/A columns, each on its own, because each is a different missing property and a reader
    # of a red run needs to know WHICH one marco grew or lost.
    want "...Class: N/A (marco's check window carries no WM_CLASS)" "^Class: N/A$" "$m"
    want "...PID: N/A (and no _NET_WM_PID)" "^PID: N/A$" "$m"
    want "...and the showing-the-desktop mode is N/A (no _NET_SHOWING_DESKTOP after start)" \
         "showing the desktop\" mode: N/A" "$m"
    note "marco --version: $(guest 'marco --version' | tr -d '\r' | head -1 || true)"
    # Byte parity, the oracle this whole tree is measured against, taken in one command so that nothing
    # can move between the two reads.  On an X11 session the two are the same binary by construction --
    # which is the claim: the handover happened and nothing mangled the argv on the way.
    local diffrc
    diffrc=$(guest 'wwmctl -lGpx > /tmp/w11-a.txt 2>&1; wmctrl -lGpx > /tmp/w11-b.txt 2>&1;
                    diff /tmp/w11-a.txt /tmp/w11-b.txt >/dev/null 2>&1; echo $?' | tr -d ' \r\n' || true)
    same "wwmctl -lGpx is byte-identical to wmctrl -lGpx" "0" "$diffrc"
    note "the list: $(ev "$(guest 'cat /tmp/w11-a.txt' || true)")"
    # Shading: marco kept it, so it is a live state here and a script that depends on it must keep
    # working.  wmctrl does the work; what is asserted is that the state STICKS.
    guest "wwmctl -r w11smoke -b add,shaded" >/dev/null 2>&1 || true
    sleep 1
    want "-b add,shaded sets _NET_WM_STATE_SHADED (marco kept what mutter dropped)" \
         "_NET_WM_STATE_SHADED" "$(guest "wxprop -id $win _NET_WM_STATE" || true)"
    guest "wwmctl -r w11smoke -b remove,shaded" >/dev/null 2>&1 || true
    sleep 1
    wantnot "-b remove,shaded takes it off again" "_NET_WM_STATE_SHADED" \
            "$(guest "wxprop -id $win _NET_WM_STATE" || true)"
    # The original's own limit, reproduced rather than papered over: Ubuntu ships xdotool 3.20160805.1,
    # which has no `windowstate` at all, and the handover means the user gets that binary's answer --
    # the same sentence README footnote (b) makes about the X11 column [M recon2/mate.md 3].
    want "wdotool windowstate is the installed xdotool 3.x's own Unknown command" \
         "Unknown command: windowstate" "$(guest "wdotool windowstate --add MAXIMIZED_VERT $win 2>&1" || true)"
    # The desktops marco publishes, and mate-panel's strut inside them: _NET_WORKAREA went from
    # 0,0 1920x1080 to 0,28 1920x1024 when the panel came up [M recon2/mate.md 6].
    local d; d=$(guest 'wwmctl -d' || true)
    same "wwmctl -d lists marco's four workspaces" "4" "$(printf '%s\n' "$d" | grep -c '^[0-9]' || true)"
    note "the desktop rows (WA carries mate-panel's strut): $(ev "$d")"
}

# The MATE-specific rig risk, measured for the first time: a head unplugged from the HOST and plugged
# back in, and whether mate-settings-daemon's xrandr plugin brings it back.  vmctl head is the rig's own
# hotplug; the oracle is real xrandr, which is what MATE itself drives.
phase_heads() {
    local n0 n1 n2 idx conn
    n0=$(oracle_outputs | grep -c . || true)
    # Default false, and vm/build-image.sh's desktop_mate does not write it: the value is read here
    # rather than assumed, because it is the key the outcome below hangs on.
    note "turn-on-external-monitors-at-startup: $(guest 'gsettings get \
             org.mate.SettingsDaemon.plugins.xrandr turn-on-external-monitors-at-startup' | tr -d '\r' || true)"
    if [ "${n0:-0}" -lt 2 ]; then
        note "one head only: this phase needs --heads 2 or more"
        return 0
    fi
    # The LAST head, by the oracle's own x order, for display_pair's reason: unplugging a middle head
    # leaves a hole in the layout and the check would be about the hole.
    conn=$(oracle_outputs | awk -F'[ ,]' '{ print $2, $1 }' | sort -n | awk '{ print $2 }' | tail -1)
    idx=$(( ${conn##*-} - 1 ))
    "$VM" head "$NAME" "$idx" off >/dev/null 2>&1 || true
    sleep 5
    n1=$(oracle_outputs | grep -c . || true)
    same "unplugging $conn (head $idx) from the host leaves $((n0 - 1)) enabled outputs" \
         "$((n0 - 1))" "$n1"
    # 1920x1080 is vmctl's own DEFAULT_HEAD, i.e. the size `vmctl start --heads N` gave it.
    "$VM" head "$NAME" "$idx" 1920x1080 >/dev/null 2>&1 || true
    sleep 8
    n2=$(oracle_outputs | grep -c . || true)
    # A plain `want` since the golden carries the key this was waiting for:
    # `org.mate.SettingsDaemon.plugins.xrandr turn-on-external-monitors-at-startup=true` is a
    # gschema override vm/build-image.sh's desktop_mate writes and then reads back with
    # gsettings in the same build (the key's own default is false, and MATE is the only rig
    # desktop where it is).  The `--auto` arm below stays: it is what the phase says instead
    # when the head does not come back by itself.
    want "$conn comes back on its own when it is plugged in again" "^$n0$" "$n2"
    note "after the replug the oracle sees: $(ev "$(oracle_outputs || true)")"
    if [ "$n2" != "$n0" ]; then
        guest "wxrandr --output $conn --auto" >/dev/null 2>&1 || true
        sleep 3
        same "...and wxrandr --output $conn --auto puts it back by hand" "$n0" "$(oracle_outputs | grep -c . || true)"
    fi
}

# F2.3 for an X11 session, which is a different claim from common.sh's phase_root: there the tools look
# for a WAYLAND session, here they have to find a display and a cookie.  `env -i` is what makes it a
# measurement rather than an accident -- ssh already hands root an environment with no DISPLAY, but it
# leaves PATH and XDG_* behind; with nothing at all, the only route to the seated session is the scan of
# /proc for one of _SESSION_LEADERS' processes owned by that user, which here is `mate-session` (12
# bytes, inside comm's 15).
phase_x11root() {
    # `env -i` and not `env -i PATH=...`: the tools are zipapps whose shebang is `/usr/bin/env python3`,
    # and with PATH unset execvp falls back to confstr(_CS_PATH) -- /bin:/usr/bin -- which is where the
    # interpreter is.
    # This phase opens its own window and closes it, the way lxqt.sh's does, rather than living off the
    # one phase_marco left up: `--phases x11root` is a run somebody makes, and the anchor below would
    # then rest on whichever of mate-panel and caja happens to be in _NET_CLIENT_LIST -- which nobody
    # has measured here (recon2/mate.md 6 measured the panel's STRUT in _NET_WORKAREA, not its window
    # in the client list), and which would be a claim about MATE rather than about the tools.
    editor_start
    local uu ru orig
    uu=$(guest 'wwmctl -l | wc -l' | tr -d ' \r\n' || true)
    ru=$(root 'w=$(command -v wwmctl); env -i "$w" -l | wc -l' | tr -d ' \r\n' || true)
    # The anchor first: two identical answers prove nothing when both are "no windows".
    if [ "${uu:-0}" -gt 0 ]; then
        same "root with an EMPTY environment lists the seated user's windows" "$uu" "$ru"
    else
        fail "the seated user lists no windows: there is nothing for root's list to be compared against"
    fi
    # The contrast, and the whole point: the original alone, run the same way, cannot open a display.
    orig=$(root 'env -i /usr/bin/wmctrl -l 2>&1' || true)
    want "the real wmctrl, run the same way, cannot open a display" "annot open display|X connection" "$orig"
    uu=$(guest "wxrandr --query | awk '\$2==\"connected\"{print \$1}'" | tr '\n' ' ' || true)
    ru=$(root 'w=$(command -v wxrandr); env -i "$w" --query 2>/dev/null | awk "\$2==\"connected\"{print \$1}"' \
         | tr '\n' ' ' || true)
    if [ -n "$(printf '%s' "$uu" | tr -d ' ')" ]; then
        same "root with an empty environment names the same connected outputs" "$uu" "$ru"
    else
        fail "the seated user names no connected output: nothing to compare root's --query against"
    fi
    # LightDM writes ~/.Xauthority, which is find_xauthority()'s last fallback, so this desktop is the
    # one where the leaders' scan could be masked by luck.  Naming both files says which route answered.
    note "cookie: XAUTHORITY=$(guest 'echo $XAUTHORITY' | tr -d '\r' || true), \
~/.Xauthority $(guest 'test -f $HOME/.Xauthority && echo present || echo absent' | tr -d '\r' || true)"
    note "the session leader root learned DISPLAY and XAUTHORITY from is mate-session:"
    note "  $(root 'ps -o comm= -u test | sort -u | tr "\n" " "' || true)"
    guest "pkill xterm; true" >/dev/null 2>&1 || true
}
