# wxprop — design contract

Drop-in `xprop` clone for Wayland: works on **XWayland windows** (real X properties,
byte-parity with real xprop) and **native Wayland windows** (a synthesized property
set printed in xprop's exact formats, so `xprop -id N WM_CLASS`-style script parsing
just works). Same house rules as the rest of the tree
([Technical.md](Technical.md)): pure-stdlib Python, no third-party dependency,
byte-parity oracles.

## On Wayland, with xprop installed, this runs xprop

Since the X11 proxy, `wxprop` hands over on a **Wayland** session as well as on an X11 one:
the original is exec'd with `DISPLAY` pointing at [`xw11`](XW11.md), which answers for
native Wayland windows as well as for Xwayland's. The clone still runs when the original
is not installed, when `W11_PROXY=never` (or `WXPROP_PROXY=never`) asks for it, when
`W11_PASSTHROUGH=never` is set, and whenever the command uses one of our own options --
of which `xprop` has none: every byte this clone takes, xprop takes too.
Everything this document says about the clone's output is still what the clone prints;
through the proxy the output is the original's, byte for byte, because it **is** the
original. The six rules in order are
[XW11.md § The wrapper rules](XW11.md#the-wrapper-rules).

## Planes

- **X windows** (id ≥ the X resource base, or found via the compositor tree's
  `"window"` field): everything goes through `wdotool.x11_mini` against the real
  XWayland server — genuine GetProperty/ListProperties output, -set/-remove via
  ChangeProperty/DeleteProperty, -spy via PropertyNotify events. Real xprop in the
  devshell is the byte oracle for these.
- **Native windows** (compositor node ids): synthesize the property set from
  compositor data and print it byte-compatibly: WM_CLASS(STRING) from app_id,
  WM_NAME/_NET_WM_NAME from title, _NET_WM_PID(CARDINAL), _NET_WM_DESKTOP,
  _NET_WM_STATE (fullscreen/hidden/sticky as applicable), WM_CLIENT_MACHINE,
  _NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NORMAL. `-len`/`-notype`/format
  args apply identically. `-set`/`-remove` on a native window: one clear error line,
  exit 1 (can't fake a property store). `-spy` on native: the backend's own window-event stream
  (sway IPC, the bridge's `WindowEvent` on GNOME, KWin's), reprinting a synthesized
  property when its source changes.
- Window selection: `-id` (0x-hex/decimal), `-name` (exact match on title, then on
  instance/class, dsimple.c's Window_With_Name semantics), `-root` (the X root when X
  is up: real root properties; without X, a synthesized minimal set built around
  `_NET_SUPPORTING_WM_CHECK`), and no selector at all, which is click-to-select
  through the backend's own picker with the stderr hint (the wwmctl pattern).
  `-frame` is a no-op flag: there are no reparenting frames on wlroots, and it is
  accepted.

## Notes

- Property VALUES for X windows are the server's truth — never synthesize for a
  window that has a real X id; parity diffs must be byte-exact vs real xprop for the
  same window (modulo _NET_ properties the compositor updates between calls — pin
  the window state first).
- Exit codes and stderr strings per xprop source (e.g. "No such property" wording,
  usage exit 1).
- `-display`, `-fs`, `-grammar` edge flags: -display honored for the X plane;
  -grammar prints the real grammar text.
- The whole option set, which is xprop 1.2.8's own and is what the usage text
  lists: `-help`, `-grammar`, `-display`, `-id`, `-name`, `-font`, `-remove`,
  `-set`, `-root`, `-len`, `-notype`, `-fs`, `-frame`, `-f`, `-spy`, `-version`.
  Single dash, all of them: `--help` and the other double-dash forms are
  `wxprop: unrecognized argument --help` and exit 1, which is the real tool's
  answer too, so there is no long option anywhere in this tool to document.
- `-font <name>` is real: XWayland serves the core fonts (xfonts-base), so the
  font plane is `OpenFont` + `QueryFont` on the X connection and the FONTPROPs
  print through xprop's *font* format table — which replaces the window one for
  the whole run, chosen by pre-scanning argv the way xprop.c does. Values carry
  no type, so no `(TYPE)` is printed and a property the table does not name
  falls back to xprop's default `0x` (bare hex). `-remove`/`-set` on a font are
  the oracle's own `… works only on windows, not fonts`, and `-spy` dumps and
  exits. Byte-identical to `xprop -font fixed` on GNOME 46.

## GNOME

On GNOME the compositor plane is the w11 bridge
(`wdotool.backend_gnome.GnomeBackend`, `gnome/README.md`); wxprop uses its
typed hooks — `views()`, `workspaces()`, `x_info()`, `events()`,
`select_window()` — ahead of the sway tree path, which is unchanged.

* **Planes.** `views()` says which windows are XWayland (`xid ≠ 0`) and
  which are native. `-id` takes an X id or a bridge id: an XWayland
  window's bridge id (`wdotool search` output) redirects to its X id and
  the real X properties, exactly as a sway node id does; a native window
  gets the synthesized set. **X ids match first**, across every window,
  before any bridge or node id: `-id` is an X window id in xprop's manual,
  and the two number spaces are not disjoint — KWin mints its own ids and
  Mutter's are Mutter's — so one window's compositor id can equal
  another's X id. An id the bridge does not know is handed to
  the X server like xprop would — but only when Xwayland is actually
  running (a typo must not spawn a server; see below), else `window id #
  0x… does not exists!`.
* **Atom ids.** The native plane has no X server to allocate atoms, so it
  numbers the EWMH names itself, from `0x40000000` — deliberately outside
  the range any X server hands out, so a numeric id copied out of a native
  window's dump and fed to a real X tool fails loudly instead of naming a
  plausible wrong atom.
* **Native windows** synthesize, in this order and in xprop's grammar:
  `_NET_WM_STATE` (Mutter's own atom order, with `SHADED` at the front on
  Cinnamon only: `SHADED`, `SKIP_TASKBAR`,
  `MAXIMIZED_HORZ`, `MAXIMIZED_VERT`, `FULLSCREEN`, `HIDDEN` (minimized or
  show-desktop; a shaded window is HIDDEN too, since muffin's own
  `meta_window_x11_set_net_wm_state` writes the pair for a window still
  showing on its workspace), `ABOVE`, `DEMANDS_ATTENTION`, `STICKY` — sway's
  synthesis keeps its `FULLSCREEN, HIDDEN, STICKY` subset, and every backend
  but muffin leaves `View.shaded` False so no other desktop's dump moves),
  `_NET_WM_WINDOW_TYPE` from `Meta.WindowType` (`DESKTOP`, `DOCK`,
  `DIALOG` (also for `MODAL_DIALOG`), `TOOLBAR`, `MENU`, `UTILITY`,
  `SPLASH`, `DROPDOWN_MENU`, `POPUP_MENU`, `TOOLTIP`, `NOTIFICATION`,
  `COMBO`, `DND`, else `NORMAL`), `_NET_WM_DESKTOP` (`0xFFFFFFFF` when
  sticky), `_NET_WM_PID`, `WM_CLIENT_MACHINE` (hostname), `WM_CLASS`
  (`app_id`, `app_id` — the same pair `wwmctl -lx` prints; a window with a
  `WM_CLASS` pair but no app id uses that pair), `_NET_WM_NAME`, `WM_NAME`,
  `WM_STATE` (`Normal`/`Iconic` from the window's visibility, icon window
  `0x0` — Mutter writes it on every X11 window it manages, so a script
  that asks "is this minimized?" gets the same answer on both planes).
  `WM_TRANSIENT_FOR` appears when the bridge reports a parent, and
  `_NET_WM_STATE_FOCUSED` last in `_NET_WM_STATE`, where Mutter's own
  `set_net_wm_state` puts it. A `WM_NAME` that does not fit latin-1 is
  typed `UTF8_STRING`: type `STRING` *means* ISO 8859-1 and xprop re-encodes
  it for the locale, so UTF-8 bytes typed `STRING` print as mojibake.
  Nothing else is invented (no `_NET_FRAME_EXTENTS`, no `WM_HINTS`).
  `-set`/`-remove` on them fail with the usual one line.
* **The X plane** is opened with the `DISPLAY`/`XAUTHORITY` the bridge
  reports (`x_info()`: gnome-shell's own, else Mutter's
  `$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` cookie found by
  `w11common.session`), which is what makes `ssh root@`, `sudo` and a GNOME
  custom-shortcut process all work; `-display` still wins and then uses
  `$XAUTHORITY`/the session cookie. Mutter spawns Xwayland **on demand**,
  so wxprop connects only when an XWayland window is listed or an
  `Xwayland` process exists (`session.xwayland_running()`): `wxprop -root`
  and `-name` on a purely native desktop never start an X server. If
  Xwayland is up but unreachable, an XWayland window degrades to the
  bridge's view of it (`WM_CLASS` from Mutter's pair).
* **`-root`** — the documented choice: with Xwayland up the target is the
  **real X root** (Mutter is a full EWMH window manager for Xwayland:
  `_NET_SUPPORTING_WM_CHECK` → the `GNOME Shell` check window,
  `_NET_WORKAREA`, `_NET_SHOWING_DESKTOP`, `_NET_SUPPORTED`, … all real,
  `-set`/`-remove` go there) **with six properties re-synthesized from the
  bridge**, because the X root only ever sees X clients:
  `_NET_CLIENT_LIST` and `_NET_CLIENT_LIST_STACKING` (every window, by the
  id the tools print — X id for XWayland, bridge id for native — in
  Mutter's stacking order), `_NET_ACTIVE_WINDOW` (the focus window on
  either plane, `0x0` when none), `_NET_NUMBER_OF_DESKTOPS`,
  `_NET_CURRENT_DESKTOP`, `_NET_DESKTOP_NAMES` (the workspace manager
  directly; Mutter's X root tracks the same values, but only once an X
  client exists). Without Xwayland the root is the synthesized set alone,
  same as on sway, plus `_NET_CLIENT_LIST_STACKING` and
  `_NET_DESKTOP_NAMES`, with `_NET_SUPPORTING_WM_CHECK` = `0x0`. Real
  xprop on the same session would print the X-only list; the merged view
  is the point of the tool.
  `-set`/`-remove` always address the real X root, never the synthesis.
  That gap is where damage used to disappear: `wxprop -root -remove
  _NET_CLIENT_LIST` breaks every EWMH client on the X plane (`wmctrl -l`:
  *Cannot get client list properties*) while `-root` went on printing the
  compositor's healthy-looking list. Writing or removing one of the six
  now prints a line on stderr saying where the write went, and reads of
  that name for the rest of the run come from the X root. It is *not*
  enough to treat a missing override as damage: Mutter writes
  `_NET_CURRENT_DESKTOP` on the X root only once the workspace first
  changes, so a fresh GNOME 46 session legitimately has none while the
  compositor knows the answer.
* **`-spy`** on an XWayland window is the X `PropertyNotify` loop. On a
  native window it follows the bridge's `WindowEvent` signals
  (`backend.events()`): `title` reprints `WM_NAME`/`_NET_WM_NAME`,
  `fullscreen_mode`/`minimized`/`urgent` reprint `_NET_WM_STATE`,
  `workspace` (also on stickiness changes) `_NET_WM_DESKTOP` and
  `_NET_WM_STATE`, `close` ends with exit 0; the window is re-read from
  the bridge before each print. On the root without Xwayland the
  `new`/`close`/`focus` window events and the `WorkspaceEvent`s reprint
  the synthesized set; with Xwayland the two streams are merged — the X
  root's own `PropertyNotify`s for everything Mutter owns, the bridge for
  the six synthesized names (an X-side update of one of those is *not*
  reprinted: it would show the X-only view).
* **Limitations, not defects.** xdg-shell has no window-type hint, so the
  bridge reports `NORMAL` for a GTK dialog: wxprop prints
  `_NET_WM_WINDOW_TYPE_NORMAL` where the window's XWayland twin would print
  `DIALOG`. Under `-len` truncation, out-of-range `?$n=` thunks read
  *uninitialised heap* in real xprop — the same binary prints `window
  gravity: Forget` for a full dump and an empty value for the same
  truncation with explicit atoms, and `-len 8` on a `_NET_WM_ICON` renders
  an icon out of whatever followed the buffer. Byte parity there is
  unattainable in principle; `fmt._oob_thunk` prints a stable value and
  `format_icons` stops at the budget. Real `xprop -id 99999999999999999999`
  segfaults; we report the id.
* **Click-to-select** (no `-root`/`-id`/`-name`) is the backend's
  `select_window()` with a stderr hint that follows the backend: *click* the
  target window on GNOME (the bridge's grab) and KDE (KWin's picker), *focus*
  it on sway, whose IPC has no picker. wxprop continues with the window it
  answers, on whichever plane it lives.
  **`-name`** keeps xprop's semantics first — pre-order `QueryTree` walk
  from the X root, exact `WM_NAME` match, frames included (Mutter's
  `mutter-x11-frames` windows may carry the client's title, bug-for-bug)
  — when Xwayland is up, then exact title, then exact app id over the
  bridge's windows.
* **Errors** are one line, exit 1: without the bridge, click-to-select
  says `can't select a window: gnome backend: the w11 bridge
  extension is not running in GNOME Shell; run gnome/install-bridge.sh …`,
  `-id N` for a window the X server does not have says `cannot look up
  window id # 0x…: gnome backend: …`, `-root` without Xwayland `cannot
  examine the root window: gnome backend: …`; `-name` keeps `No window with
  name … exists!`. Unit coverage: `tests/test_wxprop_gnome.py` on the
  mock bridge plus an in-memory X server stand-in.

Verified live (same rigs as WWMCTL.md's GNOME section, GNOME 46 and 50):
the full `-id <xterm>` dump is byte-identical to real `xprop` (31 lines);
`-root` differs from real `xprop -root` in exactly the merged names
(`_NET_CLIENT_LIST(_STACKING)` and `_NET_ACTIVE_WINDOW` covering the
native windows — Mutter's X root lists the xterm only and, on 50, names
its own no-focus window `0x200003` as active — plus `_NET_CURRENT_DESKTOP`,
which Mutter's X root does not carry); native windows dump the synthesized
set; a bridge id of the xterm redirects to its X properties; `-spy` on the
calculator reprints `_NET_WM_STATE` through fullscreen/hidden toggles and
exits 0 when it is closed; `-root -spy` prints the focus change and the
workspace switches (a switch can arrive more than once, as an X
`PropertyNotify` storm would); `-name "Both by wwmctl"` finds the
`mutter-x11-frames` frame first, exactly as real `xprop` does;
`-set WM_NAME` / `-remove WM_ICON_NAME` on the xterm are read back by real
`xprop`; click-to-select returns with the window `wdotool windowactivate`
focused; all of it also from `ssh root@` with `env -i`, under `sudo` and
from a custom shortcut.

## KDE Plasma

On Plasma the compositor plane is `wdotool.backend_kwin.KwinBackend` (KWin
scripting, nothing installed) and wxprop uses the same typed hooks as on
GNOME — `views()`, `workspaces()`, `x_info()`, `events()`,
`select_window()`.

* **Planes.** `-id` takes an X id or a backend id: an XWayland window's
  backend id redirects to its real X properties (byte-identical to real
  `xprop` on both releases), a native window gets the synthesized set. The
  backend ids KWin's uuids are minted into are 32-bit and biased to
  `0x40000000`, out of the range Xwayland gives its clients, so the two id
  spaces cannot be confused — that bias is why `wxprop -id`, which parses
  into an XID like `dsimple.c` does, can carry them at all.
* **`_NET_WM_STATE` for native windows.** Read from KWin's own properties.
  On 5.27 `maximizeMode` is not scriptable, so MAXIMIZED_HORZ/VERT are
  derived from the frame being exactly the maximize area; a window sized to
  fill the work area by hand therefore reads as maximized there.
* **`-root`.** `_NET_CLIENT_LIST`, `_NET_CLIENT_LIST_STACKING`,
  `_NET_ACTIVE_WINDOW` and `_NET_DESKTOP_NAMES` are ours (they list native
  toplevels and the true active window); KWin's own X root copies are stale
  on the desktop count. Everything else is the X server's.
* **`-spy`** works on both releases, on the root and on a window, native or
  X11: the backend's `events()` is a KWin script that stays loaded for the
  iteration and pushes `workspace`'s and every window's Qt signals out.
