# live-smoke.d/gnome-x11.sh -- GNOME Shell 46 on XORG (noble-gnome-x11), the session 24.04's gear menu
# offers and 26.04 no longer has.
#
# The handover body is Xfce's, unchanged, for the reason kde-x11.sh gives: handing our argv to the real
# xdotool/wmctrl/xprop/xrandr is a property of the SESSION TYPE and not of the desktop.  Here that is
# the whole product claim under its hardest condition: org.gnome.Shell, org.gnome.Mutter.DisplayConfig
# AND org.w11.Bridge are all on this session's bus, every one of them a route this toolbox
# knows, and the answer is still the X server's -- because passthrough.session_kind() is asked before
# any backend is detected [M recon2/gnome-xorg.md 2, against a simulated bus owning all three].
#
# Four phases only this flavor can run:
#
#   * `bridge`.  The bridge, taken out of the running shell and put back with nobody logged out, and
#     the tools still handing over the whole time.  What the first two live runs of this flavor found is
#     that `gnome-shell --replace` -- the move this phase was written around -- is not that: Ubuntu
#     24.04 runs the shell as `org.gnome.Shell@x11.service` (Restart=always, RestartSec=0ms,
#     RefuseManualStart/Stop=on) and `--replace` walks it into its own start limit, whose OnFailure sets
#     `org.gnome.shell disable-user-extensions true` -- persistently, for that user, in every session
#     after it.  phase_bridge's own comment carries the whole measurement; the move it makes now is
#     `gnome-extensions disable`/`enable` in the running shell, watched on the bus.
#   * `display`.  Two oracles that must agree: real xrandr, and org.gnome.Mutter.DisplayConfig, which
#     mutter 46 serves from MetaMonitorManagerXrandr on this very session (`strings` of libmutter-14:
#     MetaBackendX11Cm and meta-monitor-manager-xrandr.c are both in it, and both are gone from
#     libmutter-18 [M recon2/gnome-xorg.md 1]).  A tool that picked its backend by bus name would take
#     the D-Bus route on a session where xrandr is the truth.
#   * `root`.  GDM keeps an X11 session's cookie at <runtime dir>/gdm/Xauthority -- one directory below
#     where find_xauthority() used to look.  With the session leader gone it answered None although the
#     file was there [M recon2/gnome-xorg.md 3]; the glob that fixes it is in the tree and this is where
#     it meets a real GDM.
#   * `uinput`.  On X11 the events our own code writes into /dev/uinput are consumed by the X SERVER,
#     which is the one input path that differs from Wayland only in who reads it.  Nobody has run it:
#     the recon container had no /dev/uinput at all.
#
# The editor is gnome-text-editor, which ubuntu-desktop puts on the image, so this flavor's package set
# stays byte-identical to noble-gnome's -- the pair is a controlled experiment and one extra package
# would be one uncontrolled variable.  Its WM_CLASS is NOT the same on the two halves of that pair, and
# that is the one thing the twin cannot tell you: under Wayland gnome-text-editor carries its desktop-file
# id (`org.gnome.TextEditor`, gnome.sh's EDITOR_CLASS), while the SAME binary on this Xorg session is a
# plain X client and carries the X11 pair -- measured on the live session, 2026-09-09:
#
#     wmctrl -l -x  ->  0x00a00004  0 gnome-text-editor.gnome-text-editor  ... w11-smoke.txt (~/) - Text Editor
#
# so `search --class TextEditor` (an extended regex, handed to the REAL xdotool here) matches nothing --
# `TextEditor` has no hyphen in it -- and both checks that look for the editor failed with an empty
# answer in CI runs 34308982263 and 34319854037.  EDITOR_CLASS is the ONE spelling that session carries,
# with no alternation in it: common.sh:343 (phase_windows, which `--phases windows` can select on any
# flavor) interpolates EDITOR_CLASS into a guest command unquoted, and a `|` there is a pipe in the
# guest's shell into a command named TextEditor.  The window title is matched separately, by editor_xid.
#
# shellcheck source=live-smoke.d/xfce.sh
. "$STEPS/xfce.sh"

