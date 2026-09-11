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
# 1920x1080 heads), and the one thing it found before any check ran: **Xwayland takes the session down**.
# Cinnamon starts completely -- `Cinnamon took 2888 ms to start`, both monitors seen, applets loaded,
# /run/user/1000/wayland-0 listening -- and then Xwayland 24.1.10 dies with `Fatal server error: Caught
# signal 11 (Segmentation fault)` after `Xwayland glamor: GBM Wayland interfaces not available` /
# `Failed to initialize glamor, falling back to sw`; muffin treats that as fatal (`mutter-WARNING:
# Connection to xwayland lost`), exits, and cinnamon-session gives up with `App 'cinnamon-wayland.desktop'
# respawning too quickly`.  The whole smoke then reports "no Wayland session found" -- 11 pass, 10 fail.
# Not a nesting artefact: the recon saw the same crash under `muffin --wayland --nested` and this is a
# real KMS guest.  Forcing software GL (LIBGL_ALWAYS_SOFTWARE=1, GALLIUM_DRIVER=llvmpipe in
# /etc/environment) changes nothing, measured; muffin 6.4 has no glamor switch to turn off (`strings
# libmuffin.so.0` lists MUTTER_DEBUG_*, XWAYLAND_STFU and _XWAYLAND_ALLOW_COMMITS and no
# XWAYLAND_NO_GLAMOR).  With /usr/bin/Xwayland moved aside the session comes up and STAYS up, and every
# measurement below was taken on that session -- so the route out is a rig one first: a GL-capable
# virtio-vga (virtio-vga-gl/virgl) in vm/vmctl, and failing that AGENTS.md route 5, an Xwayland patched
# not to die on the sw path.  Until then the X plane is missing here: `wwmctl -l` lists native windows
# only, cinnamon-settings-daemon's X-dependent components do not register, and the mixed list stays
# unmeasured.
#
# Measured on that session with the tree's zipapps and the 0.4.0 deb installed (this is what the checks
# below assert, byte for byte):
#
#     wxrandr --print-backend                 cinnamon
#     wxrandr --print-backend --verbose       compositor: Muffin / protocol: org.cinnamon.Muffin.
#                                             DisplayConfig (D-Bus) / available: yes
#     warandr --print-backend                 cinnamon
#     wxrandr --listmonitors                  REFUSED: `xrandr: the cinnamon backend is named by
#                                             --backend and is not built into this install` (wxrandr/
#                                             cli.py still raises for the flavour mutter.py has)
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
#     -b add,shaded                           is_shaded() true, then false again -- and wxprop says
#                                             nothing about it (see the xwant in phase_wm)
#     wdotool type / key Return               byte-exact through /dev/uinput with the package's udev rule
#     mousemove 300 300 / getmouselocation    x:300 y:300 screen:0 window:1, and Cinnamon's own
#                                             global.get_pointer() answered "300,300"
#     wmirror --check                         the two-protocol refusal, rc 1
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

# Is Cinnamon's own shell answering on the session bus?  `(true, '"2"')` when it is; the bus itself is
# systemd's and is up either way, so a dead compositor answers ServiceUnknown and never the word `true`.
# This is the same question wdotool's detection asks (`org.Cinnamon` in ListNames) and the reason it is
# the gate below: with muffin gone the socket at /run/user/1000/wayland-0 is still THERE and refuses
# connections, so every tool in the run reports something true about a corpse.
cin_alive() { guest "gdbus call --session --dest org.Cinnamon --object-path /org/Cinnamon \
                     --method org.Cinnamon.Eval 'String(1+1)' 2>&1"; }

