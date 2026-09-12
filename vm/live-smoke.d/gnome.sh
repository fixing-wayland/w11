# live-smoke.d/gnome.sh -- GNOME Shell 46.0 / 50.1 / 51.beta.
#
# The oracle for every assertion in here is the hand smoke of 2026-09-08 on
# noble-gnome, resolute-gnome and stonking-gnome: bridge v3 ACTIVE after one
# reboot (packaging/common/enable-bridge enabled it ~12 s after the session came up),
# every window/desktop/input/display operation correct, the maximize pair
# restoring to exactly 100,100 800x600 on all three (b7a60f0), `wdotool type
# 'de: yz@ Straße'` byte-exact under a Super+Space switch, Mutter refusing a
# gapped layout, --persistent reverting after 20 s unless the bridge's
# ConfirmDisplayChange(true) answers the dialog, and the overlap route applying
# on 46.0 with six green checks.
#
# It also runs on fedora44-gnome (GNOME Shell 50.4 on mutter 50.4), on
# fedora43-gnome (GNOME Shell 49.9 on mutter 49.7, libmutter-17) and on
# arch-gnome.  One fact differs from Ubuntu and is handled below rather than in a
# second file: GDM's config and state live under /etc/gdm and /var/lib/gdm
# instead of gdm3 (measured on Fedora 44 and on Arch [recon2/fedora 5,
# recon2/arch 5]).  The overlap route is no longer one of them: every generation
# these flavors run is in gnome/w11-overlap@w11/generations.json --
# 46, 49, 50 and 51 -- and 49 is the one added by measurement on the
# fedora43-gnome golden on 2026-09-11 (MetaMonitorsConfig 80 bytes out of that
# build's own GType registry, three tail slots proved by the sentinel, the
# shared 920 columns of two heads byte-identical, sha256 ecf41c1210e8c94d).

# `remove` is deliberately not in this list: it destroys the installation every
# other phase measures, so live-smoke.sh runs it last and only under --remove.
SMOKE_PHASES="busrec install bridge windows wm proxy input five display persistent overlap enablebridge udev"
SMOKE_PHASES="$SMOKE_PHASES root nodialog"
EDITOR_CLASS=TextEditor          # WM_CLASS is org.gnome.TextEditor, exactly as
                                 # under X: `--class gnome-text-editor` matching
                                 # nothing is not a bug and is not asserted here.
BRIDGE_UUID=w11-bridge@w11
OVERLAP_UUID=w11-overlap@w11
BR='--session -d org.w11.Bridge -o /org/w11/Bridge -m org.w11.Bridge1'
MX='$HOME/.config/monitors.xml'  # expanded in the guest, not here

# GDM's own two paths, resolved in the GUEST by which directory the package
# created and never by the distro name -- the same rule vm/build-image.sh's
# dm_gdm follows.  Ubuntu's gdm3 uses /etc/gdm3 and /var/lib/gdm3; Fedora's and
# Arch's gdm use /etc/gdm and /var/lib/gdm [recon2/fedora 5, recon2/arch 5].
GDM_CONF='$(ls /etc/gdm3/custom.conf /etc/gdm/custom.conf 2>/dev/null | head -1)'
GDM_HOME='$(ls -d /var/lib/gdm3 /var/lib/gdm 2>/dev/null | head -1)'

editor_start() {
    # A GUI started in the foreground over ssh never returns: detach it.
    guest "setsid nohup gnome-text-editor $SMOKE_FILE >/dev/null 2>&1 </dev/null & sleep 3; true" >/dev/null || true
}
editor_save() { guest "wdotool key ctrl+s" >/dev/null || true; }
set_scale2()  { guest "gsettings set org.gnome.desktop.interface scaling-factor 2; sleep 3" >/dev/null || true; }

shell_major() { guest "gnome-shell --version" | sed -n 's/^GNOME Shell \([0-9]*\).*/\1/p'; }
mx_sum()      { guest "md5sum $MX 2>/dev/null | cut -d' ' -f1" | tr -d ' \n'; }

install_extra() {
    # The tree's bridge over the package's copy: the working tree is what is
    # under test, and gnome-shell loads whatever is in the extension directory
    # at the next login (the reboot phase_install does right after this).
    "$VM" scp "$NAME" "$REPO/gnome/$BRIDGE_UUID/extension.js"                "$NAME:/tmp/b-extension.js" >/dev/null
    "$VM" scp "$NAME" "$REPO/gnome/$BRIDGE_UUID/metadata.json"               "$NAME:/tmp/b-metadata.json" >/dev/null
    "$VM" scp "$NAME" "$REPO/gnome/$BRIDGE_UUID/org.w11.Bridge1.xml" "$NAME:/tmp/b-iface.xml" >/dev/null
    root "d=/usr/share/gnome-shell/extensions/$BRIDGE_UUID; mkdir -p \$d;
          install -m 644 /tmp/b-extension.js \$d/extension.js;
          install -m 644 /tmp/b-metadata.json \$d/metadata.json;
          install -m 644 /tmp/b-iface.xml \$d/org.w11.Bridge1.xml; true" >/dev/null
    pass "the tree's $BRIDGE_UUID is installed over the package's copy"
}

