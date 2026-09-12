# Changelog

Every claim in this file was measured on the VM rig, or on a real desktop, before it
was written down. The README keeps one short section per version; this is the long
form.

## Unreleased

The version in `w11common.VERSION` is still 0.4.0 and nothing below has been released.
Everything here was measured on the rig or on a real desktop, and the flavor, the run
and the bytes are named wherever a number is.

- **The project is called w11 now.** It was written under the name `fuckwayland`; the
  repository, the Python packages (`fwcommon` is `w11common`), the .deb/.rpm/PKGBUILD and
  the flake outputs all carry the new one, and so does everything they install: the GNOME
  extension UUIDs `w11-bridge@w11` and `w11-overlap@w11` with their GIR namespaces
  `W11Overlap14`/`18`/`51`, the bus names `org.w11.Bridge1`, `org.w11.Overlap1` and
  `org.w11.KWin1` on `/org/w11/...`, `60-w11-uinput.rules`, `w11-uinput.conf`,
  `w11-enable-bridge.desktop`, `/usr/lib/w11/`, `/var/lib/w11/installed`, `~/.config/w11/`,
  and every environment variable the tools read (`W11_PASSTHROUGH`, `W11_SYSTEM_STAMP`,
  and the rig's `W11_ORACLE_PATH`, `W11_NIX_LIVE`, `W11_SHIM_SEAMS`, `W11_PARITY_DISPLAY`,
  `W11_ROOT`). No behaviour moved with the name and no test lost a claim. There is no
  compatibility shim and nothing reads the old names. Current packages no longer
  declare automatic replacement of the legacy package. Uninstall that package before
  installing `w11`, since both own some of the same files. For Nix, replace the old
  flake input. The version remains 0.4.0.

- **Hyprland gets a first-class backend on both sides.** `wdotool/backend_hypr.py` and
  `wxrandr/hypr.py` speak Hyprland's own request socket, one connection per request,
  where the generic wlroots backend had reported the whole output as every window's
  rectangle, pid 0 and desktop -1. Window ids are a hash of the compositor's `address`,
  so they no longer move when another window closes; `getwindowgeometry`, `windowmove`
  and `windowsize` are real; XWayland windows are listed under their real X id.
  `wxrandr --print-backend` answers `hypr` and not `wlr`, and that is not a preference:
  Hyprland advertises `zwlr_output_manager_v1` v4 and takes exactly one apply per
  session through it, the second times out after 10 s having changed nothing, with
  `wlr-randr`, the reference client, hanging for ever on the same request. Measured on
  0.53.3 and again on 0.56.2.
- **A Wayfire backend over Wayfire's JSON IPC.** Where the generic wlr backend refused
  `windowmove`, `windowsize`, `windowraise`, `windowlower`, `getwindowpid`,
  `selectwindow` and every desktop command, Wayfire with `plugins = ipc ipc-rules` now
  answers all of them. `wwmctl -l -x` prints the real X window id and `xterm.XTerm`
  where it printed a synthetic id and `XTerm.XTerm`. Wayfire has a real `windowlower`,
  which sway has never had, and a `getmouselocation` that answers from the compositor
  before anything has warped the pointer.
- **Cinnamon, both session types.** `org.Cinnamon.Eval` carries the window-management interface with
  nothing installed, and `wxrandr --backend cinnamon` is Mutter's DisplayConfig under
  Muffin's bus name, eight behaviours that used to be keyed on the token `mutter` are
  keyed on the implementation's flavour instead, so `--dryrun`, `--noprimary`,
  `--listmonitors`, the overlap-status sentences and `--brightness`/`--gamma` all answer
  on Cinnamon where they used to answer about GNOME or die. `--persistent` writes
  `~/.config/cinnamon-monitors.xml` behind Cinnamon's own *Keep these display settings?*
  dialog, and that dialog has now been answered live over Eval.
- **A COSMIC backend, and the generic wlroots backend made honest.** cosmic-comp publishes no
  `zwlr_foreign_toplevel_manager_v1` at all, so `wdotool/backend_cosmic.py` drives the
  COSMIC toplevel protocols instead: real workspaces, ids minted from the 32-character
  `identifier`, and activate/close/maximize/minimize/fullscreen gated on the manager's
  own capability array. On the generic wlr backend the four geometry refusals stopped
  borrowing sway's tiling excuse and now name the protocol; desktops work wherever the
  compositor publishes `ext_workspace_manager_v1`; toplevels are joined to
  `_NET_CLIENT_LIST` so XWayland windows appear under their real X ids; and a compositor
  that accepts a request and does nothing, river 0.4, is waited for and reported
  rather than believed.