# The rig gate, and the first phase for that reason: without it nothing below this line is a measurement
# of ours.  Twice in CI (runs 34308982263 and 34319854037, `--deb --remove --heads 3`) this flavor ended
# 19/9 and 19/10 with ONE cause -- Xwayland 24.1.10 segfaults as muffin brings it up, muffin treats that
# as fatal and exits, and cinnamon-session gives up; `vmctl session` still logs an active logind session
# (cinnamon-session keeps respawning) and every check downstream then reads a compositor that is not
# there: `wdotool` said `no Wayland session found`, `wxrandr --print-backend` fell through to `wlr` with
# `cannot connect to the compositor`, and `wmirror --check` said `[Errno 111] Connection refused` against
# the socket file muffin left behind.  The header records the same crash from this flavor's first run.
#
# So the phase asks the question first and says which world the rest of the run is in.  With Xwayland
# moved aside the session comes up and stays up -- that is where every byte in the header's table was
# measured -- and the X plane is missing, which the checks that need it already carry as xwants naming
# their route.  Moving the binary is the rig's own lowest rung and not a route on AGENTS.md's ladder at
# all: nothing of ours is being worked around here, and the day the rig has a render node the xwant below
# goes XPASS and the move stops happening.
#
# What the recording under tests/fixtures/live/ carries is the --reuse branch only -- `test -x
# /usr/bin/Xwayland` -> no, then the `want` on the bus -- because capture-from-run ran against the
# instance this phase had already fixed.  The crash branch (the survival xwant, the mv, the reboot)
# is the live run's alone and cannot be replayed at all, for the reason no recording carries
# `install`: a reboot is not a guest command with an answer.  It was measured on 2026-09-09 and the
# run log of that measurement is what the report cites.
phase_xwayland() {
    local alive present
    alive=$(await 30 'true' "gdbus call --session --dest org.Cinnamon --object-path /org/Cinnamon \
                             --method org.Cinnamon.Eval 'String(1+1)' 2>&1" || true)
    # Whether the binary is still there decides which claim this phase can make.  On a --reuse run of an
    # instance this phase has already moved it aside, "the session is up" says nothing about Xwayland and
    # the xwant below would XPASS on the wrong evidence; so that case is a plain `want` on the session and
    # a note saying why the other line is not being asked.
    present=$(root 'test -x /usr/bin/Xwayland && echo yes || echo no' | tr -d ' \r\n' || true)
    if [ "$present" = no ]; then
        note "/usr/bin/Xwayland is already aside in this instance (an earlier run of this phase moved it)"
        want "Cinnamon answers on the session bus with Xwayland moved aside" "true" "$alive"
        return 0
    fi
    xwant "the session survives with Xwayland installed (until this rig has a render node: virtio-vga-gl \
with -display dbus,gl=on, which this host refuses with 'egl: no drm render node available' and no /dev/dri; \
else AGENTS.md route 5, an Xwayland that does not die on the software path)" \
          "true" "$alive"
    if printf '%s\n' "$alive" | grep -q true; then
        note "org.Cinnamon answers: $(ev "$alive") -- nothing was moved aside"
        return 0
    fi
    note "muffin's last words: $(ev "$(root "journalctl -b --no-pager | grep -iE \
         'Fatal server error|Caught signal|Connection to xwayland lost|respawning too quickly'" \
         | tail -3 || true)")"
    root "test -x /usr/bin/Xwayland && mv /usr/bin/Xwayland /usr/bin/Xwayland.moved-aside; true" >/dev/null
    step "Xwayland moved aside; rebooting into a session that can hold a compositor"
    root "( sleep 1; reboot ) >/dev/null 2>&1 &" >/dev/null 2>&1 || true
    sleep 8
    wait_session >/dev/null || { fail "no session after the Xwayland reboot"; return 1; }
    after_reboot
    want "with /usr/bin/Xwayland moved aside Cinnamon answers on the session bus" "true" "$(cin_alive)"
    note "cinnamon's own processes now: $(ev "$(root 'ps -o comm= -u test | sort -u' | tr '\n' ' ' || true)")"
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
    # for a native toplevel.  The recon read 16777228 (0x0100000c) off an X client on this compositor.
    # No session on this rig has had a living Xwayland yet (the header), so nobody has seen an X window and
    # a native one in one list here: this is the check that measurement will fill in, not a claim.
    xwant "the XWayland xterm is listed under its real X id with the WM_CLASS instance (until Xwayland \
survives on this rig: virtio-vga-gl in vm/vmctl, else AGENTS.md route 5)" \
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
    # ...and the half of it that does not work.  Measured 2026-09-09 on this flavor: with is_shaded()
    # answering true, `wxprop -id 1 _NET_WM_STATE` printed `_NET_WM_STATE_FOCUSED` and nothing else.
    # Real xprop on a Cinnamon X11 session prints _NET_WM_STATE_SHADED there, and X is the oracle:
    # wxprop/core.py's _props() has no `shaded` arm at all and its atom table (core.py:328-338) has no
    # _NET_WM_STATE_SHADED to name.  Requested of the Cinnamon batch in requests-batch-7.md.
    xwant "wxprop says the window is shaded, as xprop does under X (fix wxprop/core.py's shaded arm, \
unowned this wave: requests-batch-7.md)" \
          "_NET_WM_STATE_SHADED" "$(guest "wxprop -id $WIN _NET_WM_STATE" || true)"
    guest "wwmctl -r :ACTIVE: -b remove,shaded" >/dev/null || true
    sleep 1
    same "remove,shaded unshades it again" "false" "$(cin_shaded "$WIN" || true)"
    want "wwmctl -d lists the workspaces" "^0 " "$(guest 'wwmctl -d' || true)"
    # Same event: _NET_CLIENT_LIST is a property of the X root window, and there is no X plane to hold one
    # until Xwayland lives through a login here.
    xwant "wxprop -root _NET_CLIENT_LIST names the X client under its real id (until Xwayland survives on \
this rig: virtio-vga-gl in vm/vmctl, else AGENTS.md route 5)" "window id # 0x[0-9a-f]{6}" \
          "$(guest 'wxprop -root _NET_CLIENT_LIST' || true)"
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
    sleep 3
    local ex; ex=$(guest 'wdotool keys explain --chars z' || true)
    want "keys explain reads the keymap off the wire and names the group" \
         "^layout: .* -- group [0-9]+ of [0-9]+" "$ex"
    # Measured 2026-09-09 on the Xwayland-less session: `layout: English (US) -- group 1 of 1, from
    # wayland`.  `current` really was uint32 1 and `sources` really had two entries -- but the keymap on
    # the wire still had ONE group, because it is cinnamon-settings-daemon-keyboard that applies the
    # sources to the compositor, and on a session whose Xwayland died it never registers ("Application
    # 'cinnamon-settings-daemon-keyboard.desktop' failed to register before timeout").  With one group
    # choose_group is CERTAIN and the reader is never asked, exactly as on wayfire.sh's single-layout ini.
    xwant "the group came from Cinnamon's input-sources (until csd-keyboard applies the second source)" \
          "from wayland \+ cinnamon input-sources" "$ex"
    xwant "the second source is the live group (until csd-keyboard runs: it needs a session Xwayland)" \
          "group 2 of [0-9]+" "$ex"
    editor_clear
    guest "wdotool type --delay 30 -- $(sq 'de: yz@')" >/dev/null || true
    sleep 0.6; editor_save; sleep 1
    # Byte-exact either way; with one group on the wire this is the uinput path and not yet the
    # compensation the second layout would ask for (measured: `de: yz@` arrived exactly, group 1 of 1).
    same "wdotool type is byte-exact with the second source selected" "de: yz@" "$(editor_text)"
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
    xwant "...and names Muffin in the clause that lists who does (until wdotool/vkbd.py's parenthesis names it)" \
          "Muffin|Cinnamon" "$vk"
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
    # The same gate phase_display carries, and for the same reason: with cli.py's cinnamon arm refusing,
    # `--persistent` never reaches an apply and every check below is red for that one reason (fix U25).
    if ! guest 'wxrandr --listmonitors 2>&1' | grep -q '^Monitors:'; then
        note "--persistent skipped: wxrandr/cli.py's cinnamon arm refuses before any apply (fix U25);"
        note "  phase_display's xwant is the line that says when this comes back"
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