# F0.0 / F6.2: what the tools say when the bridge is there but marked out of
# date -- the whole of the 51.beta measurement, recorded before anything is
# fixed up, because the advice those messages give ("reinstall a matching
# gnome/ from the repo") cannot help: the repo's gnome/ has the same list.
record_out_of_date() {
    local out st
    for c in "wwmctl -l" "wdotool search --name ." "wxprop -root _NET_CLIENT_LIST"; do
        st=0; out=$(guest "$c 2>&1") || st=$?
        note "F0.0 evidence: \`$c\` exit $st: $(ev "$out")"
    done
    want "the out-of-date message names the shell-version list" \
         "marked out of date|out of date" "$(guest 'wwmctl -l 2>&1' || true)"
}

phase_bridge() {
    local info major
    major=$(shell_major)
    note "gnome-shell $(guest 'gnome-shell --version' | tr -d '\n')"
    # ~12 s on 46 and 50 (50 showed INITIALIZED first: timing, not a bug), so 30 s.
    info=$(await 30 'State: ACTIVE' "gnome-extensions info $BRIDGE_UUID" || true)
    if printf '%s\n' "$info" | grep -q 'State: ACTIVE'; then
        pass "the bridge reaches ACTIVE within 30 s of the session (packaging/common/enable-bridge enabled it)"
    elif printf '%s\n' "$info" | grep -q 'OUT OF DATE'; then
        fail "the bridge is OUT OF DATE as shipped on GNOME $major (F0.0/F6.2)"
        record_out_of_date
        "$VM" scp "$NAME" "$STEPS/guest-gnome-meta51.sh" "$NAME:/tmp/w11-meta.sh" >/dev/null
        note "adding \"$major\" to the INSTALLED metadata.json (never the repo's):" \
             "$(root "sh /tmp/w11-meta.sh $major" | tr -d '\n')"
        root "( sleep 1; reboot ) >/dev/null 2>&1 &" >/dev/null 2>&1 || true
        sleep 8
        wait_session >/dev/null || { fail "no session after the metadata reboot"; return 1; }
        sleep 15
        after_reboot
        info=$(await 30 'State: ACTIVE' "gnome-extensions info $BRIDGE_UUID" || true)
        want "with \"$major\" in shell-version and one reboot the bridge is ACTIVE" "State: ACTIVE" "$info"
    else
        fail "the bridge is neither ACTIVE nor OUT OF DATE [$(ev "$info")]"
    fi
    note "journal: $(guest "journalctl --user -b --no-pager 2>/dev/null \
              | grep -o '\[w11-bridge\].*' | tail -1" || true)"
    want "the bridge answers GetVersion on the session bus" "uint32|[0-9]" \
         "$(guest "gdbus call $BR.GetVersion 2>&1" || true)"
    # The name, on its own line, because acquiring it is what every window
    # command depends on and because it is the one half of the bridge that was
    # measured on Fedora's GNOME 50.4 before any flavor existed: the shell log
    # said `[w11-bridge] acquired org.w11.Bridge` and
    # `install-bridge.sh --check` said `owned: yes` [recon2/fedora 3.2].
    want "org.w11.Bridge is owned on this shell" "org.w11.Bridge" \
         "$(guest "gdbus call --session --dest org.freedesktop.DBus \
                     --object-path /org/freedesktop/DBus \
                     --method org.freedesktop.DBus.ListNames 2>&1 \
                   | tr ',' '\\n' | grep w11" || true)"
}

