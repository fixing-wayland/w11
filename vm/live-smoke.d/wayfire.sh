# live-smoke.d/wayfire.sh -- Wayfire 0.10.0 (resolute-wayfire), the JSON-IPC path.
#
# The second compositor family that needs no privilege for anything, and the only one in the rig whose
# ORACLE is independent of the protocol under test: `window-rules/list-outputs` is Wayfire's own view of its
# outputs, where wlr-randr and wxrandr's wlr backend are two clients of one zwlr_output_manager_v1.  oracle.py
# reads the IPC first and falls back to wlr-randr; the display phase prints both.
#
# Every check below was measured on 2026-09-08 against wayfire 0.10.0-1 -- the exact package this flavor
# installs -- run headless on the dev box with the rig's own plugin list (WLR_BACKENDS=headless,
# WLR_RENDERER=pixman, tests/support.py:HeadlessWayfire).  What that run cannot show is the three things a
# real seat owns: logind flipping the session Type to wayland, wf-panel painting, and /dev/uinput being
# there -- which is what this file exists to find out.  It has since found out: two live runs on the
# golden, 53/0/1 on 2026-09-08 and 58/0/1 on 2026-09-09 (instance wf1, two heads, the 0.4.0 deb), and the
# numbers this file cites as "live" are the second one's.
#
# Wayfire FLOATS by default (no tiling plugin in the stock list), so unlike sway.sh nothing here is skipped
# for being a tiling window manager: move, size, minimize, the maximize pair and the nine viewports are all
# claims the tool makes and all of them are checked.  What is NOT exact is the window SIZE: a terminal
# commits the nearest whole character cell, so `windowsize 800 600` on foot came back 798x598 and
# `wwmctl -e 0,10,20,300,200` came back 300x195 (measured).  The POSITION is exact to the pixel here, so the
# size is checked to within one cell and the position byte for byte.
#
# X11 gives no single number to be byte-equal to either, which is why the tolerance is a tolerance and not a
# literal: `xdotool windowsize 800 600` on an xterm (Ubuntu's /usr/bin/xdotool, Xvfb :98 on this box,
# 2026-09-08) read back 800x600 under no window manager and under xfwm4, and 796x589 under openbox and
# marco, because those two honour the WM_NORMAL_HINTS resize increments the terminal sets.  Position under a
# reparenting WM moved too (100,100 asked, 102,140 read back under openbox).  The rounding is the window
# manager's business on X and the client's on Wayland, and both sides of that fork fit inside one cell.

SMOKE_PHASES="busrec install windows wm proxy input desktops display mirror root nodialog"
EDITOR_CLASS=foot

# The X client the WM_CLASS-instance and _NET_CLIENT_LIST checks need; started by phase_wm and killed there.
XTERM_TITLE=smokex

# No text editor on this golden either: the "editor" is foot running `cat >>`, sway.sh's helper, and for the
# same reason -- `cat >` keeps its offset across editor_clear's truncation and the next line lands behind a
# run of NULs.  O_APPEND makes the write after a truncate land at offset 0.
editor_start() {
    guest "rm -f $SMOKE_FILE; setsid nohup foot -- sh -c 'cat >> $SMOKE_FILE' >/dev/null 2>&1 </dev/null &
           sleep 3; true" >/dev/null || true
}
editor_save()  { guest "wdotool key Return" >/dev/null || true; }
editor_clear() { guest "wdotool key ctrl+u; : > $SMOKE_FILE" >/dev/null 2>&1 || true; }

