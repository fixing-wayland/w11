# live-smoke.d/sway.sh -- sway 1.11 (resolute-sway), the protocol path.
#
# Measured 2026-09-08, package route: wxrandr --listmonitors and --off/--auto,
# `wwmctl -d` showing the three per-output workspaces, a foot window found by
# class with geometry/activate correct, `type` arriving byte-exact through
# uinput AND through `--vkbd on` (no privilege), set_desktop/get_desktop
# switching workspaces, mouse and wxprop correct.
#
# There is no bridge, no monitors.xml and no portal here at all -- which is why
# the phase list is the short one and why the observation this file keeps is an
# ORDERING one: `wxrandr --listmonitors` lists Virtual-3, Virtual-2, Virtual-1
# (sway's get_outputs order) where xrandr would list in output order.  Harmless,
# and a parity nit for scripts that index monitors by position.
#
# The same file, unchanged, is what fedora44-sway (sway 1.11 on Fedora 44),
# arch-sway (1:1.12) and nixos-sway (1.12) run.  All six tools were measured
# identical on all three before any of those flavors existed -- the protocol
# path is a compositor property and none of these distros moves a socket, a
# global or a version string [recon2/fedora 3.1, recon2/arch 3.1, recon2/nixos].
# What does differ is what is INSTALLED beside sway: Arch's archive carries
# xdotool 4.20260303.1, the exact version the parity oracle compares against,
# and Fedora's carries 3.20211022.1 like Ubuntu's [recon2/arch 2, recon2/fedora 7].
# Neither changes a check here; both are why arch-sway is the flavor the
# parity job runs on.

SMOKE_PHASES="busrec install windows wm proxy input display root nodialog"
EDITOR_CLASS=foot

# No text editor on this golden: the "editor" is a terminal running `cat`, which
# is exactly how the hand measurement read the typed bytes back -- the shell's
# line discipline flushes a line per Return, so `editor_save` is a Return.
#
# `cat >>` and not `cat >`: editor_clear truncates the file, and a descriptor
# opened without O_APPEND keeps its offset across a truncation -- the next line
# typed would land at the old offset behind a run of NULs, which bash then drops
# from $(...) with a warning, so the comparison would pass by accident on a file
# that is garbage.  O_APPEND makes the write after a truncate land at offset 0.
editor_start() {
    guest "rm -f $SMOKE_FILE; setsid nohup foot -- sh -c 'cat >> $SMOKE_FILE' >/dev/null 2>&1 </dev/null &
           sleep 3; true" >/dev/null || true
}
editor_save()  { guest "wdotool key Return" >/dev/null || true; }
editor_clear() { guest "wdotool key ctrl+u; : > $SMOKE_FILE" >/dev/null 2>&1 || true; }

# sway tiles, and the hand measurement on resolute-sway measured exactly this
# much: "foot window found by class, geometry, activate; set_desktop/get_desktop
# switch workspaces; mouse and wxprop correct".  windowmove/windowsize on a
# TILED window are ignored by the compositor, windowminimize has no meaning in a
# tiling layout, and the maximize pair was never measured here -- so the two
# common phases are overridden with the measured subset rather than run whole
# and left to fail on claims nobody has made.  (The floating alternative,
# `swaymsg '[app_id=foot] floating enable'` before the geometry steps, would
# measure a different window manager state than the one that was measured.)
phase_windows() {
    guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
    editor_start
    local out
    out=$(await 30 '[0-9]' "wdotool search --class $EDITOR_CLASS | head -1" || true)
    WIN=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$WIN" ]; then fail "wdotool search --class $EDITOR_CLASS found no window [$(ev "$out")]"; return 1; fi
    pass "wdotool search --class $EDITOR_CLASS -> $WIN"
    want "getwindowname is not empty" "." "$(guest "wdotool getwindowname $WIN" || true)"
    want "getwindowgeometry reads the tiled window's geometry" "[0-9]+x[0-9]+" \
         "$(guest "wdotool getwindowgeometry $WIN" || true)"
    note "(no windowmove/windowsize/windowminimize here: sway tiles, and none of the three was measured on it)"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    same "windowactivate --sync then getactivewindow is that window" "$WIN" \
         "$(guest 'wdotool getactivewindow' | tr -d ' \n' || true)"
    guest "wdotool set_desktop 1" >/dev/null || true
    sleep 1
    same "set_desktop 1 -> get_desktop (sway workspaces)" "1" "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
    guest "wdotool set_desktop 0" >/dev/null || true
    sleep 1
    same "set_desktop 0 -> get_desktop" "0" "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    guest "wdotool mousemove 300 300" >/dev/null || true
    want "mousemove 300 300 -> getmouselocation" "x:300 y:300" "$(guest 'wdotool getmouselocation' || true)"
    local st=0
    guestq "wdotool mousemove 300 300 click 1" || st=$?
    ok "mousemove 300 300 click 1" "$st"
}

phase_wm() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    want "wwmctl -l lists the terminal" "." "$(guest 'wwmctl -l' || true)"
    want "wwmctl -l -G carries a geometry column" "[0-9]+ +[0-9]+ +[0-9]+ +[0-9]+" "$(guest 'wwmctl -l -G' || true)"
    want "wxprop -id answers for a sway window" "." "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    # On sway the root is Xwayland's real root: _NET_CLIENT_LIST names X clients
    # and no native window (measured on a headless sway 1.11 with foot and xterm
    # side by side: the list carried the xterm's id alone).  Only GNOME's root
    # is re-synthesized from the bridge.  So the check needs an X client.
    if guest 'command -v xterm' >/dev/null 2>&1; then
        guest "setsid nohup xterm -T smokex -e sh -c 'sleep 600' >/dev/null 2>&1 </dev/null & sleep 3; true" >/dev/null || true
        want "wxprop -root _NET_CLIENT_LIST names the X client (and no native window: the X root is real here)" \
             "window id # 0x[0-9a-f]+" "$(guest 'wxprop -root _NET_CLIENT_LIST' || true)"
        guest "pkill -x xterm; true" >/dev/null 2>&1 || true
    else
        note "(no xterm in this image: _NET_CLIENT_LIST on sway is the X plane, nothing to list without an X client)"
    fi
    note "(the b7a60f0 maximize pair is a GNOME/KWin check: sway has no _NET_WM_STATE maximize to toggle)"
}

# The one input route that is sway's alone: the virtual keyboard, which needs no
# uinput and no privilege at all.  Both arrived byte-exact in the measurement.
layout_phase() {
    editor_clear
    guest "wdotool --vkbd on type --delay 30 -- 'vkbd: yz@'" >/dev/null 2>&1 || true
    sleep 0.5
    editor_save
    sleep 1
    same "--vkbd on types byte-exact with no uinput and no privilege" "vkbd: yz@" "$(editor_text)"
    # sway names them 1..N, one pinned to each output; wwmctl -d prints the number, not the output
    want "wwmctl -d shows one workspace per output" "^2 +[*-] " "$(guest 'wwmctl -d' || true)"
    note "listmonitors order: $(guest 'wxrandr --listmonitors' | tr '\n' ' ' || true)"
    note "(sway answers in get_outputs order, xrandr in output order: a parity nit, not a failure)"
}