- **i3 is a dialect and not a lie.** The sway backend asks `GET_VERSION` once and knows
  which compositor it is talking to: every id is the window's X id and not i3's 47-bit
  container pointer, `windowmove`/`windowsize` work on a floating i3 window,
  `getwindowpid` answers from `_NET_WM_PID`, `--onlyvisible` matches, and any
  `wxrandr` apply is refused up front in one line, because i3 has no `output` command
  and the X server owns the layout there. The four `sway:`-prefixed refusals say `i3`
  on i3.
- **Five desktops answer which keyboard layout is live, where two did.**
  `hyprctl devices` on Hyprland, `wayfire/get-keyboard-state` on Wayfire and
  `org.cinnamon.desktop.input-sources` through Eval on Cinnamon joined KWin and GNOME's
  input-sources setting; COSMIC needs no reader at all, because
  `zcosmic_keyboard_layout_manager_v1` puts the group on the wire the way sway does.
  Neither of the two write methods is ever called: one of them recompiles Wayfire's
  keymap as the selected layout duplicated, and the other moves the session's own state.
- **Every refusal names a route and its cost.** A missing feature is written down as
  **not yet** plus the lowest-numbered route on [AGENTS.md](AGENTS.md)'s ladder and what
  that route costs, in the tools' own stderr as well as in these documents.
  `windowreparent` used to print a sentence saying reparenting was not possible; it now
  names one `XReparentWindow` over the X11 connection for an XWayland window and a patched
  compositor for a native one, and stays a warn-and-succeed.
  `tests/test_one_rule.py` is the gate.
- **A Fedora package and an Arch package.** `packaging/rpm/` and `packaging/arch/`,
  built by `scripts/build-rpm.sh` and `scripts/build-pkgbuild.sh`, `build-deb.sh`'s
  siblings, same options, same first gate: the version has to agree everywhere before
  anything runs, and it is now written in eight places rather than four. Neither package
  is committed and neither is published yet, though since the tree became BSD-2-Clause
  nothing about the licence stands in the way: both carry the BSD-2-Clause tag and both
  build scripts refuse a build whose tag disagrees with `LICENSE`'s own SPDX header.
- **The udev procedure is one procedure in three dialects.** `%post`/`%postun`,
  `post_install`/`post_upgrade`/`post_remove` and dpkg's `postinst`/`postrm` are compared
  phrase for phrase by the tests, so a change to any one of them turns the other two red.
  The ordering reason is the same on all three: the trigger has to follow the reload,
  because both Fedora's systemd-udev file trigger and Arch's
  `35-systemd-udev-reload.hook` run *after* the scriptlet.
- **`debian/enable-bridge` and its `.desktop` are now `packaging/common/`.** They were
  never Debian-specific, a per-user gsettings enable and an XDG autostart entry, and an
  Arch recipe reaching into a directory named for dpkg is what said so. Same bytes, one
  copy, three packagings.
- **The flake is more than one derivation.** `nixosModules.default` and
  `homeManagerModules.default`, six packages instead of one (and 330.9 MiB less closure
  for anyone who only wants the CLI tools), `meta.mainProgram` so `nix run` works at all,
  and five `checks` so `nix flake check` stops passing with zero tests.
  `packages.xdotool` and `packages.wmctrl` exist, so `scripts/parity-oracle.sh`'s
  instructions are true, and `meta.license` is `bsd2` now that `LICENSE` exists.
- **The rig grew from thirteen flavors to 38, over four distributions.** Ubuntu gained
  `resolute-hypr`, `resolute-wayfire`, `resolute-labwc`, `resolute-xfce-wayland`,
  `resolute-budgie`, `resolute-lxqt-wayland`, `resolute-cinnamon`,
  `resolute-cinnamon-wayland`, `resolute-mate`, `resolute-i3`, `resolute-lxqt` and
  `noble-gnome-x11`; Fedora 43 and 44, Arch and NixOS arrived with GNOME, KDE, sway,
  Hyprland, COSMIC and river behind them. `vm/build-image.sh` is package-manager and
  display-manager neutral in one file, `vm/build-nixos-golden.sh` is a third golden
  builder, and `arch-river` is the rig's first from-source build step, pinned to a commit
  and a sha256.
