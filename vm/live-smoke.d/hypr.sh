# live-smoke.d/hypr.sh -- Hyprland (aquamarine), over the compositor's own IPC socket.
#
# Two flavors run this file: resolute-hypr (Ubuntu 26.04, Hyprland 0.53.3) and arch-hypr
# (Arch 20260901, 0.56.2).  Everything but the display applies is the same on both; the applies
# are why there are two flavors at all, and the split below is keyed on $DISTRO.
#
# Every claim here is either a byte the recon measured in a QEMU VM with virtio heads
# [recon2/hyprland, recon2/arch 3.4] or a string the unit tests pin (tests/test_backend_hypr.py,
# tests/test_wxrandr_hypr.py).  What NOBODY has run is this file: neither golden exists yet, so
# tests/fixtures/live/ carries no hypr recording and NOT-YET-RUN says so.  The first run is also
# asked to RECORD three things nothing else can answer.  They are the `note` lines below:
#
#   1. a second `hyprctl -j devices` from a real session.  `HyprLayouts` picks the keyboard row
#      by three clauses and the third -- a name matching keyboard|keybd|kbd -- is a heuristic over
#      exactly one recorded device list, in which `main: true` was wdotool's OWN injected device
#      and the first non-virtual row was `power-button` (tests/fixtures/hypr/devices.json).
#   2. what `wdotool type` through /dev/uinput produces AFTER the physical keyboard's group has
#      been switched.  Hyprland keeps XKB state per DEVICE [recon2/hyprland 3], so the reader
#      answers for the physical keyboard while the keystrokes land on a device of wdotool's own,
#      whose group nobody has read while the two disagreed.
#   3. whether `dispatch alterzorder top,address:...` EXISTS -- its reply, and no more than that.
#      `hyprctl -j clients` publishes no stacking order at all (backend_hypr.NO_STACKING is the
#      refusal that says so), so nothing in this file can say whether anything MOVED; the only
#      ordering hint the JSON carries is `focusHistoryID`, which answers about focus, not about
#      z.  windowraise focuses instead, and backend_hypr.py's raise_() asks for the reply.
#
# How the recording gets made, when a golden of either flavor exists.  The transcript
# tests/fixtures/live/ wants is the guest commands of the `windows` and `wm` phases IN THE ORDER
# THIS FILE ASKS THEM (fake-vmctl answers a repeated command with each recording in turn), and
# the order here is not the common one: the move is refused before the window is floated, the
# minimize is a refusal rather than a state change, and the phase ends by closing the
# first-arrived window.  vm/live-smoke.d/guest-capture-winwm.sh is the GNOME shape of that job
# and cannot be pointed at this one; a hypr-shaped sibling of it is what the first run needs, and
# until somebody writes one the honest state is the `hypr` line in tests/fixtures/live/NOT-YET-RUN.
#
# `mirror` is in the phase list because Hyprland is, with sway, one of the two compositors
# measured where wmirror works at all [recon2/hyprland 3].  `nodialog` is in it with a caveat
# worth reading in the log rather than in a check: an `xdg-desktop-portal-hyprland` Recommends
# puts org.freedesktop.impl.portal.desktop.hyprland on the session bus of a default install, so
# the portal is PRESENT here where it is absent on sway -- and phase_nodialog counts method
# CALLS by interface, which is the reason a name sitting on the bus does not turn it red.
SMOKE_PHASES="busrec install windows wm proxy input display mirror root nodialog"
EDITOR_CLASS=foot

# No text editor on this golden either: the "editor" is a terminal running `cat`, sway.sh's
# hook verbatim, including the `cat >>` -- see the paragraph there for why O_APPEND is the
# difference between a comparison and a file full of NULs that compares equal by accident.
editor_start() {
    guest "rm -f $SMOKE_FILE; setsid nohup foot -- sh -c 'cat >> $SMOKE_FILE' >/dev/null 2>&1 </dev/null &
           sleep 3; true" >/dev/null || true
}
editor_save()  { guest "wdotool key Return" >/dev/null || true; }
editor_clear() { guest "wdotool key ctrl+u; : > $SMOKE_FILE" >/dev/null 2>&1 || true; }

# ---------------------------------------------------------------- the compositor as the oracle
# hyprctl is to this file what swaymsg is to sway.sh: the second opinion every claim about a
# window or a head is checked against.  The JSON is parsed on the HOST (the guest is asked only
# for bytes), which is also why every one of them is `guest ... | python3`, not `guest 'python3'`.

# "<x>,<y> <w>x<h>" for the one client of class $1, as Hyprland itself reports it.
hypr_geom() {
    guest 'hyprctl -j clients' | python3 -c '
import json, sys
rows = [r for r in (json.load(sys.stdin) or []) if r.get("class") == sys.argv[1]]
print("%d,%d %dx%d" % (rows[0]["at"][0], rows[0]["at"][1], rows[0]["size"][0], rows[0]["size"][1])
      if rows else "")' "$1" 2>/dev/null || true
}

