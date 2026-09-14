# live-smoke.d/cinnamon-wayland.sh -- Cinnamon 6.4.13 on muffin 6.4.1 (resolute-cinnamon-wayland).
#
# Muffin is Mutter's fork with Mutter's D-Bus display API under Cinnamon's names and NO wlroots protocol
# of any kind, so this desktop is the GNOME row of the matrix reached by a different door: displays over
# `org.cinnamon.Muffin.DisplayConfig`, windows over `org.Cinnamon.Eval` (an ungated
# `JSON.stringify(eval(code))` on the session bus, ~9 ms a round trip -- KWin's loadScript story, not
# GNOME's bridge story), input over /dev/uinput alone, and no capture protocol at all
# [recon2/cinnamon 2.1, 2.2, 4].
#
# THE FLAVOR'S FIRST RUN, 2026-09-09 on this rig (golden resolute-cinnamon-wayland, 1783 packages, two
# 1920x1080 heads), and the one thing it found before any check ran: **Xwayland took the session down**.
# Cinnamon started completely -- `Cinnamon took 2888 ms to start`, both monitors seen, applets loaded,
# /run/user/1000/wayland-0 listening -- and then Xwayland 24.1.10 died with `Fatal server error: Caught
# signal 11 (Segmentation fault)`; muffin treats that as fatal (`mutter-WARNING: Connection to xwayland
# lost`), exits, and cinnamon-session gives up with `App 'cinnamon-wayland.desktop' respawning too
# quickly`.  The whole smoke then reported "no Wayland session found" -- 11 pass, 10 fail.
#
# THAT IS FIXED IN THE GOLDEN, and it was never the GPU [goal2/recon/gl.md 1-5, 2026-09-11]: the crash is
# Xwayland 24.1.10's `damage_report()` NULL dereference (guest objdump, offset 0x5be4a, %rbx NULL one
# instruction after pixman_region_union), fixed upstream in 24.1.11, which Ubuntu 26.04 has not shipped
# -- the same 2:24.1.10-1 is fine under mutter and under kwin on this identical rig, `-glamor off -shm`
# crashes identically, and so does a real virgl render node.  AGENTS.md route 5 at its cheapest end
# answers it: vm/flavors/resolute-cinnamon-wayland.yaml's build hook installs Debian's pinned
# `xwayland_24.1.13-1_amd64.deb` into the golden, and with it the session comes up and STAYS up with a
# LIVING X plane -- `wmctrl -l` and `xprop -root _NET_CLIENT_LIST` answer, and this is the one flavor in
# the rig that lists an XWayland xterm and a native gnome-terminal in one list.  It is a hold: the day
# resolute ships xwayland >= 24.1.11 the hook goes and nothing here changes.
#
# Measured on THAT session (2026-09-09, the one with Xwayland moved aside by hand) with the tree's
# zipapps and the 0.4.0 deb installed -- every native-plane byte below still stands, and the block
# after it is the same run repeated on the fixed golden:
#
#     wxrandr --print-backend                 cinnamon
#     wxrandr --print-backend --verbose       compositor: Muffin / protocol: org.cinnamon.Muffin.
#                                             DisplayConfig (D-Bus) / available: yes
#     warandr --print-backend                 cinnamon
#     wxrandr --listmonitors                  Monitors: 2 / 0: +*Virtual-1 ... (U25 landed 2026-09-09:
#                                             cli.py's cinnamon arm builds MutterOutputs(flavor=MUFFIN))
#     oracle.py cinnamon-wayland              Virtual-1 0,0 / Virtual-2 1920,0
#     wwmctl -m                               Name: Mutter (Muffin)
#     wwmctl -l -x                            0x00000001  0 org.gnome.Terminal.org.gnome.Terminal ...
#     wwmctl -l -G -p                         0x00000001  0 5726  305 31 654 472
#     wwmctl -d                               four workspaces, DG 3840x1080, WA 0,0 1920x1040
#     windowmove 100 100 + windowsize 800 600 100,100 798x591 (VTE cells; the position is exact)
#     getwindowpid                            5726 -- get_pid() is -1 for a native window, get_client_pid
#                                             is the one that answers [recon2/cinnamon 2.3]
#     the maximize pair                       100,100 798x591 -> 0,0 1920x1040 -> 100,100 798x591 (b7a60f0)
#     wxprop -id 1 _NET_WM_STATE              _NET_WM_STATE_MAXIMIZED_HORZ, ..._VERT, ..._FOCUSED
#     -b add,shaded                           is_shaded() true, then false again; wxprop said nothing
#                                             about it that day and prints _NET_WM_STATE_SHADED now
#     wdotool type / key Return               byte-exact through /dev/uinput with the package's udev rule
#     mousemove 300 300 / getmouselocation    x:300 y:300 screen:0 window:1, and Cinnamon's own
#                                             global.get_pointer() answered "300,300"
#     wmirror --check                         the two-protocol refusal, rc 1
#
# MEASURED AGAIN 2026-09-12, on a golden carrying Xwayland 24.1.13, `vm/live-smoke.sh
# resolute-cinnamon-wayland --heads 3` over the working tree: **76 pass, 1 fail** (this flavor's first
# run was 11 pass / 10 fail, and CI's two were 19/9 and 19/10).  What the X plane answers now, and what
# the checks below assert:
#
#     wwmctl -l -x        0x0120000c  0 xterm.XTerm           <host> smokex
#                         0x00000008  0 org.gnome.Terminal.org.gnome.Terminal  <host> Terminal
#                         -- the mixed list this flavor was built for [recon2/cinnamon 8]
#     wxprop -root _NET_CLIENT_LIST   window id # 0x10000df, 0x10000db, 0x10000d7, 0x800004, 0x800008,
#                                     0x80000c, 0xf, 0x120000c, 0x8
#     the shaded window   _NET_WM_STATE_SHADED, _NET_WM_STATE_HIDDEN, _NET_WM_STATE_FOCUSED -- and the
#                         ORACLE agrees byte for byte: with an xterm shaded, real
#                         `DISPLAY=:0 xprop -id 0x120000c _NET_WM_STATE` on this session's own X server
#                         prints `_NET_WM_STATE_SHADED, _NET_WM_STATE_HIDDEN`, same atoms, same order
#     wdotool --vkbd on   ...names Muffin on both sides of the semicolon
#
# THE ONE FAIL is common.sh's `phase_proxy` "the original wmctrl -l lists what the clone lists, ids
# tokenised", and it is a real disagreement about ONE column.  The six desktop/background X windows
# (3 csd-background, 2 nemo-desktop, Desktop) are listed `-1` by the clone and `0` by the original
# through the proxy.  Measured here, 2026-09-12: muffin marks all six `_NET_WM_STATE_STICKY` and
# `is_on_all_workspaces()` is true for each -- and muffin publishes NO `_NET_WM_DESKTOP` and no
# `_WIN_WORKSPACE` on their X windows at all (`DISPLAY=:0 xprop -id 0x800004 _NET_WM_DESKTOP` ->
# `not found.`), while /usr/bin/wmctrl 1.07 prints `0` for a window carrying neither.  So each side is
# reporting what it can see, and which one has to move is the oracle's call: what real `wmctrl -l`
# prints for nemo-desktop on a Cinnamon **X11** session, where muffin's own X11 path does set the
# property.  `resolute-cinnamon` is that session and the run is one `wmctrl -l`; NOT YET only because
# every golden key in the rig moved this wave and that image is a 53-minute build today.  Nothing in
# wwmctl or xw11 should be changed before it is taken.
#
# The recon's own numbers still stand behind the shape of all this: muffin's DisplayConfig is Mutter's
# interface byte for byte, a native window's resize is ASYNCHRONOUS (the same call chain read the old
# size and the new one two seconds later, which is why backend_cinnamon polls get_frame_rect and why the
# size below is compared with a tolerance), and the adjacency validator strings are in
# libmuffin.so.0.0.0 with nothing having made muffin print one yet [recon2/cinnamon 2.2, 4].