- **The smoke has a package axis per distribution.** `vm/live-smoke.sh --pkg` installs
  the flavor's own package, the `.deb` on Ubuntu, the rpms on Fedora, the
  `.pkg.tar.zst` on Arch, and on NixOS nothing at all, because the package is in the
  image; `--remove` is the mirror, and on NixOS it is a `switch-to-configuration test`
  into a `without-w11` specialisation. Two phases belong to a distribution
  rather than to a desktop and the driver appends them itself: `selinux` on Fedora and
  `pkgverify` (`rpm -V`, `pacman -Qkk`) on both.
- **An X11 proxy, so the originals work on Wayland.** `xw11` is a seventh command and it
  is not a clone of anything: it binds a display number, forwards every byte to the
  session's X server, and answers the requests that server has no answer for, because the
  windows they are about are native Wayland toplevels that were never X windows.
  Measured on headless sway with Xwayland 24.1.10 on 2026-09-11: the pinned, unmodified
  `xdotool 4.20260303.1` printed `6291457` for `search --class footw` — a `foot` window
  with no Xwayland window at all — and then `getwindowname` on that id printed its title.
  `xdotool get_desktop`, `set_desktop`, `get_num_desktops` and `wmctrl -d` exited **1** on
  sway with *Your windowmanager claims not to support `_NET_CURRENT_DESKTOP`* and exit
  **0** through the proxy; `xprop -spy -id <window> WM_NAME`, `xprop -spy -root
  _NET_ACTIVE_WINDOW` and `xdotool behave <window> mouse-enter` were RC 124 under a
  timeout and now print their line; `xrandr --output HEADLESS-1 --mode 800x600` was
  `BadMatch` and exit 1 and now moves sway's own output. `xrandr -q` through the proxy is
  byte-identical to `xrandr -q` without one, and the whole parity oracle runs through a
  pass-through proxy differing only in the `Ran N tests in` lines
  (`W11_PARITY_PROXY=1 sh scripts/parity-oracle.sh`). What it does not do yet is a table
  with a route and a cost per row, in [docs/XW11.md](docs/XW11.md). The rig has a `proxy`
  phase of its own now, after `wm` on every Wayland flavor and on none of the X11 ones,
  and its first run was **resolute-sway (sway 1.11, Xwayland 24.1.10) on 2026-09-11: 27
  pass, 0 fail** -- the original xdotool, wmctrl and xprop answering about a `foot`
  window with no X window behind it, and `xrandr --output Virtual-2 --pos 1920x200`
  moving sway's own head and moving it back.
- **On a Wayland session the four wrapped tools now run the originals.** `wdotool`,
  `wwmctl`, `wxprop` and `wxrandr` hand over on Wayland the way they already did on X11,
  with `DISPLAY` pointing at `xw11`, and the first call that needs a proxy starts one —
  53 to 203 ms over six measured runs, 105 ms typical, then nothing until it idles out
  fifteen minutes after its last client. Six rules decide it and the first five cost no
  process at all; `W11_PROXY=never` (or `WDOTOOL_PROXY=never`, and the three siblings)
  asks for our own code, `W11_PROXY=always` says the original's bytes or exit 127, and a
  command carrying one of our own options — `--layout`, `--vkbd`, `wdotool keys`,
  `--persistent`, `--backend`, the five `--*gnome-overlap*` flags — always runs the clone.
  A help or version request runs the original and starts nothing, because a usage string
  is not worth a process that outlives the command by fifteen minutes. The package now
  **Recommends** `xdotool`, `wmctrl`, `x11-utils` and `x11-xserver-utils` rather than
  suggesting them, on all three packagings.
- **5435 tests**, up from 4146, the new ones being the four new window and display
  backends and every desktop behind them, the rig's own scripts sliced and run against
  stubbed package managers and display managers, the three distribution packagings read
  back out of what they build, the flake and its NixOS module, and the CI workflow and
  its three container images read as the text they are. `python3 -m unittest discover -s
  tests` is what counts them, and the number is pinned in four documents by
  `tests/test_docs_numbers.py`.

## Version 0.4

Everything here was measured on the rig or on a real desktop, and most of it was found
by using the tools on one rather than by reading them.

- **The input daemon ends itself.** It checks its own socket by inode every fifteen
  seconds and exits when that socket is gone or has been replaced, and it exits after
  fifteen minutes with no client. Before that it ran for ever: a logout clears
  `/run/user/<uid>`, and the daemon left behind was unreachable, held
  `wdotool.sock.lock`, and made every later `wdotool` command in that boot fail with
  "cannot start wdotool daemon". Measured on GNOME, KDE and sway, including through a
  real `loginctl terminate-session`: the runtime directory goes at t+15s and the
  daemon with it, and nothing is left behind.