# The title Hyprland holds for the one client of class $1.  `getwindowname` is compared against
# THIS and not against a "not empty" regex: guest() merges stderr into stdout, so `.` would pass
# on a `wdotool: ...` error line as happily as on a title.
hypr_title() {
    guest 'hyprctl -j clients' | python3 -c '
import json, sys
rows = [r for r in (json.load(sys.stdin) or []) if r.get("class") == sys.argv[1]]
print(rows[0].get("title") or "" if rows else "")' "$1" 2>/dev/null || true
}

# `fullscreen` for the one client of class $1: 0 none, 1 maximize (both axes), 2 fullscreen
# [wdotool/backend_hypr.py FS_*].  win_geom and `wxprop -id ... _NET_WM_STATE` are BOTH synthesized
# from this same j/clients row through our own backend, so the maximize pair below would otherwise
# only ever agree with itself; recon2/hyprland 7 item 3 asked for hyprctl's own field as the
# second opinion, and this is it.
hypr_fs() {
    guest 'hyprctl -j clients' | python3 -c '
import json, sys
rows = [r for r in (json.load(sys.stdin) or []) if r.get("class") == sys.argv[1]]
print(rows[0].get("fullscreen") if rows else "")' "$1" 2>/dev/null || true
}

# "<w>x<h>" for monitor $1, from Hyprland itself -- the mode, where oracle.py reads the position.
hypr_mode() {
    guest 'hyprctl -j monitors' | python3 -c '
import json, sys
for m in (json.load(sys.stdin) or []):
    if m.get("name") == sys.argv[1]:
        print("%dx%d" % (m["width"], m["height"]))' "$1" 2>/dev/null || true
}

# The PHYSICAL keyboard, picked without repeating the rule HyprLayouts uses: the QEMU guest's
# keyboard is `at-translated-set-2-keyboard` on both recons, and the fallback is the first row
# that is neither wdotool's injected device nor one of Hyprland's own virtual keyboards.  An
# oracle that shared the reader's three clauses would only ever agree with it.
hypr_keyboard() {
    guest 'hyprctl -j devices' | python3 -c '
import json, re, sys
ks = (json.load(sys.stdin) or {}).get("keyboards") or []
named = [k for k in ks if k.get("name") == "at-translated-set-2-keyboard"]
# The fallback drops the injected devices AND the pseudo-keyboards an ACPI button registers as:
# `power-button` is the FIRST row of the one recorded device list, and switching ITS group and
# then asking the reader to agree would assert nothing about the session.
skip = re.compile(r"wdotool-|hl-virtual-keyboard|power|sleep|video|lid", re.I)
real = [k for k in ks if not skip.search(str(k.get("name") or ""))]
row = (named or real or [{}])[0]
print(row.get("name") or "")' 2>/dev/null || true
}

# The active layout index Hyprland holds for device $1 -- the number `keys explain` has to agree
# with, read from the compositor and not from the reader under test.
hypr_kb_index() {
    guest 'hyprctl -j devices' | python3 -c '
import json, sys
for k in ((json.load(sys.stdin) or {}).get("keyboards") or []):
    if k.get("name") == sys.argv[1]:
        print(k.get("active_layout_index"), k.get("layout"), k.get("active_keymap"))' "$1" 2>/dev/null || true
}

# ---------------------------------------------------------------- windows
# Hyprland TILES, and a tiled window ignores an absolute move until it is floated -- "as on sway",
# in the recon's own words, which is the caveat and not a recorded byte: the movewindowpixel that
# DID move a window there was sent after a `setfloating` [recon2/arch 3.4].  What the dispatcher
# answers when the window is still tiled is a byte nobody has written down; the refusal check
# below is the rule backend_hypr._refuse_tiled() encodes and the thing that will measure it.  So
# the backend refuses up front the way the sway backend does, and this phase measures the refusal
# FIRST and floats the window afterwards -- the only state in which 100,100 800x600 means anything.
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
    local orc name
    orc=$(hypr_title "$EDITOR_CLASS"); name=$(guest "wdotool getwindowname $WIN" || true)
    if [ -z "$orc" ]; then
        fail "hyprctl -j clients holds no $EDITOR_CLASS client to name (the oracle is empty)"
    else
        same "getwindowname is the title hyprctl -j clients holds for that window" "$orc" "$name"
    fi
    want "windowmove on a TILED window is refused by name, not silently ignored" \
         "cannot move a tiled window" "$(guest "wdotool windowmove $WIN 100 100 2>&1" || true)"
    guest "wdotool windowactivate --sync $WIN; hyprctl dispatch setfloating" >/dev/null || true
    sleep 1
    guest "wdotool windowmove $WIN 100 100; wdotool windowsize $WIN 800 600" >/dev/null || true
    sleep 1
    # Both halves: what our tools read back, and what the compositor says it did.  The wlr floor
    # answered the whole output (0,0 1920x1080) to the first and had no second [recon2/hyprland 3].
    same "windowmove 100 100 + windowsize 800 600 -> getwindowgeometry" "100,100 800x600" "$(win_geom "$WIN")"
    same "and hyprctl -j clients agrees, at and size" "100,100 800x600" "$(hypr_geom "$EDITOR_CLASS")"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    same "windowactivate --sync then getactivewindow is that window" "$WIN" \
         "$(guest 'wdotool getactivewindow' | tr -d ' \n' || true)"
    # The floor accepted this and did nothing at all (`hyprctl` still said mapped: 1 hidden: 0).
    want "windowminimize is refused by name: Hyprland has no minimize" \
         "not supported by the hypr backend \(Hyprland has no minimize\)" \
         "$(guest "wdotool windowminimize $WIN 2>&1" || true)"
    # Workspaces are Hyprland's, dense from 1 and created on demand, so the pair needs no
    # pre_desktop_pair hook: `dispatch workspace 2` makes the second one.  On the floor
    # get_desktop/set_desktop failed outright (`not supported by the wlr backend`, rc 1).
    guest "wdotool set_desktop 1" >/dev/null || true
    sleep 1
    same "set_desktop 1 -> get_desktop (workspace 2, 0-based as wmctrl counts)" "1" \
         "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
    guest "wdotool set_desktop 0" >/dev/null || true
    sleep 1
    same "set_desktop 0 -> get_desktop" "0" "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    guest "wdotool mousemove 300 300" >/dev/null || true
    want "mousemove 300 300 -> getmouselocation" "x:300 y:300" "$(guest 'wdotool getmouselocation' || true)"
    same "and hyprctl cursorpos agrees to the pixel (measured 0 px of error, twice)" "300, 300" \
         "$(guest 'hyprctl cursorpos' | tr -d '\r\n' || true)"
    local st=0
    guestq "wdotool mousemove 300 300 click 1" || st=$?
    ok "mousemove 300 300 click 1" "$st"
}