# The layout half.  us,de with the Super+Space gesture; German types
# `de: yz@ Straße` byte-exact and `keys explain` says group 2 of 3 (measured on
# 46, 50 and 51 -- the third group is the keymap's, not a third source).
layout_phase() {
    "$VM" scp "$NAME" "$STEPS/guest-gnome-layout.sh" "$NAME:/tmp/w11-layout.sh" >/dev/null
    guest "gsettings set org.gnome.desktop.input-sources sources \"[('xkb','us'),('xkb','de')]\"" >/dev/null || true
    sleep 3
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    local got
    got=$(guest "sh /tmp/w11-layout.sh de" | tr -d ' \n' || true)
    same "the Super+Space gesture reaches the German source" "de" "$got"
    want "keys explain names the second group and where it came from" \
         "group 2 of 3" "$(guest 'wdotool keys explain yz@ 2>&1' || true)"
    same "wdotool type under de arrives byte-exact" "de: yz@ Straße" "$(type_and_read 'de: yz@ Straße')"
    got=$(guest "sh /tmp/w11-layout.sh us" | tr -d ' \n' || true)
    same "the gesture switches back to us" "us" "$got"
    # The same running daemon must follow the switch: no restart between these.
    same "the same daemon follows the switch back and types the US string" "us: yz@" "$(type_and_read 'us: yz@')"
}

# Five sources, the 51.beta measurement: es/gr/fr/de/us each reached by holding
# Super, each typing `yz@` -- except gr, which skips the Latin letters with the
# documented warning.  That measurement is what REFUTED the review finding
# "GNOME 51 changes the i % 3 + 1 chunk rule".
phase_five() {
    [ -n "$WIN" ] || { fail "no window from phase_windows"; return 1; }
    "$VM" scp "$NAME" "$STEPS/guest-gnome-layout.sh" "$NAME:/tmp/w11-layout.sh" >/dev/null
    guest "gsettings set org.gnome.desktop.input-sources sources \
           \"[('xkb','us'),('xkb','de'),('xkb','fr'),('xkb','gr'),('xkb','es')]\"" >/dev/null || true
    sleep 3
    guest "wdotool windowactivate --sync $WIN" >/dev/null || true
    # `yz@` and nothing else, under every source, because that is exactly what
    # the five-source measurement typed.  A `$L: ` prefix would put Latin
    # letters in the string, and under gr there are none to type: the
    # measurement's own words are "gr skips Latin letters with the documented
    # warning", so a check asserting a Latin prefix under Greek is a guaranteed
    # FAIL that says nothing about the tool.
    local L got expl warn
    for L in es gr fr de us; do
        got=$(guest "sh /tmp/w11-layout.sh $L" | tr -d ' \n' || true)
        if [ "$got" != "$L" ]; then fail "the gesture did not reach $L (at $(ev "$got"))"; continue; fi
        expl=$(guest "wdotool keys explain yz@ 2>&1 | head -1" || true)
        note "$L: $(ev "$expl")"
        editor_clear
        warn=$(guest "wdotool type --delay 30 -- yz@ 2>&1 >/dev/null" || true)
        sleep 0.6; editor_save; sleep 1
        got=$(editor_text)
        if [ "$L" = gr ]; then
            # y and z have no key on the Greek layout, in any group.  The tool
            # types what it can and warns once PER CHARACTER, in the words
            # wdotool/daemon.py:1690 gives it: `Can't type character 'y' (not on
            # the Greek layout). Skipping.`  Both characters have to be named:
            # one warning and a silent second skip would be the bug.
            want "gr names the first character it cannot type and says it skipped it" \
                 "Can't type character 'y' \(not on the .* layout\)\. Skipping\." "$warn"
            want "...and the second one too, not just the first" \
                 "Can't type character 'z' \(not on the .* layout\)\. Skipping\." "$warn"
            # The file is NOT asserted under gr, and this is why: `editor_clear`
            # and `editor_save` are ctrl+a and ctrl+s, and a and s have no key
            # on a Greek group either -- so under gr the editor is neither
            # cleared nor saved and $SMOKE_FILE still holds the PREVIOUS
            # source's bytes.  Measured on gnome46 (GNOME 46.0) on 2026-09-08:
            # the warning named y and z exactly as above while the file still
            # read `yz@` from the `es` iteration.  The five-source measurement
            # claims the warning, not a read-back, and this is the reason it
            # cannot claim one.
            note "gr warned: $(ev "$warn")"
            note "gr read-back (recorded, NOT asserted -- ctrl+a/ctrl+s are unreachable on a Greek group,"
            note "so the editor was neither cleared nor saved): $(ev "$got")"
        else
            same "$L types yz@ (five sources, group $(printf '%s' "$expl" | grep -o 'group [0-9]* of [0-9]*'))" \
                 "yz@" "$got"
        fi
    done
    guest "gsettings set org.gnome.desktop.input-sources sources \"[('xkb','us'),('xkb','de')]\"" >/dev/null || true
}