SMOKE_PHASES="xwayland busrec install windows wm proxy input display persistent root nodialog"

# gnome-terminal is the terminal cinnamon-core's `gnome-terminal | x-terminal-emulator` first
# alternative puts on the image, and it is the smoke's NATIVE Wayland client.  The pattern is a regex
# (`wdotool search` compiles it case-insensitively), and it is written to match either spelling because
# nobody has yet read what muffin reports for a native GTK toplevel: X11 gnome-terminal is
# `gnome-terminal-server`/`Gnome-terminal`, while a Wayland GTK app carries its desktop-file id, the way
# GNOME's own editor is `org.gnome.TextEditor` and not `gnome-text-editor` (gnome.sh's EDITOR_CLASS
# comment).  `gnome[-.]terminal` is both, and matches no other window on this image.
EDITOR_CLASS='gnome[-.]terminal'

#: Muffin's saved configuration -- `cinnamon-monitors.xml`, NOT monitors.xml.  The name is a string in
#: libmuffin.so.0.0.0 [recon2/cinnamon 2.2]; expanded in the GUEST's shell, like gnome.sh's $MX.
CMX='$HOME/.config/cinnamon-monitors.xml'

# That file's checksum, or empty when it is not there -- gnome.sh's mx_sum with Cinnamon's file name, and a
# helper for the reason gnome.sh has one: `$(guest "md5sum ..." | tr -d ' \r\n' || echo absent)` binds the
# `||` to `tr`, which SUCCEEDS on empty input, so a missing file compares as '' and never as 'absent'.  On a
# fresh golden there is no cinnamon-monitors.xml at all -- which is exactly when --persistent matters -- and
# the "nothing is written yet" check then failed on correct behaviour (reproduced in bash, 2026-09-09:
# expected=[absent] got=[]).  Every reader below defaults the value itself, as gnome.sh:238 does.
cmx_sum() { guest "md5sum $CMX 2>/dev/null | cut -d' ' -f1" | tr -d ' \r\n'; }

#: The X client the mixed-list check needs; started by phase_wm and killed there.
XTERM_TITLE=smokex

