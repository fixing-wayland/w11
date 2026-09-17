<a id="wmirror-design-contract"></a>

# wmirror: design contract

Mirror an output, or a **region** of one, onto another output by starting and
stopping [`wl-mirror`](https://github.com/Ferdi265/wl-mirror). The Python wrapper
uses the standard library; `wl-mirror` is an external program you must install.

The compositor must expose `zwlr_output_manager_v1` and a supported capture
protocol: `zwlr_screencopy_manager_v1` or `ext_image_copy_capture_manager_v1`.
Examples include sway, Hyprland, Wayfire, labwc and COSMIC. On sessions offering
only the latter capture protocol, `wl-mirror` 0.17 or newer is required. Run
`wmirror --check` to inspect your session; see [detection](#detection) for details.

Unlike the compatibility tools, `wmirror` has no X11 original. It provides region
mirroring that cannot be expressed by an xrandr layout command.

## Quick start

```sh
wmirror --check
wmirror DP-1 --to HDMI-A-1 --region 800x600+0+0
wmirror --list
wmirror --stop HDMI-A-1
```

Replace the output names and rectangle with values from `wxrandr --query`. Source
and destination must not overlap in the desktop layout. For a same-size mirror,
use `wxrandr --output TARGET --same-as SOURCE`, or add `--keep-layout` to wmirror
when the destination must keep its separate position.

## The interface

```
wmirror SOURCE --to TARGET [--region WxH+X+Y] [--scaling fit|cover|exact]
               [--keep-layout] [--replace] [--dry-run]
wmirror --list                # list running mirrors; remove records for exited processes
wmirror --stop TARGET         # or --stop-all
wmirror --check               # can this session mirror at all, and what is missing
```

* **`--region WIDTHxHEIGHT+X+Y`** selects a rectangle in desktop layout coordinates.
  X and Y are measured from the desktop origin, not from the source output's
  corner. Use `wxrandr --query` to find output positions. `slurp` uses the same
  coordinate space but prints `x,y widthxheight`; convert that to the syntax above.

* **`--scaling`** is passed straight through (the wl-mirror paths; the Cinnamon
  path is placed 1:1, below): `fit` letterboxes (default),
  `cover` fills and crops the sides, and `exact` enlarges by whole-number factors
  (2×, 3×, …) or reduces by their reciprocals (½×, ⅓×, …), centering the result.
  Measured on a 1920x1080 → 1280x1024 pair, content box sampled from a
  screendump of the target head: `fit` gives 1280x720+0+152, `cover` fills
  it (1280x1024+0+0, sides cropped), and `exact` gives **960x540+160+242**, wl-mirror centres what it cannot fill, horizontally as well as
  vertically.
* **`--keep-layout`** is the way to ask for a mirror the layout could
  deliver: two same-sized outputs, mirrored while the target keeps its own
  rectangle (so the desktop keeps its area). Without it, that case is
  refused with the `wxrandr` command that does it for free.
* **exit codes**: 0 started (or the picture already exists), 1 refused or
  failed, 2 usage. `--stop TARGET` with nothing running is 1; `--stop-all`
  is always 0.

## The policy, in one sentence

By default, wmirror starts a helper for regions or differently sized outputs.
`--keep-layout` also permits same-size outputs at separate positions. It never
starts a helper when source and target overlap: the capture would include its
own fullscreen window.

What that means at the command line, all decided before the helper is
started:

| situation | wmirror |
|---|---|
| target already at the source's rectangle, same size, no region | **exit 0, starts nothing**: "already shows", because it does |
| same logical size, apart | **refused**, naming `wxrandr --output T --same-as S`; `--keep-layout` overrides |
| different logical size (or transform, or scale) | **runs**, the case a shared position crops |
| any `--region` | **runs**, no geometry expresses it |
| the two rectangles overlap at all | **refused**: the helper would capture its own window |
| region not wholly inside the source | **refused** with the source's rectangle (wl-mirror would silently clamp it, measured) |
| source == target, unknown output, disabled output | **refused**, naming it |
| target already mirroring | **refused** (wl-mirror would run two, the older invisible); `--replace` swaps it |
| source already mirroring the target | **refused**: they would capture each other |

## Why it exists (and why it is this small)

Everything below was measured on sway 1.11 / wlroots, three virtual heads,
two heads screendumped from outside the guest and compared with ImageMagick
(`compare -metric AE` counts differing pixels). The outputs were given
different wallpapers so "whose rendering is this?" is answerable from one
pixel.

**What the layout already does, with no helper at all:**

| two outputs at the same position | the second head shows | evidence |
|---|---|---|
| same logical size | **byte-identical**, the whole frame | `AE 0`, twice, wallpaper and status bar included |
| 1920x1080 vs 1280x1024 | **a crop**, the top-left 1280x1024; the rest is lost | `AE 0` against that crop; `RMSE 14%` against a squash, so not a scale |
| scale 2 (logical 960x540) | the top-left 960x540, magnified | `RMSE 1.0%` against that crop upscaled |
| transform 90 | the leftmost 1080 columns, on their side | un-rotate by -90 → `RMSE 0` |
| partial overlap at x=960 | the shared region byte-identical | `AE 0` over the whole shared region |

Two consequences of that first row decide this whole design:

* **a shared rectangle is a complete mirror**, not merely a shared scene:
  with per-output wallpapers live, both heads showed the *source's*
  wallpaper (`srgb(32,64,128)`), and separating them again put each head's
  own back;
* **a window on the second output's own workspace is drawn on both heads**
  (`AE 77`, the ticking clock only). Per-output workspaces do not partition
  the pixels.

The measurements identify two cases that need a helper and one that does not:

1. **a region** of an output on another output, no layout expresses it.
   **Needs a helper.**
2. **a whole output onto a differently-shaped one**, a shared position
   *crops*, exactly the case KWin needed `set_replication_source` for, and
   `zwlr_output_management_v1` has no replication request. **Needs a helper.**
3. **two outputs that cannot share a position**, **no helper needed**: every shared
   position and every overlap in the table above was accepted, silently.

wmirror handles cases 1 and 2. `--keep-layout` also permits a same-size mirror
when the destination must retain its separate position.

## Why a separate command, and not `wxrandr --same-as`

Three reasons, all measured rather than stylistic:

1. **`--same-as` cannot host it.** wl-mirror is an ordinary xdg-toplevel
   window made fullscreen on the target, not an output driver. Over a
   shared rectangle both heads draw the same composite *including that
   window*, so a mirror window on a target that shares the source's
   rectangle is drawn on the source too, and wl-mirror ends up capturing its
   own picture. wl-mirror's own guard (`fullscreen_output cannot be same as
   the output to be mirrored`) does not catch it: the two outputs are
   distinct objects that merely share a rectangle. A true whole-output copy
   here therefore needs the target to **keep its own place in the layout**, which is not what `--same-as` means, and not what a KWin replica does.
2. **A layout script has to keep running on a plain X11 box.** `warandr`
   saves `~/.screenlayout/*.sh` files that arandr loads and that real
   `xrandr` executes. There is no xrandr syntax for "mirror this rectangle
   there", so anything we invented would be a line arandr cannot parse and
   X11 cannot run. Nothing wmirror does can appear in a saved layout.
3. **wxrandr applies a layout and exits.** This starts a resident process
   that holds most of a core and stops the compositor ever idling (below).
   `--brightness` does leave a holder behind, but a gamma holder is asleep;
   this one is not, and a user who typed a geometry command would not go
   looking for it.

`wxrandr`, `warandr` and the rest are untouched by this feature. No other
package in the tree mentions wmirror, `tests/test_wmirror_cli.py` fails if
one starts to.

## Lifetime

wl-mirror has to outlive the command that starts it, so wmirror follows the
repo's existing precedent for exactly that, `hacks/display/gamma.py`'s gamma
holder: **double-fork + `setsid`**, a `(pid, starttime)` pair in a state
file (`$XDG_RUNTIME_DIR/wmirror-state.json`, wmirror's own, keyed by the
compositor socket, using wxrandr's `State` for its locking, its three-way
merge and its refusal to trust a file that is not ours), a **euid check
before any signal**, and a bounded `SIGTERM` → `SIGKILL`.

One thing gamma does not have: **the process is not ours**. So what we
detach is a small supervisor of our own, and the record carries *two*
(pid, starttime) pairs, the supervisor's and wl-mirror's.

* The supervisor names itself up the status pipe **before** it can fail, so
  even a start that then hangs leaves a record a later `wmirror --stop` can
  act on. There is no unstoppable orphan.
* It watches the layout over `zwlr_output_management_v1`, the protocol
  wxrandr already speaks, and ends the mirror when the source or target
  **disappears or is switched off**, or when the two outputs **come to share
  pixels** (someone ran `wxrandr --same-as` underneath it). wl-mirror only
  handles the first of those itself.
* **If our supervisor is killed**, wl-mirror keeps painting, and the
  record's second pid pair means `--list` still shows it (`(supervisor
  gone)`) and `--stop` still ends it.
* **If wl-mirror dies**, its source unplugged or disabled, the compositor
  restarted, the supervisor exits with it, and the next `--list`/`--stop`
  reaps the record silently.
* A pid whose starttime no longer matches is a different process and is
  **never signalled**, whatever the state file says. Nor is one that is not
  a pid at all: the file is plain JSON and hand-editable, so a value there
  may be `"4242"`, `1.5`, `-1` or a list, and only a positive integer names
  a process. Such a record is reaped like any other dead one, before the
  check, a single mistyped line made `--list`, `--stop` and `--stop-all`
  alike exit 1 with `%d format: a real number is required`, including the
  commands that would have cleaned it up.
* A record written **without** a starttime (`?`, when /proc could not be read
  at spawn time) falls back to matching the process's own **command line**,
  which is where the word `wmirror` appears, the executable name is
  `python3` for a clone, a pyz and `python3 -m wmirror` alike, and matching
  that instead made such a record claim every python process the user was
  running.
* The helper is **told which compositor to talk to** (`WAYLAND_DISPLAY` and
  `XDG_RUNTIME_DIR` set from the socket wmirror found by scanning
  `/run/user/*`), so a mirror starts from a hotkey, from `sudo`, and from
  `ssh root@box` with an empty environment, like everything else here.
* Every command that reads the records **verifies them first**, and a query
  that found nothing to correct does not rewrite the file, a session that
  has never mirrored has no state file at all. A **zombie is not alive**:
  /proc keeps the owner and the start time of a process that has exited and
  not been waited for, so without that test `--list` would print a mirror
  that had stopped painting and `--stop` would report ending it.
* **Two wmirrors at once cannot both start one.** Every command that writes
  the records holds a lock across read-decide-start-write, so the second
  start finds the first record and refuses instead of spawning a second
  helper. (Without it, measured through the real command line: two starts,
  two helpers, one record, and the unrecorded one survives `--stop-all`,
  fullscreen on the target, with nothing in the toolbox able to end it.)
  It is a POSIX record lock (`fcntl.lockf`) on a file of its own: a record
  lock belongs to the process, so the supervisor we fork does not inherit
  it, with `flock` a start that was killed before it could unlock would
  leave the lock held for the life of the mirror (both measured), and it
  is a separate file because closing any fd to a file drops that process's
  record locks on it, which `State.save` does to its own lock file on every
  write.
* **A start that is interrupted still leaves the mirror stoppable.** The
  record is written in a `finally`, so a Ctrl-C in the second the start
  blocks for cannot leave a helper nobody can find (measured before that:
  it did). And a start whose record cannot reach the file at all is
  **stopped again** rather than left painting.
* A **region** mirror ends if its source moves or changes size. A region is
  a rectangle of the layout, resolved against the source once, when
  wl-mirror starts; move that output afterwards and the same rectangle is a
  different picture, or one that is no longer there, and wl-mirror does
  not complain, it clamps. The start already refuses a region that does not
  fit; the layout must not be able to arrange it behind the mirror's back.
  A **whole-output** mirror is left alone by the same move: it follows its
  source, which is what was asked for.

Measured helper behaviour this is built on: source unplugged **or disabled**
→ wl-mirror exits (`output disappeared, closing`); source mode change or
move → survives and re-fits live; **target unplugged → survives**, and sway
relocated the window onto another output, in the measured run, onto its own
source; SIGTERM/SIGKILL → target's own desktop back on the next frame;
compositor restart → broken pipe, exits, no orphan; two mirrors on one
target → both run, the older invisible.

## What it costs

A mirror is not free, and the cost is architectural rather than an artefact
of the test rig. An idle sway desktop repaints only on damage; a running
mirror asks for a frame every frame, so the compositor composites at full
rate for as long as it lives. Measured in the guest (2 vCPU, software
rendering, a real GPU shrinks the absolute numbers, which is *inferred*,
not measured):

| state | wl-mirror | sway |
|---|---|---|
| no mirror, idle desktop | n/a | **0.0%** |
| mirroring a whole output | **88.3%** | **38.7%** |
| mirroring a 220x300 region | 48.0% | 41.6% |
| a second mirror, occluded | **0.0%** | n/a |

Latency was 28–184 ms behind the source, median ~63 ms, against a sampling
floor of ~65 ms, i.e. at or below what the rig could resolve, consistent
with one or two frames. Startup to a visible surface is ~150 ms.

`wxrandr --query` reports the same layout while a mirror runs because `wl-mirror`
is a fullscreen client window, not a new output configuration. All three outputs
retained their geometry during the measurement. Use `wmirror --list` to inspect
running mirrors; it checks that the recorded processes still exist before listing
them.

## Where wl-mirror does not exist

**GNOME and KDE, over wl-mirror, and it is not close.** wl-mirror's own README: it
"does not work on KDE and Gnome", needing wlroots' `zwlr_screencopy_manager_v1`
or the standard `ext-image-copy-capture-v1`. Those are two first-class routes
rather than one and an alternative: sway 1.11, Hyprland and Wayfire publish the
first, COSMIC publishes only the second, and labwc and sway 1.12 publish both.
Neither KWin nor Mutter implements either; on KWin `ext_image_copy_capture_v1`
is an open feature request. Cinnamon (muffin) also publishes neither, but it is
NOT in this paragraph any more: it mirrors over `org.Cinnamon.Eval` and a
Clutter clone instead (the *Cinnamon* subsection below), so wl-mirror is never
the tool there.

On **GNOME and KDE** the route out is the desktop portal's ScreenCast
(AGENTS.md route 4): it exists — GNOME's is backed by `org.gnome.Mutter.ScreenCast`
through `xdg-desktop-portal-gnome`, KDE's by `xdg-desktop-portal-kde` — and the
only work left is the wiring, which asks the user once per session, is useless
from the hotkey a layout script exists for, and would cost a second capture path
in `hacks/mirror/core.py` beside `wl-mirror`. This is the same split
`hacks/display/kwin.py` already records: KWin's `allowInterface` blacklists
`screencast` for unauthenticated clients while never blacklisting
`kde_output_*`, which is exactly why the output half needs no permission and
the capture half does. *(GNOME/KDE from upstream documentation, not re-measured.)*

**Cinnamon: closed at route 2, and it captures nothing.** Cinnamon's ScreenCast
really is absent everywhere — measured on `resolute-cinnamon-wayland` (Cinnamon
6.4.13 / muffin 6.4.1), 2026-09-14: muffin exports `org.cinnamon.Muffin.DisplayConfig`
and `org.cinnamon.Muffin.IdleMonitor` on the session bus and **no**
`org.cinnamon.Muffin.ScreenCast` (`gdbus introspect` → `ServiceUnknown … not
provided by any .service files`); `org.freedesktop.portal.Desktop` exposes
`Screenshot` but no `ScreenCast`/`RemoteDesktop` (count 0), and neither impl
backend (`.xapp`, `.gtk`) serves ScreenCast; and `libmuffin.so.0.0.0` carries
one vestigial `MetaScreenCastWindow` and none of the service strings — the
ScreenCast machinery is compiled out. So rung 1 (a protocol) never existed and
rung 4 (the portal) is empty. That once read as a **not-yet at rung 6** (a
muffin rebuilt with screen-cast). It was wrong: **rung 2 was never probed, and
rung 2 works.** Cinnamon has `org.Cinnamon.Eval` — the same ungated arbitrary-JS
surface the window plane rides (`hacks/window/backend_cinnamon.py`) — and the
compositor is already holding every actor's texture, so a `Clutter.Clone` of an
on-screen actor draws that actor's live pixels wherever it is placed, tracking
it frame for frame with **no capture at all**. `hacks/mirror/cinnamon.py` puts a
clipped `Clutter.Actor` on `global.stage` at the target head's origin and fills
it with one clone per `Meta.WindowActor`/`Meta.BackgroundActor` (walked from
`global.window_group` and `global.top_window_group`) plus the `panel`, each
offset by the region origin.

Measured on the same golden (two heads, monitor 0 = `1280x800+0+0`, monitor 1 =
`1920x1080+1280+0`), 2026-09-14: mirroring region `1000x700+0+0` of monitor 0
onto monitor 1's origin, a QMP `screendump` of head 1 cropped to the region
compares against the same crop of head 0 at **`compare -metric AE` 0, RMSE 0** —
byte identical — over a native Wayland window (gnome-terminal), the XWayland
desktop/wallpaper actors and the panel. It stays 0 after the source's content
changes (the terminal's clock ticked — source-vs-source AE 3350, mirror-vs-source
AE 0, a `Clone` tracks its source) and 0 again after a second window opens (the
`restacked`/`window-created` handlers re-walk the tree); `--stop` destroys the
actor and head 1 shows its own content again (AE 700000). The one thing that
does **not** work, so nobody retries it: a single `Clutter.Clone` of
`Main.uiGroup` (what `magnifier.js` clones) does not mirror across heads — on
this golden its crop mismatched head 0 at every one of 700000 pixels (RMSE 0.29):
only the panel comes through, the windows and wallpaper are black. The per-actor
walk is the working pattern.

What is **not yet** on this path (`AGENTS.md route 2`, the same rung): a
supervisor that watches the OUTPUT layout and drops the mirror when the two
heads come to share pixels or the target is unplugged — what
`hacks/mirror/supervise.py` does for wl-mirror over `zwlr_output_manager_v1` —
is not wired here, because muffin has no wlr output manager; the same watch
would read `org.cinnamon.Muffin.DisplayConfig`. Start-time geometry is fully
policed by `core.decide`; only the live re-check while it runs is owed. The
in-compositor group is torn down by `--stop`, by `--replace`, and when the
session bus goes away. And `--scaling`: the clone group is placed 1:1 at the
target's origin and clipped to the region, so a mirror onto a differently-sized
head is neither letterboxed nor filled; an explicit `--scaling`, `fit` included,
is refused with the sentence `NO_SCALING` carries (`hacks/mirror/cinnamon.py`) —
`cinnamon mirror: --scaling fit is not yet done on this path: the Clutter clone
group is placed 1:1 at the target origin and clipped to the region, so fit, cover
and exact are not applied here` — rather than accepted and not done. The route
is rung 2 again — the same `Eval` program carrying a `set_scale` and a centring
translation on the group — and the cost is a rig measurement
(`resolute-cinnamon-wayland`, two different-sized heads, a screendump of the
target) of what `clip_to_allocation` does under a scaled actor before it ships.
`--list` prints `scaling 1:1` for such a mirror.
*(Cinnamon measured on the rig, 2026-09-14.)*

**X11**: `xrandr --output B --same-as A` mirrors whole outputs, and a region
mirror is not written yet: it wants an X capture client of our own, XShm off
the root window, which is a tool this toolbox has not written. wmirror says so
and exits 1; it never hands over to anything, like `warandr`.

## Detection

Never assumed, always named:

* **no `wl-mirror` on PATH** → `wmirror: wl-mirror is not installed (no
  wl-mirror on PATH)` + `on Ubuntu/Debian: sudo apt install wl-mirror`. That
  second line is table-driven off `/etc/os-release`
  (`wmirror.core.install_hint()`): `on Fedora: sudo dnf install wl-mirror`,
  `on Arch: sudo pacman -S wl-mirror`, `on NixOS: sudo nix-env -iA
  nixpkgs.wl-mirror`, and a family the table does not know keeps the Debian
  bytes. It
  is in Ubuntu **universe**, 24.04 has 0.16.1, 26.04 has 0.18.5, 46 kB,
  no exotic dependencies, so a user's step is one apt line. Both spell the
  three options we use the same way (`--fullscreen-output`, `--scaling`,
  `--region`), which is read from 0.16.1's own man page; only 0.18.5 was run
  on the rig. 0.16.1 has no extcopy backend at all, so on a compositor that
  offers `ext-image-copy-capture-v1` and nothing else it needs 0.17 or
  newer, the failure is then an error from wl-mirror, included in the diagnostic.
* **no capture protocol** (neither `zwlr_screencopy_manager_v1` nor
  `ext_image_copy_capture_manager_v1` in the registry) → say so, name both,
  and name the route out — which is not the same on the three desktops that
  answer this. The line names who has which: `wl-mirror needs a compositor with
  zwlr_screencopy_manager_v1 (sway, Hyprland, labwc, Wayfire, ...) or
  ext_image_copy_capture_manager_v1 (COSMIC, labwc, sway 1.12)`. Then the route:
  on **GNOME and KDE** the desktop portal's ScreenCast exists and only wants
  wiring (route 4). **Cinnamon is not reached by this line at all**: it publishes
  neither capture protocol either, but `wmirror` mirrors there over
  `org.Cinnamon.Eval` and a Clutter clone (route 2, measured AE 0 — see *Where
  wl-mirror does not exist* above), so the start and `--check` take that path
  before this refusal is built. `zwlr_export_dmabuf_manager_v1`
  does not count: it is what wl-mirror's `auto` picks for a whole output,
  and a **region cannot use it** (measured: a region falls back to shm).
* **no `zwlr_output_manager_v1`** → the layout is read the compositor's own
  way instead. wmirror reads it through the very reader `wxrandr --query`
  picks for this session (`hacks/display/core.pick_backend`): the wlr floor
  wherever `zwlr_output_manager_v1` is advertised (sway, Hyprland, labwc,
  river, Wayfire, COSMIC), and where it is not, KWin's
  `kde_output_management_v2`, GNOME's `org.gnome.Mutter.DisplayConfig`, or
  Cinnamon's `org.cinnamon.Muffin.DisplayConfig` on the session bus (route 2).
  Measured on `resolute-cinnamon-wayland` (Cinnamon 6.4.13 / muffin 6.4.1),
  where the compositor advertises no wlr output manager: `wmirror --check`
  used to print `this compositor does not advertise zwlr_output_manager_v1, so
  wmirror cannot read the output layout` on its `outputs:` line, and now prints
  the three Muffin heads there, byte-identical to `wxrandr --query`'s
  geometry. Only a session that advertises none of the six output protocols is
  refused, and it is named a dead end (`this compositor speaks no output
  protocol wmirror can read the layout from`), not a gap — there is no lower
  rung left to point at. The **capture** half on Cinnamon is the Clutter clone
  over `org.Cinnamon.Eval` (route 2, measured AE 0, *Where wl-mirror does not
  exist* above): reading the layout gives the target head's origin, and the
  clone paints the region onto it — so both halves are route 2, and neither is
  a not-yet at route 6 any more.
* `wmirror --check` prints all of it, plus the outputs and what is running,
  and exits 1 if anything is missing.

The helper's stderr is **never** read as failure: `libEGL warning:` and even
`error:` lines appear there while it works perfectly. It goes to an unlinked
temp file rather than a pipe, a pipe fills if nobody drains it (stalling the
helper) and, after our own death, would kill it with SIGPIPE at an
unpredictable moment, which is exactly the mirror that is supposed to stay
alive and stoppable. The file is truncated when it grows past 64 KiB, and it
is read only when the process actually exits during the first second: that is
what turns wl-mirror's `error: options::find_output(): output X not found`
into our one-line refusal.

The chatter is skipped even then. Live, a mirror that ran for minutes and
matched its source pixel for pixel printed `error:
mirror-screencopy::on_dmabuf_allocated(): failed to allocate dmabuf` and
fell back to shm; blaming that line for a helper that died of something else, a signal, an exit with nothing said, is a confident wrong answer, so a
dmabuf/EGL/Mesa line is never the verdict, and a helper killed by a signal
is reported by the signal's name.

On an **X11** session with no `wl-mirror` installed, the missing package is
not what to say: there is no wl-mirror for X11 and never will be. The
session is checked before the binary, so the answer is `xrandr --same-as`
and not an apt line.

## Deliberately out of scope

* **A warandr GUI surface.** It would save a script X11 cannot run, see
  reason 2 above.
* **`--stream` mode.** wl-mirror can be re-aimed on stdin without a restart;
  worth having only once someone wants to move a region live.
* **`--no-show-cursor`.** The cursor is captured and mirrored by default
  (measured: pointer at layout 900,600 changed the target at exactly the
  fitted 600,552). The negative test was inconclusive on the rig, so the
  option is not exposed rather than documented on a guess.
* **Restarting a mirror the compositor ended.** When an output comes back,
  the user (or their layout script) starts it again; nothing here polls for
  hardware.

## What the live pass settled

Run on sway 1.11 with wl-mirror 0.18.5, three heads with different
wallpapers and a window placed where only a region mirror can reach it,
every head screendumped from outside the guest:

* **The self-capture premise, as one experiment.** Two 1920x1080 outputs at
  0,0, raw `wl-mirror --fullscreen-output Virtual-2 --scaling fit
  Virtual-1`: **both heads went entirely black**, every sampled pixel
  `000000`, the two heads `AE 0` against each other and `AE 2073600`
  (every pixel) against the same head before the mirror. The feedback
  destroys the desktop it was meant to copy. The whole overlap-refusal set
  rests on this and it holds.
* **The pictures.** A region mirror matched its source to `RMSE 0.46%`
  (resampler noise) with the letterbox bars black rather than the target's
  own wallpaper; the whole-output case onto a smaller head measured
  `RMSE 0.25%`; and the window that a shared position crops away came back
  in both.
* **The simple path still wins.** `wxrandr --output B --same-as A` gave
  `AE 0` including that window; `wmirror A --to B` then exited 0 saying so,
  with no helper started.
* **The watch is ours, not wl-mirror's.** With the *target* head unplugged,
  a raw wl-mirror kept running (invisible, on no output); under wmirror the
  helper was gone. Disabling the target with `wxrandr --off`, and running
  `wxrandr --same-as` underneath a running mirror, both ended it within a
  poll.
* **Nothing was left behind** in any transition, including a SIGKILLed
  supervisor (helper survives, `--list` shows `(supervisor gone)`, `--stop`
  ends it) and a killed compositor (both gone, the stale record refused by
  its start times and rewritten empty).
* **The cost is real**: sway at ~60% of a software-rendered core with one
  region mirror, from an idle desktop.

Re-verified on the same rig after a review, each of these being a fix that
came out of running the code rather than reading it:

* **The region watch, live.** With a region mirror running, moving its
  source (`wxrandr --output Virtual-1 --pos 0x1080`, no overlap created)
  ended the mirror **before the `wxrandr` call returned** and reaped the
  record. The identical move under a **whole-output** mirror left it running
  and listed, the discrimination is real, not incidental. And raw
  `wl-mirror` in the same situation kept running and kept painting a full
  head (`AE 1307904` against the bare desktop, i.e. every pixel): it says
  nothing about a region whose source has moved, which is the whole reason
  the supervisor has to.
* **Two starts at the same moment**, one target: one helper, one record, the
  loser refused by name (`Virtual-3 is already mirroring Virtual-1`). Before
  the lock, the same race through the same command line left two helpers,
  one record, and an orphan that survived `--stop-all`.
* **A start interrupted 0.4 s in** (`timeout -s INT`) left the helper
  running *and* recorded: `--list` found it, `--stop` ended it.
* The policy set, `--replace`, `--stop-all` and the overlap refusals are
  unchanged, and a region mirror still paints (head 2 differs from its own
  desktop by 1307904 pixels while it runs, and comes back byte for byte
  when it stops).

Still not measured, and marked as such:

1. **`--no-show-cursor`**, which is why it is not exposed: QEMU's
   `screendump` never composes the hardware cursor plane, so the rig cannot
   answer it either way.
2. **The GPU numbers.** The CPU table is llvmpipe; only the shape of it
   ("the desktop never idles again") is claimed to generalise.
3. **wl-mirror 0.16.1** (Ubuntu 24.04) with the exact argv we build. The
   three options are read from its own man page; only 0.18.5 was run.

## Tests

* `tests/test_wmirror_cli.py`, the policy table above, region syntax, the
  argv built for each case, detection and its absence, the queries, and that
  no other package learned a new word.
* `tests/test_wmirror_lifetime.py`, every lifetime transition, driven
  against a stub standing in for wl-mirror (one that prints the libEGL
  chatter the real one prints, and that can fail at startup or die later):
  start, stop, replace, reap, a killed supervisor, a dying helper, a
  disappearing output, a compositor that goes away, `SIGTERM` then
  `SIGKILL`, and a recycled pid that must never be signalled, plus what a
  review found by running it: a zombie helper, two commands at once, a
  start interrupted mid-flight, a start that cannot be written down, the
  region watch, and what a dead helper is blamed on.
* `tests/test_wmirror_live.py`, the same tool against a **real headless
  sway** (`swaymsg create_output` for the second head, the stub still
  standing in for wl-mirror): `--check` reading the capture protocols and
  both heads off the real registry, a start that is really listed, and the
  two output changes that must end a mirror, `swaymsg output HEADLESS-2
  disable`, and moving the target onto the source, driven through sway
  rather than through a fake. It is the only place the supervisor's watch
  reads real `zwlr_output_management` events; everywhere else a double
  decides both what an event looks like and when it arrives.
