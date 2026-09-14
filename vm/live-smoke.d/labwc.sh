# live-smoke.d/labwc.sh -- labwc 0.9.3 on wlroots 0.19.2, and with it the whole generic
# wlroots floor.
#
# Four flavors run this file: resolute-labwc (bare labwc under greetd), resolute-budgie and
# resolute-xfce-wayland and resolute-lxqt-wayland (three shipped desktops whose Wayland
# session IS labwc, each of which sources this file and overrides its terminal).  What they
# share is the thing worth measuring: labwc has no IPC socket at all -- the package is labwc,
# labnag and lab-sensible-terminal and nothing else -- so detection lands on WlrBackend and
# wxrandr's `wlr` backend, and every check below runs through the GENERIC code
# [recon2/labwc 1, 4].  resolute-sway, the only other wlroots image, takes the sway IPC
# backend instead and so has never exercised a line of it.
#
# Every claim here is a byte the recon measured against a real headless labwc 0.9.3
# [recon2/labwc, recon2/budgie, recon2/xfce-wayland, recon2/openbox B] or a string the unit
# tests pin (tests/test_backend_wlr.py, tests/test_backend_wlr_workspaces.py,
# tests/test_labwc_live.py).  What NOBODY has run is this file: no golden of any of the four
# flavors exists yet, so tests/fixtures/live/ carries no labwc recording and NOT-YET-RUN says
# so for all four tokens.
#
# The floor as it was, before batch 8, on one headless labwc with an xterm and a foot:
#
#   $ wwmctl -lGpx
#   0x000f4240 -1 0      0    0    1280 720  xterm.xterm    w11 xtermwin
#   0x000f4241 -1 0      0    0    1280 720  foot.foot      w11 footwin
#   $ wwmctl -d
#   get_desktop is not supported by the wlr backend                              (rc 1)
#
# while that xterm's real X id was 0x40000c and the X server's own WM_CLASS for it was
# "xterm", "XTerm".  A same-compositor control settled that this is the backend and not
# labwc: on ONE headless sway 1.11, the same foot window read `0x00000005 0 85920` through
# the sway backend and `0x000f4240 -1 0` under WDOTOOL_BACKEND=wlr [recon2/labwc 3].  Three
# of those columns are what this file measures the fix of; the fourth (geometry for native
# windows) has no request in the protocol and is NOT YET -- the lowest route that would give
# it is AGENTS.md route 5, ConfigureWindow over the X plane for XWayland windows only, which
# would leave a listing where half the windows can be moved, so it needs a decision before it
# needs code [recon2/labwc 6c].
#
# The one capability labwc has that sway 1.11 has not: ext_workspace_manager_v1 v1 (and
# zcosmic_workspace_manager_v1 v1), where `wayland-info | grep -cE "ext_workspace|zcosmic_workspace"`
# on sway 1.11 in the same guest printed 0 [recon2/labwc 2].  vm/build-image.sh's labwc_config
# writes three desktops named 1, 2 and 3 into ~/.config/labwc/rc.xml precisely so that this
# file has something to count.
#
# `mirror` is in the phase list because labwc advertises BOTH capture protocols --
# zwlr_screencopy_manager_v1 v3 and ext_image_copy_capture_manager_v1 v1 -- and `wmirror
# --check` listed both and started and stopped a region mirror in the recon [recon2/labwc 3].
# `nodialog` is trivially true here and cheap: this session runs no xdg-desktop-portal at all
# (no org.freedesktop.portal.Desktop on the bus, measured on the Xfce variant
# [recon2/xfce-wayland 1]).
SMOKE_PHASES="busrec install windows wm proxy input display mirror root nodialog"

# The terminal, in two knobs rather than a copied editor_start per flavor: budgie.sh and
# lxqt-wayland.sh and xfce-wayland.sh source this file and set these BEFORE the source, which
# is why every one of them is a `:` default and not an assignment.  EDITOR_CLASS is what
# `wdotool search --class` matches (foot's app_id is `foot`), EDITOR_TERM the binary,
# EDITOR_TERM_EXEC the flag that makes it run the rest of the line (`--` for foot, `-e` for
# qterminal, `-x` for xfce4-terminal).
: "${EDITOR_CLASS:=foot}"
: "${EDITOR_TERM:=foot}"
: "${EDITOR_TERM_EXEC:=--}"

