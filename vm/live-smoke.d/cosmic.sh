# live-smoke.d/cosmic.sh -- COSMIC (cosmic-comp, Smithay), on its own protocols.
#
# Two flavors run this file: fedora44-cosmic (Fedora 44, cosmic-comp 1.6.0-3.fc44) and
# arch-cosmic (Arch 20260901, 1:1.7.0-1).  The driver appends `selinux` and `pkgverify` on
# Fedora and `pkgverify` on Arch, so no branch on the distro is needed anywhere below.
#
# COSMIC is the one desktop in this tree where a window backend had to be written from
# nothing.  cosmic-comp publishes NO zwlr_foreign_toplevel_manager_v1 at all, so before
# wdotool/backend_cosmic.py every window command -- all of wwmctl, wxprop's native plane,
# every `wdotool window*` -- answered, on a live session:
#
#   $ wwmctl -l
#   wwmctl: no Wayland session found: no sway/i3 IPC socket, no KWin or GNOME Shell on the
#   session D-Bus, and the compositor does not offer wlr-foreign-toplevel. ...      [rc 1]
#
# while `wxrandr --query`, `warandr`, `wmirror --check` and `wdotool type` all already worked
# [recon2/cosmic 3, recon2/arch 3.5].  What it publishes instead, measured off the wire on
# 1.7.0: ext_foreign_toplevel_list_v1 v1, zcosmic_toplevel_info_v1 v3,
# zcosmic_toplevel_manager_v1 v4, ext_workspace_manager_v1 v1, zcosmic_workspace_manager_v2 v2,
# zwlr_output_manager_v1 v4 + zcosmic_output_manager_v1 v3, zcosmic_keyboard_layout_manager_v1
# v1, ext_image_copy_capture_manager_v1 v1 + ext_output_image_capture_source_manager_v1 v1.
#
# Three ABSENCES on that list are checks here rather than footnotes, and each one is the
# reason a check below has the shape it has:
#
#   * no zwlr_virtual_pointer_manager_v1.  Typing needs no privilege (zwp_virtual_keyboard_v1
#     is there and `wdotool type` landed byte-exact with no /dev/uinput node on the box);
#     the POINTER needs /dev/uinput, and without it said so and stopped -- with, as root, it
#     moved.  The package installs the udev rule, so the smoke's own pointer commands go
#     through uinput as the seated user, and what is asserted about the protocol half is the
#     refusal wdotool/vptr.py prints [recon2/arch 3.5].
#   * no zwlr_screencopy_manager_v1.  `wmirror --check` passed on
#     ext_image_copy_capture_manager_v1 ALONE, which is why the check below names that one and
#     not the pair labwc has.
#   * no zwlr_gamma_control_manager_v1.  --brightness and --gamma have no route here at all;
#     that is recorded as a note, not asserted, because a refusal's wording is a unit test's
#     job and this file has no second opinion about a LUT.
#
# The COSMIC backend is a MIDDLE capability tier and the checks track it exactly: real
# geometry (when the event arrives -- see phase_windows), real workspaces, activate/close/
# maximize/minimize/fullscreen gated on the capability array, ids minted from the 32-character
# `identifier` rather than arrival order, and NO move, NO resize and NO pid, because
# zcosmic_toplevel_manager_v1 has set_rectangle (a minimise-animation hint) and nothing else
# [recon2/cosmic 4, recon2/arch 3.5].
#
# The question that used to hang over this file -- whether cosmic-comp's KMS backend paints on
# virtio-vga with no 3D (pop-os/cosmic-comp#136) -- is ANSWERED, on Fedora's cosmic-comp
# 1.6.0-3.fc44 on 2026-09-09: `/sys/kernel/debug/dri/0/state` said `allocated by = cosmic-comp`,
# cosmic-panel was up, and a QMP screendump of head 0 measured standard deviation 0.131454
# against the rig's 0.02 flat-colour line.  So the phases below really do get a desktop, and the
# nested COSMIC_BACKEND=winit fallback the plan wrote down is not needed.  Two bytes from that
# same boot are what the display phase leans on: logind says `Type=tty` for the seated session
# (greetd registers a tty and libseat did not flip it, which is why vmctl's DESKTOPS row accepts
# both), and `cosmic-randr list --kdl` on a real head reads `transform "normal"` -- NOT the
# `flipped180` of the winit document in tests/fixtures/vm -- with only the current mode carrying
# any flag at all.  See vm/flavors/fedora44-cosmic.yaml for the whole reading.
SMOKE_PHASES="busrec install windows wm proxy input display mirror root nodialog"
EDITOR_CLASS=foot