- **A key combination the layout cannot produce is refused.** `key ctrl+s` on a Greek
  layout warns that `s` is not reachable and presses ctrl alone, where it used to press
  the US position of that key, which on Greek is σ. Measured end to end in Kate on both
  Plasma generations (nothing saved, exit 0, empty stderr, before) and against a sway
  binding on the US `s` position, which is the objective oracle: `us` creates the file,
  `gr` does not and warns, `us,gr` creates it again.
- **The layout notice reaches every command.** A session with two layouts warned once
  and then typed the wrong characters in silence. It now warns on every command that
  types, and `WDOTOOL_XKB_GROUP` is read from the environment of the *command* and
  carried to the daemon with the text, because the daemon keeps the environment it was
  spawned with and outlives it: the pin the notice asks for used to be ignored by a
  daemon that was already running. That notice announced a *guess*: the next three
  entries are what became of the guess, and the fourth is the one call it costs.
- **wdotool reads the active keyboard layout on KDE instead of assuming it.** A
  session with two layouts configured and the second one switched on typed the first
  one's characters and printed a notice saying which layout it had assumed: measured on
  Plasma 6.6 with `us, de` switched to German, `wdotool type 'yz@'` arrived in Kate as
  `zy""`. KWin publishes the live layout on the session bus, `org.kde.KWin` `/Layouts`
  `org.kde.KeyboardLayouts.getLayout`, a 0-based index into the configured list, which
  is the keymap's group order name for name, so the active group is that index plus
  one, and wdotool now asks. The same string arrives as `yz@`, byte-exact, on Plasma
  6.6 *and* 5.27 (the earlier claim that 5.27 needed `org.kde.kded5
  /modules/keyboard` was wrong: KWin 5.27 has `/Layouts` too), a switch made while the
  daemon is running is followed command by command, and the notice says nothing where
  nothing is assumed, `wdotool keys explain` reports `group 2 of 2, from wayland +
  kwin` rather than `group 1 of 2 (assumed)`. The bus is opened only where the group
  would otherwise be a guess, so a plain US session, a one-layout session and GNOME's
  `us,us` never open one; KWin is asked and kded never is, because both kded copies of
  that interface crash on `getLayout`; and every failure, no bus, no KWin, an older
  KWin without the object, an index the keymap cannot hold, leaves the old guess and
  the old notice exactly as they were, measured unchanged on a two-source GNOME
  session. `WDOTOOL_XKB_GROUP` still outranks it. (GNOME is the entry below; nothing
  changes on sway or X11.)
- **And on GNOME, from the setting the shell keeps.** The same defect, the same
  measurement: `us, de` configured and German switched on, `wdotool type 'yz@'`
  arrived in a real `gnome-text-editor` window as `zy"` on GNOME 50.1 *and* 46.0.
  GNOME publishes the answer in `org.gnome.desktop.input-sources`, which
  `xdg-desktop-portal` serves on the session bus, so wdotool now reads it before every
  `type` and `key` with the D-Bus client it already ships, no new dependency, and
  nothing to install on a default Ubuntu desktop. The head of `mru-sources` is the
  live source, written on every switch by every means a user has, and `current` is
  deprecated and ignored by the shell whatever it looks like; Mutter appends its own
  `us` group after the user's sources and compiles them in chunks of three, so the
  active group is the source's index within its chunk. The string arrives as `yz@`,
  byte-exact, on both generations, after `Super+Space`, after the panel menu, and on
  the first command of a session rebooted with German last used, where the old build
  typed `zy"` before the user had touched anything. With five sources and the fifth
  picked, where the old guess assumed Russian and typed **nothing at all**, it types.
  A switch made under a running daemon is followed command by command, and it costs
  1.2 ms a command as the session user, 6.3 ms as root, where the read has to happen
  in a forked child because the portal answers the session user only.
- **The layout notice is gone wherever the layout is known.** It existed because we
  were guessing, and on GNOME it fired on every command of every non-US desktop, with a *single* layout configured too, because Mutter's appended `us` fallback makes
  a one-layout session look exactly like a two-layout one in the keymap. The setting
  tells them apart, so a one-layout GNOME session now says nothing at all, and
  `wdotool keys explain` reports `group 1 of 2, from wayland + gnome input-sources`
  where it used to report `group 1 of 2 (assumed), from wayland`. Everything that
  cannot be read leaves the guess and the notice exactly as they were: no GNOME
  Shell on the bus, no portal, a setting that will not parse, an input source that
  is an IBus engine rather than an `xkb` layout, an index that does not fit the
  keymap, per-window layouts turned on, and a `mru-sources` head that is no longer
  in `sources`.