# `<pos> <w>x<h>` from win_geom, compared the way the two halves deserve: the position exactly, the size to
# within `tol` pixels on each axis (the client's own cell quantisation, never the compositor's).
geom_is() {   # geom_is <what> <x,y> <w> <h> <tol> <got>
    local what=$1 pos=$2 w=$3 h=$4 tol=$5 got=$6 gp gw gh
    gp=${got%% *}; gw=${got#* }; gh=${gw#*x}; gw=${gw%x*}
    if [ "$gp" != "$pos" ]; then fail "$what [position want '$pos', got '$(ev "$got")']"; return; fi
    if awk -v a="$gw" -v b="$w" -v c="$gh" -v d="$h" -v t="$tol" \
           'BEGIN { exit !((a - b < t) && (b - a < t) && (c - d < t) && (d - c < t)) }'; then
        pass "$what ($got; the size is $w x $h to within $tol px of character-cell rounding)"
    else
        fail "$what [want ${pos} ${w}x${h} +-${tol}, got '$(ev "$got")']"
    fi
}

# Wayfire's own IPC, as a ONE-LINE question from the guest: int32-LE length + JSON both ways, the framing
# oracle.py already speaks.  Used to cross-read what the tools say against what the compositor thinks --
# never to make anything happen.  Five reads happen over a run, in this order:
# `window-rules/get_cursor_position` (twice: before anything has warped the pointer and after the warp),
# `window-rules/get-focused-output`, `window-rules/list-outputs outputs` and `wayfire/get-keyboard-state`.
#
# One line and NOT a heredoc, though a heredoc reads better: guest-capture.sh's `run` echoes `### <command>`
# and then the command's output, so a heredoc's body would be recorded as bytes off the compositor, and
# tests/fixtures/live/README.md says a transcript carries nothing written by hand.  One line per method also
# gives fake-vmctl a distinct key per read (its norm() collapses whitespace and strips the trailing pipes),
# so replay does not depend on three different questions sharing one prefix and coming back in the order
# they were recorded; only the two get_cursor_position reads share a key, and take() walks those in order.
# Whoever records this flavor runs the same five commands: an unrecorded one is answered with empty and
# status 0, which turns the `same` checks red in selftest-offline's first pass.  The two list-outputs reads
# differ only by that trailing `outputs` word, and answer() tries the exact key before its longest-prefix
# fallback -- so a recording that has the JSON read and not the `outputs` one makes the display check red
# rather than quietly agreeing with itself.  Proven over the transcript of the 2026-09-09 live run
# (scratchpad/b13r/): 49 checks green in pass 1, and nine one-value mutations each turning exactly one red.
WFIPC_PY='import json,os,socket,struct,sys,glob;'\
'r=os.environ.get("XDG_RUNTIME_DIR") or "/run/user/1000";'\
'p=os.environ.get("WAYFIRE_SOCKET") or (sorted(glob.glob(r+"/wayfire-*.socket")) or [""])[0];'\
'sys.exit("no wayfire socket: is plugins = ... ipc ipc-rules in wayfire.ini?") if not p else None;'\
's=socket.socket(socket.AF_UNIX);s.connect(p);f=s.makefile("rb");'\
'b=json.dumps({"method":sys.argv[1],"data":json.loads(sys.argv[2])}).encode();'\
's.sendall(struct.pack("=i",len(b))+b);n=struct.unpack("=i",f.read(4))[0];a=json.loads(f.read(n));'\
'v=a if isinstance(a,list) else (a.get("outputs") or []);'\
'print("\n".join("%s %d,%d"%(o["name"],o["geometry"]["x"],o["geometry"]["y"]) for o in v)'\
' if len(sys.argv)>3 else json.dumps(a))'

wfipc() {   # wfipc <method> [outputs] -- one question, one answer, no state changed
    guest "python3 -c '$WFIPC_PY' $1 '{}'${2:+ $2}"
}

# The compositor's own cursor, as `<x> <y>`: get_cursor_position answers floats
# (`{"result": "ok", "pos": {"x": 300.0, "y": 300.0}}`), and a pointer position is an integer everywhere the
# tools print one, so the fraction is cut rather than rounded -- the same int() the backend does.
wf_cursor() {
    wfipc window-rules/get_cursor_position \
        | sed -n 's/.*"x": *\([0-9]*\)\.[0-9]*, *"y": *\([0-9]*\)\.[0-9]*.*/\1 \2/p' | tr -d '\r'
}

# Wayfire floats, so this is the FULL window phase: everything common.sh does plus the two things the wlr
# floor refused on this very compositor and the IPC backend answers -- a real geometry (the floor reported
# the OUTPUT rectangle for every window) and a pid (the floor said "has no pid associated with it").
phase_windows() {
    guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
    editor_start
    local out
    out=$(await 30 '[0-9]' "wdotool search --class $EDITOR_CLASS | head -1" || true)
    WIN=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$WIN" ]; then fail "wdotool search --class $EDITOR_CLASS found no window [$(ev "$out")]"; return 1; fi
    pass "wdotool search --class $EDITOR_CLASS -> $WIN"
    want "getwindowname is not empty" "." "$(guest "wdotool getwindowname $WIN" || true)"
    same "getwindowclassname is the app-id" "foot" \
         "$(guest "wdotool getwindowclassname $WIN" | tr -d ' \r\n' || true)"
    guest "wdotool windowmove $WIN 100 100; wdotool windowsize $WIN 800 600" >/dev/null || true
    sleep 1
    # 100,100 798x598 measured; 2 px on each axis is foot's cell, and the recon's own configure-view read
    # `40,50 500x400` back as `40,50 498x390`.  8 px of tolerance covers a cell of any font this image has.
    geom_is "windowmove 100 100 + windowsize 800 600 -> getwindowgeometry" 100,100 800 600 8 "$(win_geom "$WIN")"
    # The floor's refusal here was `window 1000000 has no pid associated with it.` [recon2/wayfire 2.3];
    # list-views has carried the pid all along.
    local pid; pid=$(guest "wdotool getwindowpid $WIN" | tr -d ' \r\n' || true)
    if guest "test -d /proc/${pid:-0}" >/dev/null 2>&1 && [ -n "$pid" ]; then
        pass "getwindowpid names a live process ($pid)"
    else fail "getwindowpid did not answer a running pid [$(ev "$pid")]"; fi
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    same "windowactivate --sync then getactivewindow is that window" "$WIN" \
         "$(guest 'wdotool getactivewindow' | tr -d ' \n' || true)"
    guest "wdotool windowminimize $WIN" >/dev/null || true
    sleep 1
    local act; act=$(guest 'wdotool getactivewindow 2>&1' | tr -d ' \n' || true)
    if [ "$act" = "$WIN" ]; then fail "windowminimize left the window active (getactivewindow still $WIN)";
    else pass "windowminimize: the window is no longer the active one (getactivewindow: $(ev "$act"))"; fi
    guest "wdotool windowmap $WIN; wdotool windowactivate --sync $WIN" >/dev/null || true
    # The pointer BEFORE anybody has warped it, which is the read the wlr floor cannot serve: with no
    # compositor query to ask, `getmouselocation` has only the input daemon's model of what IT has injected,
    # and on a daemon that has injected nothing wdotool refuses (POINTER_UNKNOWN in wdotool/input_cmds.py --
    # 0,0 there is the virtual tablet's untouched axis state, not a position).  Wayfire answers from
    # get_cursor_position, so the FIRST pointer read of the run is an answer, and the value is the
    # compositor's: what the compositor says is the wanted side of the `same`, not a literal, because where
    # a seat puts its cursor at login is the seat's business (640,360 headless, 960,540 on the golden).
    local cpos mloc st1=0
    cpos=$(wf_cursor)
    mloc=$(guest 'wdotool getmouselocation') || st1=$?
    ok "getmouselocation answers before this run has warped the pointer" "$st1"
    want "and answers a position rather than the daemon's refusal" \
         "^x:[0-9]+ y:[0-9]+ screen:0 window:" "$mloc"
    if [ -z "$cpos" ]; then fail "the compositor did not answer get_cursor_position, so there is no oracle"
    else same "the unwarped pointer is where the compositor says it is" "$cpos" \
              "$(printf '%s' "$mloc" | sed -n 's/^x:\([0-9]*\) y:\([0-9]*\).*/\1 \2/p')"; fi
    guest "wdotool mousemove 300 300" >/dev/null || true
    want "mousemove 300 300 -> getmouselocation" "x:300 y:300" "$(guest 'wdotool getmouselocation' || true)"
    # The pointer read is Wayfire's own get_cursor_position, and the recon measured 0 px of error between the
    # two: `{"pos":{"x":300.0,"y":300.0}}` for `x:300 y:300`.  Read here as floats, compared as integers.
    same "the compositor's own get_cursor_position agrees to the pixel" "300 300" "$(wf_cursor)"
    local st=0
    guestq "wdotool mousemove 300 300 click 1" || st=$?
    ok "mousemove 300 300 click 1" "$st"
}

# The window-manager half: the two columns the wlr floor left empty on this compositor (a real X id and the
# WM_CLASS INSTANCE) and the maximize pair, which Wayfire does through the grid plugin.
phase_wm() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    guest "setsid nohup xterm -T $XTERM_TITLE -e sh -c 'sleep 600' >/dev/null 2>&1 </dev/null & sleep 3; true" \
        >/dev/null || true
    local rows; rows=$(guest 'wwmctl -l -x' || true)
    want "wwmctl -l -x lists the foot window under its app-id" "foot\.foot" "$rows"
    # The measurement this flavor is for: on the wlr floor the same xterm listed as `0x000f4242 -1
    # XTerm.XTerm` -- a minted id, no desktop, and the CLASS where wmctrl prints the INSTANCE.  Through the
    # IPC backend it is `0x0040000c  0 xterm.XTerm`, which is byte-identical to `wmctrl -l -x` [recon2/
    # wayfire 2.4; re-measured on this box 2026-09-08 with the backend in the tree].
    want "the xterm's row carries the WM_CLASS INSTANCE, not the class in both fields" \
         "^0x[0-9a-f]+ +[0-9]+ +xterm\.XTerm" "$rows"
    # The id half, stated against the ORIGINAL and not against a shape: `wmctrl` is in this flavor's
    # EXTRA_PKGS and reads the same Xwayland root over the same X connection, so parity here is byte
    # equality between two rows and nothing weaker.  A regex would not have caught the thing this check
    # exists for -- the floor's minted `0x000f4242` is eight valid hex digits and passes any id-shaped
    # pattern; only wmctrl's own answer distinguishes it [recon2/wayfire 5.2 item 2, which asks for exactly
    # this cross-read].
    local both theirs mine
    both=$(guest 'wmctrl -l -x' || true)
    theirs=$(printf '%s\n' "$both" | awk -v t="$XTERM_TITLE" 'index($0, t) { print $1; exit }')
    mine=$(printf '%s\n' "$rows" | awk -v t="$XTERM_TITLE" 'index($0, t) { print $1; exit }')
    if [ -z "$theirs" ]; then
        fail "the real wmctrl -l -x did not list the xterm, so the id column has no oracle [$(ev "$both")]"
    else
        same "wwmctl gives that window the same X id the real wmctrl prints" "$theirs" "$mine"
    fi
    wantnot "no window is left on desktop -1 (the floor's answer for every row)" "^0x[0-9a-f]+ +-1 " "$rows"
    want "wwmctl -l -p carries the pid column" "^0x[0-9a-f]+ +[0-9]+ +[0-9]{2,} " "$(guest 'wwmctl -l -p' || true)"
    # wlroots' own xwm string, on Wayfire's Xwayland check window: Wayfire adds none of its own.
    want "wwmctl -m names the window manager wlroots' xwm reports" "^Name: wlroots wm$" \
         "$(guest 'wwmctl -m' || true)"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    sleep 0.5
    # b7a60f0's claim, stated over the geometry the window HAS rather than over a literal, because the
    # literal is the client's cell rounding and not the tool's business.  Measured: 10,20 300x195 ->
    # 0,0 1920x1080 -> 10,20 300x195, exact both ways.
    local before; before=$(win_geom "$WIN")
    guest "wwmctl -r :ACTIVE: -b add,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    local maxed; maxed=$(win_geom "$WIN")
    if [ "$maxed" = "$before" ]; then fail "add,maximized_vert,maximized_horz did not change the geometry ($before)";
    else pass "add,maximized_vert,maximized_horz: $before -> $maxed"; fi
    want "wxprop -id says the window is maximized on both axes" \
         "_NET_WM_STATE_MAXIMIZED_HORZ.*_NET_WM_STATE_MAXIMIZED_VERT" \
         "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    guest "wwmctl -r :ACTIVE: -b remove,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    same "b7a60f0: remove,maximized_vert,maximized_horz restores exactly what was there" "$before" \
         "$(win_geom "$WIN")"
    # The X root here is Xwayland's real one, as on sway -- but the list is not the X plane alone: the
    # backend's views() puts every native window in it too, under the id wwmctl printed.  Measured with one
    # foot and one xterm up: `window id # 0x40000c, 0x5`.
    want "wxprop -root _NET_CLIENT_LIST names the X client under its real id" "window id # 0x[0-9a-f]{6}" \
         "$(guest 'wxprop -root _NET_CLIENT_LIST' || true)"
    guest "pkill -x xterm; true" >/dev/null 2>&1 || true
}