SMOKE_PHASES="install passthrough bridge display root uinput"
EDITOR_CLASS=gnome-text-editor
BRIDGE_UUID=w11-bridge@w11
BR='--session -d org.w11.Bridge -o /org/w11/Bridge -m org.w11.Bridge1'
# The bridge's reload method, and the file it re-reads.  Deliberately NOT a method of
# org.w11.Bridge1: that interface is the client surface the tools and MockBridge are pinned to.
RL='--session -d org.w11.Bridge -o /org/w11/Bridge -m org.w11.BridgeReload1.Reload'
EXTJS=/usr/share/gnome-shell/extensions/$BRIDGE_UUID/extension.js

# xfce.sh's editor is an xterm, which this image does not carry.  A GUI started in the foreground over
# ssh never returns: detach it.
editor_start() {
    guest "setsid nohup gnome-text-editor $SMOKE_FILE >/dev/null 2>&1 </dev/null & sleep 3; true" >/dev/null || true
}
editor_save() { guest "wdotool key ctrl+s" >/dev/null || true; }

# gnome.sh's install_extra, and for its reason: the phase below is about the BRIDGE, and in the default
# (working-tree) mode the tree's zipapps land in /usr/local/bin while the extension stays whatever the
# package put under /usr/share/gnome-shell/extensions -- so without this the one phase that only this
# flavor can run would be measuring the shipped 0.4.0 extension.js against a tree of tools.  It is
# gnome.sh's body verbatim; this flavor sources xfce.sh (the handover), which has no such hook.  The
# driver calls it only in tree mode, before the reboot that makes gnome-shell rescan the directory.
install_extra() {
    "$VM" scp "$NAME" "$REPO/gnome/$BRIDGE_UUID/extension.js"                "$NAME:/tmp/b-extension.js" >/dev/null
    "$VM" scp "$NAME" "$REPO/gnome/$BRIDGE_UUID/metadata.json"               "$NAME:/tmp/b-metadata.json" >/dev/null
    "$VM" scp "$NAME" "$REPO/gnome/$BRIDGE_UUID/org.w11.Bridge1.xml" "$NAME:/tmp/b-iface.xml" >/dev/null
    root "d=/usr/share/gnome-shell/extensions/$BRIDGE_UUID; mkdir -p \$d;
          install -m 644 /tmp/b-extension.js \$d/extension.js;
          install -m 644 /tmp/b-metadata.json \$d/metadata.json;
          install -m 644 /tmp/b-iface.xml \$d/org.w11.Bridge1.xml; true" >/dev/null
    pass "the tree's $BRIDGE_UUID is installed over the package's copy"
}

# The editor window's X id, as the REAL wmctrl prints it: the anchor several phases need and the one
# number both planes agree on.
editor_xid() {
    guest "wmctrl -l -x | awk '/TextEditor|Text Editor/ { print \$1; exit }'" | tr -d ' \r\n'
}

# `NameHasOwner org.w11.Bridge` -- `(true,)` or `(false,)`, the bus's own answer to "is the
# extension's code running in there right now".  It is the only honest oracle for the off/on move below:
# `gnome-extensions info` reports what the shell's ExtensionManager thinks, and the name is what every
# tool of ours actually needs.
#
# <regex> is what the caller is waiting FOR, and there is no version of this without one.  A shell that
# has been told to disable an extension releases the name when it gets to it: on this rig that was about
# three seconds after the disable and about four after the enable, and a fixed `sleep` cut to those two
# numbers is a check that measures a KVM runner's load average instead of the bridge -- exactly the kind
# of red this phase's rewrite was for.  await polls for thirty seconds and returns the moment the bus
# agrees, so the `same` below stays byte-equal and the timing drops out of it.
bridge_owned() {
    await 30 "${1:-.}" "gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
           --method org.freedesktop.DBus.NameHasOwner org.w11.Bridge 2>&1" | tr -d ' \r\n'
}