- **One portal call is now made, and the no-dialog guarantee is narrowed to say so.**
  `org.freedesktop.portal.Settings.ReadAll` is the interface every GTK and Qt
  application calls at start-up for the colour scheme: read-only, answered with no
  permission check, and with no entry in the portal's permission store to allow or
  deny. Watched with `dbus-monitor` on both generations, one `wdotool type` makes
  exactly that one call to the portal and no other, nothing appears on screen, and
  the permission store lists nothing for `settings`. `tests/test_no_portal.py` now
  exempts that one interface by name, keeps every other interface on the same bus
  name a failure, `RemoteDesktop` and `InputCapture` first among them, and has a
  test of its own for the width of the hole.
- **Keeping a layout on GNOME says what happened to it.** A persistent apply reports
  when GNOME has already discarded `~/.config/monitors.xml` (one entry that fails the
  adjacency check throws the whole file away, at every boot), warns when the layout
  being saved is one that a change to fractional scaling would make GNOME refuse, and
  copies the file it is about to replace to `monitors.xml.wxrandr-backup`.
- **One route through GNOME's adjacency rule**, `wxrandr --unsafe-gnome-overlap`, off
  by default, behind a second Shell extension that the package now carries and nothing
  enables, and behind an agreement recorded against the build id of the `libmutter` the
  checks ran on rather than against a version string. `warandr` has the same route as
  an option that never asks, for a window started from a hotkey.
- **A table to add a GNOME to, and one way to force past not being in it.** Everything
  that differs per GNOME generation, the soname to match, the Meta typelib version,
  the namespace of the type description, the size `MetaMonitorsConfig` must report and
  the tail slots the description is built from, is one record in
  `gnome/w11-overlap@w11/generations.json`, with the extension, the
  installer, the generator and `wxrandr` reading it instead of computing it. That
  matters because mutter 51 renumbered its library to the GNOME major, so GNOME 51
  ships `libmutter-51.so.0` where the old arithmetic said `libmutter-19`.
  `--unsafe-gnome-overlap-unmeasured <major>` is the one thing here that gets past a
  refusal: it skips the check that the build is in the table and no other, takes the
  running GNOME's major as its argument so a command line copied from a forum is
  refused by number, is remembered nowhere, and cannot be reached from `warandr` at
  all. `--dryrun` is refused together with it rather than offered as a rehearsal it
  cannot be: the remaining checks run inside `gnome-shell` and can end the session
  before anything of ours decides whether to write.
- **GNOME 51 was measured by following that procedure and nothing else**, on Ubuntu
  26.10 with `libmutter-51.so.0`: 80 bytes, three tail slots, taken from mutter 51's
  own header and confirmed against the live GType registry, then an overlap applied on
  three heads with the shared columns byte identical between them. Doing it found two
  things the procedure had not said. An extension whose `metadata.json` does not name
  the running Shell major is never loaded, so on exactly the builds the forcing option
  exists for the bus name was never taken; the installer now writes the running major
  into the installed copy. And a type description must name no shared library: a
  forced run picks its description by struct size on a machine whose `libmutter` is by
  definition the wrong one, and a description naming a file that is not there made
  `gjs` abort `gnome-shell` on the first call through it, which took a session on a
  `--dryrun` that writes nothing. The generated descriptions name none, and one that
  does is refused by name.
- **The retest of all of it** found four things wrong and one missing: `--persistent`
  and `--unsafe-gnome-overlap` were handed to the real xrandr on an X11 session, where
  the first refused the whole command over a flag xrandr has never had; the geometry
  query printed a made-up size one line above refusing to guess one; several documents
  disagreed with what the tools do; and the package committed in `release/` was still
  the 0.3 build, so a user who installed the way the README says got none of this. The
  package is built from this tree, and was rebuilt again once the overlap extension
  had a table to read, which is what `tests/test_release_deb.py` noticed, and once
  more over the code that reads the active layout: that last build is the file in
  `release/`, installed with one `apt-get install` on a default Plasma desktop and a
  default GNOME one, where a session switched to German typed `yz@` byte for byte on
  both and said nothing on stderr.
- **The exit 127 line names the reason that applies.** Handing over to a real tool that
  is not installed said "this is an X11 session" whoever asked, including the two ways
  of asking for the handover on a Wayland desktop (`W11_PASSTHROUGH=always` and
  `wxrandr --backend x11`), where it is not one. It now says a handover was asked for
  instead. Found by running the release package on the 26.04 default install.