# ---------------------------------------------------------------- the window manager
phase_wm() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    guest "wdotool windowactivate --sync $WIN; wdotool windowmove $WIN 100 100; \
           wdotool windowsize $WIN 800 600" >/dev/null || true
    sleep 1
    same "the pair starts at" "100,100 800x600" "$(win_geom "$WIN")"
    # `dispatch fullscreen 1` takes both axes at once, so wwmctl folds the pair into one call
    # and refuses a lone axis rather than half-applying it.  Three readings follow that one call
    # and only the third is a second opinion: win_geom and `wxprop -id ... _NET_WM_STATE` are both
    # synthesized from the same j/clients row through our own backend and agree by construction,
    # while hypr_fs is Hyprland's own `fullscreen` field -- the one recon2/hyprland 7 item 3 asked
    # for, and the one reading here that can disagree with the other two.
    guest "wwmctl -r :ACTIVE: -b add,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    local maxed; maxed=$(win_geom "$WIN")
    if [ "$maxed" = "100,100 800x600" ]; then
        fail "add,maximized_vert,maximized_horz did not change the geometry"
    else
        pass "add,maximized_vert,maximized_horz -> $maxed"
    fi
    want "wxprop -id says the window is maximized on both axes" \
         "_NET_WM_STATE_MAXIMIZED_(HORZ|VERT)" "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    same "and hyprctl's own fullscreen field for that client is 1, its word for maximize" "1" \
         "$(hypr_fs "$EDITOR_CLASS")"
    want "a LONE axis is refused, with the reason" "maximizes both axes at once" \
         "$(guest "wwmctl -r :ACTIVE: -b add,maximized_vert 2>&1" || true)"
    guest "wwmctl -r :ACTIVE: -b remove,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    same "b7a60f0: remove,maximized_vert,maximized_horz restores exactly" "100,100 800x600" \
         "$(win_geom "$WIN")"
    same "and hyprctl's fullscreen field is back to 0" "0" "$(hypr_fs "$EDITOR_CLASS")"
    # The other value of the same field, which the recon listed beside the pair: `fullscreen` is
    # dispatch 0 and shows as 2.  `-c` (close) is deliberately NOT exercised here -- the editor is
    # the window every phase after this one types into, and closing it is already done once, on
    # purpose, by hypr_ids_are_stable below.
    guest "wwmctl -r :ACTIVE: -b add,fullscreen" >/dev/null || true
    sleep 1.5
    same "add,fullscreen takes the same field to 2, Hyprland's word for full" "2" \
         "$(hypr_fs "$EDITOR_CLASS")"
    guest "wwmctl -r :ACTIVE: -b remove,fullscreen" >/dev/null || true
    sleep 1.5
    same "remove,fullscreen puts the window back where it was" "100,100 800x600" "$(win_geom "$WIN")"
    # The floor printed `-1` for the desktop, `0` for the pid and the whole output as the
    # rectangle, on every row [recon2/hyprland 3, recon2/arch 3.4].  All three are the backend's.
    want "wwmctl -l lists the terminal on desktop 0, not the floor's -1" "^0x[0-9a-f]+ +0 " \
         "$(guest 'wwmctl -l' || true)"
    want "wwmctl -lGpx carries the real pid and the real rectangle" \
         "^0x[0-9a-f]+ +0 +[1-9][0-9]* +100 +100 +800 +600 +foot\.foot" "$(guest 'wwmctl -lGpx' || true)"
    want "wwmctl -d lists the workspaces with the current one starred" "^0 +\*" "$(guest 'wwmctl -d' || true)"
    note "wwmctl -m: $(guest 'wwmctl -m' | tr '\n' '|' || true)"
    hypr_xwayland
    hypr_ids_are_stable
}

