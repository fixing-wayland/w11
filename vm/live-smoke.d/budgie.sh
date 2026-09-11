# live-smoke.d/budgie.sh -- Ubuntu Budgie 10.10.2, which on 26.04 is labwc under a panel.
#
# labwc.sh is sourced whole for the same reason xfce-wayland.sh does it: Budgie 10.10 dropped
# its Mutter fork (magpie) and became protocol-first over wlroots, its session file is
# `Exec=/usr/bin/startbudgielabwc` with DesktopNames=Budgie, and startbudgielabwc ends in
# `exec labwc --config-dir ~/.config/budgie-desktop/labwc -S budgie-desktop`.  budgie-core
# 10.10.2 ships no /usr/share/xsessions entry at all and no budgie-wm [recon2/budgie 0].  The
# four protocols Budgie itself names as required -- wlr-foreign-toplevel-management,
# wlr-layer-shell, wlr-output-management, xdg-output -- are the ones this repo's wlr paths
# already speak, and the fifth, ext-workspace-v1, is what backend_wlr's workspace half reads.
#
# `foot` is the terminal here too, and it is in the flavor's EXTRA_PKGS on purpose:
# budgie-desktop Depends on no terminal emulator at all, so using Budgie's default would mean
# this file and labwc.sh differed in the client as well as the desktop [recon2/budgie 5].
#
# One thing worth knowing while reading a red run: budgie-desktop 10.10.2 DEPENDS on xdotool,
# wlr-randr and wdisplays, so the real X11 tool this project replaces is installed on this
# golden by the desktop itself and not by EXTRA_PKGS.  A handover would still be wrong here --
# the session is Wayland -- and xfce-wayland.sh is where that is proved.

# shellcheck source=live-smoke.d/labwc.sh
. "$STEPS/labwc.sh"

# labwc's list plus one phase of Budgie's own.
SMOKE_PHASES="busrec install windows wm proxy input display mirror budgie root nodialog"

# Budgie runs a display service of its own -- org.buddiesofbudgie.Services on the SYSTEM bus,
# with an Outputs object -- and nothing in the recon established what it does when something
# else moves an output.  If it restores its saved layout ten seconds later, then every
# `wxrandr` apply on this desktop is a race, and the honest place to find that out is here.
#
# So this phase is a MEASUREMENT first: apply, wait, re-read, and record both readings.  The
# check is the weakest one that is still a claim -- that the apply was not undone -- and it is
# an xwant until a run has said what the service does, because a red line here today would be
# reporting an unmeasured Budgie behaviour as a failure of ours.
phase_budgie() {
    local pair first second before after
    pair=$(display_pair); first=${pair%% *}; second=${pair#* }
    if [ -z "$second" ]; then
        note "one head only: the layout-restore question needs a second head (--heads 2)"
        return 0
    fi
    # The SESSION bus, as the seated user.  The first run of this flavor (2026-09-09) asked the system bus
    # as root and got nothing back, which read as "the service is not there" when it is: every
    # org.buddiesofbudgie.* name on this golden is on user 1000's bus, and Services itself is owned by
    # /usr/libexec/budgie-desktop/org.buddiesofbudgie.Services (pid 1703, connection :1.13).
    note "org.buddiesofbudgie.* on the session bus: \
$(guest 'busctl --user list --no-pager 2>/dev/null | grep -i buddiesofbudgie' | tr '\n' '|' || true)"
    guest "wxrandr --output $second --below $first" >/dev/null 2>&1 || true
    sleep 2
    before=$(opos "$second")
    beside below "$first" "$second" "--below puts $second under $first (immediately)"
    sleep 10
    after=$(opos "$second")
    note "$second at $before immediately after the apply, at $after ten seconds later"
    if [ -z "$before" ]; then
        # Without a position to compare against, a check on `^$` would pass on an equally empty second
        # reading and report agreement between two failures to read anything.
        fail "wlr-randr gave no position for $second after the apply: the wait below has nothing to compare"
    else
        # A want since the first run of this flavor answered the question it was an xwant for: on
        # resolute-budgie (Budgie 10.10.2 over labwc 0.9.3, 2026-09-09) `--output Virtual-3 --below
        # Virtual-2` left Virtual-3 at 1920,1080 immediately and at 1920,1080 ten seconds later, with
        # org.buddiesofbudgie.Services running on the session bus the whole time.  Budgie's display
        # service does not restore a layout it did not write, so a wxrandr apply here is not a race.
        want "Budgie's own display service leaves a wxrandr apply alone" \
             "^$(printf '%s' "$before" | sed 's/[.[]/\\&/g')$" "$after"
    fi
    guest "wxrandr --output $second --right-of $first" >/dev/null 2>&1 || true
    sleep 2
    # The panel is a layer-shell surface and must NOT be in the window list: budgie-panel draws
    # through zwlr_layer_shell_v1 v4, which publishes no foreign toplevel.  A panel appearing
    # in `wwmctl -l` would mean the backend is listing surfaces rather than toplevels.  The
    # listing is checked to be a listing FIRST: `wantnot` on a command that failed outright
    # would pass on an empty string and call it proof.
    local wins; wins=$(guest 'wwmctl -lx' || true)
    want "wwmctl -lx still lists windows here (the wantnot below is worthless on an empty list)" \
         "^0x[0-9a-f]+ " "$wins"
    wantnot "budgie-panel is a layer-shell surface and is not among them" "budgie-panel" "$wins"
}