phase_display() {
    # Mutter's own rule, and the one wxrandr has to relay rather than invent:
    # a layout with a hole between two monitors is "Logical monitors not
    # adjacent", and the answer is to re-place the neighbours in one call.
    local pair first second
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    common_display_phase
    [ -n "$second" ] || return 0
    local out st=0
    out=$(guest "wxrandr --output $second --pos 4000x0 2>&1") || st=$?
    if [ "$st" = 0 ]; then fail "a gapped layout was accepted (Mutter refuses one)"; else
        want "a gap is refused with Mutter's own words and the re-place hint" \
             "not adjacent" "$out"
        want "the refusal tells the user what to do instead" "re-place" "$out"
    fi
    guest "wxrandr --output $second --right-of $first" >/dev/null || true
}

# --persistent, and the bridge's ConfirmDisplayChange.  Measured: nothing is
# written before the dialog is answered, the layout reverts after 20 s,
# ConfirmDisplayChange(true) -> (true,) and Mutter writes monitors.xml
# immediately, a second persistent apply leaves monitors.xml.wxrandr-backup,
# (false) reverts, and (true) with no dialog on screen -> (false,).
# NOTE recorded by the same measurement: no tool calls ConfirmDisplayChange --
# wxrandr --persistent leaves the user to click even when the bridge is up.
phase_persistent() {
    local pair first second before out st=0 pos0
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    [ -n "$second" ] || { note "one head: --persistent needs two"; return 0; }
    guest "wxrandr --output $second --right-of $first" >/dev/null || true
    sleep 2
    # Where the revert has to land, read rather than assumed: on a three-head
    # instance the rightmost head sits at 3840, not at the 1920 a two-head
    # measurement would have seen.
    pos0=$(opos "$second")
    note "$second starts at $pos0"
    before=$(mx_sum)
    note "monitors.xml before: ${before:-absent}"
    out=$(guest "wxrandr --output $second --below $first --persistent 2>&1") || st=$?
    want "--persistent prints the Keep-changes warning" 'Keep changes\?' "$out"
    local now; now=$(mx_sum)
    same "nothing is written before the dialog is answered" "${before:-absent}" "${now:-absent}"
    sleep 25
    same "the layout reverts on its own after the 20 s countdown" "$pos0" "$(opos "$second")"
    # Now answer it through the bridge, inside the 20 s.
    guest "wxrandr --output $second --below $first --persistent >/dev/null 2>&1 &" >/dev/null || true
    sleep 4
    same "the bridge's ConfirmDisplayChange(true) finds and presses the dialog" "(true,)" \
         "$(guest "gdbus call $BR.ConfirmDisplayChange true 2>&1" | tr -d ' \n' || true)"
    sleep 3
    local after; after=$(mx_sum)
    if [ -n "$after" ] && [ "$after" != "$before" ]; then
        pass "Mutter wrote ~/.config/monitors.xml as soon as the change was kept"
    else fail "monitors.xml did not change after the confirmation (${before:-absent} -> ${after:-absent})"; fi
    guest "cp $MX /tmp/w11-monitors-kept.xml" >/dev/null 2>&1 || true
    # A second persistent apply keeps the previous bytes next to it.
    guest "wxrandr --output $second --right-of $first --persistent >/dev/null 2>&1 &" >/dev/null || true
    sleep 4
    guest "gdbus call $BR.ConfirmDisplayChange true" >/dev/null 2>&1 || true
    sleep 3
    want "a second persistent apply leaves monitors.xml.wxrandr-backup" "wxrandr-backup" \
         "$(guest "ls $MX.wxrandr-backup 2>&1" || true)"
    # (false) puts it back...
    guest "wxrandr --output $second --below $first --persistent >/dev/null 2>&1 &" >/dev/null || true
    sleep 4
    same "ConfirmDisplayChange(false) answers the dialog too" "(true,)" \
         "$(guest "gdbus call $BR.ConfirmDisplayChange false 2>&1" | tr -d ' \n' || true)"
    sleep 3
    same "...and the layout is the one from before that apply" "$pos0" "$(opos "$second")"
    # ...and with no dialog on screen there is nothing to press.
    same "ConfirmDisplayChange(true) with no dialog returns false" "(false,)" \
         "$(guest "gdbus call $BR.ConfirmDisplayChange true 2>&1" | tr -d ' \n' || true)"
    note "no tool in the tree calls ConfirmDisplayChange: --persistent still leaves the user to click,"
    note "and docs/WXRANDR.md:1176 says there is no D-Bus call to confirm from outside the shell,"
    note "which this project's own bridge contradicts (candidate improvement, recorded not asserted)"
}

