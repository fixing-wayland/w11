# live-smoke.d/river.sh -- river 0.4.8 + tinyrwm, the flavor that measures a NEGATIVE result.
#
# One flavor runs it: arch-river.  Ubuntu has no river at any version, and building it there
# would need a zig 0.16 tarball and a source build of wlroots 0.20 on top of the window
# manager this image already builds [recon2/river 1, 5].
#
# river is a compositor and nothing else.  Window management is a separate client over
# river_window_manager_v1, that protocol is EXCLUSIVE (binding it second while tinyrwm holds
# it answers `unavailable` and nothing more, and that event is guaranteed to be the first and
# only one), and with no window-manager client running the compositor maps no windows at all:
# a foot client connects and lives while both zwlr_foreign_toplevel_manager_v1 and
# ext_foreign_toplevel_list_v1 report ZERO handles and `wwmctl -l` prints nothing with rc 0
# [recon2/river 2b].  So every number in this file is river PLUS tinyrwm, upstream's own
# reference window manager, built from a pinned commit by the flavor.
#
# THE MEASUREMENT.  river 0.4's wlr-foreign-toplevel is READ-ONLY.  Window.zig creates the
# handle and only ever pushes title, app_id and activated at it; it registers no listener for
# any handle request (`grep request_close|request_activate|request_maximize|request_minimize|
# request_fullscreen` over river/*.zig finds none) and never sends maximized, minimized or
# fullscreen.  Measured against a live 0.4.8 + tinyrwm with one foot window:
#
#   windowclose 1000000  (twice, 5 s apart)   rc 0   foot still running, still listed
#   windowactivate 1000000 (other focused)    rc 0   ACTIVATED stayed on the other window
#   windowminimize 1000000                    rc 0   states unchanged
#   windowstate --add FULLSCREEN 1000000      rc 0   states stayed ['ACTIVATED']
#
# and the same four on river-classic 0.3.17, where close really closed and FULLSCREEN really
# set [recon2/river 2a].  rc 0 and nothing happening is worse than a refusal, and
# backend_wlr.py's verify-after-act is the answer: every mutating request now waits
# VERIFY_TIMEOUT (0.5 s) for the handle's own state event and WARNS when it never comes.  This
# file is where that warning is checked against the compositor that provokes it -- and it
# checks the window did not change too, because a warning about a window that really moved
# would be its own kind of wrong.
#
# What is NOT YET, and the route: real window control on river 0.4.  The lowest rung of the
# AGENTS.md ladder that reaches it is 1 -- river_window_manager_v1, river's own protocol --
# and nothing we can send over it reopens the door: binding it second while tinyrwm holds it
# answers opcode 0, `unavailable`, which the XML itself calls the first and only event the
# server will send (river-window-management-v1.xml:122), and a session with no window manager
# has no windows to drive at all [recon2/river 2b].
# Route 6, a patched river that wires request_close and request_activate into the handle it
# already creates, is the next one and is a package nobody has costed.  river-classic 0.3.17
# is a third answer for close and fullscreen only, and is a legacy line.  Until one of those
# is taken, the honest behaviour is the warning, and it is what runs here.
#
# Everything else on river already worked unmodified: detection lands on WlrBackend with no
# change; wxrandr reads and applies (pos, off/auto, right-of, scale) and warandr answers
# `wlr`; typing landed byte-exact through zwp_virtual_keyboard_v1 on a box with no
# /dev/uinput; `wmirror --check` found both capture managers.  `wl-mirror` itself could NOT be
# run in the recon (no EGL in that sandbox: `failed to create EGL display`), so this rig is
# the first place a real mirror on river is measured at all [recon2/river 3].
SMOKE_PHASES="busrec install windows wm proxy input display mirror root nodialog"
EDITOR_CLASS=foot

