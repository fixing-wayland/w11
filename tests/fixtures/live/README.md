<a id="testsfixtureslive-bytes-off-the-real-desktops-on-the-qemu-rig"></a>

# tests/fixtures/live: bytes off the real desktops on the QEMU rig

Captures taken with `vm/live-smoke.d/guest-capture.sh` and
`guest-capture-winwm.sh` inside a `vmctl` guest, in the transcript format the
smoke's stand-in reads:

    ### <the exact command>
    <its output, byte for byte>
    ### rc=<exit status>

The transcripts contain captured command output, except for the project-name
normalization described below and the two explicitly identified oracle fixtures.
`vm/live-smoke.d/fake-vmctl` replays these recordings so the checks in
`vm/live-smoke.sh` can run without booting a guest. The offline self-test verifies
that the checks accept successful recordings and reject deliberately broken ones.

One class of byte in here is **not** the desktop's any more. On 2026-09-10 the
project was renamed from `fuckwayland` to `w11`, and the rename rewrote our own
names inside every recording so that the replays still match the renamed steps:
the package and the .deb file name in the `dpkg`/apt lines, the extension UUIDs
and their `Name:`/`Description:` text, the bus names, and `W11_PASSTHROUGH` in
the command lines. Those bytes are ours, not a capture, until each flavor is
re-recorded, and the widths went with them, because `dpkg -l` sized its name
column for an eleven-character package (`ii  fuckwayland    0.4.0` became
`ii  w11    0.4.0` in `noble-gnome-46.0-capture.txt`, which is not what dpkg
would print for `w11`). Everything the compositors, the portals and the distro
printed about themselves is untouched, and all eight `*-replay.txt` files still
replay with no failure.

### `noble-gnome-46.0-capture.txt`

From instance `gnome46` (noble-gnome golden, GNOME Shell 46.0 on Ubuntu
24.04.4, `w11 0.4.0` out of `release/`, three heads), 2026-09-08 02:34
UTC. It holds the shapes every GNOME step asserts against: `getwindowgeometry`
before and after the maximize pair, `wwmctl -l` / `-l -G` / `-d`, `wxprop -id`
and `-root`, `wxrandr --listmonitors` / `--query` / `--backends` /
`--print-backend --verbose`, Mutter's `Logical monitors not adjacent` refusal
with the re-place hint, `--gnome-overlap-status`, and the six `overlap check`
lines of a `--dryrun --unsafe-gnome-overlap`.

### `noble-gnome-46.0-windows-wm-replay.txt`

The same instance, 2026-09-08 02:38 UTC: the `windows` and `wm` phases'
commands **in the order the phases ask them**, which is what `fake-vmctl`
needs. `wdotool getwindowgeometry` is asked four times across the two phases
and has to answer `100,100 800x600`, `100,100 800x600`, the maximized
`66,32 1854x1048`, and `100,100 800x600` again, the last of those is b7a60f0.

Two facts worth reading straight out of the first file, because prose elsewhere
in the tree paraphrases them: `WM_CLASS` on the GNOME text editor is
`org.gnome.TextEditor` (so `wdotool search --class gnome-text-editor` matching
nothing is X11 behaviour, not a bug), and `/dev/uinput` reads `root root 660`
while the package is installed, the mode is the ACL mask, the `user:test:rw-`
entry underneath is what udev's `uaccess` tag put there.

The KWin and sway equivalents are not here yet: each needs its own golden
booted, and on this host that is one VM at a time (`vm/live-smoke.d/guest-capture.sh
kde` / `sway` produces them).

### `NOT-YET-RUN`

Which step files have no recording yet, one token per line, the step file's own
name without `.sh`. Every file in `vm/live-smoke.d/<token>.sh` is either listed
there or has a `<flavor>-…-replay.txt` here, never both and never neither;
`tests/test_live_smoke.py` (R31) fails on a file in neither place, and
`selftest-offline.sh` repeats the check. A recording's flavor is the longest
flavor name in `vm/flavors/` that its file name starts with, and that flavor's
`# vmctl-desktop:` header is the token it covers, so
`noble-gnome-46.0-windows-wm-replay.txt` is `gnome`'s.

## The two oracle fixtures

`vm/live-smoke.d/oracle.py` is the only file here that is not replayed through
`fake-vmctl`: it is parsed directly. Most of its branches are sliced against
`tests/fixtures/vm/` (batch 2's `wlr-randr-*`, `hyprctl-monitors-*`,
`cosmic-randr-*`); two had no fixture anywhere, so they live here.

### `oracle-wayfire-list-outputs.json`

`window-rules/list-outputs` off the Wayfire recon's own IPC
(`recon2/wayfire/captures/window-rules_list-outputs.json`, 2026-09-08): two
heads, `HEADLESS-1` 1280x720 at 0,0 and `HEADLESS-2` 1920x1080 at 1280,0, each
with the 3×3 workspace grid Wayfire gives every output. The `geometry` rect is
in layout coordinates and `workarea` is not, a parser reading the wrong one
answers `0,0` for both heads, which this file is shaped to expose.

### `oracle-muffin-getcurrentstate.txt`

**Derived, not recorded.** What is measured in it is Cinnamon's: nested muffin
6.4.13 reported one output `LVDS1` at 800x600+0+0, and
`org.cinnamon.Muffin.DisplayConfig`'s `GetCurrentState` signature is
byte-for-byte Mutter's (`recon2/cinnamon` §3.1, §4). What is derived is the text
form: the bytes here are what GLib's own printer emits for a variant of that
signature, measured on this host on 2026-09-08 with
`GLib.Variant(...).print_(True)`, which is exactly what `gdbus call` prints.
That printing is the point of the fixture: `uint32 0` for the transform field
and `@a{sv} {}` for an empty dictionary are what a `gdbus`-parsing oracle has to
survive, and the shipped one did not.

### `resolute-sway-1.11-windows-wm-proxy-replay.txt`

resolute-sway (sway 1.11, Xwayland 24.1.10; the originals are the 26.04 golden's
test-support packages, `xdotool` 1:3.20160805.1 and `wmctrl` 1.07 per the package table
in vm/README.md; `xrandr`'s version was not recorded and is not guessed here),
2026-09-11, two 1920x1080 virtual heads, recorded off a live run with
`vm/live-smoke.d/capture-from-run` -- sway.sh overrides `phase_windows` and `phase_wm`,
so `guest-capture.sh`'s fixed command list is the wrong shape for it. 94 sections; the
run was 27 pass, 0 fail and the replay is 27 pass, 0 fail. It is the first recording of
the `proxy` phase: the ORIGINAL xdotool, wmctrl and xprop answering through `xw11` about
a `foot` window with no X window behind it, and `xrandr --output Virtual-2 --pos
1920x200` moving sway's own head and putting it back.

    CAPLOG=/tmp/cap.txt LIVE_SMOKE_VMCTL=vm/live-smoke.d/capture-from-run \
        vm/live-smoke.sh resolute-sway --reuse --keep --phases windows,wm,proxy