# The bridge, taken out of a running shell and put back, with nobody logged out -- plus the one that
# matters more: the four tools go on handing over while the bridge holds its name, because the session
# type decided that before any bus name was read.
#
# This phase used to make that claim with `gnome-shell --replace`, and the first two live runs of the
# flavor (CI 34308982263 and 34319854037) say what that does on Ubuntu 24.04's GNOME 46, which runs the
# shell as a systemd user unit.  Measured by hand on this rig, 2026-09-09:
#
#   * /usr/lib/systemd/user/org.gnome.Shell@x11.service is `Restart=always`, `RestartSec=0ms`,
#     `RefuseManualStart=on`, `RefuseManualStop=on`, `OnFailure=org.gnome.Shell-disable-extensions.service
#     gnome-session-failed.target`.
#   * `gnome-shell --replace` therefore does not hand the WM role anywhere: the unit's shell exits, systemd
#     restarts it instantly, the hand-started one still holds the selection, and after four rounds the unit
#     goes `failed (Result: protocol)` -- `Start request repeated too quickly` -- and fires OnFailure.
#   * OnFailure is `ExecStart=gsettings set org.gnome.shell disable-user-extensions true`, a PERSISTENT
#     dconf key, plus the three `gnome-session-failed` windows both CI logs show in `wmctrl -l -x`.
#   * From then on that user has no extensions in any session: the bridge reads `Enabled: No / State:
#     INITIALIZED`, `org.w11.Bridge` is unowned, and our own backend says (correctly) "installed
#     but not enabled (state 2)".  It does not come back on a reboot, or on `gnome-extensions enable`, or
#     on deleting /run/user/1000/gnome-shell-disable-extensions -- all three tried.  One thing recovers it,
#     live and with no logout: `gsettings set org.gnome.shell disable-user-extensions false`, after which
#     the shell loads the extension and takes the name inside four seconds.
#
# So `--replace` is not run here any more.  What replaces it is a better measurement of the same claim: an
# extension the running shell already knows can be taken out and put back IN THAT PROCESS, which is what
# "no logout" means for a bridge that is already installed.  The other half -- reloading CHANGED
# extension code without a logout -- used to be an xwant asked of GNOME's own ReloadExtension; it is
# route 3 in the tree now (the bridge re-reads its own file), and the block at the end measures both:
# that GNOME's method still refuses, and that ours works.
phase_bridge() {
    note "gnome-shell $(guest 'gnome-shell --version' | tr -d '\r\n' || true), session $(guest \
         'echo $XDG_SESSION_TYPE' | tr -d '\r' || true)"
    local pid0 pid1 pid2 info owned ownerpid ours oid xid v0 i rl3
    # A window first: it is the thing the id checks below are about, and phase_install's reboot may have
    # left none.
    editor_start
    # The precondition, as a check rather than an assumption: this one key off makes every line below red
    # for a reason that has nothing to do with the bridge, and that is exactly how both CI runs failed.
    same "this user's extensions are switched on at all (org.gnome.shell disable-user-extensions)" \
         "false" "$(guest 'gsettings get org.gnome.shell disable-user-extensions' | tr -d ' \r\n' || true)"
    pid0=$(guest 'pgrep -x gnome-shell | head -1' | tr -d ' \r\n' || true)
    info=$(await 40 'State: ACTIVE' "gnome-extensions info $BRIDGE_UUID" || true)
    want "the bridge is ACTIVE in this Xorg session, with nobody logged out since the install" \
         "State: ACTIVE" "$info"
    owned=$(guest "gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
                   --method org.freedesktop.DBus.ListNames 2>&1 | tr ',' '\n' | grep w11" || true)
    want "org.w11.Bridge is owned on this Xorg session" "org.w11.Bridge" "$owned"
    # Owned by WHICH process: the name has to be held by the session's own gnome-shell and not by
    # something else that took the name.  `GetConnectionUnixProcessID` is the bus's own answer.
    ownerpid=$(guest "gdbus call --session --dest org.freedesktop.DBus \
                      --object-path /org/freedesktop/DBus \
                      --method org.freedesktop.DBus.GetConnectionUnixProcessID org.w11.Bridge \
                      2>&1 | sed -n 's/.*uint32 \([0-9]*\).*/\1/p'" | tr -d ' \r\n' || true)
    if [ -n "$pid0" ] && [ -n "$ownerpid" ]; then
        same "...and the name is held by this session's own gnome-shell" "$pid0" "$ownerpid"
    else
        fail "nothing holds org.w11.Bridge [shell '$pid0', owner '$ownerpid']"
    fi
    want "the bridge answers GetVersion" "uint32|[0-9]" "$(guest "gdbus call $BR.GetVersion 2>&1" || true)"
    # The no-logout move, in the bus's own words.  Measured live on this rig: `(false,)` about three
    # seconds after the disable, `(true,)` about four after the enable, and the shell's pid unchanged
    # across both -- waited for rather than slept through, for the reason bridge_owned's comment gives.
    guest "gnome-extensions disable $BRIDGE_UUID" >/dev/null 2>&1 || true
    same "disabling the bridge releases the name inside the running shell" "(false,)" \
         "$(bridge_owned '\(false,\)')"
    guest "gnome-extensions enable $BRIDGE_UUID" >/dev/null 2>&1 || true
    same "...and enabling it takes the name back, with nobody logged out" "(true,)" \
         "$(bridge_owned '\(true,\)')"
    pid1=$(guest 'pgrep -x gnome-shell | head -1' | tr -d ' \r\n' || true)
    same "...in the same gnome-shell process, which never restarted" "$pid0" "$pid1"
    # The other half, and it is in the tree now: a bridge whose extension.js has CHANGED, re-read by the
    # shell that is already running it, with nobody logged out.  The ladder, lowest first.  Route 2 (the
    # compositor's own scripting surface) has the method and refuses -- which is the check below, kept as
    # a check because the day GNOME un-deprecates ReloadExtension is a day this phase should notice.
    # Route 3 (code we install into the compositor) is ours and is what ships: the object gnome-shell
    # constructs is a ROOT that keeps org.w11.BridgeReload1 and hands the session to a copy of the file,
    # and a reload re-reads extension.js under a path gjs has not imported yet -- see the route-3 block
    # in extension.js for what `?v=<mtime>` does on gjs 1.80.2, which is why the copy goes through
    # $XDG_RUNTIME_DIR.  Measured on this flavor, 2026-09-11 (GNOME Shell 46.0, gjs 1.80.2): `Reload`
    # answers `{"version":4,...,"reread":true}` about a second after an edit, `GetVersion` then answers
    # `(uint32 4,)`, gnome-shell's pid never moves, ten reloads cost gnome-shell 996 kB of RSS (~100 kB
    # a copy, which is the module gjs cannot unload), and the runtime dir is empty again afterwards.
    # Route 6 (a package of ours that patches the shell or its unit) stays unwritten, and is not needed.
    want "ReloadExtension is still the deprecated no-op GNOME 46 made of it" \
         "NotSupported: ReloadExtension is deprecated and does not work" \
         "$(guest "gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell \
                    --method org.gnome.Shell.Extensions.ReloadExtension $BRIDGE_UUID 2>&1" || true)"
    # A Reload that follows no edit must not read the file again: the copy would be one more module in a
    # heap gjs never gives back.
    want "a reload with nothing edited does not read the file again" '"reread":false' \
         "$(guest "gdbus call $RL 2>&1" || true)"
    # ...and now the edit.  The version number is read out of the installed file rather than assumed, so
    # this still measures a reload the day the bridge's own version moves.
    v0=$(root "sed -n 's/^const VERSION = \([0-9]*\);/\1/p' $EXTJS" | tr -d ' \r\n')
    root "sed -i 's/^const VERSION = [0-9]*;/const VERSION = 99;/' $EXTJS" >/dev/null
    want "the shell re-reads the bridge's own code with no logout (AGENTS.md route 3)" '"reread":true' \
         "$(guest "gdbus call $RL 2>&1" || true)"
    want "...and the edited file is the code answering on the bus" "^\(uint32 99,\)$" \
         "$(guest "gdbus call $BR.GetVersion 2>&1" || true)"
    pid2=$(guest 'pgrep -x gnome-shell | head -1' | tr -d ' \r\n' || true)
    same "...in the same gnome-shell process, which never restarted either" "$pid0" "$pid2"
    # The leak that would make this trick unshippable is a handler the replaced copy left connected: gjs
    # unloads no module, so one of those fires for the rest of the session and every WindowEvent arrives
    # twice, then four times.  It is NOT checked here, and the reason is honest rather than an omission:
    # counted on the bus (`gdbus monitor`, one minimize and one unminimize of the editor through the real
    # xdotool), the number is Mutter's and it moves with the desktop's state -- by hand on this flavor it
    # was 4 before three reloads and 4 after, twice over, but inside this phase the same two calls read 2
    # and then 0, because what a minimize emits depends on what the shell was doing at the time.  A check
    # that reads 2, 4 or 0 for reasons that are not the bridge's is a red CI run waiting to happen, so
    # the claim is pinned where it can be counted exactly instead: tests/test_bridge_js.py
    # (`test_a_reload_leaves_no_listener_no_timeout_and_no_export_behind`) counts the handlers on the
    # doubles after three reloads -- 2 on the display, 3 on the workspace manager, 10 per window, the
    # same before and after -- and one WindowEvent, on one exported object, for one window-created.
    # Three reloads also happen right here, with three mtimes that are three different numbers whatever
    # the clock says (the reload key is the file's mtime AND the SHA256 of its bytes, and two `touch`es
    # in the same second move neither, so they would not re-read -- see _read() in extension.js for the
    # measurement that put the sum in the key): the bridge has to survive all three, which the checks
    # below it go on to use.
    for i in 1 2 3; do
        root "touch -d '2030-01-0$i 00:00:00' $EXTJS" >/dev/null
        rl3=$(guest "gdbus call $RL 2>&1" || true)
    done
    want "...and three more re-reads leave a bridge that still answers" '"reread":true' "$rl3"
    # Put the file back the way it was found and read it again, so the instance ends the phase running
    # the tree's own bridge and not a version this check invented.
    root "sed -i 's/^const VERSION = 99;/const VERSION = $v0;/' $EXTJS" >/dev/null
    guest "gdbus call $RL" >/dev/null 2>&1 || true
    same "...and the file restored is read back too, so the session ends on the tree's own bridge" \
         "(uint32 $v0,)" "$(guest "gdbus call $BR.GetVersion 2>&1" | tr -d '\r\n' || true)"
    # And now the point of the whole phase: with all three GNOME names on the bus, the tools are still
    # the originals.  `--version` is the shortest proof -- the answer is the INSTALLED xdotool's version
    # string (3.x on Ubuntu), never our 4.20260303.1.
    want "wdotool still hands over while the bridge is owned" "^xdotool version 3\." \
         "$(guest 'wdotool --version' || true)"
    want "...and wxrandr still says x11, not mutter" "^x11$" \
         "$(guest 'wxrandr --print-backend' | tr -d ' \r' || true)"
    # The route the bridge exists for is reachable when it is asked for by name, which is what
    # `W11_PASSTHROUGH=never` means: a GNOME backend over a session whose windows are X windows.
    ours=$(guest 'W11_PASSTHROUGH=never wwmctl -l -x 2>&1' || true)
    want "forced onto our own code, the bridge lists this session's windows" "^0x[0-9a-f]+ " "$ours"
    # The same window on both planes, by the number both of them print.  The bridge hands out X ids on an
    # Xorg session [M recon2/gnome-xorg.md 2]; the real wmctrl is the oracle for what that id is, and the
    # editor is the window that survived the replace above.
    oid=$(printf '%s\n' "$ours" | awk '/TextEditor|Text Editor/ { print $1; exit }' | tr -d ' \r\n')
    xid=$(editor_xid)
    if [ -n "$xid" ]; then
        same "...and prints the editor under the X id the real wmctrl prints for it" "$xid" "$oid"
    else
        fail "the real wmctrl named no editor window: nothing for the bridge's id to be compared against"
    fi
    note "through the bridge: $(ev "$ours")"
    note "through the real wmctrl: $(ev "$(guest 'wmctrl -l -x' || true)")"
}

# The X server is authoritative here; DisplayConfig is a second opinion that has to agree with it.
phase_display() {
    common_display_phase
    want "wxrandr --backends lists mutter as available on this Xorg session" "mutter +available" \
         "$(guest 'wxrandr --backends' || true)"
    # oracle.py keyed on `gnome` is the DisplayConfig reader; keyed on this flavor's own token it is
    # xrandr.  One guest, two readers, and the claim is that they say the same thing.
    local x11 mutter
    x11=$(oracle_outputs | sort | tr '\n' ' ' || true)
    mutter=$(guest "python3 $ORACLE gnome" | grep -E '^[A-Za-z]' | sort | tr '\n' ' ' || true)
    if [ -n "$(printf '%s' "$x11" | tr -d ' ')" ]; then
        same "org.gnome.Mutter.DisplayConfig agrees with xrandr, output for output" "$x11" "$mutter"
    else
        fail "xrandr named no output: there is nothing for DisplayConfig to be compared against"
    fi
    # The overlap pair.  X places overlapping monitors natively -- that is why --unsafe-gnome-overlap
    # refuses an X11 session in the first place -- and Mutter's own config manager refuses the same
    # layout with `Logical monitors not adjacent`, from meta-monitor-config-manager.c, which is backend
    # independent and present in libmutter-14 [M recon2/gnome-xorg.md 9].  Whether the mutter backend
    # can be made to REFUSE it on Xorg was the one thing the recon could not measure at all, and this
    # flavor's first run answers it: it CANNOT.  Measured by hand on the live session, 2026-09-09,
    #
    #     wxrandr --backend mutter --output Virtual-3 --pos 1920x0   ->  rc 0, no output at all
    #     xrandr --query                                             ->  Virtual-2 and Virtual-3 BOTH at
    #                                                                    1920x1080+1920+0
    #
    # -- so on Xorg the D-Bus route places an overlapping layout exactly as the X server does, and the
    # `Logical monitors not adjacent` string that IS in libmutter-14 belongs to the path a Wayland Mutter
    # takes, not to what this session's DisplayConfig validates.  Both halves of the pair are `same`
    # checks on the position now; the second one used to be an xwant waiting for this run.
    local pair first second pos0 back
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    if [ -n "$second" ]; then
        pos0=$(opos "$second"); back=$(printf '%s' "$pos0" | tr ',' 'x')
        guest "wxrandr --output $second --pos $(opos "$first" | tr ',' 'x')" >/dev/null 2>&1 || true
        sleep 2
        same "an overlapping layout is accepted through the X server, which places one natively" \
             "$(opos "$first")" "$(opos "$second")"
        # back first, so the D-Bus route below is the thing that moves the head and not a no-op that
        # would pass on the X server's own work
        guest "wxrandr --output $second --pos $back" >/dev/null 2>&1 || true
        sleep 2
        # What `--backend mutter` IS here, before what it does: the position check below can only say
        # that the head moved, and a cli.py that one day handed `--backend mutter` over to xrandr on an
        # x11 session would keep it green while its label went false.  So the route is pinned first, in
        # the tool's own words -- measured on this session, 2026-09-09:
        #
        #     wxrandr --print-backend --backend mutter --verbose
        #     mutter / session: x11 / chosen by: flag (--backend mutter) / compositor: Mutter /
        #     protocol: org.gnome.Mutter.DisplayConfig (D-Bus) / available: yes
        #
        # (cinnamon-wayland.sh pins its own Muffin route the same way, for the same reason.)
        want "--backend mutter is the D-Bus route on this Xorg session, not a fallthrough to xrandr" \
             "protocol: org\.gnome\.Mutter\.DisplayConfig \(D-Bus\)" \
             "$(guest 'wxrandr --print-backend --backend mutter --verbose 2>&1' || true)"
        guest "wxrandr --backend mutter --output $second --pos $(opos "$first" | tr ',' 'x')" \
            >/dev/null 2>&1 || true
        sleep 2
        same "...and through Mutter's own DisplayConfig, which does not refuse one on Xorg either" \
             "$(opos "$first")" "$(opos "$second")"
        guest "wxrandr --output $second --pos $back" >/dev/null 2>&1 || true
        sleep 2
        same "...and the layout is put back where the phase found it" "$pos0" "$(opos "$second")"
    fi
    # U18: the status query answers before any handover, so on an X11 session it used to point at an
    # extension nobody needs here.  It names the session now, in the sentence wxrandr/cli.py:1073-1075
    # writes -- the whole sentence and not the token `x11`, which any of the old answers could carry.
    local st; st=$(guest 'wxrandr --gnome-overlap-status 2>&1' || true)
    want "--gnome-overlap-status says this session is x11, in the words the tool prints" \
         "this session is x11, which places overlapping monitors" "$st"
    # And what must never come back: the pre-U18 answer pointed at a shell extension on a session that
    # has no shell in the path at all [M recon2/gnome-xorg.md 7].
    wantnot "...and never sends an X11 user after the overlap extension" \
            "overlap extension is not running" "$st"
}

# F2.3 for an X11 session, which is a different claim from common.sh's phase_root: there the tools look
# for a WAYLAND session, here they have to find a display and a cookie -- and GDM's cookie is the one
# that lives a directory down, at <runtime dir>/gdm/Xauthority.
phase_root() {
    # `env -i` and not `env -i PATH=...`: the tools are zipapps whose shebang is `/usr/bin/env python3`,
    # and with PATH unset execvp falls back to confstr(_CS_PATH) -- /bin:/usr/bin -- which is where the
    # interpreter is.
    editor_start
    local uu ru orig
    uu=$(guest 'wwmctl -l | wc -l' | tr -d ' \r\n' || true)
    ru=$(root 'w=$(command -v wwmctl); env -i "$w" -l | wc -l' | tr -d ' \r\n' || true)
    if [ "${uu:-0}" -gt 0 ]; then
        same "root with an EMPTY environment lists the seated user's windows" "$uu" "$ru"
    else
        fail "the seated user lists no windows: there is nothing for root's list to be compared against"
    fi
    orig=$(root 'env -i /usr/bin/wmctrl -l 2>&1' || true)
    want "the real wmctrl, run the same way, cannot open a display" "annot open display|X connection" "$orig"
    uu=$(guest "wxrandr --query | awk '\$2==\"connected\"{print \$1}'" | tr '\n' ' ' || true)
    ru=$(root 'w=$(command -v wxrandr); env -i "$w" --query 2>/dev/null | awk "\$2==\"connected\"{print \$1}"' \
         | tr '\n' ' ' || true)
    if [ -n "$(printf '%s' "$uu" | tr -d ' ')" ]; then
        same "root with an empty environment names the same connected outputs" "$uu" "$ru"
    else
        fail "the seated user names no connected output: nothing to compare root's --query against"
    fi
    # The file the glob was added for, named: this is the one display manager that keeps it here.
    note "GDM's cookie: $(root 'ls -l /run/user/1000/gdm/Xauthority 2>&1' | tr -d '\r' || true)"
    note "the seated session's own XAUTHORITY: $(guest 'echo $XAUTHORITY' | tr -d '\r' || true)"
    # And the harder half of the same claim: with gnome-shell's environ unreadable the glob is the ONLY
    # route left, which is what the recon measured going to None.  `pkill -STOP` would stop the session;
    # naming the file is as far as a smoke should go, so the check is that root reads it directly.
    want "root can read that cookie and talk to the display with it" "^[0-9]+$" \
         "$(root "w=\$(command -v wdotool); env -i \"\$w\" search --onlyvisible --class '$EDITOR_CLASS' \
                  2>&1 | head -1" || true)"
}