#: Where the second terminal writes its own pid.  In $HOME and not /tmp, for the reason
#: common.sh gives for SMOKE_FILE: noble's systemd-tmpfiles empties /tmp on every boot.
#: Single-quoted, so $HOME is expanded by the GUEST's shell.
SECOND_PID='$HOME/w11-second.pid'

editor_start() {
    guest "rm -f $SMOKE_FILE; setsid nohup foot -- sh -c 'cat >> $SMOKE_FILE' >/dev/null 2>&1 </dev/null &
           sleep 3; true" >/dev/null || true
}
editor_save()  { guest "wdotool key Return" >/dev/null || true; }
editor_clear() { guest "wdotool key ctrl+u; : > $SMOKE_FILE" >/dev/null 2>&1 || true; }

# ---------------------------------------------------------------- windows
# The reading half works and the writing half does not, and this phase is that sentence in
# checks.  There is no oracle for a native window on river -- no IPC, no bus name, and
# riverctl is gone from 0.4 -- so the second opinion for "did anything change" is the tool's
# own listing taken before and after, which is what the protocol's state events feed.
phase_windows() {
    guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
    editor_start
    local out
    out=$(await 30 '[0-9]' "wdotool search --class $EDITOR_CLASS | head -1" || true)
    WIN=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$WIN" ]; then
        # The first thing to suspect is the window manager, not the tools: with no WM client
        # river maps nothing and this is exactly what that looks like.
        fail "wdotool search --class $EDITOR_CLASS found no window -- is tinyrwm running? \
river with no window-manager client reports zero toplevels [$(ev "$out")]"
        note "river -c: $(guest 'pgrep -a -x tinyrwm' || true)"
        return 1
    fi
    pass "wdotool search --class $EDITOR_CLASS -> $WIN"
    want "getwindowname is not empty" "." "$(guest "wdotool getwindowname $WIN" || true)"
    # The NATIVE half of the geometry gap, as on labwc: $WIN is foot, no X server has heard of
    # it, and zwlr_foreign_toplevel_management_v1 carries no rectangle, so 0,0 plus the output
    # rectangle is everything this session knows.  Recorded and not asserted, for the reason
    # labwc.sh gives at the same check: pinning `Position: 0,0` would go red the day a real
    # rectangle arrives.  NOT YET, rung 1 -- a foreign-toplevel protocol that carries a
    # rectangle, which costs wlroots writing and shipping one -- with rung 6, a patched
    # compositor, as the fallback.  The XWayland half is route 5 and is checked in
    # river_xwayland below, against the real X server.
    local wgeom; wgeom=$(guest "wdotool getwindowgeometry $WIN" || true)
    note "getwindowgeometry, native window (the floor: no protocol carries a rectangle yet, \
AGENTS.md route 1): $(printf '%s' "$wgeom" | tr '\n' '|')"
    # getdisplaygeometry is the zxdg_output LAYOUT BOX and not one head's mode -- measured
    # 2560 720 across two heads where WlrBackend.display_size() would have said 1280 720
    # [recon2/river 3].
    note "getdisplaygeometry (the xdg-output layout box): $(guest 'wdotool getdisplaygeometry' \
| tr -d '\r\n' || true)"
    river_read_only
}