# The rig gate, and the first phase for that reason: without it nothing below this line is a measurement
# of ours.  Twice in CI (runs 34308982263 and 34319854037, `--deb --remove --heads 3`) this flavor ended
# 19/9 and 19/10 with ONE cause -- Xwayland 24.1.10 segfaulted as muffin brought it up, muffin treats
# that as fatal and exits, and cinnamon-session gives up; `vmctl session` still logs an active logind
# session (cinnamon-session keeps respawning) and every check downstream then read a compositor that was
# not there: `wdotool` said `no Wayland session found`, `wxrandr --print-backend` fell through to `wlr`
# with `cannot connect to the compositor`, and `wmirror --check` said `[Errno 111] Connection refused`
# against the socket file muffin had left behind.
#
# The question the phase asks is Cinnamon's own shell on the session bus (`(true, '"2"')` when it is,
# ServiceUnknown when it is not) -- the same question wdotool's detection asks (`org.Cinnamon` in
# ListNames), and the right one because the bus is systemd's and is up either way: with muffin gone the
# socket at /run/user/1000/wayland-0 is still THERE and refuses connections, so every tool in the run
# would otherwise report something true about a corpse.
#
# The golden carries Xwayland 24.1.13 now (the flavor yaml's build hook, AGENTS.md route 5), so this is
# a plain `want` and no longer a branch: the session either survives with Xwayland INSTALLED AND RUNNING
# -- which is what every X-plane check below depends on -- or the run says so here, in the first phase,
# with muffin's own last words beside it.  The move-aside-and-reboot arm this phase used to carry is
# gone with the crash it worked around: nothing in the rig moves a binary out of the way any more, and
# `/usr/bin/Xwayland` is where the package put it [goal2/recon/gl.md 3, 5, 8].
phase_xwayland() {
    local alive
    alive=$(await 30 'true' "gdbus call --session --dest org.Cinnamon --object-path /org/Cinnamon \
                             --method org.Cinnamon.Eval 'String(1+1)' 2>&1" || true)
    want "the session survives with Xwayland installed (24.1.13, the flavor yaml's build hook)" \
         "true" "$alive"
    if printf '%s\n' "$alive" | grep -q true; then
        note "org.Cinnamon answers: $(ev "$alive")"
        return 0
    fi
    note "muffin's last words: $(ev "$(root "journalctl -b --no-pager | grep -iE \
         'Fatal server error|Caught signal|Connection to xwayland lost|respawning too quickly'" \
         | tail -3 || true)")"
    # `-f='${Version}'`, quoted for the guest's own `sh -c`, the way the build hook writes it: the
    # payload reaches the guest through ssh's word-join and is parsed twice, so a bare ${Version}
    # arrives as an empty format and dpkg-query answers `error in show format: may not be empty
    # string` (rc 2) -- measured through the same chain on this box, 2026-09-12.  This is the line
    # that says whether the hold fell out of the golden, so it has to say the version.
    note "xwayland in this image: $(ev "$(root "dpkg-query -W -f='\${Version}' xwayland 2>&1" || true)")"
}

# No text editor on this golden: the "editor" is gnome-terminal running `cat >>`, which is sway.sh's
# helper and for sway.sh's reason -- `cat >` keeps its offset across editor_clear's truncation and the
# next line lands behind a run of NULs.  O_APPEND makes the write after a truncate land at offset 0.
editor_start() {
    guest "rm -f $SMOKE_FILE; setsid nohup gnome-terminal -- sh -c 'cat >> $SMOKE_FILE' \
           >/dev/null 2>&1 </dev/null & sleep 4; true" >/dev/null || true
}
editor_save()  { guest "wdotool key Return" >/dev/null || true; }
editor_clear() { guest "wdotool key ctrl+u; : > $SMOKE_FILE" >/dev/null 2>&1 || true; }

# `<pos> <w>x<h>` from win_geom, compared the way the two halves deserve: the position exactly, the size
# to within `tol` pixels on each axis.  A terminal commits whole character cells, so the size a
# compositor reports back is the client's rounding and not the tool's business; the same helper, for the
# same reason, is in wayfire.sh (measured there: `windowsize 800 600` on foot came back 798x598).
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

# Cinnamon's own view of itself, as a READ-ONLY question: `org.Cinnamon.Eval` through gdbus, which is
# the oracle for the window plane the way `window-rules/list-outputs` is Wayfire's -- what the shell
# thinks, not what our backend thinks.  Nothing here changes anything.  Whoever records this flavor has
# to put every cin_eval call below into guest-capture.sh's list: fake-vmctl answers an unrecorded
# command with empty and status 0, and the checks that read one are `same` checks that would then be red
# in selftest-offline's first pass.  The four scripts, their measured answers and the ordering rule are
# written out for batch 11 (guest-capture.sh's owner) in requests-batch-11.md.
cin_eval() {   # cin_eval <javascript> -- prints gdbus's `(true, '<json>')`
    guest "gdbus call --session --dest org.Cinnamon --object-path /org/Cinnamon \
           --method org.Cinnamon.Eval $(sq "$1")"
}

# Is the window with stable sequence <n> shaded, as CINNAMON sees it?  The id is checked to be digits
# before it goes anywhere near the script: the backend's own rule is that every interpolation into JS is
# an integer formatted with %d and no string from the compositor or the user ever enters a program
# (wdotool/cinnamon_js.py's landmine), and a helper here that broke it would be teaching the wrong thing.
cin_shaded() {   # cin_shaded <stable-sequence>
    case "$1" in ""|*[!0-9]*) echo "not-an-id"; return ;; esac
    cin_eval "String(global.display.list_windows(0).filter(w => w.get_stable_sequence() == $1)[0].is_shaded())" \
        | sed -n 's/.*"\(true\|false\)".*/\1/p' | head -1
}

