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
# /etc/plasmalogin.conf template and never run [recon2/fedora 5], so that half
# is an `xwant` until the first fedora44-kde golden builds and says XPASS.
phase_dm() {
    local svc
    svc=$(guest 'loginctl show-session "${XDG_SESSION_ID:-auto}" -p Service --value 2>/dev/null' \
            | tr -d ' \r' || true)
    note "logind says this session's Service is '${svc:-<empty>}'"
    if [ "$DISTRO" = fedora ]; then
        xwant "plasma-login-manager seated the session (until fedora44-kde builds once)" \
              "^plasmalogin" "$svc"
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
    if [ "$(guest 'command -v xterm' | wc -l)" -gt 0 ]; then
        guest "setsid nohup xterm -T fwtwin >/dev/null 2>&1 </dev/null &
               setsid nohup xterm -T fwtwin >/dev/null 2>&1 </dev/null & sleep 4; true" >/dev/null || true
        local ids; ids=$(guest "wdotool search --name '^fwtwin$'" | grep -E '^[0-9]+$' || true)
        local n; n=$(printf '%s\n' "$ids" | grep -c .)
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