# The four read-only requests.  Each one: send it, then assert BOTH halves -- the tool warned,
# and the window is unchanged.  A check on the warning alone would pass on a compositor that
# warned and then closed the window anyway; a check on the window alone would pass on a tool
# that quietly did nothing, which is the state this file exists to end.
river_read_only() {
    local before after out
    before=$(guest 'wwmctl -lx' || true)
    # 1. minimize.  Its reason names sway and river-classic as well, because neither of those
    #    has a minimized state either and blaming river 0.4 alone would be a false attribution.
    out=$(guest "wdotool windowminimize $WIN 2>&1" || true)
    want "windowminimize warns that the request was accepted and not applied" \
         "accepted set_minimized and did not apply it" "$out"
    want "...and the reason names river 0.4's read-only foreign-toplevel" \
         "river 0.4's wlr-foreign-toplevel is read-only" "$out"
    # 2. fullscreen.
    out=$(guest "wdotool windowstate --add FULLSCREEN $WIN 2>&1" || true)
    want "windowstate --add FULLSCREEN warns instead of reporting success" \
         "accepted set_fullscreen and did not apply it" "$out"
    wantnot "and the window did not go fullscreen" "_NET_WM_STATE_FULLSCREEN" \
            "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    # 3. activate.  A second window is needed for this to mean anything: activating the window
    #    that is already focused proves nothing whichever way it goes.  Its pid is written by
    #    the shell that BECOMES it (`exec`, so $$ stays the terminal's), because there is no
    #    other way to get it: this is a NATIVE foot window, and the toplevel protocol carries
    #    no pid for one, so `getwindowpid` has nothing to answer with here (the XWayland half
    #    is answered off the X plane in river_xwayland below; the native half is NOT YET at
    #    rung 1, a foreign-toplevel protocol that carries the pid).  And
    #    `pkill -f 'sleep 600'` would match the `sh -c` wrapper running this very command --
    #    the self-match hypr.sh documents.  It has to go before the input phase, since
    #    windowactivate does not work here and typing would otherwise land in whichever window
    #    river last focused.
    guest "setsid nohup sh -c 'echo \$\$ > $SECOND_PID; exec foot -- sh -c \"sleep 600\"' \
           >/dev/null 2>&1 </dev/null & sleep 3; true" >/dev/null || true
    local second; second=$(guest "wdotool search --class $EDITOR_CLASS" \
        | grep -E '^[0-9]+$' | grep -vx "$WIN" | head -1)
    if [ -z "$second" ]; then
        fail "a second $EDITOR_CLASS window did not appear: the activate check needs two"
    else
        # Which of the two river focused is river's business -- tinyrwm's policy, not ours --
        # so it is READ and the request is aimed at the OTHER one.  That way the claim is
        # exact whichever way the compositor went: activate the window that is not focused,
        # and focus must not move.  Asking for the one already focused would pass on a
        # compositor that honours the request perfectly.
        local was other
        was=$(guest 'wdotool getactivewindow' | tr -d ' \r\n' || true)
        if [ "$was" = "$WIN" ]; then other=$second; else other=$WIN; fi
        note "river focused $was of the two ($WIN and $second); asking it to activate $other"
        out=$(guest "wdotool windowactivate $other 2>&1" || true)
        want "windowactivate warns that the compositor took the request and did nothing" \
             "accepted activate and did not apply it" "$out"
        sleep 1
        same "and the focus really did not move: $was is still the active window" \
             "$was" "$(guest 'wdotool getactivewindow' | tr -d ' \r\n' || true)"
        # 4. close.  Its reason has a third cause the others do not -- the client itself may be
        #    asking to save -- so the wording is CLOSE_REASON and not the read-only line, and
        #    what makes this river's and not the client's is that the window is still listed.
        out=$(guest "wdotool windowclose $second 2>&1" || true)
        want "windowclose warns that the window did not close within the deadline" \
             "did not close within" "$out"
        want "...and offers the compositor as one of the causes, by name" \
             "the compositor ignored the request \(river 0.4\)" "$out"
        sleep 2
        want "and the window really is still there: this is river's silence, not the client's" \
             "^$second$" "$(guest "wdotool search --class $EDITOR_CLASS" | grep -E '^[0-9]+$' || true)"
        guest "kill \$(cat $SECOND_PID 2>/dev/null) 2>/dev/null; rm -f $SECOND_PID; true" \
            >/dev/null 2>&1 || true
        sleep 1
    fi
    after=$(guest 'wwmctl -lx' || true)
    note "before: $(ev "$before")"
    note "after:  $(ev "$after")"
    # The refusals that are refusals rather than silences, for contrast: these four have no
    # request in the protocol at all and so fail loudly on every wlroots compositor.
    want "windowmove is a refusal and not a silence, naming the protocol" \
         "windowmove is not supported by the wlr backend: zwlr_foreign_toplevel_management_v1 carries no geometry" \
         "$(guest "wdotool windowmove $WIN 100 100 2>&1" || true)"
    guest "wdotool windowactivate --sync $WIN" >/dev/null 2>&1 || true
    guest "wdotool mousemove 300 300" >/dev/null || true
    want "mousemove 300 300 -> getmouselocation" "x:300 y:300" "$(guest 'wdotool getmouselocation' || true)"
    local st=0
    guestq "wdotool mousemove 300 300 click 1" || st=$?
    ok "mousemove 300 300 click 1" "$st"
}

