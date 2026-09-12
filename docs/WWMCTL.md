# wwmctl — design contract

Drop-in `wmctrl` clone for Wayland, handling **both** native Wayland apps and legacy X
apps (XWayland) as first-class citizens. Lives in this repo beside wdotool and reuses
its machinery. Pure-stdlib Python, same rules as the rest of the tree
([Technical.md](Technical.md)): no third-party dependency, byte-parity output against
the installed original.

## On Wayland, with wmctrl installed, this runs wmctrl

Since the X11 proxy, `wwmctl` hands over on a **Wayland** session as well as on an X11 one:
the original is exec'd with `DISPLAY` pointing at [`xw11`](XW11.md), which answers for
native Wayland windows as well as for Xwayland's. The clone still runs when the original
is not installed, when `W11_PROXY=never` (or `WWMCTL_PROXY=never`) asks for it, when
`W11_PASSTHROUGH=never` is set, and whenever the command uses one of our own options --
of which `wmctrl` has one, `--true-geometry`; every other byte this clone takes, wmctrl takes too.
Everything this document says about the clone's output is still what the clone prints;
through the proxy the output is the original's, byte for byte, because it **is** the
original. The six rules in order are
[XW11.md § The wrapper rules](XW11.md#the-wrapper-rules).

## The dual-plane trick

On wlroots compositors the *compositor itself* is the X window manager for XWayland, so
an XWayland client's real X11 window id can be recovered — and there are two ways to
recover it, one per backend. On sway and i3, `swaymsg -t get_tree` publishes it outright
(the node's `"window"` field, plus `window_properties`), and the same is true of
Hyprland's `j/clients`, Wayfire's `window-rules/list-views` and Cinnamon's
`get_xwindow()`. On the generic wlr floor and on COSMIC nothing publishes it, so the id
is *matched* instead, through `wdotool/xid_match.py` against the X server's own
`_NET_CLIENT_LIST` — with title and a lowercased `app_id` against `WM_CLASS` as the only
separators there, no pid and no geometry, so a pair that nothing separates keeps id 0
rather than being handed one of two ids. Either way:

- The **unified window list** comes from the compositor backend (reuse
  `wdotool.backend_detect/backend_sway` — do not fork them).
- **XWayland windows** are printed with their real X11 window ids (`0x%08x`), so other
  X tools (`xprop -id`, real wmctrl, old scripts) interoperate on the same ids. X-only
  data (WM_CLASS instance.class, WM_CLIENT_MACHINE, _NET_WM_PID fallback) is read
  straight from the XWayland server over a pure-stdlib X11 wire client. Name-setting
  (-N/-I/-T) uses X ChangeProperty for X windows.
- **Native Wayland windows** get their compositor node id as the printed id (collision
  with X ids is practically impossible — X ids live at 0x00400000+ resource bases; we
  also check X-plane matches first on `-i` lookups). class comes from app_id
  (`app_id.app_id`), hostname from uname, pid from the compositor.
- **Actions** (activate, close, move/resize, desktops, states) go through the
  compositor backend for both planes — it is authoritative for XWayland windows too.
  The X plane is for identity/properties, not for actions (wlroots' xwm honors some
  EWMH root messages, but the compositor route is strictly more reliable).
- No X server around (pure Wayland, xwayland disabled)? Everything still works;
  X-enrichment silently degrades (class comes from window_properties).
- **Machine column rule** (both planes): WM_CLIENT_MACHINE when the X plane can
  read it, else the local hostname (XWayland and Wayland clients are local by
  construction); "N/A" only when gethostname() itself fails. This is *more*
  filled-in than real wmctrl, which prints "N/A" whenever the property is
  missing — deliberate, since the hostname is always correct here.

## The X11 wire client

`wdotool/x11_mini.py` is the pure-stdlib X11 client wwmctl reads the X plane with.
It lives under `wdotool/` because it already imports `w11common.session`, and it has
three callers: wwmctl (identity, geometry, EWMH ClientMessages), wxprop (all of its
X-window work) and `wdotool.backend_kwin` (the XWayland ids KWin 6 does not export).
What it speaks, what it does not, and its two-class error model are
[Technical.md section 3](Technical.md#3-the-wire-clients-and-their-error-models); the
module docstring is the API.

## wmctrl surface (byte-parity against wmctrl 1.07)

`-l` (with `-p` pid, `-G` geometry, `-x` class), `-d`, `-s N`, `-a/-c/-R <STR>`,
`-t N -r <STR>`, `-e G,X,Y,W,H -r <STR>`, `-b add/remove/toggle,P1[,P2] -r <STR>`,
`-N/-I/-T <STR> -r <STR>` (in a UTF-8 environment — any UTF-8 locale, or `-u` —
the legacy `WM_NAME`/`WM_ICON_NAME` is *deleted* rather than written as a lossy
`STRING`, exactly as wmctrl's `window_set_title` does),
`-i`, `-F`, `-v`, `-m`, `-k on|off|toggle`, `-o X,Y`,
`-n N`, `-h`. Selection default: case-insensitive substring on title. Exact printf
formats, column widths, error strings and exit codes come from the real wmctrl source
and from reference dumps of the real binary, which the devshell and every golden image
carry.
Desktop semantics map exactly like wdotool's desktop commands (sway workspaces,
0-based). `-k`/`-o`/`-n` are warn+succeed where Wayland cannot do the thing, the same
rule wdotool's cosmetic commands follow.

**One deliberate deviation from the oracle, in the listing.** The machine column is
right-aligned to the *longest* `WM_CLIENT_MACHINE` in the whole list; wmctrl 1.07's
`main.c` sizes it from the *last* row it printed instead. Our `-l` is in stacking
order, so with wmctrl's rule the column would re-flow every time a window was raised.
The `-r <WIN> -L` row keeps wmctrl's own single-window behaviour (`display_window`'s
`max_client_machine_len == 0` branch), where nothing is padded at all.

**Two oracle generations.** Ubuntu 24.04 ships wmctrl 1.07; Ubuntu 25.04+ and
Debian 13+ ship 1.07+git20240228, which adds `-j` (print the current desktop,
`printf("%-2d\n")`), `-S` (list in stacking order), `-Y <WIN>` (iconify), `-r
<WIN> -y <MVARG>` (move/resize, then activate), the undocumented `-z <WIN>`
(lower) and `-E <WIN>` (print the title), and `-k toggle`; the Debian and Ubuntu
patches add two more on top of that, `-r <WIN> -M <PATH>` (an XPM file becomes the
window's `_NET_WM_ICON`, X plane only) and `-r <WIN> -L` (the window's own `-l` row).
Both generations answer `1.07` to `-V`. wwmctl implements the **union** on every
flavor — being a drop-in that rejects `wmctrl -j` on one distro is worse than accepting
it on both — so `-S` is accepted and does nothing (our `-l` is already stacking order,
see below) and `-k toggle` is accepted everywhere. `-k`'s argument *error* is the one
place the extension is not advertised: it stays wmctrl's own `The argument to the -k
option must be either "on" or "off"`, because that string is parity-checked against
both generations, and naming a third value in it would fail the oracle. Only `--help`, which
documents a specific upstream release rather than any behavior, follows the
oracle installed on the box: `wmctrl --help` is consulted once and cached, and
`$WWMCTL_WMCTRL_GENERATION=1.07|git` forces the answer. With no oracle installed
(we may *be* `/usr/bin/wmctrl`) the 1.07 text is printed, the documented parity
target of this clone.

**Limitations of the compositor plane, not defects.**

* `shaded` and `modal` in `-b` are no-ops on both sides: Mutter does not
  implement window shading, and `_NET_WM_STATE_MODAL` on an existing window
  changes nothing an oracle run can observe either.
* `hidden` is a deliberate improvement, not parity: EWMH says a client may
  not set `_NET_WM_STATE_HIDDEN` and real wmctrl's request is dropped on the
  floor; we minimize the window, which is what the person typing
  `-b add,hidden` meant.
* The oracle's own defects, kept as they are because we are not bug-compatible
  where the bug is visibly wrong: real `wmctrl -lG` double-counts the reparent
  offset on Mutter (its geometry column is off by the frame extents); real
  `wmctrl -l` prints `N/A` for a window whose `WM_NAME` is Latin-1 rather than
  the title.

**`--true-geometry` (a flag wmctrl 1.07 never had, the way `wxrandr --persistent` is
one).** Default `-G` reproduces wmctrl's `XTranslateCoordinates`-from-`x,y` doubling
for byte parity — a window at `100,100` prints as `200,200` in the `-G` columns,
because that is the bug the bullet above keeps. `--true-geometry` turns the doubling
off and prints the compositor's own rectangle instead, the one `xdotool
getwindowgeometry` and `xwininfo` agree on: the same window prints `100 100 800 600`
(measured on hypr 0.56.2 against a `foot` placed at `100,100`, `wwmctl --true-geometry
-lGpx`). It is intercepted a screen above the X11 handover by `wwmctl.cli.own_flags_in`
(the `--persistent` pattern) and is listed in `xw11/wrap.py:CLONE_ONLY["wmctrl"]` as
the clone's own token, so a `wmctrl --true-geometry` typed on Wayland keeps our code
rather than reaching the original — which would answer `invalid option`.

`-d` rows are sorted by that desktop id — ascending and positionally indexed,
as real `wmctrl -d` always is. sway answers `GET_WORKSPACES` in *creation*
order, which is what wwmctl used to print, so "the third line is desktop 2"
was not true after a workspace was made out of order.

**Known desktop-id mapping hole**: sway workspace *number* N prints as desktop
N-1, but a workspace literally numbered 0 and *named* (numberless) workspaces
both print as desktop `-1` — colliding with wmctrl's `-1` = sticky/all-desktops
notation, and unreachable via `-s`/`-t` (which count 0-based and so address sway
numbers 1+ only). This is inherent to mapping wmctrl's dense 0-based desktop
list onto sway's sparse/named workspaces; `-R`/`-t -1` sidestep it by using
sway's own "workspace current".

## Testbed

- The devshell ships xwayland, xterm, xprop, xwininfo, xeyes and the real wmctrl.
  Headless sway with `xwayland enable` in its config starts XWayland lazily (at the
  first X client); DISPLAY is announced in `swaymsg -t get_tree`-visible env or in
  sway's log — export it and real X apps run. Real wmctrl (an X client) then works
  against the same session: it is the live oracle for list formats AND for which
  actions work on XWayland windows.
- Full desktops: `vm/vmctl`, whose golden images all carry the real `wmctrl`,
  `xdotool` and `x11-utils` for exactly this. `vm/README.md` has the flavors and
  what each tool does on each.

## GNOME

On a stock GNOME Wayland session (Ubuntu 24.04 / GNOME 46, 26.04 / GNOME 50)
the compositor plane is the w11 bridge extension
(`gnome/install-bridge.sh`, see `gnome/README.md`) through
`wdotool.backend_gnome.GnomeBackend`. wwmctl never reaches into backend
privates there: it consumes the typed hooks `views()`, `workspaces()`,
`x_info()`, `select_window()`, `show_desktop()`, `set_num_desktops()` of
`wdotool/backend.py`, tried *ahead of* the sway `_nodes()` path (which is
untouched) and the generic `list()` fallback.

* **Ids and the list.** `views()` carries Mutter's X11 client window id for
  every XWayland window (`Meta.Window` → `lookup_xwindow`), so `-l` prints
  the same `0x%08x` that `xprop -root _NET_CLIENT_LIST` and real `wmctrl -l`
  show, and `-i` accepts either that X id or the bridge id
  (`Meta.Window.get_id()`, the decimal id `wdotool search` prints). Native
  windows print the bridge id. Rows are Mutter's stacking order, bottom to
  top (real wmctrl prints `_NET_CLIENT_LIST`, i.e. creation order — the
  ids are the same, the order is not).
* **Columns.** `-x`: `instance.class` from the X plane's `WM_CLASS` for
  XWayland windows (the bridge's `wm_class_instance`/`wm_class` pair stands in
  when Xwayland cannot be reached), `app_id.app_id` for native windows (the
  bridge's `gtk_app_id`, and Mutter's `wm_class` — the app id a Wayland client
  gave — for a window that has no `gtk_app_id`). Those two are usually the
  same string and sometimes are not: Ubuntu 24.04's `gnome-terminal` reports
  `gtk_app_id` `org.gnome.Terminal` and `wm_class` `gnome-terminal-server`, so
  `wwmctl -lx` prints `org.gnome.Terminal.org.gnome.Terminal` for a window
  that `wdotool search --class`, which matches `wm_class` first
  ([WDOTOOL.md](WDOTOOL.md)), finds as `gnome-terminal-server` — which is the
  string real `xdotool` matches for the same application on an X11 session,
  and the reason a ported script keeps working. Match on a substring
  (`gnome-terminal`, case-insensitively) and both answer. `-p`: Mutter's pid,
  `_NET_WM_PID` only as a fallback. `-G`: the X client rectangle (GetGeometry
  + TranslateCoordinates, root coordinates) for XWayland windows — one
  titlebar below the frame, Mutter being a reparenting WM — and the bridge's
  `get_frame_rect()` for native ones (logical pixels, no CSD shadows). Machine
  column: the `WM_CLIENT_MACHINE` of X windows, the local hostname otherwise,
  right-aligned to the *longest* one in the list. Real wmctrl 1.07 sizes that
  column from the *last* row (a bug in its `main.c`), which looks stable only
  because its rows come from `_NET_CLIENT_LIST`, i.e. creation order; our rows
  are in stacking order, so copying the quirk would re-flow the column by the
  difference in hostname lengths every time a window is raised. On a session
  where every client is local — every session with XWayland or Wayland clients
  — the two rules print the same bytes. The desktop column is the workspace
  index, `-1` for a sticky window — Mutter's dense 0-based indices are exactly
  wmctrl's, so GNOME has none of the sway id-mapping hole.
* **The X plane** is opened with the `DISPLAY`/`XAUTHORITY` the bridge
  reports (`XInfo`: gnome-shell's own environment, else Mutter's
  `$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` cookie found by
  `w11common.session`), passed to `x11_mini.X11Conn(display, xauthority=)`.
  Mutter starts Xwayland with `-auth`, so the cookie is mandatory — the
  cookie-less same-uid pass that works on wlroots is refused there — and
  this is what makes `ssh root@box` with an empty environment, `sudo`, and
  a GNOME custom-shortcut process all reach it. Xwayland is spawned **on
  demand** by Mutter (the listening socket exists even when no server
  does), so wwmctl only connects when an XWayland window is listed or an
  `Xwayland` process exists (`session.xwayland_running()`); listing a
  purely native desktop never starts an X server.
* **`-d`** comes from `ListWorkspaces`: `DG` is `global.display.get_size()`,
  `VP` is Mutter's own `_NET_DESKTOP_VIEWPORT` when Xwayland is up — one
  pair, so the current workspace's row prints it (`0,0`) and every other
  prints `N/A`, which is what real `wmctrl -d` prints on that session — and
  `0,0` on every row when there is no X plane at all, the origin each
  desktop begins at and the pair the `xw11` proxy publishes for a session
  with no X of its own, `WA` is the workspace's work area over all monitors
  (what Mutter writes into `_NET_WORKAREA`: `0,32 1920x1048` under the top
  bar), the name is `Meta.prefs_get_workspace_name(i)` (`Workspace 1`, …,
  the strings in `_NET_DESKTOP_NAMES`; a nameless workspace prints its
  index). With dynamic workspaces (GNOME's default) the trailing empty
  workspace is listed too — it is real for `-s`/`-t`.
* **`-m`** reads `_NET_SUPPORTING_WM_CHECK` → `_NET_WM_NAME` (`GNOME
  Shell`), `WM_CLASS`/`_NET_WM_PID` (`N/A`: Mutter's check window has
  neither) and `_NET_SHOWING_DESKTOP` (`ON`/`OFF`) from the X root when
  Xwayland is up — byte-identical to real wmctrl there —, waiting up to 2 s
  for a freshly started Xwayland to get its root properties. Without
  Xwayland the name comes from the backend (`GnomeBackend.wm_name`, the
  same string) and the showing-desktop mode is `N/A` — Mutter's real
  show-desktop state has no public API off the X root, which is also why
  `-k` prefers that root (below).
* **`-k`** sends `_NET_SHOWING_DESKTOP` to the X root, exactly as real
  wmctrl does, and Mutter's own show-desktop mode answers: every window is
  hidden, `-k off` brings them all back untouched, and `-m` — which reads
  the same property — agrees. The bridge exports a stand-in that minimizes
  every window on the active workspace (the shell has no API for the real
  mode) and it is the fallback, for a session with no X plane or an
  Xwayland whose window-manager half has not come up; the root property is
  polled for a second to tell the two apart. `-k toggle` (1.07+git) reads
  the same property, and reads an absent one as off.
* **Actions** otherwise go through the bridge, for XWayland and native
  windows alike: `-a` `Activate` (switches workspace, unminimizes, raises,
  focuses), `-c` `Close` (polite delete), `-R` `MoveToWorkspace(current)` +
  `Activate`, `-t N` `MoveToWorkspace(N)` (`-t -1` = current; an index past
  the last workspace is a one-line error, exit 1, where wmctrl would fire
  the request into the void), `-s N` `SetActiveWorkspace` (same for a bad
  index), `-b (add|remove|toggle),P1[,P2]` `SetState` per property —
  `fullscreen`, `maximized_vert`/`maximized_horz` (real per-axis
  maximization on every GNOME release), `hidden` (minimize), `above`,
  `sticky`, `demands_attention` are applied by Mutter. **Both maximize
  axes in one `-b` are sent as one request** (the bridge's `MAXIMIZED`),
  because on Mutter two single-axis calls are not the same thing: it
  unmaximizes to the window's *current* frame rect and takes only the axis
  it is unmaximizing from the saved rectangle, so the second call — issued
  before the Wayland client has answered the first configure — carries the
  still maximized half into its target, and once both flags are clear that
  rectangle becomes the restore size. `-b remove,maximized_vert,
  maximized_horz`, wmctrl's documented way to unmaximize, left a 200,150
  900x600 window at 200,32 900x1048 on GNOME 46 and 50, and reversing the
  two only moved the damage to the other axis. Mutter's own EWMH handler
  folds the two atoms of one ClientMessage exactly the same way, down to
  deciding a `toggle` of the pair on the horizontal flag. KWin needs none
  of this: it settles each state before it answers (see below). Five have no
  Wayland setter at all — `below`, `skip_taskbar`, `skip_pager`, `shaded`,
  `modal` — and for an **XWayland** window those go to the X plane
  instead, as the `_NET_WM_STATE` ClientMessage real wmctrl sends: Mutter
  is the EWMH window manager there and applies `below`, `skip_taskbar` and
  `skip_pager` for real (verified against the oracle on GNOME 46), while
  `shaded` and `modal` are no-ops for the oracle too. On a **native**
  window there is no X twin to ask and they warn `…; ignoring` and exit 0,
  like any request "the WM may ignore". The compositor stays the first
  choice everywhere else — `hidden` really minimizes through the bridge,
  where the X route is a no-op. `-e G,X,Y,W,H` carries
  `_NET_MOVERESIZE_WINDOW`'s meaning: `W,H` are the **client** size and
  the gravity names the point of the window the request positions —
  `1` NorthWest puts the frame's top-left at `X,Y`, `5` Center puts its
  centre on the requested rectangle's, `9` SouthEast its bottom-right at
  `X+W,Y+H`, `10` Static the client itself at `X,Y`; `0` means "the
  window's own `WM_SIZE_HINTS` gravity" and is taken as NorthWest, the
  ICCCM default. What a `-1` keeps depends on the request as a whole *and*
  on the GNOME release, both measured against real wmctrl on the same
  window. Where the request carries a coordinate, a `-1` on the other axis
  keeps that axis' unchanged frame edge (`9,-1,200,W,H` leaves the left
  edge alone) — anchoring it on the gravity point instead put us 80 px
  out. In a **bare resize** (`-e G,-1,-1,W,H`, both coordinates omitted)
  GNOME 46 keeps the gravity's reference point, so `9,-1,-1,W,H` pins the
  bottom-right corner and grows the window up and to the left, while
  **GNOME 50 applies no gravity at all** and keeps the top-left corner
  whatever `G` says. wwmctl follows the compositor: `GnomeBackend
  .compositor_version()` (org.gnome.Shell's `ShellVersion`) decides, the
  cut sits right after 46, and a backend that reports no version keeps the
  46 behaviour — which is what sway and the rest have always done. Some rows of the grid stay 1–16 px apart from the oracle: real
  wmctrl hands Mutter `_NET_MOVERESIZE_WINDOW` with the omitted fields
  still filled in as `(unsigned long)-1`, and Mutter's own arithmetic for
  them is neither the frame rectangle nor the client one. Where an axis is
  omitted the oracle can also *resize* it (`-e 0,300,200,-1,300` grows a
  496-wide xterm to 520): `-1` means unchanged here, per `wmctrl -h`. The
  frame extents — Mutter's
  server-side titlebar, i.e. the difference between its frame rect and
  the X client rectangle `-lG` prints — turn that client rectangle into
  the `Resize` (frame size) and `Move` (frame top-left) the bridge takes:
  `-e 0,10,20,300,200` on an xterm under a 37 px bar is a `300x237` frame
  at `10,20` around a `300x200` client at `10,57`, which is what real
  wmctrl gets from Mutter. Native windows (and an XWayland window whose X
  plane could not be reached) have no extents, so there every gravity but
  Static collapses to NorthWest and `X,Y,W,H` are the frame rectangle
  `-lG` prints. A maximized or fullscreen window is silently constrained
  by Mutter, as on X11. Two Mutter quirks are deliberately **not**
  copied, both from its gravity code reading the frame rect *with* the
  invisible resize border (worth `28` px horizontally and `66` px
  vertically on GNOME 46) and placing by the visible one: with an
  explicit `X,Y` under a trailing or centre gravity real wmctrl lands
  1–2 px further out
  (`-e 9,900,700,300,200`: Mutter `902,665`, wwmctl `900,663`), and a
  value left at `-1` is re-read from that inflated rect, so
  `-e 6,-1,-1,300,-1` grows the height it was not asked to touch from
  `400` to `466` and `-e 3,-1,60,-1,-1` slides x by 16 px. wwmctl keeps
  what the `-1` asked it to keep.
  `-k on|off` is the bridge's `ShowDesktop` (minimizes every normal
  window on the active workspace and restores exactly those on `off` —
  Mutter's own mode is not scriptable). `-n N` is `SetNWorkspaces`: works
  with static workspaces, and with GNOME's default dynamic workspaces the
  bridge refuses and wwmctl prints `wwmctl: dynamic workspaces are enabled
  …; ignoring` (exit 0, the request the WM ignored). `-o`/`-g` warn and
  succeed as everywhere.
* **`-N`/`-I`/`-T`** set `WM_NAME`/`_NET_WM_NAME` (and the icon names) on
  XWayland windows over the X plane (Mutter re-reads them at once); on
  native windows they warn `native window; ignoring` and exit 0 — Wayland
  has no way to rename another client's toplevel.
* **`:SELECT:`** is the bridge's `SelectWindow`, and since bridge v2 it is a
  **click-to-pick**, like real wmctrl's: the extension takes a stage grab and
  resolves the pick with the window under the pointer at the next button press,
  so clicking the window that already has focus answers it. Escape, the 30 second
  cap, a second concurrent picker and a shell that is already modal (the overview,
  a menu) are all rc 1 with the reason. wwmctl prints the hint the *backend*
  supplies (`b.select_window_hint`), because the sentence is not the same
  everywhere: a click on GNOME and KDE, the next focus change on sway, whose IPC
  has no picker, no pointer position and no way to grab input from outside the
  compositor. `:ACTIVE:` is Mutter's focus window.
  **Limitation, not a bug, and on sway only:** there `:SELECT:` returns when focus
  moves to a *different* window, so re-selecting the focused window never returns. No
  Wayland compositor lets a client grab the pointer for another client's
  windows, and the shell exports no click-to-pick API.
* **Errors.** Every failure is one line on stderr, exit 1: the bridge not
  installed (`gnome backend: the w11 bridge extension is not
  running in GNOME Shell; run gnome/install-bridge.sh and restart the
  session (log out and back in)`), installed but disabled, the screen
  locked (extensions stop behind the lock screen), the bridge gone
  mid-session; `-a`/`-c`/… on a window that vanished exits 1 silently like
  a no-match. Unit coverage: `tests/test_wwmctl_gnome.py` on the mock
  bridge of `tests/test_backend_gnome.py`.

Verified live (branch `gnome-wm-tools`, `vm/vmctl` rigs, xterm + xeyes
+ gnome-text-editor + gnome-calculator): Ubuntu 24.04 / GNOME Shell 46 and
Ubuntu 26.04 / GNOME Shell 50, as the desktop user, from a
`<Ctrl><Super>F7` custom shortcut, from `ssh root@` with `env -i`, and
under `sudo` — `-l/-lpGx` (the xterm under its X id `0x00800020` /
`0x0060001e`, `xterm.XTerm`, `WM_CLIENT_MACHINE`; natives under bridge ids
like `0x14a3062e`), `-d` (`WA: 66,32 3774x1048  Workspace 1` on two
1920x1080 heads with the dock; real `wmctrl -d` fails there with `Cannot
get current desktop properties`: Mutter's X root carries no
`_NET_CURRENT_DESKTOP`), `-m` byte-identical to real `wmctrl -m`, `-a`,
`-c :ACTIVE:`, `-i` with either id, `-e` against real `wmctrl` on the
same window (`xterm`, `_NET_FRAME_EXTENTS 0,0,37,0`): identical
rectangles for gravity `0`/`1`/`10` with both coordinates given or both
omitted, on GNOME 46 and 50 alike, and within 1–27 px elsewhere (the `-1`
arithmetic above); xterm snaps
`500x400` to `496x392` on its size increments, `-b add,fullscreen` /
`maximized_vert` seen by real `xprop`, `shaded,below`/`skip_taskbar`
warn+exit 0, `-N/-I/-T` read back by real `xprop`, `-t 1`, `-R`, `-s`,
`-t 7` → `workspace 7 not found`, `-k on/off` (0 then 3 visible windows),
`-n 3` warn+exit 0, `:SELECT:` returning on a `wdotool windowactivate`.
Real `wmctrl -lG` prints the doubled coordinates (`80 118` for our `66
69`) — the non-reparenting-xwm quirk the contract already excludes.

## KDE Plasma

On a Plasma Wayland session (5.27 and 6.6) the compositor plane is
`wdotool.backend_kwin.KwinBackend` — KWin scripting over the session bus,
nothing installed. wwmctl consumes the same typed hooks as on GNOME
(`views()`, `workspaces()`, `x_info()`, `select_window()`, `show_desktop()`,
`set_num_desktops()`), so `-l`, `-d`, `-m`, `-a/-c/-R/-s/-r -t/-e/-k` and
`:SELECT:` work with no wwmctl-side special casing. What is worth knowing:

* **Ids.** XWayland rows print the real X id (`w.windowId` on 5.27; matched
  through the X server's client list on 6, where KWin exports none -- pid and
  `WM_CLASS` filter, title and geometry score, and the order of the two lists
  breaks the tie two windows of one application in one place leave behind),
  native rows print the backend id KWin's uuid is minted into — 30 bits of the uuid,
  so `0x40000000`–`0x7FFFFFFF`. `-i` takes either.
* **`-l -G`.** Real `wmctrl` doubles the frame offset under a non-reparenting
  window manager; our positions are the true ones (the same divergence as on
  GNOME, documented at `wdotool/x11_mini.py:get_geometry`). That is Plasma 6.6
  and sway: KWin 5.27's xwm *does* reparent, so on 5.27 both tools print the
  same positions. Sizes, classes, pids and the row set are identical to real
  `wmctrl -lpxG` everywhere.
* **`-e` on 5.27.** KWin runs `frameGeometry` writes through its placement
  constraints, so a resize larger than the work area is clamped to it; its
  own EWMH path is not, which is why real `wmctrl -e` grows the window and
  ours stops at the work area. Plasma 6 does not clamp.
* **`-d`.** The VP column is `_NET_DESKTOP_VIEWPORT`, and it follows the
  same two-case rule the GNOME `-d` bullet above spells out. With an X plane
  already up it is read from the X server, exactly as wmctrl reads it — KWin
  publishes one pair per desktop, so every row prints `VP: 0,0`; a WM that
  publishes a single pair prints it against the current desktop only. With no
  X plane (no Xwayland process) there is no property to read, so every row
  prints `VP: 0,0` — the origin each desktop begins at and the same pair the
  `xw11` proxy publishes for a session with no X of its own (`wwmctl/core.py`,
  which is what makes real `wmctrl -d` through `xw11` and `wwmctl -d` agree on
  this column). On Plasma 6
  KWin's X root does not follow desktops created over D-Bus, so `wmctrl -d`
  there lists fewer desktops than we do and the rows past its array print
  `VP: N/A`; on 5.27 the two are byte-identical.
* **`-b`.** Both axes of `add,maximized_vert,maximized_horz` land: the
  backend waits for each state to be applied before the next one reads it
  back. `shaded` works on 5.27 for **X11 windows only** — KWin shades
  nothing else — and warns on 6 (shading removed). When KWin accepts a state
  and does not apply it (a window rule, or size hints a fullscreen cannot
  satisfy), wwmctl falls back to the EWMH `_NET_WM_STATE` ClientMessage real
  wmctrl sends, which reaches an XWayland window through KWin's X-plane
  window manager; the fallback reads `_NET_WM_STATE` back rather than
  calling a sent message a success, so a compositor that drops the message
  is reported instead of being silently accepted.
* **`-n`.** KWin caps virtual desktops (20 on 5.27, 25 on 6) and keeps at
  least one; past that the count is capped at the limit and a warning names
  it — the command succeeds (rc 0), it does not fail.

## Hyprland, Wayfire, the wlroots floor and COSMIC

Four more window backends answer here, and what differs is what their compositor
publishes rather than anything wmctrl asks for.

* **`-m`** on a session with no X plane prints the compositor's own window-manager name
  rather than the backend token. `wlroots wm` on the wlr floor — that is the string every
  wlroots xwm publishes on its check window, byte-identical on labwc, Wayfire, river and
  sway — and `Smithay X WM` on COSMIC. It used to print `wlr`.
* **`-l`** lists X and native windows together everywhere, and on the floor and on COSMIC
  the X rows now carry their real X id and the X server's own `WM_CLASS`: measured on
  `resolute-wayfire`, `wwmctl -l -x` and the original `wmctrl -l -x` print the xterm's row
  byte for byte the same, id column included
  (`0x0040000c  0 xterm.XTerm           wf1 smokex` from both). The join is
  `wdotool/xid_match.py`, and the floor and COSMIC feed it title and a lowercased
  `app_id` against `WM_CLASS` and nothing else — no pid, no geometry — so a pair that
  nothing separates keeps id 0.
* **`-d`** works on the wlr floor wherever the compositor publishes
  `ext_workspace_manager_v1` (labwc, Budgie 10.10, Xfce 4.20 on Wayland) and on COSMIC,
  which has two workspaces. Where it does not (sway 1.11, Wayfire 0.10) the refusal names
  the protocol and the routes: version 1 where the compositor grows it, else route 2, the
  compositor's own IPC. On Hyprland `-d`, `-s` and `-r -t` all work — workspace N+1 is
  desktop N — and on Wayfire the desktop list is the 3x3 viewport grid flattened, so
  there are nine of them.
* **`-p`** prints 0 for every window on COSMIC: no pid exists in either COSMIC toplevel
  protocol. Hyprland and Wayfire both publish one.
* **`-e`** is refused on the wlr floor with the protocol named and the routes after it —
  `zwlr_foreign_toplevel_management_v1 carries no geometry and no stacking; not yet here,
  and the routes are the X plane for an XWayland window (AGENTS.md route 5, a real
  ConfigureWindow) or a patched compositor for a native one (route 6)`. COSMIC's is its
  own sentence with its own rung, because the protocol and the fix both differ: `the
  COSMIC toplevel protocol has no move, resize, raise or lower; not yet here, and the
  route is a patched cosmic-comp (AGENTS.md route 6)`. It works on Hyprland (a tiled window has to be floated first) and on Wayfire (which
  floats by default), where the size is exact only to the client's own quantisation —
  `wwmctl -e 0,10,20,300,200` on a foot read back `300x195`, one whole character cell.
* **On river 0.4** every mutating command is accepted by the compositor and changes
  nothing. `wwmctl` is told so by the backend and falls back to its EWMH route; see
  README footnote **(n)**.