# The input path X and Wayland share up to the kernel: our own uinput devices, created UNPRIVILEGED
# under the package's udev rule, with the X server as the consumer instead of the compositor.
phase_uinput() {
    want "the package's rule grants the seated user /dev/uinput" "^user:[a-z]" \
         "$(root 'getfacl -p /dev/uinput 2>/dev/null' || true)"
    local out win
    editor_start
    # --onlyvisible, and it is not a nicety: on this Xorg session gnome-text-editor owns TWO windows of
    # that class -- 0x00a00002 `gnome-text-editor` (never mapped) and 0x00a00004 `w11-smoke.txt (~/) - Text
    # Editor`, measured live on 2026-09-09 -- and `search --class` prints both, lowest first.  Activating
    # the unmapped one focuses nothing, and every keystroke below then lands wherever the pointer left the
    # focus.  Under Wayland this does not arise: the bridge lists toplevels and there is only one.
    out=$(await 30 '[0-9]' "wdotool search --onlyvisible --class '$EDITOR_CLASS' | head -1" || true)
    win=$(printf '%s\n' "$out" | grep -E '^[0-9]+$' | head -1)
    if [ -z "$win" ]; then fail "no $EDITOR_CLASS window to type into [$(ev "$out")]"; return 1; fi
    guest "wdotool windowactivate --sync $win" >/dev/null 2>&1 || true
    guest "rm -f $SMOKE_FILE; touch $SMOKE_FILE" >/dev/null || true
    # Forced onto our own code, because the handover would type this with XTEST and prove nothing about
    # uinput.  No sudo anywhere in the line: the whole point of the rule is that there is none.
    out=$(guest "W11_PASSTHROUGH=never wdotool type --delay 30 -- 'x11 uinput' 2>&1" || true)
    wantnot "wdotool creates its uinput devices without sudo" "cannot create uinput devices" "$out"
    sleep 1
    editor_save
    # gnome-text-editor's ctrl+s is asynchronous, and the file is read when it holds the text rather than
    # one second later: on a --reuse run of this instance on 2026-09-09 the read one second after ctrl+s
    # came back EMPTY and the same file held `x11 uinput` a moment after -- a fixed sleep here measures
    # the guest's disk and not the input path.
    local typed; typed=$(await 20 'x11 uinput' "cat $SMOKE_FILE 2>/dev/null" || true)
    # It has been run now, and it works: the X server hotplugs the device our own code creates (`xinput
    # list` shows `wdotool virtual keyboard` beside the XTEST one) and delivers its events to the focused
    # window like any other keyboard -- `x11 uinput` arrived in gnome-text-editor byte for byte on the
    # live session, 2026-09-09, with no sudo anywhere in the line.  So this is a `want` and no longer the
    # xwant the recon left, whose container had no /dev/uinput at all [M recon2/gnome-xorg.md 2].
    want "the keystrokes reach the focused X window" "x11 uinput" "$typed"
    note "the editor holds: $(ev "$(editor_text || true)")"
}