# No text editor on any of these goldens: the "editor" is a terminal running `cat`, sway.sh's
# hook including the `cat >>` -- see the paragraph there for why O_APPEND is the difference
# between a comparison and a file full of NULs that compares equal by accident.
editor_start() {
    guest "rm -f $SMOKE_FILE; setsid nohup $EDITOR_TERM $EDITOR_TERM_EXEC sh -c 'cat >> $SMOKE_FILE' \
           >/dev/null 2>&1 </dev/null & sleep 3; true" >/dev/null || true
}
editor_save()  { guest "wdotool key Return" >/dev/null || true; }
editor_clear() { guest "wdotool key ctrl+u; : > $SMOKE_FILE" >/dev/null 2>&1 || true; }

# ---------------------------------------------------------------- the X plane as the oracle
# labwc has no IPC and no D-Bus surface, so there is no second opinion about a NATIVE window
# anywhere on this desktop -- which is the honest reason phase_windows below asserts the
# floor's own numbers rather than a compositor's.  For XWayland windows there IS one, and it
# is the real X server: `xprop` and `wmctrl` are installed on every one of these goldens.

# The X id of the one X client whose WM_CLASS instance is $1, as the REAL xprop reports it.
# Read through xprop and not through wxprop: wxprop -root goes through the same X connection
# our own views() uses, and an oracle sharing the code under test asserts nothing.
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