# The overlap route.  On 46.0 (libmutter-14 build 9e23feb34618) this applied
# with all six checks green and the shipped W11Overlap14 typelib.
phase_overlap() {
    local pair first second out st=0 mxsum undo dryundo
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    [ -n "$second" ] || { note "one head: the overlap route needs two"; return 0; }
    mxsum=$(guest "sha256sum $MX 2>/dev/null | cut -d' ' -f1" | tr -d ' \n' || true)
    local pos0; pos0=$(opos "$second")
    note "$second starts at $pos0; the overlap apply moves it to 1000,0, on top of its neighbour"
    st=0; out=$(guest "gnome-extensions enable $OVERLAP_UUID 2>&1") || st=$?
    ok "gnome-extensions enable $OVERLAP_UUID (46: works live, no relogin -- the package left it INITIALIZED)" "$st"
    sleep 2
    # The first line is the machine-readable token: `available` (it applies
    # here and asks first) or `agreed` (an agreement for THIS build is already
    # recorded, so it applies quietly).  `unavailable` is the failure, and
    # matching the bare word `available` would match that too.
    local status; status=$(guest 'wxrandr --gnome-overlap-status 2>&1' || true)
    note "--gnome-overlap-status: $(ev "$status")"
    # One plain check on every distro since the GNOME 49 record landed.  It used
    # to be an `xwant` off Ubuntu because generations.json carried 46/50/51 and
    # nothing else: Fedora's and Arch's own libmutter-18 was unread, and Fedora
    # 43 is GNOME 49 / libmutter-17, which had no record at all.  Both halves are
    # measured now -- arch-gnome and fedora44-gnome XPASSed this line on mutter
    # 50.4 (CI run 34628777544, gaps.md 1a #4), and GNOME 49.9 on the
    # fedora43-gnome golden reports MetaMonitorsConfig at 80 bytes from its own
    # GType registry, which is the W11Overlap17 record in the table (measured
    # 2026-09-11, batch 8: libmutter-17.so.0 build c7c9add6935d, the sentinel
    # round-tripping at the declared offset and the swapped-tail description
    # refused by name).  A GNOME that is not in the table is a red line here and
    # that is the point: the answer is the record, which is docs/Technical.md
    # section 6, and until it is measured the tool says so itself.
    want "--gnome-overlap-status says the route works on this build" "^(available|agreed)$" \
         "$(printf '%s\n' "$status" | head -1)"
    # The overview holds a modal grab, and the extension refuses every call while
    # anything does (check `pending-dialog`: that grab is how GNOME asks "Keep
    # changes?", and confirming one while this had moved a monitor is the single
    # way an overlapping layout could reach monitors.xml).  On GNOME 49 the
    # session comes up IN the overview and stays there with a window open --
    # measured on the fedora43-gnome golden 2026-09-11: modalCount 1 with
    # gnome-text-editor on screen, cleared by one Escape.  So press it with our
    # own tool, and retry the dryrun twice: the refusal's own advice is to answer
    # the grab and run it again, and a phase that does what the message says is
    # the phase that measures the route rather than the overview.
    local try
    for try in 1 2 3; do
        out=$(guest "wxrandr --dryrun --unsafe-gnome-overlap --output $second --pos 1000x0 2>&1" || true)
        printf '%s\n' "$out" | grep -q '(pending-dialog)' || break
        note "attempt $try refused with pending-dialog: sending Escape (GNOME 49 comes up in the overview)"
        guest "wdotool key Escape" >/dev/null 2>&1 || true
        # and the second way out of the overview, for a session where the key
        # did not land: activating a window leaves it, which is the bridge's
        # own path rather than the input daemon's.
        if [ -n "${WIN:-}" ]; then
            guest "wdotool windowactivate --sync $WIN" >/dev/null 2>&1 || true
        fi
        sleep 3
    done
    # Each is one stderr line, `xrandr: overlap check <name>: <detail>`:
    # shell-version, typelib, sentinel, pending-dialog, bounded-read, public-view.
    local n; n=$(printf '%s\n' "$out" | grep -c 'overlap check ')
    same "the dryrun runs six checks inside the extension" "6" "$n"
    # The generation the extension says it just checked, read out of the
    # shell-version line's `libmutter-<n>.so.0` -- which the extension takes
    # from gnome-shell's own /proc/self/maps, not from the table.  The agreement
    # below has to record this same label, which is what makes the recorded
    # consent be about the build the checks ran on (17 on the fedora43-gnome
    # golden, measured 2026-09-11; 14 on noble, 18 on resolute, 51 on stonking).
    local lmgen; lmgen=$(printf '%s\n' "$out" \
        | sed -n 's/.*overlap check shell-version:.*libmutter-\([0-9][0-9]*\)\.so\.0.*/\1/p' | head -1)
    note "the libmutter generation the checks ran against: $(ev "$lmgen")"
    want "every check passed and nothing was written" "dryrun: nothing was written" "$out"
    dryundo=$(printf '%s\n' "$out" | sed -n 's/^ *To undo: *//p' | head -1)
    note "the DRYRUN's printed undo: $(ev "$dryundo")"
    # The APPLY's own output, kept, and taken BEFORE any agreement is recorded.
    # gnome_overlap.py:341-388 prints the whole warning -- the `To undo:` line
    # with it -- on every invocation until `--gnome-overlap-allow` has recorded
    # an agreement for this build, "after which it is one line".  The
    # measurement's undo came from exactly this apply, the first one, with no
    # agreement on disk.  Measured on gnome46 on 2026-09-08: with the agreement
    # recorded first, the apply prints the one-line form and there is no undo to
    # paste at all -- which is why a leftover agreement is withdrawn here first.
    guest "wxrandr --gnome-overlap-forget >/dev/null 2>&1; true" >/dev/null || true
    st=0; out=$(guest "wxrandr --unsafe-gnome-overlap --output $second --pos 1000x0 2>&1") || st=$?
    ok "--unsafe-gnome-overlap applied the overlapping position with no agreement recorded" "$st"
    undo=$(printf '%s\n' "$out" | sed -n 's/^ *To undo: *//p' | head -1)
    note "the APPLY's printed undo: $(ev "$undo")"
    if [ -n "$undo" ] && [ -n "$dryundo" ] && [ "$undo" != "$dryundo" ]; then
        note "(it differs from the dryrun's line: $(ev "$dryundo"))"
    fi
    sleep 2
    same "$second is at 1000,0 -- it now repeats its neighbour's pixels from x=1000" "1000,0" "$(opos "$second")"
    shot overlap-applied
    if [ -n "$undo" ]; then
        guest "$undo" >/dev/null 2>&1 || true
        sleep 2
        same "the undo the APPLY printed put $second back where it was" "$pos0" "$(opos "$second")"
    else fail "the apply printed no undo command"; fi
    # Now the agreement, which the measurement recorded after that first apply:
    # what it holds, and the one thing it changes -- how much is printed.
    guest "wxrandr --gnome-overlap-allow >/dev/null 2>&1; true" >/dev/null || true
    out=$(guest 'cat ${XDG_CONFIG_HOME:-$HOME/.config}/w11/overlap-consent.json 2>&1' || true)
    want "the agreement names libmutter" '"libmutter"' "$out"
    want "the agreement names the libmutter build id" '"libmutter_build"' "$out"
    want "the agreement names the shell version" '"shell"' "$out"
    want "the agreement names the MetaMonitorsConfig size it was measured against" '"struct_size"' "$out"
    want "the agreement names the libmutter generation the checks ran against (libmutter-$lmgen)" \
         "\"libmutter\": \"$lmgen\"" "$out"
    out=$(guest "wxrandr --unsafe-gnome-overlap --output $second --pos 1000x0 2>&1" || true)
    sleep 2
    wantnot "with an agreement recorded the apply is quiet: no undo paragraph" "To undo:" "$out"
    same "...and it applied just the same" "1000,0" "$(opos "$second")"
    guest "$undo" >/dev/null 2>&1 || true
    sleep 2
    guest "wxrandr --gnome-overlap-forget >/dev/null 2>&1; true" >/dev/null || true
    wantnot "--gnome-overlap-forget withdrew the agreement" "libmutter" \
        "$(guest 'cat ${XDG_CONFIG_HOME:-$HOME/.config}/w11/overlap-consent.json 2>&1' || true)"
    same "~/.config/monitors.xml is untouched by the whole overlap route" "$mxsum" \
         "$(guest "sha256sum $MX 2>/dev/null | cut -d' ' -f1" | tr -d ' \n' || true)"
}