# The editor is a terminal running `cat`, sway.sh's hook including the `cat >>`: see the
# paragraph there for why O_APPEND is the difference between a comparison and a file full of
# NULs that compares equal by accident.
editor_start() {
    guest "rm -f $SMOKE_FILE; setsid nohup foot -- sh -c 'cat >> $SMOKE_FILE' >/dev/null 2>&1 </dev/null &
           sleep 3; true" >/dev/null || true
}
editor_save()  { guest "wdotool key Return" >/dev/null || true; }
editor_clear() { guest "wdotool key ctrl+u; : > $SMOKE_FILE" >/dev/null 2>&1 || true; }

# ---------------------------------------------------------------- cosmic-randr as the oracle
# cosmic-randr is to this file what swaymsg is to sway.sh: an independent reader, shipped with
# every COSMIC install, with a machine-readable mode.  It tracked a
# `wxrandr --rotate left --scale 1.5x1.5` apply exactly in the recon (`scale 1.50`,
# `transform "flipped270"`) [recon2/cosmic 6].  The KDL is parsed on the HOST, which is why
# every one of these is `guest ... | python3` and not `guest 'python3'`.

# "<w>x<h>" for output $1, from cosmic-randr's own current mode -- where oracle.py reads the
# position, this reads the mode, so the two never assert the same field.
cosmic_mode() {
    guest 'cosmic-randr list --kdl' | python3 -c '
import re, sys
doc, want, cur = sys.stdin.read(), sys.argv[1], None
for block in re.split(r"(?m)^output ", doc):
    if not block.startswith("\"%s\"" % want):
        continue
    m = re.search(r"mode\s+(\d+)\s+(\d+)\s+\d+[^\n]*current=#true", block)
    if m:
        print("%sx%s" % (m.group(1), m.group(2)))
    break' "$1" 2>/dev/null || true
}

# cosmic-randr's own transform word for output $1 -- "normal", "flipped270" and so on.
cosmic_transform() {
    guest 'cosmic-randr list --kdl' | python3 -c '
import re, sys
doc, want = sys.stdin.read(), sys.argv[1]
for block in re.split(r"(?m)^output ", doc):
    if block.startswith("\"%s\"" % want):
        m = re.search(r"transform\s+\"([a-z0-9]+)\"", block)
        print(m.group(1) if m else "")
        break' "$1" 2>/dev/null || true
}