# The full window phase.  Everything common.sh does, with two changes measured on the prototype backend:
# the size is a tolerance (a native window's resize is asynchronous and a terminal quantises to cells),
# and the pid is asserted to be a LIVE process because on a native Wayland window muffin's `get_pid()`
# answers -1 -- it reads _NET_WM_PID -- and only `get_client_pid()` knows [recon2/cinnamon 2.3, 4].
phase_windows() {
    guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
    editor_start
    local out
    # The pattern is single-quoted for the GUEST's shell: it is a regex with brackets in it, and an
    # unquoted `gnome[-.]terminal` is a glob the guest would try to expand against the seated user's HOME.
    out=$(await 30 '[0-9]' "wdotool search --class '$EDITOR_CLASS' | head -1" || true)
    WIN=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$WIN" ]; then fail "wdotool search --class $EDITOR_CLASS found no window [$(ev "$out")]"; return 1; fi
    pass "wdotool search --class $EDITOR_CLASS -> $WIN"
    # Ids are get_stable_sequence(): small and dense, the sway shape.  get_id() is a ~3e9 counter (two
    # windows two apart at 3,070,932,382) and is deliberately not what wwmctl prints [recon2/cinnamon 2.3].
    want "the id is a stable sequence number, not muffin's ~3e9 get_id() counter" "^[0-9]{1,6}$" "$WIN"
    want "getwindowname is not empty" "." "$(guest "wdotool getwindowname $WIN" || true)"
    guest "wdotool windowmove $WIN 100 100; wdotool windowsize $WIN 800 600" >/dev/null || true
    sleep 2
    # Measured here: `100,100 798x591` -- 2 px of cell on x, 9 on y, the terminal's own quantisation, and
    # the position exact to the pixel.  24 px covers a cell of any font this image has.
    geom_is "windowmove 100 100 + windowsize 800 600 -> getwindowgeometry" 100,100 800 600 24 "$(win_geom "$WIN")"
    local pid; pid=$(guest "wdotool getwindowpid $WIN" | tr -d ' \r\n' || true)
    if [ -n "$pid" ] && guest "test -d /proc/$pid" >/dev/null 2>&1; then
        pass "getwindowpid names a live process ($pid) for a NATIVE window, where get_pid() answers -1"
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
    # Cinnamon's workspaces are a fixed set (four on the recon's session), so the count is read and the
    # pair is only run when there are two -- common.sh's rule, kept.
    local nd; nd=$(guest 'wwmctl -d' | grep -c '^[0-9]' || true)
    if [ "${nd:-0}" -lt 2 ]; then
        note "$nd workspace(s): the set_desktop pair needs two, skipped"
    else
        guest "wdotool set_desktop 1" >/dev/null || true
        sleep 1
        same "set_desktop 1 -> get_desktop (of $nd workspaces)" "1" \
             "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
        # The shell's own answer, so that the round trip is not the backend agreeing with itself.
        want "Cinnamon's own workspace_manager agrees the active workspace is 1" "true, '1'" \
             "$(cin_eval 'global.workspace_manager.get_active_workspace_index()' || true)"
        guest "wdotool set_desktop 0" >/dev/null || true
        sleep 1
        same "set_desktop 0 -> get_desktop" "0" "$(guest 'wdotool get_desktop' | tr -d ' \n' || true)"
    fi
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    guest "wdotool mousemove 300 300" >/dev/null || true
    want "mousemove 300 300 -> getmouselocation" "x:300 y:300" "$(guest 'wdotool getmouselocation' || true)"
    local ptr; ptr=$(cin_eval 'global.get_pointer().slice(0, 2).join(",")' || true)
    same "the shell's own global.get_pointer() agrees to the pixel" "300,300" \
         "$(printf '%s\n' "$ptr" | sed -n "s/.*'\"\\([0-9]*,[0-9]*\\)\"'.*/\\1/p" | head -1)"
    local st=0
    guestq "wdotool mousemove 300 300 click 1" || st=$?
    ok "mousemove 300 300 click 1" "$st"
}