# F0.3: the greeter is a GNOME Shell session too, and gdm runs it as its own
# user with its own dconf.  enable-bridge must refuse to run there: no stamp
# under gdm's state directory (/var/lib/gdm3 on Ubuntu, /var/lib/gdm on Fedora
# and Arch) and no w11 in gdm's enabled-extensions after a boot that
# nobody logs into.
phase_enablebridge() {
    root "c=$GDM_CONF; sed -i 's/^AutomaticLoginEnable=.*/AutomaticLoginEnable=false/' \$c 2>/dev/null;
          grep -c AutomaticLoginEnable \$c" >/dev/null 2>&1 || true
    root "( sleep 1; reboot ) >/dev/null 2>&1 &" >/dev/null 2>&1 || true
    sleep 45
    local i
    for i in $(seq 1 20); do root 'pgrep -u gdm gnome-shell >/dev/null && echo up' | grep -q up && break; sleep 5; done
    note "greeter: $(root 'pgrep -u gdm -a gnome-shell | head -1' | tr -d '\n' || true)"
    wantnot "no enable-bridge stamp under gdm's own state directory (nobody logged in)" "bridge-enabled" \
        "$(root "find $GDM_HOME -name bridge-enabled 2>/dev/null" || true)"
    # HOME, explicitly.  `runuser -u gdm -- dconf read` keeps ROOT's environment,
    # so dconf opens /root/.config/dconf/user and answers about root's database:
    # the check would pass on that error output whatever gdm's dconf held.  The
    # grep over gdm's own dconf directory is the second, independent half.
    #
    # And a runtime directory gdm can WRITE, made here rather than assumed:
    # nobody has logged in as gdm, so logind has never made /run/user/<gdm>, and
    # dconf does not create it -- `XDG_RUNTIME_DIR=/run/user/$(id -u gdm)` got
    # `dconf-CRITICAL: unable to create directory '/run/user/42/dconf':
    # Permission denied` and no answer at all, which a `wantnot` passes on
    # whatever gdm's database holds.  Measured on the fedora43-gnome golden
    # 2026-09-11 (gdm is uid 42 there, /run/user/42 absent) and reported on
    # nixos-gnome as uid 132 (flavors.md 7, design A6); with a private directory
    # chowned to gdm the same read answers cleanly, rc 0, on the same box.
    #
    # And the read is asserted to have ANSWERED before its answer is read: a
    # `wantnot` over an error string passes for the wrong reason, so the two
    # halves are captured separately and the first check is that the dconf half
    # carries no complaint and exited 0 (the `|| echo dconf-read-failed` is the
    # rc, which is otherwise lost to the `rm -rf` at the end of the command).
    # `chown gdm` failing -- no gdm user, a read-only /tmp, a runtime directory
    # that wants more than an owner, which is the open question on nixos-gnome
    # -- is how this silently goes back to the mode it was just fixed out of;
    # this line makes that a red line instead.
    local dconf_out grep_out
    # DCONF_PROFILE=gdm ON NIXOS ONLY.  On NixOS the `user` profile's system
    # database is where nix/module.nix enables the bridge for every seated
    # user, so the greeter's read must name gdm's own profile or it answers
    # ['w11-bridge@w11'] for a greeter that does not have it (CI run
    # 34662004383).  On Ubuntu/Fedora/Arch the `user` profile is the greeter's
    # and gdm has no profile of its own on disk: naming it makes dconf warn
    # `unable to open named profile (gdm)` and, worse, reach into /root's
    # XDG_DATA_DIRS with a `Permission denied` the wantnot below then trips on
    # (fedora43/fedora44/arch-gnome, same run).  So the profile is named only
    # where the module puts the bridge in the shared database.
    local dprof=""; [ "$DISTRO" = nixos ] && dprof="DCONF_PROFILE=gdm"
    dconf_out=$(root "t=\$(mktemp -d); chown gdm \"\$t\" 2>/dev/null;
                      runuser -u gdm -- env HOME=$GDM_HOME XDG_RUNTIME_DIR=\$t $dprof \
                          dconf read /org/gnome/shell/enabled-extensions 2>&1 \
                              || echo dconf-read-failed;
                      rm -rf \"\$t\"" || true)
    grep_out=$(root "grep -ras w11 $GDM_HOME/.config/dconf 2>/dev/null | head -2" || true)
    wantnot "gdm's dconf read answered as gdm (rc 0, no dconf-CRITICAL, no permission error)" \
        "dconf-CRITICAL|Permission denied|unable to create directory|dconf-read-failed" "$dconf_out"
    wantnot "gdm's own dconf has no w11 in enabled-extensions" "w11" "$dconf_out
$grep_out"
    root "c=$GDM_CONF; sed -i 's/^AutomaticLoginEnable=.*/AutomaticLoginEnable=true/' \$c 2>/dev/null; true" \
        >/dev/null 2>&1 || true
    root "( sleep 1; reboot ) >/dev/null 2>&1 &" >/dev/null 2>&1 || true
    sleep 10
    wait_session >/dev/null || { fail "no session after restoring autologin"; return 1; }
    sleep 12
    after_reboot
    pass "autologin restored and the session came back"
    WIN=""      # the editor went with the reboot
}