# ---------------------------------------------------------------- the window manager
phase_wm() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    want "wwmctl -l lists the terminal" "." "$(guest 'wwmctl -l' || true)"
    # `Name: wlroots wm` is Xwayland's own _NET_SUPPORTING_WM_CHECK string, not river's: river
    # sets no window-manager name of its own, and neither does tinyrwm [recon2/river 3].
    want "wwmctl -m prints the wlroots xwm's name, because river sets none" \
         "^Name: wlroots wm$" "$(guest 'wwmctl -m' || true)"
    # river has TAGS, not workspaces, and publishes no workspace protocol at all -- there is no
    # ext_workspace_manager_v1 in its 53 globals -- so this is the one wlroots flavor where
    # `wwmctl -d` is expected to refuse, and the refusal is the check [recon2/river 2].
    want "wwmctl -d refuses: river publishes no workspace protocol (tags are not workspaces)" \
         "not supported by the wlr backend" "$(guest 'wwmctl -d 2>&1' || true)"
    # NOT YET, and the route: river-classic's zriver_status_manager_v1 would give tags as
    # desktops (AGENTS.md route 1, on the OTHER river), and on 0.4 there is nothing below
    # route 6.  Recorded here so a reader of a green run knows what the refusal costs.
    note "(tags as desktops is not done yet on river 0.4: the lowest route is 1 on river-classic, \
whose zriver_status_manager_v1 carries them, and 6 on 0.4 itself)"
    river_xwayland
}

# The X id for WM_CLASS $1, looked up through the REAL xprop -- the same helper labwc.sh
# carries, copied rather than shared because common.sh belongs to another batch.  Taking the
# first id in _NET_CLIENT_LIST would be wrong the moment a session has any other X client:
# river's Xwayland is started for the session, not for our xterm.
xplane_id() {
    local ids id
    # The ids are collected FIRST and the loop is a `for` over a variable, deliberately: a
    # `while read` fed by a pipe would have each `guest` call inside it read the loop's own
    # stdin -- vmctl user opens an ssh, and ssh consumes whatever it is given -- and the loop
    # would see one id and then end.
    ids=$(guest "xprop -root _NET_CLIENT_LIST" | grep -o '0x[0-9a-f]*' || true)
    for id in $ids; do
        if guest "xprop -id $id WM_CLASS" | grep -q "\"$1\""; then
            printf '%s\n' "$id"
            return 0
        fi
    done
    return 1
}

