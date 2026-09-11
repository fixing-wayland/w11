# wxrandr — design contract

Drop-in `xrandr` clone for Wayland with **first-class multimonitor**: query and
reshape real multi-output layouts — relative positioning, mirroring, rotation,
reflection, per-output scale, custom modes, monitors — the crazy configurations are
the point, not an afterthought. House rules per Technical.md.

## On Wayland, with xrandr installed, this runs xrandr

Since the X11 proxy, `wxrandr` hands over on a **Wayland** session as well as on an X11 one:
the original is exec'd with `DISPLAY` pointing at [`xw11`](XW11.md), which answers for
native Wayland windows as well as for Xwayland's. The clone still runs when the original
is not installed, when `W11_PROXY=never` (or `WXRANDR_PROXY=never`) asks for it, when
`W11_PASSTHROUGH=never` is set, and whenever the command uses one of our own options --
`--persistent`, `--backend`, `--backends`, `--print-backend` and the four
`--*gnome-overlap*` flags.
Everything this document says about the clone's output is still what the clone prints;
through the proxy the output is the original's, byte for byte, because it **is** the
original. The six rules in order are
[XW11.md § The wrapper rules](XW11.md#the-wrapper-rules).

`wxrandr` stays the front end for the things xrandr has never had: `--persistent`,
`--backend`, `--backends`, `--print-backend` and the GNOME overlap consent flow. A
command carrying any of those runs the clone on every session. Everything else on a
Wayland session with `xrandr` installed is the original, through the proxy -- and an
apply through the proxy is always a temporary, non-overlap apply.

## Planes / backends

- **sway/i3-compatible (flagship)**: query from `GET_OUTPUTS` (+ `w11common.wayland_mini`
  wl_output for physical mm sizes); mutate via `output ...` IPC commands
  (mode/--custom, position, transform, scale, enable/disable, dpms).
- **Generic wlroots**: `zwlr_output_management_unstable_v1` over `wayland_mini` —
  atomic apply of whole-layout configurations, which is exactly xrandr's model. This is the
  backend that makes crazy configs atomic: build the full config, apply once, handle
  `succeeded/failed/cancelled` events.
- **wlroots scale arithmetic** (`core.wlr_scale` / `core.logical_size`, both
  backends above): sway quantises any scale it is handed to 120ths —
  fractional-scale-v1's unit — in float32 (`scale = round(scale * 120) / 120`,
  sway 1.9 `output.c`), and `wlr_output_effective_resolution` then divides the
  pixel size by that float and truncates. **What it is handed depends on the
  transport**: the sway IPC takes the number as text (`output NAME scale 1.03`),
  while `zwlr_output_management` takes a `wl_fixed` that `wayland_mini`'s
  marshaller truncates to 256ths — so `--scale 1.03` runs as 1.0333 on the sway
  backend and as 1.025 on the wlr one, and `--query` will say so. Both steps are
  single precision and both matter: a double division puts 1920 ÷ 1.6 at 1199
  where the compositor has 1200. The wlr backend is one atomic call with no
  phase-2 re-read, so a position computed from the number the user typed is the
  position the layout keeps — measured against a live sway at 201 scales per
  backend (`tests/test_wxrandr_unit.py::WlrootsScale` pins the captures,
  `test_wxrandr_live.py::test_42` re-measures).