# ---------------------------------------------------------------- windows
# The generic wlr floor: ids minted from arrival order, pid 0, desktop -1 in the LISTING, and
# no geometry for native windows.  What changed with batch 8 is that the four commands with no
# request behind them now name the protocol instead of borrowing sway's tiling excuse, that
# the requests that ARE sent are verified rather than assumed, and that XWayland rows carry
# their real X id.  The geometry line stays the floor's, and says so.
phase_windows() {
    guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
    editor_start
    local out
    out=$(await 30 '[0-9]' "wdotool search --class $EDITOR_CLASS | head -1" || true)
    WIN=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$WIN" ]; then
        fail "wdotool search --class $EDITOR_CLASS found no window [$(ev "$out")]"
        return 1
    fi
    pass "wdotool search --class $EDITOR_CLASS -> $WIN"
    # 1000000 + arrival order, which is what BASE_ID is; a minted-from-a-handle id would be a
    # different backend and this check would be the first thing to say so.
    want "the id is the wlr floor's: 1000000 plus arrival order" "^10000[0-9]{2}$" "$WIN"
    want "getwindowname is not empty" "." "$(guest "wdotool getwindowname $WIN" || true)"
    # The NATIVE half of the geometry gap, and it is a recorded row rather than a red or an
    # xwant.  $WIN is foot, a Wayland client no X server has ever heard of, and
    # zwlr_foreign_toplevel_management_v1 carries no rectangle -- so 0,0 plus the widest
    # output's mode is the whole truth this session has about it [recon2/labwc 3, measured
    # `Position: 0,0 (screen: 0)` / `Geometry: 1280x720`].  NOT YET: the lowest route is rung 1,
    # a foreign-toplevel protocol that carries a rectangle, which costs wlroots writing and
    # shipping one; the fallback is rung 6, a patched compositor with one geometry event per
    # window.  Pinning `Position: 0,0` as REQUIRED would go red the day either lands, which is
    # the outcome we want, so the reading is recorded and not asserted.
    # The XWAYLAND half is in reach and is checked -- as a plain `same` against an oracle, not
    # an xwant -- in labwc_xwayland below, where the oracle is the real X server (route 5).  Measured on the
    # resolute-labwc golden 2026-09-12: an xterm at 718,395 484x316 to `xdotool
    # getwindowgeometry 0x40000c` read 0,0 1920x1080 from us until backend_wlr.list() folded
    # the X plane in.
    local wgeom; wgeom=$(guest "wdotool getwindowgeometry $WIN" || true)
    note "getwindowgeometry, native window (the floor: no protocol carries a rectangle yet, \
AGENTS.md route 1): $(printf '%s' "$wgeom" | tr '\n' '|')"
    # The refusal's REASON.  README note (c) explains the wlroots family's missing move with
    # sway's tiling; labwc is a STACKING compositor where a move would be natural, so the only
    # true sentence names the protocol [recon2/labwc 6c, backend_wlr.NO_GEOMETRY].
    local refusal; refusal=$(guest "wdotool windowmove $WIN 100 100 2>&1" || true)
    want "windowmove names the protocol as the reason" \
         "windowmove is not supported by the wlr backend: zwlr_foreign_toplevel_management_v1 carries no geometry" \
         "$refusal"
    wantnot "and does NOT blame tiling: labwc stacks" "til(e|ing)" "$refusal"
    want "windowsize says the same thing" "windowsize is not supported by the wlr backend: " \
         "$(guest "wdotool windowsize $WIN 800 600 2>&1" || true)"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    same "windowactivate --sync then getactivewindow is that window" "$WIN" \
         "$(guest 'wdotool getactivewindow' | tr -d ' \n' || true)"
    # labwc really minimizes (measured rc 0 and the state event came back), which is why this
    # is a plain check here and the same command is a NEGATIVE check in river.sh.
    guest "wdotool windowminimize $WIN" >/dev/null || true
    sleep 1
    local act; act=$(guest 'wdotool getactivewindow 2>&1' | tr -d ' \n' || true)
    if [ "$act" = "$WIN" ]; then
        fail "windowminimize left the window active (getactivewindow still $WIN)"
    else
        pass "windowminimize: the window is no longer the active one (getactivewindow: $(ev "$act"))"
    fi
    guest "wdotool windowmap $WIN; wdotool windowactivate --sync $WIN" >/dev/null || true
    sleep 1
    # Desktops.  vm/build-image.sh's labwc_config writes three names into rc.xml; the desktops
    # a session RUNNING labwc (Budgie, Xfce, LXQt) has are its own, so the count is read and
    # not assumed -- Budgie's default is four and Xfce's is four [recon2/budgie, xfce-wayland].
    local nd; nd=$(guest 'wwmctl -d' | grep -c '^[0-9]' || true)
    if [ "${nd:-0}" -lt 2 ]; then
        fail "wwmctl -d lists ${nd:-0} desktop(s): ext_workspace_manager_v1 is what this flavor is for"
    else
        pass "wwmctl -d lists $nd workspaces over ext_workspace_manager_v1 (the floor refused outright)"
        guest "wdotool set_desktop 1" >/dev/null || true
        sleep 1
        same "set_desktop 1 -> get_desktop (of $nd)" "1" \
             "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
        guest "wdotool set_desktop 0" >/dev/null || true
        sleep 1
        same "set_desktop 0 -> get_desktop" "0" "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
    fi
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    guest "wdotool mousemove 300 300" >/dev/null || true
    want "mousemove 300 300 -> getmouselocation" "x:300 y:300" "$(guest 'wdotool getmouselocation' || true)"
    # Recorded, not asserted.  getmouselocation's `window:` field runs hit_test over a list in
    # which every window claims 0,0 + the output rectangle, so it answers whichever window
    # sorts first rather than the one under the pointer -- a floor defect, not labwc's, and one
    # nothing here can fix while the geometry column is a fiction [recon2/labwc 3].
    note "getmouselocation window field (the floor hit-tests over identical rectangles): \
$(guest 'wdotool getmouselocation' || true)"
    local st=0
    guestq "wdotool mousemove 300 300 click 1" || st=$?
    ok "mousemove 300 300 click 1" "$st"
    # ...and then put the desktop back, because that click LANDED and labwc did what its own
    # rc.xml says: a press of button 1 on the Root context is `ShowMenu root-menu`, and with
    # 300,300 on the wallpaper of Virtual-1 the menu (Terminal / Reconfigure / Exit) opens
    # there and takes the keyboard grab.  Every keystroke after it goes to the menu, which is
    # why phase_input read an empty $SMOKE_FILE three times in CI run 34308982263 on this
    # flavor and only on this flavor -- Budgie, Xfce and LXQt put a desktop window under that
    # pixel, so the click never reaches labwc's Root.  Measured on the rig 2026-09-09: the
    # menu is in the head-0 screendump after the click, `wdotool type -- 'us: yz@'` leaves the
    # file empty, and one Escape followed by the same type lands it byte-exact.
    guest "wdotool key Escape" >/dev/null 2>&1 || true
    sleep 0.5
    guest "wdotool windowactivate --sync $WIN" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------- the window manager
phase_wm() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    want "wwmctl -l lists the terminal" "." "$(guest 'wwmctl -l' || true)"
    # `Name: wlroots wm` is read off the X plane, not invented: labwc's wlroots xwm publishes
    # a check window whose _NET_WM_NAME is that string, byte-identical to what a sway session
    # prints [recon2/labwc 3].  WlrBackend.wm_name says the same thing when there is no X
    # plane at all, so this check has one answer either way.
    local m; m=$(guest 'wwmctl -m' || true)
    want "wwmctl -m names the wlroots xwm" "^Name: wlroots wm$" "$m"
    want "and has no class and no pid to give" "^Class: N/A" "$m"
    # The workspace half, over ext_workspace_manager_v1.  The names are the compositor's own:
    # labwc_config writes 1/2/3, Budgie ships four and Xfce four, so what is asserted is the
    # SHAPE -- a starred current row and one row per workspace -- plus the count agreeing with
    # get_num_desktops, which is the only pair here that can disagree.
    local d; d=$(guest 'wwmctl -d' || true)
    # Exactly one starred row, and the index it stars is the one get_desktop answers.  Not
    # `^0 +\*`: which workspace a session starts on is the desktop's business -- labwc_config
    # writes three and Budgie four -- and pinning it to 0 would make this check a claim about
    # a session's startup policy instead of about the workspace reader.
    same "wwmctl -d stars exactly one desktop" "1" \
         "$(printf '%s\n' "$d" | grep -cE '^[0-9]+ +\*')"
    same "and the starred index is the one get_desktop answers" \
         "$(printf '%s\n' "$d" | sed -n 's/^\([0-9]\+\) *\*.*/\1/p')" \
         "$(guest 'wdotool get_desktop' | tr -d ' \r\n' || true)"
    same "wwmctl -d has one row per workspace and get_num_desktops counts the same" \
         "$(printf '%s\n' "$d" | grep -c '^[0-9]')" \
         "$(guest 'wdotool get_num_desktops' | tr -d ' \r\n' || true)"
    note "wwmctl -d: $(printf '%s\n' "$d" | tr '\n' '|')"
    # The maximize pair.  The geometry column is the floor's, so a rectangle cannot say
    # whether anything happened; what CAN is the state the compositor echoes back, which is
    # also exactly what backend_wlr's verify-after-act waits for -- a compositor that accepted
    # the request and did nothing (river 0.4) would print the warning instead.
    local mx; mx=$(guest "wwmctl -r :ACTIVE: -b add,maximized_vert,maximized_horz 2>&1" || true)
    wantnot "add,maximized_* is applied and not merely accepted (no read-only warning)" \
            "accepted .* and did not apply it" "$mx"
    sleep 1.5
    want "wxprop -id says the window is maximized" "_NET_WM_STATE_MAXIMIZED_(HORZ|VERT)" \
         "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    guest "wwmctl -r :ACTIVE: -b remove,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    wantnot "remove,maximized_* clears it again" "_NET_WM_STATE_MAXIMIZED" \
            "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    labwc_xwayland
}