# The window-manager half.  The mixed list is the measurement this flavor was built for -- a nested
# muffin's Xwayland is dead, so no X and native window have ever been listed together here -- and
# SHADED is the state Cinnamon has and GNOME and KWin 6 do not (`shade`/`unshade`/`is_shaded` are on
# muffin's Meta.Window and _NET_WM_STATE_SHADED is in its _NET_SUPPORTED) [recon2/cinnamon 2.3, 3.1].
phase_wm() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    guest "setsid nohup xterm -T $XTERM_TITLE -e sh -c 'sleep 600' >/dev/null 2>&1 </dev/null & sleep 3; true" \
        >/dev/null || true
    local rows; rows=$(guest 'wwmctl -l -x' || true)
    # Measured: `0x00000001  0 org.gnome.Terminal.org.gnome.Terminal  cin15 Terminal` -- a Wayland GTK
    # app's WM_CLASS is its desktop-file id in both columns, so the pattern is the word they share.
    want "wwmctl -l -x lists the native terminal" "[Tt]erminal" "$rows"
    # xid comes straight from get_xwindow() -- no _NET_CLIENT_LIST matching, unlike KWin 6 -- and is 0
    # for a native toplevel.  The recon read 16777228 (0x0100000c) off an X client on this compositor,
    # and with Xwayland 24.1.13 in the golden (route 5, the flavor yaml's hook) this is the one list in
    # the rig with an X window and a native one in it -- a claim now, not a wait.
    want "the XWayland xterm is listed under its real X id with the WM_CLASS instance" \
         "^0x0[0-9a-f]{6,7} +[0-9]+ +xterm\.XTerm" "$rows"
    want "wwmctl -l -p carries the pid column" "^0x[0-9a-f]+ +[0-9]+ +[0-9]{2,} " "$(guest 'wwmctl -l -p' || true)"
    want "wwmctl -m names the window manager muffin's check window names" "^Name: Mutter \(Muffin\)$" \
         "$(guest 'wwmctl -m' || true)"
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    sleep 0.5
    local before; before=$(win_geom "$WIN")
    guest "wwmctl -r :ACTIVE: -b add,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    local maxed; maxed=$(win_geom "$WIN")
    if [ "$maxed" = "$before" ]; then fail "add,maximized_vert,maximized_horz did not change the geometry ($before)";
    else pass "add,maximized_vert,maximized_horz: $before -> $maxed"; fi
    want "wxprop -id says the window is maximized on both axes" \
         "_NET_WM_STATE_MAXIMIZED_(HORZ|VERT)" "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    guest "wwmctl -r :ACTIVE: -b remove,maximized_vert,maximized_horz" >/dev/null || true
    sleep 1.5
    same "b7a60f0: remove,maximized_vert,maximized_horz restores exactly what was there" "$before" \
         "$(win_geom "$WIN")"
    # Shading.  muffin kept what mutter dropped, so this is the one desktop in the rig where the state
    # is real on a WAYLAND session; the shell's own is_shaded() is the oracle, not our own read-back.
    guest "wwmctl -r :ACTIVE: -b add,shaded" >/dev/null || true
    sleep 1
    same "add,shaded really shades the window (muffin kept the state mutter dropped)" "true" \
         "$(cin_shaded "$WIN" || true)"
    # ...and the half that used to be missing.  Measured 2026-09-09 on this flavor: with is_shaded()
    # answering true, `wxprop -id 1 _NET_WM_STATE` printed `_NET_WM_STATE_FOCUSED` and nothing else,
    # while real xprop on a Cinnamon X11 session prints _NET_WM_STATE_SHADED there -- and X is the
    # oracle.  The arm landed (wxprop/core.py's `shaded` key, the atom table and _NET_SUPPORTED;
    # wdotool/backend_cinnamon.py reads the `shaded` field cinnamon_js.py:57 had been sending all
    # along), and muffin's own set_net_wm_state puts SHADED FIRST, before SKIP_PAGER, which is where
    # this reads it.  The oracle has been read since, on this very flavor now that it has an X plane
    # (2026-09-12): an `xterm` shaded through `wwmctl` answers real `DISPLAY=:0 xprop -id 0x120000c
    # _NET_WM_STATE` with `_NET_WM_STATE_SHADED, _NET_WM_STATE_HIDDEN`, and the clone prints those two
    # atoms in that order for the same window at the same moment.  The native terminal below adds
    # _NET_WM_STATE_FOCUSED to the pair.
    want "wxprop says the window is shaded, as xprop does under X" \
         "_NET_WM_STATE_SHADED" "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    guest "wwmctl -r :ACTIVE: -b remove,shaded" >/dev/null || true
    sleep 1
    same "remove,shaded unshades it again" "false" "$(cin_shaded "$WIN" || true)"
    want "wwmctl -d lists the workspaces" "^0 " "$(guest 'wwmctl -d' || true)"
    # Same event from the root window's side: _NET_CLIENT_LIST is an X root property, so it holds the
    # X client's own id and nothing native.  It needs an Xwayland that lives through a login, which is
    # what the 24.1.13 hold in the flavor yaml bought.
    want "wxprop -root _NET_CLIENT_LIST names the X client under its real id" \
         "window id # 0x[0-9a-f]{6}" "$(guest 'wxprop -root _NET_CLIENT_LIST' || true)"
    guest "pkill -x xterm; true" >/dev/null 2>&1 || true
}