# ---------------------------------------------------------------- windows
phase_windows() {
    guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
    editor_start
    local out
    out=$(await 30 '[0-9]' "wdotool search --class $EDITOR_CLASS | head -1" || true)
    WIN=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$WIN" ]; then
        fail "wdotool search --class $EDITOR_CLASS found no window (the whole backend is this line) [$(ev "$out")]"
        return 1
    fi
    pass "wdotool search --class $EDITOR_CLASS -> $WIN"
    # Ids are 30 bits of blake2b over the 32-character `identifier`, under 0x40000000 and out
    # of Xwayland's range -- NOT 1000000 + arrival order, which is the wlr floor's and which
    # renames the survivor when another window closes.  A floor-shaped id here would mean
    # detection took the wrong branch, and that is worth catching before anything else does.
    if [ "$WIN" -ge 1000000 ] && [ "$WIN" -le 1000099 ]; then
        fail "the id $WIN is in the wlr floor's 1000000+arrival range: this is not the cosmic backend"
    elif [ "$WIN" -ge 1073741824 ]; then
        # 0x40000000.  backend.mint_id keeps 30 bits so that a minted id can never collide with
        # an Xwayland window's, which views() puts in the same listing.
        fail "the id $WIN is at or above 0x40000000, where an XWayland id could collide with it"
    else
        pass "the id $WIN is minted from the toplevel identifier: outside the floor's arrival \
range and below 0x40000000"
    fi
    want "getwindowname is not empty" "." "$(guest "wdotool getwindowname $WIN" || true)"
    # Geometry, sometimes.  zcosmic_toplevel_handle_v1.geometry arrives only alongside
    # output_enter or on a change, and in the nested rig it never arrived at all -- not in 4 s
    # and not after a maximize [recon2/cosmic 4].  So this is a `want` on a real rectangle and
    # a note beside it: a run where the rectangle is the whole output is the backend falling
    # back to the floor, and this line is where that shows.
    # The fallback is a STRING we can build -- 0,0 plus some head's mode, and cosmic-randr
    # prints every head's mode -- so the reading is compared against it rather than against a
    # rectangle shape, which the fallback satisfies too.  No mode from cosmic-randr means the
    # comparison could not be made, and that is an XFAIL and never an XPASS.
    local g floors verdict o
    g=$(win_geom "$WIN")
    floors=$(for o in $(oracle_outputs | awk '{print $1}'); do
                 printf '0,0 %s\n' "$(cosmic_mode "$o")"
             done | grep -E '^0,0 [0-9]+x[0-9]+$' || true)
    verdict="NO-HEAD-MODE"
    if [ -n "$floors" ] && printf '%s\n' "$g" | grep -Eq '^[0-9]+,[0-9]+ [0-9]+x[0-9]+$'; then
        if printf '%s\n' "$floors" | grep -Fxq "$g"; then verdict="FLOOR-FALLBACK"
        else verdict="OWN-RECTANGLE"; fi
    fi
    note "getwindowgeometry: $g (the floor's fallbacks on this session: $(ev "$floors"))"
    xwant "getwindowgeometry is the window's own rectangle and not 0,0 + a head's mode (until \
a run on a KMS cosmic-comp says whether the geometry event arrives there at all)" \
          "^OWN-RECTANGLE" "$verdict $g"
    # The four with no request behind them.  set_rectangle is a minimise-animation hint, so
    # NOT YET on move and resize: the lowest route on the AGENTS.md ladder that would give
    # them is 6, a patched cosmic-comp, and nobody has costed carrying that package.
    local refusal; refusal=$(guest "wdotool windowmove $WIN 100 100 2>&1" || true)
    want "windowmove is refused by the cosmic backend (rc 1, and the backend named in the \
prefix; the reason after it is batch 8's wording and is deliberately not pinned here)" \
         "windowmove is not supported by the cosmic backend: " "$refusal"
    want "windowsize says the same" "windowsize is not supported by the cosmic backend: " \
         "$(guest "wdotool windowsize $WIN 800 600 2>&1" || true)"
    wantnot "and neither of them blames the wlr floor: this is a different backend" \
            "wlr backend" "$refusal"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    same "windowactivate --sync then getactivewindow is that window" "$WIN" \
         "$(guest 'wdotool getactivewindow' | tr -d ' \n' || true)"
    # Workspaces, over ext_workspace_manager_v1: the recon's session had two, named `1` and `2`
    # with coordinates [1] and [2] [recon2/cosmic 4].  The count is read and not assumed.
    local nd; nd=$(guest 'wwmctl -d' | grep -c '^[0-9]' || true)
    if [ "${nd:-0}" -lt 2 ]; then
        note "$nd workspace(s): the set_desktop pair needs two, skipped"
    else
        guest "wdotool set_desktop 1" >/dev/null || true
        sleep 1
        same "set_desktop 1 -> get_desktop (of $nd COSMIC workspaces)" "1" \
             "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
        guest "wdotool set_desktop 0" >/dev/null || true
        sleep 1
        same "set_desktop 0 -> get_desktop" "0" "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
    fi
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    # The pointer goes through /dev/uinput here and nowhere else in this file: cosmic-comp has
    # no virtual-pointer protocol, and the package's udev rule is what makes the node writable
    # for the seated user.  phase_input checks the other half, the refusal.
    guest "wdotool mousemove 300 300" >/dev/null || true
    want "mousemove 300 300 -> getmouselocation (through /dev/uinput, the only route here)" \
         "x:300 y:300" "$(guest 'wdotool getmouselocation' || true)"
    local st=0
    guestq "wdotool mousemove 300 300 click 1" || st=$?
    ok "mousemove 300 300 click 1" "$st"
}