# The X plane, on whichever X client the image has.  `xmessage` is in Ubuntu's x11-utils and the
# recon measured that exact window on 0.53.3: WM_CLASS "xmessage","Xmessage", X id 0x00400020, at
# 300,200 400x300 -- and `wwmctl -lpx` giving it a MINTED id instead of the X one, which is what
# views()'s matcher fixed [recon2/hyprland 2, 3].  Arch ships xmessage in `xorg-xmessage`, which
# arch-sway's EXTRA_PKGS does not carry and arch-hypr copies verbatim on purpose, so there the
# client is the `xterm` that EXTRA does carry -- and it is the window recon2/arch 3.4 measured the
# same join on (`window id # 0x40000c` for an XTerm).  Only the WM_CLASS wanted differs.  xterm's
# -geometry counts CELLS, not pixels, so the size is asked in cells and the position, which is
# what the wmctrl comparison below reads, in pixels as usual.
hypr_xwayland() {
    local xcmd xcls xproc
    if guest 'command -v xmessage' >/dev/null 2>&1; then
        xcmd="xmessage -geometry 400x300+300+200 smokex"
        xcls='xmessage\.Xmessage'; xproc=xmessage
    elif guest 'command -v xterm' >/dev/null 2>&1; then
        xcmd="xterm -geometry 80x24+300+200 -T smokex -e sh -c 'sleep 600'"
        xcls='xterm\.XTerm'; xproc=xterm
    else
        note "(no xmessage and no xterm in this image: the XWayland id join needs an X client)"
        return 0
    fi
    guest "setsid nohup $xcmd >/dev/null 2>&1 </dev/null & sleep 3; true" >/dev/null || true
    local list xid row oracle
    # The ORACLE is the real xprop's root list, not ours.  Ours is deliberately a superset: the merged root
    # lists native windows too, by the minted id the tools print for them (wxprop/core.py:680), and the
    # editor this phase already opened is one of those -- so `head -1` of OUR list is whatever arrived
    # first, and on the 2026-09-09 run that was the editor's minted 0x48bea19f, not the X client's
    # 0x00400020.  The check failed on its own oracle while the backend was right: `wwmctl -lpx` and the
    # original `wmctrl -lpx` both said 0x00400020 in the same run.
    oracle=$(guest 'xprop -root _NET_CLIENT_LIST 2>/dev/null' || true)
    list=$(guest 'wxprop -root _NET_CLIENT_LIST' || true)
    want "wxprop -root _NET_CLIENT_LIST names the X client (Hyprland's Xwayland root is the real one)" \
         "window id # 0x[0-9a-f]+" "$list"
    xid=$(printf '%s\n' "$oracle" | grep -o '0x[0-9a-f]*' | tail -1)
    if [ -n "$xid" ]; then
        # and our merged list carries what the X root carries: a minted id is biased into 0x4000_0000 and
        # up (wdotool/backend.py ID_BASE) so it can never be read as an X id, but the X ids themselves have
        # to be there
        want "and our merged root list carries the X root's own id $xid" "$xid" "$list"
    else
        note "(the real xprop printed no id: no X client on the root, so there is nothing to join)"
    fi
    row=$(guest 'wwmctl -lpx' | grep -i "$xproc" || true)
    if [ -n "$xid" ]; then
        # xprop writes the id short (`0x40000c`) and wmctrl pads it to eight (`0x0040000c`), so the
        # two are compared as NUMBERS: a run that matched the padding instead would go red the first
        # time an X id needed seven digits [recon2/hyprland 3 has the padded form, recon2/arch 3.4
        # the short one, for the same kind of window].
        local bare; bare=$(printf '%s' "${xid#0x}" | sed 's/^0*//')
        want "wwmctl lists that window under its REAL X id $xid, not a minted one" "0x0*$bare\b" "$row"
    else
        # Not a second FAIL: an empty $xid is the very condition the want above has just failed on,
        # and counting one broken read twice would say the phase is twice as broken as it is.
        note "(no X id in _NET_CLIENT_LIST, so the id join has nothing to join: see the line above)"
    fi
    want "and under the X server's WM_CLASS, instance first" "$xcls" "$row"
    # Recorded, not asserted: the original wmctrl doubles an XWayland window's coordinates
    # (600,400 for a window at 300,200) through a non-reparenting xwm.  Whether we reproduce that
    # bug here is a parity question this run is the first to have the two lines for.
    note "ours:   $(guest 'wwmctl -lGpx' | grep -i "$xproc" || true)"
    note "theirs: $(guest 'wmctrl -lGpx' | grep -i "$xproc" || true)"
    guest "pkill -x $xproc; true" >/dev/null 2>&1 || true
}