# Wayfire's desktops are a 3x3 viewport grid per output, flattened y*3+x -- nine, fixed, on every output,
# where sway has one workspace per output and GNOME grows them on demand.  The floor refused all three of
# these commands on this compositor [recon2/wayfire 2.3].
phase_desktops() {
    same "get_num_desktops is the 3x3 viewport grid" "9" \
         "$(guest 'wdotool get_num_desktops' | tr -d ' \r\n' || true)"
    local dl; dl=$(guest 'wwmctl -d' || true)
    same "wwmctl -d prints one row per viewport of the 3x3 grid" "9" \
         "$(printf '%s\n' "$dl" | grep -c '^[0-9]' || true)"
    want "and 4 is one of them, not the current one" "^4 +- " "$dl"
    guest "wdotool set_desktop 4" >/dev/null || true
    sleep 1
    same "set_desktop 4 -> get_desktop (viewport 1,1 of the grid)" "4" \
         "$(guest 'wdotool get_desktop' | tr -d ' \r\n' || true)"
    want "wwmctl -d now marks 4 as the current viewport" "^4 +\* " "$(guest 'wwmctl -d' || true)"
    # Wayfire's own answer, so that the round trip is not the backend agreeing with itself: `workspace`
    # on the focused output carries x and y, and 4 is x=1 y=1.
    want "the compositor's own list-outputs agrees the viewport is 1,1" '"x": 1, "y": 1' \
         "$(wfipc window-rules/get-focused-output || true)"
    guest "wdotool set_desktop 0" >/dev/null || true
    sleep 1
    same "set_desktop 0 -> get_desktop" "0" "$(guest 'wdotool get_desktop' | tr -d ' \r\n' || true)"
    # The refusal names the grid rather than a count, because the grid is what the user can change:
    # `wayfire: desktop 9 does not exist (this output's viewport grid is 3x3)`, exit 1 (measured).
    local st=0 out
    out=$(guest "wdotool set_desktop 9 2>&1") || st=$?
    if [ "$st" = 0 ]; then fail "set_desktop 9 was accepted on a nine-viewport grid (0..8) [$(ev "$out")]"
    else want "set_desktop 9 is refused, naming this output's viewport grid" \
              "desktop 9 does not exist .this output.s viewport grid is 3x3" "$out"; fi
}

