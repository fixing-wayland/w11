# live-smoke.d/kde.sh -- Plasma 6.6 on Wayland (KWin 6.6.6), resolute-kde.
#
# Measured 2026-09-08, package route, nothing else installed: wdotool
# search/move/size/activate/getactivewindow, wwmctl -l/-d and the maximize pair
# (restore exact), wxprop WM_CLASS and _NET_WM_STATE, type, mousemove+click all
# correct; wxrandr --off/--auto prints KWin's "applies and saves this layout
# immediately" notice plus the exact restore command line; the live-layout read
# reports "group 2 of 2, from wayland + kwin" and types
# `[1] yz@ Straße | [0] yz@ Strae |` -- ß skipped on US with the documented
# warning, byte-exact, under ONE running daemon.
#
# There is no bridge, no overlap route and no monitors.xml here, so those phases
# are simply not in the list; `kwin` is this desktop's own extra phase (T60).

# `dm` is the one phase that is here for a distro rather than for Plasma:
# Fedora 44 replaced SDDM with plasma-login-manager in every KDE variant, and
# vm/build-image.sh's dm_plasma writes one autologin file or the other by which
# one the packages created.  Which one actually seated the session is a thing
# only the guest can say.
SMOKE_PHASES="busrec install dm windows wm proxy input display kwin root nodialog"
EDITOR_CLASS=kate

# The greeter that logged the session in, from logind's own record of it.
# The regex is a PREFIX and not `^sddm$` on purpose: what was measured is
# `Service=sddm-autologin` on the SDDM-autologin guest of the openbox recon
# [recon2/openbox 97, `loginctl show-session 1` -> Service=sddm-autologin
# Type=x11], because autologin's PAM service carries the suffix.  No live-smoke
# log in vm/live-smoke.out/ carries a Service line at all -- the three noble-kde
# runs predate this phase -- so Ubuntu's Plasma being seated by sddm is a `want`
# on that recon and on nothing else.  Fedora 44 is seated by plasmalogin.service
# instead, whose autologin keys were read out of the plasma-login-manager rpm's
# /etc/plasmalogin.conf template [recon2/fedora 5].  That half was an `xwant`
# until the golden built: the first fedora44-kde rig run says
# `logind says this session's Service is 'plasmalogin-autologin'` and XPASS
# [M goal2/ci/rig-fedora44-kde.log:543-544, 2026-09-11], so it is a plain
# `want` now -- the same prefix rule as sddm's, because autologin's PAM
# service carries the suffix on both.
phase_dm() {
    local svc
    svc=$(guest 'loginctl show-session "${XDG_SESSION_ID:-auto}" -p Service --value 2>/dev/null' \
            | tr -d ' \r' || true)
    note "logind says this session's Service is '${svc:-<empty>}'"
    if [ "$DISTRO" = fedora ]; then
        want "plasma-login-manager seated the session" "^plasmalogin" "$svc"
    else
        want "sddm seated the session" "^sddm" "$svc"
    fi
}

# Kate needs a document or it opens on nothing and swallows the typing: `-n`
# forces a new instance so a second start does not attach to the first.
editor_start() {
    guest "setsid nohup kate -n $SMOKE_FILE >/dev/null 2>&1 </dev/null & sleep 5; true" >/dev/null || true
}
editor_save() { guest "wdotool key ctrl+s" >/dev/null || true; }
set_scale2()  { guest "kscreen-doctor output.1.scale.2 >/dev/null 2>&1; sleep 3; true" >/dev/null || true; }