river_xwayland() {
    if ! guest 'command -v xterm' >/dev/null 2>&1; then
        note "(no xterm in this image: the XWayland id join needs a real X client)"
        return 0
    fi
    guest "setsid nohup xterm -T smokex -e sh -c 'sleep 600' >/dev/null 2>&1 </dev/null & sleep 3; true" \
        >/dev/null || true
    local list xid bare
    list=$(guest 'wxprop -root _NET_CLIENT_LIST' || true)
    want "wxprop -root _NET_CLIENT_LIST names the X client" "window id # 0x[0-9a-f]+" "$list"
    xid=$(xplane_id xterm || true)
    # poll for the X-plane join: the window can be in _NET_CLIENT_LIST before the wlr
    # floor lists it, so a single fetch races empty (arch-river, CI run 34733966065).
    # Driver-side retry keeps the recorded command `wwmctl -lGpx` unchanged, so the
    # committed recording still replays (the recorded row answers the first poll) while
    # a live run waits the join out, bounded ~10s.  LIVE_SMOKE_SLEEP=0 zeroes the sleep
    # in replay, where the first call already matches.
    local row=''; local _i
    for _i in 1 2 3 4 5 6 7 8 9 10; do
        row=$(guest 'wwmctl -lGpx' | grep -i xterm || true)
        [ -n "$row" ] && break
        sleep 1
    done
    if [ -n "$xid" ]; then
        # The recon's own numbers for this exact join on river: the XTerm was 0x0040000c with
        # pid 51601 to the real wmctrl and 0x000f4243 with pid 0 and class XTerm.XTerm to us,
        # because WlrBackend had no views() [recon2/river 3].
        bare=$(printf '%s' "${xid#0x}" | sed 's/^0*//')
        want "wwmctl lists that window under its real X id $xid, not a minted 0x000f42xx" \
             "0x0*$bare\b" "$row"
    else
        note "(the real xprop found no X client to join to: see the line above)"
    fi
    want "and with a real pid from the X server, where the floor printed 0" \
         "^0x[0-9a-f]+ +[0-9-]+ +[1-9][0-9]* " "$row"
    # The RECTANGLE, AGENTS.md route 5, exactly as labwc.sh checks it: our own id for this
    # xterm against the same window's rectangle read by the ORIGINAL xdotool over its own
    # connection.  river is the flavor where this check earns its keep twice over, because the
    # xterm it starts really does sit at 0,0 here -- the CI run of 2026-09-11 measured
    # `0x0040000c ... 0 0 484 316` on arch-river [goal2/ci/rig-arch-river.log:559] -- so a
    # check on "a position that is not 0,0" would have been red on a correct rectangle, and
    # what says the fold happened is agreement with the oracle on all four numbers.
    local wid ours theirs
    # By TITLE and not by class: the app id an xwm hands the toplevel is not the same string on
    # every compositor -- labwc's xterm arrives as `xterm` and river's recon read `XTerm.XTerm`
    # [recon2/river 3] -- while `-T smokex` above is ours on both.
    wid=$(guest 'wdotool search --name smokex | head -1' | tr -d ' \r\n' || true)
    if [ -z "$wid" ] || [ -z "$xid" ] || ! guest 'command -v xdotool' >/dev/null 2>&1; then
        # Not a second FAIL, for the reason the $xid branch above gives: a listing with no row
        # for this xterm is what the id check has just gone red about.  It is also what the
        # REPLAY rig looks like -- the recordings under tests/fixtures/live/ were captured
        # before this command existed and fake-vmctl answers an unrecorded command with
        # nothing -- so re-recording them is what makes this check run there too.
        note "(no wdotool id for the xterm, or no xdotool to be the oracle: the rectangle check \
needs both -- id '$(ev "$wid")', X id '$(ev "$xid")')"
    else
        ours=$(win_geom "$wid")
        theirs=$(guest "xdotool getwindowgeometry $xid" | awk '
            /Position:/ { pos = $2 } /Geometry:/ { geo = $2 }
            END { printf "%s %s\n", pos, geo }')
        note "getwindowgeometry, XWayland window: ours '$ours', the oracle xdotool '$theirs'"
        same "getwindowgeometry on the XWayland window is the X server's own rectangle \
(route 5), not the floor's 0,0 + a head's mode" "$theirs" "$ours"
    fi
    # The PID, the other half of the same route-5 join.  `_NET_WM_PID` is on the X window and
    # zwlr_foreign_toplevel_management_v1 carries no pid at all, so `wdotool getwindowpid` on
    # this xterm answered `window 1000000 has no pid associated with it` while the X plane in
    # the same process knew the number -- `wwmctl -lGpx` printed `0x0040000c -1 2045 718 395
    # 484 316` for that very window on the resolute-labwc golden 2026-09-12
    # [goal2/requests-batch-12.md 4].  The oracle is the ORIGINAL wmctrl's own pid column for
    # the same X id, read over its own connection to the X server.
    local theirrow ourpid theirpid
    theirrow=$(guest 'wmctrl -lGpx' | grep -i xterm || true)
    theirpid=$(printf '%s\n' "$theirrow" | awk '{ print $3 }')
    if [ -n "$wid" ] && [ -n "$xid" ]; then
        ourpid=$(guest "wdotool getwindowpid $wid 2>&1" | tr -d ' \r\n' || true)
    fi
    # Exactly two answers degrade to a note, and NEITHER of them is a pid: nothing at all, and
    # `fake-vmctl: nothing recorded for ...` (guest() folds the fake's stderr into its stdout,
    # live-smoke.sh:352), which is what a recording cut before this command existed replies under
    # FAKE_VMCTL_STRICT=1.  Everything else goes through `same` -- above all the refusal sentence
    # `window 1000000 has no pid associated with it`, which is the regression this check exists to
    # catch and which an "is it all digits?" guard would have waved through as a note.  A live run
    # with no id is already red one line earlier, at the X-id `want`.
    case "${ourpid:-}" in
        ""|*nothingrecorded*)
            note "(no getwindowpid answer for the xterm -- ours '$(ev "${ourpid:-}")', the original \
wmctrl '$(ev "${theirpid:-}")': a recording cut before this check answers neither)" ;;
        *)
            if [ -n "$theirpid" ]; then
                same "getwindowpid on the XWayland window is the X server's _NET_WM_PID (route 5), \
not the floor's 0" "$theirpid" "$ourpid"
            else
                note "(the original wmctrl printed no pid column for the xterm, so it cannot be the \
oracle here -- ours '$(ev "$ourpid")')"
            fi ;;
    esac
    note "ours:   $row"
    note "theirs: $theirrow"
    guest "pkill -x xterm; true" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------- input