# The one input route that needs nothing at all: zwp_virtual_keyboard_v1, no uinput, no root, no group.
# Measured byte-exact on this box against wayfire 0.10.0 (`vkbd: yz@\n` out of foot's `cat >>`).
layout_phase() {
    editor_clear
    guest "wdotool --vkbd on type --delay 30 -- 'vkbd: yz@'" >/dev/null 2>&1 || true
    sleep 0.5
    editor_save
    sleep 1
    same "--vkbd on types byte-exact with no uinput and no privilege" "vkbd: yz@" "$(editor_text)"
    local ex; ex=$(guest 'wdotool keys explain --chars z' || true)
    want "keys explain reads the keymap off the wire" "^layout: .* -- group [0-9]+ of [0-9]+" "$ex"
    # A plain `want` since the golden's ini carries the second layout: vm/build-image.sh writes
    # `xkb_layout = us,de` into wayfire.ini, so the keymap has two groups, choose_group is no
    # longer CERTAIN and fetch() asks WayfireLayouts -- which is what puts wayfire's name in the
    # clause.  The route was the ini and only the ini: `wayfire/set-config-options
    # {"input/xkb_layout": "us,de"}` answered `{"result":"ok"}` and left get-keyboard-state
    # reporting one layout (measured on this box 2026-09-08, wayfire 0.10.0), and
    # `set-keyboard-state` corrupts the layout list [recon2/wayfire 2.7].
    want "keys explain names wayfire as the group's source" "from wayland \+ wayfire" "$ex"
    note "get-keyboard-state: $(ev "$(wfipc wayfire/get-keyboard-state || true)")"
}