- **The one method nothing had ever exercised is exercised now.**
  `ConfirmDisplayChange` answers GNOME's "Keep these display settings?" dialog, the one
  a `--persistent` apply raises for twenty seconds, and it shipped on two assumptions
  about a dialog no measurement had ever put on screen. Both hold, on GNOME 46.0 and
  50.1 alike, and the code now says what was measured rather than what was assumed: the
  dialog is `DisplayChangeDialog`, a `ModalDialog` that `ModalDialog._init` adds
  straight to `Main.layoutManager.modalDialogGroup`, which keeps its JS class name
  through GObject registration, and whose `_onSuccess`/`_onFailure` are the actions of
  its Keep and Revert buttons. Asking to keep answers the dialog in about a second and
  a half, leaves the layout up long past the countdown that would have taken it away,
  and gets `~/.config/monitors.xml` written; asking to revert puts the previous layout
  back and writes nothing; with no dialog on screen the answer is `false` and neither
  the layout, the file nor the shell moves, which is what the second assumption --
  that `Shell.WM.complete_display_change` is a no-op with nothing pending -- had
  claimed with no evidence behind it. The dialog's own buttons still work afterwards.
  Nothing needed fixing, and `gnome/README.md` no longer lists this as never exercised.
- **4146 tests**, up from 2262, the new ones being the daemon's two ways of ending,
  the chord the layout cannot produce, the pin carried on the request, the guards
  around the saved display configuration, every refusal of the overlap route
  classified and then re-run with the forcing option to see which of them it changes,
  the dry run that cannot rehearse a forced one, and the active layout read from each
  desktop: the interface answering, absent, wedged, dying and coming back, answering
  nonsense, refusing (per-window layouts, a stale `mru-sources` head, an IBus source),
  an index the keymap cannot hold, a switch between two commands on one daemon, the
  forked read itself, and a kded landmine on the mock bus that fails the test if
  anything ever calls it -- and the answer to GNOME's display-change dialog: found and
  pressed either way, found by nothing when a second call comes too late, and the two
  halves of the lookup held to the shell source they were measured against.
- **The documents were read against the code again**, which is the check this release
  exists to keep passing: `scripts/check-docs.py` reads the options out of the source,
  out of every help text each tool prints (the subcommands included) and out of every
  markdown file, and reports where the three disagree. Everything it reports is a real
  disagreement now: an option that is accepted and deliberately unprinted, or that a
  package writes for another program's command line, is a table entry with its reason
  written down, and an entry that has stopped being true is itself reported. It exits
  non-zero when there is anything to say, and today it says nothing.

Measured on the same rig, now thirteen images: eleven built from an Ubuntu cloud image
plus a desktop metapackage, GNOME 51 on 26.10 among them, and two installed by the
Ubuntu desktop installer itself.

## Version 0.3

A subtraction release. Nothing here is a new tool: the same six do the same things,
with 602 production lines fewer behind them, one package shape instead of two, and a
documentation set that no longer disagrees with itself.

- **`w11common/`, a package for what every tool shares.** Session discovery, the X11
  handover, the D-Bus and Wayland wire clients, the exception every command raises,
  the exit-status rule for an output that never arrived, and detached children. It
  imports nothing outside the standard library and nothing of `wdotool`, which is
  what let the three display tools stop carrying `wdotool` at all: built from the same
  script on both releases, the `wxrandr` zipapp lost 63% of its bytes, `wmirror` 60%
  and `warandr` 56%, because a bundle copies whole directories and those three used to
  drag in the keysym table, the input daemon and every window backend for the sake of
  three small modules.
- **Four things written more than once became one each.** C's `atoi` and `strtol`,
  which had six copies, became `wdotool/cnum.py` with C's semantics kept, including
  `[0-9]` rather than `\d`, so a Unicode digit gives zero exactly as C does. Three
  getopt wrappers became one. One pointer hit-test that had been written three times,
  over three tables of which window layers to look through, became one function over
  one table. The detach protocol both the gamma holder and the mirror supervisor
  needed became `w11common/procs.py`. Two Wayland-to-RandR transform tables became one.
  Four display backends grew the same six methods, so a session now holds one backend
  instead of four handles and six name tests.