# Ids are minted from the compositor's `address`, so another window closing cannot rename one.
# On the wlr floor they were arrival order: two foots listed as 0x000f4240/0x000f4241 and closing
# the FIRST renamed the survivor to 0x000f4240 [recon2/hyprland 3].  Closing the first is
# therefore the only shape of this check that can tell the two apart -- so the editor is what
# goes, and it is started again at the end for the phases that come after.
hypr_ids_are_stable() {
    guest "setsid nohup foot -- sh -c 'sleep 600' >/dev/null 2>&1 </dev/null & sleep 3; true" \
        >/dev/null || true
    local ids second pid
    ids=$(guest "wdotool search --class $EDITOR_CLASS" | grep -E '^[0-9]+$' || true)
    second=$(printf '%s\n' "$ids" | grep -vx "$WIN" | head -1)
    if [ -z "$second" ]; then
        fail "a second $EDITOR_CLASS window did not appear [$(ev "$ids")]"
    else
        pid=$(guest "wdotool getwindowpid $second" | tr -d ' \r\n' || true)
        want "getwindowpid answers a pid (the floor: 'has no pid associated with it')" "^[0-9]+$" "$pid"
        guest "kill $(guest "wdotool getwindowpid $WIN" | tr -d ' \r\n' || true) 2>/dev/null; true" \
            >/dev/null 2>&1 || true
        sleep 2
        same "the survivor keeps its id when the first-arrived window closes" "$second" \
             "$(guest "wdotool search --class $EDITOR_CLASS" | grep -E '^[0-9]+$' | head -1 || true)"
    fi
    # Asked of this run by wdotool/backend_hypr.py's raise_(): windowraise focuses a floating
    # window because no report has ever run the dispatcher that would really raise one.  What is
    # recorded is the REPLY -- whether the dispatcher exists at all -- and nothing about movement:
    # j/clients publishes no stacking order (backend_hypr.NO_STACKING), so no check in this file
    # could see a window change places.
    note "alterzorder replies: $(guest 'hyprctl dispatch alterzorder top' | tr -d '\r\n' || true)"
    # Killed by PID, never by command line.  `pkill -f 'sleep 600'` matches the `sh -c` wrapper
    # that runs this very command, and any other shell whose argv carries the string, so -f would
    # SIGTERM its own parents -- the self-match common.sh:190 documents for dbus-monitor, which
    # works only by accident (every match is signalled in one pass and the status is swallowed).
    # $pid is the second foot's, straight from `wdotool getwindowpid` above.
    if [ -n "$pid" ]; then guest "kill $pid; true" >/dev/null 2>&1 || true; fi
    sleep 1
    editor_start
    local out
    out=$(await 30 '[0-9]' "wdotool search --class $EDITOR_CLASS | head -1" || true)
    WIN=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    [ -n "$WIN" ] || { fail "the editor did not come back after the id check [$(ev "$out")]"; return 1; }
    guest "wdotool windowactivate --sync $WIN; hyprctl dispatch setfloating" >/dev/null || true
    note "the editor is back as $WIN (the phases after this one type into it)"
}

# ---------------------------------------------------------------- input
# Hyprland advertises zwp_virtual_keyboard_manager_v1 and zwlr_virtual_pointer_manager_v1, so
# every injecting command here runs with no root, no group and no udev rule -- measured on both
# versions [recon2/hyprland 3, recon2/arch 3.4].  The package installs the uinput rule anyway,
# which is why the default route below is uinput and the `--vkbd on` route is checked beside it.
phase_input() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    same "wdotool type arrives byte-exact in the editor" "us: yz@" "$(type_and_read 'us: yz@')"
    editor_clear
    guest "wdotool type --delay 30 -- abc; wdotool key Return; \
           wdotool type --delay 30 -- def" >/dev/null || true
    sleep 0.6; editor_save; sleep 1
    same "wdotool key Return is a keystroke, not text" "abc
def" "$(editor_text)"
    editor_clear
    guest "wdotool --vkbd on type --delay 30 -- 'vkbd: yz@'" >/dev/null 2>&1 || true
    sleep 0.5
    editor_save
    sleep 1
    same "--vkbd on types byte-exact with no uinput and no privilege" "vkbd: yz@" "$(editor_text)"
    layout_phase
}