# common.sh's body, plus the second and third opinions: the IPC is the oracle, wlr-randr is the other client
# of the protocol wxrandr wrote through, and the three must agree on every enabled head.
#
# The count check in common_display_phase is the right one here, measured: `--output HEADLESS-2 --off` takes
# the head out of `window-rules/list-outputs` ENTIRELY -- Wayfire drops the wf::output rather than keeping a
# disabled row -- and wlr-randr drops its `Position:` line for the same head, so both readers go from two
# lines to one.  `--auto` brings it back at 0,0, on top of head 1, until the `--right-of` that follows moves
# it (measured on this box against wayfire 0.10.0; the two readers agreed in all three states).
phase_display() {
    common_display_phase
    note "wlr-randr: $(guest "wlr-randr | awk '/^[A-Za-z]/ { n = \$1 } /Position:/ { print n, \$2 }'" \
                      | tr '\n' ' ' || true)"
    note "Wayfire IPC: $(ev "$(wfipc window-rules/list-outputs || true)")"
    # `a` comes from the IPC DIRECTLY and not through oracle_outputs: oracle.py's wayfire() falls back to
    # wlr-randr when the socket is missing or list-outputs answers nothing, which would leave this line
    # comparing wlr-randr with wlr-randr -- a check that cannot fail is not a check.  The reply to
    # list-outputs is a bare JSON array, one object per output, and `outputs` is the mode of the helper that
    # turns it into oracle.py's `<name> <x>,<y>` lines.
    local a b
    a=$(wfipc window-rules/list-outputs outputs | sort | tr '\n' ' ')
    b=$(guest "wlr-randr | awk '/^[A-Za-z]/ { n = \$1 } /Position:/ { print n, \$2 }'" | sort | tr '\n' ' ')
    if [ -z "$(printf '%s' "$a" | tr -d ' ')" ]; then
        fail "window-rules/list-outputs named no output at all, so wlr-randr has nothing to agree with"
    else
        same "Wayfire's own list-outputs and wlr-randr name the same heads at the same places" "$a" "$b"
    fi
}