- **Nine bugs in error paths**, all found by challenging the tree rather than by using
  it: a wedged compositor that hung a sway command for ever, a bus that died mid
  authentication and came back as a traceback, an unmarshallable KWin argument that
  did the same, a missing bridge extension reported as a locked screen, `sleep 1e300`,
  `type --file -` on bytes that are not UTF-8, a keysym past the end of Unicode,
  `__keymap --group`, and a layout script saved non-atomically. And no tool prints a
  traceback or exits 120 when its own stdout is gone: the status is 1, or silence for
  a closed pipe, as the originals do. The release check on a default 26.04 desktop
  found the other half of that still open, `tool >/dev/full 2>&1`, where the one-line
  diagnostic about the lost output cannot land either, tracebacking in all six and
  exiting 120 in five, with apport filing crash reports for two of them. Every last
  word a tool writes now goes through `w11common/stdio.py`'s `warn()`, which closes
  stderr when it cannot write to it. The same run found one more, in a diagnostic
  rather than in a tool: `gnome/install-bridge.sh --check` looked for the udev rule
  under `/etc` only, so on a machine installed from the package, which ships it in
  `/usr/lib/udev/rules.d` where a package belongs, it printed `udev rule: no` two
  lines above `uinput usable by test: yes (logind ACL)`, the check the README's
  troubleshooting table sends people to, contradicting itself. It now names whichever
  copy is on disk, `/etc` first, and says when the one it found came from the package.
- **One package for Ubuntu 24.04 and 26.04**, `Architecture: all`, built by
  `scripts/build-deb.sh` into `release/` and committed there, so a clone is already
  installable. It carries the six tools, the GNOME bridge extension, the udev rule and
  the `warandr` menu entry. Installed on a default 26.04 desktop it is one command:
  the rule takes effect at once with no reboot, and after the single logout the
  package asks for, the bridge is enabled and ACTIVE with nothing typed. `apt remove`
  puts `/dev/uinput` back to `root:root 0600` with no ACL and leaves the running
  session alone.
- **The no-authorization-dialog guarantee, measured.** Six images, every command run
  three ways, with the session bus, the system bus, the window list and both screens
  watched throughout: no prompt, no window we did not open, not one portal call. The
  same rig pointed at a real portal client and at `pkexec` raised both dialogs, so it
  does see one when there is one. `tests/test_no_portal.py` is the static half.
- **The rig grew a second default install.** Ubuntu 24.04 off the desktop ISO joins
  the 26.04 one, both built by the real Ubuntu installer with every question left
  alone, because "it works out of the box" is a claim about an installed system and a
  cloud image plus `ubuntu-desktop` measurably is not one. Running the install guide
  verbatim on the 24.04 one corrected three sentences of it. `vm/SETUP.md` is how to
  stand the rig up on a machine of your own.
- **2262 tests**, up from 2085, on a suite that now shares its fakes instead of
  keeping seven of them: one recorder device, one `env()`, one evdev fake, one
  headless sway, and one Wayland marshaller library with a server base. Four files
  that were scripts became test cases, so one broken assertion no longer aborts the
  whole collection.
- **The documents were re-read against the code rather than against each other.**
  The eight of them moved into `docs/`, the images into `media/`, everything about
  installing collected into one section near the top of the README, and every count,
  path, option and version string checked against what the tree does today.

Measured on the same rig, now twelve images: ten built from an Ubuntu cloud image
plus a desktop metapackage, and two installed by the Ubuntu desktop installer itself.

## Version 0.2

Six tools, and on sway nothing wdotool injects needs a privilege at all.