# ---------------------------------------------------------------- the window manager
phase_wm() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    want "wwmctl -l lists the terminal (it printed the no-session line before this backend)" \
         "." "$(guest 'wwmctl -l' || true)"
    # `Smithay X WM` is cosmic-comp's own X window manager name, read off the live session's
    # _NET_SUPPORTING_WM_CHECK; CosmicBackend.wm_name answers the same string with no X plane
    # at all, so this check has one answer either way [recon2/cosmic 3].
    want "wwmctl -m names cosmic-comp's own X window manager" "^Name: Smithay X WM$" \
         "$(guest 'wwmctl -m' || true)"
    # pid 0, and it is not a bug to fix here: neither ext_foreign_toplevel_handle_v1 nor
    # zcosmic_toplevel_handle_v1 carries a pid, so `wwmctl -p` prints 0 for a NATIVE row and
    # `windowkill` refuses.  An X row gets its pid from the X server through views().
    want "wwmctl -lp prints 0 for the native row: no protocol here carries a pid" \
         "^0x[0-9a-f]+ +[0-9-]+ +0 " "$(guest 'wwmctl -lp' | grep -i foot || true)"
    # The maximize pair, gated on the capability array.  The manager announced capabilities
    # [1,2,3,4,6] in the recon -- close, activate, maximize, minimize, move_to_workspace -- and
    # decision C5.15 is to trust the array rather than send anyway, so a compositor that stops
    # advertising 3 turns the first line below into a named refusal and not a silent no-op.
    guest "wwmctl -r :ACTIVE: -b add,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    want "wxprop -id says the window is maximized" "_NET_WM_STATE_MAXIMIZED_(HORZ|VERT)" \
         "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    guest "wwmctl -r :ACTIVE: -b remove,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    wantnot "remove,maximized_* clears it again" "_NET_WM_STATE_MAXIMIZED" \
            "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    # Fullscreen is capability 5, which the live manager did NOT advertise although the request
    # worked when it was tried -- that disagreement is upstream's and the recon says to ask
    # them before pinning behaviour, so this is a note and not a check [recon2/cosmic 8].
    note "add,fullscreen: $(guest 'wwmctl -r :ACTIVE: -b add,fullscreen 2>&1' | tr '\n' '|' || true)"
    guest "wwmctl -r :ACTIVE: -b remove,fullscreen" >/dev/null 2>&1 || true
    cosmic_xwayland
}

# The X plane.  cosmic-comp runs an Xwayland, and CosmicBackend joins the toplevels to
# _NET_CLIENT_LIST through the same XPlaneViews the wlr backend uses -- which is where an X
# row's real id, its real WM_CLASS and its pid come from.  Whether XWayland windows are even
# distinguishable in COSMIC's toplevel list was one of the recon's open questions
# [recon2/cosmic 8], so the join is asserted and the pid is recorded.
# The X id for WM_CLASS $1, through the REAL xprop -- labwc.sh's helper, copied rather than
# shared because common.sh belongs to another batch.  The first id in _NET_CLIENT_LIST is not
# ours: cosmic-comp starts its own Xwayland with the session, so any other X client on it would
# make the join below assert against the wrong window.
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