# The layout, which is where Hyprland differs from every other desktop in this tree: XKB state
# is kept PER DEVICE, so switching the physical keyboard's group leaves the injected device on
# its own.  `HyprLayouts` reads the physical keyboard's index off `j/devices`; whether the
# keystrokes then land in that group was the open question this phase existed to record, and the
# 2026-09-09 run on resolute-hypr answered it: they do not.  See the xwant at the end.
#
# One more thing that run measured, and that `_hypr_keyboard`'s clauses had better not lean on:
# `main` MOVES.  With three keyboards (`power-button`, `at-translated-set-2-keyboard`,
# `wdotool-virtual-keyboard`) it was our own injected device that was `main: true` before the
# switch, and the physical one afterwards -- so `main` is "last used", not "the real keyboard",
# and the rule that skips `wdotool-*` before reading it is what makes the reader work at all.
layout_phase() {
    local kb
    kb=$(hypr_keyboard)
    if [ -z "$kb" ]; then fail "no keyboard in hyprctl -j devices"; return 1; fi
    note "physical keyboard: $kb"
    guest "hyprctl keyword input:kb_layout 'us,de'; hyprctl keyword input:kb_options grp:alt_shift_toggle" \
        >/dev/null 2>&1 || true
    sleep 2
    want "the compositor recompiled the keymap with two groups" "us,de" "$(hypr_kb_index "$kb")"
    guest "hyprctl switchxkblayout $kb 1" >/dev/null 2>&1 || true
    sleep 1
    want "hyprctl itself now holds group 2 for that device" "^1 " "$(hypr_kb_index "$kb")"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    want "keys explain names the second of two groups" "group 2 of 2" \
         "$(guest 'wdotool keys explain yz@ 2>&1' || true)"
    want "and names Hyprland's own device list as the source" "hyprland devices" \
         "$(guest 'wdotool keys explain yz@ 2>&1' || true)"
    wantnot "the group is read, not assumed" "\(assumed\)" "$(guest 'wdotool keys explain yz@ 2>&1' || true)"
    local got; got=$(type_and_read 'yz@ Straße')
    note "typed through /dev/uinput with the session in group 2: $(ev "$got")"
    note "$(guest 'hyprctl -j devices' || true)"
    # ANSWERED, 2026-09-09, on this flavor: the injected device does NOT follow the session's group, and
    # a reader that is right about the session is what makes `type` wrong.  With
    # `at-translated-set-2-keyboard` switched to group 1 (German) `hyprctl -j devices` still reported
    # `wdotool-virtual-keyboard` at `active_layout_index: 0`, `keys explain` correctly said
    # `layout: German -- group 2 of 2, from wayland + hyprland devices`, and `wdotool type "zy@ Strasse"`
    # into a `foot -e cat` produced `zyq Strasse`: the `@` was encoded as the German AltGr+Q and landed in
    # the US group as a plain `q`.  Before HyprLayouts, wdotool admitted it was guessing and typed
    # CORRECTLY, because the guess (US) was what the injected device really was [recon2/hyprland 3].
    #
    # NOT YET, and the route is ours and not a rung of the ladder: the uinput path must encode for the
    # group ITS OWN device is in, which is a second question from the one `keys explain` answers about the
    # session -- `xkbmap.HyprLayouts`' "fourth rule", named as open in `_hypr_keyboard`'s docstring
    # (requests-batch-12.md item 2, from batch 10).  Below that, route 2: `hyprctl switchxkblayout
    # wdotool-virtual-keyboard <n>` before the injection, which moves the session's own state and would
    # have to be put back.
    xwant "German types byte-exact after a switch on the physical keyboard (fix xkbmap.HyprLayouts: the \
injected device keeps its own group; measured 2026-09-09, zy@ typed as zyq)" "yz@ Straße" "$got"
    guest "hyprctl switchxkblayout $kb 0" >/dev/null 2>&1 || true
    sleep 1
    same "back in group 1, US types byte-exact again" "us: yz@" "$(type_and_read 'us: yz@')"
}