# F0.6: `install-bridge.sh --udev --uninstall` says it restored root:root 0600,
# but it only removes ITS OWN /etc/udev/rules.d copy -- the .deb's rule lives in
# /usr/lib/udev/rules.d and is still there, so the next uevent tags the node
# again and logind hands the ACL straight back.  This measures that.
phase_udev() {
    "$VM" scp "$NAME" "$REPO/gnome/install-bridge.sh"           "$NAME:/tmp/install-bridge.sh" >/dev/null
    "$VM" scp "$NAME" "$REPO/gnome/60-w11-uinput.rules" "$NAME:/tmp/60-w11-uinput.rules" >/dev/null
    "$VM" scp "$NAME" "$REPO/gnome/modules-load-uinput.conf"    "$NAME:/tmp/modules-load-uinput.conf" >/dev/null
    want "the seated user has the uaccess ACL on /dev/uinput to start with" "^user:[a-z]" \
         "$(root 'getfacl -p /dev/uinput 2>/dev/null' || true)"
    local unin; unin=$(root 'sh /tmp/install-bridge.sh --udev --uninstall 2>&1' || true)
    note "--udev --uninstall said: $(ev "$unin")"
    # Fix 9 of the plan (gnome/install-bridge.sh:471, T11): the uninstall branch
    # says it restored root:root 0600 while the package's own rule is still in
    # /usr/lib/udev/rules.d, so the grant comes straight back at the next
    # uevent.  What it owes the user is a sentence naming the command that
    # removes the package on the distro in front of them -- which is NOT `apt`
    # on three of the five GNOME rigs: this xwant fired on fedora43-gnome,
    # fedora44-gnome and arch-gnome as well (CI run 34628777544, gaps.md 1b #10),
    # where the answer is `dnf remove w11` and `pacman -R w11`.
    # The sentence landed in gnome/install-bridge.sh (pm_remove_w11, ~line 451:
    # it reads ID and ID_LIKE out of /etc/os-release the way w11common/distro.py
    # does), so this is a plain check everywhere the package's rule is a file in
    # /usr/lib/udev/rules.d.  NixOS is the one place it is not: the rule comes
    # from services.udev.packages (nix/module.nix:210) and /etc/udev/rules.d
    # resolves into the store, so install-bridge.sh's module_owns_udev_rule
    # (readlink -f into /nix/store) fires the sentence there instead of the
    # `[ -f "$UDEV_PKG" ]` guard, naming the module option to turn off.
    local rm_cmd
    case "$DISTRO" in
    fedora) rm_cmd="dnf remove w11" ;;
    arch)   rm_cmd="pacman -R w11" ;;
    nixos)  rm_cmd="programs.w11.uinput.enable" ;;
    *)      rm_cmd="apt remove w11" ;;
    esac
    want "--udev --uninstall names $rm_cmd when the package's rule is there (fix T11)" \
         "$rm_cmd" "$unin"
    want "the package's own rule is still installed" "60-w11-uinput.rules" \
         "$(root 'ls /usr/lib/udev/rules.d/60-w11-uinput.rules 2>&1' || true)"
    root 'udevadm control --reload-rules; udevadm trigger --name-match=uinput; udevadm settle --timeout=5' \
        >/dev/null 2>&1 || true
    sleep 2
    local acl; acl=$(root 'getfacl -p /dev/uinput 2>/dev/null' || true)
    if printf '%s\n' "$acl" | grep -Eq '^user:[a-z]'; then
        pass "F0.6: the ACL comes back at the next trigger -- --udev --uninstall does not undo the .deb's rule"
    else
        fail "F0.6: the ACL did not come back [$(ev "$acl")] -- the finding's premise no longer holds"
    fi
    # Leave the system the way the PACKAGE made it: `--udev` here would install a
    # second, non-package rule into /etc/udev/rules.d, and the --remove phase
    # that runs after every phase would then be measuring a system dpkg never
    # produced.  The uninstall + one trigger is what puts it back.
    root 'sh /tmp/install-bridge.sh --udev --uninstall >/dev/null 2>&1;
          udevadm control --reload-rules; udevadm trigger --name-match=uinput;
          udevadm settle --timeout=5; true' >/dev/null 2>&1 || true
    sleep 2
    want "the package's rule alone still grants the seated user the node" "^user:[a-z]" \
         "$(root 'getfacl -p /dev/uinput 2>/dev/null' || true)"
    if [ "$DISTRO" = nixos ]; then
        # NixOS: /etc/udev/rules.d IS the module's store directory, so our rule
        # is listed there by name whatever --uninstall did.  The leftover a copy
        # would leave is a regular file; the module's resolves into the store.
        want "and the rule under /etc/udev/rules.d is the module's store file, not a copy of ours" "^/nix/store/" \
             "$(root 'readlink -f /etc/udev/rules.d/60-w11-uinput.rules 2>/dev/null' || true)"
    else
        wantnot "and no /etc/udev/rules.d copy of ours is left behind" "60-w11-uinput" \
                "$(root 'ls /etc/udev/rules.d/ 2>/dev/null' || true)"
    fi
}