cosmic_xwayland() {
    if ! guest 'command -v xterm' >/dev/null 2>&1; then
        note "(no xterm in this image: the XWayland id join needs a real X client)"
        return 0
    fi
    guest "setsid nohup xterm -T smokex -e sh -c 'sleep 600' >/dev/null 2>&1 </dev/null & sleep 3; true" \
        >/dev/null || true
    local list xid row bare
    list=$(guest 'wxprop -root _NET_CLIENT_LIST' || true)
    want "wxprop -root _NET_CLIENT_LIST names the X client" "window id # 0x[0-9a-f]+" "$list"
    xid=$(xplane_id xterm || true)
    row=$(guest 'wwmctl -lGpx' | grep -i xterm || true)
    if [ -n "$xid" ]; then
        bare=$(printf '%s' "${xid#0x}" | sed 's/^0*//')
        want "wwmctl lists that window under its real X id $xid" "0x0*$bare\b" "$row"
    else
        note "(the real xprop found no X client to join to: see the line above)"
    fi
    want "and under the X server's own WM_CLASS, instance first" "xterm\.XTerm" "$row"
    note "ours:   $row"
    note "theirs: $(guest 'wmctrl -lGpx' | grep -i xterm || true)"
    guest "pkill -x xterm; true" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------- input
# The keyboard half needs no privilege and the pointer half does, and that split is COSMIC's
# alone in this tree: zwp_virtual_keyboard_manager_v1 is advertised and
# zwlr_virtual_pointer_manager_v1 is not [recon2/arch 3.5, recon2/cosmic 3].
# common.sh's phase_input already types, checks Return and then calls layout_phase, so the two
# halves COSMIC alone has go at the top of the layout hook, the way labwc.sh does it -- a copy
# of that phase here would drift from it the first time common.sh changes.

# The layout.  COSMIC is the only desktop in this tree whose live group arrives on the WIRE:
# zcosmic_keyboard_layout_v1's `group` event carries the active index and the XML says it is sent
# even when the client has no focused window, which is exactly what wl_keyboard.modifiers will
# not do -- so no bus, no portal and no reader below [recon2/cosmic 4, wdotool/xkbmap.py
# _cosmic_group].  It is read inside _fetch_wayland and not by desktop_group(), so the source
# line says `wayland` here where a Hyprland session says `wayland + hyprland devices`.
#
# What this run CANNOT do is switch to a second layout: the golden carries one, and configuring
# a second COSMIC layout has no measured route at all -- the recon recorded no cosmic-comp
# config path, no key and no value, and vm/build-image.sh's desktop_cosmic deliberately writes
# nothing into ~/.config/cosmic rather than guess one.  So the group event is exercised by
# nothing here and that is a gap, not a claim: it is NOT YET, and the lowest route is 2,
# cosmic-comp's own config file, once somebody reads its schema and writes it down.
layout_phase() {
    editor_clear
    guest "wdotool --vkbd on type --delay 30 -- 'vkbd: yz@'" >/dev/null 2>&1 || true
    sleep 0.5
    editor_save
    sleep 1
    same "--vkbd on types byte-exact with no uinput and no privilege" "vkbd: yz@" "$(editor_text)"
    # The other half of the same sentence: the protocol pointer is not there, and the refusal
    # has to say which compositors do and do not have it rather than leaving the user guessing.
    want "--vkbd on click names the missing pointer protocol and the compositors that have it" \
         "does not implement zwlr_virtual_pointer_manager_v1" \
         "$(guest 'wdotool --vkbd on click 1 2>&1' || true)"
    want "...and says COSMIC's keyboard half works, which is the useful half of the answer" \
         "COSMIC's keyboard half works" "$(guest 'wdotool --vkbd on click 1 2>&1' || true)"
    local ex; ex=$(guest 'wdotool keys explain yz@ 2>&1' || true)
    note "keys explain: $(printf '%s\n' "$ex" | tr '\n' '|')"
    want "keys explain reads the keymap off the wire and says where its group came from" \
         "group [0-9]+ of [0-9]+.*, from wayland" "$ex"
    wantnot "and no group is assumed on this session" "\(assumed\)" "$ex"
    note "(one layout on this golden, so the zcosmic_keyboard_layout_v1 group event has nothing"
    note "to choose between: a second COSMIC layout has no measured config route yet, route 2)"
}

# ---------------------------------------------------------------- display
# The display half worked on COSMIC before any of the window work: `wxrandr --query` read
# Virtual-1 1920x1080+0+0 and cosmic-randr tracked a rotate + 1.5x scale apply exactly
# [recon2/arch 3.5, recon2/cosmic 6].  The token stays `wlr` -- COSMIC's output extension
# changes no code path -- and what changed is the NAME the verbose line prints.
phase_display() {
    want "wxrandr --print-backend is wlr: COSMIC's output extension changes no code path" \
         "^wlr$" "$(guest 'wxrandr --print-backend' | tr -d ' \r' || true)"
    want "--print-backend --verbose names COSMIC and not 'wlroots'" \
         "compositor: COSMIC" "$(guest 'wxrandr --print-backend --verbose' || true)"
    local outs; outs=$(oracle_outputs || true)
    note "native oracle (cosmic-randr list --kdl): $(ev "$outs")"
    same "wxrandr --listmonitors counts the outputs cosmic-randr has enabled" \
         "$(printf '%s\n' "$outs" | grep -c .)" \
         "$(guest 'wxrandr --listmonitors' | sed -n 's/^Monitors: //p' | tr -d ' \r')"
    local pair first
    pair=$(display_pair); first=${pair%% *}
    if [ -z "$first" ]; then fail "no enabled output in the oracle [$(ev "$outs")]"; return 1; fi
    common_display_phase
    # The rotate the recon watched cosmic-randr track, read back through the KDL's own
    # `transform` word -- a field oracle.py never touches, so this is a second reading and not
    # the same one twice.  The word is compared against what it WAS and never against a
    # literal: a resting winit output calls itself `flipped180` [recon2/cosmic 6] and a resting
    # virtio KMS head calls itself `normal` (measured here 2026-09-09), so the only sentence
    # true of both is that a --rotate changes it and --rotate normal puts it back.
    local t0 t1
    t0=$(cosmic_transform "$first")
    note "cosmic-randr's resting transform for $first: $(ev "$t0")"
    guest "wxrandr --output $first --rotate left" >/dev/null 2>&1 || true
    sleep 2
    t1=$(cosmic_transform "$first")
    if [ -n "$t0" ] && [ -n "$t1" ] && [ "$t0" != "$t1" ]; then
        pass "cosmic-randr's own transform word followed a --rotate left ($t0 -> $t1)"
    else
        fail "--rotate left did not change cosmic-randr's transform for $first [$(ev "$t0") -> $(ev "$t1")]"
    fi
    guest "wxrandr --output $first --rotate normal" >/dev/null 2>&1 || true
    sleep 2
    same "and --rotate normal puts the transform back exactly where it was" "$t0" \
         "$(cosmic_transform "$first")"
    note "mode of $first per cosmic-randr: $(cosmic_mode "$first")"
    # No gamma protocol at all on cosmic-comp, so this is recorded rather than asserted: the
    # refusal's wording belongs to a unit test, and there is no second opinion about a LUT here.
    note "wxrandr --brightness 0.8: $(guest 'wxrandr --brightness 0.8 2>&1' | tr '\n' '|' || true)"
}

# ---------------------------------------------------------------- mirror
# The row footnote (k) put ext_image_copy_capture in a footnote; on COSMIC it is the whole
# story, because there is no zwlr_screencopy_manager_v1 in cosmic-comp's 53 globals at all and
# `wmirror --check` passed on the other one alone [recon2/cosmic 3, recon2/arch 3.5].
phase_mirror() {
    local chk; chk=$(guest 'wmirror --check' || true)
    want "wmirror --check finds the helper" "wl-mirror" "$chk"
    want "and qualifies on ext_image_copy_capture alone: cosmic-comp has no screencopy manager" \
         "capture: .*ext_image_copy_capture" "$chk"
    wantnot "...and really has none, so the check may not be passing on the wrong protocol" \
            "zwlr_screencopy_manager_v1" "$chk"
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
