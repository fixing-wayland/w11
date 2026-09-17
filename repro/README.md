# Reproducers for the KDE + sway stress pass

Run these on the *host*: `sh deploy-to-vm.sh <repo> <vm>` builds the zipapps and
installs the five these scripts use on a `vmctl` guest (`wmirror` has no reproducer
here), then
`vmctl user <vm> -- sh /tmp/<script>.sh` runs one as the desktop user
(`vmctl ssh <vm> -- sh ...` runs it as root). The two `kde-outreg-*` scripts
are the exception: they need no guest at all. What they exercise, how Plasma
6.7 publishes outputs, does have an image now (`stonking-kde`), but that one
is a moving development release, and these two keep the same path checkable on
a machine with no Plasma 6.7 on it.

| script | finding | image |
|---|---|---|
| `kde-1-probe-kwin-id-reuse.sh` | KWin hands out a script id a live script still owns (gdbus only, no wdotool) | any KDE |
| `kde-1-script-id-race.sh` | the same, through concurrent wdotool commands | any KDE |
| `kde-5-pointer-no-uinput.sh` | `getmouselocation` needed /dev/uinput for a pure query | any KDE |
| `kde-6-pointer-3head.py` | claim 1, measured: every corner and centre of every output of a three-head layout, asked for with `mousemove` and read back from KWin's own `workspace.cursorPos`. Exact everywhere but an output's top-left *pixel*, which a 1x1 KWin screen edge pushes back one pixel (and, with the stock Plasma 6 config, opens the Overview). Needs python3-dbus and PyGObject in the guest | `resolute-kde`, `noble-kde` |
| `kde-tools-3-argv0-oracle.sh` | what the real wmctrl/xprop print as their program name | any |
| `kde-xid-twins.py` | one X11 client, N windows sharing a pid, a class, a title and a rectangle: the tie the Plasma 6 xid matcher used to settle by coin flip. `WM_WINDOW_ROLE` carries the truth (KWin exposes it, the matcher ignores it), so `wwmctl -l` can be graded against it | `resolute-kde` |
| `kdex11-1-sddm-x-cookie.sh` | Plasma on X11: SDDM keeps the X cookie in `/tmp/xauth_*`, so from a root shell the handover found no cookie at all (run as **root**) | `noble-kde-x11` |
| `kde5-verify.sh` | SHADED on a native window, the X11 state fallback, WM_CLASS case, `-l -G` | `noble-kde` |
| `kde6-before.sh`, `kde6-verify-a.sh`, `kde6-verify-b.sh` | the Plasma 6.6 sweep | `resolute-kde` |
| `kde-keys-1-group-guess.sh` | typing and chords into a real Kate window under a German layout: right on one configured layout, wrong on `us, de` switched to German, where group 1 was assumed. The picture of the defect `kde-keys-3` closed, kept because the one-layout case has to keep working either way | `resolute-kde`, `noble-kde` |
| `kde-keys-3-live-layout.sh` | the same `us, de` session, once with each build: `type 'yz@'` arrives as `zy""` while group 1 is assumed and as `yz@` once KWin is asked for the live layout, with the group switched back and forth under one running daemon | `resolute-kde`, `noble-kde` |
| `gnome-keys-1-group-guess.sh` | GNOME's half of the same question, in a real `gnome-text-editor` window: one German source (where the notice used to fire on every command), `us, de` switched to German with the panel menu, and five sources with the fifth picked, which is where Mutter chunks the keymap. Each case is typed once with the build that assumed the group and once with the one that reads it | `resolute-gnome-iso`, `noble-gnome-iso` |
| `kde-keys-2-nonlatin-chord.sh` | the same on Greek: `type` warns and skips the Latin it cannot reach, and `key ctrl+s` did the opposite -- it silently pressed the US position, which is sigma there. The defect this found; it now warns the same way, and this is what shows it | `resolute-kde`, `noble-kde` |
| `kde-outreg-conformance.py` | every wire constant in `hacks/display/kwin.py` against the upstream protocol XML, run it when a new Plasma lands | none (host, needs network) |
| `kde-outreg-specfake.py` | a Plasma 6.7 compositor generated from that XML: the registry discovery path end to end, and its three failure modes | none (host) |
| `matrix-1-warandr-empty-env.sh` | `env -i warandr --command` | any |
| `sway-1-layout-flag-fr.sh` | `--layout us` ignored by the daemon (needs a French keymap to show) | `resolute-sway` |
| `sway-3-root-desktops.sh` | synthesized `_NET_CURRENT_DESKTOP` past the count (as root) | `resolute-sway` |
| `sway-verify.sh`, `sway-verify-b.sh` | tiled resize/move exit codes, `-d` ordering, the rest | `resolute-sway` |

## Reproducers for the wxrandr + warandr stress pass

The wire-level ones need nothing but the repo: `tests/fixtures/fake_wlr.py` is
a `zwlr_output_manager_v1` server that can be told to go quiet at three
different moments, and `fake_kwin_server.py` runs the suite's own FakeKWin as
a standalone server, so the real CLI (a real `Session`, a separate process)
can be pointed at either.  The two `-vm` scripts run *inside* a guest against
the real compositor: unpack a tree at `/home/test/<name>` and
`vmctl user <vm> -- bash /home/test/repro.sh <name>`.