# wmirror end to end on the second compositor family that has it: the recon measured --check, a region
# mirror, --list and --stop all green here, and 0.10 has zwlr_screencopy and NOT ext-image-copy-capture
# (that lands in Wayfire 0.11) -- which is the line --check has to print.
phase_mirror() {
    local out st=0
    out=$(guest 'wmirror --check') || st=$?
    ok "wmirror --check" "$st"
    want "--check names zwlr_screencopy, the only capture protocol Wayfire 0.10 has" \
         "capture: +zwlr_screencopy_manager_v1 v3" "$out"
    local pair first second
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    if [ -z "$second" ]; then note "one head only: a mirror needs somewhere to put it, skipped"; return 0; fi
    # The region offset is in LAYOUT coordinates and not in the source head's own, so it is built from
    # where that head actually IS.  Measured in CI on 2026-09-09 (run 34308982263, job 102331500198): the
    # fixed `+100+100` gave `wmirror: the region 400x300+100+100 is not inside Virtual-2, which is
    # 1920x1080+1920+0`, and the three checks after it fell with the refusal -- four of that run's four
    # FAILs on this flavor.  vm/live-smoke.d/hypr.sh's phase_mirror carries the same line for the same
    # reason.
    local org rx ry
    org=$(oracle_outputs | sed -n "s/^$first //p" | head -1)
    rx=$(( ${org%%,*} + 100 )); ry=$(( ${org##*,} + 100 ))
    local region="400x300+$rx+$ry"
    st=0
    # The target head's framebuffer BEFORE the mirror starts, read from INSIDE the guest so the answer is
    # a recorded command the replay reproduces: grim (resolute-wayfire.yaml:54) captures $second off
    # wayfire's own zwlr_screencopy and we count the PNG's bytes.  The host-side `$VM shot` this used to
    # take is a QMP screendump the replay cannot make -- fake-vmctl writes a text placeholder where the
    # png goes -- so the pixel pass it emitted was live-only, and the recording replayed 88 checks where
    # the run logged 89 (requests-batch-16 §1b, rig-recordings refused to name the fixture).  grim moves
    # that one check onto a recorded guest command; the rest of the pixel claim stands unchanged.
    local base; base=$(guest "grim -o $second - 2>/dev/null | wc -c" | tr -d ' \r\n' || true)
    note "head $second grim png bytes before the mirror: ${base:-none}"
    out=$(guest "wmirror $first --to $second --region $region") || st=$?
    ok "wmirror $first --to $second --region $region" "$st"
    want "the mirror is running with the region it was given" \
         "$second <- $first +region 400x300\+$rx\+$ry" "$out"
    want "wmirror --list shows it" "$second <- $first" "$(guest 'wmirror --list' || true)"
    # wl-mirror paints the target when its first screencopy frame lands, a few seconds after the started
    # line on a loaded runner (CI run 34329371964 measured the target painted; run 34336882062, the same
    # tree, took the one shot too early and saw it flat).  So poll grim and compare each capture with the
    # baseline: a mirror that delivered frames REPLACES the target's wallpaper with the scaled source
    # region, and the PNG size moves out of all doubt -- measured on resolute-wayfire 0.10.0 2026-09-12
    # (this box, two 1920x1080 heads): Virtual-2's wallpaper PNG was 3316378 bytes on one boot and 3316259
    # on the next before the mirror (the baseline is PER-RUN -- the panel clock and the boot's wallpaper
    # shift it, which is exactly why the check compares against its OWN baseline, not a constant) and
    # dropped to 1420780 while the mirror ran, each stable across six polls.  A change of more than an
    # eighth of the baseline is that arrival; a target still within an eighth ten seconds in is a mirror
    # that delivered nothing.  Every read is a recorded guest command, so the replay walks the same polls
    # in order (fake-vmctl take() over one key) and reaches the same verdict, which is what makes it 89/89.
    #
    # A baseline of 0 is NOT "flat", it is "grim captured nothing" -- `grim | wc -c` prints 0 when grim
    # fails (no socket, no binary), and the two must not read the same: a 0 baseline would make every
    # later non-zero read pass falsely (0 vs anything is > 0/8), and a grim that fails throughout would
    # blame the mirror for a capture that never happened.  grim is on this golden (resolute-wayfire.yaml:54),
    # so a 0-byte baseline is a real failure of this flavor, reported as itself below.
    local painted=false i now
    if [ "${base:-0}" -gt 0 ]; then
        for i in 1 2 3 4 5 6 7 8 9 10; do
            now=$(guest "grim -o $second - 2>/dev/null | wc -c" | tr -d ' \r\n' || true)
            note "head $second grim png bytes: ${now:-none}"
            if [ "${now:-0}" -gt 0 ] \
               && awk -v a="$now" -v b="$base" 'BEGIN { d = a - b; if (d < 0) d = -d; exit !(d > b / 8) }'; then
                painted=true; break
            fi
            sleep 1
        done
    fi
    # The host-side QMP screendump of the same head, as a NOTE and never a pass/fail: it reads the actual
    # scanout (a second opinion on the compositor buffer grim reads), but it is a live-run measurement the
    # replay's placeholder cannot serve, so it stays out of the count that has to match on replay.
    local probe sd; probe=$(mktemp -t wf-mirror-XXXXXX.png)
    if "$VM" shot "$NAME" "$((${second##*-} - 1))" "$probe" >/dev/null 2>&1 \
       && sd=$(identify -format '%[fx:standard_deviation]' "$probe" 2>/dev/null); then
        note "host-side QMP screendump of $second: standard deviation $sd"
    fi
    rm -f "$probe"
    if $painted; then
        pass "the target head is painting something while the mirror runs"
    elif [ "${base:-0}" -gt 0 ]; then
        fail "the target head did not change ten seconds into the mirror: nothing arrived" \
             "(this flavor paints the source: wf-background and wf-panel are up on every boot)"
    else
        fail "grim captured nothing on $second before the mirror (0 bytes): cannot tell if it painted" \
             "(grim is on this golden, resolute-wayfire.yaml:54, so a 0-byte capture is a real failure)"
    fi
    st=0
    out=$(guest "wmirror --stop $second") || st=$?
    ok "wmirror --stop $second" "$st"
    same "wmirror --list is empty again" "" "$(guest 'wmirror --list' | tr -d ' \r\n' || true)"
}