# river advertises zwp_virtual_keyboard_manager_v1 v1 and zwlr_virtual_pointer_manager_v1 v2,
# so nothing injected needs root, a group or a udev rule -- measured on a box with no
# /dev/uinput, where `wdotool type "hello river"` into a foot running `cat` produced exactly
# that file [recon2/river 3].
layout_phase() {
    editor_clear
    guest "wdotool --vkbd on type --delay 30 -- 'vkbd: yz@'" >/dev/null 2>&1 || true
    sleep 0.5
    editor_save
    sleep 1
    same "--vkbd on types byte-exact with no uinput and no privilege" "vkbd: yz@" "$(editor_text)"
    # The layout.  river advertises river_xkb_config_v1 v2 and river_xkb_bindings_v1 v3, and no
    # reader in wdotool/xkbmap.py binds either, so the keymap alone answers here -- which is
    # right on a golden carrying one layout and is the check below.  A SECOND group read off
    # river's own protocol is NOT YET: the route is 1, a sixth reader beside the Hyprland,
    # Wayfire, KWin, GNOME and Cinnamon ones, and nobody has scheduled it.
    local ex; ex=$(guest 'wdotool keys explain yz@ 2>&1' || true)
    note "keys explain: $(printf '%s\n' "$ex" | tr '\n' '|')"
    want "keys explain reads the keymap off the wire" "group [0-9]+ of [0-9]+.*, from wayland" "$ex"
    wantnot "and no group is assumed on a single-layout session" "\(assumed\)" "$ex"
}