# The layout half of phase_input.  Cinnamon keeps the live index in `org.cinnamon.desktop.input-sources`
# `current` -- a plain 0-based index into `sources`, with no `mru-sources` head to reason about, which
# is the difference from GNOME's reader -- and wdotool/xkbmap.py's CinnamonInputSources reads it through
# one Eval of Gio.Settings [recon2/cinnamon 2.2, 5].
layout_phase() {
    # The original is recorded and put back at the end: `sources` is session state, the display, persistent
    # and root phases run after this one, and `vm/live-smoke.sh --reuse` runs the whole file again on the
    # same instance.  Every measurement in this file was taken on a one-source session.
    local src0; src0=$(guest 'gsettings get org.cinnamon.desktop.input-sources sources' | tr -d '\r' || true)
    guest "gsettings set org.cinnamon.desktop.input-sources sources \"[('xkb','us'),('xkb','de')]\"" \
        >/dev/null 2>&1 || true
    guest "gsettings set org.cinnamon.desktop.input-sources current 1" >/dev/null 2>&1 || true
    # ...and the two halves those settings do NOT do, both measured on this golden 2026-09-12 on a
    # freshly booted session.  Writing them alone leaves ONE group on the wire (`wdotool keys explain`
    # reads `layout: English (US) -- group 1 of 1, from wayland`): neither csd-keyboard nor muffin 6.4
    # turns the second source into a second keymap group.  And even with two groups there, `current`
    # does not move the group muffin DECODES keystrokes under -- it stayed locked to 0 while the setting
    # said 1.  Two `org.Cinnamon.Eval` calls are what apply the layout, AGENTS.md route 2, the
    # compositor's own scripting surface (the rung hypr.sh's config apply uses):
    #
    #   set_keymap("us,de", "", "")   the second group arrives on the wire, and wdotool then reads
    #                                 `layout: German -- group 2 of 2, from wayland + cinnamon
    #                                 input-sources` -- the NAME off the keymap, the INDEX off `current`
    #   lock_layout_group(1)          muffin decodes keystrokes under that group
    #
    # muffin 6.4 exposes both setters on MetaBackend and NO getter for either (`get_keymap_layout_group`
    # -> undefined, measured), which is why these are writes the phase makes and not state it reads.
    cin_eval 'imports.gi.Meta.get_backend().set_keymap("us,de", "", ""); String(1)' >/dev/null 2>&1 || true
    sleep 2
    cin_eval 'imports.gi.Meta.get_backend().lock_layout_group(1); String(1)' >/dev/null 2>&1 || true
    sleep 3
    local ex; ex=$(guest 'wdotool keys explain --chars z' || true)
    want "keys explain reads the keymap off the wire and names the group" \
         "^layout: .* -- group [0-9]+ of [0-9]+" "$ex"
    # Measured 2026-09-09 on the Xwayland-less session: `layout: English (US) -- group 1 of 1, from
    # wayland`, and the reason offered then was that csd-keyboard never registered ("Application
    # 'cinnamon-settings-daemon-keyboard.desktop' failed to register before timeout") on a session whose
    # Xwayland had died.  RE-MEASURED 2026-09-12 with Xwayland 24.1.13 in the golden, and with a
    # /dev/uinput keyboard alive (that last part is the one every earlier reading was missing: muffin
    # advertises NO wl_seat keyboard capability on a session with no keyboard device, so `keys explain`
    # answered `layout: US (built-in table) -- group 1 of 1, from built-in` with the note `the
    # compositor's keymap could not be read (the seat has no keyboard capability)` until the first
    # `wdotool type` created one).  With a keyboard on the seat AND the `set_keymap` Eval above, wdotool
    # reads `layout: German -- group 2 of 2, from wayland + cinnamon input-sources` -- the group NAME off
    # the keymap, the group INDEX off `current`.  With the settings and no Eval, on a freshly booted
    # session, it reads `layout: English (US) -- group 1 of 1, from wayland`: the settings alone buy
    # nothing on the wire.
    #
    # What `current` does NOT do either is move the group muffin decodes keystrokes under, and that was
    # the whole of the typing gap.  Three trials on this session, same text, same sink (/dev/uinput;
    # muffin 6.4 has no virtual-keyboard protocol), reading back out of an xterm running `cat`:
    #
    #   muffin locked group 1 (de), wdotool using group 2 (German)   -> `de: yz@`   byte-exact
    #   muffin locked group 0 (us), wdotool using group 2 (German)   -> `de> zyñ`
    #   muffin locked group 0 (us), wdotool pinned WDOTOOL_XKB_GROUP=1 -> `de: yz@`  byte-exact
    #
    # So the uinput path compensates correctly -- it presses GERMAN positions (key 21 for `z`, key 44
    # for `y`, 52+shift for `:`, 16+AltGr for `@`, `wdotool keys explain --chars 'zy:@'` on this very
    # session) and the middle row is those German keycodes read back under a US group, not US positions
    # pressed.  The missing half was the lock, which the Eval above now makes.
    # tests/test_keymap.py::TypingUnderALiveGermanGroup pins the four keys off
    # tests/fixtures/keymaps/us_de.xkb.
    want "the group came from Cinnamon's input-sources" \
         "from wayland \+ cinnamon input-sources" "$ex"
    want "the second source is the live group" "group 2 of [0-9]+" "$ex"
    editor_clear
    guest "wdotool type --delay 30 -- $(sq 'de: yz@')" >/dev/null || true
    sleep 0.6; editor_save; sleep 1
    # Byte-exact WITH A LIVE GERMAN GROUP, which is the claim that was worth arranging: the keystrokes
    # are German positions chosen against the compositor's own keymap and muffin decodes them under the
    # group the Eval locked.  Before the lock the same call arrived as `de> zyñ`.
    same "wdotool type is byte-exact under the live German group" "de: yz@" "$(editor_text)"
    # Put the session back the way the two Evals found it -- the display, persistent and root phases run
    # after this one and `--reuse` runs the whole file again on the same instance.  The lock first, so
    # that nothing a later phase types is decoded under a group whose keymap has just gone.
    # muffin 6.4 has no keymap getter (`get_keymap_layout_group` -> undefined, measured), so what the
    # keymap is restored TO is read out of the one thing that does answer: the `sources` setting the
    # phase captured into $src0 before it wrote anything.  This golden answers `@a(ss) []` -- the key is
    # unset, Cinnamon falls back to the system layout, and `us` is the restore (the recorded transcript's
    # `set_keymap("us", "", "")` is this branch).  A flavor whose `sources` names its layouts restores
    # THOSE, instead of a literal that would quietly leave the session on the wrong keymap for the
    # display, persistent and root phases and for a `--reuse` rerun.
    local lay0
    lay0=$(printf '%s\n' "$src0" | grep -o "'xkb', *'[^']*'" | sed "s/^.*'\([^']*\)'$/\1/" \
           | tr '\n' ',' | sed 's/,$//')
    [ -n "$lay0" ] || lay0=us
    cin_eval 'imports.gi.Meta.get_backend().lock_layout_group(0); String(1)' >/dev/null 2>&1 || true
    cin_eval "imports.gi.Meta.get_backend().set_keymap(\"$lay0\", \"\", \"\"); String(1)" \
        >/dev/null 2>&1 || true
    guest "gsettings set org.cinnamon.desktop.input-sources current 0" >/dev/null 2>&1 || true
    # `guest` merges stderr, so the recorded value is only trusted when it looks like the GVariant list
    # gsettings prints (`[('xkb', 'us')]`); anything else -- an error line, an empty answer -- resets the
    # key instead of writing that line back into it.
    case $src0 in
        \[*\]) guest "gsettings set org.cinnamon.desktop.input-sources sources $(sq "$src0")" \
                   >/dev/null 2>&1 || true ;;
        *)     guest 'gsettings reset org.cinnamon.desktop.input-sources sources' >/dev/null 2>&1 || true ;;
    esac
    sleep 2
    # There is no --vkbd here and no privilege-free typing at all: muffin 6.4 advertises 23 globals and
    # not one of them is zwp_virtual_keyboard_manager_v1 (master, the 6.6/6.8 line, adds it -- and still
    # no virtual pointer), so every keystroke above went through /dev/uinput, which the package's udev
    # rule hands the seated user [recon2/cinnamon 2.1].
    local vk; vk=$(guest 'wdotool --vkbd on type -- x 2>&1' || true)
    want "--vkbd on says which protocol this compositor does not implement" \
         "does not implement zwp_virtual_keyboard_manager_v1" "$vk"
    # wdotool/vkbd.py's parenthesis names Muffin on both sides now -- "(Mutter, KWin and Cinnamon's
    # Muffin 6.4 do not; sway, Hyprland, the wlroots family, COSMIC and Muffin 6.6+ do)" -- which is the
    # sentence a Cinnamon user needs: 6.4 has no zwp_virtual_keyboard_manager_v1 and 6.6 does.
    want "...and names Muffin in the clause that lists who does" "Muffin|Cinnamon" "$vk"
}