# ---------------------------------------------------------------- display
# The backend is Hyprland's own IPC and not the wlroots floor, and that is the whole point.
# Through zwlr_output_management on 0.53.3 the FIRST apply of a fresh session worked (rc 0 in
# 0.25 s) and every one after it timed out at 10.06 s with nothing changed -- `wlr-randr`, the
# reference client, hung for ever on the same request.  After such an apply Hyprland's OWN
# `keyword monitor` also stops taking effect (`ok`, nothing changes, three times in a row on one
# session) while the same command on a fresh session applies at once [recon2/hyprland 4].  That
# last sentence is what the hypr backend rides on, so the applies are plain checks here, and this
# phase is careful to be the first thing in the run that applies anything.
#
# One half of the recon paragraph above did NOT reproduce on this flavor, 2026-09-09, and the phase
# body says where: two timed-out wlr applies on a virgin three-head session were followed by two
# `keyword monitor` applies that landed in 0.28 s and 0.36 s, so a wlr apply does not stop 0.53.3
# taking `keyword monitor`.  What it does do is change how the wlr path itself answers: after a
# `keyword monitor` apply the same wlr request stops timing out and returns rc 0 in 0.64 s having
# changed nothing.  Both measurements are checks in this phase.
#
# What 0.56.2 differs in, measured: the first wlr apply of a FRESH single-head session already
# times out, where 0.53.3's worked [recon2/arch 3.4].  `hyprctl keyword monitor` was tried in that
# same session and answered ok changing nothing -- but only after three wxrandr applies and two
# wlr-randr attempts had timed out first, which is exactly the state 0.53.3 was measured to stop
# taking `keyword monitor` in.  So whether a fresh 0.56.2 session takes it -- the route this
# backend uses, AGENTS.md route 2 -- is UNMEASURED, and settling it is what arch-hypr exists for.
# Until that run the applies are `xwant` on Arch; if they go red there, the next route down the
# ladder is a patched Hyprland (route 6), a package to build and carry that nobody has costed.
# Reading is right on both versions and is checked on both.
phase_display() {
    want "wxrandr --print-backend is hypr and not the wlr floor" "^hypr$" \
         "$(guest 'wxrandr --print-backend' | tr -d ' \r' || true)"
    want "--print-backend --verbose names the compositor and its version" "compositor: Hyprland [0-9]" \
         "$(guest 'wxrandr --print-backend --verbose' || true)"
    want "and the protocol it really speaks" "protocol: Hyprland IPC" \
         "$(guest 'wxrandr --print-backend --verbose' || true)"
    local outs; outs=$(oracle_outputs || true)
    note "native oracle (hyprctl -j monitors): $(ev "$outs")"
    # The count, against the compositor's own list -- not `Monitors: [0-9]+`, which asserts only
    # that a number was printed and would pass on a header over an empty table.  (The Ubuntu arm
    # runs common_display_phase below, which asks the loose form once more; that line is common.sh's.)
    same "wxrandr --listmonitors counts the outputs hyprctl -j monitors has enabled" \
         "$(printf '%s\n' "$outs" | grep -c .)" \
         "$(guest 'wxrandr --listmonitors' | sed -n 's/^Monitors: //p' | tr -d ' \r')"
    local pair first
    pair=$(display_pair); first=${pair%% *}
    if [ -z "$first" ]; then fail "no enabled output in the oracle [$(ev "$outs")]"; return 1; fi
    # ------------------------------------------------------------------ the wlr route, FIRST
    # This used to be the last thing in the phase, on the reading of [recon2/hyprland 4] that an apply
    # through the wlr path is what stops a 0.53.3 session taking `keyword monitor`.  MEASURED on this
    # flavor 2026-09-09, and that is not what happens -- what happens is the reverse, and it is why the
    # check has moved to the top:
    #
    #   virgin session, three heads:  two wlr applies in a row, 10.16 s and 10.26 s, both timed out,
    #                                 the head unchanged, both naming the Hyprland clause;
    #   then `keyword monitor`:       0.28 s and 0.36 s, both landed -- so a timed-out wlr apply does
    #                                 NOT poison the route this backend uses;
    #   after a `keyword monitor`:    the SAME wlr apply answers rc 0 in 0.64 s, prints nothing, and
    #                                 changes nothing.
    #
    # So run last, this check was asking a session that cannot time out to time out: it saw an empty
    # stderr and went red about a compositor doing nothing wrong (67 pass / 1 fail, 2026-09-09).  Run
    # first, the premise holds and the rest of the phase still applies fine.  What is asserted is
    # wxrandr/core.py's Hyprland clause (U08) and not the rc: the generic "timed out" is true and
    # useless on this compositor, and the clause says which backend does work.  TWO applies, because on
    # a single head the first wlr apply of a session was measured to WORK (rc 0 in 0.25 s) and it is
    # the second that times out; with a second output present even the first hangs, so the second is
    # the one that carries the message on either shape.
    if [ "$DISTRO" != arch ]; then
        local m0; m0=$(hypr_mode "$first")
        guest "wxrandr --backend wlr --output $first --mode 1680x1050" >/dev/null 2>&1 || true
        want "the wlr route's timeout names the Hyprland clause and the backend that works (U08)" \
             "use --backend hypr" \
             "$(guest "wxrandr --backend wlr --output $first --mode 1280x1024 2>&1" || true)"
        # "timed out ... with nothing changed" is half the sentence, so the mode is read back: two wlr
        # applies asked for 1680x1050 and then 1280x1024, and $first has to still be in $m0.
        same "and nothing changed while it timed out, which is the other half of the sentence" \
             "$m0" "$(hypr_mode "$first")"
    fi
    # ------------------------------------------------------------------ the applies
    # The pair of applies this file exists for, taken before anything else in the phase has
    # touched an output.
    # Both applies SHRINK, and that is a fact about the rig and not a softened check.  Measured on
    # resolute-hypr 2026-09-09: on `-device virtio-vga` a head's mode can be made smaller as often as you
    # like and never larger again within a session.  1920x1080 -> 1680x1050 -> 1280x1024 all land;
    # 1280x1024 -> 1920x1080 does not, and the compositor is not what refuses it -- `hyprctl keyword
    # monitor` answers `ok`, aquamarine logs `atomic drm request: failed to commit: Invalid argument,
    # flags: ATOMIC_ALLOW_MODESET ATOMIC_TEST_ONLY`, and with `AQ_NO_ATOMIC=1` the legacy path fails the
    # same way (`drmModeSetCrtc failed`).  wxrandr's own re-read is what catches it and says so, which is
    # the behaviour this phase is here to prove.  Growing again needs a display device this rig cannot
    # have on this host: `-device virtio-vga-gl` is refused by the plain dbus display ("The display
    # backend does not have OpenGL support enabled") and `-display dbus,gl=on` dies with "egl: no drm
    # render node available" -- there is no /dev/dri on the dsb guest at all.  The claim under test is
    # "a second apply of the session lands", and two shrinks test it exactly.  The GROW is not dropped
    # for being unanswerable here: it is the third apply below, an xwant that goes XPASS on the day the
    # rig can answer, because `xrandr --mode 1920x1080` after `--mode 1280x1024` is an everyday script
    # and X does it.
    local one two three
    guest "wxrandr --output $first --mode 1680x1050" >/dev/null 2>&1 || true
    sleep 2; one=$(hypr_mode "$first")
    guest "wxrandr --output $first --mode 1280x1024" >/dev/null 2>&1 || true
    sleep 2; two=$(hypr_mode "$first")
    if [ "$DISTRO" = arch ]; then
        xwant "the first apply of a session lands on Hyprland 0.56.2 (until arch-hypr's first run \
says whether a fresh 0.56.2 session takes keyword monitor)" "^1680x1050$" "$one"
        xwant "the second apply of a session lands too on 0.56.2 (until the same run)" "^1280x1024$" "$two"
        note "0.56.2 is the bracket, and the two lines above are the whole measurement: the wlr apply"
        note "was dead from the first request of a fresh session [recon2/arch 3.4], while the only"
        note "keyword monitor tried there came after five timed-out wlr applies -- the state 0.53.3"
        note "stops taking it in too [recon2/hyprland 4].  The off/auto/right-of/below dance is not"
        note "run until they answer: every step of it is an apply, and each would put a question about"
        note "the compositor in the log as a failure of ours.  resolute-hypr runs the dance."
    else
        same "the FIRST apply of the session changes the mode, and hyprctl agrees" "1680x1050" "$one"
        same "the SECOND apply lands too (the wlr path wedged here at 10.06 s)" "1280x1024" "$two"
        # And back up, which is the shape parity actually owes: xrandr grows a head as readily as it
        # shrinks one.  It is the rig that refuses (the two modeset paths and their kernel errors are
        # named above this pair), so the line stays as a check with its route rather than as a comment.
        guest "wxrandr --output $first --mode 1920x1080" >/dev/null 2>&1 || true
        sleep 2; three=$(hypr_mode "$first")
        xwant "a mode that GROWS back lands (until the rig has a render node: -device virtio-vga-gl \
with -display dbus,gl=on -- refused here with 'egl: no drm render node available', no /dev/dri on the \
dsb guest -- or a different KMS device: qxl, bochs-display, virtio-gpu blob=on, untried)" \
             "^1920x1080$" "$three"
        common_display_phase
        # The other half of the wlr measurement at the top of this phase, and the one that is OURS.
        # After a `keyword monitor` apply Hyprland stops timing the wlr path out and starts answering
        # it: rc 0 in 0.64 s, empty stderr, and the head exactly where it was (measured 2026-09-09,
        # Virtual-2 asked for 1920x1080 while sitting at 1280x1024, three times).  A silent success
        # that changed nothing is worse for a script than the timeout it replaced, and no rung of
        # AGENTS.md's ladder is needed to fix it: `HyprOutputs._verify_applied` already re-reads what
        # it applied and says "Hyprland accepted the mode ... and did not apply it", and wxrandr's wlr
        # backend does not.  NOT YET, so it is an xwant naming that fix, asserted on the OUTPUT rather
        # than on the mode -- the mode here would also be held down by the rig's grow limit above, and
        # the claim is that the tool SAYS something, not that the rig can do it.
        local silent; silent=$(guest "wxrandr --backend wlr --output $first --mode 1920x1080 2>&1" || true)
        note "the wlr route after a keyword apply: [$(ev "$silent")], $first at $(hypr_mode "$first")"
        xwant "a wlr apply that changed nothing does not answer success in silence (fix wxrandr's wlr \
backend: re-read what was applied, the way HyprOutputs._verify_applied does)" "." "$silent"
    fi
}