# ---------------------------------------------------------------- display
# The half of river that has nothing wrong with it.  Applies land, all rc 0 and verified by a
# following --query: pos swaps, --off, --auto --right-of, --scale 2 [recon2/river 3].  The
# enumeration-order nit is sway's too: the wlr manager listed HEADLESS-2 before HEADLESS-1, so
# --listmonitors indexes are registry order and not layout order.
phase_display() {
    want "wxrandr --print-backend is wlr" "^wlr$" \
         "$(guest 'wxrandr --print-backend' | tr -d ' \r' || true)"
    local outs; outs=$(oracle_outputs || true)
    note "native oracle (wlr-randr): $(ev "$outs")"
    note "(wlr-randr speaks the protocol wxrandr writes through -- a weak oracle; river has no \
output query of its own, riverctl is gone from 0.4, and the independent reading is the \
host-side screendump per head)"
    same "wxrandr --listmonitors counts the outputs wlr-randr has enabled" \
         "$(printf '%s\n' "$outs" | grep -c .)" \
         "$(guest 'wxrandr --listmonitors' | sed -n 's/^Monitors: //p' | tr -d ' \r')"
    note "listmonitors order: $(guest 'wxrandr --listmonitors' | tr '\n' ' ' || true)"
    # river 0.4.8's output manager will not re-enable a head it switched --off: wlr-randr --on answers
    # `failed to apply configuration` (no IPC, riverctl gone from 0.4), so common_display_phase's three
    # re-enable checks are route-6 xwants here [run 34667595059, diagnosis §4].
    OFF_HEAD_STAYS_OFF="river 0.4.8's output manager answers 'failed to apply configuration' to wlr-randr --on"
    common_display_phase
}

# ---------------------------------------------------------------- mirror
# river advertises zwlr_screencopy_manager_v1 v3 AND ext_image_copy_capture_manager_v1 v1, and
# `wmirror --check` listed both -- but the helper itself has never run on river anywhere: the
# recon's sandbox had no EGL device and wl-mirror died with `failed to create EGL display`,
# which is that sandbox and not river [recon2/river 3].  So this is the first real mirror on
# this compositor, and the checks are the same ones sway and labwc pass.
phase_mirror() {
    local chk; chk=$(guest 'wmirror --check' || true)
    want "wmirror --check finds the helper" "wl-mirror" "$chk"
    want "and both capture protocols river advertises" \
         "capture: .*zwlr_screencopy_manager_v1.*ext_image_copy_capture" "$chk"
    local pair first second
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    if [ -z "$second" ]; then
        note "one head only: a mirror needs a target, --heads 2 is what gives it"
        return 0
    fi
    # The region offset is in LAYOUT coordinates and not in the source head's own, so it is built from
    # where that head actually IS: a fixed `+100+100` is inside the source only while the source sits at
    # the origin, and `display_pair` picks the RIGHTMOST head.  Measured in CI on 2026-09-09 (run
    # 34308982263, the labwc job): `wmirror: the region 800x600+100+100 is not inside Virtual-3, which
    # is 1920x1080+9600+1080`, and the three checks after it fell with that refusal -- four FAILs each
    # on labwc, xfce-wayland, budgie and lxqt-wayland, and four more on resolute-wayfire.
    # vm/live-smoke.d/hypr.sh and wayfire.sh carry the same three lines for the same reason.
    local org rx ry
    org=$(oracle_outputs | sed -n "s/^$first //p" | head -1)
    rx=$(( ${org%%,*} + 100 )); ry=$(( ${org##*,} + 100 ))
    local region="800x600+$rx+$ry"
    local out st=0
    out=$(guest "wmirror $first --to $second --region $region") || st=$?
    ok "wmirror $first --to $second --region $region (never run on river before)" "$st"
    want "the started line names target, source, region, scaling and the helper's pid" \
         "$second <- $first +region 800x600\+$rx\+$ry +scaling fit +wl-mirror pid [0-9]+" "$out"
    want "wmirror --list shows it running" "$second <- $first" "$(guest 'wmirror --list' || true)"
    want "wmirror --stop ends it" "^stopped +$second <- $first" \
         "$(guest "wmirror --stop $second" || true)"
    same "and no wl-mirror is left behind" "" "$(guest 'pgrep -x wl-mirror' | tr -d ' \r\n' || true)"
}
