<a id="w11-bridge-the-gnome-shell-extension"></a>

# w11 bridge: the GNOME Shell extension

`w11-bridge@w11` is a small GNOME Shell extension that exports
Mutter's window, workspace and monitor state, and the actions on them, over
the session D-Bus. It is what lets `wdotool`, `wwmctl` and `wxprop` work on a
stock GNOME Wayland session (Ubuntu 24.04 / GNOME 46 and Ubuntu 26.04 / GNOME
50, plus everything in between). `wxrandr` and `warandr` never touch it: no
module of theirs names this interface, because monitors go through Mutter's own
DisplayConfig.

## Why an extension

The bridge supplies window-management operations that GNOME's existing client
interfaces do not expose. Mutter does not implement `wlr-foreign-toplevel-management`,
and `ext-foreign-toplevel-list` has neither geometry nor action requests.
`org.gnome.Shell.Introspect` is read-only and restricts callers, while
`org.gnome.Shell.Eval` is disabled unless Shell is running in unsafe mode.

Running an extension inside GNOME Shell provides access to the required Mutter
APIs without enabling arbitrary evaluation. The extension accepts defined D-Bus
requests and returns JSON; the Python clients use the project's standard-library
D-Bus implementation without importing `gi` or spawning `gdbus` for each command.
For background on the interfaces available across compositors, see
[Exploring the Fragmentation of Wayland](https://www.semicomplete.com/blog/xdotool-and-exploring-wayland-fragmentation/).

## Files

```
gnome/
  w11-bridge@w11/
    metadata.json                 uuid, and the shell-version list, written by hand
                                  (45 to 51 today; the other extension's is generated)
    extension.js                  the extension (ESM)
    org.w11.Bridge1.xml   introspection XML (also embedded in extension.js)
  install-bridge.sh               POSIX sh installer / checker / uninstaller (+ --udev)
  60-w11-uinput.rules     udev rule: /dev/uinput for the active seat user (uaccess)
  modules-load-uinput.conf        loads uinput at boot (installed with the rule)
  README.md                       this file
```

The Python side is `hacks/window/backend_gnome.py` (a `dbus_mini` client of this
interface; every backend method maps to one call) and
`tests/test_backend_gnome.py` (a mock bridge on the in-process mock bus).

## Install

```sh
sh gnome/install-bridge.sh            # ~/.local/share/gnome-shell/extensions/<uuid>
sh gnome/install-bridge.sh --system   # /usr/share/gnome-shell/extensions/<uuid> (sudo)
sh gnome/install-bridge.sh --check    # is it loaded? is org.w11.Bridge owned? udev rule?
sh gnome/install-bridge.sh --uninstall
sudo sh gnome/install-bridge.sh --udev  # /dev/uinput for the logged-in user (see below)
```

The script copies the files, adds the uuid to `org.gnome.shell
enabled-extensions` (`gnome-extensions enable`, or gsettings by hand), and then
tries to make it live without a logout:

1. If the running shell already knows the uuid (`ListExtensions` lists it, true after any login with the files in place) it calls `EnableExtension`
   and the bridge is up immediately.
2. Otherwise you will be told to **log out and back in once**. gnome-shell
   scans extension directories only at login and, on Wayland, cannot be
   restarted in place (Alt+F2 `r` only ever worked on X11 and is gone in 50).
   The extension is already enabled in gsettings, so it comes up by itself
   after the re-login.
3. `--try-unsafe` is the no-logout escape hatch: it drives Looking Glass
   (Alt+F2, `lg`, `global.context.unsafe_mode = true`) through `wdotool`'s
   input injection, loads the extension with `org.gnome.Shell.Eval`, enables
   it, and switches unsafe mode off again. It needs `wdotool` with working
   uinput access (root, or the udev rule from the main README), an unlocked
   session and five seconds of not touching the keyboard. It is best-effort by
   nature; if it fails nothing is left behind except the files and the
   gsettings entry, and the next login finishes the job.

Running the installer through `sudo` is fine: it figures out the desktop user
from `$SUDO_USER`, chowns the files to them and does every shell/gsettings
call as that user on `/run/user/<uid>/bus`.

Ubuntu ships `org.gnome.shell disable-user-extensions = false` and
`allow-extension-installation = true`; `--check` prints both in case a site
policy changed them. Upgrading an already-loaded extension still needs a
re-login (the ESM module cache is per process).

**The extension changed in this release** (`extension.js`, `metadata.json`):
`SetState` folds both maximize axes into one Mutter call (bridge v3, below);
`SelectWindow` is a new implementation, a stage grab resolved by a button
press, and it refuses, rather than grabs, when another picker is running or
the shell is already modal. Reinstall and log back in; nothing else in the
interface moved.

**An already-installed bridge must be reinstalled for v2.** `SelectWindow`
became a click-to-select picker in bridge version 2 (it waited for a focus
change in v1), so an installed v1 has to be replaced: re-run
`sh gnome/install-bridge.sh` **and log out and back in once**, copying the
files over a loaded extension does not reload it. `--check` prints the
running bridge's version; `wdotool selectwindow` refuses to run against a v1
bridge and says this, rather than hanging on the focused window.

**Bridge v3 changed what `SetState` does with the maximize pair.** `MAXIMIZED`
under `toggle` now follows the horizontal flag, the way Mutter itself reads
the two atoms of one `_NET_WM_STATE` message; v1 and v2 asked for "both axes
are already set". Nothing gates on the version, `MAXIMIZED` is accepted by
every bridge that has shipped, and `add`/`remove` of the pair (the corrupted
restore size described under `SetState`) are fixed against a v2 bridge too, so a stale bridge only differs on the toggle. Reinstall and log back in to get
the whole fix.

Debugging: `journalctl --user -f -o cat _COMM=gnome-shell | grep -i
w11`. Enable/disable, bus-name changes and unexpected (`.Failed`)
errors are logged at message level and always show up; expected errors
(`NotFound`, `InvalidArgs`, `Unsupported`) and per-call tracing are at debug
level and need `G_MESSAGES_DEBUG=all` in the shell's environment. `--check`
prints the extension state and any load error.

## The other extension: w11-overlap

`w11-overlap@w11` is a **second** extension in this directory, and it
is not the one above. Everything above is the bridge: feature-detected JavaScript
against public API, GNOME Shell 45 to 51, needed by `wdotool`, `wwmctl` and `wxprop`,
safe to install and forget. The overlap extension is a different kind of thing and is
kept apart from it on purpose, its own uuid, its own installer
(`sh gnome/install-overlap.sh`) and its own **separate enable step**. The package
carries its files, and carries nothing that turns it on: no autostart entry enables it
the way one enables the bridge, so on a machine installed from the .deb it sits in
`/usr/share/gnome-shell/extensions` doing nothing until somebody enables it by hand
(`gnome-extensions enable w11-overlap@w11`). Log out and back in first if the
package was installed during the current session, so Shell discovers the directory.
Enable it only when you need overlapping monitor positions; the other tools do
not require it.

### Install, check and remove

```sh
sh gnome/install-overlap.sh          # exit 1 until you log out and back in on first install
sh gnome/install-overlap.sh --check  # run the compatibility probe without applying a layout
sh gnome/install-overlap.sh --uninstall
```

Unlike `wxrandr --gnome-overlap-status`, the installer's `--check` runs the extension's
probe, including the private-structure checks. It does not change the visible layout;
the sentinel write uses a temporary configuration object.

The compiled typelibs are included in the tree, so users do not need a compiler.
Maintainers regenerate them with `python3 gnome/overlap-typelib/gen-gir.py` and use
its `--check` mode to detect stale generated files. See
[adding a GNOME generation](../docs/Technical.md#the-table-and-adding-a-gnome-generation)
for the generation table and measurement requirements, and
[the bus interface](../docs/Technical.md#the-bus-interface-request-by-request)
for the probe and apply requests.

### Scope and reference

The bridge uses public Shell APIs. The overlap extension uses a compiled description
of private Mutter structures and supports only builds that pass its compatibility
checks. It can end the session if an incompatible structure evades those checks.

For the command flags, supported operations, saved acknowledgment and build checks,
use [WXRANDR.md](../docs/WXRANDR.md#--unsafe-gnome-overlap-the-one-route-through).
For the GUI warning and its checkbox, use
[WARANDR.md](../docs/WARANDR.md#overlapping-monitors-on-gnome). The structure layouts,
measurements and recovery procedures are maintained in
[Technical.md](../docs/Technical.md#why-mutter-refuses-monitors-that-share-area).

An acknowledgment applies to the measured GNOME build; it does not bypass checks
and is invalidated by a library update. Keep recovery instructions available before
using the extension. To disable it, run
`gnome-extensions disable w11-overlap@w11`; see the technical reference if the
graphical session is unavailable.

## Security note

Installing the bridge grants **every process that can reach your session
bus**, which includes a sandboxed app allowed to talk to `org.gnome.Shell`,
because the object answers there too, the ability to

1. read the title, class, pid, geometry, workspace and app-id of every
   window, which stock GNOME withholds by sender-allowlisting
   `org.gnome.Shell.Introspect`;
2. move, resize, restack, close and **SIGKILL** any window;
3. learn `DISPLAY` and the path of Mutter's Xwayland auth cookie;
4. take the shell's modal input grab for the length of one window pick
   (bounded at 30 s, and followed by a quiet period as long as the grab it
   just held, so a caller in a loop cannot keep the session locked out); and
5. confirm a pending display-configuration change, except one that would
   leave no enabled monitor, which is refused because nothing could then
   press Revert.

There is no partial mode and no caller check. That is roughly the situation
on X11, where any client of the display can do all of that and more (read
keystrokes, grab the screen), and it is the deliberate trade: the tools exist
to give scripts that power back. The bridge never evaluates code and never
injects input; input goes through the kernel (`/dev/uinput`), not the shell.

If you do not want any process on the bus to have this power, do not install
the extension, there is no partial mode.

**The overlap extension is a second grant, and a narrower one.** Installing
`w11-overlap` puts `org.w11.Overlap` on the same session bus, so any
process that can reach the bus can ask it to move the logical monitors around and, in
doing so, to run its write. What that caller cannot do bounds the damage: it names
connectors and positions and nothing else, never an address (every address comes from
the extension's own bounded read of the live configuration); the request is refused
unless it is a layout Mutter's validator would reject, so this is not a general
display-configuration API; it cannot change a mode, a scale, a rotation, the primary
or which outputs mirror; and it cannot cause `~/.config/monitors.xml` to be written.
The worst it buys is a session whose monitors are arranged unhelpfully until the next
logout, plus, if the guards are all wrong on that build, a dead `gnome-shell`, which
is the risk the whole feature is about. It is a second installer and a second enable
step precisely so that this grant is a second decision.

## The interface

Bus name `org.w11.Bridge` (the object also answers to
`org.gnome.Shell`, because it lives on gnome-shell's own connection), object
path `/org/w11/Bridge`, interface `org.w11.Bridge1`.
Structured results are JSON strings; every method is wrapped so a broken call
returns a D-Bus error instead of taking the shell down.

```sh
gdbus call --session --dest org.w11.Bridge --object-path /org/w11/Bridge \
  --method org.w11.Bridge1.ListWindows | python3 -c 'import ast,json,sys; print(json.dumps(json.loads(ast.literal_eval(sys.stdin.read())[0]), indent=1))'
```

### Windows

| Method | Signature | Does |
|---|---|---|
| `ListWindows` | `() → s` | JSON array, bottom-to-top stacking order, all managed windows except override-redirect ones. Object layout below. |
| `GetWindow` | `(t id) → s` | one such object; `NotFound` for unknown ids |
| `Activate` | `(t)` | `activate(ts)`: switch workspace, unminimize, raise, focus (xdotool `windowactivate`) |
| `Focus` | `(t)` | `focus(ts)` without raising when the window is showing on the active workspace, otherwise `Activate` (xdotool `windowfocus`) |
| `Close` | `(t)` | `delete(ts)`, polite close |
| `Kill` | `(t)` | `kill()`, Mutter kills the client (works for XWayland and native, as any user) |
| `Minimize` / `Unminimize` | `(t)` | |
| `Raise` / `Lower` | `(t)` | |
| `Move` | `(t, i x, i y)` | `move_frame(true, x, y)`; frame coordinates, logical pixels |
| `Resize` | `(t, i w, i h)` | `move_resize_frame` keeping the frame's top-left |
| `MoveResize` | `(t, i, i, i, i)` | `move_resize_frame` with all four numbers in one call, so a move and a resize cannot race each other across two configures. Exported since v1 and **no client calls it yet**: `wwmctl -e` on GNOME still sends `Resize` then `Move`, which is why a request that moves *and* resizes keeps its new size and its old position. The KWin backend takes the one-call path already (`hacks/window/wmctl.py`, `move_resize`) |
| `SetState` | `(t, s state, s action) → b` | `state` ∈ `FULLSCREEN MAXIMIZED_HORZ MAXIMIZED_VERT MAXIMIZED HIDDEN ABOVE BELOW STICKY DEMANDS_ATTENTION SHADED SKIP_TASKBAR SKIP_PAGER MODAL`, `action` ∈ `add remove toggle`. `MAXIMIZED_HORZ`/`_VERT` are real per-axis operations on every release. **`MAXIMIZED` is not a shorthand for sending the two axis names in a row: it is the only correct way to ask for both.** Mutter unmaximizes to the window's current frame rect and takes only the axis it is unmaximizing from the saved rectangle, so a second single-axis call that beats the client's commit keeps the maximized half and is then saved as the restore size, measured on 46 and 50, `-b remove,maximized_vert,maximized_horz` left a 200,150 900x600 window at 200,32 900x1048. `toggle` of the pair follows the horizontal flag (**v3**; v1/v2 used "both are set"), which is Mutter's own rule for two atoms in one `_NET_WM_STATE` message. Returns `false` (and does nothing) for what Mutter cannot set: `SHADED`, `SKIP_*`, `MODAL`, `BELOW`. Unknown ids still raise `NotFound`. |
| `MoveToWorkspace` | `(t, i index)` | `change_workspace_by_index`; `index < 0` = stick to all workspaces (EWMH 0xFFFFFFFF); `NotFound` for a missing workspace |
| `SelectWindow` | `(u timeout_ms) → t` | **v2**: takes a stage grab and resolves with the window under the pointer at the next button press (`xdotool selectwindow`, including the window that already has focus); `0` when the press landed on no window. Escape, the deadline, a caller that disconnected and a disabled extension all come back as `Cancelled`. `timeout_ms = 0` means "as long as the user takes" and is still capped at 30 seconds, as is any larger value, a grab is never held indefinitely, and a finished selection leaves behind a quiet period as long as the grab it held, so a caller cannot re-arm in a loop (`Unsupported` until it passes). Set your D-Bus call timeout above it (the clients use none). v1 resolved on the next *focus change* instead. |

There is no hit-test method on purpose: `getmouselocation`'s window is
computed client-side from `ListWindows` (`wdotool.backend.hit_test()`, looking
through `DESKTOP`/`DOCK` windows) with the focused-first/topmost rule every
backend shares, so the two cannot drift apart.

Window object (`ListWindows`/`GetWindow`):

```
id                 Meta.Window.get_id() (64-bit, stable for the shell's lifetime; the
                   same number org.gnome.Shell.Introspect uses). Every t argument above.
xid                X11 client window id for XWayland windows, 0 for native ones
title, wm_class, wm_class_instance, gtk_app_id, sandboxed_app_id, desktop_id, role
pid                0 when unknown
client_type        "x11" | "wayland"
window_type        Meta.WindowType name: NORMAL DESKTOP DOCK DIALOG MODAL_DIALOG ...
x y width height   get_frame_rect(): logical pixels, no CSD shadows, SSD frame included
buffer_rect        {x,y,width,height} of the full buffer (shadows included)
focused minimized hidden on_all_workspaces on_active_workspace
                   hidden = !showing_on_its_workspace()
workspace          index, -1 when on all workspaces
monitor            index into ListMonitors
fullscreen maximized_h maximized_v above urgent skip_taskbar decorated
transient_for      id or 0
stable_sequence    get_stable_sequence() (creation counter)
```

### Workspaces

| Method | Signature | Does |
|---|---|---|
| `GetActiveWorkspace` | `() → i` | |
| `SetActiveWorkspace` | `(i)` | `activate(ts)`; `NotFound` beyond the last one |
| `GetNWorkspaces` | `() → i` | with dynamic workspaces this counts the trailing empty one |
| `SetNWorkspaces` | `(i)` | append/remove and update `num-workspaces`; error `Unsupported` while `org.gnome.mutter dynamic-workspaces` is on (the default) |
| `ListWorkspaces` | `() → s` | `[{index, name, active, work_area:{x,y,width,height}, viewport:{x:0,y:0}}]`, what `wmctrl -d` prints |
| `ShowDesktop` | `(b)` | `true`: minimize every normal window on the active workspace and remember them; `false`: unminimize those. Mutter's real show-desktop mode has no public API. |

### Screen

| Method | Signature | Does |
|---|---|---|
| `DisplaySize` | `() → (ii)` | `global.display.get_size()`, the whole layout |
| `GetPointer` | `() → (iiu)` | the real pointer and Clutter modifier mask. Diagnostic only (`GnomeBackend.real_pointer()`, no command uses it): `getmouselocation` reports the daemon-tracked injected pointer by design, and this is how the two are checked against each other |
| `ListMonitors` | `() → s` | `[{index, x, y, width, height, scale, primary, connector}]`; `connector` is filled on GNOME 49+ (`MonitorManager.get_monitors()` + `get_monitor_for_connector()`) and empty on 46 (match on x/y against `DisplayConfig.GetCurrentState` there) |
| `XInfo` | `() → (ss)` | gnome-shell's own `DISPLAY` and `XAUTHORITY`, how a root caller finds Xwayland and its cookie file. When the shell has no `XAUTHORITY`, the newest `$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` is returned instead; `""` means unknown |
| `ConfirmDisplayChange` | `(b keep) → b` | best-effort: finds the "Keep these display settings?" dialog and presses Keep/Revert; returns `false` when no dialog was found (the verdict is still forwarded to Mutter, which ignores it when nothing is pending) |

### Misc, signals, errors

* `GetVersion() → u` and read-only property `Version` (`u`), both `3`.
  (GJS resolves method and property names on the same object, so the method
  could not also be called `Version`.)
* Signal `WindowEvent(t id, s change)` with sway's vocabulary: `new`, `close`,
  `focus` (id `0` = nothing focused), `title`, `fullscreen_mode`, `move`
  (position or size), `urgent`, `workspace` (also when stickiness changes),
  plus `minimized`.
* Signal `WorkspaceEvent(s change)`: `switch`, `add`, `remove`.
* Errors: `org.w11.Bridge1.NotFound` (bad window/workspace id),
  `.Unsupported`, `.InvalidArgs`, `.Failed` (anything else, message included).

## Verified live

Ubuntu 24.04.4 / GNOME Shell 46.0 (gnome-shell 46.0-0ubuntu6~24.04.14, gjs
1.80.2, Xwayland 23.2.6), fresh autologin session, xterm + gnome-text-editor +
gnome-calculator: user install → "log out and back in" → after the reboot
`--check` shows state 1 / name owned / version 1 (the bridge version then;
the picker rewrite made it 2 and the maximize pair 3) and the journal only has
`enabled`/`acquired` lines (no JS ERROR). Through `wdotool`: `search`
(name/class/classname/pid/desktop/onlyvisible), `getactivewindow`,
`getwindow{name,classname,pid,geometry}`, `windowactivate --sync` (switches
workspace, unminimizes), `windowfocus`, `windowmove/windowsize --sync`
(xterm snaps to its cell grid: 640x480 → 640x470), `windowstate`
FULLSCREEN / MAXIMIZED_VERT (per-axis: 500x768 at y=32) / ABOVE / STICKY /
DEMANDS_ATTENTION (SKIP_TASKBAR warns; SHADED and BELOW error), `windowminimize` /
`windowmap --sync`, `set_desktop[_for_window]`, `get_desktop_for_window` (-1
when sticky), `getmouselocation` window hit-test, `windowraise/lower`,
`windowclose`, `windowkill`, `type`/`key` into xterm, `selectwindow`
(bridge v1: returned on the next focus change). As `root` over ssh with no session environment the
same commands work (bus found next to `wayland-0`, dbus_mini's fork auth,
`x_info()` = `(':0', '/run/user/1000/.mutter-Xwaylandauth.*')`). The
installer's live paths: re-run while loaded → "is live", `--uninstall` →
immediate "bridge not running" error from the tools, re-install → live again
without a logout.

Ubuntu 26.04 / GNOME Shell 50.1 (gnome-shell 50.1-0ubuntu1.2, gjs 1.88.0,
Xwayland 24.1.10, kernel 7.0, sudo-rs 0.2.13): the identical run passes,
including per-axis `MAXIMIZED_VERT` through `set_maximize_flags()` (500x768
at y=32), `ListMonitors.connector = "Virtual-1"` (the `get_monitors()` +
`get_connector()` route), `XInfo` = `(':0', '/run/user/1000/.mutter-Xwaylandauth.*')`,
and the installer under sudo-rs. Extra checks there: 200 `ListWindows` calls
take 0.21 s wall in total (~1 ms each) with gnome-shell at 366 MB RSS;
**lock screen**: `loginctl lock-session` → the journal shows `disabled` and
`org.w11.Bridge` is gone (the tools say "bridge is unavailable while
GNOME Shell is in 'unlock-dialog' mode"), unlock → `enabled`/`acquired` again
within a second; **`--try-unsafe`**: after `--uninstall` + reboot (shell has
never seen the uuid), `WDOTOOL=... sh gnome/install-bridge.sh --try-unsafe`
run as the plain user (uinput via the udev ACL) drove Looking Glass, loaded
and enabled the extension without a logout, and left `Eval('1+1')` at
`(false, '')`, unsafe mode off again; `--check` state 1 afterwards.
`getmouselocation` after `mousemove 400 300` matches the bridge's
`GetPointer` exactly, i.e. the injected tablet pointer and Mutter's frame
rects share one coordinate space.

X ids (TV-1, on 50.1): the bridge's `xid` for xterm (`0x60000c`) is the
entry in `xprop -root _NET_CLIENT_LIST` and `wmctrl -l`; `xprop -id` on it
gives `WM_CLASS = "xterm", "XTerm"` (= `wm_class_instance`/`wm_class`),
`_NET_WM_PID` = `pid`, `_NET_WM_NAME` = `title`, and `_NET_WM_DESKTOP`
follows `set_desktop_for_window` (1) and `STICKY` (`0xffffffff`,
`_NET_WM_STATE_STICKY`). Note that on 26.04 even the desktop user needs
`XAUTHORITY=$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` to talk to Xwayland
(`XInfo`/`session.find_xauthority()` return exactly that file).

Bridge v2 on 24.04 / GNOME Shell 46.0 (install → re-login → `--check` says
version 2, journal has `enabled (bridge v2, gnome-shell 46.0)` and no
`JS ERROR`): `selectwindow` while the calculator has focus, with the click
injected by `wdotool click 1` over its centre, prints that window's id and
rc 0, the case the old focus-change picker could never answer, and the
press does not reach the application (its window stays focused, nothing is
typed into it); a click on empty desktop is `selectwindow: no window under
the pointer`, rc 1; Escape is `selectwindow: cancelled with Escape`, rc 1;
killing the client mid-pick releases the grab (the next injected click and
keystroke reach the application again); `SelectWindow(2500)` over `gdbus`
with no click answers `.Cancelled: no window picked within 2500 ms`. The
session was usable after every one of them. Hit-testing Mutter's raw window
list rather than `ListWindows`' rows was found here too: every click answered
with an untitled surface no other command reports (hence the rule in
`_windowUnderPointer`).

`--clearmodifiers` was measured on 24.04, 26.04 and KDE with QEMU's emulated
PS/2 keyboard (a real keyboard to the guest kernel and to libinput) holding
Ctrl, reading back through a raw-mode terminal. Two findings, both now fixed
and both the reason L7 reads as it does:
* pressing the modifier back put it down on **our** virtual keyboard, where
  the user's own release could not clear it: `type ab` after the sequence
  still produced `^A^B`, for the rest of the daemon's life. Reproduced
  identically on GNOME 46, GNOME 50 and KWin; not on sway, where wlroots does
  not reference-count and the press was merely invisible.
* not one of the eight key-ups appeared on the wire at all, the kernel drops
  a release from a device that does not hold the key, so the flag had never
  cleared a foreign modifier in the first place.

  wdotool now restores only what it was holding itself, and says (as root)
  which modifier it could not clear. Re-verified after the fix on 26.04 /
  GNOME Shell 50.1 with the same emulated PS/2 keyboard: with Ctrl held on it,
  `type --clearmodifiers ab` still arrives as `^A^B` (the modifier cannot be
  cleared, and the warning now says so, naming `ctrl`), our virtual keyboard
  is left holding **nothing**, and after the host releases Ctrl a plain
  `type ab` arrives as `ab`, no stick, matching `origin/main`. Also measured
  there: `keydown shift` is released and pressed back around the injection;
  `keyup --clearmodifiers ctrl` ends with nothing down; two foreign keyboards
  are named in one warning and neither is pressed; the key released *during*
  an 8-character injection leaves nothing down on either device; chained
  `--clearmodifiers` ops warn once per command and nothing warns without the
  flag; `WDOTOOL_NO_KEYSTATE=1` is silent and still restores our own modifier;
  and the desktop user (`READABLE=0 UNREADABLE=4`) gets rc 0, zero bytes of
  stderr, no traceback, and the same clear/restore of wdotool's own modifiers.
  The ordinary injected event stream is byte-identical to `origin/main` (188
  events, md5 `790a14c8e5ccdfd00aa48bbcab37088e`).

Bridge v2's picker was re-verified on **26.04 / GNOME Shell 50.1** after the
review (install → re-login → `--check` version 2, journal clean, `JS ERROR`
count 0 across every run): clicking the already-focused window answers it with
rc 0 and the press does not reach the application; clicking the other window
answers that one; Escape and a click on empty desktop are rc 1 with a reason;
overlapping windows answer with whichever is raised, both ways round;
`SelectWindow(2500)`/`(1200)` come back as `.Cancelled` after 2689/1383 ms; a
client killed mid-pick releases the grab (typing works immediately after).
Wedge attempts, none of which wedged anything: two pickers at once (the second
is `.Unsupported: another window selection is already in progress`, the first
still answers the click, none left running), the picker over the **Activities
overview** and over **Alt+F2** (both `.Unsupported: the shell is already
modal`, and a normal pick works immediately afterwards), `gnome-extensions
disable` with a grab held, two SIGKILLs of the client mid-pick, twelve rapid
pick/cancel cycles, and a timeout racing a click, after every one of them an
injected click and `type` reached the application. `wwmctl -a :SELECT:` and
`wxprop` click-select now print *click* the target window, and complete.

Bridge v3's maximize pair was measured on the **default desktop installs** of
both releases (26.04 / GNOME Shell 50 and 24.04 / GNOME Shell 46), gnome-text
-editor, screenshots of every end state. Before: a window at 200,150 900x600,
maximized on both axes and then unmaximized with `wwmctl -b remove,
maximized_vert,maximized_horz`, came back 200,32 900x1048, the right width at
the full height, and stayed there: a second `remove` did nothing, a `toggle`
of `maximized_vert` set the flag back on without moving anything, and `-e` put
the size but not the position back. Reversing the two properties moved the
damage to the other axis (67,150 1853x600). After: 200,150 900x600 on both
releases, byte for byte the rectangle it started from, for `remove` and for
`toggle` alike. Unchanged either way, and re-measured: one axis up and down
again, `remove` of the pair when only one axis was set, and the two axes as
two separate `wdotool windowstate` commands (two processes, so the client has
answered in between, which is why that route never showed the bug).

`ConfirmDisplayChange` was measured on the default installs of both
releases (26.04 / GNOME Shell 50.1, gjs 1.88.0; 24.04 / GNOME Shell 46.0,
gjs 1.80.2), two 1920x1080 heads, the bridge installed the documented way
(copy → reboot → `--check` state 1, name owned, version 3, installed
`extension.js` md5-identical to the tree's). The dialog was raised by
`wxrandr --output Virtual-1 --mode 1920x1080 --pos 0x0 --output Virtual-2
--mode 1280x1024 --right-of Virtual-1 --persistent` and every end state
screendumped. What it is, on both
releases from the shell's own gresource and identical between them:
`GObject.registerClass(class DisplayChangeDialog extends
ModalDialog.ModalDialog)` in `windowManager.js`, added to
`Main.layoutManager.modalDialogGroup` by `ModalDialog._init`, holding
"Revert Settings" (`action: this._onFailure`, `key: Escape`) and "Keep
Changes" (`action: this._onSuccess`, `default: true`); each handler is one
`this._wm.complete_display_change(bool)` and one `this.close()`. The screen
shows *Keep these display settings? / Settings changes will revert in 20
seconds* over a dimmed desktop.

* **Nothing pressed** (the control): the layout applies at once and 32 s
  later it is back where it started, with no `~/.config/monitors.xml` at
  all, which is what the two calls below are measured against.
* **`ConfirmDisplayChange(true)`** → `true` about 1.5 s after the apply: the
  next screendump is a plain undimmed desktop, `monitors.xml` appears in that
  same second (Mutter's writer; nothing existed while the dialog was up) with
  exactly the applied layout in it, and the layout is still the new one 34 s
  later, long past the 20 s the countdown would have taken.
* **`ConfirmDisplayChange(false)`** → `true`: two seconds later the previous
  layout is back on screen and `monitors.xml` is byte-identical, same md5 and
  same mtime. Nothing written.
* **No dialog on screen** → `false`, seven calls of both verdicts per release:
  layout, `monitors.xml` and the shell all untouched, journal clean, the
  bridge still answering. `Shell.WM.complete_display_change` is in the
  `Shell-14`/`Shell-18` typelib on both, so the probe passes and the verdict
  really is forwarded, that is what "a no-op when nothing is pending" was
  measured to mean. A second `true` on a dialog already answered is `false`,
  and a `false` straight after a confirmed keep does **not** undo it: the kept
  layout was still there 20 s on, and the saved file unchanged.
* **The dialog's own buttons afterwards**, with the bridge having answered one
  moments before: Escape on the next dialog reverts and leaves the file
  untouched, a click on "Keep Changes" keeps and rewrites it, on both
  releases. GNOME Shell 50.1 logs `Source ID N was not found when attempting
  to remove it` when the dialog is dismissed through its *own* button or key
  (four presses out of five) and never through the bridge; 46.0 logs nothing
  either way. That one is gnome-shell's, on the path we do not take.
* No `JS ERROR` or `JS WARNING` in any of it. One thing worth knowing while
  testing: the 5-minute idle blank puts the shell in `unlock-dialog` mode,
  which disables the extension (as documented under **lock screen** above), the bridge comes back on unlock.

Everything this document describes has now been exercised on a live desktop, on
both GNOME releases, including the escape hatch that drives Looking Glass to
install without a logout and the dialog answering above.

Review fixes re-verified on Ubuntu 24.04 / GNOME Shell 46.0 (fresh instance,
systemd 255): the uaccess-only rule gives `root:root 0600` plus
`user:test:rw-` right after `--udev` and again right after autologin on the
next boot, `wdotool mousemove` works as the plain user; `--udev` over the
earlier `GROUP="input"` rule prints "resetting the node" and ends in the
same state; `--udev --uninstall` leaves `root:root 0600`, no ACL, no udev
tags, and repeated `udevadm trigger --name-match=uinput` keep it that way
(`open('/dev/uinput')` as the user → `EACCES`, `wdotool` prints its
run-as-root hint). `windowstate --add/--toggle/--remove SHADED` → rc 1
"Mutter does not implement window shading", `SKIP_TASKBAR`/`MODAL` still
warn with rc 0, `BELOW` rc 1. `getmouselocation`'s window is unchanged with
`WindowAt` gone (`gdbus call … WindowAt` → `UnknownMethod`), `GetPointer`
answers. With the extension disabled, `dbus-monitor` sees no `Eval` from the
tools by default and exactly one with `WDOTOOL_GNOME_AUTOLOAD=1` (refused
outside unsafe mode, same "installed but not enabled" message either way);
`loginctl lock-session` → "unavailable while the screen is locked" (`Mode`
stays `ubuntu` on 46, `ScreenSaver.GetActive` is `true`), unlock → working
again. Journal clean throughout.

<a id="gnome-46-vs-50-and-other-honest-limits"></a>

## GNOME 46 vs 50 and other limitations

* **Partial maximization** works on every release: GNOME 46–48 take the
  directions in `maximize(flags)`/`unmaximize(flags)`, 49+ in
  `set_maximize_flags(flags)`/`set_unmaximize_flags(flags)` (plain
  `maximize()` there is just "both"). Reading goes through the
  `maximized-horizontally/-vertically` properties everywhere.
* **Moving/resizing a maximized, fullscreen or tiled window** is silently
  constrained by Mutter, as it is on X11. Unmaximize first if you mean it.
* **`xid`** is `global.display.get_x11_display().lookup_xwindow(window)`
  (public in 46 and 50; `meta_window_get_xwindow` does not exist) for
  windows whose `get_client_type()` is X11; the leading hex number of
  Mutter's window description (`0x<xid>` on 46, `0x<xid> (title)` on 50) is
  the fallback. Wayland-native windows get `0`.
* **`connector`** in `ListMonitors` needs `MonitorManager.get_monitors()` and
  `Meta.Monitor.get_connector()` (Mutter 49+); on 46 it is `""`.
* **Lock screen**: extensions run only in the `user` session mode by default,
  so the bridge vanishes while the screen is locked (verified: `disabled` on
  lock, `enabled` on unlock; the tools report the mode). Add
  `"session-modes": ["user", "unlock-dialog"]` to `metadata.json` if a test
  rig needs it alive there.
* No `Eval`, no input injection, no screenshots: the bridge is the
  window-management plane only. The Python side never touches
  `org.gnome.Shell.Eval` either, unless `WDOTOOL_GNOME_AUTOLOAD=1` is set, then, with the shell already in unsafe mode, the backend loads an installed
  but not-yet-loaded copy of the extension on first use instead of asking for
  a re-login.
* **Hotkeys**: do not bind scripts to `Ctrl+Alt+F1`…`F12` on GNOME Wayland.
  Mutter's native backend owns those chords as VT switches
  (`switch-to-session-N`), gsd-media-keys cannot grab them (`Failed to grab
  accelerator` in the journal) and injecting one switches the console, the
  session keeps running on the VT you just left (`chvt N` brings it back).
  `<Ctrl><Super>F7` and the like work fine (verified: custom keybinding →
  script → `wdotool search … windowactivate type`).

## Known limitations on GNOME

Found by stress-testing the tools against real GNOME 46 (24.04) and GNOME 50
(26.04) sessions on a three-head 5760x1080 rig. These are the things that
behave differently from xdotool on X11 and that no amount of code in this
repo can fix; the bugs that *were* fixable have been.

**Keyboard**

* **L1 (lifted in 0.4), which of several configured layouts is active is
  not on the wire, and is read from the shell instead.** `key`/`type` send
  evdev keycodes and the compositor reads them through the session's active
  layout, so wdotool reads that layout's keymap off `wl_keyboard.keymap` and
  works out which key produces the character asked for (see **Keyboard
  layouts** in the top-level README), `type ü`, `type y` and `key ctrl+z`
  are all right on a German session. What Mutter will not tell an unfocused
  client is *which group* of a multi-layout keymap is active:
  `wl_keyboard.modifiers` carries the group and Mutter sends it only to the
  window with keyboard focus (`focus_resource_list`), which an injector never
  is. GNOME publishes it anyway, and wdotool reads it before every command, so
  a session configured `us, de` and switched to German types German and says
  nothing. `WDOTOOL_XKB_GROUP=<n>` still outranks it, and is
  read from the environment of the **command** and carried to the daemon with
  the text it is to type, so a script that sets it mid-run is obeyed by a
  daemon that is already running. Where the setting cannot be read, or
  describes no one layout, the old behaviour stands unchanged: group 1,
  the notice on every command that types, and the pin as the answer. Rig
  facts worth having:
  - **`mru-sources` is the live truth and `current` is dead.** The head of
    `mru-sources` is the active source, written on every switch by every
    means, `Super+Space`, the panel indicator's menu, and it is what a
    login restores, so the *first* command of a fresh session can already be
    on the second layout. It is `[]` only in a session that has never
    switched, and there `sources[0]` is active. `gsettings describe
    org.gnome.desktop.input-sources current` ends "DEPRECATED: This key is
    deprecated and ignored" on both generations, `dconf watch` across a
    session of switching shows the shell writing only `mru-sources`, and
    `gsettings set ... current 1` moves nothing. (This file used to say
    `current` was the index of the active source. It is not, and never was on
    either of these generations.)
  - **dconf is not readable over the bus; the portal is.** `ca.desrt.dconf`
    publishes `Init`, `Change` and the `Notify` signal and no read method at
    all. `org.freedesktop.portal.Settings` version 2 answers
    `ReadAll(["org.gnome.desktop.input-sources"])` live on both generations,
    and `xdg-desktop-portal` and `xdg-desktop-portal-gnome` are both in the
    default 24.04 and 26.04 installs. A `SettingChanged` signal carries every
    switch too; wdotool re-reads per command and subscribes to nothing.
  - **Mutter appends its own `us` group** after your sources, always, even
    when `us` is already one of them, so one `de` source compiles as
    "de,us", and from the keymap alone one layout and two are the same thing.
    Beyond three sources it compiles in chunks of three around the one in
    use: `de,fr,gr,ru,es` is `de, fr, gr, us` until Spanish is picked and
    then `ru, es, us`.
  - **`xkb-options` is re-read only when the `sources` setting itself
    changes**, so an option set on its own leaves the compiled keymap
    byte-identical.
* **L2, `type` skips characters the active layout cannot produce**, with one
  warning per character ("Can't type character 'ß' (not on the French
  layout). Skipping."), and types the rest of the string. Dead-key pairs are
  *not* in that bucket: `ô` on French is dead_circumflex then `o`, and a bare
  `´` is dead_acute twice, both composed by the application exactly as they
  would be for a physical keyboard. What stays unreachable is a character the
  layout only reaches through a Compose sequence that is not a dead-key pair
  (`ø` on German, say), and one the layout simply does not have (`ñ` on
  `fr(basic)`, which has neither `dead_tilde` nor `ntilde`).
* **L7, `--clearmodifiers` clears wdotool's own modifiers, not the ones in
  your hand.** X11 lets xdotool read the modifier state, clear it and put it
  back. Through uinput neither half is possible for a key held on a *physical*
  keyboard, and both input paths were measured on live GNOME, KDE and sway
  sessions:
  * the kernel (`input_handle_event()`) **drops an `EV_KEY` release for a code
    the emitting device does not hold**, so the eight key-ups wdotool sends
    produce no events at all when it is holding nothing. The modifier is not
    "ignored by the compositor"; nothing reaches the wire.
  * a key-*down* wdotool sends is real, and it belongs to wdotool's virtual
    keyboard until wdotool releases it. Mutter and KWin reference-count key
    state across the seat's devices, so the user letting go of the same
    modifier takes the count 2→1 and leaves it **active for the rest of the
    session**, nothing else will ever send the matching release.

  So the flag releases the eight modifier keys and presses back exactly the
  ones **wdotool itself** was holding (from an earlier `keydown`); a modifier
  held on a real keyboard is left alone, and the injection goes ahead with it
  down. That cannot strand anything: every key wdotool presses, wdotool (or
  the daemon exiting, which destroys the device) releases.

  When wdotool may read the key state it says which modifier it could not
  clear, once per command, `EVIOCGKEY` on `/dev/input/event*`,
  `hacks/input/keystate.py`, reading only, never deciding what to inject. That
  read needs **root**: logind's `uaccess` ACL covers `/dev/uinput` (and
  joysticks, and sound) but *not* keyboards, measured on 24.04 and 26.04
  alike, the seat user cannot open `/dev/input/event*` (`crw-rw---- root:input`,
  no ACL), and this repo ships no rule to change that, because a read ACL on
  every keyboard is a system-wide keylogger for every process of that user.
  Without the access the **behaviour is identical** and the diagnostic is
  simply absent, so nothing warns about root. `WDOTOOL_NO_KEYSTATE=1` forces
  that path. Clearing a foreign modifier for real would need the device
  grabbed away from the compositor (`EVIOCGRAB`), a different tool.
* **L11, a held `keydown` autorepeats.** The compositor applies its own
  key-repeat to an injected key held down, exactly as it does for a physical
  key. `keydown a; sleep 2; keyup a` types a row of `a`s. xdotool on X11
  behaves the same way; scripts that want one keystroke should use `key`.
* **L12, injected hotkey chords race the program they launch.** A chord
  injected with `key super+1` is delivered to the shell, which starts the
  bound application; if the script goes straight on to `type`, the modifier
  release and the new keystrokes can interleave with the launch and the
  shell can see stray `Super+<digit>` presses. Put a `search --sync` on the
  new window between the chord and the typing.

**Pointer**

* **L3, `getmouselocation`'s `window` field is a Mutter window id**, the
  same 64-bit number every other wdotool command uses, not an X11 window
  id, even for XWayland clients, and `0` when the pointer is over the
  desktop, the top bar or a dock. Piping it into `xprop -id` does not work;
  use `wxprop -id`, which speaks the same ids.
* **L13, `click --window W` activates W but does not move the pointer.**
  There is no send-event on Wayland: the click is a real button press
  wherever the pointer happens to be. Move the pointer first
  (`mousemove --window W x y click 1`) when the position matters.

**Windows and the session**

* **L5, shell grabs eat injected input.** While the Activities overview,
  the run dialog (`Alt+F2`) or the print/file dialogs of the shell itself
  have a keyboard grab, injected keys and buttons go to the grab, not to the
  window `getactivewindow` reports, and `windowactivate` does not dismiss
  it. An injected `super` needs *two* Escapes to close the overview
  afterwards. Check for and dismiss the overview before a scripted run.
* **L6, the lock screen and the greeter.** Behind the lock screen the
  bridge extension is not running, so every window/workspace command fails
  fast with rc 2 ("no Wayland session found"), and on the GDM greeter the
  backend says so explicitly. `type`, `key` and `mousemove` still inject:
  `/dev/uinput` does not care that the screen is locked, and neither does
  the compositor. Treat a machine where anyone can reach `/dev/uinput` as a
  machine where anyone can type into the lock screen.
* **L8, `selectwindow` is a real picker, with a deadline.** The bridge
  takes a Clutter stage grab and answers with the window under the pointer at
  the next button press, `xdotool selectwindow`'s own semantics, the
  already-focused window included. Differences that remain: the press is
  swallowed (it does not reach the application, as under an X11 pointer
  grab), keystrokes during the pick are swallowed too and **Escape cancels**
  (rc 1, `selectwindow: cancelled with Escape`), a click on no window is an
  error rather than X11's root window, and the grab is capped at **30
  seconds**, an input grab that outlived its client would leave the session
  unable to click anything, so it is bounded, released when the caller
  disconnects (Ctrl-C), and released when the extension is disabled. The cap
  bounds one call; a **quiet period as long as the grab just held** bounds
  the caller, so a client that re-arms in a loop gets at most half the time
  while a normal window selection (a click, in a second or two) is never delayed. The grab
  is held a moment past the press (≤300 ms) for the matching button-release,
  so the application does not receive half a click it never saw the start of;
  scroll, touch and pad events during a pick are swallowed as well, while
  pointer motion is let through so hover feedback still works while aiming.
  **Two refusals, both rc 1 with a reason and neither taking a grab:** a
  second `selectwindow` while one is already running (two stage grabs coexist,
  but only the first handler sees each event, so the second would silently eat
  the user's next click), and a shell that is **already modal**, the
  Activities overview, `Alt+F2`, an open menu, a system dialog. In the last
  case the extension reads the shell's own state (`Main.actionMode`,
  `Main.modalCount`, `Main.overview.visible`, each feature-detected) rather
  than trusting `pushModal`, which nests on top of the overview on GNOME 50
  instead of refusing: measured with the overview up, grabbing anyway
  hit-tested the click against the windows' real frame rects, which are not
  what is on screen then, and answered with a window nowhere near the pointer
  (46 answered with none). Dismiss the overview and pick again. Needs bridge v2: see *Install*. On sway/i3 there is no picker at all and
  `selectwindow` still waits for a focus change (that IPC has neither an
  interactive picker nor a pointer position); KDE has always used KWin's own
  `queryWindowInfo` picker, which is click-to-select already.
* **L9, `--sync` on a maximized, fullscreen or modal-attached window.**
  Mutter constrains moves and resizes of such windows and silently refuses
  them, exactly as an X11 window manager does, so the size or position never
  changes. Since the fix for B3 every `--sync` wait is bounded (10 s by
  default, `WDOTOOL_SYNC_TIMEOUT` overrides it): the command now prints
  `wdotool: gave up waiting for …` and exits 1 instead of hanging for ever.
  Unmaximize first if you mean it.
* **L10, `windowsize --usehints` is pixels.** Size hints
  (`WM_NORMAL_HINTS` increments, a terminal's character cell) are not
  exported by Mutter for Wayland clients, so `--usehints` warns once and
  interprets width/height as pixels. Note that Mutter *does* still snap the
  result to the client's grid: asking an xterm for 497x392 leaves it at
  496x392. `windowsize --sync` accepts that snapped size as the answer.
* **L4, one daemon per uid *and* per runtime dir.** The input daemon's
  socket is `$XDG_RUNTIME_DIR/wdotool.sock`, `/run/wdotool.sock` for root,
  and `/tmp/wdotool-<uid>.sock` when there is no `XDG_RUNTIME_DIR` at all, so a cron job without a runtime dir gets a *different* daemon from the one
  the desktop session uses, with its own pointer model. Since the fix for B6
  the models are reconciled from the compositor on GNOME (`getmouselocation`
  and `mousemove_relative` both ask Mutter first), so the split no longer
  moves the pointer to the wrong place; it still means two sets of virtual
  input devices. Export `XDG_RUNTIME_DIR=/run/user/$(id -u)` in cron jobs.

## `/dev/uinput` without root (`--udev`)

Input injection (`wdotool key/type/click/mousemove`) goes through
`/dev/uinput`, which stock Ubuntu ships as `root:root 0600` with no udev rule
of its own. Instead of running everything as root or adding yourself to the
`input` group and logging out, install one udev rule:

```sh
sudo sh gnome/install-bridge.sh --udev             # install rule + modules-load, apply now
sudo sh gnome/install-bridge.sh --udev --uninstall
sh gnome/install-bridge.sh --check                 # ...also prints the rule/driver/ACL state
```

`--udev` copies `gnome/60-w11-uinput.rules` to `/etc/udev/rules.d/`
and `gnome/modules-load-uinput.conf` to
`/etc/modules-load.d/w11-uinput.conf`, runs `modprobe uinput`,
`udevadm control --reload` and `udevadm trigger --name-match=uinput`:

```
KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess"
```

* `TAG+="uaccess"` makes systemd-logind put an ACL for the user of the
  **active** seat session on the node (the mechanism that hands you your
  sound card and webcam). The `udevadm trigger` re-runs the rule on the
  existing node, so the user logged in right now gets the ACL **without
  logging out**; logind re-applies it at every login and VT switch.
* `OPTIONS+="static_node=uinput"` has udev create `/dev/uinput` at boot from
  the module's devname alias, with these permissions and tags, on kernels
  where uinput is a module that is not loaded yet (the first `open()`
  autoloads it). The `modules-load.d` file loads it at boot anyway so nothing
  depends on the autoload path.
* Deliberately **no `MODE`/`GROUP`**: the node stays `root:root 0600` plus
  the ACL, so only the user at the seat can inject input. The ACL is
  consulted at `open()` only, so the daemon re-checks it before every
  injection and drops its virtual devices when logind hands the seat to
  another session, otherwise a user who switched away would keep typing
  into the session that replaced theirs. An earlier
  revision added `MODE="0660", GROUP="input"` as a fallback for sessions
  logind does not manage; that hands every member of `input` (service
  accounts included) a standing keystroke-injection channel into the seat
  user's session whether or not they are logged in there, which is exactly
  what the uaccess tag avoids. Sessions without logind run the tools as root.

Verified on Ubuntu 24.04 (GNOME 46, kernel 6.8):

* uinput is **built into the Ubuntu kernel** (`modinfo -n uinput` →
  `(builtin)`), so `/dev/uinput` exists from boot and the "module must be
  loaded before login" worry does not arise there;
  `systemd-modules-load` simply ignores the builtin. The `static_node` /
  `modules-load.d` pieces are for kernels that build it as a module.
* Installing the rule inside a running session: `getfacl /dev/uinput`
  shows `user:test:rw-` immediately after `--udev` (the trigger did it), and
  `wdotool type` works as the plain user in the same session, no relogin.
* After a reboot the ACL is there right after autologin (`--check`: "uinput
  usable by test: yes (logind ACL)"), i.e. the rule present at boot is
  enough.
* `--udev --uninstall` puts the node back to `root:root 0600` with the ACL
  cleared, and makes that stick. udev tags are sticky in its database: with
  the rule merely deleted, the next `udevadm trigger` of the node still
  matched `73-seat-late.rules`' `TAG=="uaccess"` and brought the ACL back
  (observed on systemd 255, where `TAG-=` only drops the *current* tag and
  `TAG==` matches the sticky set). The uninstall therefore deletes the
  files, then the node's udev database entry and tag index links
  (`/run/udev/data/c10:223`, `/run/udev/tags/*/c10:223`, plus the
  `static_node-tags/uaccess/uinput` link logind would act on at the next
  session activation) so udev starts from scratch at the node's next event
  as after a boot, then `setfacl -b` + `chmod 0600`. Verified: repeated
  `udevadm trigger --name-match=uinput` afterwards leave `root:root 0600`,
  no ACL, no tags. Re-running `--udev` over the earlier `GROUP="input"`
  revision of the rule resets the node the same way before installing the
  new one.
* The tools' "bridge unavailable" diagnosis: `org.gnome.ScreenSaver
  .GetActive()` says whether the screen is locked (GNOME 46 keeps the
  shell's `Mode` property at the session mode while locked; 50 reports
  `unlock-dialog` there, both are handled), `Mode` = `gdm` means you
  reached the greeter's bus (nobody logged in). The unlocked session's mode
  is `ubuntu` on Ubuntu (`classic` in GNOME Classic), which is *not*
  treated as locked, a disabled extension there is reported as disabled.

Check with `getfacl /dev/uinput`, expect a `user:<you>:rw-` line while your
session is active (`ls -l` shows `root root` and `rw-rw----`: the group
column is the ACL mask, nobody is in that group). Security-wise, whoever can
open `/dev/uinput` can type as you; the rule limits that to the physically
logged-in user, not to a group, which is the X11 status quo.

The kernel device is also why none of these tools ever shows an
authorization dialog: the route that makes GNOME and KDE ask, the desktop
portal's RemoteDesktop/InputCapture, the one libei clients take, is one we
never take. See **No authorization dialog** in the top-level `README.md` for
what each desktop uses instead and for the measurement behind that claim.

## Uninstall

```sh
sh gnome/install-bridge.sh --uninstall           # or --uninstall --system
```

disables the extension in the running shell, removes it from
`enabled-extensions` and deletes the directory. The loaded copy stays in the
shell process until the next login.
