<a id="warandr-design-contract"></a>

# warandr: design contract

Drop-in `arandr` clone that works on **Wayland** (through `wxrandr`) and on
**X11** (through `xrandr`). Same window, same menus, same `~/.screenlayout/*.sh`
files, arandr's layout scripts load here and ours load in arandr. House rules
per Technical.md, with one exception spelled out below.

## Quick start

The GUI requires GTK 3 and PyGObject; see [installation](../README.md#install).
Open `warandr`, drag the monitor rectangles, then choose **Apply**. Save layouts
as scripts in `~/.screenlayout/`. Loading a script does not execute arbitrary shell
commands; see [layout scripts](#layout-scripts) for the supported format.

```sh
warandr
warandr ~/.screenlayout/desk.sh
warandr --print-backend --verbose
warandr --command
```

## CLI

Usage: `warandr [options] [savedfile]`. The optional file is a saved layout to open.

| Option | Behavior |
|---|---|
| `--randr-display D` | Selects the display to configure; the GUI uses its normal display. |
| `--force-version` | Accepted for arandr compatibility and ignored. |
| `--save FILE` | Writes the current layout, or the loaded layout adapted to current outputs, then exits without opening the GUI. |
| `--command` | Prints the command Apply would run, including a forced backend, then exits. |
| `--backend NAME` | Selects `auto`, `x11`, `sway`, `hypr`, `wlr`, `mutter`, `cinnamon` or `kwin`. Aliases: `hyprland`, `gnome`, `muffin`, `kde`. Applies to the GUI, `--command` and `--save`. |
| `--print-backend` | Prints the selected backend token and exits. |
| `--verbose` | With `--print-backend`, explains the selection and reports session details. |
| `--version` | Prints the warandr release and exits. |
| `--unsafe-gnome-overlap` | Suppresses the GUI overlap warning for this run; all compatibility checks still apply. |
| `-h`, `--help` | Prints command-line help. |

An unknown backend produces an error listing the valid names. To inspect a valid
selection, use:

```console
$ warandr --print-backend --verbose
mutter
kind: Wayland
runs: /usr/bin/python3 -m wxrandr
chosen by: wxrandr package at /usr/lib/python3/dist-packages
session: wayland
compositor: Mutter
protocol: org.gnome.Mutter.DisplayConfig (D-Bus)
available: yes
```

The report combines warandr's selection with the child backend's session details.
Backend, argument and file errors print `warandr: ...` and exit 1.

`--unsafe-gnome-overlap` is the last option and the only dangerous one, and
it is the command-line half of [Overlapping monitors on
GNOME](#overlapping-monitors-on-gnome): it applies an overlapping
layout without ever opening that dialog, for a window started from a hotkey
or a desktop entry where there is nobody to answer it. It waives the
question and not one check, and it records no agreement, the window's box
is what records one, so a hotkey cannot agree on the user's behalf. Where
the route is not there, or the layout does not overlap, it changes nothing.
The flag that gets past an *unmeasured* GNOME is a wxrandr option with no
warandr spelling at all, on purpose: WXRANDR.md §
[`--unsafe-gnome-overlap-unmeasured`](WXRANDR.md#forcing-past-a-refusal-on-a-gnome-nobody-has-measured).

## Toolkit rule (the exception)

Everything in this repo is pure-stdlib Python, except the warandr GUI, which
uses **GTK 3 through PyGObject**, stock Ubuntu desktops (24.04 and 26.04) ship
`python3-gi` and `gir1.2-gtk-3.0` as dependencies of update-manager /
software-properties-gtk / apport-gtk. They do **not** ship `python3-gi-cairo`,
so there is no cairo drawing: the canvas is built from widgets (a `Gtk.Fixed`
inside a scrolled window; one CSS-coloured `Gtk.EventBox` + label per output,
dragged with button/motion events). Everything that is not the window
(`xrandr_parse`, `model`, `randr`, `cli --save/--command`) stays stdlib-only
and is what the tests exercise without a display. A missing GTK is one line,
and it names the box's own packages, read from `/etc/os-release` through
`w11common/distro.py`: `warandr: GTK 3 for Python is not available (...) - on
Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0`, `on Fedora: sudo dnf
install python3-gobject gtk3`, `on Arch: sudo pacman -S python-gobject gtk3`, and
`on NixOS: nix-env -iA nixpkgs.python3Packages.pygobject3 nixpkgs.gtk3`, no `sudo`
on that last one, because `nix-env` is per-user. The Debian bytes are unchanged, and
an unidentifiable distribution gets them. MATE is the one X11 desktop measured where
the pair is satisfied out of the box and `arandr` is absent, so `warandr` is a real
addition there; a minimal sway or i3 image has `python3-gi` and not the GTK 3
typelib, which is what the repo README's footnote (h) actually names.

## Backend selection (`warandr/randr.py`)

First match wins:

0. an explicit choice, `warandr --backend NAME`, or the GUI's
   **Layout ▸ Backend**. `NAME` is `auto` (the default: everything below),
   `x11`, or one of wxrandr's own backends `sway|hypr|wlr|mutter|cinnamon|kwin`
   (aliases `hyprland`, `gnome`, `muffin`, `kde`), spelled exactly as wxrandr's
   own flag. `x11` runs the
   real `xrandr`; a Wayland backend runs wxrandr with `--backend NAME`,
   which inside wxrandr beats `$WXRANDR_BACKEND` and its detection, so the
   documented rule holds end to end: **flag > environment > detection**.
   There is no silent fallback: a Wayland backend with no wxrandr to run it
   is an error (`the mutter backend needs wxrandr, which is not here ...`),
   not plain xrandr.
1. `$WARANDR_XRANDR`, a command line to run instead (shlex-split). Its kind
   is Wayland when it mentions `wxrandr`, X11 otherwise; `$WARANDR_BACKEND`
   (`x11`/`wayland`) overrides the kind. Tests point this at the fake. A
   forced Wayland backend appends `--backend NAME` to it (it is still "the
   command to run instead"), a forced `x11` does not.
2. shared session detection identifies Wayland and the `wxrandr` package is importable (the repo
   checkout, or the pyz that bundles it): run the **same interpreter** with
   `-m wxrandr`, `PYTHONPATH` pointing at wherever the package was found (a
   zipapp path works, zipimport). No second copy of wxrandr is needed.
3. shared session detection identifies Wayland and `wxrandr` is on `PATH`.
4. `xrandr`. Automatic selection can fall back to the XWayland view when
   wxrandr is absent. An explicitly forced Wayland backend instead errors.

Session detection also works with missing environment variables; see
[session discovery](Technical.md#2-session-discovery-and-the-x11-handover).

warandr **never hands its own process over** to the real tool (no `execve`,
unlike the four clones): it chooses which one to *run*, as a child, which is
what makes the choice switchable while the window is open.

Being a child rather than a handover, the X11 runner does not pass through
`passthrough.child_env()`, but it takes the same **X11 repair**,
`passthrough.repair_x_env()`: a missing or dead `$DISPLAY`/`$XAUTHORITY` is
filled in from the session (logind's record of it, the owner of the X socket,
the compositor's own cookie file) before the child starts. Without it
`--command` and `--save` were the one thing in this repo that still failed with
`warandr: xrandr failed (1): Can't open display` under `sudo`, over `ssh
root@box` or from cron, in the very shell where the four clones worked.
`--randr-display` is applied afterwards and still wins; the Wayland runner is
left alone (wxrandr finds the session for itself and opens no X server); and a
*saved* layout script is unaffected, it calls the bare command word, exactly
as arandr's does, so it still wants a session of its own to run in.

Which backend it turned out to be is asked, once, off the main loop:
`wxrandr --print-backend --verbose` gives the token (`mutter`, `sway`, ...)
and the fuller explanation, `wxrandr --backends` the availability table the
Backend menu greys itself with (both are layout-free and answer on X11 too).
The X11 runner is the real xrandr, which has no such options, so its answer
is composed locally; a wxrandr too old to know them leaves the coarse name
`wxrandr (Wayland)` and greys nothing.

The kind decides two things: the **command word** written into layout scripts
(`wxrandr` on Wayland, `xrandr` on X11; both are accepted when loading) and the
**scale semantics** (below). `--randr-display D` sets `WAYLAND_DISPLAY` /
`DISPLAY` for the backend only, like arandr.

## Parsing (`warandr/xrandr_parse.py`)

One parser for `--query` and `--verbose` (xrandr 1.5.x bytes; wxrandr renders
the same). Screen line (min/current/max); output header `NAME connected|
disconnected|unknown connection [primary] [WxH+X+Y [(0xID)] [rotation
[reflection phrase]]] [(allowed rotations/reflections)] [MMmm x MMmm]`; query
mode rows grouped by name with ` %6.2f` rate columns and `*`/`+` flags; verbose
modelines (`  NAME (0xID) CLOCKMHz flags *current +preferred`) with the rate
taken from the `v:` clock, the width/height from `h:`/`v:` (custom mode names
carry no size); the tab block yields Identifier, CRTC and the **Transform**
matrix. Xvfb's minimal server (`screen connected 1280x1024+0+0 0mm x 0mm`, a
`0.00*` rate, no rotations list, `normal (normal)` in verbose) parses too.
warandr runs `--verbose` once per snapshot (arandr does the same).

## Model (`warandr/model.py`)

- `Output.size()` is the drawn (logical) size: scale, then the rotation swap.
  **Wayland**: scale is the compositor's HiDPI factor, logical = `px / scale`
  truncated (sway's rule; wxrandr's `--scale` sets exactly that), recovered
  from `mode / geometry` since compositors report an identity transform.
  **X11**: `--scale` is xrandr's framebuffer factor, logical = `ceil(px *
  scale)`, read from the verbose transform matrix. The Scale submenu (1, 1.25,
  1.5, 1.75, 2, 3) exists on the Wayland path only (arandr has no scale and
  X11 has no per-output HiDPI factor); an existing X11 transform is drawn
  correctly and written back as `--scale`, so what is drawn is what xrandr is
  told.
- **Snapping** (arandr's `Snap`): while dragging, within `factor * 5` layout
  pixels of another active output's (or the virtual screen's) left/right/top/
  bottom edge, the dragged output's own opposite edge, or a centre line, the
  coordinate snaps there. Deviation: the *nearest* candidate wins (arandr
  takes an arbitrary set member).
- **Overlaps are the backend's business, not ours** (arandr allows them and
  so do we): two active outputs may intersect freely. `Layout.overlaps()`
  lists the pairs that intersect at *different* origins (same origin is a
  clone, below); `Layout.overlap_refusal` holds the live backend's reason for
  refusing one, or `None` where it takes one, and `check()` raises exactly
  that sentence, so a refused drop snaps back with "<output> not moved:
  GNOME's Mutter refuses monitors that are not edge-adjacent …" and never
  with a rule of our own. Every edit is still validated as a whole and
  reverted on failure. See *What an overlap means*, below.
- **Clones read back as mirrors**: an active output sharing its origin with
  an earlier one (a screen already running `DP-2 --same-as DP-1`, or our own
  Mirror-of after Apply → New) loads as *Mirror of* that output, drawn
  dashed, follows its target, saves as `--same-as`, so such screens stay
  editable (arandr writes two `--pos 0x0`). Chained `--same-as` in a script
  (`A --same-as B`, `B --same-as C`) is flattened onto the root like xrandr's
  fixpoint; a cycle is an error.
- **Normalisation** (deviation: arandr refuses negative positions): after each
  edit the active outputs shift so the layout's top-left is (0, 0). Dragging
  past the left/top edge therefore moves everything else. Inactive outputs
  keep their last position and are parked (dimmed) right of the layout.
- Activating an output places it right of the layout with its preferred
  mode, normal orientation, scale 1 (arandr puts it at 0,0 on top of whatever
  is there). Deactivating drops primary and detaches mirrors of it.
- Screen bound: a layout wider/taller than the server's maximum is refused
  with arandr's "A part of an output is outside the virtual screen."

## What an overlap means (per backend)

Two active outputs may intersect. arandr allows it, `xrandr --pos` has always
taken it, and warandr no longer refuses it: a drop that creates an overlap is
accepted wherever the backend in use accepts one, and refused **only** where
the backend refuses, with that backend's own sentence, so the user reads
whose limit it is. The layout half (does the compositor take the geometry?)
is the only half we control; whether the shared region then shows the same
pixels is a property of how that compositor renders, which no client can
influence from outside. Both halves were measured on two 1920×1080 heads with
the second at x=960, comparing the two crops of the shared region with
ImageMagick `compare -metric AE` and an md5 of the raw RGB:

| backend | geometry | shared region | evidence |
|---|---|---|---|
| `x11` (Xorg) | taken, silently | **same pixels** | `--pos 960x0` exit 0, screen 3840×1080 → 2880×1080; a yellow window at 1052,348 inside the region: `AE 0`, both crops md5 `c05208d2…` (measured, Xorg + Xfce) |
| `kwin` (Plasma 6) | taken | **same pixels** | `kscreen-doctor …position.960,0` exit 0, no warning; `AE 0`, equal md5 at *two* overlap widths (960 px and 480 px), KWin renders each output as a view onto one shared scene (measured, Plasma 6 / KWin Wayland) |
| `sway` / `wlr` (wlroots) | taken | **same pixels** | `swaymsg … position 960 0` → success; a window floated to layout 1100,300, wholly inside the region, is drawn on **both** heads: `AE 0`, equal md5 (measured, sway 1.11) |
| `mutter` (GNOME) | **refused**, unless the overlap extension is installed | same pixels, through it | `ApplyMonitorsConfig` → `Logical monitors not adjacent` for x = 0, 100, 960, 1919, 1921, 2500, every layout that is not exactly edge-adjacent, overlap and gap alike; nothing half-applied (measured, GNOME 46 / Mutter). With `w11-overlap` installed, warandr places it anyway and the shared region comes back byte-identical on both heads: [Overlapping monitors on GNOME](#overlapping-monitors-on-gnome) |

Measured, all four rows. **Inferred**, and said as inference: the `wlr`
backend beyond sway (same wlroots renderer, only sway 1.11 on the bench);
KWin 5.27 (only Plasma 6 was measured); Mutter's other message, `Logical
monitors overlap`, which two monitors never produce because adjacency is
checked first. A backend nobody has measured claims nothing, the window says
*This backend has not been measured here; Apply reports whatever the
compositor makes of it* and takes the overlap.

The expectation that sway's per-output workspaces would stop the mirroring
was **wrong**: a workspace binds where the tiler *places* windows, not which
pixels an output scans out, and a surface straddling two outputs is drawn on
both, identically.

Two things an overlap deliberately does **not** change. *Clones*: outputs at
the same origin are `--same-as`, not a partial overlap, `overlaps()` skips
them, they are still drawn and saved as a mirror, every backend groups
same-position outputs into one logical monitor (Mutter) or replicates them,
and Mutter takes them, so a clone stays legal even where a partial overlap is
refused. *Gaps*: they were never refused here and still are not, on any
backend. Mutter refuses a hole exactly as flatly as an overlap (the same
`Logical monitors not adjacent`), but arranging a layout means passing
through gaps, so warandr keeps arandr's behaviour and lets Mutter say no at
Apply, with its error message. The only geometry warandr itself refuses is a
layout larger than the server's maximum screen, and an overlap can only
make the screen *smaller*.

Where the user meets it: the status bar, at the moment of the drop
(`DP-2 overlaps DP-1. X11 draws both outputs from one framebuffer, so the
shared region shows the same pixels on both.`); the saved script's comment
header, two lines, what overlaps and what it means; and the backend
indicator's tooltip, as an `overlap:` line, that paragraph (`--print-backend
--verbose`, the About dialog, Script Properties ▸ Backend) is the one that
already explains the live backend, whereas the Backend menu's own tooltips
are taken: they say why a backend is unreachable, and GTK 3 pops no tooltip
over an insensitive menu item anyway. On GNOME the drop is refused with
Mutter's sentence, never with ours, unless the overlap extension is there, in
which case the same three places say what warandr will do instead, and the
indicator's tooltip carries an `overlapping layouts:` line as well. `--save`/`--command` ask
`--print-backend` first, because `auto` on Wayland is only "wxrandr" until
it has answered and a layout GNOME will not take must not be written out as
if it were fine; the window asks off the main loop instead and patches the
layout when the answer lands.

Verified live, end to end, on all four rows, each in its own VM:

- **Xfce/X11.** An overlapping drop in the real window is taken, the sentence
  appears in the status bar, Apply returns 0, `xrandr --listmonitors` really
  reads `+960+0`, the saved script carries both comment lines and re-runs on
  a plain X11 box with bare `xrandr` on the PATH. Screendumps of the two
  heads: the shared region is byte-identical (`AE 0`, equal md5).
- **Plasma 6 / KWin Wayland.** The same drag and Apply through the window:
  rc 0, `kscreen-doctor -o` reads `Geometry: 960,0`, `wxrandr --listmonitors`
  agrees, the saved script says KWin's sentence, and the two heads' crops of
  the shared region are byte-identical. The undo line wxrandr prints for an
  overlapping *previous* layout is a real inverse: replaying it verbatim put
  `+960+0` back.
- **GNOME 46 / Mutter.** `wxrandr --output V-2 --pos 960x0` and its `--dryrun`
  both come back `xrandr: GNOME's Mutter refused this layout: Logical
  monitors not adjacent` with nothing half-applied; a *gap* (`--pos 2500x0`)
  gets that same sentence from Mutter, at Apply; `warandr --command` on an
  overlapping arandr script exits 1 with Mutter's sentence; the window's drop
  reverts with it and the boxes go back; `--same-as` is still accepted.
- **sway 1.11 / wlroots.** `--pos 960x0` and `--backend wlr --pos 480x0` both
  apply (`swaymsg -t get_outputs` agrees), `warandr --save` writes sway's
  sentence, and the pixels bear it out: a `foot` window floated to layout
  1100,300, wholly inside the shared region, is drawn on **both** heads,
  `AE 0`, equal md5, `srgb(0,0,204)` at the same layout point on each. It
  opened on the focused output's workspace, which is the only thing that
  stayed per-output.

**Why GNOME's row reads that way, and what to tell the user instead.** The
refusal is one validator on the way in, `meta_verify_logical_monitor_config_list()`
in `src/backends/meta-monitor-config-utils.c`, which requires every submitted
logical monitor to share an edge with another by exact integer equality, and
which runs before anything is applied. It is not a permission problem and no
client routes around it: GNOME's own Displays panel submits the same call and
reads the same sentence. Nothing else in Mutter needs the rule, lookup by
point takes the first match, lookup by rectangle falls back to the primary,
pointer constraints clamp only when the pointer is in no view at all, the
screen size is a bounding box, and the renderer already draws several stage
views over identical rectangles because that is how mirroring works, and
GNOME on Xorg never runs the verifier at all, deriving its logical monitors
from the X layout, overlaps included. That is why the same drag is taken as
drawn on the other three rows, and why the status bar says *Mutter refuses
this* rather than *this cannot be done*.

### Overlapping monitors on GNOME

Since 0.4 there is one route through that validator, and `warandr` can use it:
`w11-overlap@w11`, a *separate* GNOME Shell extension with its
own installer and its own enable step, which writes the two numbers that are a
monitor's position inside `gnome-shell` and asks Mutter to apply the result.
What it is, what it risks and every check that stands between it and a dead
session is [WXRANDR.md § `--unsafe-gnome-overlap`](WXRANDR.md#--unsafe-gnome-overlap-the-one-route-through);
this section is only the window's half.

**What the window does.** At startup, once the backend has a name, it asks
`wxrandr --gnome-overlap-status` on the same worker thread as
`--print-backend --verbose`. The answer is one token:

* `unavailable`, no extension, or a GNOME nobody has measured. **Nothing
  changes**: the drop is refused with Mutter's sentence, exactly as the row
  above says, and Apply passes no flag it did not pass in 0.3. There is no way
  to force past that from the window, on purpose: the flag that gets past an
  unmeasured build takes the running GNOME's major as an argument, read out of
  a refusal the person has just read, and a checkbox carries none of that
  (WXRANDR.md § `--unsafe-gnome-overlap-unmeasured`).
* `available`, the route is there and nothing has been agreed to. The drop is
  taken, the status bar says what will be done instead of refusing, and the
  first Apply asks.
* `agreed`, the route is there and this build has been agreed to. The first
  Apply does not ask either.

The status query is cheap by contract: it reads the Shell's public version
property and a bus name, and nothing at all out of `gnome-shell`. A GUI runs it
at startup, so answering *would this work?* must not cost a walk of Mutter's
heap.

**The one question, and the box.** The first time an overlapping layout is
applied on a GNOME where the route exists, Apply opens a dialog: which two
monitors share which rectangle, that Mutter refuses this and why the ordinary
path cannot be used, what the extension does (eight bytes per monitor, inside
`gnome-shell`), what that risks (`gnome-shell` is the session), that nothing is
saved, and *Cancel* / *Apply anyway*, with **Cancel the default response**, so
Return on a dialog nobody read is not a yes.

A dialog at the moment of the first Apply, rather than a preference in a menu,
because that is when the user has actually asked for the thing: the explanation
is then about something they want instead of something they might one day want,
and a preference nobody opens is read by nobody. The box on it, *Do not ask again on this GNOME*, is what makes it once rather than every
time, and it is **not ticked by default**: agreeing is a second, deliberate act,
and applying a single overlapping layout without agreeing to anything stays
possible.

The box records the same agreement `wxrandr --gnome-overlap-allow` records, in
the same file, scoped to the same build, and it is recorded **after** a
successful apply, never before it. If that write were ever the one that ended
the session, the appropriate state to wake up in is one where nothing was agreed and
the question is asked again.

**What the window says while it does it.** The status bar already names the
backend; an overlapping layout is the other unusual thing, so it is said out
loud in the same place:

* at the drop, `Virtual-2 overlaps Virtual-1. GNOME's Mutter refuses monitors
  that are not edge-adjacent, so warandr places these through the
  w11-overlap extension instead; the layout is temporary and is gone at
  the next login.`
* as the command, because the status bar's promise is that it shows what Apply
  runs, `wxrandr --unsafe-gnome-overlap --output Virtual-1 …`. That flag is
  added for an overlapping layout on GNOME with a route, and for nothing else;
  a saved script still gets the bare `wxrandr`, because a layout script must run
  on any desktop.
* after the apply, `applied through the w11-overlap extension a layout
  GNOME refuses (Virtual-1 and Virtual-2 share 960x1080 at +960+0); not saved,
  gone at the next login`, plus `- agreed, warandr will not ask again on this
  GNOME` when the box was ticked.
* in the indicator's tooltip, About and Script Properties ▸ Backend, as an
  `overlapping layouts:` line: the date it was agreed and how to withdraw, or
  that Apply will explain and ask once.

Withdrawing is `wxrandr --gnome-overlap-forget`; there is no button for it,
because withdrawing has to work from a text console with a session that will
not start, and that is where the command already works.

**Verified live** on GNOME 50.1 (Ubuntu 26.04, two 1920×1080 virtio heads,
`w11-overlap` installed): drag `Virtual-2` onto `Virtual-1`, Apply, the
dialog, the box, *Apply anyway*, `wxrandr --query` then really reads
`Virtual-2 … 1920x1080+960+0`, the second head's screendump shows the shared
960 px, and the agreement file exists. The second overlapping Apply produced no
dialog at all, passed the same flag, and `wxrandr` said one line.

The alternative where the route is **not** there, no extension, or a
GNOME this has not been measured on, and the answer for a user who asks the
window for an overlap on such a GNOME: GNOME will not place two monitors so
that they share area,
and the closest thing available is a **mirrored region**, where the pixels
match exactly, the copy takes the clicks that land on it instead of passing
them to the window they came from, and a screen lock ends it where it is made
by capture rather than by the layout. It is never called an overlap. Whole-monitor mirroring
*is* in the layout on GNOME (`--same-as`, at an identical mode, rotation and
scale) and warandr's *Mirror of* draws and saves it; a region is `wmirror` on
wlroots, and on GNOME only through the desktop portal, which prompts once per
session.

> **Never hand-edit `~/.config/monitors.xml` to force an overlap**, and warandr
> never writes it. Mutter's parser calls that same verifier and discards the
> **entire file** on any error, so one bad entry silently destroys every other
> monitor arrangement the user had saved, at every boot, with the only trace in
> the system journal. Mutter's own writer does not validate, so a file that
> kills itself this way can also be written by Mutter itself. The measurements
> and the routes are
> [Technical.md § 6](Technical.md#why-mutter-refuses-monitors-that-share-area).

**Out of scope here: region mirroring, which now has its own command.**
Making a region show the same pixels on a compositor that does not already do
it is a resident helper capturing one output and painting it onto another
every frame. It is not part of warandr, and deliberately not saveable from
it: a `~/.screenlayout/*.sh` file has to keep running on a plain X11 box with
the real `xrandr`, and no `xrandr` syntax expresses "mirror this rectangle
onto that one". On wlroots the route is the existing
[wl-mirror](https://github.com/Ferdi265/wl-mirror), which since 0.2 the
`wmirror` command drives and supervises (`WMIRROR.md`), a mirror there is a
resident process, not a layout, so nothing the GUI could save would describe
it. On GNOME and KDE the route is the desktop portal's ScreenCast (AGENTS.md
route 4), which asks once per session and is not wired up here yet, useless from
the hotkey that is the whole point of a layout script; on Cinnamon `wmirror`
already does it over `org.Cinnamon.Eval` and a Clutter clone (route 2, measured
byte-identical on the rig — `WMIRROR.md`), capturing nothing.

## Command line (`Layout.args()`)

One stanza per output in server order, arandr's shape and order, extras only
when they carry information:

```
--output N --off                               (inactive or disconnected)
--output N [--primary] --mode M [--rate R] (--pos XxY | --same-as O)
           --rotate R [--reflect x|y|xy] [--scale SxS]
```

`--rate` appears only when the chosen rate differs from what `--mode` alone
would pick (the `+` preferred rate, else the first listed), so an untouched
layout produces byte-for-byte what arandr 0.1.11 saves (tested against a
genuine arandr save of the same screen). `--scale SxS` appears when the
output is scaled *or was scaled when the screen was read* (`1x1` then), xrandr and wxrandr both keep an existing scale when it is not mentioned.
Apply runs the backend once with the whole line **on a worker thread** (the
window keeps repainting while a slow or hung compositor takes its 30 s
timeout; Apply is greyed meanwhile); a non-zero exit shows stderr in an
"XRandR failed" dialog and **keeps the edited layout** (arandr raises before
re-reading the screen); on success the screen is re-read. The status bar
shows the command Apply would run.

**Every backend read runs there too**, startup, New, Open and a backend
switch, for the same reason: a read is an `xrandr --query`, on Wayland a
`wxrandr --query` against a compositor that may be busy reconfiguring
outputs or wedged, and the backend allows one up to 30 s. So the window is
up and answering before the first layout is, the toolbar is greyed and the
status bar says what is being read, and a read whose result a newer one has
already superseded is dropped. A read that fails keeps the layout on screen
and says so in a dialog; when there is no layout at all, the first read of
all, warandr prints the same one `warandr:` line the command line would and
exits 1.

**Save As and the `.sh` suffix.** A layout script is saved with `.sh`, appended
when the typed name has none, *after* the file chooser has confirmed
overwriting the name it was given, so the chooser never asks about the file
that really gets replaced. arandr 0.1.11 does the same, and silently: typing
`desk` over an existing `desk.sh` loses it. warandr asks for that case itself,
in the chooser's words, before writing anything.

## Layout scripts

```
#!/bin/sh
xrandr --output DP-1 --primary --mode 1920x1080 --pos 0x0 --rotate normal --output HDMI-2 --off
```

arandr's format and arandr's default template: `#!/bin/sh` first line,
exactly one command line, a fresh save of an untouched screen is
byte-identical to arandr's, everything else is a template that survives a
reload (arandr's `%(xrandr)s` mechanism, a file loaded from arandr saves
back byte-identical, extra script lines included). Loading accepts `xrandr` and `wxrandr` command words, `--off`,
`--primary`, `--mode`, `--rate`, `--pos`, `--rotate`, `--reflect`, `--scale`,
`--same-as`, `--left-of/--right-of/--above/--below` (resolved against the
new geometry), and re-bases on the *current* outputs like arandr (unknown
output/mode → error). A stanza with `--primary` moves the primary (the
previous one is cleared even when the script does not mention it, as
xrandr does); a mentioned output without `--primary` loses it (arandr). Saving appends `.sh`, chmods 0700, defaults to
`~/.screenlayout/`, all arandr habits. **The directory is created if it is not
there** (`os.makedirs(parent, exist_ok=True)`), which is what the GUI's Save As has
always done and what a fresh account needs, so `--save` is the same recipe without a
window rather than one that fails for want of one directory. The command word on
save is the
current backend's, so an X11 arandr file re-saved on Wayland says `wxrandr`.
arandr itself can load our files when they use only arandr's vocabulary
(`--rate/--reflect/--scale/--same-as` make its parser bail, documented gap).

**A forced backend is recorded, as a comment.** A layout script has to stay
arandr's and stay runnable by `sh` on a plain X11 box, and `--backend` is not
an option the real `xrandr` would ignore, it would abort on it, so the note
is a comment and nothing else:

```
#!/bin/sh
# warandr: backend mutter forced (wxrandr --backend mutter)
wxrandr --output DP-1 --primary --mode 1920x1080 --pos 0x0 --rotate normal
```

**A partial overlap is recorded the same way**, as two more comment lines, what overlaps and what it means on the backend that wrote the file:

```
#!/bin/sh
# warandr: partial overlap (DP-1 and DP-2 share 1280x720 at +320+180)
# X11 draws both outputs from one framebuffer, so the shared region shows the same pixels on both.
xrandr --output DP-1 --primary --mode 1920x1080 --pos 0x0 --rotate normal --output DP-2 --mode 1280x720 --pos 320x180 --rotate normal
```

The pair is named symmetrically and the shared rectangle spelled xrandr's way
(`WxH+X+Y`, `--listmonitors`' spelling): neither output is *over* the other, on every backend that takes an overlap both draw that rectangle, which is the
whole point, and the note must not read differently just because the server
happened to list the two outputs the other way round. The sentence is the
backend's, not the file's: the same layout saved on GNOME
would not exist (Mutter refuses it) and the same layout saved on KDE says
KWin's sentence. Both notes appear only in the *default* template, so
a file loaded from disk is still written back byte-identically, arandr's
template rule wins. Nothing reads it back: it documents which backend the
window was talking to when the layout was captured, so that a hotkey script
that misbehaves on another machine says why. To pin the backend in a script,
edit the command word's line yourself (`wxrandr --backend mutter --output
...`), on that machine, where it is true.

### A layout script on a key

Saving a layout and binding it to a key is how a layout comes back, on all four
desktops. Nothing here restores one on its own, and what each desktop does with a
layout of its own accord is
[WXRANDR.md § Keeping a layout](WXRANDR.md#keeping-a-layout).

```console
$ warandr --save ~/.screenlayout/desk.sh     # or Save As, from the window
$ sh ~/.screenlayout/desk.sh                 # run it once before binding it
```

`--save` creates `~/.screenlayout` if it is not there, which is what the GUI's Save
As has always done and what a fresh account needs. The saved script calls bare
`wxrandr` (bare `xrandr` on an X11 session), so that has to be on `PATH`: install
`dist/wxrandr` as `/usr/local/bin/wxrandr`, or take the `.deb`, which puts it in
`/usr/bin`. An output the script has to switch back on needs `--auto` in its line,
not only a position.

Then bind it:

- **GNOME**, Settings ▸ Keyboard ▸ Custom Shortcuts, or from a terminal:

  ```console
  $ K=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/
  $ S=org.gnome.settings-daemon.plugins.media-keys
  $ gsettings set $S custom-keybindings "['$K']"
  $ gsettings set $S.custom-keybinding:$K name 'restore desk layout'
  $ gsettings set $S.custom-keybinding:$K command "$HOME/.screenlayout/desk.sh"
  $ gsettings set $S.custom-keybinding:$K binding '<Super>F8'
  ```

  Bind a chord Mutter does not own: `<Ctrl><Alt>F1` to `<Ctrl><Alt>F12` are its VT
  switches, on every hotkey in this repo.

- **sway**, one line in `~/.config/sway/config`, then a reload:

  ```console
  $ echo 'bindsym $mod+F8 exec ~/.screenlayout/desk.sh' >> ~/.config/sway/config
  $ swaymsg reload
  ```

  The reload itself drops the current layout, so press the key afterwards.

- **Xfce**, Settings ▸ Keyboard ▸ Application Shortcuts, or live, with no restart:

  ```console
  $ xfconf-query -c xfce4-keyboard-shortcuts -p "/commands/custom/<Super>F8" \
      -n -t string -s "$HOME/.screenlayout/desk.sh"
  ```

- **KDE Plasma**, System Settings ▸ Shortcuts, as a custom shortcut. This is the one
  desktop where the shortcut cannot be set up from the command line: an entry written
  into `kglobalshortcutsrc` does register, and running it over D-Bus does start the
  script, but the key never fires it. KDE is also the desktop that needs the script
  least, since KWin already has the layout after a reboot.

Test it by pressing the key and reading the screen back with `wxrandr --query`. Two
things to expect. The shortcut is dead until the session is up: the first press after
a reboot is silently lost on GNOME and on Xfce (about 24 s from `reboot` on the test
rig), while sway ran the script on the first press. And the script itself is quick,
0.13 s to 0.76 s for three heads across the four desktops, 0.7 s to 2 s end to end
from the key press.

One GNOME habit to know: an Apply that turns a monitor off or on makes Mutter move
the keyboard focus off the window, so click it before the next Ctrl+S.

## GUI (`warandr/gui.py`)

Window "Screen Layout Editor" (arandr's title). Menus: **Layout** (New, Open,
Save As, Apply ⌃⏎, Script Properties ⌥⏎, Quit), **View** (Zoom In/Out/Fit
stepping through arandr's 1:4 / 1:8 / 1:16 radios, which follow; default
1:8), **Outputs** (one submenu per output, disconnected ones greyed),
**Help** (About). Layout also carries **Backend ▸** (right after Script
Properties, with the two things it governs, arandr has no such menu, and
View is about how the canvas is drawn, not about who answers): radio items
*Automatic*, *X11 (xrandr)*, *sway*, *Hyprland (hypr)*, *wlroots (wlr)*,
*GNOME (mutter)*, *Cinnamon (muffin)*, *KDE (kwin)*, the seven backends
`wxrandr --backends` prints, plus *Automatic*, which is the GUI's own entry and
no backend at all. The menu's order is the reading order of a layout tool
(X11 first, KDE last); `--backends` prints its own, `AUTO_ORDER` (sway, hypr,
kwin, mutter, cinnamon), then the `wlr` fallback, then `x11`. A backend this session cannot reach is **insensitive** with the
reason in its tooltip (GTK 3 pops no tooltip over an insensitive item, so the
same table is spelled out in Script Properties ▸ Backend); the current choice
is never greyed out from under the user. Choosing one re-reads the layout
through that backend and redraws the canvas, a `New`, so unapplied edits go,
like arandr's New, and from then on Apply, the command in the status bar,
Save As and the per-output menu (Scale is Wayland-only) are that backend's.
One that cannot be reached leaves everything as it was: the dialog says
`Cannot use the mutter backend: ...` in the Apply-failure style, the radio
goes back, and the window is never left empty. Toolbar: Apply | New,
Open, Save As | zoom. Canvas: dark grey, one box per connected-or-active
output, a distinct pastel per output (server order), black border, name big
and underlined when primary (arandr), resolution (and `= mirror target` /
`@scale`) below, label rotated with the orientation, dimmed when inactive,
white border when selected, dashed when a mirror. No tooltips (one would pop
over the context menu): hovering a box puts its description in the status
bar. Boxes are stacked smallest last, i.e. on top: an overlap may put one
box wholly inside another, a `Gtk.Fixed` would hand the click to whichever
output the server listed last, and an output you cannot press is an output
you cannot drag back out. Left-drag moves (snap on motion, validate on drop;
a drop that creates an overlap is taken wherever the backend takes one and
says in the status bar what it will mean there, and where the backend refuses
one the drop reverts with *that backend's* sentence); right-click (or the Outputs menu) opens arandr's
per-output menu, **Active**, **Primary**, **Resolution** ▸, **Orientation** ▸
(normal/right/inverted/left), then, after a separator, **Refresh rate** ▸,
**Reflection** ▸, **Mirror of** ▸ (none / each other non-mirror active
output) and, on Wayland only, **Scale** ▸. Right-click on empty canvas: the
outputs menu. Empty-layout Apply asks first, like arandr. The status bar
shows, by priority, a transient message (what a new overlap means, a refused
drop, a saved file; cleared
by the next redraw or after a few seconds), the hovered output's
description, or the command Apply would run, which is the *whole* command,
so a forced backend shows there too (`wxrandr --backend mutter --output
...`); a saved script still gets the bare command word. Right of that line,
always visible, is the backend indicator: `backend: mutter (Wayland)`,
`backend: xrandr (X11)`, the compositor backend on Wayland (once
`--print-backend` has answered, `wxrandr` until then), the tool itself on
X11. Its tooltip is the fuller explanation, `--print-backend --verbose`'s
lines under what warandr runs and why it picked it, including the
`overlap:` line, the one sentence about what an overlapping layout does on
this backend; the same text is a paragraph in the About dialog and a page of
Script Properties. Save As reports `saved PATH`
and, when the script's command word is not on `PATH` (a stock desktop has
no `wxrandr`), appends `- note: wxrandr is not on PATH, the script needs
it`, arandr's scripts call bare `xrandr`, ours call bare `wxrandr`, and a
hotkey running the script needs it installed. Without a display warandr
exits 1 with one line (`warandr: cannot open display ...`).

## Launching on GNOME (verified live: Ubuntu 24.04 / GNOME 46, 26.04 / GNOME 50)

- **Install**: `scripts/build-pyz.sh`; `dist/warandr` runs on the stock
  desktop (`python3-gi` + `gir1.2-gtk-3.0` are there, GTK 3.24.41 / 3.24.52,
  PyGObject 3.48 / 3.56, Python 3.12 / 3.14) and carries wxrandr inside for
  its own Apply. The scripts it saves call bare `wxrandr` (arandr's shape,
  bare `xrandr`), so install `dist/wxrandr` as `/usr/local/bin/wxrandr` too;
  Save As's status line says when it is missing. `warandr.desktop` goes to
  `~/.local/share/applications/` (or `/usr/share/applications/`).
- **Hotkeys**: GNOME custom shortcuts (Settings ▸ Keyboard, or `gsettings`
  on `org.gnome.settings-daemon.plugins.media-keys custom-keybindings`)
  run their command with the session's environment, `warandr` on one,
  `~/.screenlayout/desk.sh` on another, nothing to export. Bind chords Mutter
  does not own: `<Ctrl><Alt>F1`–`F12` are its VT switches (the chord changes
  the VT and gsd logs `Failed to grab accelerator`); `<Super>F6`/`<Super>F7`
  work. The window is up ~2 s after the chord; a three-head layout script
  restores the screen in about a second (0.65 s / 0.99 s measured). The
  shortcut is dead until the session is up, so the first press after a
  reboot is silently lost (~24 s from `reboot` on the rig).
- **Temporary, like xrandr**: Apply uses Mutter's non-persistent method, no "Keep changes?" dialog, nothing written to `monitors.xml`, and Mutter
  lays the monitors out afresh at the next login (`wxrandr --persistent` is
  the other way). At a hotplug it is dropped only while the set of monitors
  is different, and comes back in full when the original set does (GNOME 50;
  the hotplug bullet below). The shortcut script is how a layout comes back,
  as with arandr, and it is the same answer on the other three desktops:
  what each of them does on its own, and how to bind the script there, is
  [WXRANDR.md § Keeping a layout](WXRANDR.md#keeping-a-layout).
- **Mutter allows no gaps**: a layout leaving a hole between monitors
  (`--pos 5000x0`) is refused by Mutter itself; the dialog shows its text
  (`XRandR failed: xrandr: Logical monitors not adjacent`), the screen is
  unchanged and the edited layout stays on the canvas. Mutter keeps
  neighbours adjacent for wxrandr's own size changes (WXRANDR.md).
- **Hotplug**: a monitor plugged in while the window is open is not picked
  up until New (Ctrl+N), arandr's behaviour; the new head appears where
  Mutter put it (right of the row, e.g. `Virtual-4 1280x1024 at 5760x0`).
  Mutter itself discards the temporary layout at hotplug and re-lays every
  monitor out in a row; after the unplug GNOME 46 keeps that row, GNOME 50
  restores the pre-hotplug temporary layout.
- **Keyboard focus after Apply**: when an Apply changes the set of
  monitors (an output turned off or on), Mutter moves the keyboard focus
  off the window, on GNOME 50 the next `Ctrl+S` landed in the desktop
  (Desktop Icons' "Clear Current Selection before New Search" dialog), until it is clicked again. A Wayland client cannot take focus back
  itself, so click the window (a driver: the canvas) before the next
  accelerator; mouse clicks on the boxes and toolbar keep working.
- **Windows on Wayland**: `org.gnome.Shell.Introspect.GetWindows` is
  `AccessDenied` on a stock session and wdotool has no GNOME window
  backend there, so a driver finds the window by screenshot; `super+Up`
  maximizes it so the layout dump's window-relative coordinates become
  absolute after the shell's chrome offset (dock + top bar: (66, 32) on
  24.04, (67, 32) on 26.04 at 1920x1080; `"window"` is then 1853x1048).
  Idle: 0 % CPU, ~70–80 MB RSS, no GTK/GLib warnings on either release.

## Test hooks (env)

- `WARANDR_TEST_LAYOUT_DUMP=FILE`: append one JSON line per redraw
  (`{"kind":"layout","boxes":{name:[x,y,w,h]},"buttons":{...},
  "menubar":{...},"xid":..,"coords":..,"window":[w,h],"settled":..,
  "backend":..,"backend_label":..,
  "factor":..,"status":..,"busy":..,"command":[...]}`), per popup menu
  (`"kind":"menu","name":..,"items":{label:[x,y,w,h]},"modelled":{...},
  "sensitive":{label:bool},"tooltips":{label:text},"active":{label:bool},
  "coords":..`), per status-bar change (`"kind":"status","text":..`), per
  backend indicator refresh (`"kind":"backend","name":..,"forced":..,
  "indicator":..,"word":..,"available":{name:bool},"ok":true`, and
  `"ok":false,"wanted":..,"error":..` for a refused switch), per
  apply (`rc`, `stderr`) and per save (`path`), so xdotool/wdotool can click
  real widgets. Coordinates: `"coords":"root"`, root-window pixels, on X11;
  `"coords":"window"` on Wayland, where a toplevel's position is unknowable
  and everything is relative to the toplevel surface (which includes the
  CSD shadow unless the window is maximized; `"window"` is the surface
  size, `"xid"` null). A layout line is written only once GTK has
  allocated every box at the size and place the redraw asked for
  (`"settled":true`; after 3 s of waiting it is written anyway with
  `false`): the frame clock's layout phase runs after an idle callback, and
  on Wayland it waits for the compositor's frame callback, which stalls
  while Mutter reconfigures outputs right after an Apply, a dump taken
  then would carry the previous boxes. Popup items: GDK cannot read a
  popup's position back on Wayland (`get_origin` is a constant), so
  `"modelled"` holds positions computed from what GTK asked the compositor
  for, a menu popped at the pointer sits at pointer + (1, 1), a submenu at
  its parent item's north-east corner shifted by the menu's
  `horizontal-offset`/`vertical-offset` style and top padding (its first
  item level with the parent item), a menubar drop-down at the item's
  south-west corner; `"items"` is what a driver clicks: GDK's truth on X11,
  the model on Wayland. The X11 GUI test checks the model against GDK for
  all three placements (it is exact under Xvfb); unconstrained placement is
  assumed, near a screen edge the compositor may flip or slide a popup.
- `WARANDR_TEST_SAVE_AS=FILE`: Save As writes there without a file chooser.

## Files

`warandr/{__init__,__main__,cli,randr,xrandr_parse,model,gui}.py`,
`warandr.desktop`, `scripts/build-pyz.sh` (→ `dist/warandr`, bundling
w11common + wxrandr so the Wayland backend runs from inside the pyz),
`pyproject.toml`
console script. Tests: `tests/test_warandr_parse.py` (Xvfb captures, an
xrandr 1.5.4 laptop capture, wxrandr renders for 1–4 outputs),
`tests/test_warandr_model.py` (geometry, snapping, edits, command line,
scripts incl. `tests/fixtures/arandr-saved.sh`, a genuine arandr 0.1.11
save, backend choice, forcing one and asking which it is, the saved
script's backend comment, CLI), `tests/test_warandr_gui.py` (Xvfb + xdotool
drive of the real editor against `tests/fixtures/fake_xrandr.py`, a RandR
simulator rendering through wxrandr's renderers, including the popup
position model against GDK's X11 truth, plus the backend indicator,
Layout ▸ Backend with its greyed-out entries, a switch that re-reads through
the new backend and one that is refused, and warandr's own
`--backend`/`--print-backend`/`--print-backend --verbose`;
`tests/fixtures/gui_probe.py`, the editor in-process with a stub backend, Apply off the main loop, failure keeps edits, layout dumps that wait for the
allocation, popup release, zoom radios, menu shapes, the Save As PATH hint;
a no-display run; skipped
without GTK/Xvfb).