| script | finding | needs |
|---|---|---|
| `disp-hostile1-wlr-goes-quiet.sh` | a wlroots compositor that answers `succeeded` and then stops used to hang the CLI for ever | nothing |
| `disp-hostile2-scale-to-nothing.sh` | a `--scale` that truncates the logical size below the advertised 16x16 minimum | nothing |
| `disp-hostile4-non-finite.sh` | `nan` / `inf` / `1e400` to every option that takes a number | nothing |
| `disp-hostile9-broken-stdout.sh` | a closed or full stdout exited **120**, a code no xrandr produces | `/dev/full` |
| `disp-kwin-hostile.sh <tree>` | the same hostile set against KWin's protocol; takes a tree so before/after can be compared | nothing |
| `disp-layout2-wlr-scale-placement.sh` | `--right-of` off by 1-10 px at 149 of 201 fractional scales on the wlr backend | a headless sway (`$SWAYENV`) |
| `disp-sway-vm.sh before\|after` | the five sway/wlr findings in one pass, against the real compositor | `resolute-sway` |
| `disp-gnome-vm.sh before\|after` | `--same-as` relocating the primary, and the adjacency message | `noble-gnome` |
| `gnome-confirm-display-change.sh` | the bridge answering GNOME's own *Keep these display settings?* dialog, the one `--persistent` raises for 20 s: the countdown reverting a layout nobody confirmed, `ConfirmDisplayChange(true)` keeping it and getting `monitors.xml` written, `(false)` putting it back and writing nothing, the same calls with no dialog on screen changing nothing at all, and the dialog's own Escape still working after the bridge has pressed one. Needs the bridge installed and re-logged-in; screendump case 1 from the host to see the dialog | `resolute-gnome-iso`, `noble-gnome-iso` |

## The scaling pass

Which pixel space `wdotool`'s pointer lives in, under fractional and HiDPI
scaling, measured on `resolute-gnome-iso` (26.04 / GNOME 50),
`noble-gnome-iso` (24.04 / GNOME 46) and `resolute-kde` (Plasma 6.6): scale 1,
2 and 1.5, on one head and on two of different scales, one VM at a time.

Every target is read four ways, and the last of them shares nothing with the
tools:

| reading | where it comes from |
|---|---|
| `daemon` | the input daemon's model of what it injected (`DaemonClient.pointer()`), read first because `getmouselocation` reseeds it |
| `query` | `wdotool getmouselocation` |
| `comp` | the compositor itself: Mutter's `global.get_pointer()` through the bridge, KWin's `workspace.cursorPos` |
| `hw` | the **KMS cursor plane**, `/sys/kernel/debug/dri/*/state`, device pixels on the scanout (vm/README.md's oracle; the path is not always `dri/0`). Where the compositor draws its own cursor instead of using that plane, KWin at 200%, whose sprite does not fit the 64x64 virtio-gpu one, a QMP screendump differenced against a parked one |

The KMS oracle is blind in two places, and both are the oracle's doing rather
than a coordinate's: the plane goes dark within one hotspot of a head's
top-left corner (virtio-gpu cannot place it at a negative `crtc-pos`), and for
the four pixels either side of a seam Mutter keeps the sprite on the left-hand
head.

| script | what it is |
|---|---|
| `scale-probe.py` | guest side, run as **root** against a repo tree at `$W11` (default `/root/w11`): takes the four readings for a list of targets and prints JSON. A target is `[x, y]`, or `{"frac": [fx, fy]}` of the layout box, or `{"mon": i, "off": [dx, dy]}` from one monitor's own origin |
| `scale-runmatrix.py` | host side: `scale-runmatrix.py <vm> <outdir> <configs.json>` applies each layout (heads, scales) and runs the probe in the guest |
| `scale-spaces.py` | scores every reading against BOTH candidate maps, `dev = (asked - origin) * scale` and `dev = asked - origin`, and reports the one whose residual is *constant*. It determines the pixel space rather than assuming one: the constant is the cursor hotspot, the spread is the error |
| `scale-summary.py` | one line per config: the worst error each reading showed |
| `scale-shotcursor.py` | the screendump oracle, for a compositor that composites its own cursor |
| `scale-wldump.py` | `wl_output` and `zxdg_output_v1` side by side, the two numbers `_wayland_bbox()` chooses between |
| `scale-1-gnome46-xdg-output-stale.sh` | **the Mutter defect.** Walks GNOME 46 through turning "Fractional Scaling" on while the monitor is already at 200%, printing the layout box, Mutter's two reports of the same thing and where the cursor really went, at each step. The advertised layout goes stale, the cursor still lands where it was asked for (Mutter maps absolute motion across the stale rectangle too), and `getdisplaygeometry` is the thing that is then wrong |

Everything agreed to 0 px on 26.04 and on Plasma 6.6, at every scale and on
both layouts, and on 24.04 in each of its two layout modes taken by itself.
The *change* between 24.04's two modes leaves Mutter advertising a layout it
has stopped drawing; the pointer follows the advertised one and still lands
where it was asked, and `getdisplaygeometry` is what goes wrong. See
`tests/test_scale_spaces.py::StaleXdgOutput`, which replays it on the wire.
The raw readings behind all of it are in `tests/fixtures/scaling/`.