- **--brightness**: gamma via `zwlr_gamma_control_manager_v1` (ramps computed like
  xrandr's gamma math, passed over an fd). The control dies with its client, so a
  non-1.0 brightness forks a tiny detached holder process per output (pattern: the
  wdotool daemon fork, simplified); brightness 1.0 kills the holder. Insane, works.
  Headless outputs (WLR_BACKENDS=headless sway) have no gamma LUT, so the
  compositor refuses the control immediately — `--brightness`/`--gamma` there exit
  1 with `xrandr: Gamma size is 0.` (verified live). The holder lifecycle is
  therefore proven against a wire-level mock (tests/test_wxrandr_gamma.py), not a
  headless session. A typo'd `--output NAME --brightness` prints only the bare
  not-found warning and exits 0, like real xrandr (no holder is spawned).
- **GNOME / Mutter**: `org.gnome.Mutter.DisplayConfig` on the session bus over the
  pure-stdlib `w11common.dbus_mini` — see "Mutter backend" below. Stock Ubuntu 24.04
  (GNOME 46) and 26.04 (GNOME 50), no extension, no root.
- **KDE Plasma / KWin**: the plasma-wayland-protocols pair `kde_output_device_v2`
  (read) + `kde_output_management_v2` (write) over `wayland_mini` — see "KWin
  backend" below. Unauthenticated (no portal, no polkit): the same path
  kscreen-doctor and the System Settings KCM take. Plasma 5.27 (Ubuntu 24.04)
  through 6.7+, no extension, no root.
- **Hyprland**: `hyprctl -j monitors all` to read, `hyprctl keyword monitor` to write,
  both over Hyprland's own IPC socket — see "Hyprland backend" below. It is second in the
  auto order, after sway, so a Hyprland box never touches the session bus to choose a
  backend.
- **Cinnamon / muffin**: `org.cinnamon.Muffin.DisplayConfig` — Mutter's DisplayConfig
  under Muffin's bus name, and the Mutter backend with three names swapped. See "Mutter
  backend" below; everything it says about adjacency, gaps, mirroring and one-primary
  holds verbatim, because muffin carries Mutter's validator with Mutter's strings.

## xrandr options that mean nothing here

The usage text is xrandr's, byte for byte, so it lists options that describe an X
server rather than a compositor. They are accepted rather than refused, because a
script written for an X11 machine should keep working, and what each one does is:

| option | what happens |
|---|---|
| `--nograb` | accepted, ignored. It asks the X server not to grab, and there is no X server |
| `--screen N` | accepted, ignored. Wayland has one screen and it is 0 |
| `--display D`, `-d D` | the compositor to talk to, not an X display: a Wayland socket name such as `wayland-1`. On an X11 session it is handed to the real xrandr and means what it always meant |
| `--q12`, `--q1` | accepted, ignored. They pick an X extension version to speak |
| `--fb WxH` | accepted, ignored: the desktop size follows the monitors, and no compositor lets a client set it directly |
| `--fbmm WxH` | accepted, ignored, as above but in millimetres |
| `--dpi N` | accepted, ignored. Scaling is `--scale` and the compositor's own factor |
| `--crtc N` | accepted, ignored, and still refused before `--output` as xrandr refuses it. A CRTC is a piece of X server bookkeeping |
| `--prop`, `--properties` | **does something**: adds each output's properties under it, the way xrandr does |
| `--delmonitor NAME` | accepted, ignored: the monitor list is the compositor's and cannot be edited |
| `--orientation R` | xrandr's own spelling of `--rotate`, taken as an alias for it |
| `--refresh N` | xrandr's own spelling of `--rate`, taken as an alias for it |
| `--panning` | parsed and refused by name, because a compositor that could pan would have to be asked for it and none of the four can |

Ignored means exactly that: the option is consumed, nothing changes, and the rest of
the command runs. Nothing here warns about them, because a script that carries
`--nograb` is not asking a question.

## Backend selection

Precedence, one rule: **`--backend NAME` beats `WXRANDR_BACKEND=NAME` beats
auto-detection.** `NAME` is `auto` (the default), `x11`, or one of
`sway|hypr|wlr|kwin|mutter|cinnamon` (aliases `kde`, `gnome`, `muffin` and
`hyprland`). Detection: a sway/i3 IPC socket wins, then a Hyprland IPC socket,
then a compositor advertising `kde_output_management_v2`, then a session bus
owning `org.gnome.Mutter.DisplayConfig`, then one owning
`org.cinnamon.Muffin.DisplayConfig`, then wlr — which is the fallback and is
therefore never probed for the decision. The KWin probe *is* the Wayland
connection the backend then keeps, so a KDE session still opens exactly one,
and a backend forced with the flag is probed the same way. Whatever the other
probes opened on the way is closed as soon as the backend is chosen, rather
than left to the collector — an unclosed socket comes back as a
`ResourceWarning` on stderr at an arbitrary later moment.
`--listproviders` names the chosen one (`name:sway`, `name:hypr`, `name:wlroots`,
`name:kwin`, `name:mutter`, `name:muffin`). `--print-backend --verbose` names the
compositor as well as the protocol, and two of those names are read off the session
rather than off the backend token: `compositor: COSMIC (wlr-output-management)` when the
registry also carries `zcosmic_output_manager_v1`, and
`compositor: wlroots (XDG_CURRENT_DESKTOP=labwc:wlroots)` — or `Budgie`, `XFCE`,
`LXQt:labwc:wlroots` — when the variable is set and is not `sway`. The token stays `wlr`
either way, because it is the same backend.

`x11` means *hand over to the real xrandr* — what happens by itself on an X11
session. That handover is an `execve` at the top of `main()`, **before any
option is parsed**, so the hook looks ahead in argv for `--backend NAME` and
`--backend=NAME` (and for the two informational options below) and honours
it: `--backend sway` on an X11 session runs our own code, `--backend x11` on
a Wayland session hands over, and the flag is stripped from the argv the
original is exec'd with (real xrandr has no such option). The look-ahead
walks argv with xrandr's own option arities, so an output literally named
`--backend` (`--output --backend --off`) is a value there too and is handed
over untouched.

**Nothing else of ours reaches the original either.** `--persistent` is
stripped from the handed-over argv as well, so an X11 apply that asks for it
applies and simply saves nothing, which is what this document has always said
an X11 apply does; it used to be handed on, and the real xrandr answered
`unrecognized option '--persistent'`, exit 1, layout unchanged.
`--unsafe-gnome-overlap` is refused before the handover in the same words a
KDE or a sway session refuses it in — this is not GNOME, and X11 places
overlapping monitors without any of it. The three bookkeeping options
(`--gnome-overlap-status`, `--gnome-overlap-allow`, `--gnome-overlap-forget`)
answer for themselves on every session and never hand over.

Three options real xrandr does not have, and — like `--persistent` — not in
its usage text, so `--help` and every other byte stay xrandr's:

* **`--backend NAME`**. An unknown name lists the valid ones
  (`xrandr: --backend: invalid argument 'banana'; valid: auto, x11, sway,
  hypr, wlr, mutter, cinnamon, kwin` + xrandr's `Try 'xrandr --help' for more information.`,
  exit 1). A backend that is not available *in this session* is one clear
  line naming what was missing and exit 1, never a silent fallback:
  `xrandr: --backend sway is not available in this session: no sway or i3 IPC
  socket ($SWAYSOCK)`. A `--backend` with no value at all is our own
  `xrandr: --backend requires an argument`, on either kind of session: the
  look-ahead keeps the flag's *presence*, or an X11 session would hand
  `--backend` to the original and answer with its `unrecognized option`
  instead. `$WXRANDR_BACKEND` deliberately keeps its older behaviour — no
  pre-check, so it still fails the way the backend itself fails (`Can't open
  display`, `org.gnome.Mutter.DisplayConfig is not on the session bus`) and
  no existing byte moves — with one exception: `WXRANDR_BACKEND=x11` asks
  for the real xrandr exactly as `--backend x11` does, on any session. That
  is the one thing the variable gets to say about the handover, and it has
  to: the handover is decided before parsing, so a variable left to reach
  the backend selection would only be able to ask this process to be a
  thing it can no longer become.
* **`--print-backend`** prints the chosen backend and exits 0 without
  touching the layout (and without handing over, so it answers on X11 too).
  First line: the bare token, for scripts. `--verbose` adds the rest.

  ```console
  $ wxrandr --print-backend
  mutter
  $ wxrandr --print-backend --verbose
  mutter
  session: wayland
  chosen by: detection
  compositor: Mutter
  protocol: org.gnome.Mutter.DisplayConfig (D-Bus)
  available: yes
  ```

  `chosen by:` is `detection`, `flag (--backend mutter)` or
  `environment (WXRANDR_BACKEND=mutter)`; `protocol:` carries the version
  where the protocol has one (`kde_output_management_v2 version 12`,
  `zwlr_output_manager_v1 version 4`), `compositor:` sway's own
  `GET_VERSION` string where it has one; and for `x11` there is a
  `real xrandr: /usr/bin/xrandr` line naming what would be exec'd.
* **`--backends`** — one line per backend, its availability in this session,
  a short true reason when it has none, and `*` on the one *auto* would
  choose (not on a forced one — `--print-backend` answers that):

  ```console
  $ wxrandr --backends
    sway      unavailable  no sway or i3 IPC socket ($SWAYSOCK)
    hypr      unavailable  no Hyprland IPC socket ($HYPRLAND_INSTANCE_SIGNATURE)
    kwin      unavailable  the compositor does not advertise kde_output_management_v2
  * mutter    available    org.gnome.Mutter.DisplayConfig on the session bus
    cinnamon  unavailable  org.cinnamon.Muffin.DisplayConfig is not on the session bus
    wlr       unavailable  the compositor does not advertise zwlr_output_manager_v1
    x11       available    /usr/bin/xrandr
  ```

  The name column is 8 characters wide because `cinnamon` is the longest token
  `--backend` takes; a narrower one would put a row's state and reason out of line with
  every other's.

  This is what warandr greys its Backend menu with.

Tests: `tests/test_wxrandr_backend.py` (hermetic — the probes are a table, no
socket or bus is touched: precedence, the detection order, the look-ahead in
both directions, the two outputs byte for byte, every error path) and, for
the handover itself, `BackendFlag` in `tests/test_passthrough_exec.py`
against the fake install tree.

## Overlapping outputs

wxrandr has no geometry policy of its own and never gains one. An overlapping
`--pos` is resolved, normalised and sent exactly as asked, on every backend;
so is a rotation or a mode change that runs one output into its neighbour.
Overlap is what X11 has always done — every output is a viewport into one
framebuffer — and refusing it here would refuse what real `xrandr` accepts.
The guards that stay are the ones protecting against something real: no
enabled output at all (KWin), a negative origin the protocol rejects
(`normalise`, KWin's "Position of enabled output %1 is negative"), Mutter's
no-holes follow-your-neighbour shift, which never moves an output the
invocation positioned explicitly.

What each compositor then does with it, measured on two 1920×1080 heads with
the second placed at x=960 (the shared region cropped from each head and
compared with ImageMagick `compare -metric AE` plus an md5 of the raw RGB):

| backend | geometry | shared region | evidence |
|---|---|---|---|
| `x11` | taken, silently | **same pixels** | `--pos 960x0` exit 0, no output; screen 3840×1080 → 2880×1080; `AE 0`, `PAE 0`, both crops md5 `c05208d2…` (measured, Xorg) |
| `kwin` | taken | **same pixels** | `AE 0` and equal md5 at 960 px *and* 480 px of overlap; no warning from KWin. The XML's "no gaps or overlaps" sentence is not enforced by the code, and KWin renders each output as a view onto one shared scene (measured, Plasma 6 / KWin Wayland) |
| `sway` / `wlr` | taken | **same pixels** | `swaymsg … position 960 0` → `"success": true`; a window floated to layout 1100,300, wholly inside the region, is drawn on **both** heads, `AE 0`, equal md5 (measured, sway 1.11 / wlroots) |
| `mutter` | **refused** | n/a | `ApplyMonitorsConfig` → `Logical monitors not adjacent` for x = 0, 100, 960, 1919, 1921, 2500 — every layout that is not exactly edge-adjacent, overlap and gap alike, adjacency being checked first. `GetCurrentState` is unchanged afterwards: nothing is half-applied. `--same-as` (one logical monitor, two members) *is* accepted (measured, GNOME 46 / Mutter) |

All four rows are measured. **Inferred**, and only inferred: the `wlr` backend
beyond sway (same wlroots renderer; only sway 1.11 was on the bench); KWin
5.27 (only Plasma 6 was measured); and Mutter's other string, `Logical
monitors overlap`, which two monitors never produce. The expectation that
sway's per-output workspaces would prevent mirroring was **wrong** — a
workspace binds where the tiler *places* windows, not which pixels an output
scans out.

### Why Mutter refuses (and what the refusal is not)

Read against stock GNOME 46.0 and 50.1. The refusal is **one validator on the
way in**: `meta_verify_logical_monitor_config_list()`, in
`src/backends/meta-monitor-config-utils.c`, walks the submitted logical
monitors and requires each one to share an edge with another by *exact integer
equality*. Adjacency is checked before anything else, which is why one
sentence, `Logical monitors not adjacent`, covers a gap and an overlap alike.
It is not a permission check and it is not something a client can route
around: gnome-control-center's own Displays panel submits the same
`ApplyMonitorsConfig` and gets the same sentence back.

**Nothing else in Mutter needs the invariant.** Monitor lookup by point
returns the first match, lookup by rectangle falls back to the primary,
pointer constraints clamp only when the pointer is in no view at all, the
screen size is a bounding box, and the renderer already builds several stage
views over identical rectangles, because that is how mirroring works. GNOME's
*Xorg* session goes further and never calls the verifier: it derives its
logical monitors from whatever layout X reports, so overlapping logical
monitors already exist inside Mutter the moment somebody runs plain `xrandr`
on GNOME/X11. An overlap is therefore a state Mutter can hold perfectly well
and one function declines to accept from a client, which is what makes it a
limitation rather than a law of nature, and why the identical layout is taken
as drawn by KWin, by wlroots and by X (the table above).

Every supported route in runs through that same function: `ApplyMonitorsConfig`
validates before it applies on all three methods, verify included, so
`--dryrun` gets the same no. **The saved configuration file is worse than
closed**, and that is the one thing here worth a warning of its own:

> **Never hand-edit `~/.config/monitors.xml` to force an overlap.** Mutter's
> parser calls the same verifier, and a failure discards the **entire file**,
> so one bad entry silently destroys every other monitor arrangement the user
> had saved, at every boot, with the only trace in the system journal. Mutter's
> own writer does not validate, either: `meta_monitor_config_manager_save_current()`
> will happily write an overlapping layout into the file that the reader then
> rejects in full, for ever.

One route is not closed, and since 0.4 it is packaged: a GNOME Shell extension
can reach the non-introspected libmutter symbols by shipping a type description
of its own. It is opt-in, it is off, and it has a whole section to itself
below. The long form of every closed route is
[Technical.md § 6](Technical.md#why-mutter-refuses-monitors-that-share-area).

### `--unsafe-gnome-overlap`, the one route through

**Off, and its name is the whole warning.** Nothing else in this CLI begins
`--unsafe-`, no `xrandr` manual page contains the string, no environment
variable or config file sets it, and `warandr` has no button for it. It exists
because the refusal above is one function's opinion rather than anything the
compositor needs, and because every other desktop takes the same layout as
drawn. It is also the only thing in this repository that can cost you the
session you are sitting in, and the rest of this section is that sentence in
detail. A reader who finishes it and decides against the flag has used this
document correctly.

#### What happens, in the order it happens

**1. Nothing, unless GNOME refuses the layout.** With the flag typed, a layout
Mutter accepts goes down the same `ApplyMonitorsConfig` path it always did, and
the extension is not so much as asked whether it is installed. Only a layout
Mutter's own validator would reject — an overlap or a gap, one sentence for
both — changes route. The risky code is not reachable by the configuration
almost everybody has, and that is deliberate.

**2. Refusals that need nothing outside this process.** Before a bus call is
made:

* `--persistent`, in either argument order. The file that saves to is read back
  through the very validator this flag exists to get past, and one entry it
  refuses discards every other saved arrangement at every boot, for ever;
* any backend but `mutter`. KDE, wlroots and X place the layout as drawn, so
  there is nothing here for them to buy;
* a GNOME Shell whose private layout has not been measured — the table,
  read from the shell's public `ShellVersion` property, because a wrong offset
  does not raise an error, it writes into the heap. This is the one refusal that
  can be overruled, deliberately and per invocation, by
  [`--unsafe-gnome-overlap-unmeasured`](#forcing-past-a-refusal-on-a-gnome-nobody-has-measured);
  it is also the one whose message is written for a maintainer, and it costs one
  extra call: a `Probe` that refuses at its own first check, having read nothing
  private, so that the message can name the `MetaMonitorsConfig` size this build
  reports;
* an invocation that changes anything but positions: a mode, a scale, a
  rotation, the primary, which outputs mirror. The extension writes two words
  per monitor and can do none of that, so the answer is to make that change the
  ordinary way first;
* an extension that is not on the bus, answered with the line that installs it.

**3. The warning**, in full, on stderr, before the call: what moves and to
where, that eight bytes per monitor are going into `gnome-shell`'s own memory,
which checks stand between that and a dead session, that nothing is saved, the
exact `wxrandr` line that undoes it — built from the layout running *now*, and
going through DisplayConfig, so the way back does not depend on the dangerous
half still working — and the way back from a session that will not start.

It is printed on every invocation until it is *agreed to*, once, for the build
it was measured on: see [Agreeing once](#agreeing-once-and-withdrawing) below.
An agreement changes nothing but this paragraph; every guard in the two lists
below runs whether or not one exists.

There is **no confirmation prompt on the command line**, and that is a decision
rather than an omission. The flag is the confirmation: it cannot be typed by
accident and it appears in no `xrandr` manual. A prompt would have to be
skippable to keep the tool usable from a script, and a guard with a documented
bypass protects nobody, while the guards that do the work run whether or not a
human answered a question. GNOME's own "are you sure" is the *Keep changes?*
dialog with its 20-second revert, and this path cannot have it, because it does
not go through the D-Bus method that arms the timer. What is offered instead is
a printed undo and a printed way back.

`warandr` does ask, once, and for the same reason turned round: a GUI has no
flag to type, so its dialog *is* the flag — the deliberate act that the command
line gets from the spelling of the option. It is not a guard either, and it
skips nothing: what it can record is this agreement, and this agreement decides
what is printed. [WARANDR.md § Overlapping monitors on
GNOME](WARANDR.md#overlapping-monitors-on-gnome).

**4. The write.** `w11-overlap@w11`
([gnome/README.md](../gnome/README.md#the-other-extension-w11-overlap))
loads a type description of its own for symbols libmutter *exports but does not
publish* — they are in `nm -D` and absent from the introspection data — reads
the configuration object the session is running, and writes two 32-bit words
per moved monitor: the `x` and `y` of a `MetaLogicalMonitorConfig`, at an
offset that is a private implementation detail of one libmutter generation.
Then it calls `meta_monitor_manager_apply_monitors_config()`, which applies
what it is given without validating it.

Say that plainly: **this is not an API.** It is one program editing another
program's memory, using knowledge of a structure layout that nobody promised
would stay put, in a process whose death takes the desktop with it. It is true
of the three builds it has been measured on and of nothing else.

```console
$ wxrandr --unsafe-gnome-overlap --output Virtual-2 --pos 960x0
xrandr: --unsafe-gnome-overlap: GNOME will not place these monitors, so they are going to be
  placed by writing into the running gnome-shell instead of asking it.
  What it does:         move Virtual-2 from +1920+0 to +960+0
  ...
  To undo:              wxrandr --output Virtual-1 --pos 0x0 --output Virtual-2 --pos 1920x0 --output Virtual-3 --pos 3840x0
                        or log out and back in.
xrandr: GNOME's rule this breaks: logical monitors not adjacent (an overlap counts, and so does a gap)
mutter's own validator on the result: refused: Logical monitors not adjacent
/home/test/.config/monitors.xml: unchanged (absent)
```

The `mutter's own validator` line is not decoration. The verifier is run on the
mutated configuration as a **positive control**, and it has to *refuse* it: if
Mutter's own validator accepts what was built, the write did not land on the
field that validator reads, and nothing is applied. The line under it is the
saved configuration file's digest, compared before and after the call.

#### Agreeing once, and withdrawing

The paragraph above is right the first time somebody does this and noise the
fiftieth, and a warning that is noise is not read. So it can be agreed to —
deliberately, once, and *for one build*:

```console
$ wxrandr --gnome-overlap-allow
check shell-version: GNOME Shell 50.1, libmutter-18.so.0 (build 25d36850030c)
check typelib: W11Overlap18, MetaMonitorsConfig 80 bytes as declared
check sentinel: switch_config round-tripped at the declared offset
check pending-dialog: nothing holds a modal grab, so GNOME is not asking "Keep changes?"
check bounded-read: 2 logical monitors, every address range-checked
check public-view: identical to Mutter's public view (global.display + MetaMonitorManager.get_monitors)

Agreeing to --unsafe-gnome-overlap on GNOME Shell 50.1 (libmutter-18 build 25d36850030c, MetaMonitorsConfig 80 bytes).

  What it does:         places monitors GNOME's own validator refuses, by writing
                        ...
  What is agreed:       this build and no other.  The agreement records
                            GNOME Shell 50.1 (libmutter-18 build 25d36850030c, MetaMonitorsConfig 80 bytes)
                        and stops applying the moment any of that changes, which
                        is what a distribution upgrade does.
  What is not agreed:   any of the checking.  Every check above runs again on
                        every single apply, agreed or not, and a build this does
                        not recognise is refused however old the agreement is.
  To withdraw:          wxrandr --gnome-overlap-forget
  ...
recorded in /home/test/.config/w11/overlap-consent.json
```

**The probe comes first and the record second**, never the other way round:
`--gnome-overlap-allow` runs all six checks and writes nothing to the record
unless they pass, so there is no way to agree to a compositor they have not just
run on. The four facts it stores are four the checks *measured* — the Shell
version string, libmutter's generation, the size this build's
`MetaMonitorsConfig` turned out to be, and the GNU build id of the `libmutter`
the session has mapped — read out of the running GType registry and out of the
mapped library's own ELF note, and relayed by the extension. None of them is
typed anywhere in this tree.

**Why the build id is one of them.** Because the version string cannot see the
update a user is most likely to get. Ubuntu bumps mutter's upstream version
inside one Shell major — 24.04 has carried mutter 46.2 under GNOME Shell 46.0
for most of its life — so `ShellVersion` reads the same before and after. That
was measured both ways: on 24.04, swapping libmutter 46.2 for the GA 46.0 under
an unchanged shell left every check green and the layout unmoved, and on 26.04
the `-proposed` update (gnome-shell 50.1-0ubuntu1.3, libmutter 50.1-0ubuntu2.3)
did the same. Without the build id, an agreement given on one binary would have
carried silently to another, which is not what "this build and no other" says.
With it, the run after such an update prints the difference by name, deletes the
record, and asks in full next time:

```console
xrandr: --unsafe-gnome-overlap: this GNOME is not the one that was agreed to
(libmutter build 286710f8eb3e, not 25d36850030c); the agreement has been
withdrawn and the next run will ask again
```

It is bookkeeping and not a guard, in the same tier as the two audits below: the
library in memory is the one all six checks just ran against, so a new build is
news, not danger. It is read from the ELF note of the file the mapping came from,
and never read at all when that file is no longer the mapped one (below).

**Where it lives.** `$XDG_CONFIG_HOME/w11/overlap-consent.json`, else
`~/.config/w11/overlap-consent.json`. Per user, because it is the user's
own session a wrong offset ends and root's answer must not stand in for anybody
else's. A plain file rather than GSettings or dconf, because `wxrandr` has to be
able to read it with no GLib bindings and no schema installed, because you should
be able to see what you agreed to, and because withdrawing it has to be possible
with `rm` from a text console when the session will not start.

**What a later run then says** — one line, and it is the last one:

```console
$ wxrandr --unsafe-gnome-overlap --output Virtual-2 --pos 960x0
xrandr: --unsafe-gnome-overlap: applying a layout GNOME refuses ("logical monitors not adjacent (an overlap counts, and so does a gap)"), as agreed on 2026-09-06
```

The validator's positive control and the `monitors.xml: unchanged` line go with
the paragraph: they are reassurance, and reassurance is what somebody who has
agreed does not need every time. What survives is the line that is *not*
reassurance — a saved configuration file that moved when it cannot have is still
shouted about. `--dryrun` is never quiet either: it exists to be read.

**On a different build it asks again.** The Shell version is compared before
anything is read out of `gnome-shell`, because that comparison decides whether
the paragraph is printed *before* the write; libmutter's generation, the struct
size and the build id are audited against the answer that comes back, and if any
of them disagrees the record is deleted on the spot and the next run asks in
full. The generation and the size can only differ on a build that kept its
version string and moved its private layout, which the extension's own `typelib`
guard turns into a refusal rather than a bad write. The build id differs on every
ordinary `apt upgrade` of GNOME, which is the point of it. Either way the audit
is after the reply and therefore after that apply: an update is noticed on the
first overlapping run following it, not before.

**One note that is not an audit.** `apt upgrade` replaces libmutter under a live
session: the running `gnome-shell` keeps the library it started with mapped —
the mapping's line in `/proc/self/maps` gains ` (deleted)` — while the new file
sits on disk under the same name. Measured on both releases: the session
survives it, the layout stays, and every check goes on passing, because they all
run against the library in memory, which is the one being written to. It is
still worth saying, so it is said once, as a note rather than a verdict:

```console
xrandr: note: libmutter has been replaced on disk since this session started
(/usr/lib/x86_64-linux-gnu/libmutter-14.so.0.0.0 is no longer the file this
gnome-shell has mapped).  The checks ran against the library this session is
running, which is the one being written to, so this changes nothing now -- but
the next login runs the new one, and this feature may refuse there
```

While that is true the build id is reported as unknown rather than read from the
file at that path, because that file is a different library from the one the
answer is about.

**Withdrawing** is `wxrandr --gnome-overlap-forget`, which opens no session and
no socket at all: the moment somebody most needs it is from a text console with
a session that will not start.

**Asking** is `wxrandr --gnome-overlap-status`, whose first line is a token for
scripts and for `warandr` — `agreed`, `available` or `unavailable` — followed by
what is behind it. It reads the Shell's public version property and a bus name
and *nothing* out of `gnome-shell`: a GUI runs it at startup, and answering
"would this work?" must not cost a walk of Mutter's heap. It also answers where
there is no session to ask at all — `unavailable`, the reason, and the record
read back — for the same reason `--gnome-overlap-forget` does.

```console
$ wxrandr --gnome-overlap-status
agreed
shell: 50.1
extension: running
agreed on: 2026-09-06T09:12:44Z
agreed for: GNOME Shell 50.1 (libmutter-18 build 25d36850030c, MetaMonitorsConfig 80 bytes)
agreed by: wxrandr --gnome-overlap-allow
file: /home/test/.config/w11/overlap-consent.json
```

None of the three turns the flag on. `--unsafe-gnome-overlap` is still typed on
every invocation that uses it, and an overlapping layout without it is still
GNOME's refusal, agreement or no agreement.

#### Forcing past a refusal, on a GNOME nobody has measured

`--unsafe-gnome-overlap-unmeasured <major>`, and it is the only thing in this
repository that gets past a refusal. It exists because "we have not measured
your GNOME" is a statement about *us*, and somebody who knows their machine is
entitled to disagree with it — at their own risk, having read what that risk is.

```console
$ wxrandr --unsafe-gnome-overlap --unsafe-gnome-overlap-unmeasured 52 \
      --output Virtual-2 --pos 960x0
```

**The name, and the argument.** It is a second flag, never a value of the first:
`--unsafe-gnome-overlap` stays the only option that turns any of this on, and
typing the unmeasured one alone is a usage error, so there is no command line in
which forgetting the dangerous flag and typing this one does anything. It begins
`--unsafe-`, which nothing else in this CLI does, and it says `unmeasured`, which
is the word the refusal it answers uses — so it reads as what it is: an admission
about the machine, not a demand. And its argument is the GNOME Shell major that
is *running here*, compared with the real one out in `wxrandr` and again inside
`gnome-shell`. That is the part a bare `--force` cannot have: a line copied out
of a forum names the GNOME the poster had, and on anybody else's machine it is
refused by number before a byte is read.

```console
xrandr: --unsafe-gnome-overlap-unmeasured 49 names GNOME Shell 49; this session is
GNOME Shell 52.0.  It has to name the GNOME in front of you, so that a command line
copied from somewhere else is refused here rather than run
```

**It is not a default and it is never remembered.** No environment variable sets
it, nothing in a state file sets it, and it cannot be recorded: a forced run
neither reads nor writes the agreement, so the paragraph is printed every single
time, and `--gnome-overlap-allow` is refused while it is in use — an agreement
names a build every check passed on, and forcing is what is done when they have
not. **`warandr` has no way to reach it, on purpose**: what makes forcing
defensible is that somebody typed the version of the GNOME in front of them out
of the refusal they had just read, and a checkbox is a click. The window keeps
reporting GNOME's refusal on an unmeasured build, exactly as it did before any of
this existed, and the way to overrule that is a terminal.

**What it skips: one check.** That this GNOME is in the table — and with it the
tie between the shell and the libmutter it is supposed to carry, because on a
build nobody has measured there is no supposed to. The description to write
through is then picked by the size the running build's own GType registry reports
for `MetaMonitorsConfig`. That is a *selection*, not a relaxation: the size still
has to be exactly a shipped description's, and a size nothing here describes is a
refusal saying so — forcing cannot invent a description. Two shipped descriptions
*can* be the same size: GNOME 50 and GNOME 51 are both 80 bytes with the same
three tail slots, and where descriptions of one size agree on their shape like
that the newest is picked and the message names the others (`the size W11Overlap51
describes … and the same bytes as W11Overlap18`). Two that disagreed on their
shape would be a refusal rather than a coin toss, because then the size cannot
say which one this build is.

**What it cannot skip is everything else**, and that is a rule rather than a list
of exceptions. A refusal here is either *cautious* — this is a build nobody has
measured — or *certain* — something is missing, or has just proved itself wrong.
Only the cautious one is forceable, and there is exactly one of them:

| refusal | kind | with the flag typed |
|---|---|---|
| the Shell major is not in the table | **cautious** | **forced**: the description is picked by struct size, and every other guard runs |
| `--persistent` | certain | still refused: that file is read back through the validator this exists to get past |
| the session is not GNOME | certain | still refused: KDE, wlroots and X place the layout as drawn |
| the shell will not say its version | certain | still refused: forcing is vouching for a build by naming it, and a version nothing can read is not a build anybody can name |
| the extension is not on the bus | certain | still refused: there is nothing there to talk to |
| the invocation changes a mode, scale, rotation, primary or mirroring | certain | still refused: the extension writes two words and can do none of that |
| `symbols` — libmutter no longer exports something declared | certain | still refused: the code cannot run |
| `shared-library` — the description names a library that is not mapped | certain | still refused, and this one is why forcing is survivable at all: see below |
| `struct-size` — no shipped description is this size | certain | still refused: forcing picks a description, it does not write one |
| `sentinel`, `bounded-read`, `layout-mode`, `public-view` | certain | still refused: the read has just disagreed with Mutter, which is the guard working |
| `read-back`, `positive-control` | certain | still refused, and the old bytes are put back |
| `pending-dialog` | certain | still refused, and this one must never be forceable: it is what keeps an overlap out of `~/.config/monitors.xml` for ever |

The extension decides which of its own refusals is forceable and says so in its
answer; `wxrandr` reads that flag rather than parsing the wording.

**What it prints before it does anything**, always, in full:

```console
xrandr: --unsafe-gnome-overlap-unmeasured 52: forcing past the one check that says this GNOME has
  been measured.  This session may end.
  What is skipped:      one thing: that GNOME Shell 52.0 is a build this project has
                        measured, and with it that the libmutter mapped into gnome-shell is
                        the one this GNOME is supposed to carry ...
                        MetaMonitorsConfig is 80 bytes here, which is the size
                        W11Overlap51 describes (measured on GNOME 51, and the
                        same bytes as W11Overlap18)
  What is not skipped:  everything else, and none of it can be forced: exactly one libmutter
                        mapped, the Meta typelib agreeing with it, the struct size equal to
                        the description's, every symbol callable, the sentinel through
                        Mutter's own setter, the modal-grab check, the bounded read, the
                        layout mode, the field-by-field comparison against Mutter's public
                        view, the read-back of the two words, Mutter's own validator as a
                        positive control, and the digest of ~/.config/monitors.xml.
  What may happen:      ... If it is wrong, gnome-shell crashes, and on
                        Wayland gnome-shell is the session: every program running in it goes
                        with it, unsaved work included.
  What is recorded:     nothing.  A forced run neither reads nor writes the agreement, so
                        this is printed every single time, and --gnome-overlap-allow
                        refuses while this flag is in use.
  If it happens:        ...Ctrl+Alt+F3 ... gnome-extensions disable ...
```

and then the ordinary warning underneath it — what moves, what it risks, what it
saves, the undo line — because forcing adds a paragraph and replaces nothing. If
it works, the apply says which description it used and that nothing was recorded.

**`--dryrun` cannot rehearse a forced run, and the two together are refused.**
Writing nothing is all a dry run ever promised, and it is not the thing at risk
here: the checks a forced run makes happen *inside* `gnome-shell` and read
through a description nobody has proved on this build, so they can end the
session before anything of ours decides whether to write. Measured, on the first
forced run ever attempted on a real GNOME 51: `gnome-shell` aborted during the
checks, on a `--dryrun`, and the session went with it. The cause was a
description that named `libmutter-18.so.0` — see the next paragraph — and it is
fixed, but the shape of the risk is not: on an unmeasured build the checks
themselves are the dangerous part, because they are the first thing to read
private memory. Somebody who types a dry run is being careful, so what they get
is one line rather than a rehearsal that is not one:

```console
$ wxrandr --dryrun --unsafe-gnome-overlap --unsafe-gnome-overlap-unmeasured 52 \
      --output Virtual-2 --pos 960x0
xrandr: --unsafe-gnome-overlap-unmeasured cannot be rehearsed with --dryrun: reaching an
unmeasured build means loading a description built for another one, which can end the
session before anything is decided, dry run or not. Run it for real, on a machine you can
afford to lose the session on, or add the build first (the refusal without
--unsafe-gnome-overlap-unmeasured says how)
```

`--dryrun` on a **measured** build is untouched, and is
[below](#--dryrun-and-unrecognised-builds): every guard runs, nothing is
written.

**A description names no shared library, since 0.4 and because of that crash.**
A `shared-library` in a `.gir` makes GIRepository `dlopen` exactly that file, and
a forced run picks its description by struct size on a machine whose `libmutter`
is by definition not the one that description was measured against. The `dlopen`
fails, and gjs does not raise: it asserts and aborts the process, which on
Wayland is the session. The shipped descriptions therefore name nothing and the
symbols resolve out of `gnome-shell`'s own global scope, where `libmutter`
already is; a description that *does* name one — an older install's, or a hand
built one — is refused by name (`shared-library`) before anything is called
through it. Which library is mapped is still proved, from `/proc/self/maps`,
which is where a file name belongs.

**On an unmeasured GNOME the extension has to be able to load at all**, and it
could not: `gnome-shell` refuses to load an extension whose `metadata.json` does
not name the running Shell major, and that list is generated from the table. So
on exactly the builds this flag exists for, the extension sat installed, enabled
and `OUT_OF_DATE`, the bus name was never taken, and the flag met *the overlap
extension is not running*, which is certain and not forceable.
`gnome/install-overlap.sh` now names the running major in the **installed** copy
of `metadata.json` when that major is not in the table, and says so. That makes
the extension load; it does not make it act. Its own `shell-version` check still
refuses every call on a build the table does not name unless the call carries
this flag.

**And when it is not what you want**, which is most of the time: the refusal it
answers is also the message that says how to make this GNOME a measured one, and
that is the better ending. See below.

#### Every guard, and what it catches

The tool's three refusals above are the cheap half. The rest run inside the
extension, **on every call and never once at install** — a distribution upgrade
can replace libmutter under a running session — and every one of them has been
made to fire on purpose, across the three releases, by installing a deliberately
wrong type description over the shipped one:

| guard | what it catches | what it did when made to fire |
|---|---|---|
| `shell-version` | a build nobody has measured: the shell major is one the table names (46, 50 and 51 today), exactly one libmutter mapped and of the matching generation, the `Meta` typelib version agreeing | with the table edited to claim libmutter 19 for GNOME 50: `refused (libmutter): GNOME Shell 50.1 should carry libmutter-19, this process has [18]` |
| `typelib` (refusing as `struct-size`) | the structure is not the shape we describe: our own record's size, read back from *our own* typelib through `GIRepository` so there is no constant to go stale, against `GObject.type_query(MetaMonitorsConfig).instance_size` | `this build's MetaMonitorsConfig is 72 bytes, the description shipped for libmutter-14 is 80` — **having read nothing at all** |
| `sentinel` | the tail has moved even though the size has not: a value written through Mutter's own exported `set_switch_config` must reappear at the offset we believe, on a throwaway `create_linear()` object and never on the live one | pins the two offsets that actually differ between mutter 14 and 18 |
| `pending-dialog` | the one window in which a mutated configuration could reach Mutter's *writer*: `Main.modalCount` must be exactly 0, and anything else, including a count that will not read as a whole number, is a refusal | with *Keep these display settings?* on screen, on both releases: `refused (pending-dialog): something holds a modal grab on the shell (Main.modalCount is 1) …` and the confirmed dialog then saved the layout **GNOME** had applied. It is the one guard measured refusing when it should not have: see below |
| `bounded-read` | anything unreadable: the whole configuration is copied out with `g_memdup2`/`g_strndup`, every address range-checked against `/proc/self/maps` first, list walks capped | with `key` and `logical_monitor_configs` swapped, which are the same size: `node[1]: 0x1+24 is not in a readable mapping` — the wild pointer that killed a shell back when pointers were declared as pointers |
| `public-view` | everything else: count, `x`, `y`, `w`, `h`, scale, primary and connector names against `global.display` and `MetaMonitorManager` | with the list offset shifted by 8: `private read has 0 monitors, Mutter reports 3` |

Two more run after the write and before the apply: the configuration is re-read
the same bounded way and must differ in **exactly** the two requested words,
and `meta_verify_monitors_config()` must refuse the result. A refusal at any
point puts the old bytes back before it returns, because
`meta_monitors_config_copy` is exported on mutter 18 and not on mutter 14, so
there is no copy to work on and the live object is mutated in place.
`layout_mode` is cross-checked against the value DisplayConfig reports
publicly, which pins the same tail with a public number: on GNOME 46, whose
default is physical, claiming logical gets `layout_mode reads 2 … DisplayConfig
says 1`.

**In every one of those deliberate breakages, `gnome-shell` survived.** Twelve
of them now across the three releases — five while this was being built, four more
during the update testing below, and three with GNOME 51 (a 72-byte description
under the GNOME 51 name, `key` and `logical_monitor_configs` exchanged, and a
description naming a `libmutter` that is not mapped, which is the `shared-library`
guard that measuring GNOME 51 produced) — each refused by name, no crash, no core
dump, and the desktop still running afterwards. The one input that ever did take
a session down is the last of those, and it did it *before* that guard existed:
see `--dryrun` above.

#### The one false refusal, and why the guard was not loosened

`pending-dialog` has been measured refusing with nothing on screen. Once, on
24.04: the first overlapping run after a post-update login came back

```console
xrandr: --unsafe-gnome-overlap: the overlap extension refused (pending-dialog):
something holds a modal grab on the shell …
```

with no dialog, no menu and no overview anywhere (the screenshot was checked),
and the very next run seconds later applied normally. It did not reproduce: a
dozen probes across two ordinary reboots were all clean. Something in a session
that has just come up holds a grab for a moment, and `Main.modalCount` cannot
tell that from the dialog that matters.

**The guard was left exactly as strict**, because the two mistakes do not cost
the same: a false refusal costs one retry, and a false pass costs
`~/.config/monitors.xml` — the whole file, at every boot, for ever (below). What
was wrong was the *message*, which told the user to answer a dialog that was not
there. It now names the count it read and says that a grab nobody can see goes
away by itself:

```console
… something holds a modal grab on the shell (Main.modalCount is 1).  If that is
GNOME asking whether to keep a display change, confirming it while this had
moved a monitor is the one way an overlapping layout could reach
~/.config/monitors.xml and stay there.  Nothing was read and nothing was
written.  Answer it -- or close the overview, or the menu -- and run this again.
If there is nothing on screen to answer, this is a grab that has not been
released yet, which was measured in the first seconds of a fresh session: wait a
moment and run the same command again
```

#### What ordinary updates actually do

Eight version pairs, on default desktops built by the Ubuntu installer, three
virtio heads, the extension installed once and never touched again. Every
release+updates pair either release can deliver today, plus the one nobody has
received yet:

| release | from → to | what moved | result |
|---|---|---|---|
| 24.04 | `~24.04.14` / 46.2-…16 (newest in `-updates`) | nothing was pending | applied, 6/6 |
| 24.04 | `~24.04.13` / 46.2-…14 (**what the ISO ships**) → `~24.04.14` / …16 | `apt upgrade` + reboot | applied both sides; one transient `pending-dialog` refusal after the post-update login, then clean |
| 24.04 | `~24.04.14` / **46.0-1ubuntu9** (the GA library under today's shell) → 46.2-…16 | libmutter only, **invisible to `ShellVersion`** | applied both sides, 6/6 |
| 24.04 | `~24.04.9` / 46.2-…10 (mid-life) → newest, **upgraded with the overlap on screen** | shell + libmutter under a live session | session survived, overlap held, nothing persisted |
| 26.04 | 50.1-0ubuntu1.2 / 2.2 (the ISO's own) | nothing was pending | applied, 6/6 |
| 26.04 | 1.2 / 2.2 → **1.3 / 2.3 from `-proposed`** — the next update | shell + libmutter, version string unchanged | applied, 6/6, still 80 bytes |
| 26.04 | **GA** 1 / 2 → 1.2 / 2.2, **upgraded with the overlap applied** | shell + libmutter under a live session | session never blinked, overlap held |
| 26.04 | shell 1.2 over **GA libmutter** | the mix `ShellVersion` cannot see | applied, 6/6 |

**Nothing moved the structure.** Seven distinct libmutter builds — four on
24.04, three on 26.04 — and on every one: `GObject.type_query(MetaMonitorsConfig).instance_size` was 72 (46) /
80 (50) as declared, the sentinel round-tripped at the declared offset, and the
bounded read agreed with Mutter's public view field for field. Independently, at
source level, `src/backends/meta-monitor-config-manager.h` is byte-identical
between the mutter 46.0 and 46.2 tarballs, and
`gnome/overlap-typelib/gen-gir.py --from-header` lays the mutter 46 and mutter 50
headers out and arrives at exactly those two of the three shipped descriptions
(`tests/fixtures/mutter/` carries those two headers; GNOME 51's was derived the
same way on the machine it was measured on).

**It cannot break by a generation change inside a release.** One
`libmutter-N-0` soname per Ubuntu release, for the life of the release — bionic
2, focal 6, jammy 10, noble 14, plucky 16, questing 17, resolute 18, stonking 51
(mutter 51 renumbered the library to the GNOME major, which is why that last one
is not 19) — and `-backports` has never carried mutter or gnome-shell at all. The
generation moves at a release upgrade and nowhere else.

**In every measured update, the shared region stayed byte-identical**: head 0's
right 960 px and head 1's left 960 px with the same SHA-256 as raw RGB, `AE` and
`RMSE` 0, against a control of ~1.007 M differing pixels for one head's own two
halves.

**What a user should expect.** After an ordinary update: nothing — the same
command keeps working, and the only new thing is one line saying the agreement
has been withdrawn because the library it named is gone. After a release
upgrade: a refusal, by name, until this project measures the new GNOME. That is
the design, not a failure of it.

#### The risk that is left

Not softened, because a reader has to be able to decide against this:

* **A wrong write is not a wrong answer, it is a dead compositor.** The guards
  turn nearly every wrong description into a refusal, and the ones tried were
  all refused, but *nearly* is the honest word. Twelve deliberate breakages
  caught is not a proof that a thirteenth would be.
* **The residual case the design cannot close by construction** is two fields
  of the same size swapped by an upstream change. The size gate passes and the
  sentinel may pass, and what is left is the bounded reader refusing an address
  that is not mapped, or the public-view comparison noticing that the numbers
  are nonsense. Both were measured catching exactly that, on two different
  swaps, and both are checks rather than certainties.
* **If `gnome-shell` dies, everything in the session dies with it.** Not the
  layout: the browser, the editor, the unsaved buffer, the terminal you typed
  this in. On Wayland the compositor is the session, and there is no restarting
  it in place.
* **The allowlist is a claim about three builds this project measured**, stock
  Ubuntu 24.04, 26.04 and 26.10. A distribution that backports a Mutter change without
  moving the shell's major version can make the version gate say yes to a
  library it has never seen. What stands behind it then is the structure size,
  the sentinel and the public-view comparison, in that order. **This is the live
  one, and it is not hypothetical**: the version gate said yes to seven
  different libmutter builds during the update testing above, because that is
  what it is for, and every one of them happened to have the same private
  layout. The mechanism that would beat it exists today — mutter
  50.1-0ubuntu2.3, in `resolute-proposed`, adds a field to a private struct
  (`_MetaMonitorManagerPrivate`, in an Ubuntu patch for auto-rotate on phones)
  under an unchanged 50.1 shell version. It happens not to be
  `_MetaMonitorsConfig`. Nothing says the next one will not be, and nothing in
  24.04's 28 months has been.
* **The measurement has a shelf life.** Everything above was true of the archive
  on the day it was run. A guard that has been right eight times is a guard that
  has been right eight times.
* **Any modal grab refuses**, not only the dialog that matters, because the
  dialog cannot be named from an extension on either release (see
  [Technical.md § 6](Technical.md#why-mutter-refuses-monitors-that-share-area)
  for what that cost the first cut of this check). If the overview or a menu is
  open, this refuses and says so, and the answer is to close it — and it has
  been measured refusing with nothing open at all, once, seconds into a fresh
  session, which is the section above.

#### It cannot write `~/.config/monitors.xml`

That matters more here than anywhere else in this tree, because the file is
read back through the same validator, and one entry it refuses discards the
whole file at every boot, for ever. Four things hold it:

1. `--persistent` and this flag refuse each other, in either argument order;
2. the extension's type description does not name
   `meta_monitor_config_manager_save_current` or any other writer, so the
   symbol is not callable from it at all — asserted against both the `.gir`
   sources and the shipped `.typelib` bytes;
3. the apply method is the constant `METHOD_TEMPORARY`, and `METHOD_PERSISTENT`
   appears nowhere in the extension under any spelling;
4. the file's SHA-256 is taken before and after every call and handed back, so
   the tool *reports* rather than assumes, and shouts if it ever differs.

There is one way an overlap could still have reached that file, and it is the
reason `pending-dialog` exists. Mutter saves whatever configuration is
**current** when a pending display change is confirmed, so a *Keep changes?*
dialog armed by something else — the Settings panel in another window — and
confirmed while this had moved a monitor would write the overlap to disk. That
was measured happening, on both releases, at a time when this check could not
fire. It is now measured refusing, on both releases, with the dialog on screen;
the confirmed dialog then saved the layout GNOME itself had applied, and the
next boot read that file back with no complaint in the journal.

Measured with a saved file already present: its digest is identical across an
overlapping apply, and a later `--persistent` of a *valid* layout, confirmed at
the dialog, writes that valid layout. The overlap was gone from memory by then,
and was never on disk.

#### Nothing persists, so nothing has to be undone

The layout is applied with method 1, exactly like an ordinary `wxrandr` run
without `--persistent`. It does not survive a logout, a reboot, or a
`gnome-shell` that has died and been logged into again — measured on both
releases: back to the row, `monitors.xml` untouched, not a line in the journal.
The undo command is printed before the change and goes through DisplayConfig,
so it works whether or not the extension is still loaded.

One surprise worth knowing: an overlap **does** survive a monitor unplug and
replug inside the same session, because Mutter restores the layout from its own
in-memory store rather than validating it again. It still goes at logout.

#### If a session will not start

It should not come to this. The extension does nothing at login: `enable()`
exports one D-Bus object and stops, so an enabled extension that is never
called cannot hurt anything, which was measured over about ten logins on GNOME 50
and five on GNOME 46: the only journal line either of them ever wrote was
`w11-overlap: enabled (idle; it acts only when called)`. The route back
is printed in the warning anyway:

1. **Log in again.** A `gnome-shell` that dies drops you at the login screen,
   and nothing was saved, so what comes back is the layout you started with.
   Measured aside, twice: after a `gnome-shell` killed outright, GNOME came back
   with `org.gnome.shell disable-user-extensions` set to `true` on its own, so
   the next session had this extension, and every other, inert.
2. **If no session will start**, Ctrl+Alt+F3 to a text console, log in, and
   `gnome-extensions disable w11-overlap@w11`, then Ctrl+Alt+F1
   back. This works from a real text login, which has `XDG_RUNTIME_DIR` set.
   **It does not always work from a bare shell with no session bus at all**: it
   prints `dconf-WARNING … failed to commit` and exits **0** having changed
   nothing, which is a `gnome-extensions` behaviour and not something this
   project can fix.
3. **The route that always works** is deleting the directory:
   `rm -rf ~/.local/share/gnome-shell/extensions/w11-overlap@w11`.
   It is printed in the warning for that reason.

#### `--dryrun`, and unrecognised builds

`--dryrun --unsafe-gnome-overlap` prints the same warning and runs every guard
inside the extension against the running libmutter, writing nothing. (On a build
the table does not name it refuses, as it always did, and adding the forcing flag
to a dry run is refused too: [above](#forcing-past-a-refusal-on-a-gnome-nobody-has-measured).)

```console
xrandr: overlap check shell-version: GNOME Shell 46.0, libmutter-14.so.0 (build 9e23feb34618)
xrandr: overlap check typelib: W11Overlap14, MetaMonitorsConfig 72 bytes as declared
xrandr: overlap check sentinel: switch_config round-tripped at the declared offset
xrandr: overlap check pending-dialog: nothing holds a modal grab, so GNOME is not asking "Keep changes?"
xrandr: overlap check bounded-read: 3 logical monitors, every address range-checked
xrandr: overlap check public-view: identical to Mutter's public view (global.display + get_monitor_for_connector on the requested names)
xrandr: dryrun: nothing was written
```

`sh gnome/install-overlap.sh --check` runs the same probe from the installer,
and is the honest way to ask whether your GNOME is one of the three this has been
measured on.

On anything else it refuses without reading anything private — 47, 48, 49, 52,
a shell that will not name its version — and the refusal is written for whoever
is going to add that release:

```console
xrandr: --unsafe-gnome-overlap: GNOME Shell 52.0 is not a build this has been measured on.
  Nothing was read out of gnome-shell and nothing was written.

  What is running:      GNOME Shell 52.0
                        libmutter-51.so.0
                        Meta typelib 51
                        MetaMonitorsConfig 80 bytes, from this build's GType registry
  What is shipped:      GNOME 46 -> libmutter-14.so.0, W11Overlap14, Meta typelib 14, MetaMonitorsConfig 72 bytes
                        GNOME 50 -> libmutter-18.so.0, W11Overlap18, Meta typelib 18, MetaMonitorsConfig 80 bytes
                        GNOME 51 -> libmutter-51.so.0, W11Overlap51, Meta typelib 51, MetaMonitorsConfig 80 bytes
  To add this build:    one record in each of these two, keyed by the GNOME major, and
                        nothing else anywhere:
                            gnome/w11-overlap@w11/generations.json
                            wxrandr/gnome_overlap.py  (GENERATIONS)
                        then
                            python3 gnome/overlap-typelib/gen-gir.py --from-header \
                                <mutter source>/src/backends/meta-monitor-config-manager.h
                            python3 gnome/overlap-typelib/gen-gir.py
                        which derives the offsets from that release's own header and then
                        writes the description, its typelib and the extension's
                        metadata.json out of the table.  The procedure, and the three-head
                        measurement that has to follow it before anything ships, is
                        docs/Technical.md section 6, "Adding a GNOME generation".
  To try it here now:   wxrandr --unsafe-gnome-overlap --unsafe-gnome-overlap-unmeasured 52 <the rest of the line>
                        which skips this one check and no other, records nothing, and
                        may end this session.  It prints what it is doing first; read
                        that before you type it.
```

The versions found, the size this build reports, what was expected, where the
answer goes and what to run: enough to add a generation without having seen this
code. Nothing private is read to produce any of it — a version string, the file
names in `/proc/self/maps`, `GIRepository`, and `GObject.type_query()` on a type
whose name is in a public header. The extension's `metadata.json` says
`["46", "50"]` as well, so `gnome-shell` will not load it elsewhere, and the
extension refuses on its own account if it somehow runs there.

#### What to do when it refuses

The refusal names the check, and the check names the cause. In order, and none
of it needs a debugger:

1. **Run it again.** `pending-dialog` is the one guard that can refuse a session
   that is fine, and the answer to it is a retry (above).
2. **`sh gnome/install-overlap.sh --check`.** Every guard against the running
   libmutter, writing nothing.
3. **`shell-version`** — this GNOME is not one of the ones that were measured.
   The refusal prints what to add and where (above). That is what a release
   upgrade produces and it is the intended outcome: the feature is off here until
   somebody measures this release. If you know this machine and want it anyway,
   [`--unsafe-gnome-overlap-unmeasured <major>`](#forcing-past-a-refusal-on-a-gnome-nobody-has-measured)
   skips this check and no other.
4. **`typelib` or `sentinel`** — same generation, different structure. This is
   the case regeneration exists for, and the route is the release's own header
   rather than the running compositor:

   ```console
   $ python3 gnome/overlap-typelib/gen-gir.py --from-header \
         mutter-51/src/backends/meta-monitor-config-manager.h --shell 51
     0  24  GObject parent
     …
     instance size 80
     the two numbers a record needs:  "struct_size": 80, "tail_slots": 3
   ```

   It lays the struct out under the x86-64 rules, refuses on any type it does
   not know the size of rather than guessing, and refuses outright if the head
   of the struct has moved, because then the description's *shape* is wrong and
   not just its numbers. Adding a generation is one record in
   `gnome/w11-overlap@w11/generations.json`, the same record in
   `GENERATIONS` in `wxrandr/gnome_overlap.py`, one run of the script above —
   which writes the `.gir`, the `.typelib` and `metadata.json`'s `shell-version`
   out of the table — and then the three-head measurement.
   [Technical.md § 6](Technical.md#the-table-and-adding-a-gnome-generation) has the
   order and what to prove at each step.
5. **`public-view`** — the read is nonsense. Do not adjust offsets until the
   numbers agree; that is how a same-size field swap ships.

**Why the description is not generated from the running compositor**, which
would turn most of this into self-repair: because the description is what the
guards are checked *against*. A description derived by asking libmutter about
itself agrees with libmutter by construction — `struct-size` would pass because
it was told the size, and `sentinel` could only be satisfied by searching
offsets until the marker turned up, which is a search that confirms itself. The
independence of the two is exactly what caught a wrong-generation description
before a byte was read, on both releases. Deriving from the release's *source*
keeps that independence; deriving from the process you are about to write into
spends it.

#### What it deliberately does not have

No environment variable, so a layout script cannot acquire this by accident, and
no setting that turns it on: the recorded agreement makes the tool quieter and
never more capable. No `Restore` method on the extension, because the undo is a
plain validated `wxrandr` line that does not depend on the dangerous half. No
reconfiguration beyond positions. No enabling by the package: the `.deb` carries
the files since 0.4, and switching it on stays a `gnome-extensions enable` and a
re-login that a person types, with its own installer and its own enable step from a
clone. No support for GNOME 47 to 49, nor for 52 and whatever follows it — only a
way for somebody who knows their own machine to overrule that refusal, per
invocation, having read what it may cost, with nothing remembered afterwards.

`warandr` has no button for it either, and none for
`--unsafe-gnome-overlap-unmeasured` at all: forcing rests on somebody typing the
version of the GNOME in front of them out of the refusal they just read, and a
checkbox carries none of that. What it has is a dialog, the first time
somebody drags two monitors into an overlap and presses Apply on a GNOME where
the extension is installed — the explanation at the moment it is about something
the user has actually asked for, with a box that records the same agreement this
section describes. [WARANDR.md § Overlapping monitors on
GNOME](WARANDR.md#overlapping-monitors-on-gnome) is that half.

#### What it was measured doing

GNOME 50.1 and 46.0, three virtio heads, stock images built by the Ubuntu
installer. Monitors at 0, 960 and 2880; both heads' screendumps cropped to the
960-pixel shared region and dumped as raw RGB have the **same SHA-256**, and
ImageMagick's `AE` and `RMSE` between them are 0, against a control of 507,079
differing pixels for the same head's own left and right halves. A window moved
wholly inside the shared region is drawn byte-identically on both heads. The
pointer crosses the whole bounding box with no jump, no dead zone and no
duplication, and a click in the shared region focuses the window drawn there. A
1024×768 head placed at `+448+156` inside a 1920×1080 one is byte-identical to
the sub-rectangle of its neighbour, which is a thing mirroring cannot express
at all.

**What to reach for instead: a mirrored region, which is not an overlap and is
never called one here.** GNOME will not place two monitors so that they share
area; the closest thing available is a mirrored region, where the pixels match
exactly and that is the whole of the resemblance. The copy takes the clicks
that land on it rather than passing them through to the window they came from,
and where it is produced by capture rather than by the layout it lives only as
long as that capture session, which a screen lock ends. On GNOME the layout
itself still mirrors whole monitors (`--same-as`, identical mode, rotation and
scale); a *region* there has only the desktop portal, which prompts once per
session. On wlroots it is `wmirror`, below, with no prompt at all.

An overlapping layout is also nothing special to the KWin backend's undo
line: `restore_command` spells every position out absolutely, so the line
printed while the *previous* layout overlapped replays into that same
overlap (verified live on Plasma 6, and pinned in `tests/test_wxrandr_kwin.py`).

**Refusals are said in the compositor's name.** We pass every layout on
unchanged, so a "no" is never ours: Mutter's D-Bus error comes back as
`xrandr: GNOME's Mutter refused this layout: <Mutter's words>`, KWin's
`failure_reason` as `xrandr: KWin rejected the output configuration:
<KWin's words>`, and sway/wlroots as `xrandr: compositor rejected …`. On
Mutter `--dryrun` is the compositor's own method-0 verify, so the same
attributed one-liner is available on stderr without applying anything
(stdout stays xrandr's dryrun bytes).

**Region mirroring lives in its own command (`wmirror`), not here.**
Compositor-level *output* mirroring is in scope wherever the compositor has
it — Mutter's one logical monitor with several members, KWin's
`set_replication_source` — and `--same-as` uses it there, but only where the
simpler shared position cannot already deliver what was asked (below). Making
a *region* show the same pixels is a different and much larger thing: a
resident helper capturing one output and painting it onto another every
frame. wxrandr does not do it and is not going to. The route does exist and
is packaged, though, so since 0.2 the toolbox drives it from a **separate
command**: `wmirror` runs the existing
[wl-mirror](https://github.com/Ferdi265/wl-mirror) on wlroots and owns its
lifetime — `WMIRROR.md` has the measurements and the contract. Three measured
reasons it stays out of `wxrandr`: no `xrandr` syntax expresses "mirror this
rectangle onto that one", so it could not be spelled in a command line or in
a saved layout script that has to keep running on a plain X11 box; a shared
rectangle is the one geometry in which the helper would capture its own
window (a fullscreen window on the target is drawn on the source too --
measured directly, both heads went entirely black, every pixel), so
`--same-as` cannot host it; and it leaves a resident process that stops the
compositor ever idling, which is not what a layout tool should leave behind.
On GNOME and KDE the route left is the desktop portal's ScreenCast (AGENTS.md
route 4), which asks once per session and is not wired up here yet, which makes
it useless from the hotkey a layout script exists for.

## Hyprland backend (`wxrandr/hypr.py`)

`--backend hypr` (alias `hyprland`, `WXRANDR_BACKEND=hypr`) reads `hyprctl -j monitors
all` and applies with one `hyprctl keyword monitor
NAME,WxH@Hz,XxY,SCALE[,transform,N][,mirror,OTHER]` per touched output, over Hyprland's
own request socket. It is second in the auto order, after sway, so a Hyprland box never
touches the session bus to choose a backend.

**Why it exists rather than `wlr`.** Hyprland advertises `zwlr_output_manager_v1`
version 4 and **takes exactly one apply per session through it**. The second times out
after 10 s with nothing changed and no `[COutputConfiguration] Applying configuration`
in Hyprland's own log; with a second output present even the first one hangs;
and `wlr-randr`, the reference client, hangs for ever on the same request. Measured on
0.53.3 and again on 0.56.2, where the apply is dead from the first request of a fresh
session. On 0.56.2 `hyprctl keyword monitor` was also seen answering `ok` and changing
nothing — but only in a session five timed-out wlr applies had already been through, and
never on a fresh one, so that reading is not what this backend rests on and
`vm/live-smoke.d/hypr.sh` carries the Arch applies as `xwant` until `arch-hypr` settles
it. `keyword monitor` on a session that has not touched the protocol applies at once,
which is the route this backend takes. `--backend wlr` on a
Hyprland session is still allowed, and its timeout says so:

```
xrandr: timed out waiting for the compositor to apply the output configuration (Hyprland
answers only the first output-configuration apply of a session; use --backend hypr)
```

Three live measurements on `resolute-hypr` (0.53.3, three heads, 2026-09-09) sharpen
that. On a virgin session both wlr applies time out (10.16 s, 10.26 s) with the head
unchanged. A timed-out wlr apply does **not** stop the session taking `keyword monitor`:
the two applies right after it landed in 0.28 s and 0.36 s. And after a `keyword monitor`
apply the same wlr request stops timing out and answers rc 0 in 0.64 s **having changed
nothing** — no stderr, the head still where it was. That last one is a defect of ours
with no rung of the ladder under it: this backend re-reads what it applied and the wlr
backend does not. **Not yet**; the fix is one re-read in our own code.

**Every apply is verified by re-reading `j/monitors all`**: enabled or disabled,
position, mode size and transform — and deliberately not the scale, because asked 1.37
Hyprland applied 1.33 and answered `ok`. A compositor that answers `ok` and changes
nothing gets one line and rc 1 rather than a reported layout that is not on the screen:
`Hyprland accepted the mode 1920x1080 for Virtual-2 and did not apply it (it reports
1280x1024)`.

`j/monitors all` and not `j/monitors` is load-bearing: a head Hyprland has disabled is
not in the plain answer at all — the row VANISHES rather than gaining `disabled: true`.
Measured live with Virtual-3 off, `j/monitors` answers Virtual-1 and Virtual-2 while
`j/monitors all` answers all three with `disabled: true` on the third and the same 26
`availableModes` an enabled row has. So `--output NAME --off`, and `--auto`/`--right-of`/
`--below` after it, work; before it, `--off` refused with "Hyprland accepted the
configuration for Virtual-3 and then stopped listing it" and the three commands after it
warned `output Virtual-3 not found; ignoring` about an output that was there the whole
time. `wxrandr --query` prints `Virtual-3 connected (normal left inverted right x axis y
axis)` for a head that is off, which is xrandr's own shape.

`--same-as` works, and the value Hyprland reports back is the mirrored monitor's numeric
`id` **as a string**, never its name (`mirrorOf: "0"` after `,mirror,Virtual-1`). Both
spellings are accepted on the way in, and the id is translated back to a name before it
is remembered, because an id is a position in Hyprland's list that a hotplug moves.

Two things are **not yet** here. `--persistent` is accepted and says once that it does
nothing: Hyprland's layout lives in `hyprland.conf`, and the route is a `monitor=` line
in a snippet that file sources (route 2), at the cost of owning a file the user
hand-edits; this layout lasts as long as the session. `--dryrun` prints its plan
unchecked, because `keyword` **is** the apply and there is nothing to validate against;
the route is a validating call in Hyprland's IPC (route 6).

`--brightness`/`--gamma` are unaffected by any of this — they go over
`zwlr_gamma_control_manager_v1`, which Hyprland advertises at v1, and not through the
backend.

**`wxrandr/hypr.py` carries a second copy of `wdotool/hypr_ipc.py`'s reader on purpose.**
`scripts/build-pyz.sh` builds `dist/wxrandr` out of `w11common` and `wxrandr` alone, so a
`from wdotool...` there would work from the .deb and quietly not from the zipapp — and on
Hyprland "quietly" means falling back to a wlr path that cannot apply. This is the same
trade `wdotool/layoutbox.py` already makes with Mutter's logical-size rule, and it is
pinned the same way: `tests/test_wxrandr_hypr.py:TheTwoClients` drives both clients
against one double and insists on the same bytes and the same sentences.

## Mutter backend (`wxrandr/mutter.py`)

Mutter has no `zwlr_output_management`; its display API is the D-Bus object
`/org/gnome/Mutter/DisplayConfig` (`GetCurrentState` + `ApplyMonitorsConfig`), which any
client on the user bus may call. The bus is found like the compositor socket
(`session.find_session_bus`: `$DBUS_SESSION_BUS_ADDRESS` when it belongs to the Wayland
session's user, else the `bus` in the runtime dir owning the Wayland socket), so
wxrandr works from a GNOME custom keyboard shortcut, from `sudo`, and from
`ssh root@host` with an empty environment (root's `/run/user/0` bus is skipped; when
dbus-daemon turns root away, `dbus_mini` reconnects as the socket's owner).

What maps:

| xrandr | Mutter |
|---|---|
| output name, make/model/serial, mm size | monitor connector, spec; mm from `width-mm`/`height-mm` when Mutter sends them, else from `wl_output.geometry` (Mutter 46/50 never emit the D-Bus keys — verified with gdbus — but give the EDID size to `wl_output`, which is what XWayland's RandR prints: 480mm x 270mm on a 1920x1080 QEMU head, byte-identical here) |
| mode table (`*` current, `+` preferred, `WxHi` interlaced) | the monitor's mode list; opaque ids kept verbatim (`Mode.mode_id`) |
| enabled, `WxH+X+Y`, rotation/reflection, scale | membership of a logical monitor; its x/y, transform, scale. Mutter numbers transforms like `wl_output` with the spec's counter-clockwise 90 — through Mutter's XWayland real xrandr prints 1 as `left`, 3 as `right`, 5 as `left X axis`, 7 as `right X axis` (all eight measured on GNOME 50), whereas sway's verified table has "90" == `right`; `mutter.MUTTER_RANDR_VIEW` holds the measured words and the 1↔3 / 5↔7 permutation follows from it |
| `--mode/--rate/--auto/--preferred` | mode id chosen by size + nearest rate |
| `--scale S` | snapped to the nearest of the mode's `supported_scales` (warning when it changed); logical size = `roundf(px / scale)` in layout-mode 1, raw pixels in layout-mode 2 (GNOME 46 without "Fractional Scaling": integer scales only) — the dryrun plan uses the same math |
| `--pos`, `--left-of/--right-of/--above/--below` | positions in Mutter's logical space, resolved against pending sizes like everywhere else |
| `--same-as` | one logical monitor with several members (Mutter requires the same mode, rotation and scale: otherwise `xrandr: cannot mirror B onto A: ...`). Mutter flags the primary *logical monitor*, not a connector, so a mirror group holds several and names none: wxrandr keeps whichever member the user made primary and falls back to the group's first only when the choice is not in it (taking the first outright moved the primary onto whichever output the group happened to be built around) |
| `--primary` | the real primary flag (exactly one; what Mutter reports overrides the state file). GNOME 50 keeps a stale `primary=true` on the previous logical monitor after a temporary re-primary (until that monitor is rebuilt), so when several are flagged the legacy `GetResources` output property `primary` — which tracks the real one, as XWayland shows — breaks the tie |
| `--off` | the connector is left out of the configuration. A disabled output cannot stay primary on Mutter (X keeps the flag and prints `connected primary` for it): the primary moves to the first enabled output and the query shows it there |
| `--listmonitors` | one RandR monitor per active output, the primary listed first (the X server orders monitors that way; verified against real xrandr on GNOME 50) |
| several `--output` stanzas | one `ApplyMonitorsConfig` call — atomic; after it, wxrandr waits for `MonitorsChanged` (≤ 5 s) and re-reads |
| `--newmode/--addmode/--rmmode/--delmode` | state file as on sway/wlr; *applying* a custom mode works only when a real mode with that size and rate exists, else `cannot find mode NAME` |

What warns and succeeds: `--brightness`/`--gamma` (Mutter exposes no gamma LUT),
`--noprimary` (GNOME requires a primary; the current one is kept), an output enabled
without a position (`--auto` on a disabled output; xrandr would put it at 0,0, which
Mutter rejects as an overlap, so it is placed right of the rightmost output), a scale
Mutter cannot do (snapped), `--filter`/`--set`/`--transform`/`--panning` as elsewhere.

Holes: X tolerates a gap, Mutter does not (every logical monitor must share an edge
with another). So an output that touched a neighbour's right or bottom edge keeps
touching it when that edge moves because the neighbour changed mode, rotation or
scale: `--output A --rotate left`, `--output A --mode SMALLER`, `--scale`, `-s`, `-o`
in the middle of a row shift the outputs right of (below) it along, chains included,
one warning each — `xrandr: output C moved to +3000+0 to stay adjacent to A`. Explicit
positions are the user's: an output given `--pos`/`--right-of`/... in the same call
never moves and nothing follows it (its old neighbours may not be neighbours any
more), and an output whose neighbour went `--off` is not moved either; those layouts
get Mutter's own verdict — re-place the neighbour in the same call
(`--output C --right-of A`). The `--verbose`/`--dryrun` plan and the `--fb` check
show the shifted layout.

What fails, one line and exit 1, with Mutter's own text after
`xrandr: GNOME's Mutter refused this layout: ` — Mutter's words, in Mutter's
name, because the refusal is never ours: a hole or an overlap (`Logical
monitors not adjacent`; with two monitors that one sentence covers both,
adjacency being checked first — `Logical monitors overlap` needs a layout
where adjacency already holds), turning everything off (`Monitors config
incomplete`), `ApplyMonitorsConfigAllowed=false` (`Monitor configuration via
D-Bus is disabled`).
A configuration serial that went stale between our read and the apply is re-read:
when the monitors and the layout are still the ones the plan was built from (GNOME
bumps the serial on its own as well) the same call is retried once; otherwise — a
hotplug or someone else's re-layout in that window, which the plan knows nothing
about — it is `output configuration cancelled by a concurrent change; try again`.

Persistence: by default the apply uses method 1 (temporary — xrandr semantics, no
dialog, nothing written to disk). The layout is gone at the next login; at a hotplug
Mutter lays the remaining monitors out in a row, and on GNOME 50 it restores the
layout in full the moment the original set of monitors comes back (measured both
ways round — the rotated head unplugged, and a different one; GNOME 46 keeps the
row, WARANDR.md). `--persistent` (a wxrandr
option, not in xrandr's usage text) or `WXRANDR_PERSIST=1` uses method 2: the layout
is applied and gnome-shell shows its "Keep changes?" dialog — wxrandr prints a
one-line warning; confirming it makes Mutter write `~/.config/monitors.xml` at once,
and the layout then survives a hotplug and a reboot; otherwise the previous layout
comes back after 20 s and nothing is written (verified on GNOME 46 and 50: nothing is
written before the confirmation). Confirming it is a click today: the bridge
extension does export `ConfirmDisplayChange`, and it works — `(true)` keeps the
layout and Mutter writes `monitors.xml` at once, `(false)` reverts, and with no
dialog on screen it answers `(false,)` and changes nothing (measured on GNOME 46,
50 and 51.beta) — but no tool in this project calls it, so `--persistent` still
leaves the confirmation to you even where the bridge is up.

What every desktop does with an applied layout, and how to get one back
from a key, is [Keeping a layout](#keeping-a-layout) below.

That file is **all or nothing**, which is worth knowing before you keep anything in
it. It holds one entry per monitor set you have ever saved, and Mutter's reader
verifies every one of them: a single bad entry makes it discard the whole file, at
every login, with nothing said on any screen — the layouts are simply not applied any
more. Nothing wxrandr does can put a bad entry there (every layout goes through the
D-Bus call, which validates first, and Mutter writes the file only when you confirm
its dialog), but something else can, so `--persistent`:

- reads the file first and tells you when GNOME has already discarded it, since the
  save you are about to confirm rewrites it *whole* — after a discarded read that means
  the layout you are saving and nothing else;
- copies what was there to `~/.config/monitors.xml.wxrandr-backup` once Mutter has
  accepted the layout (a refused apply copies nothing). GNOME keeps one generation of
  its own in `monitors.xml~`, but every save overwrites that, this one's included;
- says so when the layout you are saving is one that a later settings change will
  break: with **Fractional Scaling off** the positions are saved in physical pixels and
  the file records no layout mode, so turning that setting on re-reads them as logical
  pixels, a scaled monitor is narrower than the gap its neighbour was saved at, and
  GNOME refuses the whole file. Measured on 24.04 with a `--scale 2` head at the left
  of a row: `Failed to read monitors config file … Logical monitors not adjacent`, both
  saved monitor sets gone. The layout was valid, Mutter accepted it and Mutter wrote
  it; what changed is what the numbers mean.

`--dryrun` additionally submits the exact configuration with method 0 (verify
only) and prints `mutter verify: ok` on stderr (stdout stays xrandr's own dryrun
lines), or Mutter's rejection as the fatal a real run would give.

Launching it (verified on 24.04 and 26.04): a GNOME custom keyboard shortcut runs the
command with the session's environment, nothing to set up — but bind a chord Mutter
does not own: `<Ctrl><Alt>F1`–`F12` are its VT switches (`switch-to-session-N`),
gsd-media-keys logs `Failed to grab accelerator` and the chord changes the VT instead;
`<Super>F8` works. A `cron @reboot` job, or any script with an empty environment,
needs no exports at all (the session is found from the runtime dir owning the Wayland
socket), only to wait until the session bus is up and owns the name — on the rig
`org.gnome.Mutter.DisplayConfig` appears 3–4 s after the session; loop on
`python3 -m wxrandr --listmonitors` until it exits 0. Toggle scripts: `--output C
--right-of B` on a disabled output only positions it (xrandr semantics); to turn it
on say `--output C --auto --right-of B`.

Coordinates are Mutter's logical ones (what `wl_output`/xdg-output and GNOME
Settings show). Real `xrandr` through XWayland agrees byte for byte on GNOME 46 and,
at scale 1, on GNOME 50; Ubuntu 26.04 ships XWayland native scaling, so with any
output at scale 2 real xrandr there reports every output multiplied by that integer
factor (`Virtual-1 2560x1600+0+0` for a 1280x800 panel next to a scale-2 monitor) —
an X-plane artefact wxrandr does not imitate.

Tests: `tests/test_wxrandr_mutter.py` runs the whole CLI against a wire-level mock
DisplayConfig service on `dbus_mini`'s mock bus that validates like mutter
(serial, ids, scales, adjacency, overlap, primary, offset) and emits `MonitorsChanged`.

### Cinnamon, which is this backend with three names swapped

`--backend cinnamon` (aliases `muffin`, `WXRANDR_BACKEND=cinnamon`) builds
`MutterOutputs` with a MUFFIN flavour: the bus name is `org.cinnamon.Muffin.DisplayConfig`,
the object path and the interface follow it, and nothing else about the code path changes.
`GetCurrentState` and `ApplyMonitorsConfig` are byte-for-byte Mutter's signatures,
`APPLY_SIG` applies unchanged, and a real mode change applied and read back with only
those three names swapped. Everything the Mutter section above says about adjacency,
gaps, mirroring and one primary holds verbatim, because muffin carries Mutter's validator
with Mutter's strings — `Logical monitors not adjacent`, `Logical monitors overlap`,
`Logical monitor scales must be identical`, `Config contains multiple primary logical
monitors` — and on a live three-head `resolute-cinnamon-wayland` a muffin was made to
print `not adjacent` for the first time.

Eight behaviours used to be keyed on the token `mutter` and are keyed on the
implementation's flavour instead. What a Cinnamon user sees that they did not:

| what | before | now |
|---|---|---|
| `--gnome-overlap-status` | `reason: this session is cinnamon, which places overlapping monitors without any of this` | `reason: this is Cinnamon, whose Meta-0 typelib has no generation to check; not yet here, and the route is org.Cinnamon.Eval reaching MetaMonitorsConfig inside muffin with nothing installed (route 2), at the cost of an offset record measured per Cinnamon release instead of per Meta generation` |
| `--gnome-overlap-allow` | the same sentence, as a Fatal | the same function's sentence |
| `--unsafe-gnome-overlap` | `only means anything on GNOME; this session is cinnamon, which places overlapping monitors without it` | `only means anything on GNOME; this is Cinnamon, whose Meta-0 typelib has no generation to check`, with the same route and cost after it |
| `--brightness` / `--gamma` | rc 1, `cannot set gamma: no wayland socket` (the wlr path) | `--brightness/--gamma are not supported on Muffin (no gamma LUT API)`, rc 0 |
| `--noprimary` | silence | `Cinnamon requires a primary output; keeping eDP-1` |
| `--dryrun` | silence | `cinnamon verify: ok` |
| `--dryrun --verbose` plan | a screen 240 px too wide (`5760x1600` where the run leaves `5520x1600`) | the neighbours the apply shifts are in the plan |
| `--listmonitors` | the primary not first | the primary first, as RandR 1.5 does |

Every GNOME string is byte-identical, because the words come off `wxrandr/mutter.py`'s
`Flavor` record (`.name`, `.desktop`, `.compositor`). One wording change reaches beyond
Cinnamon: `--unsafe-gnome-overlap` on `kwin`, `sway` and `wlr` now ends *...which places
overlapping monitors without any of this* where it used to end *...without it* — one
sentence for the flag, the status query and the apply gate, which is the point of having
one function behind all three.

Two differences that are Cinnamon's rather than the backend's. A refusal is relayed as
*Cinnamon's Muffin refused this layout: ...*. And `--persistent` warns about Cinnamon's
own dialog — *Keep these display settings?*, with GNOME's 20-second countdown — and backs
up `~/.config/cinnamon-monitors.xml` rather than `monitors.xml`: `xrandr: Cinnamon will
ask "Keep these display settings?" for 20 s; confirm the dialog or the layout reverts`.
That dialog has been answered live over `org.Cinnamon.Eval`
(`global.window_manager.complete_display_change(true)` answers `(true, '"undefined"')`)
and the file, absent before the apply, is written. The discarded-file warning names the
reader that discarded it, so on Cinnamon it reads *Cinnamon has already discarded ...
Muffin's reader drops the whole file, not the one bad entry* where GNOME's says
*GNOME ... Mutter's reader ...*; the `Fractional Scaling` label in the layout-mode warning
is GNOME Settings' own name for that switch and stays capitalised on both.

Tests: `tests/test_wxrandr_cinnamon.py` — `FakeMutter` on a `MutterMockBus(flavor=MUFFIN)`,
the same fake with three names swapped.

## KWin backend (`wxrandr/kwin.py`)

**This is the Wayland session only.** Plasma on Xorg is a plain X11 session: the X
server's RandR is the truth there, so `main()` hands over to the real `xrandr` before
any of this runs, `--print-backend` says `x11` (`compositor: X server (RandR)`), and
`--backends` marks `kwin` `unavailable  no wayland socket` — measured on the
`noble-kde-x11` flavor, where `kwin_x11` owns `org.kde.KWin` on the session bus and
`/run/user/<uid>` holds no compositor socket at all. `--backend kwin` there is the
same one-line refusal (`xrandr: --backend kwin is not available in this session: no
wayland socket`), because the protocol pair below exists only in `kwin_wayland`.

KWin has no `zwlr_output_management` and no D-Bus display API (`org.kde.KWin` only
exposes `activeOutputName()`). Everything goes through the Wayland protocols from
plasma-wayland-protocols — NOT from the kwin repo — which are unauthenticated:
`kde_output_device_v2` per output (read), `kde_output_management_v2`
(`create_configuration` → per-output setters → one `apply`), and
`kde_output_order_v1` (read: which output is primary).

| xrandr | KWin |
|---|---|
| output name, make/model/serial, mm, subpixel, EDID | `name` (device v2; `uuid` is the fallback), `geometry` make/model + `serial_number`, `geometry` mm/subpixel, `edid` (base64, collected for callers, not printed) |
| enabled, `WxH+X+Y` | `enabled` / request `enable`; `geometry` x/y — **logical** coordinates, and the position is not scaled |
| mode list, current, preferred | one `mode` object per mode (server-allocated new_id; `size` in hardware pixels, `refresh` in mHz), `current_mode` (sent only while enabled), the mode's `preferred` marker. `mode` takes an object, never a WxH triple, so a mode is matched by size + nearest refresh |
| `--rotate`/`--reflect` | `transform` (wl_output enum, as libkscreen reads it: 1 → xrandr `left`, 3 → `right`, 4..7 the same rotations reflected — the same 90↔270 permutation of the sway names the Mutter backend needs) |
| `--scale S` | `scale` (wl_fixed), quantised server-side to 1/120 with `std::round` (so `--scale 1.4375` lands on 173/120 = 1.44167, exactly where `kscreen-doctor` puts it); logical size = transform-swapped mode size ÷ scale, and **how that becomes an integer changed with Plasma 6**: 6.x takes the enclosing integer (1920 ÷ 1.4 → 1372, 1080 ÷ 1.4 → 772, measured against `kscreen-doctor` at seven scales), 5.27 rounds (1371). One pixel short is not cosmetic — the neighbour then overlaps by a pixel and KWin keeps it — so the rule is gated on the advertised management version (≥ 7 is Plasma 6). Never `core.logical_size`'s wlroots truncation |
| `--primary` | `set_priority(dev, 1..N)` (management v3) over the whole list, `--primary` first and the rest in the order KWin already has — libkscreen's own `setPrimaryOutput` semantics. `set_primary_output` (v2) is sent alongside but is **accepted and ignored** by KWin on 5.27 and on 6.6 alike (measured: the output order does not move, XWayland's `primary` does not move), so it can never be the mechanism. Read back from `kde_output_order_v1` — its first entry is the primary plasmashell and XWayland follow, and it is advertised on 5.27 too, where the device `priority` event (v18) is out of reach. Below management 3, `--primary` warns and is *not* written to the state file: `--query` never names a primary the compositor was not asked for |
| `--same-as` | plainly the same position while that *is* the clone, and `set_replication_source` (management v13) only where it is not. On KWin every output is a view onto one shared scene, so two outputs at one position show identical pixels — measured byte-identical (`AE 0`) whenever their **logical rectangles coincide**, at different refresh rates as much as at the same one. Where the rectangles differ the smaller output shows a *crop* of the bigger one's scene, measured on KWin 6.6: 1280x1024 against 1920x1080 is exactly the top-left crop (`AE 0` against that crop, and the window at x=1450 is on one head and not the other), `--scale 2` is the top-left quarter magnified (RMSE 1.2% against that crop, 12.4% against the whole frame), `--rotate left` is the leftmost 1080 columns turned on their side (RMSE 0) with the rest of the panel showing desktop that is not there. Replication is what turns those into a copy: KWin fits the source's whole image into the replica's panel, aspect preserved and centred — `scale = min(dst_w/src_w, dst_h/src_h) * src_scale`, `offset = (dst_px − src_px/src_scale × dst_scale) / 2` — measured to the pixel in both directions (a 96-row letterbox at 1024x768, first lit column exactly 285 at 1920x1080), and the replica's own scale is overridden outright, so where the sizes allow it the clone is byte-identical (`AE 0` with `--scale 2` requested). So the rectangle differing is the whole trigger: not the refresh rate, and not `--same-as` itself. The rectangle compared against is the one the *scene* comes from, which is not always the output `--same-as` named: an output that is itself mirroring shows somebody else's scene, so `--same-as` follows the chain to its root and replicates that. Below management 13 the crop case is refused by name (`xrandr: cannot mirror DP-1 onto eDP-1: at the same position DP-1 would show a 2560x1600 crop of eDP-1's 1920x1080, and cloning it needs kde_output_management_v2 version 13 (this KWin offers 3)`) and the coinciding case still works, on 5.27 as on 6.6 |
| a copy of a copy | is what KWin will not draw. `set_replication_source` naming an output that is itself replicating is accepted, stored and persisted, takes the output out of the layout like any other replication — and then paints it never: measured on KWin 6.6, the panel kept its last frame byte-for-byte across a window move that repainted every other head, and two outputs replicating *each other* left `kde_output_order_v1` with neither of them in it. So `--same-as` never sends one: it resolves the source through the chain to the output whose scene it really shows and replicates that (which is also the right answer — the second replica letterboxes the same picture), a chain that comes back to the output itself is nothing to replicate at all (`--same-as` the output that mirrors you is a shared position), and a loop somebody else left is refused by name. One found in the wild is reported with the geometry KWin stores for it — it is not showing its source's rectangle either — and named on stderr |
| the rectangle a replica occupies | is its **source's**, and that is what every relation measures from: `--right-of` a 1280x1024 replica of a 1920x1080 output starts at 1920, not at 1280. Measured on KWin 6.6 the other way round: the replica's own panel size put the neighbour 640 px *inside* its source, two overlapping panels on a desktop meant to be a row, because the replica contributes nothing of its own to the layout. Disabling the source hands it back to itself — layout, position, scale and all — so the position it comes back at is the one `--query` was reporting for it |
| a replicated output | stops being a layout member: its `wl_output` global goes away, it leaves `kde_output_order_v1` — so it can never be the primary, `set_priority` on it moving nothing (measured), and `--primary` on one says so instead of sending it — and it contributes nothing of its own to the bounding box. Its own **mode and transform still count** (they are the panel the copy is fitted onto); its own **position and scale are inert**, but still accepted, still read back and still persisted, and come back the moment the mirror ends. `--query` therefore reports it with the *source's* rectangle and its own mode table, so the pair reads the way xrandr renders a mirror and the `Screen` line matches the desktop KWin really has. Clearing the source (the empty string) restores it completely, and so does disabling the source. Mirroring an output onto itself is KWin's own `failed` (`An output cannot mirror itself`); a uuid naming no enabled output is accepted and silently does nothing, so the source uuid always comes from a fresh snapshot. It is persisted like everything else, as `replicationSource` in `~/.config/kwinoutputconfig.json`, and the undo line spells it `--same-as` rather than as a position. `kscreen-doctor` 6.6 can read the replication source and has no syntax to set one |

Versions move fast. KWin's own `s_version`, device / management, per release:
5.27 = 2/3, 6.0 = 6/7, 6.3 = 11/12, 6.4 and 6.5 = 16/16, 6.6 = 20/19,
**6.7 = 23/21**, master = 25/22 — so we bind LOW (device
`min(advertised, 13)`: `name` is since 2 and `replication_source` since 13;
management `min(advertised, 13)` for `set_priority`, `failure_reason` and
`set_replication_source`) and gate mirroring on **both** halves — the request
to send one and the event to read one back, because a mirror that cannot be
read back could never be cleared again and `--right-of` a replica would be
silently inert — and
gate every optional feature on the *bound* version: on 5.27 a rejection carries no
reason string at all and says so. `done` is the publish barrier: an output
appears in the snapshot only after it arrives — and management advertised with not
one device published is a refusal, not an empty screen and an apply that silently
succeeds.

### The two discovery paths, and which Plasma made the second one

Through Plasma 6.6 every output is its own `kde_output_device_v2` wl_registry
global. **Plasma 6.7.0 is where that stopped** — not 6.6, and not master-only:
kwin commit `7e32e00c`, *“wayland: Don't advertise kde-output-device-v2 globals
anymore”* (2026-03-05, “all various Plasma components have been migrated to the
registry global”), landed beside `67f58528` *“Implement kde-output-device-v2
v21”* and shipped in v6.7.0. It was never backported: v6.6.6, cut four months
later, still builds `new OutputDeviceV2Interface(m_display, output)`, while
v6.7.0's `wayland_server.cpp` constructs only `m_outputDeviceRegistry` and
answers a hotplug with `m_outputDeviceRegistry->offer(output)`. So on 6.7 there
is no device global left to bind, and the devices arrive as `new_id`s on
`kde_output_device_registry_v2` (plasma-wayland-protocols ≥ v1.21.0), whose two
events are `finished` (opcode 0, no arguments) and `output` (opcode 1, one
`new_id`).

Three things about that interface decide the client:

* **It cannot be bound low.** `kde_output_device_registry_v2_bind_resource()`
  answers `wl_resource_post_error(..., error_unsupported_version, "unsupported
  version")` below 21, so `REG_MIN` is a floor, not a preference — the one
  place in this backend where binding low is wrong.
* **The device version is the registry's.** `offer(target->client(),
  target->version(), …)` → `wl_resource_create(client,
  &kde_output_device_v2_interface, version, 0)`; there is no second bind to
  negotiate with, and the trailing `0` is what puts the object id in the
  server's `0xff000000` range, which is how we tell an announcement from
  anything else on the wire.
* **The burst is synchronous.** `d->add(resource)` runs
  `kde_output_device_v2_bind_resource()`, which sends geometry … `done` right
  there, so binding the registry publishes every output in one roundtrip.

A hotplug-out on this path is likewise not a global going away — there is no
global — but the device's own `removed` (event 36, since 21), with the mode
objects torn down behind it. Both paths are implemented and the device object
behaves identically past discovery; libkscreen's own client — the one
`kscreen-doctor` and the System Settings KCM are built on — made exactly this
move, and its `WaylandOutputDeviceRegistry` binds the registry at the same
number KWin advertises (23 at v6.7.4, 25 on master) and collects its devices
from `kde_output_device_registry_v2_output`, which is what `min(advertised,
REG_WANT)` lands on here too. It dropped the globals path outright; we do not,
because 5.27 through 6.6 are still supported.

**Measured on Plasma 6.7.4** (KWin `4:6.7.4-0ubuntu2`, Ubuntu 26.10, the
`stonking-kde` vmctl flavor), which settles it: `kde_output_device_v2` is
**absent from `wl_registry`** on that session — the globals are
`kde_output_device_registry_v2` v23, `kde_output_management_v2` v21 and
`kde_output_order_v1` v1 — so the registry path is not an optimisation there,
it is the only way to see an output at all. It also decides the order the
outputs are *listed* in: 6.7 hands them out in the order the registry announces
them, which on the rig puts `Virtual-2` before `Virtual-1`, where 5.27 and 6.6
(and the real xrandr on the same machine) list `Virtual-1` first. Nothing here
sorts them: the list is the compositor's own, on every backend, and on KDE it is
the one `kscreen-doctor` shows. Through it, on two virtual heads:
`--query` and `--listmonitors` list both outputs with every mode, `--mode`,
`--pos`/`--right-of`, `--rotate left`, `--off` and back, `--scale 1.5`
(1920 ÷ 1.5 → 1280×720, byte-equal to `kscreen-doctor`'s `Geometry`),
`--primary` (priorities 1 and 2, and `kscreen-doctor` agrees) and `--same-as`
(`set_replication_source`, read back as `replication source:2`, the replica
reported at its source's rectangle) all apply and read back correctly, the
restore line each apply prints round-trips, and both refusals still refuse
(`cannot disable all outputs`, `cannot find mode 1234x567`). A head plugged and
unplugged from outside the guest is picked up both ways — on this path an
unplug is the device's own `removed` (event 36, since 21), not a global going
away, and that is the one thing no other Plasma can exercise.

Behind that, and what stands when a newer Plasma lands: every opcode, `since`
and argument shape this backend uses is checked field by field against
plasma-wayland-protocols `kde-output-device-v2.xml` /
`kde-output-management-v2.xml` at v1.21.0 and master by
`repro/kde-outreg-conformance.py` (45 constants, all matching), and the fake
compositor in `tests/test_wxrandr_kwin.py` is built from that same table: it
sends **every** event the bound version covers in KWin's own `bind_resource()`
order — all 40 of them at v23, not the fifteen the backend reads — because a
client that mis-parsed one of the other twenty-five would lose its place in the
stream. The failure modes are engineered too, and measured against
`repro/kde-outreg-specfake.py`: a registry that binds and announces nothing is
a refusal naming the path and the version bound (`xrandr:
kde_output_management_v2 announced no outputs (this compositor hands them out
through kde_output_device_registry_v2; wxrandr bound it at version 23)`), one
too old to bind says so and falls back to the globals if the compositor still
has them, and one that goes quiet after binding is a single `xrandr:` line
after the socket deadline — never a hang, never a traceback.

Apply is atomic and **one-shot** — a second `apply` on one configuration object is
a fatal `already_applied` protocol error that kills the connection — so every
attempt builds a fresh object, and only deltas are sent (an unchanged invocation
creates no configuration at all: a no-op changeset still costs a modeset). An
output coming back from disabled is described in full. A hotplug between
`create_configuration` and `apply` silently invalidates the object. The retry is
keyed on evidence, not on that message: `failure_reason` is management v12, so on
5.27 (which advertises 3) the string can never arrive, and what is checked instead
is whether the set of published device objects moved while the configuration was
open. Then it is rebuilt once from a fresh snapshot, mode objects and all. A
rejection with the outputs unmoved is reported, not retried. Every socket error on
the send side is one `xrandr:` line too, never a bare `[Errno 32] Broken pipe`.

What we refuse client-side, before KWin's own message: disabling every output
(`xrandr: cannot disable all outputs (KWin requires at least one enabled output)`)
and an apply against a compositor that published no output at all.
What we normalise: the layout slides back to the origin, because KWin rejects any
enabled output at a negative coordinate. What warns and succeeds:
`--brightness`/`--gamma` (probed — no `zwlr_gamma_control_manager_v1` under KWin
and no LUT call in the protocol), `--noprimary` (KWin's output order always has a
first entry, so there is no inverse to set), `--set`/`--transform`/`--panning` as
elsewhere. What fails like Mutter:
`--newmode`/`--addmode` applied without a real mode of the same size and rate
(`cannot find mode NAME`) — protocol custom modes need management v18 plus
`capability_custom_modes`, which nothing we bind offers.

**KWin always saves the layout.** Plasma 6 writes `~/.config/kwinoutputconfig.json`
from `applyOutputConfiguration` itself; on 5.27 `kded5 kscreen` watches the same
libkscreen monitor and writes `~/.local/share/kscreen/<hash>`. There is no
temporary mode and no confirmation dialog — the 15-second countdown belongs to the
System Settings KCM, not the compositor — so `--persistent` is the only mode there
is, and on Plasma 6 the file is already on disk at first login, before any command of
ours. Clearing it needs the session stopped: deleted from inside the session it is
simply written out again when KWin exits (measured). Every apply that KWin actually
took says so once, and prints the `xrandr …`
command that restores the pre-apply snapshot, which is the only undo there is; an
apply KWin refused says neither (nothing was saved, and that command would *change*
the live layout). Because it is the only undo, it spells every property out —
`--mode/--rate/--pos/--rotate/--reflect/--scale`, plus `--primary`, defaults
included — so replaying it really is the inverse (verified live, and again after a
reboot: layout, scale, rotation and primary all come back). **The line begins with
the word `wxrandr`**, always, whatever name this process was invoked under
(`kwin._undo_word()`): on a stock Plasma image `/usr/bin/xrandr` exists, so a line
beginning `xrandr` would be pasted straight into the real one, which answers a
`BadMatch` on `RRSetScreenSize` and changes nothing. The saved layout scripts name
the bare command word for exactly the same reason. The one thing it cannot express is
KDE's full output *order*: xrandr has no syntax for it, and libkscreen permutes the
non-primary ranks the same way. `--dryrun` runs the plan client-side only (mode
resolution, the last-output refusal) and touches nothing — not even the state
file's primary: KWin has no verify request, so nothing is claimed about it.

Tests: `tests/test_wxrandr_kwin.py` runs the whole CLI against a wire-level fake
KWin — a real unix-socket wl_display speaking all three protocols over the actual
Wayland wire format, and modelling KWin's own behaviour including a
`set_primary_output` that does nothing — covering both discovery paths, both
version pairs, the mode-object matching, the 1/120 scale round trip, the Plasma
5.27/6 logical-size split, negative-coordinate normalisation, delta-only
changesets, the one-shot rule, the invalidation retry with and without a reason
string, failure with and without one, the restore command replayed as an inverse,
the last-output refusal, the no-output refusal, a compositor that hangs up
mid-apply and the query bytes.

## Keeping a layout

Nothing in this repo restores a layout on its own. There is no daemon, no service and
no autostart entry: `wxrandr` and `warandr` change the screen when you run them and
then exit, and nothing here watches for a monitor being plugged in. (The only
resident process the toolbox ever leaves behind is `wdotool`'s input daemon, which
owns input devices and has nothing to do with outputs.) What becomes of a layout
after that is the desktop's business, and no two desktops agree.

Measured on GNOME 50 (Mutter), Plasma 6 (KWin), sway 1.11 (wlroots) and Xfce 4.20 on
X11, on three heads with one of them rotated:

| | a head unplugged and plugged back in | reboot | where the desktop keeps a layout |
|---|---|---|---|
| **GNOME** (Mutter) | comes back in full: Mutter lays the *remaining* monitors out in a row while the set is short, and puts the layout back when the original set returns | lost, unless a `--persistent` apply was confirmed | `~/.config/monitors.xml`, written by GNOME Settings or a confirmed `--persistent` and by nothing else; a fresh install has none; one bad entry discards the file whole (above) |
| **KDE Plasma** (KWin) | comes back in full | **kept** | `~/.config/kwinoutputconfig.json`, written by every apply KWin takes |
| **sway** (wlroots) | comes back in full, every output | lost | nothing on disk; only `~/.config/sway/config` makes a layout stick |
| **Xfce** (X11) | **lost**: the head comes back at the end of a plain row, unrotated, and `primary` is cleared | lost, `primary` with it | nothing; `displays.xml` is byte-identical after an apply |
| **Hyprland** | not measured on the rig yet | lost | `hyprland.conf`, which nothing here writes: `--persistent` says so in one line. Saving there is **not yet**, and the route is a `monitor=` line in a snippet that file sources (route 2), at the cost of owning a file the user hand-edits |
| **Cinnamon** (Muffin) | not measured on the rig yet | lost, unless a `--persistent` apply was confirmed | `~/.config/cinnamon-monitors.xml` — Mutter's rule under Cinnamon's file name, behind Cinnamon's own *Keep these display settings?* dialog. Measured written on `resolute-cinnamon-wayland`, 2026-09-09, where it was absent before the apply |
| **labwc**, and Budgie / Xfce / LXQt on it | one head in every configuration comes back where labwc chose, which is what `WlrOutputs.apply`'s second send is for | lost | nothing on disk |

Two of those rows say "not measured on the rig yet" and mean it: the hotplug column is a
`vmctl head ... off` / `on` cycle, and neither the `resolute-hypr` nor the
`resolute-cinnamon-wayland` run has taken one. Nothing here may be read as a promise about
either until one has.

Restarting the compositor is a third event, and it splits the same way: `swaymsg
reload` puts sway's outputs back in its own enumeration order, while `xfwm4
--replace` changes nothing, because on X11 the layout belongs to the X server and not
to the window manager. Xfce's forgetting at hotplug is `xfsettingsd`'s doing — it is
what re-enables the returning head, in a row, and it clears `primary` when it starts.

**KDE saves whether you want it to or not.** KWin has no temporary mode: every apply
it takes lands in `~/.config/kwinoutputconfig.json` in the same second, the file is
there before you run anything, and `--persistent` is accepted but means nothing.
Every such apply also prints, once, the command that puts the previous layout back
(see the KWin backend section above for exactly what that line is). To clear what KDE
remembers, delete that file with the session stopped — deleting it from inside the
session achieves nothing, because KWin writes it out again on the way out.

**On GNOME an apply is temporary, like xrandr's**, and writes nothing. `wxrandr
--persistent` applies the layout and lets gnome-shell ask *Keep these display
settings?* for 20 seconds: ignored, the layout reverts and nothing is written;
confirmed, `monitors.xml` appears at once and the layout then survives both a hotplug
and a reboot. The dialog and the switch are GNOME's alone: on KDE `--persistent` is
accepted and changes nothing, and on sway and X11 nothing is written either way — on
X11 the option is dropped from the argv the real xrandr is handed, which has never had
it, so the apply itself goes through as usual.

The way to get a layout back on any of the four is to save it as a script and put
that script on a key. That is arandr's habit, it reads the same everywhere, and it
runs when you press it rather than when something guesses you wanted it:
[WARANDR.md § A layout script on a key](WARANDR.md#a-layout-script-on-a-key).

## Command surface (byte-parity target: xrandr 1.5.x)

`wxrandr --help` prints xrandr's own option list verbatim, and everything in it is
accepted. What each group does here:

- **Query**: bare `wxrandr`, `-q` and `--query` print the Screen line with
  minimum/current/maximum, then per output `NAME connected/disconnected [primary]
  WxH+X+Y (normal left inverted right x axis y axis) MMmm x MMmm` and the mode table
  with per rate columns, `*` current and `+` preferred. `--verbose` adds the transform
  matrix and the gamma and brightness lines, and omits EDID, which no compositor hands
  a client. `--current`, `--listmonitors` and `--listactivemonitors` render the RandR
  1.5 monitor format out of the output layout. `--listproviders` prints one synthesized
  provider for the compositor, capability `0xb` (Source Output, Sink Output, Sink
  Offload) with the output count on both sides: an invention, because Wayland has no
  GPU provider object, and one line is what a script parsing `xrandr --listproviders`
  expects to find.
- **Mutation**: `--output NAME` with `--mode WxH`, `--rate R`, `--auto`, `--preferred`,
  `--off`, `--pos XxY`, **`--left-of` / `--right-of` / `--above` / `--below` /
  `--same-as OTHER`** (relative placement, mirroring through `--same-as`, resolved
  against the target's *pending* geometry, so a chain like `--output B --right-of A
  --output C --right-of B` lands in one invocation), `--rotate
  normal|left|right|inverted` with xrandr's own WxH swap, `--reflect normal|x|y|xy`
  mapped onto the compositor's flipped transforms, `--scale SxS` and `--scale-from
  WxH` (Wayland scales both axes by one factor: an anisotropic request warns, names
  both numbers and uses the x factor), `--primary`, `--brightness B`, `--gamma R:G:B`,
  `--dpi`, `--fb`, `--dryrun` (resolve and print what would change, mutate nothing),
  and `--newmode` / `--addmode` / `--delmode` / `--rmmode`, whose modelines are kept in
  the state file and turned into WxH@refresh for the backend.
- **`--primary` has no Wayland equivalent**, so it is kept in a small state file keyed
  by the compositor socket and shown consistently in every listing. What each backend
  does with it on a *disabled* output differs, and *Known limitations* below says how.
- **The warn and ignore set**: `--setmonitor`, `--setprovideroutputsource`,
  `--setprovideroffloadsink`, `--panning`, `--transform` and `--set` warn on stderr,
  change nothing and exit 0, because failing them would break a script the tool
  otherwise runs unchanged.
- **Multiple `--output` stanzas in one invocation are one layout change**: the whole
  target layout is computed first and then submitted, as one `ApplyMonitorsConfig` on
  Mutter, one configuration on KWin, one atomic call on wlr, and two IPC batches on
  sway (see *Known limitations*).
- Errors are byte styled like xrandr's (`cannot find output`, `cannot find mode`) and
  carry its exit codes.
- **The options that are ours are accepted and not printed**, because that help text
  is xrandr's own bytes and adding to it would end the parity. They are, in full:
  `--persistent`, `--backend NAME`, `--backends`, `--print-backend`,
  `--gnome-overlap-status`, `--gnome-overlap-allow`, `--gnome-overlap-forget`,
  `--unsafe-gnome-overlap` and `--unsafe-gnome-overlap-unmeasured MAJOR`. That is the
  whole list, and `scripts/check-docs.py` holds it to the parser: an option of ours
  that stops being accepted, or that starts appearing in the help, is reported.
  (`--q1` and `--q12` are xrandr's own compatibility tokens, accepted and ignored,
  and absent from its usage text as well.)

## What differs on the wlroots floor, and on i3

* **A layout the compositor rearranges after accepting it.** Handled, since
  `WlrOutputs.apply` reads the layout back and re-sends the identical configuration once
  when a head is not where it was put. Measured on `resolute-labwc` (labwc 0.9.3 on
  wlroots 0.19.2, three 1920x1080 heads, 2026-09-09): `--output Virtual-3 --off` followed
  by `--output Virtual-3 --auto` asks for Virtual-3 at 0,0 — which is what xrandr does, an
  output that comes back from `--off` lands on the origin and on top of whatever is there
  — and labwc answers `succeeded` and then lays the three heads out itself. From then on
  one head in every configuration of that session came back where labwc chose:

  ```
  asked   V-1 0,0    V-2 1920,0    V-3 3840,0
  got     V-1 0,0    V-3 3840,0    V-2 5760,0
  ```

  and the stray head landed hard against the right edge of everything labwc did place;
  three applies later the rig's own mirror step was refusing `800x600+100+100` because
  Virtual-3 had drifted to `1920x1080+9600+1080`. This is not our wire: the three
  `set_position` requests were read off it in the guest and carried exactly those numbers,
  and `wlr-randr` 0.4.1 — the reference client, and that flavor's own oracle — produces the
  identical layout from the identical starting state. Re-sending lands every head where it
  was asked, on the first retry, every time it was tried, so the cost is one extra apply on
  a compositor that has rearranged and nothing at all on one that has not. labwc 0.9.3 is
  the only compositor measured that reaches the second send; sway 1.11 forced onto
  `--backend wlr` does not, and Hyprland is not on this code path at all — detection sends
  it to `wxrandr/hypr.py`, whose `_verify_applied` is this read-back's analogue there. A
  compositor that ignores the retry too gets the numbers in a sentence — `xrandr: the
  compositor accepted the position 1920,0 for Virtual-2 and put it at 5760,0 both times` —
  instead of a layout nobody asked for.
* **`--persistent` on sway and i3** says once that it saves nothing. The route is an
  `output` line in a file sway's config sources (route 2), at the cost of owning a file the
  user hand-edits. **Not yet.**
* **`--scale WxH` with W != H** — xrandr's transform matrix — warns and applies the first
  axis for both. No output-management protocol carries a per-axis scale, so the route is a
  patched compositor (route 6). The warning stays a warning: the apply goes on.
* **`--backend sway` reads "sway (also on i3)"**, and on i3 three things differ.
  `--print-backend --verbose` says `compositor: i3 4.25.1 (2026-02-06)` where it used to
  say `sway 4.25.1`. `--query` does not list i3's `xroot-0` pseudo-output (an
  `active: false` row covering the X screen) and invents no mode table, because i3's
  `GET_OUTPUTS` carries no `modes` and no `current_mode` at all. And **any apply is
  refused up front**, in one line and with nothing sent:

  ```
  xrandr: this is i3, which has no output command; the X server owns the layout here --
  use xrandr (or drop --backend sway)
  ```

  i3 has no `output` command; every apply used to die with i3's 30-token parse error after
  phase 1 had already recorded the modes.
* **Reading the layout with no `zwlr_output_manager_v1`** — which is what `wmirror` needs
  to place a mirror — is **not yet** on Cinnamon and the desktops with no output protocol.
  The route is the compositor's own display bus or IPC, which wxrandr already speaks four
  of (route 2), at the cost of one layout reader per compositor.

## Known limitations

Measured, understood, and left as they are. Each says why.

- **`--reflect y` and `--reflect xy` are not idempotent.** Wayland has eight
  transforms where RandR has sixteen (rotation × reflection) pairs, so
  `core.RANDR_VIEW` has to read every flipped transform back as a reflection in
  *x* — the compositor cannot tell us which axis the user meant. Repeating
  `--reflect y` therefore composes with what is already there instead of being a
  no-op: `normal → y → (x, rotated 180) → ...`. `--reflect x` and
  `--reflect normal` are unaffected. Spell **both** `--rotate` and `--reflect` in
  the same command and the result is exact whatever the current state (which is
  what the saved layout lines and the `--restore` path already do).
- **sway's two-phase apply can be interrupted.** The sway backend applies modes,
  scales and transforms in one IPC batch, re-reads the logical sizes the
  compositor really produced, and pins the positions in a second batch — the
  re-read is what makes relative placement exact there. A signal in the 0.04–0.06 s
  between the two leaves the modes applied and the positions stale, exactly as
  killing xrandr between two CRTC calls does on X11. Mutter (one
  `ApplyMonitorsConfig`) and KWin (one configuration) are atomic and have no such
  window; the wlr backend is one atomic call as well. Re-running the same command
  converges.
- **`--primary` on a disabled output does different things per backend.** On
  Mutter and KWin it is a silent no-op — neither compositor will hold a primary
  flag on an output that is not in the configuration — while sway and the wlr
  backend keep it in the state file and show it again when the output comes back.
  xrandr on X11 keeps the flag and prints `connected primary` for a disabled
  output, so no behaviour here is "the" right one, and wxrandr warns about none of
  them.
- **The warn-and-ignore options do not validate their argument.** `--panning`,
  `--setmonitor`, `--transform`, `--set` and a `--scale-from 0x0` warn that they
  do nothing on Wayland and succeed, without looking at what they were given —
  so four argv forms that real xrandr rejects are accepted here. This is
  deliberate: refusing an argument to an option that has no effect would fail
  scripts that the tool otherwise runs unchanged, which is the whole point of the
  warn-and-ignore set.

## Crazy-config requirements (what the torture tests check)

Headless sway grows outputs on demand (`swaymsg create_output`, `output X unplug`) —
build and verify for real: 3–4 output layouts; L-shaped and staircase arrangements;
negative origins; mixed scales (1 + 1.5 + 2); portrait (left/right) mixed with
landscape; mirrored pairs via --same-as; a custom --newmode applied to a headless
output; disabling the middle output of a row (holes are legal); repositioning in one
atomic call. Cross-tool invariant: after every layout change, `wdotool
getdisplaygeometry` and absolute `mousemove` must stay correct (the daemon re-reads
geometry per request — verify, and flag if caching breaks this), and `wwmctl -d`'s
WA geometry must track.
