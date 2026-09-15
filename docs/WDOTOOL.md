<a id="wdotool-the-reference"></a>

# wdotool: the reference

Drop-in `xdotool` clone for Wayland: all 48 commands, byte-parity against xdotool
4.20260303.1, and the same chaining, flags and exit codes. Pure-stdlib **Python
3.10+**. Four ways to install it (the `.deb`, `pip install -e .` from a clone, the
single-file zipapp, `nix build`) are described in the [installation guide](../README.md#install).
This reference covers command usage, compatibility and implementation details.

Read [Technical.md](Technical.md) for what wdotool shares with the other five tools:
session discovery, the X11 handover, the two wire clients, the environment table and
the test map. Read [gnome/README.md](../gnome/README.md) for the bridge extension's own
interface.

## Common commands

On GNOME, install the [bridge](../README.md#gnome) for window operations. Input
injection needs the [input access setup](../README.md#input-access).

```sh
wdotool search --name Terminal       # find window IDs
wdotool getactivewindow              # print the focused window ID
wdotool type 'Hello'                 # type into the focused window
wdotool key ctrl+l                   # send a key combination
wdotool click --repeat 2 1            # double-click the left button
```

See the [options table](#the-option-surface-per-command),
[current differences](#current-compatibility-differences), and
[keyboard layouts](#keyboard-layouts). Implementation details follow below.

## `click --repeat`

`click --repeat N` sends the button N times rather than once, and the gap between
them is `--delay MS`, 100 ms by default. It is the one option of `click` that is easy
to miss, because a double click is what people reach for it for:

```console
$ wdotool click --repeat 2 1              # a double click, left button
$ wdotool click --repeat 3 --delay 120 1
```

Watch the spelling, because it is not ours: the **key** commands call the same thing
`--repeat-delay` and keep `--delay` for the gap between keystrokes, while `click` has
only one gap and calls it `--delay`. That is how xdotool spells them, so it is how
these do. `--delay` on `click` does nothing at all without `--repeat`, again as
xdotool documents.

The gap is real time, so a repeat is not instantaneous, and the tool waits for it
before exiting the way xdotool does.

## On Wayland, with xdotool installed, this runs xdotool

Since the X11 proxy, `wdotool` hands over on a **Wayland** session as well as on an X11 one:
the original is exec'd with `DISPLAY` pointing at [`xw11`](XW11.md), which answers for
native Wayland windows as well as for Xwayland's. The clone still runs when the original
is not installed, when `W11_PROXY=never` (or `WDOTOOL_PROXY=never`) asks for it, when
`W11_PASSTHROUGH=never` is set, and whenever the command uses one of our own options --
`--layout`, `--vkbd` or the `keys` command, none of which xdotool has.
Everything this document says about the clone's output is still what the clone prints;
through the proxy the output is the original's, byte for byte, because it **is** the
original. The six rules in order are
[XW11.md § The wrapper rules](XW11.md#the-wrapper-rules).

## Compatibility

All 48 xdotool commands are implemented, with output byte-compatible against
xdotool 4.20260303.1, `--help` text, error strings, and several verbatim C bugs
included, e.g. `windowmove`'s percent-y quirk, and `windowstate` applying only the
last `--add`/`--remove`/`--toggle` on the line (`--add MAXIMIZED_VERT --add
MAXIMIZED_HORZ` maximizes horizontally only, exactly as upstream does). That is our
own code, i.e. every Wayland session; on an X11 session we hand over, so what you get
there is the command set of the `xdotool` that is installed.

### Current compatibility differences

These describe our Wayland implementation. On X11, the installed original's
behavior applies. A command that warns and returns success may have made no change;
check the backend-specific entry before relying on it.

| Operation | Affected backend or input path | Current behavior |
|---|---|---|
| `key`/`type --window` | Wayland input | Activates the target before injection; does not send an XSendEvent to it. |
| `getmouselocation` | Backend-dependent | Queries the compositor where available; otherwise reports a position established by wdotool or fails. See [pointer queries](#pointer-queries). |
| `--clearmodifiers` | uinput / virtual keyboard | Treatment of physically held modifiers depends on the input path. See [physically held modifiers](#physically-held-modifiers). |
| `type` non-US characters | Active layout | Warns and skips characters the layout cannot produce. |
| `search --role` | Backends without role metadata | Matches against an empty role. Reading `WM_WINDOW_ROLE` for XWayland clients is route 5; native clients need compositor metadata (routes 2–3), with a per-backend reader. |
| `windowraise` / `windowlower` | Backend-dependent | Support and focus side effects vary; see [backend notes](#backend-notes) and the [KDE differences](#what-differs-from-x-on-kde-plasma). |
| `set_window`, `windowreparent` | Current Wayland command handlers | Warn and return success without applying the operation. XWayland support needs X property/reparent requests (route 5); native support needs compositor hooks (routes 2–3, or 6), implemented per operation. |
| Desktop count / viewport setters | Backend-dependent | Some backends change workspaces; others warn and return success. KWin changes the desktop count subject to its limits. See [KDE differences](#what-differs-from-x-on-kde-plasma) and [base-class fallbacks](#what-the-base-class-answers-and-for-whom). |
| `behave` — `focus` / `blur` | Backends with an event stream | Watches the backend's `events()` (every backend has one; the wlr/cosmic floors poll the toplevel protocol's own `activated` change, route 1) and runs the action on the focus transition. As in xdotool, both names share one focus subscription and fire on **both** the watched window's FocusIn and its FocusOut (`cmd_behave.c`), so `behave W focus` also fires when W loses focus and `behave W blur` when it gains it — the original's behaviour, reproduced. |
| `behave` — `mouse-enter` / `mouse-leave` | Backends with a pointer query | Polls the pointer (the compositor's query where it has one, else wdotool's tracked position) and hit-tests `list()`; runs the action on the enter/leave transition. On the first outside sample rather than xdotool's `mouse-leave` re-query (a 100 ms sleep then a second pointer read to swallow the spurious LeaveNotify X sends at screen edges — no such event exists here, so the poll fires straight away). |
| `behave` — `mouse-click` | Every backend | **Not yet** wired: a button release wdotool did not itself inject is not in any window protocol; the route is an evdev read on `/dev/input` (route 4, the access the uinput typing path already takes), else the GNOME bridge's captured-event signal (route 3). |
| `behave_screen_edge` | Backends with a pointer | Polls the pointer against the layout box with xdotool's `--delay` dwell and `--quiesce` cooldown. Where the backend publishes no cursor position and wdotool has moved none (sway/wlr/cosmic), it is **not yet** watched here: route 1, a 1‑px `zwlr_layer_shell_v1` strip per edge whose `wl_pointer.enter` is the crossing (the waycorner shape), at the cost of a surface that eats a click on that line. |
| `selectwindow` | GNOME / KWin / Cinnamon | Uses the compositor's picker (Cinnamon's is a reactive full-stage Clutter actor pushed in over `org.Cinnamon.Eval`, route 2 — it grabs the click and hit-tests `list()`). Cancellation and concurrent-picker behavior differ; see the backend notes. |
| `selectwindow` | sway / i3 / wlr / cosmic | Waits for the next focus change. Selecting the already focused window does not complete it. A click-to-pick picker needs an input-grabbing surface (route 1, the layer-shell overlay) over a geometry source, with a temporary UI and event handling. |

### Pointer queries

GNOME, KDE and Wayfire can report the current cursor position. If such a query
fails, the command reports that backend's error. sway IPC and the generic virtual
pointer protocol do not supply a position query, so wdotool reports the position
it established itself and refuses when no such position is known.

Tracking arbitrary pointer movement there is not yet implemented. A layer-shell
overlay could read `wl_pointer.motion` (route 1), but it would intercept input and
must be removed before the next click. Reading evdev events (route 4) needs device
permissions and a known starting position. See [pointer accuracy](#pointer-accuracy)
for coordinate and scaling details.

### Physically held modifiers

On uinput, `--clearmodifiers` clears and restores keys held by wdotool itself.
A key-up on its virtual device does not release a key held by a physical keyboard.
Those keys remain held; wdotool warns when it has permission to inspect the input
devices. Without that permission the behavior is the same, but no warning is emitted.

The virtual-keyboard path has separate modifier state for its keystrokes. Pointer
clicks still inherit keyboard modifiers at the seat, so the physical-modifier warning
also applies to pointer commands. Clearing physical modifiers through uinput is not
yet supported; a compositor input hook (routes 2–3) would need to save and restore
seat state without leaving keys stuck.

### Help and additional options

`wdotool <command> --help` preserves the original command's help text, including
upstream wording. Our additional leading options, `--layout` and `--vkbd`, are
documented under [forcing the layout](#forcing-the-layout) and
[unprivileged input](#typing-and-clicking-with-no-privilege---vkbd); `wdotool keys`
has [its own help](#which-key-was-that-wdotool-keys).

### The option surface, per command

Every long option any command accepts, which is xdotool's own set and is what
`wdotool <cmd> --help` prints for each of them. Nothing here is ours: the two options
that are (`--layout` and `--vkbd`) go **before** the command and are
[below](#forcing-the-layout). `-h`/`--help` is accepted everywhere.

| commands | options |
|---|---|
| `search` | `--all` `--any` `--class` `--classname` `--desktop` `--limit` `--maxdepth` `--name` `--onlyvisible` `--pid` `--prefix` `--role` `--screen` `--shell` `--sync` `--title` |
| `set_window` | `--class` `--classname` `--icon-name` `--name` `--overrideredirect` `--role` `--urgency` |
| `type` | `--args` `--clearmodifiers` `--delay` `--file` `--terminator` `--window` |
| `key`, `keydown`, `keyup` | `--clearmodifiers` `--delay` `--repeat` `--repeat-delay` `--window` |
| `click` | `--clearmodifiers` `--delay` `--repeat` `--window` |
| `mousemove` | `--clearmodifiers` `--screen` `--sync` `--window` |
| `mousemove_relative` | `--clearmodifiers` `--polar` `--sync` |
| `mousedown`, `mouseup` | `--clearmodifiers` `--window` |
| `exec` | `--args` `--sync` `--terminator` |
| `windowstate` | `--add` `--remove` `--toggle` (last one on the line wins, as upstream) |
| `getwindowgeometry`, `getmouselocation` | `--prefix` `--shell` |
| `behave_screen_edge` | `--delay` `--quiesce` (the command itself is unsupported here, above) |
| `windowmove` | `--relative` `--sync` |
| `windowsize` | `--sync` `--usehints` |
| `windowactivate`, `windowfocus`, `windowmap`, `windowminimize`, `windowunmap` | `--sync` |
| `set_desktop` | `--relative` |
| the other 24 | none |

`--sync` is bounded everywhere it appears: [`--sync` waits are
bounded](#--sync-waits-are-bounded). `search --title` is upstream's deprecated
spelling of `--name` and behaves as one. `scripts/check-docs.py` compares the options in command help with this table.

Desktops map to workspaces (0-based). `windowunmap`/`windowminimize` use the
scratchpad on sway. GNOME has a longer list of documented differences (shell grabs, the
lock screen, `selectwindow`): see **Known limitations on GNOME** in
[gnome/README.md](../gnome/README.md). The per-desktop cells are the support matrix in
the [README](../README.md#desktop-support).

## Keyboard layouts

`key` and `type` inject keycodes, and the compositor reads those keycodes through
whatever XKB layout your session has active, so a fixed US table would type `z` for
`y` on a German layout and skip every accented character. wdotool reads the
compositor's *own* keymap instead (every Wayland client is handed it on
`wl_keyboard.keymap`) and looks the character up backwards: which key, with which
modifiers, produces it here.

```console
$ wdotool type 'Grüße, ça va?'      # de, fr, es, dvorak ... all fine
```

* AltGr (level three) and level five are pressed when the layout needs them, `@` on
  German is AltGr+Q, and wdotool finds out *which key* is AltGr from the keymap, not
  from a guess.
* A character that needs a **dead key** becomes two keystrokes (`é` on German is `´`
  then `e`) and the application composes them, exactly as it does when you type it by
  hand. An accent on its own is the dead key *twice*, and dead key plus space is what
  the Compose table every toolkit ships says it is (`'`, not `´`).
* Characters the active layout genuinely cannot produce warn and skip, one line each,
  and the rest of the string is typed, `ñ` is not on a French keyboard
  (`fr(basic)` has no `dead_tilde` and no `ntilde`), so `type 'ñ'` says so and types
  nothing. **`key` says the same thing about a key name the layout cannot reach**,
  rather than pressing the built-in US table's *position* for it: on a Greek-only
  session `key ctrl+s` warns and presses ctrl alone, because `<AC02>` is σ there and
  Ctrl+σ is not Save. (It used to press it and say nothing, measured in Kate on both
  Plasma versions: nothing saved, exit 0, empty stderr. Configure a Latin layout
  **first**, or switch to it, and the chord lands: the layout wdotool reaches is the
  live group, which on GNOME and KDE is read and elsewhere is assumed to be group 1.
  Measured on sway, which is the elsewhere, against a binding on the US `s` position:
  `us,gr` presses it, `gr,us` warns and presses nothing, and `WDOTOOL_XKB_GROUP` on
  the Latin group presses it whichever order they are in.)
* **When the active layout is plain US, none of this runs.** wdotool checks the
  keymap key by key against its built-in US table and, when they agree, uses the
  built-in table. Keyboard *options* do not spoil that: swapping Caps and Escape, or
  putting the layout switcher on Super+Space, still bypasses. The same fixed table is
  the fallback whenever the keymap cannot be read at all (no compositor, a locked
  screen, an unparsable keymap): a warning on every command that types through it,
  never a failure.
  **Which layout is "active" is the read one where it can be read**, so a `us,de`
  session switched to German stops taking the bypass and runs the reverse map, which
  is the point: it used to bypass and type US characters. A session that really is on
  US still bypasses, whatever else is configured, and `--layout us` bypasses without
  asking anybody anything.

Two things are still on the honest list. **Compose-only characters**: a character the
layout reaches only through a Compose *sequence* that is not a dead-key pair (`ñ` on
German, say) is skipped with the warning above, wdotool composes nothing itself, it
presses keys. And **which of several configured layouts is active**:
`wl_keyboard.modifiers` carries that and every compositor sends it only to the window
with keyboard focus, which an injector never is. **On KDE and on GNOME that no longer
decides anything**, KWin publishes the live layout on the session bus and GNOME keeps
it in a setting the portal serves, and wdotool reads both, below, and on sway the
event arrives anyway. Where nothing answers the keymap is all there is: when it holds
more than one group and they do not bind the same symbols, wdotool uses the **first**
and says so, on every command that types, not once per session, so a session with
two layouts types the first one's characters until you pin the group:

```console
$ WDOTOOL_XKB_GROUP=2 wdotool type 'Grüße'   # the second configured layout
```

The pin still outranks everything, KDE and GNOME included: a script that sets it keeps
saying what it means.

**On GNOME the guess is gone too, because the shell keeps the answer in a setting.**
`org.gnome.desktop.input-sources` holds the sources you configured and the order they
were last used in, and `xdg-desktop-portal` serves it on the session bus, so wdotool
reads it before every `type` and `key`. The head of `mru-sources` is the live source, written on every switch by every means you have, the `Super+Space` shortcut and the
panel indicator's menu alike, and restored at login, which is why the *first* command
of a fresh session could already be typing the wrong characters. (`current` looks like
the answer and is not: `gsettings describe` calls it "deprecated and ignored" on both
generations, the shell never writes it, and setting it switches nothing.) Nothing to
install and no new dependency: `xdg-desktop-portal` and `xdg-desktop-portal-gnome` are
both in the default Ubuntu desktop, and `w11common/dbus_mini.py` speaks the call as it
stands. It costs 1.2 ms a command as you, 6.3 ms as root, where the read has to
happen in a forked child, because the portal answers the session user and nobody
else, and neither is visible next to the rest of a `wdotool type`. It is `Settings.ReadAll`, which is the one portal interface with no consent
step, the same call every GTK application makes for the colour scheme, so
[No authorization dialog](../README.md#no-authorization-dialog) still holds, measured.

The index of that source is the keymap group, with Mutter's own two habits folded in.
It **appends its own `us` group** after your sources, so one German source is the
two-group keymap `pc_de_us_2_inet` and one Greek source is `pc_gr_us_2_inet`, from
the keymap alone a session with one layout and a session with two are the same thing,
which is exactly why the notice used to fire on every command of every non-US GNOME
desktop. The setting tells them apart, so **a one-layout GNOME session now says
nothing at all**. And beyond three sources Mutter compiles the keymap in chunks of
three around the one in use: with `de, fr, gr, ru, es` the keymap is `de, fr, gr, us`
until Spanish is picked and then it is `ru, es, us`, so the group is the source's index
*within its chunk*, and Spanish is group 2. (The guess said group 1 there, Russian, and `type` typed nothing at all.)

Two GNOME states are refused rather than answered, and there the guess and its notice
stand exactly as they did: **per-window layouts** (Settings ▸ Keyboard ▸ *Let each
window use its own layout*), where the session-wide setting describes no window in
particular, measured saying German while a newly opened window was on US, and a
`mru-sources` head naming a layout the source list no longer has. So does an input
source that is an **IBus engine** rather than an `xkb` layout, which is the one shape
here nothing has measured.

A lone `us` source is the case nobody hears from at all, on any desktop: its two groups
bind the same symbols, so there is nothing to choose between and no bus is opened.

**On KDE the guess is gone, because KWin answers the question.** `org.kde.KWin`
`/Layouts` `org.kde.KeyboardLayouts.getLayout` returns the 0-based index of the active
layout in the list you configured, and that list *is* the keymap's group order, name
for name (`getLayoutsList`'s long name is the keymap's `name[GroupN]`), so the active
group is that index plus one, with nothing to translate. Same object, same members,
same meaning on Plasma 6.6 and 5.27, and it follows every way a layout is switched:
the applet, the keyboard shortcut, `setLayout`, `switchToNextLayout`, and per-window
layouts (`SwitchMode=Window`), where it reports the focused window's layout, which is
where the keystrokes are going, so that is the right answer too.

Three rules keep that safe. wdotool asks **only where it would otherwise guess**, so a
plain US session, a one-layout session and GNOME's `us,us` never open a bus at all. It
asks **KWin itself and never kded**: `org.kde.kded6 /modules/keyboard` and the kded5
one declare the same interface and *crash kded* on `getLayout`, measured on both
generations, every call answering `NoReply` with the name changing owner afterwards, so there is no fallback to them and there must never be one. And **every failure keeps
the old behaviour**: no session bus, no KWin, an older KWin without the object, a KWin
that stops answering, an index the keymap cannot hold, each of them leaves group 1
assumed and the notice printed, exactly as before.

Measured end to end in a real Kate window on Plasma 6.6 and 5.27
(`repro/kde-keys-3-live-layout.sh`), with `us, de` configured and German switched on:

```
wdotool type 'yz@'   ->  arrived 'zy""'   (before: group 1 assumed)
wdotool type 'yz@'   ->  arrived 'yz@'    (now: KWin asked)
```

byte-exact on both generations, with no notice on stderr at all, and
`wdotool keys explain` reporting `German -- group 2 of 2, from wayland + kwin`.
Switching layouts under a daemon that is already running is followed command by
command, because the group is re-read on every one. The one-layout case
(`repro/kde-keys-1-group-guess.sh`) is unchanged: plain text, AltGr, dead keys and
chords all arrive byte for byte, as they always did.

Measured end to end in a real `gnome-text-editor` window on GNOME 50.1 and 46.0
(`repro/gnome-keys-1-group-guess.sh`), each case typed once with the build that
assumed the group and once with this one:

```
us, de switched to German     type 'yz@'  ->  'zy"'   assumed,  'yz@'  read
de,fr,gr,ru,es on Spanish     type 'yz'   ->  nothing assumed,  'yz'   read
one de source                 type 'yz@'  ->  'yz@' both, and the notice is gone
```

byte-exact on both generations, with `wdotool keys explain` reporting
`German -- group 2 of 3, from wayland + gnome input-sources` where it used to report
`English (US) -- group 1 of 3 (assumed), from wayland`. The five-source case is the
one where the old guess typed *nothing at all*: it assumed Russian, and `y` and `z`
are not on it. And on a session rebooted with German last used, before anything is
touched, the first command types German, the old build typed `zy"` there too.

**Three more desktops answer it now, and one puts the answer on the wire.** The reader is
picked per desktop and gated the same way, asked only where the keymap cannot settle it,
and every failure leaving group 1 assumed with the notice printed:

* **Hyprland**, `hyprctl devices` (`j/devices` over the IPC socket). Hyprland keeps XKB
  state *per device*, so the reader picks a row: never one of ours
  (`wdotool-virtual-keyboard`, `hl-virtual-keyboard-*`, because reading the injected
  device's index would be reading back the state we set), then `main: true`, then a name
  that looks like a keyboard, then the first row. `active_layout_index + 1`. Nothing is
  ever dispatched. What that gets right and what it does not is the Hyprland section under
  [Backend notes](#backend-notes): the answer describes the *physical* keyboard, which is
  the session's own state and the wrong state for the `/dev/uinput` path.
* **Wayfire**, `wayfire/get-keyboard-state` over the JSON IPC, whose `layout-index` is
  the same 0-based index into the configured list KWin's `getLayout` gives, so the group is
  `index + 1`. It needs `plugins = ipc ipc-rules` and not the window backend's whole set.
  `wayfire/set-keyboard-state` is **never** called and must never be: one call recompiled
  the keymap as the selected layout *duplicated* and the second layout was gone until
  restart.
* **Cinnamon**, `org.cinnamon.desktop.input-sources` through `org.Cinnamon.Eval`, one
  read-only constant program. The schema has `sources`, `current`, `show-all-sources` and
  `xkb-options` and **no `mru-sources`**, so unlike GNOME the live index really is
  `current`, the mapping is `current + 1` with no chunking (muffin appends no group of its
  own), and there is no portal call and no forked child, because Eval identifies nobody.
* **COSMIC** needs no reader at all: cosmic-comp publishes
  `zcosmic_keyboard_layout_manager_v1`, whose `group` event the protocol says is "received
  even when the client has no focused window", so the group arrives on the wire the way
  sway's does and `wdotool keys explain` says `from wayland`, not `from wayland + <who>`.

So `wdotool keys explain`'s source line now has five spellings after `wayland + `:
`kwin`, `gnome input-sources`, `hyprland devices`, `wayfire` and `cinnamon input-sources`.
Xfce-on-Wayland, labwc, river and Budgie are the sessions left guessing, and they are left
guessing because nothing in them publishes the answer.

The measured engineering behind all of it, the reverse map, the US bypass, the group
guess, the cache, is [the active layout](#the-input-daemon), below.

### Forcing the layout

`--layout` says which character table the typing commands use, ahead of everything
else. It goes before the command, and it is ours, not xdotool's:

```console
$ wdotool --layout us type 'hello'    # the built-in US table, no questions asked
$ wdotool --layout xkb type 'Grüße'   # the compositor's keymap, even on US
$ wdotool --layout auto type 'hello'  # the default: decide per session
```

`--layout us` is the one that promises something. It does not read the compositor's
keymap and it does not run the "is this plain US?" check either, so **no layout code
executes at all**. Use it when you know your keyboard is US and you would rather the
tool did not look, or to rule the layout machinery out while diagnosing something
else. `--layout fixed` is a synonym, and both spellings are named in the
invalid-argument message.

The option beats the variables below, which matters because those are read by the
daemon and a daemon may already be running with different ones.

| variable | effect |
|---|---|
| `WDOTOOL_LAYOUT=auto` | the default: the compositor's keymap, unless it is plain US |
| `WDOTOOL_LAYOUT=us` | never read the keymap; use the built-in US table |
| `WDOTOOL_LAYOUT=xkb` | use the compositor's keymap even if it looks like US |
| `WDOTOOL_XKB_GROUP=<n>` | pin the active layout group (1 = the first one) |
| `WDOTOOL_XKB_KEYMAP=<file>` | read the keymap from a file instead of the compositor |

`WDOTOOL_XKB_GROUP` is read by the **command**, which sends the pin to the daemon with
the text it is typing, exactly as `--layout` does: it is the one the notice above tells
people to set, and telling them to set a variable a running daemon then ignores was
worse than saying nothing (measured: the same command typed `zy` with a daemon up and
`yz` with none). The other three are read by the *daemon*, which keeps the environment
it was started with, so set those before the first wdotool command of a session, or
stop the running daemon first. Changing your **layout** needs none of that: the keymap
and the active group are re-read on every single command, so a long-running daemon
follows a layout switch by itself.

`wdotool __keymap` is a hidden diagnostic that prints what the compositor actually
sent; `--info` summarises it (groups, active group, whether the US bypass takes it),
`--chars STRING` shows the keystrokes each character would need, `--group N` reverses
a group other than the active one, and `--keymap PATH` reads a keymap from a file.
For "what do I press for this character?" the documented command is
`wdotool keys explain`, below.

<a id="typing-and-clicking-with-no-privilege---vkbd"></a>

## Typing and clicking with no privilege (`--vkbd`)

There is a Wayland protocol built for exactly the problem the reverse map solves the
hard way: `zwp_virtual_keyboard_v1` lets a client upload **its own** keymap and send
keycodes against it, so no reverse lookup is needed, the keymap that reads our
keycodes is the one we just uploaded. And there is a second one for the other half:
`zwp_virtual_keyboard_v1` has four requests (keymap, key, modifiers, destroy) and not
one of them is a pointer, so wlroots ships `zwlr_virtual_pointer_v1` beside it, absolute and relative motion, buttons and scroll, equally unprivileged. wdotool uses
both, under one policy and one flag.

**Who has them, and what that means for privileges.** Measured on every desktop this
branch is tested on, as the session user and as root:

| session | `zwp_virtual_keyboard_v1` | `zwlr_virtual_pointer_v1` | what injects | typing needs | pointer (`click`, `mousemove`, `mousedown/up`) needs |
|---|---|---|---|---|---|
| **sway 1.11 / wlroots** | **yes**, v1, advertised to every client and restricted to none | **yes**, v2, likewise | `/dev/uinput` when it can be opened, the protocol when it cannot | **nothing**, no root, no group, no udev rule | **nothing** either |
| **Hyprland 0.53 / 0.56** | **yes** | **yes** | the same | **nothing** | **nothing** |
| **Wayfire 0.10** | **yes**, measured typing byte-exact on a box with no `/dev/uinput` at all | **yes** | the same | **nothing** | **nothing** |
| **labwc 0.9.3, and Budgie / Xfce / LXQt on it; river 0.4** | **yes** | **yes** | the same | **nothing** | **nothing** |
| **COSMIC 1.6 / 1.7 (cosmic-comp)** | **yes**, one of 53 globals | **no**, and it is the only member of the family without it | the protocol for the keyboard, `/dev/uinput` for the pointer | **nothing** | root, or the udev rule |
| **GNOME 46 / 50 (Mutter)** | no | no | `/dev/uinput`, always | root, or the udev rule | root, or the udev rule |
| **Plasma 5.27 / 6.6 (KWin)** | no, the interface is in no Plasma library, and 5.27 does not advertise it either | no | `/dev/uinput`, always | root, or the udev rule | root, or the udev rule |
| **Cinnamon 6.4 (muffin)** | no. Muffin master, the 6.6/6.8 line, adds `virtual-keyboard-unstable-v1`, so a future Cinnamon gets the keyboard input path | no, and master does not add one | `/dev/uinput`, always | root, or the udev rule | root, or the udev rule |
| X11 (Xfce, MATE, i3, LXQt, Cinnamon, GNOME/KDE on Xorg) | not applicable, wdotool hands over to the real `xdotool` | not applicable | X | nothing | nothing |

Forcing `--vkbd on` where `zwlr_virtual_pointer_manager_v1` is absent, Mutter, KWin,
cosmic-comp, is refused, and the refusal ends `; not yet forced here, and the route is the
kernel devices `--vkbd auto` takes on the same session (AGENTS.md route 4), at the cost of
access to /dev/uinput`. This limitation applies only to **forced** mode: `auto` already clicks
through the kernel devices on those sessions, so nothing about the compositor changed.

The COSMIC split is measured on a live session rather than read off a registry dump:
`wdotool type 'hello cosmic'` landed byte-exact with no `/dev/uinput` node on the box at
all, while `wdotool mousemove 400 300` answered `cannot create uinput devices`. The two
refusals name that split rather than a protocol's absence, so a COSMIC user is told which
half of their session already works.

So on sway **every injecting command** wdotool has, `key`, `keydown`, `keyup`,
`type`, `click`, `mousedown`, `mouseup`, `mousemove`, `mousemove_relative`, needs
**no privilege whatsoever**, where before it needed root because `/dev/uinput` there
is `crw------- root root` with no `uaccess` ACL. `search`, the window commands,
`getdisplaygeometry` and `getactivewindow` never needed any. On GNOME and KDE nothing
changes at all, and the reverse map stays exactly where it is for both.

```console
$ wdotool type 'hello'              # as a plain user on sway: works, no root
$ wdotool click 1                   # ... and so does this
$ wdotool mousemove 2560 360        # ... exactly, on any head of any layout
$ wdotool --vkbd on click 1         # force the protocols (error if absent)
$ wdotool --vkbd off click 1        # force /dev/uinput, whatever is offered
```

`--vkbd` is one switch for one decision and it covers both input paths: `--vkbd on type x`
and `--vkbd on click 1` each ask for the protocol wdotool would use for that command.

| | |
|---|---|
| `--vkbd auto` | the default: `/dev/uinput`, and a protocol only where there is no usable kernel device |
| `--vkbd on` | always the protocol; a compositor that does not implement it is an error, never a silent fallback |
| `--vkbd off` | always `/dev/uinput`, including its "run it as root" error |
| `WDOTOOL_VKBD=auto\|on\|off` | the same, for the daemon; the flag beats it |

Held keys and held buttons survive across commands, because the daemon holds one
connection and one object of each kind for its life, a compositor releases whatever
a client was holding the moment that client disconnects. A hold cannot move between
the two paths (only the device that pressed a key can release it), so forcing a
different `--vkbd` mid-hold releases it on the sink that has it and says so:

```console
$ wdotool keyup shift
wdotool: the compositor restarted; the keys wdotool was holding on its virtual keyboard were released with it
$ wdotool --vkbd off type A
wdotool: the keys wdotool was holding on the virtual keyboard (shift) were released: this command types through the kernel one, and only the device that pressed a key can release it
```

The rest of this section is the measurement: what each protocol does, what it costs,
and every defect the live sessions found.
- **the virtual-keyboard path (`vkbd.py`, `us_keymap.py`)**: the second
  injection path, `zwp_virtual_keyboard_v1`, the client uploads its OWN
  keymap and sends keycodes against it, so no reverse lookup is needed. All of
  the below is measured on sway 1.11/wlroots and Plasma/KWin 6.6.6, one VM at
  a time.
  - **who has it**: sway/wlroots, v1, advertised to every client and
    restricted to none, an ordinary uid-1000 client with no group and no
    device access creates a keyboard and types, in ~2.5 ms against ~600 ms of
    uinput hotplug. **KWin 6.6.6 does not implement it at all** (the string is
    in no Plasma library; the same 62 globals go to root and to the session
    user), and neither does Mutter. The earlier claim in this file that "sway
    and KWin *do* implement" it was wrong: the reverse map stays for KDE as
    well as GNOME.
  - **THE POLICY, in one sentence, and it is one policy for both input paths**:
    `key`/`keydown`/`keyup`/`type` go through `zwp_virtual_keyboard_v1`, and
    `click`/`mousedown`/`mouseup`/`mousemove`/`mousemove_relative` through
    `zwlr_virtual_pointer_v1`, when the matching kernel device cannot be
    opened *and* the compositor implements that protocol, through
    `/dev/uinput` in every other case, with `--vkbd on|off` (`WDOTOOL_VKBD`)
    forcing either, for both input paths. Deliberately that narrow.
    Where uinput works it keeps working byte for byte, the daemon tests pin
    that event stream, and the protocol is not free where it exists: the
    compositor hands the focused client OUR keymap ahead of our first key and
    the session's keymap back when the real keyboard is next used, so every
    injection makes that application recompile its keymap twice. What it does
    buy is the case uinput cannot serve at all, `/dev/uinput` is
    `crw------- root root` with no `uaccess` ACL on stock wlroots, so a
    non-root user cannot type today, and turning a hard failure into the
    right characters is the one change that is strictly better.
  - **what still needs privilege**: nothing wdotool injects, on wlroots. This
    protocol has four requests (keymap, key, modifiers, destroy) and no
    pointer, no buttons, no scroll, the pointer input path is a second protocol,
    `zwlr_virtual_pointer_v1`, and it has its own section below. The window
    commands, `search` and `getdisplaygeometry` never needed privilege.
  - **the keymap** is captured, never generated: `us_keymap.TEXT` is
    `tests/fixtures/keymaps/us.xkb` byte for byte, uploaded verbatim with the
    trailing NUL that `size` counts. A keymap synthesised from
    `include "complete"` plus our own symbols *compiles*, the focused client
    gets it back under our group name, and then delivers no key events at
    all, twice, unexplained. Uploading plain US is also what makes
    `keymap.CHAR_TO_KEY` right by construction rather than by luck, and that
    is asserted, not assumed: `tests/test_vkbd.py` runs
    `xkbmap.active_group_is_plain_us()`, the same key-by-key check the uinput
    path uses before it trusts the fixed table, over the text we upload. The
    price of plain US is its character set: on a German session the reverse
    map reaches `ü` and this path warns and skips it, exactly as it does for
    any character the active layout cannot produce. Generating a keymap
    holding precisely the characters asked for would fix that and is blocked
    on the unexplained "compiles but delivers nothing" above; it is the first
    thing to chase.
  - **modifiers are the real difference**. wlroots does not run a virtual
    keyboard's keys through xkb state: evdev 42 down, `y`, 42 up types `y`,
    and a focused observer sees the 42 events with no `modifiers` event at
    all. So `VirtualKeyboard.key()` maps a modifier *keycode* to that keymap's
    own modifier bit (`_MOD_BITS`, checked against the file's `modifier_map`
    by the tests) and sends `modifiers` before a press and after a release,
    only when the mask really moved. The callers are unchanged.
  - **`--layout` does not apply here** and says so once: the keymap reading
    our keycodes is ours, so a table built from the session's keymap would
    type garbage. `--layout us` is what this path already does; `--vkbd off`
    is the way to ask for the session's keymap. The bypass, the reverse map
    and the group guess are not consulted at all.
  - **`--clearmodifiers` is complete here**, unlike on uinput: modifier state
    is per device (measured both ways, with a real keyboard holding shift,
    uinput typed `Y` and the virtual keyboard typed `y`), so we release the
    keycodes we hold and send `modifiers(0,0,0,0)`, and the foreign-modifier
    warning, true and unavoidable on the kernel path, is not emitted,
    because there is nothing foreign to clear. The LOCKED mask is deliberately
    left alone: CapsLock and NumLock are not held modifiers on the kernel path
    either (neither is in `keymap.MODIFIER_KEYCODES`), and one flag may not
    mean two things.
  - **one connection, one keyboard, for the daemon's life**: the compositor
    releases whatever a client holds when it disconnects (`keydown y` from a
    process that exits gave one `y` and no repeat), so a held key across two
    commands only works inside one connection. Nothing survives a compositor
    restart: the object, the uploaded keymap and the held keys all go. The
    connection is checked (one `wl_display.sync`) before each command uses it,
    so a restart costs the *hold* and not the command: the daemon reconnects,
    re-creates, re-uploads, types, and says one line about the keys it can no
    longer claim to hold. Measured across a real `pkill -x sway` and re-login:
    the daemon survives, `keyup shift` afterwards is that one line, and the
    next `type A` gives `A`. It did not before, the failing key-up had
    already taken the key out of the object's own `held`, the drop trusted
    that, and shift stayed down in the daemon's model for good, with every
    later `type A` arriving as `a`.
  - **a hold does not move between the two paths**, and `self.down` is one set
    describing two devices, which is the trap: a key can only be released by
    the device that pressed it, so forcing `--vkbd` differently between two
    commands of one daemon releases what the other sink holds, on that sink,
    and says so (`_own_sink()`). Before that, `--vkbd on keydown shift` then
    `--vkbd off type A` typed `a` on a live sway session, the kernel path
    found shift in `self.down`, believed it held, and pressed nothing, and
    the virtual shift was stuck for the daemon's life. Modifier state is per
    device in both directions, measured with two daemons at once: a
    kernel-held shift does not reach protocol keys, our `modifiers(0,0,0,0)`
    does not clear the kernel device's shift, and a CapsLock we lock applies
    to our own keys alone.
  - **same reach as uinput, including the lock screen**: measured, with
    `swaylock` holding an `ext_session_lock_v1`, an *unprivileged* client
    typed the account password and Return through the protocol and swaylock
    unlocked, while the terminal underneath received nothing. Root through
    uinput does exactly the same. Not a new hole (sway advertises the protocol
    to every client of the socket, so anything that can open it could already
    do this), but worth saying out loud.
  - `wdotool __keymap` (hidden, like `__daemon`) dumps the compositor's keymap,
    `--info` summarises it and says whether the bypass takes it, `--chars STR`
    prints the keystrokes each character would need. The test fixtures in
    `tests/fixtures/keymaps/` were captured with it. It **stays**: dumping the
    raw keymap is a bug-report tool, not a user command, and the fixture
    capture in `tests/fixtures/keymaps/README.md` is written in terms of it.
- **the virtual-pointer path (`vptr.py`)**: the pointer's half of the same
  policy, `zwlr_virtual_pointer_v1`, motion, buttons and scroll with no
  kernel device. Everything below was measured on sway 1.11 / wlroots 0.19.2
  against a three-head layout (one head at a negative origin, one at scale
  1.5), with a transparent `zwlr_layer_shell_v1` overlay on every output
  reporting the cursor's position as the compositor's own statement of it.
  - **who has it**: sway/wlroots, advertised at **v2** to every client and
    restricted to none, an ordinary session user creates a pointer and drives
    it, and root reaching the same socket gets the identical registry. Mutter
    and KWin have neither protocol. sway lists ours as
    `0:0:wlr_virtual_pointer_v1`, type pointer, with an **empty libinput
    configuration**, appearing and vanishing with the client.
  - **we bind v1 deliberately.** v2's only addition is
    `create_virtual_pointer_with_output`, which maps the coordinate space into
    one output's logical box **and confines the cursor to it**, measured, a
    relative motion of +4000 from the leftmost head stopped at that head's own
    right edge. Every wdotool coordinate is a layout coordinate, so the plain
    constructor is the one we want. The `seat` argument is allow-null and a
    NULL was accepted; we still pass the seat when there is one.
  - **the coordinate map is the identity, and exact.**
    `motion_absolute(time, x, y, x_extent, y_extent)` is a **ratio**, not
    pixels: extents of 2, 100, 65535 and 1000000 all landed the same point.
    Created without an output it addresses the **whole layout bounding box in
    logical coordinates**, so `x = target − bbox_x` with `x_extent = bbox_w`, 14 of 14 targets landed with **0.000** error, bbox corners, the
    negative-origin head and the 1.5-scaled head included. There is no
    quantisation, so B7's ceiling map and B2's unchanged-EV_ABS nudge have no
    counterpart and no way to reintroduce the off-by-one they fix. Guards:
    `x_extent == 0` is a silent no-op, so the extents are floored at 1;
    `x >= x_extent` clamps just inside the right edge; a coordinate in a hole
    between outputs clamps to the nearest edge, which is the compositor's
    business and not the map's. The layout box comes from `_wayland_bbox()`
    unchanged, and it has to be the `zxdg_output_v1` one: `wl_output.scale`
    reported **2** for the 1.5-scaled head, and the wl_output-only fallback
    would put the layout's right edge 320px out.
  - **relative motion cannot be accelerated here.** A virtual pointer is not a
    libinput device (the empty config above), so `pointer_accel` and
    `accel_profile` cannot apply to it on any wlroots compositor: 1, 10, 100,
    500 and 1000 px each moved exactly that far, and 500 separate one-pixel
    motions moved exactly 500. On the same seat a `/dev/uinput` mouse asked
    for 500 units of REL_X moved the cursor 858.33, which is B1. So the
    protocol path always sends `motion` and never the warp B1 introduced
    (`_rel_absolute(virtual=True)`), unless `WDOTOOL_REL_MODE` says otherwise;
    it is exact, and it needs no position model, so it is right even on the
    first command of a daemon that has never been told where the cursor is.
  - **buttons and scroll**: `button()` takes raw evdev codes and does not
    validate them; all eight wdotool buttons arrive verbatim (0x110..0x117, the numbers `_BTN` already uses for the kernel device). Button state is
    **refcounted per seat**: press, press then release leaves the button DOWN,
    where the kernel drops the duplicate and the release lands. So the daemon
    presses only what it is not holding and releases only what it is, on
    **both** paths, which is what makes them behave identically and keeps
    `self.btns` honest. Scroll uses `axis_discrete` (a wheel click is a notch:
    the client sees `axis_value120` = 120), with `axis_source(wheel)` ahead of
    it, and **Wayland's sign, not evdev's**, positive vertical is scroll
    *down*, so buttons 4/5/6/7 map to axis 0 −, axis 0 +, axis 1 −, axis 1 +,
    the mirror of `_WHEEL`. Every request group ends in `frame()`: motion
    applies without one but the client then never sees `wl_pointer.frame`, and
    an axis is **not delivered at all** until a frame.
  - **`getmouselocation` is refused, not guessed.** The protocol has no events, zero arrive on the object across motion, buttons and axes, sway's IPC
    carries no cursor position, and Xwayland only knows the pointer while it
    is over an X surface (it answered a frozen 1600,540 for all eight test
    positions). So the daemon answers with the position it put the pointer at,
    which on this path is exact, and raises `POINTER_UNKNOWN` when it has put
    it nowhere. `ext-image-copy-capture-v1`'s pointer cursor session *can*
    answer it, `create_pointer_cursor_session(output, wl_pointer)` delivers
    `position` in that output's device pixels, advertised unprivileged here, and is the route if this ever has to be answered in general; it needs a
    session per output and only the one that reported `enter` is
    authoritative, which is why it is not in this change.
  - **one connection, one pointer, for the daemon's life**, exactly as for the
    keyboard: a held button does not survive the client disconnecting (the
    release was delivered the instant the holder exited), `destroy` releases
    too, and nothing survives a compositor restart, which the client learns
    only on its next write, as a `BrokenPipeError`. The connection is checked
    (one `wl_display.sync`) before each command uses it, so a restart costs
    the *hold* and not the command. `_drop_vptr()` clears `self.btns` rather
    than trusting `vp.held`, for the reason `_drop_vkbd()` spells out: the
    write that failed is the one that took the button out of `held`.
  - **a hold does not move between the two paths**, `_own_pointer()` is
    `_own_sink()` for buttons, and exists for the same defect: `--vkbd on
    mousedown 1` then `--vkbd off mouseup 1` would otherwise leave a button
    down for the daemon's life, released by nobody, turning every later click
    into a drag. It is released on the pointer that holds it and said out
    loud, **and also when the sink the command named is not there**, which
    is the ordinary case on a stock wlroots box, where `--vkbd off` asks for
    a `/dev/uinput` nobody can open. Both `_own_sink()` and `_own_pointer()`
    sat *below* the `raise` in `_pick_keyboard()`/`_pick_pointer()`, so on
    the one kind of session either protocol exists for, the release never ran
    and the button stayed down: measured on sway 1.11, `--vkbd on mousedown
    1` then `--vkbd off mouseup 1` reported the uinput error and left a drag
    behind it. `_release_named_sink()` runs them on the way out of the
    failure, for **both** halves, because `--vkbd` is one switch and one
    failed command can strand one of each, and the command still fails with
    the sink's own error, because a forced mode must not quietly fall back.
    It fires only for a *named* sink: `auto` failing means neither sink
    exists, so there is nothing left that could release anything. The line it
    prints reaches the user because `serve_client()` now puts a failing
    command's warnings on the error reply (and `op_type`/`op_key` take the
    caller's list rather than a local one); without that, the one sentence
    describing a state change nobody asked for was collected and dropped.
    The keyboard and the pointer are **two connections and two objects** on
    purpose: a disconnect releases only what that connection holds, so one
    half's troubles cannot drop the other's.
  - **`--clearmodifiers` on a pointer command** clears on whichever *keyboard*
    sink holds our modifiers (a key-up on one device releases nothing the
    other holds), and when there is no keyboard of either kind it says so once
    and lets the pointer command through rather than failing it. The
    foreign-modifier warning stays on this path: modifier state reaches the
    seat from the seat's keyboards, so a shift held on a real one rides our
    click whichever device sends it.

## Which key was that? `wdotool keys`

`wdotool keys` is the layout machinery pointed the other way: what to press for a
character, or what you just pressed.

```
wdotool keys explain 'ç'   what to press, without touching the keyboard
wdotool keys watch         one line per key event, as you type
```

`keys` is ours, not xdotool's. It is a standalone command rather than a link in a
command chain, and it is not in `help`, that output stays byte-for-byte the real
xdotool's.

**`explain` needs no privilege and opens no device.** The keymap arrives on
`wl_keyboard.keymap`, which every Wayland client is handed, so it needs no root and
opens nothing under `/dev/input`. It follows the same layout rules as `type`
(`WDOTOOL_LAYOUT`, `WDOTOOL_XKB_GROUP`, the US bypass), so what it prints is what
`type` sends.

```console
$ wdotool keys explain 'ç'
layout: German -- group 1 of 2, from wayland + gnome input-sources
level keys: shift = key 42 <LFSH>   level3 = key 100 <RALT>   level5 = key 195 <LVL5>   (what wdotool presses)
'ç' -- 2 presses on German (a dead-key pair: two presses in order, not a chord)
    1. press key 13 <AE12> with level3 (key 100 <RALT>, ISO_Level3_Shift) -> dead_cedilla
    2. press key 46 <AB03> -> 'c'
    wdotool key 108+21 key 54                  (keycodes)
    wdotool type 'ç'                           (characters)
```

The first line is the layout question above, answered for this session: `(assumed)`
appears only where the group really was a guess, and the source says how it was
settled, `wayland` is the keymap alone, `wayland + kwin` is KWin having been asked on
KDE, `wayland + gnome input-sources` is GNOME's setting having been read (the example
above is a one-layout German GNOME session, where the guess said `group 1 of 2
(assumed)`), and on sway the group arrives on the wire and the marker is gone too.

That is the awkward case in full: a dead key that is itself on the third level. `ç`
on a German keyboard is AltGr held down across the `´` key, both let go of, and
*then* `c`, two presses in order, not one chord, and which of the two it is changes
the events completely. An argument that is a keysym name (`Return`, `EuroSign`) is
taken as one; anything else is taken character by character, and `--chars`/`--keysym`
say which explicitly. A character this layout cannot produce says so, names the
layout, and makes the exit status 1.

**`watch` needs root**, because it reads `/dev/input/event*`. It is the same question
asked by pressing the key:

```console
# wdotool keys watch
layout: German -- group 1 of 2, from wayland + gnome input-sources
level keys: shift = key 42 <LFSH>   level3 = key 100 <RALT>   level5 = key 195 <LVL5>   (what wdotool presses)
watching 2 keyboards, ignoring 1 of our own; codes are evdev keycodes, the replay column uses X keycodes (evdev+8). Ctrl-C to stop.
    TIME EV     CODE KEY      MODIFIERS       PRODUCES         REPLAY (keycodes)          CHARACTER (portable)
   0.000 down   100 <RALT>   -               ISO_Level3_Shift wdotool keydown 108        wdotool keydown ISO_Level3_Shift
   0.090 down    13 <AE12>   level3          dead_cedilla     wdotool key 108+21         wdotool key dead_cedilla
   0.150 up      13 <AE12>   level3          dead_cedilla     wdotool keyup 21           wdotool keyup dead_acute
   0.210 up     100 <RALT>   -               ISO_Level3_Shift wdotool keyup 108          wdotool keyup ISO_Level3_Shift
= chord     | wdotool key 108+21 | wdotool key dead_cedilla
   0.440 down    46 <AB03>   -               'c'              wdotool key 54             wdotool type 'c'
   0.500 up      46 <AB03>   -               'c'              wdotool keyup 54           wdotool keyup c
= chord     | wdotool key 54 | wdotool type 'c'
= dead pair | wdotool key 108+21 key 54 | wdotool type 'ç' | two presses in order, not a chord
```

That is the same `ç`, typed rather than looked up, and the two presses are plainly two
presses, because the `´` key is released before `c` is touched. The table goes to
**stdout** and everything else to stderr, so `wdotool keys watch --count 4 > keys.log`
is a usable recording; Ctrl-C exits 0.

| option | |
|---|---|
| `--count N` | stop after N key events, for scripting |
| `--raw` | unfiltered evdev event lines instead of the table (autorepeat, `EV_SYN`, every device) |
| `--group N` | read group N of the keymap instead of the active one |
| `--keymap FILE` | read the keymap from a file instead of the compositor |

The contract behind those two modes:
- **`wdotool keys watch|explain` (`keys_cmds.py`)**: the same two tables
  pointed at the user. `explain` is the documented front door that `__keymap
  --chars` was the sketch of; `watch` is new.
  - **Spelling.** One command, two modes, `keys watch` and `keys explain`, routed in `cli.main()` next to `__daemon`/`__keymap`, **before** the X11
    passthrough and **not** in `commands.REGISTRY`. Three reasons, all forced:
    `help` is byte-compatible with the real xdotool's and prints the registry,
    so a registered command would break `tests/test_cli_parity.py`; xdotool has
    no `keys`, so there is nothing to hand a passthrough over to; and the
    registry is also what script-mode detection consults, where, exactly as
    for the 48 built-ins, a command name has to beat a file of the same name.
    Nothing existing changes spelling or behaviour: `keys` is not a chainable
    command (`wdotool sleep 0 keys watch` still says "Unknown command"), which
    is the same deal `__keymap` has.
  - **Two reproductions, always both.** A keycode replay (`wdotool key 108+21`,
    X keycodes = evdev + 8) is exact and meaningless under any other layout; a
    character form (`wdotool type 'ç'`) is portable and may press different
    keys. Printing one would be a trap either way, so every line prints both.
  - **Chord versus sequence** is watch mode's whole job. A *run* is the span
    from "nothing held" to "nothing held". It is renderable as a chord only if
    every press precedes every release, at most one non-modifier key is
    involved, and that key is pressed last. Otherwise the literal
    `keydown …/keyup …` sequence is printed with the reason, two keys held at
    once, released out of order, a modifier pressed after the key, because a
    chord would change what the application sees. Two consecutive runs whose
    keysyms compose (a dead key, then a base letter) also get a `= dead pair`
    line: that is the case that looks like a chord written down and is not.
  - **Which key carried level three** is read from the *event*, not assumed:
    the modifier tags come from the keysym the active keymap binds to the
    keycode that was actually pressed (`<RALT>` on German, `<CAPS>` on Neo, a
    dedicated `<LVL3>` elsewhere), and the header separately reports the key
    wdotool itself would press, which is often a different one.
  - **Privilege.** `watch` reads `/dev/input/event*`, which is `root:input`
    with no ACL on every session measured (`keystate.py`) and which no udev
    rule tags, this repo's rule tags `/dev/uinput`, the injecting half, so it
    needs root, and says which case the machine is in (unreadable nodes, no
    nodes, only ours) and exits 1 rather than failing obscurely. It never
    `EVIOCGRAB`s: the compositor keeps every key and stopping leaves nothing
    behind. `explain` needs **no** privilege and opens no device, the keymap
    comes off `wl_keyboard.keymap`, and it obeys the typing path's layout
    rules exactly (`Layout.load()` mirrors `daemon._layout`), so what it prints
    is what `type` would send, US bypass included.
  - **Our own devices** are excluded by the `wdotool ` device-name prefix
    (`keystate.OWN_NAME_PREFIX`): the daemon's uinput nodes belong to another
    process, so its fd-based `UI_GET_SYSNAME` exclusion is not available here
    and the name is the reliable cross-process test. A concurrent injection is
    therefore never recorded.
  - **A live stream is not a recorded one.** Three shapes only a real session
    produces, each of which used to print something wrong: a release with no
    press (the Enter that started the command, it ended the session with an
    `IndexError`, and is now a line of its own); several keyboards, whose
    rounds are merged by the kernel timestamp so the seat's shared modifier
    state renders as one chord instead of two runs in the wrong order; and a
    keyboard unplugged holding a key, whose keys are released as the kernel
    releases them, or the run never closes again and every later line carries
    a modifier nobody holds. `EV_KEY` from `BTN_MISC` up is a button, not a
    key, and has no X keycode to replay; the table skips it and `--raw` keeps
    it. A keycode token is zero-padded when the number is also a keysym name
    (`key 9` is the digit nine, `key 09` is Escape).
  - Devices appearing and disappearing are handled on a 1 s rescan; a read that
    answers `EAGAIN` is a spurious wakeup, not a lost keyboard. Ctrl-C exits 0.
    `--count N` stops after N key events (scripting), `--raw` prints unfiltered
    evdev lines. The table is stdout, everything else stderr.

## Session readiness and exit codes

`wdotool` separates "there is no session to talk to" from "the session is fine and
nothing matched", so a cron job or a boot script can poll for a desktop without
guessing:

| rc | meaning |
|---|---|
| 0 | the command did what it says |
| 1 | the session is up and the command failed, no matching window, no active window, a wait that timed out |
| 2 | **no Wayland session found**: no compositor, no session bus, GNOME Shell absent, the screen locked, the greeter, or the bridge extension not running |
| 130 | interrupted (Ctrl-C) |

```sh
# wait for a usable desktop, then act
until wdotool getdisplaygeometry >/dev/null 2>&1; do sleep 2; done
```

`getdisplaygeometry` is the cheapest probe: it needs no window and no `/dev/uinput`.
It never invents a size, with no compositor reachable it warns and exits 2 rather
than printing a made-up `1920 1080`.

In the code: `CmdError.exit_code` is 1. `NoSessionError` (`ctx.py`) carries 2.
`cli.run_chain` returns `exit_code`. Ctrl-C is 130 (128 + SIGINT).

All six tools in this repo share one more rule, and it is
[Technical.md § stdio](Technical.md#7-detached-children-runtime-paths-and-stdio)
in full: whatever the command decided, the *last* thing every `main()` does is
`stdio.flush_stdout(prog)`, so output that never reached its reader makes the status
1, a full disk, a quota, `>/dev/full`, while a reader that closed a pipe is silent,
as the originals are. No tool prints a traceback and none exits 120. That holds
when stderr has gone with it (`wdotool help >/dev/full 2>&1`): the diagnostic is
dropped rather than raised, and the status is all that is left to say it.

## `--sync` waits are bounded

Every `--sync` wait (`windowactivate`, `windowfocus`, `windowmap`, `windowunmap`,
`windowminimize`, `windowmove`, `windowsize`) gives up after 10 seconds with
`wdotool: gave up waiting for ...` and rc 1. Set `WDOTOOL_SYNC_TIMEOUT` (seconds; `0`
waits for ever) to change it.

`window_cmds._wait_until` polls with that deadline. xdotool's own waits are bounded
too (`xdo.c` loops `MAX_TRIES` = 500 x 30ms = 15s) but then return success silently;
we prefer a diagnosis. The one exception is `search --sync`, which blocks until there
are results, that is what its manpage entry promises, and it is how scripts wait for
an application they have just launched.

`windowsize --sync` additionally accepts a size the compositor has *snapped* to.
Mutter honours the client's resize increments, so an xterm asked for 497x392 stays at
496x392 and "the size changed" is never true; the wait ends when the size changed at
all (xdotool's rule) **or** the current size is within `_SNAP_TOL` (32px, about one
character cell) of the request.

## Pointer accuracy

`mousemove` and `mousemove_relative` are pixel-exact **in the layout's own
coordinates**, with one pixel per output on KDE that is the compositor's and not
ours: the target is emitted as an absolute position on a virtual tablet mapped across
the whole output layout, so neither pointer acceleration nor an already-identical
coordinate can lose the move. On sway/i3, relative moves keep using relative events
(that rig runs `pointer_accel 0`); `WDOTOOL_REL_MODE=abs|rel` forces either mode
anywhere.

Where the [pointer protocol](#typing-and-clicking-with-no-privilege---vkbd) is used
instead of the tablet, it is exact rather than merely pixel-exact: the two paths were
measured against each other on the same three-head **wlroots** rig, one head at a
negative origin and one at scale 1.5, and every target landed on the same pixel, the
protocol path with 0.000 error, the tablet path within its own 1/32768-of-the-layout
axis step. The arithmetic on both sides is `abs scaling (B7)` and `the coordinate map`
in [the input daemon](#the-input-daemon). That rig's shape is a wlroots shape: **KWin
refuses a negative position for an enabled output** on both 5.27 and 6.6 (`Position of
enabled output %1 is negative`), so a monitor at a negative origin cannot be arranged
on KDE at all, and asking `wxrandr` for one shifts the whole layout instead.

### On KDE, the top-left pixel of an output is KWin's

Measured on Plasma 6.6 (kwin 6.6.6, Ubuntu 26.04) and 5.27 (Ubuntu 24.04), three
1920x1080 heads, in three layouts, a plain row, a row with a 1920px hole in it, and
one head at scale 1.5 beside one dropped 200px down, with every corner and centre of
every output asked for and read back from KWin's own `workspace.cursorPos`, plus 150
swept x values at the two ends of the layout. Everything landed on the pixel asked
for, including all 150 sweeps, **except an output's top-left pixel**: KWin puts a 1x1
screen edge there and pushes the cursor back, so `mousemove 0 0` reads `1,1`. On 6.6
that is every output's corner; on 5.27 only a corner no neighbour shares.

It is not the coordinate map, and nothing on this side can reach it: a
`mousemove_relative` walk into the corner in REL mode stops at `1,1` as well and only
reaches `0,0` on a second attempt, and disabling the hot corner
(`Effect-overview BorderActivate=8`) stops the Overview opening but not the push-back.
**On a stock Plasma 6, `wdotool mousemove 0 0` opens the Overview**, so a script that
parks the pointer at the origin takes the whole desktop with it: park it at `1,1`.
A target that lands in a hole in the layout snaps to the nearest output, which is
KWin's business too. `repro/kde-6-pointer-3head.py` is the measurement.

KDE's per-device output mapping does not enter into it, which is worth knowing
because it is what the complaint threads reach for: KWin sees the tablet as
`wdotool virtual tablet` and will accept an `OutputName` for it, in `kcminputrc` or
live over D-Bus, but applies that mapping to touch and tablet-tool devices and not to
absolute pointer motion. Pinning it to each head in turn changed nothing, on either
generation, every target still exact. Writing the property does make KWin persist an
`OutputUuid=` line under our device's name in the user's `kcminputrc`.

### Which pixels the coordinates are in, under scaling

They are the compositor's **layout** coordinates: what `wxrandr --query` prints and
what `getdisplaygeometry` reports, which on a scaled desktop are logical pixels, not
device ones. A 1920x1080 head at 200% is a 960x540 desktop, and `mousemove 960 540`
is its bottom-right corner. Measured at scale 1, 1.5 and 2, on one head and on two
heads of different scales, on GNOME 50, GNOME 46 and Plasma 6.6, against the KMS
cursor plane on the scanout (device pixels, which nothing here produces), 0px error
everywhere, bar the half pixel a 1.5 factor leaves.

**GNOME 46 has a second layout mode, and it is not a bug.** With "Fractional Scaling"
off, 24.04's default, Mutter is in *physical* layout mode and its layout
coordinates are raw pixels: the same head at 200% is a 1920x1080 desktop and
`mousemove 400 100` lands at device 400,100. Both modes were measured; the coordinate
you pass is whatever the desktop itself is using, and `wxrandr --query` says which.

**One GNOME 46 state is a Mutter defect, and the pointer is not the part of it that
is wrong.** Turn Fractional Scaling **on** while a monitor is already at 200% and
Mutter 46 moves to logical mode without re-sending `zxdg_output_v1.logical_size`: the
desktop it draws is 960x540 and the layout it advertises is still 1920x1080. It maps
absolute pointer motion across the layout it advertises, so `mousemove` keeps landing
on the coordinate you ask for, measured in that state against the KMS cursor plane,
every target from `100,100` to the desktop's last pixel at `959,539`, 1:1. What is
wrong is what you are **told**: `getdisplaygeometry` reports 1920x1080 for a desktop
that ends at 959,539, so a script that works out the middle of the screen from it aims
at the bottom-right corner, and `1600,1000` looks like a legal coordinate while being
off the screen.

Nothing on the wire separates that state from a legitimate physical-mode session, mode
1920x1080, `wl_output.scale` 2 and xdg logical 1920x1080 are byte for byte what both
send, so wdotool asks `org.gnome.Mutter.DisplayConfig` when, and only when, the wire
carries that signature, and **says so once** when the two answers differ, naming both
and the way out. It does not take DisplayConfig's number for the pointer: that was
tried, and since Mutter's absolute-input mapping uses the stale rectangle too, it lands
every target at twice the coordinate asked for (`hacks/input/layoutbox.py`). Changing the
scale once clears the whole state; 26.04 and Plasma never enter it.

## Architecture

- **Input injection**: virtual evdev devices via `/dev/uinput` (keyboard, relative
  mouse, absolute pointer mimicking a QEMU USB tablet). Compositor-agnostic, injection
  happens at the kernel input layer, so it works on GNOME, KDE, wlroots, everything.
  Both halves have a second path where the kernel one is closed:
  `zwp_virtual_keyboard_v1` (`vkbd.py`) for keys and `zwlr_virtual_pointer_v1`
  (`vptr.py`) for the pointer, neither of which needs any privilege on wlroots, see
  [Typing and clicking with no privilege](#typing-and-clicking-with-no-privilege---vkbd)
  for the one policy that picks them.
- **Daemon**: first invocation auto-spawns itself as a daemon (`argv[1] == "__daemon"`,
  double-fork; see `__main__.py`). The daemon owns the uinput devices (device creation
  costs ~600ms of compositor hotplug latency, pay it once), tracks the injected cursor
  position, and serves JSON-lines on a unix socket in `session.runtime_dir()`.
- **Window management**: per-compositor backends behind `backend.WindowBackend`.
  Detection uses an explicit `WDOTOOL_BACKEND` first, then sway/i3 IPC,
  Hyprland IPC, KWin/GNOME/Cinnamon on the session bus, Wayfire IPC, and finally
  the advertised wlr or COSMIC toplevel protocols. GNOME detection checks for
  Mutter or the bridge as well as `org.gnome.Shell`; if those are absent, it
  checks the registry so Budgie is not mistaken for GNOME. See
  [window backend detection](Technical.md#4-window-backends) for the precise
  rule and failure handling. A detected GNOME session without the bridge fails
  with installation instructions. Window IDs are backend-native
  numeric ids (sway: node id; GNOME: `Meta.Window.get_id()`), printed in decimal
  like xdotool.
- **Running under sudo / as root over ssh**: the session's sockets, bus and X cookie
  are found for us by `w11common/session.py`. What it looks at, in which order, and
  why `session.py` and `passthrough.py` answer different questions, is
  [Technical.md § Session discovery](Technical.md#2-session-discovery-and-the-x11-handover).
- **On an X11 session wdotool does not run at all**: it `execve`s the real `xdotool`
  with argv untouched. [Technical.md § The X11 handover](Technical.md#2-session-discovery-and-the-x11-handover)
  is the contract, including the four "not us" guards and what stays ours (`keys`,
  `__keymap`, `--layout`, `--vkbd`).

## Command dispatch contract

Chained commands parse exactly like xdotool: each command consumes its own flags and
positionals from the remaining argv and returns how many tokens it consumed:

```python
def cmd_foo(ctx: Context, args: list[str]) -> int   # tokens consumed, excluding command name
```

Failure: `raise CmdError(msg)` (`w11common/errors.py`, because every tool raises and catches it), driver prints to stderr, aborts the chain, exits 1.
Non-fatal failure (`search` with no matches): set `ctx.exit_code = 1`, consume args
normally. `Context` (`ctx.py`) provides `stack`, `resolve_window(arg|None)`,
`resolve_windows` (`%@` → whole stack), lazy `backend()` and `daemon()`.

## Command → module → function (function name = `cmd_<name>`)

- `misc_cmds`: `exec`, `sleep`, `getdisplaygeometry` (via `ctx.daemon().geometry()`).
  `cli.py` additionally handles: `help`, `version`, `-h/--help`, `-v/--version`, usage
  text, script mode (`wdotool <file> args...` when the first arg is a readable file, and
  `wdotool -` from stdin) per the manpage SCRIPTS section, and the driver loop over
  `commands.py`'s `{name: fn}` registry.
- `input_cmds`: `key`, `keydown`, `keyup`, `type`, `click`, `mousedown`, `mouseup`,
  `mousemove`, `mousemove_relative`, `getmouselocation`, `behave_screen_edge`
  (unsupported: CmdError).
- `window_cmds`: `search`, `selectwindow`, `getactivewindow`, `getwindowfocus`,
  `getwindowname`, `getwindowclassname`, `getwindowpid`, `getwindowgeometry`,
  `windowactivate`, `windowfocus`, `windowraise`, `windowlower`, `windowmap`,
  `windowunmap`, `windowminimize`, `windowmove`, `windowsize`, `windowclose`,
  `windowquit`, `windowkill`, `windowreparent` (warn+succeed), `windowstate`,
  `set_window` (warn+succeed), `behave` (unsupported: CmdError).
- `desktop_cmds`: `set_num_desktops` (warn+succeed), `get_num_desktops`,
  `set_desktop`, `get_desktop`, `set_desktop_for_window`, `get_desktop_for_window`,
  `get_desktop_viewport` (print `0 0`), `set_desktop_viewport` (warn+succeed).

Impossible-on-Wayland *cosmetic* ops (marked warn+succeed): one warning line to
**stderr**, consume args correctly, succeed, scripts must not break on them. Real
capability gaps in a detected backend: raise CmdError.

## The input daemon

`daemon.py` implements this client API. `wdotool` is the only tool here that injects
anything, and every command of it that does goes through the daemon.

```python
class DaemonClient:
    @classmethod
    def connect_or_spawn(cls) -> "DaemonClient": ...
    def type_text(self, text: str, delay_ms: int, clearmods: bool = False,
                  layout_mode: str | None = None, vkbd_mode: str | None = None): ...
    def key(self, spec: str, direction: str, delay_ms: int, clearmods: bool,
            layout_mode: str | None = None, vkbd_mode: str | None = None): ...
        # spec "ctrl+shift+t"; direction in {"press","down","up"}
        # layout_mode/vkbd_mode: --layout / --vkbd, sent only when given
        # clearmods: release the modifiers, inject, press back the ones we held
    def clear_modifiers(self) -> list: ...      # release them; -> the ones we held
    def restore_modifiers(self, held): ...      # press those back
        # kept for this API; no command uses the pair -- as two extra round
        # trips it leaves gaps another process can inject into. Every
        # injection op below carries `clearmods` instead and the daemon does
        # clear/inject/restore under one hold of its lock.
    def mousemove_abs(self, x: int, y: int, clearmods: bool = False,
                      vkbd_mode: str | None = None): ...
    def mousemove_rel(self, dx: int, dy: int, clearmods: bool = False,
                      vkbd_mode: str | None = None): ...
    def button(self, btn: int, down: bool, clearmods: bool = False,
               vkbd_mode: str | None = None): ...   # 1=L 2=M 3=R 4..7=wheel 8..12=side/extra/fwd/back/task
    def click(self, btn: int, repeat: int, delay_ms: int, clearmods: bool = False,
              vkbd_mode: str | None = None): ...
        # vkbd_mode: --vkbd, which selects the POINTER path as well as the
        # keyboard one (one switch, one decision); sent only when given
    def pointer(self) -> tuple[int, int]: ...
    def seed_pointer(self, x: int, y: int): ...   # adopt the compositor's real pointer
    def geometry(self) -> tuple[int, int]: ...    # (w, h) of the full output layout
    def geometry_full(self) -> tuple[int, int, int, int]: ...  # (min_x, min_y, w, h)
    def geometry_status(self) -> tuple[int, int, bool]: ...    # (w, h, is_a_guess)
def daemon_main() -> int: ...
```

Daemon notes:
- uinput via pure stdlib (`fcntl.ioctl` + `struct`; the legacy `uinput_user_dev`
  write-based setup is the portable path). Devices: keyboard (KEY_ 1..=255, everything the keymap can emit, numeric X keycodes included), relative
  mouse (REL_X/Y/WHEEL/HWHEEL + BTN_LEFT/MIDDLE/RIGHT/SIDE/EXTRA/FORWARD/BACK/TASK,
  the last three giving X buttons 10..12), absolute pointer
  (ABS_X/ABS_Y 0..=32767 + buttons + wheel, the QEMU usb-tablet shape every compositor
  maps across the whole output layout). Sleep ~600ms once after creation for hotplug.
- `key` spec: modifier aliases ctrl/control/alt/meta/super/shift + any keysym name;
  keysym names via `keysyms.py`, GENERATE a full name→keysym→unicode table from
  xorgproto `keysymdef.h` + `XF86keysym.h` (curl once, commit the generated file;
  no network at build). XF86 keysyms resolve through a hand-curated XF86→evdev
  table in `keymap.py` (they have no unicode); `_EVDEVK`-range keysyms
  (0x10081000+code) carry their own evdev code.
- char → keycode: static US-QWERTY table (char → keycode, shifted?) whenever the
  session's active layout *is* plain US, and the reverse of the compositor's own
  keymap otherwise (B13). Unreachable chars: stderr warning, skip.
- **`clearmods` (`--clearmodifiers`)**: inject key-up for all 8 modifier
  keys, do the injection, then press back the ones **this daemon was holding**
  (`self.down`), sampled with no ioctl and no re-read, clear, inject and
  restore run under one hold of the injection lock, so there is no window in
  which the answer can go stale and no gap for another process to inject into.
  It is deliberately not more than that, for two kernel reasons measured live
  on GNOME, KDE and sway: `input_handle_event()` **drops an `EV_KEY` release
  for a code the emitting device does not hold**, so a key-up for a modifier
  held on the user's own keyboard generates no event at all; and a key-*down*
  we send stays ours, while Mutter and KWin reference-count key state across
  the seat's devices, so the user's release of the same modifier leaves it
  active with nothing left to clear it, a modifier stuck down for the rest of
  the session. Pressing back a modifier we never held therefore restores
  nothing and strands everything.
  `keystate.py` (`EVIOCGKEY` on each `/dev/input/event*` that advertises
  modifier keys and is not one of ours, `UI_GET_SYSNAME` on our own uinput
  fds, plus a device-name check) is read **for the diagnostic only**: when a
  foreign keyboard holds a modifier, say which, once per client connection,
  rather than let a flag that cannot help look like it did nothing. That read
  needs root (logind's `uaccess` ACL covers `/dev/uinput`, not keyboards);
  without it the behaviour is identical and nothing is said.
  `WDOTOOL_NO_KEYSTATE=1` forces that path for testing.
- geometry: Wayland client via `wayland_mini` (wl_output geometry+mode; prefer
  zxdg_output logical size/position when advertised), with a 3s socket timeout so a
  wedged compositor falls back instead of hanging the daemon. Cache is the full
  layout box `(min_x, min_y, w, h)`, multi-output layouts can have non-zero or
  negative origins. **One wire state does not say which pixel space it is in**, a
  head whose advertised logical size *is* its raw mode size while it claims
  `wl_output.scale` 2 or more is either GNOME 46 in physical layout mode or GNOME 46
  that has stopped updating `logical_size`, and only that state asks
  `org.gnome.Mutter.DisplayConfig`, over a bus kept open, to compare. The box stays
  the wire's either way, because that is the rectangle the compositor maps absolute
  motion across even when it is stale; a difference is a one-off diagnostic, since it
  is `getdisplaygeometry` that is then reporting a desktop that is not there. Every
  other session (scale 1, any logical-mode session, KDE, sway, X11) never opens a bus
  and pays nothing: `hacks/input/layoutbox.py`, and
  [Pointer accuracy](#pointer-accuracy) for the measurement.
  Fallback (0, 0, 1920, 1080) + warn, and the `geometry` reply
  says `fallback: true` so `getdisplaygeometry` can refuse to print a guess
  (rc 2) instead of pretending. `DaemonClient.geometry()` keeps returning
  `(w, h)`, `geometry_full()` adds the origin, `geometry_status()` adds the flag.
- **abs scaling (B7)**: on the kernel tablet, `ceil((x - min_x) * 32768 / w)`,
  clamped to 0..32767. (On the virtual-pointer path there is no axis at all: see
  "the virtual-pointer path" below, the coordinate is sent as a ratio over the
  layout and lands exactly, so neither this nor B2 has a counterpart there.)
  libinput maps an absolute axis with `scale_axis()`, `v * w / (max - min + 1)`
  = `v * w / 32768`, and the compositor truncates that to a pixel; the forward
  map that round-trips exactly through that inverse is the *ceiling*, since
  `floor(ceil(x*32768/w) * w/32768) == x` for every `x` in `[0, w)` while
  `w <= 32768`. The old floor map `(x - min_x) * 32767 // (w - 1)` landed one
  pixel short wherever the division was inexact, 257 of 301 x values near the
  origin of a 5760px layout.
- **always land the move (B2)**: the kernel drops an `EV_ABS` whose value equals
  the axis' current value, so re-sending the coordinates the tablet reported
  last time was a silent no-op, and REL events, a physical mouse or another
  daemon may have moved the pointer since. When the computed axis pair equals
  the last one written, one axis is nudged by a single unit (1/32768 of the
  layout: sub-pixel) in its own report first. A fresh uinput device reads 0,0,
  which is why a first `mousemove 0 0` is nudged too.
- **relative moves (B1)**: `REL_X`/`REL_Y` go through the compositor's pointer
  acceleration, so `mousemove_relative 500 0` moved 462px on GNOME 46 and 267px
  on GNOME 50 while xdotool's `XWarpPointer` is exact. Everywhere but sway/i3
  the daemon emits the *target* `(px+dx, py+dy)` as an absolute warp instead.
  sway/i3 keep the REL path (that rig runs `pointer_accel 0`, and the daemon
  tests pin the `EV_REL` contract); `WDOTOOL_REL_MODE=abs|rel` forces either.
  The switch is decided once per daemon by looking for a sway/i3 IPC socket.
- **pointer position (B6)**: the daemon tracks the position it last injected, a *model*, not the truth. Clients that can ask the compositor (the GNOME
  bridge's `GetPointer`) do so first and push the answer back with
  `seed_pointer`, so `getmouselocation` reports reality and a following
  relative move counts from it. sway's IPC has no pointer query, so the model
  stands there. A daemon that never opened `/dev/uinput` refuses the `pointer`
  op with that reason rather than answering `0,0` with rc 0; a daemon on the
  virtual-pointer path refuses it with the protocol's reason instead
  (`POINTER_UNKNOWN`), /dev/uinput is not what the user would have to fix
  there, and cannot be, because nothing on that path can be asked. Only an
  **absolute** move establishes the model: a relative one is a delta, and a
  delta applied to a position nobody knows is a guess, so it moves the cursor
  and leaves `pos_known` false rather than reporting a number it invented.
  The **query the two move commands make before injecting**, `_pointer_opt`
  in `input_cmds.py`, is therefore optional by construction: `mousemove`
  wants it only to remember a position for `mousemove restore`, and
  `mousemove_relative` only to count from the truth where a compositor can
  supply it. Making it mandatory is what made the refusal above fail the
  move: on sway with no `/dev/uinput`, the session the whole path exists
  for, `wdotool mousemove 100 100` exited 1 and moved nothing, however well
  the protocol worked.
- **the active layout (B13, `xkbmap.py`)**: we inject *keycodes*, and the
  compositor reads them through whatever XKB layout the session has active, so
  the fixed US table is wrong for everyone else: `type y` gives `z` on German,
  `key ctrl+z` arrives as `ctrl+y`. X11's trick (rebind a spare keycode to the
  wanted keysym) is not on the table here -- no compositor takes a keymap edit
  from a client, and Mutter does not implement zwp_virtual_keyboard_v1 either --
  so the lookup is reversed instead: every Wayland
  client is handed the full keymap on `wl_keyboard.keymap` as an fd, and
  `xkbmap.fetch()` binds the seat, takes the keyboard, reads it, and
  `xkbmap.build()` turns the active group into char → (keycode, modifier mask).
  Levels come from the key's type when the keymap states one and from the
  xkeyboard-config convention otherwise (2 = Shift, 3 = level three, 5 = level
  five); which *key* carries level three is read out of the same group
  (`<RALT>` on German, the synthetic `<LVL3>` where the layout leaves `<RALT>`
  as Alt_R). A character that needs a dead key becomes two keystrokes (the mark
  from NFD, then the base letter) and the application composes them, exactly as
  it does for a physical French keyboard; a bare spacing accent is the dead key
  *twice* and dead-key-plus-space types what the Compose table every toolkit
  ships says it types (`'`, not `´`, claiming otherwise silently typed the
  wrong character). Keypad keys are ranked below the main block rather than
  excluded by key *name*, which used to leak `(` to `KP_Left_Parenthesis` and
  French `.` to `KP_Decimal`. Comments are stripped before parsing: a `}`
  inside one would otherwise close a block early. Unreachable characters warn
  and skip, as before.
  - **the US bypass** is the whole safety story: `xkbmap.active_group_is_plain_us()`
    verifies, key by key, that every keycode the fixed table would emit carries
    exactly the keysyms the fixed table assumes at levels 1 and 2 *in the active
    group* (plus the group name and a type whose level 2 really is Shift). If it
    holds, the old path runs and nothing else in `xkbmap` is even called, the
    check shares no code with the parser or the reverse map, and choosing a
    group is regex-only for the same reason. What it does *not* check is as
    required: only the printable characters (Escape, Return, Tab,
    BackSpace and Delete are position keys, the same everywhere, and go
    through `KEYSYM_KEYS`), and a key's type only where the fixed table
    actually presses level 2. Checking those made a plain `us` session with
    `caps:swapescape` or `grp:win_space_toggle` fail the check and drag the
    whole reverse map in, a fail-*open*, and the one thing the bypass exists
    to prevent. Any failure, no compositor, an unparsable keymap, a crash in
    the new code, also lands on the fixed table, with one diagnostic per
    daemon in the log and a warning to *every* client that types through the
    fallback.
  - **the group**: `wl_keyboard.modifiers` carries the active group but every
    compositor sends it only to the *focused* client, which an injector never
    is (measured on Mutter 46/50, and the same is true of wlroots and KWin). So
    the group is taken from that event if it ever arrives; failing that, from
    the desktop, which knows even where the protocol will not say
    (`xkbmap.desktop_group`, the two bullets below); and failing that it is
    group 1, definitively when every group binds the same symbols (GNOME
    compiles a lone `us` source as "us,us"), otherwise as a flagged assumption.
    A session with several sources whose first one is `us` verifies as plain US
    and takes the bypass, so the notice is said on that path too, because that
    is the session that types the wrong characters after a switch to its second
    layout. The notice is made once per layout state and again on every change.
    `WDOTOOL_XKB_GROUP=<n>` pins it, ahead of every desktop.
  - **the group, on KDE** (`xkbmap.KwinLayouts`): exactly where the paragraph
    above would flag an assumption, KWin is asked instead, `org.kde.KWin` `/Layouts` `org.kde.KeyboardLayouts.getLayout`, a 0-based
    index into the configured list, which is the keymap's group order, so the
    group is `index + 1` and the index is clamped against `group_count` (KDE
    truncates that list at four itself, which is XKB's `MAX_GROUPS`). The
    snapshot's source becomes `wayland + kwin` and `group_known` becomes true,
    which is what silences the notice and drops `(assumed)` from
    `keys explain`. One connection for the life of the process, opened on the
    first question that needs it (so the sessions that know their group never
    open one, and `--layout us` still runs no layout code at all); the daemon
    is root and gets there the way the KWin *window* backend does, through
    `dbus_mini`'s forked auth (~3 ms to connect, ~0.3 ms per call). `NameHasOwner`
    before the first call, because a method call to an unowned name would ask
    the bus to *start* KWin on a desktop that is not KDE; a call that fails is
    retried once on a fresh connection (a KWin restart takes ours down with it)
    and then backed off for ten seconds; a bus with no KWin on it is remembered
    as such and never dialled again. Every failure returns None and the guess
    stands. Never kded: both `/modules/keyboard` copies of this interface crash
    on `getLayout`.
  - **the group, on GNOME** (`xkbmap.GnomeInputSources`): the same seam, one
    desktop over. Mutter says no more than KWin does, but GNOME Shell keeps
    the answer in GSettings and `xdg-desktop-portal` serves GSettings on the
    session bus, so it is one
    `org.freedesktop.portal.Settings.ReadAll(["org.gnome.desktop.input-sources"])`, 1.2 ms per read in the process that has the connection, and 28 ms for
    the first one, which pays for the connect and for the bus activating the
    portal if nothing has yet. `mru-sources[0]` is the live source (`current` is deprecated
    and ignored, the shell writes only `mru-sources`, on every switch, and
    `dconf watch` across a session of switching shows nothing else), and its
    index maps onto the group: Mutter appends its own `us` group after the
    user's sources and compiles them in chunks of three, so the group is
    `index % 3 + 1`, clamped against `group_count`. `NameHasOwner` on
    `org.gnome.Shell` first, for the same reason KDE's does it, and the same
    remembered no; the same one reconnect and ten-second backoff; the same
    None for every failure. dconf itself is not an option: `ca.desrt.dconf`
    publishes `Init`, `Change` and the `Notify` signal and **no read method**.
    Refused rather than answered: `per-window` layouts, a `mru-sources` head
    that is no longer in `sources`, and a source that is not an `xkb` layout.
    **As root it costs a fork.** The portal identifies its caller by opening
    `/proc/<pid>/root` and answers the session user only, and on GNOME the
    daemon is root whenever it was started with `sudo` rather than through the
    udev rule, so the connect *and* the call happen in a child that has
    dropped to that user and put `PR_SET_DUMPABLE` back (setuid clears it, and
    a process that is not dumpable has a `/proc/self` only root may open,
    which is precisely what the portal is not), with the answer piped home as
    JSON: 6.3 ms a read against the same session's 1.2, measured on GNOME 46
    with the shipped build. `Bus(as_uid=)` cannot serve here, though it exists
    for exactly this shape of problem: its child hands the socket back and
    exits, so by the time the call goes out there is no process for the portal
    to identify (`AccessDenied: Unable to open /proc/N/root`).
  - **cache**: keyed on (sha256 of the keymap text, group), re-read on *every*
    `type`/`key`, the user can switch layout between two commands, and a
    long-lived daemon has to notice. A rebuild costs ~15ms; a hit is a Wayland
    roundtrip and a hash. A failed read backs off 5s so a wedged compositor
    cannot stall every keystroke. The read waits up to 80ms for a `modifiers`
    event the first time and then, if none came, never again, a compositor
    that does not send one to an unfocused client never will, and paying that
    wait per command cost a plain US GNOME session +87ms on every keystroke
    until the condition was keyed off the event instead of off the group.
  - **overrides**, most specific first.
    `--layout us|fixed|auto|xkb` (a global option of ours, stripped in
    `cli.main` before dispatch so no command's parser and no parity-checked
    usage text sees it; carried to the daemon on the `type`/`key` request as
    `layout_mode`, because the daemon cannot see the client's environment).
    It outranks the variables below. `--layout us` returns from `_layout()`
    before any fetch or bypass check, which is the promise it makes.
    Then `WDOTOOL_XKB_GROUP=<n>`, which pins the group and is read from the
    *client's* environment and carried on the request as `xkb_group`, for the
    same reason `--layout` is: the daemon outlives the command that started it
    and cannot see what the next one was run with, so a pin that only reached
    a fresh daemon was ignored by a running one and the notice kept printing.
    Then the rest of the environment (read by the daemon, which keeps what it
    was spawned with): `WDOTOOL_LAYOUT=us` never reads a keymap at all,
    `WDOTOOL_LAYOUT=xkb` forces the reverse map even on a US layout,
    `WDOTOOL_XKB_KEYMAP=<file>` reads a keymap from a file (what the tests use).
- **spawn hygiene (B10/B11)**: the double-forked grandchild closes every fd
  above stdio (it used to keep the client's session-D-Bus socket ESTABLISHED
  for its whole life), `chdir("/")`, keeps only the environment it needs
  (`XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`, `HOME`, `PATH`, `USER`, `LOGNAME`,
  `LANG`, `LC_ALL`, `SUDO_UID`, `PKEXEC_UID`, `WDOTOOL_*`), and re-execs as
  `wdotool __daemon` or `python -m wdotool __daemon` so `ps` shows the daemon
  and not the command that happened to spawn it. When it was started from a
  GNOME custom shortcut it also leaves the launcher's transient
  `app-*.scope`: a systemd scope stays active while any process is in its
  cgroup and neither fork nor setsid changes cgroups, so the daemon used to
  hold that scope (and the shortcut's shell script, as far as systemd was
  concerned) open for its whole life. It writes its pid into a sibling cgroup
  under the user manager's delegated subtree, one write, no `systemd-run`
  subprocess and none of its environment surprises. `session-*.scope` and
  service units are left alone: a daemon started from a login session should
  still die with it. A failure at any step is not fatal, just the old
  behaviour.
- **lifetime**: outliving the command that spawned it is the whole point, that is
  what makes the next command cheap, but not for ever, and not once nobody can
  reach it. Between `accept()` calls, once every `WDOTOOL_DAEMON_CHECK` seconds (15)
  and only while no client is connected, the daemon asks two questions:
  - **is the socket still mine?** `stat()` on the path it bound, against the
    `(st_dev, st_ino)` it saw when it bound it. The inode, not the name, so this also
    catches a second daemon having taken the path over. Logging out clears
    `$XDG_RUNTIME_DIR`, and a daemon whose socket file is gone can never be dialled
    again by anybody, it used to sit there anyway, holding its devices, until the
    machine was rebooted. (Measured on the test rig: 161 alive at once, ~3GB between
    them, sockets in directories deleted hours before, the oldest 18 hours old.)
  - **has anyone used me in the last `WDOTOOL_DAEMON_IDLE` seconds?** 900 of them, a
    quarter of an hour. The gap it has to sit through is the gap between two commands
    of one piece of work, a script, a keybinding pressed twice, a person moving a
    window by hand, and those are seconds apart; a quarter of an hour is an order of
    magnitude past the longest of them, and the price of getting it wrong is one
    respawn (~600ms, paid once, by the command that comes late). A daemon holding a
    key or a button down for the next command (`keydown ctrl`) is never idle: its
    exit would release exactly what it is there to hold.

  Both are seconds and both take `0` for "never"; `WDOTOOL_DAEMON_CHECK=0` turns off
  the tick and with it both checks, which is the bare accept loop 0.3 shipped. Idle
  costs one wakeup and one `stat()` per 15s, `accept()` blocks in the kernel in
  between, and a client that is connected at that moment is never interrupted,
  because a connection means "in use" for both checks. On the way out the daemon
  unlinks the socket **only while it is still the one it bound** (the startup lock's
  rule, a loser never unlinks the winner's socket, applied to the exit), closes the
  devices, and logs one line saying which of the two it was.
- hardening: the socket is bound under umask 0o177 and chmod 0600 (root daemon
  serves root only; non-root users spawn their own per-user daemon and hit the
  clean "/dev/uinput ... run it as root" error). Per-request catch-all keeps a
  malformed request from killing the connection; repeat ≤ 1e6, delays ≤ 300s,
  coordinates int32, request lines ≤ 16MB. Partially-created uinput devices are
  closed on failure and creation is retried on the next request.
- Protocol: one JSON object per line each way; `{"ok":true,...}` /
  `{"ok":false,"error":"..."}`. Ops mirror the client API 1:1.

## Backend notes

- **sway**: raw i3-ipc over `SWAYSOCK` ("i3-ipc" magic + u32 len + u32 type + JSON).
  Window id = node id. map/unmap = scratchpad show / move-to-scratchpad; minimize =
  move-to-scratchpad; raise/lower: warn no-op for tiled, focus for floating.
  Desktops = workspaces, 0-based for xdotool (workspace `num` - 1). `selectwindow` =
  subscribe to window focus events, return the next focused window, knowingly
  not xdotool's semantics (the window under the pointer at the next button
  press): sway's IPC has no interactive picker, no pointer position and no way
  to grab input from outside the compositor, so clicking the window that
  already has focus does not end the wait. GNOME and KDE do click-to-select.
  `getmouselocation`'s window field: hit-test the daemon-tracked pointer against
  `list()` geometries (topmost/focused first).
- **kwin**: `org.kde.kwin.Scripting.loadScript()` over `dbus_mini`, unprivileged
  on Plasma 5.27 and 6 alike (plain `Q_SCRIPTABLE` on `/Scripting`, no polkit, no
  bus policy), so **nothing has to be installed**, unlike the GNOME bridge. One
  generated script per command (`hacks/window/kwin_js.py`, the whole 5.27↔6 divergence
  lives there); it answers with the JS global `callDBus()` to a name we own
  (`org.w11.KWin` / `/org/w11/KWin` / `org.w11.KWin1`,
  members `Result(token, json)` and `Event(token, uuid, change)`), which
  `dbus_mini` serves with `serve_calls`. `Script::run()` is a delayed reply sent
  only after `evaluate()` returns and `callDBus` rides the same connection, so the
  payload is already queued when `run()` returns, no `dbus-monitor`, no sleep;
  the wait still has a deadline (a JS error only reaches the journal, `run()` still
  replies OK, so the script answers on every path and silence is an error). Unique
  `wdotool-<pid>-<seq>-<random>` pluginName per call with `unloadScript` in a
  `finally` (script *ids* are `scripts.size()` and get reused), the **signed**
  `loadScript` result rejected when negative (`-1` = that name is already
  loaded), both object paths tried (`/Scripting/Script<id>` on 6, `/<id>` on
  5.27), and a per-call token so a late payload from a timed-out call is never
  read as this one's answer. The random part of the name is required: KWin
  holds a pluginName for as long as the script object lives and nothing can
  enumerate what is loaded, so a wdotool killed between `loadScript` and
  `unloadScript` leaks its name for the rest of the session, with pid+counter
  alone the next process handed that pid would fail on its first command, for
  ever.
  Ids are minted here (the scripting API has no numeric window id at all):
  `0x40000000 | 30 bits of internalId`, with an `{id: uuid}` cache (the uuid is
  the only handle the scripting API and `getWindowInfo` take); two uuids
  colliding in those 30 bits (a one-in-a-million session) re-mint the second
  window rather than dropping it out of the listing. 32-bit clean
  because every X-shaped consumer truncates there (`wxprop -id` parses into an
  XID, the synthesized `_NET_CLIENT_LIST`, wmctrl's `0x%08lx`), and out of the
  range Xwayland hands its clients, so a native id is never mistaken for the X
  id of an XWayland window in the same listing. `class_` =
  `resourceClass`, `instance` = `resourceName`; geometry = `frameGeometry`, rounded
  (`QRectF` on 6); `visible` = not minimized, not hidden and on the current desktop;
  `list()` is stacking order bottom→top. Every `frameGeometry` write resets
  maximize (unconditionally, 5.27 has no `maximizeMode` property to test),
  tile and fullscreen first, or KWin clamps or ignores it. `activate`
  unminimizes, brings the window's desktop up and sets `activeWindow`/`activeClient`
  (a real focus+raise, so `focus` is the same call without those two); `kill` is
  `SIGKILL` on `w.pid` (`org.kde.KWin.killWindow()` is the interactive xkill
  picker, not kill-by-id). `windowstate`: FULLSCREEN, HIDDEN, ABOVE, BELOW,
  MAXIMIZED_HORZ/VERT (`setMaximize`), STICKY, SKIP_TASKBAR, SKIP_PAGER,
  DEMANDS_ATTENTION; SHADED works on 5.27 and is a capability gap on 6 (shading
  removed). Writing a state the window has no property for is refused before
  the write (assigning one QJSEngine does not know just adds a JS property to
  the wrapper and reads back as success), and a state KWin accepts and then
  ignores -- 5.27 refuses to fullscreen a window whose size hints cannot fill
  the screen exactly -- is read back and warned about, warn+succeed like the
  X tools. That read-back is a *settled* one: a Wayland client applies a
  size-changing state only when it acks the configure, so when the immediate
  read disagrees the script arms the window's own change signal plus a QTimer
  backstop (`SETTLE_MS`) and answers from whichever fires first, otherwise
  every FULLSCREEN on a native window warned although KWin had applied it.
  `settled: false` (nothing could be armed) never warns. Waiting also means
  the *next* command sees a settled window, which is what makes
  `wmctrl -b add,maximized_vert,maximized_horz` end with both axes on.
  `maximizeMode` is a Q_PROPERTY on 6 only, 5.27's `window.h` declares none, so there the mode is read off the geometry (`frameGeometry` ==
  `clientArea(MaximizeArea, w)` on that axis); without it 5.27 reported every
  window as restored, cleared the other axis on every `setMaximize()` and left
  a maximized window maximized while `windowsize` wrote under it.
  `set_num_desktops` stops as soon as a `createDesktop`/`removeDesktop`
  changes nothing: both slots are void and KWin silently refuses past its own
  limits (`maximum()` = 20 on 5.27, 25 on 6, and never below one), so without
  a progress check the loop never ends and floods the compositor. `raise` = `workspace.raiseWindow` on 6; 5.27 has no per-window raise,
  so it activates and says so on stderr. Neither release has a per-window
  **lower**: an active window is lowered with `slotWindowLower()`, otherwise it is
  marked keep-below and warned about. No script at all for `get_desktop`/
  `set_desktop` (`org.kde.KWin.currentDesktop`, 1-based), `num_desktops` /
  `workspaces()` / `set_num_desktops` (`/VirtualDesktopManager` `count`, `current`,
  `desktops a(iss)`, `createDesktop`/`removeDesktop`), `show_desktop` (NoReply) and
  `selectwindow`, `queryWindowInfo()` *is* xdotool's selectwindow, KWin's own
  interactive picker, a delayed reply carrying the uuid (`UserCancel` →
  "cancelled"). Extras for wwmctl/wxprop: `views()`, `workspaces()` (work areas
  from `workspace.clientArea(WorkArea, …)`, cached with the screen size),
  `x_info()` (the session scan, KWin publishes its Xwayland's DISPLAY nowhere),
  `backend.hit_test()` (client-side over the same list, like GNOME, so it cannot
  drift from the generic rule) and `events()`, a script that stays loaded for the
  iteration, connects `workspace`'s and every window's Qt signals and `callDBus`es
  each one out on a second connection. `View.xid` for XWayland windows: `w.windowId`
  on 5.27; on 6 (`x11window.h` has no `Q_PROPERTY` left) by matching Xwayland's
  `_NET_CLIENT_LIST`, pid and `WM_CLASS` filter, title and geometry distance
  score, greedy best-first, and only when an Xwayland process already exists,
  since connecting would start one. A pair must also *agree* on pid or class:
  an X client that publishes neither contradicts nothing, and matching it on
  geometry alone hands its id to a native window, which then claims to be an
  X11 client. `w.output` is read on 6 only, on 5.27 it is a `KWin::Output*`,
  a datatype QJSEngine has no converter for, and merely reading it logs a
  `QMetaProperty::read` warning to the journal once per window per command.
- **gnome**: the w11 bridge extension (`gnome/w11-bridge@w11`,
  installer `gnome/install-bridge.sh`) exports Mutter over the session bus, name
  `org.w11.Bridge`, path `/org/w11/Bridge`, interface
  `org.w11.Bridge1`, JSON strings for structured results, and
  `backend_gnome.py` is a thin `dbus_mini` client for it (no gdbus, no Eval, no
  Window Calls). Ids = `Meta.Window.get_id()`. `class_` = `wm_class` (Mutter reports
  the Wayland app_id there), else `gtk_app_id`; geometry = `get_frame_rect()` in
  logical pixels, the same space as the daemon's pointer; `visible` = not
  minimized/show-desktop AND on the active workspace (X11 IsViewable), `is_mapped` =
  not minimized. `list()` is stacking order bottom→top; `backend.hit_test()` looks
  through DESKTOP/DOCK layers for `getmouselocation`. `activate` = `Meta.Window.activate`
  (switches workspace, unminimizes, raises) and waits ≤ 0.5 s for the focus to land;
  `focus` = `Meta.Window.focus` (no raise); `kill` = `Meta.Window.kill` (Mutter kills
  the client, works as any uid); map/unmap = unminimize/minimize; raise/lower real.
  `windowstate`: FULLSCREEN, MAXIMIZED_HORZ/VERT (per-axis on 46 and 49+), HIDDEN,
  ABOVE, STICKY, DEMANDS_ATTENTION applied by Mutter; SKIP_TASKBAR/SKIP_PAGER/
  MODAL warn+succeed (cosmetic, no Mutter setter); SHADED (observable, Mutter
  cannot shade), BELOW and anything else CmdError. Desktops = workspaces (dynamic
  workspaces count the trailing empty one;
  `set_desktop_for_window -1` sticks). `selectwindow` = bridge `SelectWindow(0)`
  (bridge v2+, refused with a "reinstall it" message below that): a stage grab
  in the extension, resolved by the next button press with the window under
  the pointer, `.Cancelled` for Escape / its 30-second cap / a caller that went
  away (and `.Unsupported` for a call inside the quiet period a finished
  selection leaves behind: the cap bounds one call, that bounds the caller), and `.Unsupported` for a second concurrent picker or a shell that is
  already modal (the overview, a menu: `pushModal` refusing is a refusal, not
  a reason to take a plain stage grab and hit-test against frame rects that
  are not on screen), all of them rc 1 with the reason. The D-Bus call has no
  timeout, the extension always answers. Extras for wwmctl/wxprop: `views()` (xid, WM_CLASS
  instance/class, app_id, states), `workspaces()`, `x_info()` (gnome-shell's own
  DISPLAY/XAUTHORITY via the bridge, else `session.find_x_display/find_xauthority`),
  `events()` (bridge `WindowEvent` signals), `monitors()`, `real_pointer()`
  (diagnostic: the compositor's pointer vs the daemon's). The bridge exports no
  hit-test; `backend.hit_test()` is client-side over `ListWindows` so it cannot
  drift from the generic rule. **`org.gnome.Shell` on the bus is not proof of GNOME
  Shell**, and detection reaches this backend only when one of GNOME's own two names is
  beside it, `org.gnome.Mutter.DisplayConfig` (gnome-shell's own, and what wxrandr's
  mutter backend drives) or `org.w11.Bridge` (which can only be owned from inside
  gnome-shell). With neither, the registry decides, and a compositor publishing a
  foreign-toplevel protocol is not Mutter, which publishes none. Measured on the
  `resolute-budgie` golden (Ubuntu Budgie 10.10.2 over labwc 0.9.3, 2026-09-09, `busctl
  --user list --acquired`, recorded whole in
  `tests/fixtures/live/busnames-resolute-budgie-10.10.2.txt`): `org.gnome.Shell` is owned
  there by **budgie-power-dialog**, pid 2697, and no `org.gnome.Mutter.*` name is on that
  bus at all, so before this rule every window command on Budgie answered with a bridge
  hint about logging out of a GNOME Shell that was not running. That reading **corrects
  `recon2/budgie.md` §2 and §6**, which list "no `org.gnome.Shell`" among Budgie's
  absences. The GNOME arm is still never swallowed where nothing under it could answer: a
  session owning `org.gnome.Shell` whose compositor offers neither toplevel family still
  gets the bridge hint, because that is the shape a real GNOME session has. Without the
  bridge name but with `org.gnome.Shell`
  owned the constructor diagnoses (locked screen, disabled/broken extension, or
  "run `gnome/install-bridge.sh` and restart the session") without touching
  `org.gnome.Shell.Eval`; only `WDOTOOL_GNOME_AUTOLOAD=1` makes it try one Eval
  to load the installed extension first (works in unsafe mode only). Bridge gone
  mid-session (extension disabled, lock screen) → one clear error. The udev rule
  is `uaccess` only (seat user's ACL, node stays `root:root 0600`; no group).
- **wlr**: `zwlr_foreign_toplevel_management_unstable_v1` via `wayland_mini`. IDs:
  1000000 + enumeration order. list/activate/close/fullscreen/minimize only; geometry
  unknown (0,0 + output size); move/resize → CmdError.
- `search`: `re.search`, case-insensitive like xdotool's `REG_ICASE`, on title
  (`--name`) and on the class (`--class`; `--classname` takes the WM_CLASS instance
  where there is one). The class of a native toplevel is Mutter's `wm_class` and only
  then `gtk_app_id`, which is the opposite order from the app id `wwmctl -lx` prints;
  the two differ on Ubuntu 24.04's `gnome-terminal` and agree on 26.04's Ptyxis, and
  `wm_class` is the X-parity answer ([WWMCTL.md](WWMCTL.md) says which is which);
  `--pid`, `--all/--any`, `--limit N`, `--onlyvisible`, `--sync`. Writes `ctx.stack`.
  Exact output formats for search/getwindowgeometry/getmouselocation (+ `--shell`
  variants): copy from the manpage and `cmd_*.c`.
### Hyprland

Hyprland is detected from its own IPC socket, above the D-Bus checks (a Hyprland session
owns neither `org.kde.KWin` nor `org.gnome.Shell`), and gets a first-class backend rather
than the generic wlroots backend. What changes against the generic backend, all measured on 0.53.3:

* **window ids are stable.** On the generic backend two foots listed as `0x000f4240`/`0x000f4241`
  and closing the first renamed the survivor; here the id is a hash of the compositor's
  `address` and is the same in two processes.
* `getwindowgeometry`, `windowmove` and `windowsize` are real. The generic backend reported the
  whole output for a window the compositor had at `at: 22,61 size: 1876,997`.
* a tiled window refuses an absolute move or resize (`setfloating it first`), as on sway.
* `windowstate`: FULLSCREEN, the MAXIMIZED pair (both axes at once, a lone axis is
  refused) and STICKY (`pin`, floating windows only).
* `wwmctl -d`, `-s`, `-r -t` work: workspace N+1 is desktop N, and a special
  (scratchpad) workspace is -1.
* `getmouselocation` is the compositor's own cursor (`j/cursorpos`), 0 px error measured
  twice.
* `getdisplaygeometry` and `wwmctl -d`'s work area are **logical** pixels: `j/monitors`
  publishes the MODE's size, so a head at transform 1/3/5/7 is measured the tall way
  round and every head is divided by its `scale` (the recorded scale-2.0 HEADLESS is
  960x540 of layout). The same rule wxrandr reads the row with.
* XWayland windows get their real X id and `WM_CLASS` pair, joined to
  `_NET_CLIENT_LIST` on pid, class, title and geometry.

Three things are **not yet** here, each with its route and its cost.
`windowminimize`/`windowunmap`/`windowmap`: Hyprland has no minimize, and the route is a
special workspace to stash the window in over the IPC we already speak (route 2), at the
cost of the bookkeeping to bring it back and of keeping the window in the listing while it
is stashed. `windowlower`: `alterzorder bottom` over that same IPC (route 2), and it has
been run, `alterzorder top,address:0x...` on a floating window answers `ok`, but
`j/clients` publishes **nothing** that orders windows front to back (the whole key list is
`address at class contentType floating focusHistoryID fullscreen fullscreenClient grouped
hidden inhibitingIdle initialClass initialTitle mapped monitor pid pinned pseudo size
swallowing tags title workspace xdgDescription xdgTag xwayland`), so the cost is sending
it unverified, and `raise_()` keeps `focuswindow` for the same reason. `windowstate`
`MAXIMIZED_VERT`/`_HORZ` alone, and any state with no dispatcher: one dispatcher each in
Hyprland (route 6). `selectwindow` waits for the next focus as it does on sway; xdotool's
click-to-pick would be evdev for the press (route 4) plus `j/cursorpos` and `j/clients`
for the hit test (route 2).

**The layout on the uinput path types byte-exact, at route 2.** On Hyprland XKB state is
per device, so the session's group and the injected device's group are two questions.
`xkbmap.HyprLayouts` reads the PHYSICAL keyboard's group, which is right about the session,
and `keys explain` and every other reader still answer that. But it is the wrong group for
`type`, because wdotool's own device is a fresh keyboard to Hyprland with layout state of
its own. The diagnosis, `resolute-hypr` / Hyprland 0.53.3, 2026-09-09, `kb_layout = us,de`,
after `hyprctl switchxkblayout at-translated-set-2-keyboard 1`:

```console
$ hyprctl -j devices     # abridged
at-translated-set-2-keyboard  active_layout_index 1  German
wdotool-virtual-keyboard      active_layout_index 0  English (US)
$ wdotool keys explain --chars z
layout: German -- group 2 of 2
$ wdotool type "zy@ Strasse"
zyq Strasse
```

The `@` was encoded as the German AltGr+Q and landed in the injected device's US group as
a plain `q`. `xkbmap.HyprLayouts`' fourth rule (`_group_of_our_device`) closes it: the one
process that holds `/dev/uinput` open -- the daemon, and nothing else in this tree -- puts
ITS OWN device into the group it is about to encode for, over the socket it is already
holding (route 2), then encodes. Our device alone moves, so there is nothing of the
session's to put back, and the session keeps the group the user chose; `keys explain` still
answers about the SESSION. The request is the bare `switchxkblayout wdotool-virtual-keyboard
<n>` and NOT a dispatcher: the `dispatch switchxkblayout ...` spelling shipped first,
answered `Invalid dispatcher`, and typed `yz@ Strae` (the ASCII right off the fallback, the
sharp s dropped), while the bare form answers `ok` with the new index in the very next
`j/devices`. When the switch does not take, the fallback is the group our device really is
in -- what wdotool typed correctly with before any reader existed. Measured green,
`wdotool type "yz@ Straße"` byte-exact on `resolute-hypr` 0.53.3 (2026-09-14, CI) and
`arch-hypr` 0.56.2 (2026-09-11), the live `want "German types byte-exact after a switch on
the physical keyboard"` in vm/live-smoke.d/hypr.sh. On the virtual-keyboard path the
question is moot, because wdotool uploads its own keymap.

### Wayfire

* Wayfire's JSON IPC is **opt-in**. `[core] plugins` in `~/.config/wayfire.ini` REPLACES
  the default list rather than adding to it, and every IPC method comes from a loaded
  plugin. `plugins = ... ipc ipc-rules` is the minimum: with it the socket answers 23
  methods, every `window-rules/*` read and write, plus `wayfire/*` and `input/*`.
  Without `ipc-rules` the table is `["list-methods"]` alone, and wdotool says so by name
  instead of failing one command at a time:
  `wayfire backend: this Wayfire's IPC has no window-rules/list-views: the window-management backend is
  Wayfire's own IPC (AGENTS.md route 2) and it is one config line away -- `plugins = ipc
  ipc-rules` in wayfire.ini on Wayfire 0.9 or newer -- at the cost of restarting Wayfire
  to load it`. That is deliberately not a *not yet*: nothing of ours is missing, so the
  message identifies the existing IPC interface and the configuration it needs. It is reserved for
  the one thing it describes: a compositor that accepts the connection and then says nothing, or
  one that closes it, keeps its own line and is never reported as a configuration mistake.
* Three more plugins buy three more capabilities, each refused by name when it is
  missing: `wm-actions` (minimize, fullscreen, sticky, always-on-top, lower), **not** in
  Wayfire's stock plugin list, `grid` (maximize) and `vswitch` (desktops). `stipc` adds
  the Xwayland display for the X-id half.
* **Wayfire has a real `windowlower`**, which sway has never had
  (`wm-actions/send-to-back`).
* **Wayfire has no plain raise.** `windowraise` focuses the window, which raises it inside
  its layer; `set-always-on-top` is a state, not a raise, and is deliberately not used for
  one.
* `windowstate --add MAXIMIZED_VERT --add MAXIMIZED_HORZ` is one `grid/slot_c`; a single
  axis is **not yet**, and the route is `window-rules/configure-view` on the same IPC
  (route 2), at the cost of a saved rectangle to restore, because a geometry is not a
  state and the view would not report itself maximized. The grid plugin's slots are halves
  of the screen and not axes (`grid/slot_t` is the top half, `tiled-edges` 13), so there
  is no per-axis maximize to express.
* `windowstate --toggle ABOVE` is refused: Wayfire takes always-on-top and never reports
  it back (the view record's `layer` stayed `workspace` on both sides of the call), so
  there is nothing to toggle from. `--add` and `--remove` work. A Wayfire that put
  always-on-top in the view record would fix it, which is route 6.
* `getmouselocation` answers from the compositor
  (`window-rules/get_cursor_position`) **before any move**, the one thing no other
  wlroots backend can do. Measured live: `x:960 y:540`, the compositor's own
  `960.0, 540.0` to the pixel, at scale 1, which is the only scale the rig has measured.
* `wxprop -spy` works here, and so does wdotool's own `selectwindow`:
  `window-rules/events/watch` is mapped onto the `(id, change)` vocabulary
  `WindowBackend.events()` documents (`view-mapped` → new, `view-unmapped` → close,
  `view-focused` → focus, `view-title-changed` → title, `view-geometry-changed` → move,
  `view-fullscreen` → fullscreen_mode). `selectwindow` still waits for the next window to
  take focus, as on sway: Wayfire's IPC has a cursor position but no picker and no way to
  grab a button press from outside the compositor, so the click-to-pick route is evdev for
  the press (route 4) with the hit test ours.
* `windowsize` is exact only to the client's own quantisation. `wdotool windowsize <foot>
  800 600` reads back `798x598` and `wwmctl -e 0,10,20,300,200` reads back `300x195`,
  because a terminal commits the nearest whole character cell. X11 has no single answer to
  be equal to: `xdotool windowsize 800 600` on an `xterm` read back `800x600` under no
  window manager and under xfwm4, and `796x589` under openbox and marco, which honour the
  `WM_NORMAL_HINTS` increments, and under openbox the POSITION moved too (`100,100`
  asked, `102,140` read back). On Wayfire the position is exact to the pixel.

### Cinnamon

* **Nothing to install**: `org.Cinnamon.Eval`, one constant JS program per operation, with
  only integers ever interpolated into one.
* **Shading works** (`windowstate --add SHADED`): muffin kept `shade`/`unshade`/
  `is_shaded` where mutter dropped shading and KWin 6 removed it, and
  `_NET_WM_STATE_SHADED` is in muffin's `_NET_SUPPORTED` too. `wxprop -id` does not print
  it back yet, though, where the real `xprop` on a Cinnamon X11 session does, **not yet**,
  one atom in `hacks/property/core.py`.
* `SKIP_TASKBAR`, `SKIP_PAGER` and `MODAL` warn and succeed, as on GNOME; `BELOW` is
  refused by name. All four are **not yet**: for an XWayland window `wwmctl` already sets
  them on the X11 connection (route 5), and for a native one it is one setter each in muffin
  (route 6).
* **A resize is asynchronous**: a native Wayland client takes the new size when it acks
  the configure, so `windowsize` polls `get_frame_rect()` for up to half a second and
  answers when it has landed.
* `-spy` / `events()` is **polled**, one list diff every 250 ms. Eval has no signal
  route: a program run through it cannot register a D-Bus object, and `org.Cinnamon`
  publishes no window signal. A window that is already focused in the poll that first sees
  it gets both `new` and `focus`, the way sway sends them, because the rising edge a later
  poll would look for has already gone by.
* `selectwindow` **works** (route 2): `global.stage.grab` does not exist in muffin's Clutter
  (`global.begin_modal` does, and is a keyboard grab, not a click), so the picker is a reactive
  full-stage Clutter actor pushed in through `org.Cinnamon.Eval`, which installs nothing. It
  records the next button press into `global.__w11_pick`; wdotool polls Eval for it and
  hit-tests the coordinates against `list()`, and a Ctrl-C or the 60 s timeout tears the actor
  down (its own `GLib` timeout tears it down even if wdotool dies holding it — the cost this
  rung carries). Measured on resolute-cinnamon-wayland 2026-09-14: install answered `ok`, a
  QMP-injected left click at ~520,360 came back `[519,359,1]`, destroy answered `ok`. Shipping
  the same actor as a Cinnamon extension would be route 3 and only buys surviving a restart. One
  divergence from xdotool: a click on the desktop or a panel (no managed window under it) hit-tests
  to nothing and returns `0`, where `xdo_select_window_with_click` would return the root/desktop
  window itself; `wwmctl -l` does list a `Desktop` window, so hit-testing against it too would
  match X, at the cost of every empty-space pick resolving to the desktop.
* No `--vkbd`: Cinnamon 6.4 advertises no virtual-keyboard protocol, so input is
  `/dev/uinput`, i.e. the udev rule or root. Muffin master (the 6.6/6.8 line) adds
  `virtual-keyboard-unstable-v1` and `wlr-layer-shell`, so a future Cinnamon gets a
  privilege-free keyboard path; there is still no virtual pointer, no foreign-toplevel and
  no output management there.

### The wlroots floor, and COSMIC

The `wlr` backend is what answers on labwc, Budgie, Xfce-on-Wayland, LXQt-on-Wayland and
river, and on sway or Wayfire when `WDOTOOL_BACKEND=wlr` forces it. What it can and cannot
do is `zwlr_foreign_toplevel_management_v1`'s shape and is stated as such:

* `windowmove`, `windowsize`, `windowraise` and `windowlower` on an **XWayland** window go
  through the X plane — AGENTS.md route 5, a real `ConfigureWindow`, exactly what `xdotool`
  sends. Measured 2026-09-14: on labwc 0.9.3 `windowmove 300 200` lands (`300,200`),
  `windowsize 640 400` lands (`640x394`, one character cell), and `windowraise` does not
  restack (the wlroots xwm drops the stack mode); on cosmic-comp 1.8.0 the resize lands and
  the move is a no-op (the Smithay xwm drops it), byte for byte what `xdotool` gets there.
  A **native** toplevel refuses by naming the protocol,
  `zwlr_foreign_toplevel_management_v1 carries no geometry and no stacking` — not by
  borrowing sway's tiling excuse, which is wrong for labwc, a stacking compositor. **Not
  yet** for the native half, route 6, a patched compositor (`wlroots xwayland/xwm.c` drops
  `XCB_CONFIG_WINDOW_STACK_MODE`, so even the XWayland `windowraise` needs it to restack).
* Native-window geometry was `0,0` plus the output rectangle — no foreign-toplevel protocol
  carries a rectangle — and is now the compositor's on-screen rectangle wherever the compositor
  emits one. On the **labwc family** (labwc, Budgie, Xfce-on-Wayland, LXQt-on-Wayland, all of
  which run labwc) this is **route 6, landed** — our own code loaded into an UNMODIFIED labwc from
  the outside, not a fork: the `w11-labwc` session entry preloads `w11-labwc-shim.so`
  (`packaging/labwc-shim/w11-labwc-shim.c`) into the stock compositor with `LD_PRELOAD`, and labwc
  and libwlroots stay bit-for-bit what the distro ships. The shim interposes libwlroots'
  `wlr_scene_xdg_surface_create` (which ties a scene node to each xdg_surface — the correlation
  labwc keeps in its own `struct view`) and `wlr_scene_output_build_state` (per composited frame),
  and writes each toplevel view's on-screen box — read with `wlr_scene_node_coords` and
  `xdg_surface->geometry`, plus the client pid — to `$XDG_RUNTIME_DIR/w11-labwc-geometry`, rewritten
  whenever the scene changes (map, move/resize, unmap, title/app_id, so a window that renames itself
  keeps its rect); `WlrBackend._labwc_geometry` folds it onto the native rows by (app_id, title),
  the pair `match_xids` pairs the X plane on. Measured on the resolute-labwc golden 2026-09-15 (a
  1280x800 head): a native `foot` labwc placed at `292,166 696x494` read `0,0 1280x800` out of
  `wdotool getwindowgeometry` with stock labwc 0.9.3 and `292,166 696x494` with the shim preloaded
  (`getwindowpid` went from no pid to the client's `6354` on the same join; a QMP screendump
  confirmed the rectangle). On **river** the lowest reachable rung is **3** — a
  `river_window_manager_v1` WM client that reads `river_window_v1.dimensions` for the size and the
  position it set with `river_node_v1.set_position` — not landed this pass. A compositor without the
  shim (a plain labwc session, or sway/Wayfire forced onto this backend) writes no file and every
  native row stays the honest floor with `geometry_is_floor` set. Upstreaming the shim's emit into a
  foreign-toplevel protocol that carries a rect (which wlroots would have to write and ship) is what
  turns the labwc row into route 1 later.
* Desktops work where the compositor publishes `ext_workspace_manager_v1` (labwc, Budgie
  10.10, Xfce 4.20 on Wayland) and the refusal stands where it does not (sway 1.11,
  Wayfire 0.10), route 1 where the compositor grows the protocol, else route 2, the
  compositor's own IPC, which is one backend per compositor. `window_desktop` stays -1 on
  every one of them, because neither foreign-toplevel protocol carries a workspace
  association.
* `windowstate` SHADED/ABOVE/BELOW/SKIP_* on an **XWayland** window is the `_NET_WM_STATE`
  ClientMessage `wmctrl -b` sends, over the X plane (route 5). Measured 2026-09-14: on labwc
  0.9.3 all of `add,{above,below,shaded,skip_taskbar,skip_pager}` land in `_NET_WM_STATE`
  and read back on, `remove` takes them out; on cosmic-comp 1.8.0 they are dropped by the
  Smithay xwm, byte for byte what `wmctrl -b` gets there — rc 0 either way, no visible
  restack. A **native** toplevel is **not yet**, route 6: the handle's state array has
  exactly maximized, minimized, activated and fullscreen, so it is one state bit and one
  request in a patched compositor (`wlroots xwayland/xwm.c`'s `xwm_handle_net_wm_state_message`
  has no `request_above`/`below`/`shade` to hand labwc). `FULLSCREEN` against a v1 manager is
  route 1, version 2 of the protocol, which it already defines: a newer compositor build and
  no code of ours.
* `windowminimize` on a compositor with no minimized state (sway forced onto this backend,
  river-classic 0.3.17) warns after waiting 0.5 s for the handle to say otherwise. On sway
  the route is 2, its own scratchpad, which the sway backend already takes.
* **river 0.4 accepts every one of these and does nothing**, and the backend says so
  rather than believing it: see README footnote **(n)**.
* The backend's own precondition says the same kind of thing: `wlr backend: compositor
  does not offer zwlr_foreign_toplevel_management_unstable_v1; not yet here, and the route
  is that protocol where the compositor grows it (route 1), else a backend over the
  compositor's own IPC (route 2), which is what the sway, hypr and cinnamon backends
  already are`.
* `windowactivate` takes a `wl_seat` and the backend will not send a null one, so a
  registry with no seat gets `compositor offers no wl_seat; ... not yet here, and the
  route is a registry that carries one (route 1), else the compositor's own IPC where it
  has one, which focuses a window by id and needs no seat at all (route 2)`. COSMIC's twin
  says route 6 instead, because the COSMIC request names a seat in its own signature.

COSMIC is its own backend on its own protocols, one tier above the floor: real workspaces,
ids minted from the 32-character `identifier`, and activate/close/maximize/minimize/
fullscreen gated on `zcosmic_toplevel_manager_v1`'s capability array. No move, no resize,
no raise, no lower, `set_rectangle` is a minimise hint, and no pid, so `kill` answers
`no pid for window N`. `FULLSCREEN` and `STICKY` are refused where cosmic-comp's capability
array is `[1,2,3,4,6]` and carries neither 5 nor 7: the request works anyway, and the array
decides, because a compositor that advertises a capability it does not have is the case
worth reporting. `set_desktop_for_window` sends `move_to_ext_workspace`, gated on
capability 6, because cosmic-comp's handler for the deprecated `move_to_workspace` is an
empty arm while its capability array still advertises 6 rather than 8. Geometry is the
output rectangle, silently, where the `geometry` event never arrives, which is the only
COSMIC session ever measured, so a warning there would be one line of stderr on every
window command (`CosmicBackend.geometry_is_floor` is the flag for a caller that wants to
say so). A **sandboxed** COSMIC client gets none of this: cosmic-comp builds the toplevel
list, the info and the manager behind `client_not_sandboxed`, so a Flatpak or snap wdotool
is announced none of them; the route is an unsandboxed run of the protocol that is already
there (route 1), and on a compositor that really has neither it is a backend over its own
IPC (route 2).

### What the base class answers, and for whom

Two gaps live in `hacks/window/backend.py:WindowBackend`'s defaults rather than in any one
backend: a backend reaches them by not overriding the method. Measured 2026-09-14 against
the eight backend modules, gnome and kwin reach neither. `selectwindow` and `events()` used
to sit here for the wlr and cosmic floors too; both now come off the foreign-toplevel
protocol's own changes (`XPlaneViews.events`/`select_window`, route 1), so no shipped backend
reaches those two defaults any more.

| command | which backends land here | not yet, with its route and cost |
|---|---|---|
| `set_num_desktops`, `wwmctl -n` | sway, wlr, cosmic, hypr, cinnamon, wayfire | counting workspaces into existence. Route 1 on wlr and cosmic, the same `ext_workspace_manager_v1` `set_desktop` already drives, whose group creates a workspace and whose handle removes one; route 2 on sway, hypr, cinnamon and wayfire, each compositor's own IPC or bus. The cost is honouring the per-workspace capability that gates both, and a count that does not read back where a compositor drops a workspace as soon as it empties |
| `set_desktop_for_window` | wlr | binding a toplevel to a workspace. The handle has no such request and `ext_workspace_manager_v1` names workspaces without taking windows, so the route is a protocol that does both, cosmic-comp ships `move_to_ext_workspace` and the COSMIC backend already sends it (route 1), else a patched compositor (route 6) |

### i3

`WDOTOOL_BACKEND=sway` is also i3's spelling (`i3` is an alias of it; the dialect is
detected off `GET_VERSION`, not off the variable), and on i3 the backend answers what it
used to get wrong. None of it is what an i3 user gets by default, the four tools hand over
to the originals there, so this section is about `W11_PASSTHROUGH=never`.

* every id is the window's **X id**, the same number `wmctrl -l`, `xwininfo` and
  `xprop -id` use. i3's own container ids are 47-bit pointers; the one this printed
  truncated to `0x5168c680` and `wxprop -id` answered `BadWindow`.
* `windowmove` / `windowsize` work on a floating i3 window. They used to refuse every
  window as tiled, because i3 wraps a floated view in a `floating_con`. Measured live:
  `2640,398` → `125,159`.
* `getwindowpid` answers from `_NET_WM_PID`; i3's tree carries no pid at all.
* `search --onlyvisible` matches: "visible" on i3 means "on a workspace `GET_WORKSPACES`
  calls visible", which excludes both the scratchpad and every workspace that is not the
  one shown on its output.
* `getdisplaygeometry` answers (`3840 1080` on the measured two-head session) instead of
  exiting 2 with "no Wayland session found": when the daemon's `wl_output` query has
  nothing to read, the window backend is asked before the refusal. The refusal is
  unchanged when there is no backend either.
* the four `sway:`-prefixed refusals say `i3` on an i3 session. They used to name a
  program the user was not running.
* **the one difference left, stated rather than fixed**: desktop numbers. i3's own
  `_NET_WM_DESKTOP` is dense by position; ours is `workspace number - 1`, so on a session
  whose only workspace is number 2, `wwmctl -d` says 1 where `wmctrl -d` says 0. And
  `wwmctl -d` differs from `wmctrl -d` in the geometry columns too: i3 publishes no
  `_NET_DESKTOP_GEOMETRY` and no `_NET_WORKAREA`, so the original prints `DG: N/A ... WA:
  N/A` and we print the workspace rect.

### Reloading the bridge

On GNOME the bridge is an extension, and "log out and back in" is only half the story.
Measured on `noble-gnome-x11` (GNOME Shell 46.0, mutter 46.2, 2026-09-09) and true on
Wayland too:

* An extension the running shell already knows can be taken out and put back **in that
  process**: `gnome-extensions disable` releases `org.w11.Bridge` (`NameHasOwner`
  → `(false,)` about 3 s later), `enable` takes it back (about 4 s later), and
  gnome-shell's pid never changes. That is what the package's autostart uses.
* The logout is for a shell that has never **scanned** the extension directory, i.e. a
  fresh install.
* A bridge whose `extension.js` has **changed** is re-read with no logout too, and that
  is **route 3, code we install into the compositor**. Route 2, the compositor's own
  scripting surface, has the method and refuses: `org.gnome.Shell.Extensions.ReloadExtension`
  answers `NotSupported: ReloadExtension is deprecated and does not work` on 46. So the
  extension reloads itself. The object gnome-shell constructs at login is a **ROOT** that
  keeps the reload interface -- `org.w11.BridgeReload1.Reload` on `/org/w11/Bridge`,
  deliberately its own interface and not a method of `org.w11.Bridge1`, the client surface
  the tools and `MockBridge` are pinned to -- and hands the session to a **copy** of the
  file that runs `org.w11.Bridge1`. `Reload` re-reads `extension.js`, and because GJS
  re-reads a module only under a URL it has not imported yet, it writes the bytes into
  `$XDG_RUNTIME_DIR` under a name keyed by the file's mtime **and** the SHA256 of its bytes
  (the clock alone is not evidence: two saves in one second move neither, see `_read()`),
  imports that, and swaps the session onto a fresh instance. The reply is JSON --
  `{"version", "mtime", "reread", "path"}` -- so a caller tells a reload from a no-op.
  The costs are three. A split in two. A heap that only grows: GJS unloads no module, so
  every reload leaves the old copy's bytes behind and its `disable()` -- the same one the
  shell calls -- has to drop every signal and timeout by hand, or a stale listener fires
  for the rest of the session; `tests/test_bridge_js.py`
  (`test_a_reload_leaves_no_listener_no_timeout_and_no_export_behind`) counts the handlers
  on the doubles after three reloads and finds them unchanged. And two things a `Reload`
  cannot help with: `org.w11.Bridge` is unowned for the milliseconds between the old copy's
  `disable()` and the new copy's `enable()`, so a client that dials in that window gets
  `NameHasNoOwner` (the same hole `gnome-extensions disable` then `enable` always had); and
  the **ROOT**'s own code -- `enable()`, `disable()`, `_install()`, `_swap()` and the
  route-3 block that carries them -- is the copy the shell read at login, so editing *that*
  still needs a new session. Everything under `_enableHere()` is what a reload picks up.
  Measured by the committed recording, which CI replays --
  `tests/fixtures/live/noble-gnome-x11-46.0-install-passthrough-bridge-display-root-uinput-replay.txt`,
  `noble-gnome-x11`, GNOME Shell 46.0, gjs 1.80.2: a `Reload` with nothing edited answers `"reread":false`
  and reads no file; the phase then edits the shipped `const VERSION = 3` to `99`, and a
  `Reload` answers `{"version":99,...,"reread":true}` about a second later while `GetVersion`
  then answers `(uint32 99,)`; three `touch`es to three future dates each re-read; and
  `pgrep -x gnome-shell` reads the same pid across all of it (the file is put back to
  version 3 at the end). One reading is by hand rather than recorded: ten reloads cost
  gnome-shell about 996 kB of RSS, ~100 kB a copy, the module GJS cannot unload. Route 6,
  a package of ours that patches the shell or its unit, stays unwritten and is not needed.

**Do not run `gnome-shell --replace` to reload it.** On a systemd-managed GNOME session
that is not a reload, it is a way to lose your extensions:
`/usr/lib/systemd/user/org.gnome.Shell@x11.service` is `Restart=always`, `RestartSec=0ms`,
`RefuseManualStart=on`, `RefuseManualStop=on`, with
`OnFailure=org.gnome.Shell-disable-extensions.service`. A hand-run `--replace` makes the
unit's shell exit, systemd restarts it instantly, the hand-started one still holds the WM
selection, and after four rounds the unit goes `failed (Result: protocol)`. That OnFailure
unit runs `gsettings set org.gnome.shell disable-user-extensions true`, a **persistent**
dconf key, so afterwards the bridge reads `Enabled: No / State: INITIALIZED` in every later
session of that user. It does not come back on a reboot, on `gnome-extensions enable`, or
on deleting `/run/user/1000/gnome-shell-disable-extensions`, all three tried. One thing
recovers it, live and with no logout:

```sh
gsettings set org.gnome.shell disable-user-extensions false
```

after which the shell loads the extension and takes the name within four seconds.

### What differs from X on KDE Plasma

The bullets above are the mechanism. This is the same story as a script writer meets
it, one row per command, measured on Plasma 5.27 and 6.6.

| | |
|---|---|
| `windowraise` on 5.27 | KWin 5.27 has no per-window raise. The window is activated instead (which focuses it), and says so on stderr. Plasma 6 raises properly |
| `windowlower` | neither release has a per-window lower: the active window is lowered for real, any other is marked keep-below, with a warning |
| `windowstate SHADED` | works on 5.27, **for X11 windows only**, because KWin shades nothing else, and says so. Plasma 6 removed window shading, so it is a clean "not supported" there |
| `windowstate MAXIMIZED_*` on 5.27 | KWin 5.27 exposes no `maximizeMode` to scripts, so a window is read as maximized when its frame fills the maximize area to within one size increment (at least 32px per axis). A merely large window therefore reads as maximized, and `--remove MAXIMIZED_*` cannot clear that reading |
| `set_num_desktops` | KWin caps virtual desktops (20 on 5.27, 25 on 6) and keeps at least one. Asking for more is capped at that with a warning, not an error |
| window ids | KWin's only window handle is a UUID, so the printed ids are minted from it (30 bits of it, `0x40000000` to `0x7FFFFFFF`, out of the range Xwayland gives its clients). They are stable while the window lives, and an X id is not accepted in their place. On sway either, where the ids are sway node ids |
| `wwmctl -l -G` positions | on Plasma 6.6 (and sway) `wmctrl` doubles the frame offset under a non-reparenting WM and ours are the real ones. KWin 5.27's xwm *does* reparent, so both agree there |
| a state KWin ignores | KWin accepts a state a window rule or the client's size hints forbid and does nothing with it. `wwmctl` then sends the EWMH `_NET_WM_STATE` ClientMessage instead, which reaches an XWayland window through KWin's X11 window manager, and checks that one landed too. `wdotool` has no second route and says what happened |
| `selectwindow` | KWin has one reply slot for its window picker, so a second picker started while the first is up takes the click. The first call then waits until `WDOTOOL_SELECT_TIMEOUT` (2 minutes) and says so |
| XWayland ids on Plasma 6 | `x11window.h` lost every scriptable property in 6, so `View.xid` is matched through the X server's own client list: pid and `WM_CLASS` filter, title and geometry score. Where those tie, two windows of one application in the same place under the same title, the order of the two lists decides, and that is exact rather than a guess: `_NET_CLIENT_LIST` is KWin's own window list with everything but the managed X11 windows dropped. A client that publishes neither `_NET_WM_PID` nor `WM_CLASS`, and a pair that nothing at all separates, keep id 0 rather than being handed one of two ids |
| `wxprop -root` | `_NET_CLIENT_LIST`, `_NET_ACTIVE_WINDOW` and `_NET_DESKTOP_NAMES` are ours (native windows included), not KWin's stale X copies |
| `getmouselocation` | answered by KWin (`workspace.cursorPos`), like GNOME's: a mouse moved by hand, or by another process, reads correctly, and the query needs no `/dev/uinput` at all. The X11 tool is not a second opinion here: under KWin's XWayland `xdotool getmouselocation` answered the X root's centre (2880,540 on a three-head rig) wherever the pointer really was |
| `mousemove` to an output's top-left pixel | KWin owns that pixel: a 1x1 screen edge there pushes the cursor back, so `mousemove 0 0` reads `1,1` (6.6 at every output's corner, 5.27 only at a corner no neighbour shares), and on a stock Plasma 6 the same move opens the Overview. Every other pixel of every output is exact, in every layout measured. [Pointer accuracy](#pointer-accuracy) |

A window state that a client applies asynchronously (fullscreen and maximize on a
Wayland client, applied when it acks the configure) is waited for before the command
returns, so `windowstate` never reports a state it merely has not seen land yet, and
the next command sees a settled window.