# A stock Plasma 6.6 session ships ONE virtual desktop, and KWin's
# setCurrentDesktop refuses an index past the count (backend_kwin.py:640-644
# raises "desktop 1 does not exist").  Nothing in the measurement exercised
# set_desktop on Plasma, so the second desktop is MADE here -- through the tool's
# own -n, which is KWin's createDesktop -- and taken away again afterwards, so
# the session the later phases measure is the one the image ships.
pre_desktop_pair() {
    local st=0
    guestq "wwmctl -n 2" || st=$?
    ok "wwmctl -n 2 creates the second virtual desktop KWin does not ship" "$st"
    sleep 1
}
post_desktop_pair() { guest "wwmctl -n 1" >/dev/null 2>&1 || true; }

# KWin's own layout switch.  kxkbrc alone is not enough: the LayoutList has to
# CHANGE for KWin to reload it, so it is bounced through us,gb first, and the
# active one is then chosen over D-Bus.  (Measured; a plain write of the list it
# already has leaves KWin on the old keymap.)
layout_phase() {
    local major; major=$(plasma_major)
    local setl
    if [ "${major:-6}" -ge 6 ]; then
        guest "kwriteconfig6 --notify --file kxkbrc --group Layout --key LayoutList us,gb" >/dev/null || true
        sleep 1
        guest "kwriteconfig6 --notify --file kxkbrc --group Layout --key LayoutList us,de" >/dev/null || true
        guest "kwriteconfig6 --notify --file kxkbrc --group Layout --key Use true" >/dev/null || true
        sleep 2
        setl="gdbus call --session --dest org.kde.KWin --object-path /Layouts"
    else
        # Plasma 5.27 (measured 2026-09-08 on noble-kde): kwriteconfig5 has no
        # --notify, KWin 5 exports no /Layouts on org.kde.KWin (the object is
        # org.kde.keyboard's), and kxkbrc is read at login, so the list is
        # written plainly and the session restarted before the switch.
        guest "kwriteconfig5 --file kxkbrc --group Layout --key Use true" >/dev/null || true
        guest "kwriteconfig5 --file kxkbrc --group Layout --key LayoutList us,de" >/dev/null || true
        note "Plasma $major: kxkbrc is read at login, rebooting the guest for the two-layout session"
        root "( sleep 1; reboot ) >/dev/null 2>&1 &" >/dev/null 2>&1 || true
        sleep 8
        wait_session >/dev/null || { fail "no session after the layout reboot"; return 1; }
        sleep 15
        after_reboot
        guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
        editor_start
        local out; out=$(await 60 '[0-9]' "wdotool search --class $EDITOR_CLASS | head -1" || true)
        WIN=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
        [ -n "$WIN" ] || { fail "no editor window after the layout reboot [$(ev "$out")]"; return 1; }
        guest "wdotool windowactivate --sync $WIN" >/dev/null || true
        setl="gdbus call --session --dest org.kde.keyboard --object-path /Layouts"
    fi
    setl="$setl --method org.kde.KeyboardLayouts.setLayout"
    guest "$setl 1" >/dev/null || true
    sleep 2
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    want "keys explain names the second of two groups and kwin as the source" \
         "group 2 of 2" "$(guest 'wdotool keys explain yz@ 2>&1' || true)"
    want "the source is the compositor, not a guess" "kwin" "$(guest 'wdotool keys explain yz@ 2>&1' || true)"
    same "German types byte-exact under KWin" "yz@ Straße" "$(type_and_read 'yz@ Straße')"
    guest "$setl 0" >/dev/null || true
    sleep 2
    # ß has no key on the US layout: the tool types what it can and says so.
    same "the same daemon follows the switch back to US (ß skipped)" "yz@ Strae" "$(type_and_read 'yz@ Straße')"
    guest "$setl 1" >/dev/null || true
}