# The X plane, and the measurement this whole flavor family exists for.  The xterm the recon
# ran had X id 0x40000c and WM_CLASS "xterm","XTerm" to the real xprop, while `wwmctl -lGpx`
# listed it as 0x000f4240 with a synthesized "xterm","xterm" -- because WlrBackend had no
# views() and nothing joined the toplevel to the X plane [recon2/labwc 3].
labwc_xwayland() {
    if ! guest 'command -v xterm' >/dev/null 2>&1; then
        note "(no xterm in this image: the XWayland id join needs a real X client)"
        return 0
    fi
    guest "setsid nohup xterm -T smokex -e sh -c 'sleep 600' >/dev/null 2>&1 </dev/null & sleep 3; true" \
        >/dev/null || true
    local list xid row bare
    list=$(guest 'wxprop -root _NET_CLIENT_LIST' || true)
    want "wxprop -root _NET_CLIENT_LIST names the X client (labwc's Xwayland root is the real one)" \
         "window id # 0x[0-9a-f]+" "$list"
    xid=$(xplane_id xterm || true)
    row=$(guest 'wwmctl -lGpx' | grep -i xterm || true)
    if [ -n "$xid" ]; then
        # xprop writes the id short (0x40000c) and wmctrl pads it to eight (0x0040000c), so
        # the two are compared as NUMBERS: a check that matched the padding would go red the
        # first time an X id needed seven digits.
        bare=$(printf '%s' "${xid#0x}" | sed 's/^0*//')
        want "wwmctl lists that window under its REAL X id $xid, not a minted 0x000f42xx" \
             "0x0*$bare\b" "$row"
    else
        # Not a second FAIL: an empty $xid is the condition the want above already failed on.
        note "(the real xprop found no X client to join to: see the line above)"
    fi
    want "and under the X server's own WM_CLASS, instance first, capital XTerm" \
         "xterm\.XTerm" "$row"
    wantnot "not the synthesized app_id twice over, which is what the floor printed" \
            "xterm\.xterm" "$row"
    # The RECTANGLE, AGENTS.md route 5.  wdotool's own id for this xterm against the X server's
    # rectangle for the same window, read by the ORIGINAL xdotool over its own connection -- a
    # different tool, a different socket, so agreement here is agreement and not a tautology of
    # the code under test.  Measured on the resolute-labwc golden 2026-09-12 before the fold:
    # ours `0,0 1920x1080`, theirs `718,395 484x316`, one session, one second apart.
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
# labwc advertises zwp_virtual_keyboard_manager_v1 v1 and zwlr_virtual_pointer_manager_v1 v2,
# so nothing injected here needs root, a group or a udev rule -- measured on a box with NO
# /dev/uinput at all, where `wdotool type 'hello from labwc'` landed byte-exact in a foot
# running `cat` [recon2/labwc 3].  The package installs the uinput rule anyway, which is why
# the default route below is uinput and the `--vkbd on` route is checked beside it.
layout_phase() {
    editor_clear
    guest "wdotool --vkbd on type --delay 30 -- 'vkbd: yz@'" >/dev/null 2>&1 || true
    sleep 0.5
    editor_save
    sleep 1
    same "--vkbd on types byte-exact with no uinput and no privilege" "vkbd: yz@" "$(editor_text)"
    # The layout.  labwc publishes no layout protocol, has no IPC and owns no bus name, so all
    # five of desktop_group()'s readers decline and the keymap alone has to answer.  The golden
    # carries one layout, so that is the right answer and the group is KNOWN rather than
    # assumed -- which is the check: a `(assumed)` here would mean the keymap read came back
    # with more than one group and nothing could say which was live.
    local ex; ex=$(guest 'wdotool keys explain yz@ 2>&1' || true)
    note "keys explain: $(printf '%s\n' "$ex" | tr '\n' '|')"
    want "keys explain reads the keymap off the wire, with no desktop reader to ask" \
         "group [0-9]+ of [0-9]+.*, from wayland" "$ex"
    wantnot "and no group is assumed on a single-layout session" "\(assumed\)" "$ex"
    # NOT YET, with the route: a live layout SWITCH on labwc.  labwc reads XKB_DEFAULT_LAYOUT
    # out of its environment file at start and has no protocol, no IPC and no bus to change it
    # through afterwards, so a second group here would need AGENTS.md route 1 -- a layout
    # protocol labwc does not have yet -- and route 6 below that.
    note "(no live layout switch on labwc: route 1 would be a layout protocol it does not have)"
}

# ---------------------------------------------------------------- display
# wxrandr's whole apply set was measured working on labwc, atomically and through the generic
# wlr backend: pos + primary + relative chains, rotate, fractional scale, --same-as, off/on
# and custom modelines, each read back through wlr-randr [recon2/labwc 3].  common's body
# covers off/auto/right-of/below; what is added here is the modeline dance, which no other
# flavor in the rig exercises at all, and the mirror layout.
phase_display() {
    want "wxrandr --print-backend is wlr, the generic backend (this flavor's whole point)" \
         "^wlr$" "$(guest 'wxrandr --print-backend' | tr -d ' \r' || true)"
    want "--print-backend --verbose names the protocol it really speaks" \
         "protocol: zwlr_output_manager_v1 version [0-9]" \
         "$(guest 'wxrandr --print-backend --verbose' || true)"
    local outs; outs=$(oracle_outputs || true)
    note "native oracle (wlr-randr): $(ev "$outs")"
    note "(wlr-randr speaks the very protocol wxrandr writes through, so it is a WEAK oracle \
here; the independent reading on this flavor is the host-side screendump per head)"
    same "wxrandr --listmonitors counts the outputs wlr-randr has enabled" \
         "$(printf '%s\n' "$outs" | grep -c .)" \
         "$(guest 'wxrandr --listmonitors' | sed -n 's/^Monitors: //p' | tr -d ' \r')"
    common_display_phase
    labwc_modeline
    # Recorded, not asserted.  --brightness/--gamma answered `xrandr: Gamma size is 0.` rc 1 on
    # BOTH headless labwc and headless sway in the recon -- a headless output has no LUT -- and
    # this rig's heads are real DRM connectors, so this run is the first that can say whether
    # zwlr_gamma_control_manager_v1 v1 does anything here [recon2/labwc 3].
    note "wxrandr --brightness 0.8 on a real head: $(guest 'wxrandr --brightness 0.8 2>&1' \
| tr '\n' '|' || true)"
}

# The custom-modeline path: newmode/addmode/mode, each rc 0 on labwc, and the mode listed with
# a `*` afterwards [recon2/labwc 3].  The numbers below are a 800x600@60 CVT modeline; the
# name carries the flavor so a leftover from another run cannot be mistaken for this one's.
labwc_modeline() {
    local pair first
    pair=$(display_pair); first=${pair%% *}
    if [ -z "$first" ]; then fail "no enabled output for the modeline check"; return 1; fi
    local st=0
    guestq "wxrandr --newmode w11smoke600 38.22 800 832 912 1024 600 603 607 624 -hsync +vsync" || st=$?
    ok "wxrandr --newmode w11smoke600" "$st"
    st=0; guestq "wxrandr --addmode $first w11smoke600" || st=$?
    ok "wxrandr --addmode $first w11smoke600" "$st"
    guest "wxrandr --output $first --mode w11smoke600" >/dev/null 2>&1 || true
    sleep 2
    want "the custom mode is current on $first (no other flavor covers this path)" \
         "800x600" "$(guest "wxrandr --query" | grep -A2 "^$first " || true)"
    guest "wxrandr --output $first --auto" >/dev/null 2>&1 || true
    sleep 2
    local q; q=$(guest "wxrandr --query" | grep "^$first " || true)
    want "--auto leaves $first enabled with a geometry" \
         "^$first connected .*[0-9]+x[0-9]+\+[0-9]+\+[0-9]+" "$q"
    wantnot "and it is no longer on the custom 800x600 mode" "connected( primary)? 800x600" "$q"
    same "wlr-randr still counts $first among the enabled outputs" "1" \
         "$(oracle_outputs | awk -v n="$first" '$1 == n' | grep -c . || true)"
}

# ---------------------------------------------------------------- mirror
# labwc advertises BOTH capture protocols, and the recon started and stopped a region mirror
# on it verbatim [recon2/labwc 3].  A region is mirrored on purpose: two heads of the same
# size at the same position already mirror through the layout, and wmirror sends you there
# instead of starting a process.
phase_mirror() {
    local chk; chk=$(guest 'wmirror --check' || true)
    want "wmirror --check finds the helper" "wl-mirror" "$chk"
    want "and both capture protocols labwc advertises" \
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
    ok "wmirror $first --to $second --region $region" "$st"
    want "the started line names target, source, region, scaling and the helper's pid" \
         "$second <- $first +region 800x600\+$rx\+$ry +scaling fit +wl-mirror pid [0-9]+" "$out"
    want "wmirror --list shows it running" "$second <- $first" "$(guest 'wmirror --list' || true)"
    want "wmirror --stop ends it" "^stopped +$second <- $first" \
         "$(guest "wmirror --stop $second" || true)"
    same "and no wl-mirror is left behind" "" "$(guest 'pgrep -x wl-mirror' | tr -d ' \r\n' || true)"
}