- **On sway, nothing wdotool injects needs a privilege any more.** The pointer goes
  through `zwlr_virtual_pointer_v1` where the kernel device is closed to us, as the
  keyboard already went through `zwp_virtual_keyboard_v1`, so `click`, `mousemove`
  and the rest join `type` and `key` in needing no root, no group and no udev rule
  there. The two halves are chosen separately and by the same rule, so a compositor
  that implements one and not the other gets the protocol for that one and the kernel
  device, and its error, for the other. Absolute moves on the protocol path land with
  0.000 error, measured over 14 targets on a three-head layout with one head at a
  negative origin and one at scale 1.5, and relative moves cannot be accelerated
  there at all, because a virtual pointer is not a libinput device. See
  [WDOTOOL.md](docs/WDOTOOL.md#typing-and-clicking-with-no-privilege---vkbd).
- **What each desktop does with a layout after you set it**, measured through a
  hotplug and a reboot on all four, with where each one keeps it and what that means
  for a layout script. KDE saves whether you want it to or not: KWin has no temporary
  mode, every apply it takes lands in `~/.config/kwinoutputconfig.json` in the same
  second, and `--persistent` is accepted and means nothing there. GNOME writes
  nothing unless a `--persistent` apply is confirmed in Mutter's own *Keep these
  display settings?* dialog. sway and X11 write nothing either way. The tables are in
  [WXRANDR.md](docs/WXRANDR.md) and [WARANDR.md](docs/WARANDR.md).
- **Plasma over Xorg** is an X11 session like any other and is handled like one, on
  both generations, with the two things that look like they should change the answer
  named and measured: KWin owns `org.kde.KWin` on the session bus there exactly as it
  does on Wayland, and the KWin script backend *would* half work. The handover is
  decided by the session and not by the bus, before any backend is detected, which is
  the whole reason it is right.
- **KWin 6.7 stopped publishing outputs the way `wxrandr` found them.** From Plasma
  6.7.0 an output is no longer a `kde_output_device_v2` `wl_registry` global; the
  compositor hands the device objects out through a `kde_output_device_registry_v2`
  object instead (kwin `7e32e00c`, never backported, 6.6 still publishes the
  globals). On a real Plasma 6.7.4 session the old global is simply absent, so the
  second path is the only way to see an output at all, and `wxrandr` takes it. Query,
  mode, position, rotation, scale, `--off`, `--primary`, `--same-as` and hotplug all
  measured there against `kscreen-doctor`.
- **wmirror**, a sixth tool and the only one here that clones nothing. On wlroots it
  mirrors a **region** of an output, or a whole output onto a **differently shaped**
  one, by running the packaged
  [`wl-mirror`](https://github.com/Ferdi265/wl-mirror) and owning its lifetime, the
  two pictures output geometry alone cannot produce, and nothing else. Two outputs of
  the same size at the same position already mirror byte for byte on wlroots, so
  `wxrandr --output B --same-as A` stays the answer there and wmirror sends you to it
  rather than starting anything. It refuses by name what the measurements showed goes
  wrong, chief among them two outputs that **share pixels**, where a fullscreen mirror
  window is drawn on its own source: run that deliberately and both heads go entirely
  black, every pixel. Nothing it starts is left running that `wmirror --list` cannot
  find and `wmirror --stop` cannot end, not when its own supervisor is killed, not
  when a start is interrupted, not when two of them race, and a mirror ends itself
  when the layout moves out from under it. **GNOME and KDE have no unprivileged
  capture protocol at all**, and `wmirror --check` names what is missing instead of
  half working; on X11 the answer is `xrandr --same-as`, and it says so rather than
  naming a package that does not exist there.

Everything above was measured on the rig 0.1 left behind, the same desktop images
with the same heads plugged, resized and unplugged from outside the guest, and the
suite was 2085 tests.

## Version 0.1

The first tagged release. Everything below was measured on real desktops in the test
rig before it was claimed.

The tools run on **GNOME** 46 and 50, **KDE Plasma** 5.27 and 6.6, **sway** and the
wlroots family, and on any **X11** session, where they hand over to the originals
rather than pretending.

- **GNOME**: a Shell extension carrying the window commands, and monitor
  configuration straight through Mutter with nothing to install.
- **KDE**: window commands through KWin's scripting, monitor configuration through
  the KDE output protocol, including the compositor's own clone when two mirrored
  outputs would otherwise crop rather than copy.
- **X11**: the tools hand over to the real `xdotool`, `wmctrl`, `xprop` and `xrandr`
  with `execve` and argv untouched, so an X11 session behaves exactly as it did.
- **warandr**, an arandr clone for both worlds, which shows the backend in use and
  lets you change it from the window.
- **Keyboard layouts**: typing works under a non-US layout, by reading the
  compositor's own keymap and looking the character up backwards. On a plain US
  layout none of that code runs at all, verified key by key against the built-in
  table. On sway typing goes through the Wayland protocol built for it, so there it
  needs no privilege whatsoever.
- **`wdotool keys`**: watch what your keyboard really sends, or ask how to type a
  character on the layout you have.
- Partial **overlap** of outputs where the compositor allows it, with what that means
  on each one stated plainly.
- An install guide that was written by doing it and then re-run verbatim on fresh
  images of four desktops, and a threat model for a toolbox that deliberately injects
  input.

Behind it: a rig of seven desktop images with monitors that could be plugged, resized
and unplugged from outside the guest, and 1884 tests. The tools were stressed
deliberately on each desktop, and what that found is in the history, roughly fifty
defects, including a few that mattered: typing captured by another user, commands
that reported success while failing, and a monitor placed ten pixels wrong on the
wlroots backend at most fractional scales.