# T60: the KWin display backend on the real thing.
phase_kwin() {
    local out first second
    out=$(guest 'wxrandr --backends 2>&1' || true)
    # `unavailable` contains `available`, so the space before it is the check:
    # the line is `* kwin    available    kde_output_management_v2 version N`.
    want "--backends lists kwin as available" "kwin[[:space:]]+available" "$out"
    out=$(guest 'wxrandr --print-backend --verbose 2>&1' || true)
    want "--print-backend names kwin" "kwin" "$out"
    # The probe names the WRITE side it bound and the version the compositor
    # advertised: `protocol: kde_output_management_v2 version 4` on 6.6.  The
    # read side (kde_output_device_v2, and on 6.7 the registry that hands the
    # devices out) is what --query below exercises.
    want "--print-backend --verbose names the protocol it bound" "kde_output_management_v2" "$out"
    want "--print-backend --verbose names the interface version" "version [0-9]+" "$out"
    # The oracle is kscreen-doctor, never wxrandr's own --query; the pair is the
    # rightmost head and its neighbour (common.sh's display_pair), so that a
    # three-head instance is not asked to move its middle output.
    local pair; pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    local q; q=$(guest 'wxrandr --query 2>&1' || true)
    local o; for o in $(oracle_outputs | awk '{print $1}'); do
        want "--query names $o, as kscreen-doctor does" "^$o connected" "$q"
    done
    [ -n "$second" ] || { note "one head: the placement half of T60 needs two"; return 0; }
    out=$(guest "wxrandr --output $second --off 2>&1" || true)
    want "KWin's save notice is relayed, not invented" "applies and saves this layout immediately" "$out"
    want "the notice carries the exact restore command line" "wxrandr .*--output $second" "$out"
    sleep 2
    guest "wxrandr --output $second --auto" >/dev/null || true
    sleep 2
    guest "wxrandr --output $second --right-of $first" >/dev/null || true
    sleep 2
    # Edge to edge per kscreen-doctor: $second's x is $first's width (at scale
    # 1.4 the logical size is the enclosing integer, which is why this is read
    # from the compositor and not computed here).
    want "--right-of lands $second edge to edge per kscreen-doctor" "^$second [1-9][0-9]*,0$" \
         "$(oracle_outputs || true)"
    guest "wxrandr --output $second --same-as $first" >/dev/null || true
    sleep 2
    if guest 'test -e ~/.config/kwinoutputconfig.json'; then
        want "--same-as is recorded as a replicationSource in kwinoutputconfig.json" "replicationSource" \
             "$(guest 'cat ~/.config/kwinoutputconfig.json 2>&1' || true)"
    else
        note "(no kwinoutputconfig.json: that file is Plasma 6's; 5.27 keeps its layouts under ~/.local/share/kscreen)"
    fi
    # ...and the mirror has to END here, or every later phase measures a session
    # with one output missing from the layout: a replica has no wl_output and is
    # not in kde_output_order_v1 at all (kwin.py:120-134), and --query reports it
    # at its SOURCE's position.  Positioning it again is what clears the
    # replication source (kwin.py:863-866, :998-1002), so kscreen-doctor putting
    # the two at different places is the proof that it did.
    guest "wxrandr --output $second --right-of $first" >/dev/null || true
    sleep 2
    beside right "$first" "$second" "positioning the replica again ends the mirror"
    # F4.1 live: two same-title xterms on Xwayland, moved by X id.
    #
    # `-e sh -c 'sleep 600'` and not a bare xterm: an interactive shell REWRITES the
    # title `-T` set.  Measured on the arch-kde golden (Plasma 6.7.5 / KWin 6.7.5,
    # 2026-09-11): two `xterm -T fwtwin` came up titled `test@kde-b6:~` -- Arch's
    # /etc/bash.bashrc writes the xterm title out of PS1 -- so `wdotool search --name
    # '^fwtwin$'` found 0 AND the real `xdotool search --name fwtwin` found 0, on the
    # same session in the same second.  The clone agreed with X; the check was asking
    # for a title nothing on that desktop carried.  With the shell out of the way the
    # very same session answers 2, `wwmctl -lpx` lists both twins under the X ids
    # `xprop -root _NET_CLIENT_LIST` names (0x00e00012 and 0x01000012), and `-i -r
    # <xid> -e` moves the second alone (758,367 484x344 -> 700,120 600x428, the first
    # left at 718,345).  hypr.sh's X client has carried the `-e` since it was written,
    # which is why arch-hypr's XWayland id join passed in the same CI run this failed.
    #
    # Only arch-kde and fedora44-kde carry an X client at all -- noble-kde,
    # resolute-kde and stonking-kde print the note at the bottom of this block
    # instead [M vm/live-smoke.out/noble-kde-20260908-100601.log:111] -- so the
    # 2026-09-11 rig run was the first execution of this pair anywhere, and both of
    # its failures were this one bug.  Nothing under wdotool/ had to change for it:
    # the 6.7.5 join is measured working, and is a fixture in
    # tests/test_backend_kwin.py (KWIN_675_RAW / KWIN_675_CLIENTS) as of this run.
    if [ "$(guest 'command -v xterm' | wc -l)" -gt 0 ]; then
        guest "setsid nohup xterm -T fwtwin -e sh -c 'sleep 600' >/dev/null 2>&1 </dev/null &
               setsid nohup xterm -T fwtwin -e sh -c 'sleep 600' >/dev/null 2>&1 </dev/null & sleep 1; true" \
            >/dev/null || true
        # On this golden Xwayland is already up when the session seats (pid 570 on
        # :1, `-rootless`, before any X client -- measured 2026-09-11 on arch-kde);
        # a window is listed once KWin has mapped and managed it.  So this waits for
        # the pair rather than sleeping at it: 4 s was enough on this guest, a loaded
        # CI runner is not this guest, and a session configured to start Xwayland on
        # demand instead pays for the server's own startup on top of the mapping.
        await 30 '^2$' "wdotool search --name '^fwtwin\$' | grep -cE '^[0-9]+\$'" >/dev/null || true
        local ids; ids=$(guest "wdotool search --name '^fwtwin$'" | grep -E '^[0-9]+$' || true)
        local n; n=$(printf '%s\n' "$ids" | grep -c .)
        # A count that is not 2 is three different bugs (no X plane, an X client that
        # died, a title that is not the one asked for) and the run used to say which
        # one it was nowhere.  X is the oracle for all three.
        if [ "$n" != 2 ]; then
            local tq disp xw cl titles said
            tq='for w in $(xdotool search --class xterm 2>/dev/null); do'
            tq="$tq"' xdotool getwindowname $w; done'
            disp=$(guest 'echo $DISPLAY')
            xw=$(guest 'pgrep -a Xwayland | head -1')
            cl=$(guest 'xprop -root _NET_CLIENT_LIST 2>&1')
            titles=$(guest "$tq")
            said=$(guest "wdotool search --name '^fwtwin\$' 2>&1")
            note "DISPLAY=[$disp] Xwayland=[$(ev "$xw")]"
            note "the X root's own client list: [$(ev "$cl")]"
            note "the titles X has for them: [$(ev "$titles")]"
            note "and wdotool said: [$(ev "$said")]"
        fi
        same "two windows with the same title are two windows to the tools" "2" "$n"
        local id2; id2=$(printf '%s\n' "$ids" | sed -n 2p)
        local before after
        before=$(win_geom "$id2")
        guest "wwmctl -i -r $id2 -e 0,700,120,600,400" >/dev/null || true
        sleep 1.5
        after=$(win_geom "$id2")
        if [ "$before" != "$after" ]; then pass "F4.1: wwmctl -i -r <xid> -e moved the second twin ($before -> $after)"
        else fail "F4.1: -i -r <xid> -e did not move the window it names ($before)"; fi
        guest "wdotool search --name '^fwtwin\$' | while read w; do wdotool windowkill \$w; done; pkill xterm; true" \
            >/dev/null 2>&1 || true
    else
        note "no xterm on this golden: the same-title Xwayland twins (F4.1) need one"
    fi
}