# ---------------------------------------------------------------- mirror
# The only flavor besides sway that can run this: zwlr_screencopy_manager_v1 v3 is advertised on
# both versions (0.56.2 adds ext_image_copy_capture_manager_v1 v1), and the recon started and
# stopped a region mirror verbatim [recon2/hyprland 3, recon2/arch 3.4].  A region is what is
# mirrored on purpose: two heads of the same size at the same position already mirror through
# the layout, and wmirror sends you there instead of starting a process.
phase_mirror() {
    local chk; chk=$(guest 'wmirror --check' || true)
    want "wmirror --check finds the helper" "wl-mirror" "$chk"
    want "and a capture protocol" "capture: .*(zwlr_screencopy_manager_v1|ext_image_copy_capture)" "$chk"
    local pair first second
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    if [ -z "$second" ]; then
        note "one head only: a mirror needs a target, --heads 2 is what gives it"
        return 0
    fi
    # The region offset is in LAYOUT coordinates, not in the source head's own -- wmirror refuses one
    # that is not inside the source ("the region 800x600+100+100 is not inside Virtual-2, which is
    # 1280x1024+1920+0", measured 2026-09-09 on the run where phase_display's applies started landing
    # and left the anchor somewhere other than the origin).  So the offset is built from where the
    # source actually IS, and this phase no longer depends on which layout the phase before it left.
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
    want "wmirror --stop ends it" "^stopped +$second <- $first" "$(guest "wmirror --stop $second" || true)"
    same "and no wl-mirror is left behind" "" "$(guest 'pgrep -x wl-mirror' | tr -d ' \r\n' || true)"
}