# common.sh's body, plus what only Muffin's own bus can say.  The oracle is oracle.py's `muffin` branch:
# `GetCurrentState` under org.cinnamon.Muffin.DisplayConfig, the same call as GNOME's under three other
# names, whose signature is byte-for-byte Mutter's [recon2/cinnamon 2.2, 3.1].
phase_display() {
    local pair first second out st=0
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    want "wxrandr --print-backend is cinnamon and not the mutter or wlr fallback" "^cinnamon$" \
         "$(guest 'wxrandr --print-backend' | tr -d ' \r' || true)"
    out=$(guest 'wxrandr --print-backend --verbose' || true)
    want "--print-backend --verbose says the compositor is Muffin, not Mutter" "compositor: Muffin" "$out"
    want "...and names the bus interface it drives" "org\.cinnamon\.Muffin\.DisplayConfig \(D-Bus\)" "$out"
    same "warandr picks the same backend by the same name" "cinnamon" \
         "$(guest 'warandr --print-backend' | tr -d ' \r\n' || true)"
    # U25's other half landed on 2026-09-09: wxrandr/cli.py's `cinnamon` arm builds
    # `MutterOutputs(bus=<the probe's connection>, flavor=MUFFIN)` where it used to raise `the cinnamon
    # backend is named by --backend and is not built into this install`.  Measured on this flavor the same
    # day, against a live Cinnamon 6.4.13 / muffin 6.4.1:
    #
    #     wxrandr --listmonitors  ->  Monitors: 2
    #                                  0: +*Virtual-1 1920/508x1080/286+0+0  Virtual-1
    #                                  1: +Virtual-2 1920/508x1080/286+1920+0  Virtual-2
    #
    # -- the primary FIRST, which is one of the eight cli.py sites that stopped keying on the token.  The
    # other seven answered on the same session: `--gnome-overlap-status` and `--unsafe-gnome-overlap` both
    # say "this is Cinnamon, whose Meta-0 typelib has no generation to check", `--brightness` warns
    # "not supported on Muffin (no gamma LUT API)" and succeeds instead of dying on the wlr gamma path,
    # `--noprimary` says "Cinnamon requires a primary output; keeping Virtual-1", and a `--dryrun --verbose`
    # plan carries the neighbour keep_adjacent shifts (crtc 1 at +1280+0 after Virtual-1 goes to 1280x1024).
    #
    # The applies land too, which is why the gate that used to skip common_display_phase is gone.  On the
    # same session: `--output Virtual-1 --mode 1280x1024` changed the mode and warned `output Virtual-2
    # moved to +1280+0 to stay adjacent to Virtual-1`; the mode GREW back to 1920x1080 (Muffin applies
    # through DisplayConfig, so the rig's virtio-gpu grow limit -- which stops a second `keyword monitor`
    # dead on Hyprland -- does not reach here); `--output Virtual-2 --off` left one monitor and `--auto`
    # brought it back with `output Virtual-2 enabled without a position; placing it right-of Virtual-1`;
    # and a `--dryrun` ended `cinnamon verify: ok` on stderr.
    #
    # These were taken with the tree's zipapps copied in by hand on a session with /usr/bin/Xwayland moved
    # aside, because that is the only way this flavor has a session at all on this rig (see the header).
    want "--listmonitors answers through the MUFFIN flavour (U25)" \
         "Monitors: [0-9]+" "$(guest 'wxrandr --listmonitors 2>&1' || true)"
    common_display_phase
    # wmirror: muffin publishes neither capture protocol, so the refusal has to be about the COMPOSITOR
    # (the helper is installed on this image precisely so that it cannot be about the helper).  Mirroring
    # here is NOT YET, and the route is written down rather than left as a "no": route 4, a ScreenCast
    # portal backend on Cinnamon (whether xdg-desktop-portal-gtk/-xapp serves ScreenCast under muffin is
    # unmeasured -- nobody has asked one), and failing that route 3, a Cinnamon extension handing frames
    # out over Eval/D-Bus.  Either costs a second capture path in wmirror/core.py beside wl-mirror.
    st=0
    out=$(guest 'wmirror --check 2>&1') || st=$?
    same "wmirror --check exits 1 here" "1" "$st"
    want "--check names both capture protocols that are missing" \
         "advertises neither zwlr_screencopy_manager_v1 nor ext_image_copy_capture_manager_v1" "$out"
    # `helper: +/` was the check here until the review: it PASSED on the session whose compositor was dead
    # (the first run's log line 76), because all it read was that SOME path had been printed.  What the claim
    # is worth asserting for is the absence of the missing-helper story -- `wl-mirror is not installed (no
    # `wl-mirror` on PATH)` and core.install_hint()'s apt line, which is what wl-mirror being in DESKTOP_PKG
    # is there to keep out of this output.
    wantnot "...and it is the compositor that is named, not a missing wl-mirror" \
            "not installed|apt(-get)? install|dnf install|pacman -S" "$out"
    [ -n "$second" ] || return 0
    # Muffin carries Mutter's validator verbatim -- the strings `Logical monitors not adjacent`,
    # `Logical monitors overlap` and `Logical monitor scales must be identical` are all in
    # libmuffin.so.0.0.0 -- but the recon had no KMS, so no muffin has ever been made to print one.
    out=$(guest "wxrandr --output $second --pos 4000x0 2>&1") || true
    # It has been run now, on three heads: Muffin really does raise Mutter's own string.  This was an
    # xwant waiting for exactly that run.
    want "a gap is refused in Muffin's own words" "not adjacent" "$out"
    guest "wxrandr --output $second --right-of $first" >/dev/null || true
}

