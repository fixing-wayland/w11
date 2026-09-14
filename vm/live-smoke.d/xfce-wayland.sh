# live-smoke.d/xfce-wayland.sh -- Xfce 4.20's Wayland session, whose compositor is labwc.
#
# labwc.sh is sourced whole: `startxfce4 --wayland` runs
# `labwc --config-dir ~/.config/xfce4/labwc --config .../rc.xml --session xfce4-session`, and
# the registry of that labwc is byte-identical to a bare one -- the recon diffed the two dumps
# and found no differences, so Xfce adds no protocol and removes none [recon2/xfce-wayland 1].
# xfwm4 does not run at all; labwc is the window manager, and xfce4-panel and xfdesktop draw
# through zwlr_layer_shell_v1.  Everything about windows, workspaces, input, display and
# mirror is therefore labwc.sh's, and this file adds only what Xfce itself brings.
#
# The terminal is xfce4-terminal, and `-x` (not `-e`) is the flag that makes it execute the
# rest of the command line rather than a single string.
EDITOR_CLASS=xfce4-terminal
EDITOR_TERM=xfce4-terminal
EDITOR_TERM_EXEC=-x

# shellcheck source=live-smoke.d/labwc.sh
. "$STEPS/labwc.sh"

# labwc's list plus the INVERSE of xfce.sh's handover phase, which is the reason both Xfce
# flavors exist.  resolute-xfce and resolute-xfce-wayland install the same xubuntu-desktop and
# differ in one LightDM key (autologin-session), so what differs between their runs is the
# session type and nothing else.  On the X11 half every tool execs the distribution's original
# and xfce.sh proves it with recorder shims.  Here the claim is the opposite one, and it needs
# the same shims to be worth anything: `session_kind()` answers `wayland` from WAYLAND_DISPLAY
# plus a live socket before any other test, so no shim may ever be called
# [recon2/xfce-wayland 2].
SMOKE_PHASES="busrec install passthrough windows wm proxy input display mirror root nodialog"

#: Where the shims record a call.  The same path xfce.sh uses, on purpose: the two files are
#: the two halves of one measurement and a reader comparing their logs should not have to
#: translate a path.
ARGV_LOG=/tmp/w11-argv.log

# xfce.sh's install_shims, re-stated rather than sourced.  Sourcing that file would also bring
# its SMOKE_PHASES, its EDITOR_CLASS=xterm and its editor_start, every one of which is wrong
# for a Wayland session -- and its phase_passthrough asserts the handover DOES happen, which
# is the claim this file exists to contradict.  The shape is deliberately identical: shims in
# /usr/local/bin (which comes before /usr/bin, and which passthrough walks past anything that
# is us to find), one per tool that has an original, each recording its own argv and then
# exec'ing the real binary.
install_shims() {
    root 'for t in xdotool wmctrl xprop xrandr; do
            [ -x /usr/bin/$t ] || continue
            { echo "#!/bin/sh"
              echo "echo \"$t:\$*\" >> /tmp/w11-argv.log"
              echo "exec /usr/bin/$t \"\$@\""
            } > /usr/local/bin/$t
            chmod 0755 /usr/local/bin/$t
          done' >/dev/null 2>&1 || true
}

remove_shims() {
    root "rm -f /usr/local/bin/xdotool /usr/local/bin/wmctrl /usr/local/bin/xprop /usr/local/bin/xrandr" \
        >/dev/null 2>&1 || true
}

phase_passthrough() {
    install_shims
    guest "rm -f $ARGV_LOG; touch $ARGV_LOG" >/dev/null || true
    # The clone prints the version it clones -- xdotool 4.20260303.1 -- which Ubuntu's own
    # xdotool 3.20211022.1 could never print, so this one line separates "ours answered" from
    # "the original answered" without reading the log at all [recon2/arch 2].
    want "wdotool --version is OURS on a Wayland session, not the archive's 3.20211022.1" \
         "4\.20260303\.1" "$(guest 'wdotool --version' || true)"
    # ...and the log is the second, independent half: four tools, each with an original on
    # PATH, none of which may be reached.
    guest 'wwmctl -l' >/dev/null 2>&1 || true
    guest 'wxrandr --query' >/dev/null 2>&1 || true
    guest 'wxprop -root _NET_CLIENT_LIST' >/dev/null 2>&1 || true
    guest 'wdotool search --class xfce4-terminal' >/dev/null 2>&1 || true
    same "not one recorder shim was called: nothing handed over" "" \
         "$(guest "cat $ARGV_LOG 2>/dev/null" | tr -d ' \r\n' || true)"
    want "wxrandr --print-backend is the compositor's protocol and not xrandr" "^wlr$" \
         "$(guest 'wxrandr --print-backend' | tr -d ' \r' || true)"
    # wmirror on the X11 half says "this is an X11 session" before anything else; here it must
    # find a compositor to capture from instead.
    want "wmirror --check finds a capture protocol rather than announcing an X11 session" \
         "capture: " "$(guest 'wmirror --check 2>&1' || true)"
    # Recorded, not asserted: the session leader's environment, which is where
    # w11common/passthrough.py's session_kind() and vmctl's USER_WRAPPER both look.  Measured
    # in the recon on this very session: XDG_SESSION_TYPE=wayland, XDG_CURRENT_DESKTOP=XFCE,
    # DESKTOP_SESSION=xfce, WAYLAND_DISPLAY and DISPLAY set, and NO XAUTHORITY at all -- labwc
    # starts Xwayland without -auth, exactly as sway does [recon2/xfce-wayland 1].
    note "session env: $(guest 'echo type=$XDG_SESSION_TYPE desktop=$XDG_CURRENT_DESKTOP \
wl=$WAYLAND_DISPLAY display=$DISPLAY xauth=${XAUTHORITY:-none}' || true)"
    remove_shims
}