# --persistent, and the file that is NOT called monitors.xml.  Cinnamon has GNOME's confirmation dialog
# (`Keep these display settings?` / `Keep changes` in usr/share/cinnamon/js/ui/windowManager.js) and the
# same 20-second countdown, so the first three checks are gnome.sh's with Cinnamon's file name.  What
# has no counterpart is the ANSWER: there is no bridge here and no D-Bus method for the dialog, and the
# route that should work -- Eval into the same complete_display_change() the dialog's own button calls
# (AGENTS.md route 2) -- has never been run against a live dialog, so it is an xwant and not a claim.
phase_persistent() {
    local pair first second before after now out pos0 st=0
    # The gate phase_display's own --listmonitors check answers first: if the backend cannot even list
    # the monitors, `--persistent` never reaches an apply and every check below would be red for that
    # one reason rather than for its own.  It has not fired since U25 landed (2026-09-09) and did not
    # fire on the 2026-09-12 run either; it stays because a backend that stops answering should report
    # itself in one line and not in eight.
    if ! guest 'wxrandr --listmonitors 2>&1' | grep -q '^Monitors:'; then
        note "--persistent skipped: wxrandr --listmonitors answered no monitor list on this session;"
        note "  phase_display's own --listmonitors check is the line that says what happened"
        return 0
    fi
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    [ -n "$second" ] || { note "one head: --persistent needs two"; return 0; }
    guest "wxrandr --output $second --right-of $first" >/dev/null || true
    sleep 2
    pos0=$(opos "$second")
    note "$second starts at $pos0"
    before=$(cmx_sum)
    note "cinnamon-monitors.xml before: ${before:-absent}"
    out=$(guest "wxrandr --output $second --below $first --persistent 2>&1") || st=$?
    # Cinnamon's own sentence and not GNOME's: wxrandr/mutter.py:112 gives the MUFFIN flavour
    # `keep_dialog="Keep these display settings?"`, quoted off usr/share/cinnamon/js/ui/windowManager.js,
    # where GNOME's is `Keep changes?`.  This line was gnome.sh's regex and it failed on the right answer
    # -- measured on the live session, 2026-09-09: `xrandr: Cinnamon will ask "Keep these display
    # settings?" for 20 s; confirm the dialog or the layout reverts`.
    want "--persistent prints the Keep-changes warning in Cinnamon's own words" \
         'Keep these display settings\?' "$out"
    now=$(cmx_sum)
    same "nothing is written before the dialog is answered" "${before:-absent}" "${now:-absent}"
    sleep 25
    same "the layout reverts on its own after the 20 s countdown" "$pos0" "$(opos "$second")"
    guest "wxrandr --output $second --below $first --persistent >/dev/null 2>&1 &" >/dev/null || true
    sleep 4
    local keep; keep=$(cin_eval 'String(global.window_manager.complete_display_change(true))' || true)
    note "answering the dialog through Eval: $(ev "$keep")"
    sleep 3
    after=$(cmx_sum)
    # Measured, 2026-09-09, on a three-head session: `global.window_manager.complete_display_change(true)`
    # answers `(true, '"undefined"')` -- the JS function returns nothing -- and cinnamon-monitors.xml,
    # which was `absent` before the apply, is written.  So that IS the Eval spelling of the dialog's Keep
    # button, and the line is a `want`.
    want "keeping the change writes cinnamon-monitors.xml" "^[0-9a-f]{32}$" "$after"
    same "whatever the dialog did, monitors.xml is not the file this desktop writes" "absent" \
         "$(guest 'test -f $HOME/.config/monitors.xml && echo present || echo absent' | tr -d ' \r\n' || true)"
    guest "wxrandr --output $second --right-of $first" >/dev/null || true
}
