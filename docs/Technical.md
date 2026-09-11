# Technical.md — how the tree is put together

This is the orientation document. It describes the tree **as it is now**, after the
0.3 subtraction: one hit-test, one number parser, one layout decision, one detach
protocol, one transform table, a shared `w11common/` package, and four display
backends that are one shape. Read it if you are about to change something and want to
know where that something lives.

The user-facing documents are elsewhere and this file does not repeat them: the
[README](../README.md) is the install and the per-desktop support, and each tool has a
contract of its own — [WDOTOOL.md](WDOTOOL.md), [WWMCTL.md](WWMCTL.md),
[WXPROP.md](WXPROP.md), [WXRANDR.md](WXRANDR.md), [WARANDR.md](WARANDR.md),
[WMIRROR.md](WMIRROR.md), plus [gnome/README.md](../gnome/README.md) for the bridge
extension and [vm/README.md](../vm/README.md) for the rig.

## 1. The six tools

Six commands, seven Python packages, no third-party dependency anywhere except the
system GTK 3 bindings that `warandr` imports at run time — plus `xw11`, which is the
seventh command and the eighth package and is not a clone of anything
([§ 13](#13-the-x11-proxy)).

| package | command | clones | talks to |
|---|---|---|---|
| `wdotool/` | `wdotool` | xdotool 4.20260303.1 | `/dev/uinput`, `zwp_virtual_keyboard_v1`, `zwlr_virtual_pointer_v1`, one of eight window backends, and one of five desktop readers for the active keyboard layout |
| `wwmctl/` | `wwmctl` | wmctrl 1.07 | one window backend, plus the X plane through `x11_mini` |
| `wxprop/` | `wxprop` | xprop 1.2.8 | the X plane through `x11_mini`, plus one window backend for native windows |
| `wxrandr/` | `wxrandr` | xrandr 1.5.4 | sway IPC, Hyprland's IPC, `zwlr_output_management_v1`, Mutter's DisplayConfig, the same under Muffin's name, KWin's output protocol |
| `warandr/` | `warandr` | arandr | `wxrandr` or the real `xrandr`, as a child process |
| `wmirror/` | `wmirror` | nothing — there is no X11 original | the external `wl-mirror`, whose lifetime it owns |
| `w11common/` | — | — | shared by all six |
| `xw11/` | `xw11` | nothing — it is an X **server** front end, not a client clone | the session's X server upstream, one window backend, one display backend and the input daemon downstream ([§ 13](#13-the-x11-proxy)) |

`w11common/` holds what more than one tool needs and nothing else does, in eight
modules: `session.py` (which session is this, and where are its sockets), `passthrough.py`
(the X11 handover), `dbus_mini.py` and `wayland_mini.py` (the two wire clients),
`errors.py` (`CmdError`, the exception every command in the tree raises and catches),
`stdio.py` (the exit-status rule for an output that never reached its reader),
`procs.py` (detached children) and `distro.py` (the distribution family from
`/etc/os-release` — `debian`, `fedora`, `arch`, `nixos` or none — and the install command
that follows from it, over six package keys: `xdotool wmctrl xprop xrandr wl-mirror
gtk3-python`, with an unknown or missing os-release getting Debian's, which is what every
message said before). It is a package rather than a corner of `wdotool`
because that list is exactly what the *display* tools use of it: they find a session,
they talk D-Bus and Wayland, and they never type a key, never open a window backend
and never start the input daemon. It imports nothing outside the standard library, and
nothing in it imports anything of `wdotool`: the package is closed, which is what lets
a zipapp of a display tool carry it and nothing else.

`wdotool/` is a second shared layer, but only for the two window tools: `wwmctl` and
`wxprop` drive its window backends and its X11 wire client (`wdotool/x11_mini.py`).
The three display tools do not touch it at all, which is what their bundles show: they
carry `w11common` and their own packages, and nothing of `wdotool` (see § [The
single-file builds](#the-single-file-builds)). It was not always so. Until this release
three small files sat in `wdotool/` and were imported by the display tools, and because
a bundle copies whole directories rather than the modules actually reached, those three
dragged the keysym table, the daemon and every backend into each of them.

Each package's `VERSION` is derived from one constant, `w11common.VERSION`.
`pyproject.toml`, `debian/changelog` and `flake.nix` state the same number for their
own build systems, and `scripts/build-deb.sh` refuses to build when the first two
disagree. `wwmctl`'s and `wxprop`'s user-visible version strings are the *oracle's*
numbers (`1.07`, `xprop 1.2.8`) and deliberately not ours.

### The single-file builds

`scripts/build-pyz.sh` writes one zipapp per tool into `dist/`. Each is a plain
`python3 -m zipapp` with a `/usr/bin/env python3` shebang and a
`w11-clone:` stamp in its first few hundred bytes, which is the head sniff
`w11common.passthrough.is_us()` uses to recognise a copy of ourselves installed under
an original's name. What each bundle contains:

| bundle | packages inside | why |
|---|---|---|
| `dist/wdotool` | `w11common`, `wdotool` | the tool itself |
| `dist/wwmctl` | `w11common`, `wdotool`, `wwmctl` | the window backends and `x11_mini` live in `wdotool` |
| `dist/wxprop` | `w11common`, `wdotool`, `wxprop` | same |
| `dist/wxrandr` | `w11common`, `wxrandr` | the tool itself: nothing of `wdotool` is reached any more |
| `dist/warandr` | `w11common`, `wxrandr`, `warandr` | on Wayland it runs the same interpreter with `-m wxrandr`, `PYTHONPATH` pointing at the zipapp itself |
| `dist/wmirror` | `w11common`, `wxrandr`, `wmirror` | it reads the layout through wxrandr's own wlr client, and the detached supervisor is this same zipapp re-entered by fork |

zipapp copies whole package directories, so a bundle that needs three files of
`wdotool` carries all of it. Do not state a byte size for any of these here: the
script's contents are the statement that keeps.

## 2. Session discovery, and the X11 handover

Two modules answer two questions that look like one, and keeping them apart is the
whole design.

* **`w11common/session.py` answers "where is the session?"** — which runtime
  directory, which Wayland socket, which session bus, which `DISPLAY`, which X
  cookie, for a caller that may be root with an empty environment. It is what makes
  `sudo wdotool key a`, `ssh root@box wwmctl -l` and a `@reboot` cron job work.
* **`w11common/passthrough.py` answers "is this session ours to serve?"** — and, when
  it is not, `execve`s the real `xdotool`/`wmctrl`/`xprop`/`xrandr` with argv
  untouched. It has to decide **before** any backend is detected, because backend
  detection would half succeed on an X11 session: GNOME-on-Xorg owns
  `org.gnome.Shell`, KWin-on-X11 owns `org.kde.KWin`.

The first is a search. The second is a policy, and it is the policy that runs first.

Session sockets (`$XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`, `SWAYSOCK`, the user D-Bus)
are discovered by scanning `/run/user/*`, and there is one finder per compositor that
has a socket of its own:

* `find_sway_socket()` looks for `$SWAYSOCK`, `$I3SOCK`, `sway-ipc.*.sock` in the
  runtime dirs, `i3/ipc-socket.*` one level down, and
  `<TMP_DIR>/i3-<user>.XXXXXX/ipc-socket.*` owner-checked. The old `i3-ipc.*.sock`
  pattern matched nothing any i3 or sway has ever written; i3 exports `$I3SOCK` into
  the processes it spawns and not into its own environ, so on i3 the scan is the only
  route for anything the compositor did not start.
* `find_hypr_socket()`: `$HYPRLAND_INSTANCE_SIGNATURE`, else `hypr/*/.socket.sock` in
  the runtime dirs, preferring the instance directory that still holds
  `hyprland.lock` — five stale ones accumulated after five restarts. The event socket
  is `.socket2.sock` beside it.
* `find_wayfire_socket()`: `$WAYFIRE_SOCKET`, `$_WAYFIRE_SOCKET`, `wayfire-*.socket`
  in the runtime dirs, then `/tmp` owner-checked. The recorded name has an empty pid
  field (`wayfire-wayland-1-.socket`), so the match is on the prefix and the suffix.

`_SESSION_LEADERS` gained `i3, mate-session, cinnamon-session, cinnamon, lxqt-session,
lxsession, openbox, labwc, wayfire`, appended after `sway` so the existing desktops keep
winning. `comm` is matched as `name[:15]` and as `("." + name + "-wrapped")[:15]`,
because comm truncates at 15 bytes and nixpkgs wraps GUI programs — nixpkgs' gnome-shell
runs as `.gnome-shell-wr`, and the truncation is *built* rather than guessed at with a
`-wr` suffix, because a name of twelve or thirteen characters leaves no `-wr` in the
comm at all (`kwin_wayland` becomes `.kwin_wayland-w`). `find_xauthority()` also globs
`<runtime dir>/gdm/Xauthority`, which is where GDM keeps an X11 session's cookie:
measured on `noble-gnome-x11` as `-rwx------ 1 test test 130`, and it IS the seated
session's own `$XAUTHORITY`. And there is a third test seam beside `RUN_USER_DIR` and
`X11_SOCKET_DIR`, `TMP_DIR`, for the two compositors that can run with no runtime
directory at all (i3 and Wayfire). Candidate runtime dirs are anchored on the
graphical session: a dir holding a `wayland-*` socket sorts first (so `ssh root@`
with its own empty `/run/user/0` still finds the user's bus), then `SUDO_UID` /
`PKEXEC_UID`, then real users. The X plane (Xwayland) is found by
`session.find_x_display()` / `find_xauthority()`: `$DISPLAY`/`$XAUTHORITY`, the
session leader's own environment via `/proc` — gnome-shell, `startplasma-x11`,
`kwin_x11`, `plasmashell`, `xfce4-session`, `sway`, uid-qualified, which is the only
route to SDDM's `/tmp/xauth_<random>` — Mutter's
`$XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*` cookie, then `/tmp/.X11-unix/X*`.

The **cookie order matters and was measured**: SDDM 0.20 keeps its cookie in
`/tmp/xauth_<random>`, which is in none of the other three places, so the session
leader's `/proc/<pid>/environ` is the only route to it. A system account's runtime
directory is skipped, because the lowest-numbered one on a box with a display manager
is the *greeter's* and its cookie authorises nothing on the user's X server. uid 0 is
never an answer from either source: `sudo -i` run *by* root leaves `SUDO_UID=0`
behind, and believing it sends the search into `/root`. `x11_mini._session_xauthority()`
resolves the uid explicitly for the same reason and never searches root's: from
`ssh root@box` on a live SDDM + LXQt session, `session.session_uid()` answers 0 —
`/run/user/0` is a real runtime directory and the first candidate — and the search then
went to root's own environment and `/root/.Xauthority`, neither of which belongs to the
graphical session, whose cookie SDDM had written to `/tmp/xauth_<random>` under uid 1000.
With no other candidate the call is what it always was.

### The X11 handover (`w11common/passthrough.py`)

We are installed **over** the originals, so on a plain X11 session (Xfce, i3, MATE,
Cinnamon, LXQt/Openbox, GNOME/KDE on Xorg) the right thing to do is get out of the way: the X
server is authoritative there, `xdotool` has XTEST and `--sync` on real X
events, `xprop` has the real property store, `xrandr` has the real RandR, and
we cannot beat any of it from outside. Worse, backend detection would *half*
succeed — GNOME-on-Xorg owns `org.gnome.Shell`, KWin-on-X11 owns
`org.kde.KWin` — so the check has to run **before** it. Measured on the
`noble-kde-x11` flavor (Plasma 5.27 on Xorg): `backend_detect.detect()` there
does answer `KwinBackend`, the script backend does load into `kwin_x11` and
list its windows — and on the same session our own `getdisplaygeometry` has
nothing to ask, because a Plasma X11 session has no compositor socket at all.
`wxprop` is the one tool with a native X11 path of its own, so it is also the
one that could reach that backend *after* the handover declined (no real
`xprop` installed): `wxprop.core._detect_backend()` therefore answers `None`
outright on an X11 session, and `-root` is the X root, as the original's is.
That last one is hardening rather than a fix — measured on both Plasma X11
images the merged root was byte-identical to the real `xprop`, because every
window on an X11 session is an X window; what it removes is the synthesized
root the same code produces when the compositor's view carries no X id.

`w11common/passthrough.py` is pure stdlib and imports nothing from the rest of the
tree except `session.py`. Change its API deliberately: five `main()`s and the whole
of `tests/test_passthrough_exec.py` are written against the shape below.

**`session_kind() -> "wayland" | "x11" | None`**, ordered, memoised, and
reading nothing but the environment it is handed plus three seam directories
(`_X11_SOCK_DIR`, `_LOGIND_DIR`, `_RUN_USER_DIR` — which is what makes the
tests hermetic):

1. `W11_PASSTHROUGH` / `WDOTOOL_PASSTHROUGH` & co: `never` -> wayland
   (run our own code whatever the session), `always` -> x11. Those variables
   are about the *handover*, so a caller that never hands over passes
   `respect_override=False` and skips this step — see `warandr` below;
   `passthrough_mode()` is the way to ask about the variables themselves.
2. `$WAYLAND_DISPLAY` **and** its socket exists -> wayland. Wayland is tested
   first because `$DISPLAY` is set on a Wayland session too (Xwayland) and is
   therefore never evidence of an X11 session — while a live compositor
   socket is conclusive.
3. `$XDG_SESSION_TYPE`, ignored when `SUDO_UID`/`PKEXEC_UID` is set (`sudo`
   keeps root's `XDG_SESSION_TYPE=tty` from an `ssh root@` login).
4. logind's own record, `/run/systemd/sessions/*` (world-readable key=value,
   read-only best-effort; `<id>.ref` is a FIFO and is never opened): the
   active, local, non-greeter session of the target user, and its `TYPE=`.
5. Socket scan: a `wayland-*` socket **owned by the target user** — a display
   manager's Wayland greeter under another uid must not turn an Xfce box into
   a Wayland session, which is the one real trap in this design — else an X
   socket (or a `host:0` display, which the original handles and we do not).
6. Nothing -> `None`, and the tool prints its own "no session" error.

**`real_tool(name)`** walks `$PATH` for the first executable of that name that
is not us. Four independent "not us" guards, because each alone has a hole:
`samestat` against our own entry points; `basename(realpath(cand))` in
`{wdotool, wwmctl, wxprop, wxrandr, warandr}` (the normal install, where
`xdotool` is a *symlink* to our `wdotool`); a 4 KiB head sniff for the
`w11-clone:` stamp `scripts/build-pyz.sh` writes into every zipapp
(the build fails without it) or for an import of one of our packages, which
is what a `pip`-generated console script looks like — never an ELF, and
never a bare `w11`/`wmctrl` *substring*, or a third-party wrapper
that merely mentions the project would be skipped and the user told to
install what is already installed; and `_W11_PASSTHROUGH`, which
carries the realpaths already
handed over to — a process that finds *itself* in that list was exec'd as
somebody's "real tool" and refuses to go round again (plus a depth backstop).
`WDOTOOL_REAL_XDOTOOL` / `WWMCTL_REAL_WMCTRL` / `WXPROP_REAL_XPROP` /
`WXRANDR_REAL_XRANDR` skip the walk; set-but-unusable is an error naming the
variable, never a silent fallback.

**`maybe_exec_real(tool, args, ...)`** is the hook at the top of each
`main()`. It returns `None` (keep running) or an exit status; usually it does
not return at all, because the handover is `os.execve`, not `subprocess`:
exit status, death by signal, stop/cont, the controlling terminal and the
process group all survive for free, stdio stays the real fds (`xprop -root |
head -1`, `xdotool selectwindow`), and nothing extra shows up in `ps`. Two
things must happen first or parity silently breaks: `SIGPIPE` and `SIGXFSZ`
back to `SIG_DFL` (Python ignores both, and an *ignored* disposition survives
`execve`, so `| head -1` would print an EPIPE error instead of dying), and a
stdio flush. argv[0] is the original's own name, so its usage text is
internally consistent. No original installed: **127** (never confusable with
a tool failure) and one line naming the package to install and the override
variable — except for a `--help`/`--version`/bare invocation, which falls
back to our own output, and except for `wxprop` (below). The package that line names
follows `/etc/os-release` through `w11common/distro.py`: `apt install x11-utils` on
Debian and Ubuntu, `dnf install xprop` on Fedora, `pacman -S xorg-xprop` on Arch and
`nix-env -iA nixpkgs.xorg.xprop` on NixOS, with an unidentifiable distribution getting
Debian's, which is what every box printed before. Help is recognised
by each original's *exact* spellings (`-h -V --help --version` for wmctrl,
`-help -version -grammar` for xprop, `-h -v --help --version help version`
and `-hv`-style clusters for xdotool, `-help --help -v --version` for
xrandr): `-v` is `--verbose` in wmctrl and unknown to xprop, and a looser
rule would read `wmctrl -v -l` as a help request and answer it with a
Wayland error where `wmctrl -l` correctly says which package to install.

**Backend precedence and the argv look-ahead (wxrandr, warandr).** One
rule, everywhere: **`--backend NAME` beats `$WXRANDR_BACKEND` beats
auto-detection**, `auto` is the default, and detection is unchanged. `NAME`
is `auto`, `x11` or one of wxrandr's own backends (`sway`, `wlr`,
`mutter`/`gnome`, `kwin`/`kde`); `x11` *is* this handover. Which means the
hook above has to know about the flag before anything is parsed:
`wxrandr.cli.scan_backend_argv()` walks argv with xrandr's own option
arities and reports `--backend NAME` / `--backend=NAME` and whether
`--print-backend`/`--backends` are present — so `--backend sway` on an X11
session runs our own code, `--backend x11` on a Wayland session hands over
(`maybe_exec_real(..., force=True)`; the keyword was added for exactly that,
and it does not override `entry`), the
two informational options never hand over (the original would only answer
`unrecognized option`), and an *output* named `--backend` (`--output
--backend --off`) is a value, not a flag. The flag itself is stripped from
the argv the original is exec'd with: real xrandr has no such option, and
neither has it `--persistent`, which is stripped the same way (an X11 apply
then saves nothing, as it always did, instead of the original refusing the
whole command over a flag it has never heard of) or
`--unsafe-gnome-overlap`, which is refused before the handover in the words
every non-GNOME session refuses it in. Forcing
a backend that is not available here is one line naming what was missing and
exit 1, never a silent fallback; a `--backend` with no value is still
the flag (the scan returns `""`), so its error is ours on both kinds of
session. `$WXRANDR_BACKEND` keeps its older behaviour (no pre-check), because
those bytes are pinned — except for the single value `x11`, which the hook
does read: the handover is settled before parsing, so a variable that only
reached `Session` could ask this process to be something it can no longer
become, and would have to answer with a fatal about a flag nobody typed.
warandr sits on top of all of it and never hands over at all: it *chooses*
which tool to run and runs it as a child, which is what lets the window
switch backends while it is open.

**Environment repair.** On the X11 path a missing or dead `$DISPLAY` /
`$XAUTHORITY` is replaced with the session's own (logind's `DISPLAY=`, the
socket scan, the display manager's cookie), so `sudo xdotool key a`,
`ssh root@box xprop -root` and cron jobs work *through* us where the original
alone fails — the Wayland trick of `session.py`, applied to X. Values that
already work are never touched, and a `$XAUTHORITY` that points at nothing is
*removed* rather than forwarded (left in place it suppresses the original's
own `~/.Xauthority` default). The repair is `repair_x_env()` and the handover
is only its first caller: warandr's X11 runner is a *child*, so it never
reaches `child_env()`, and until it took the repair for itself it was the one
tool in the repo that still answered `Can't open display` from a root shell
while the other four worked in the same one. A repair is not a guarantee:
where the X server has no cookie file at all — wlroots starts Xwayland with no
`-auth`, so only the session user's own processes may open it — there is
nothing to find, and the real `xprop` fails from that shell too (`wxprop` falls
back to the compositor's synthesized properties; see the repo README, *Desktop
support*).

Whose session, though: as root with no `SUDO_UID` (`ssh root@box`, root cron)
the uid is in neither the environment nor `getuid()`, and `session_uid()` then
asks logind. Failing that, a system account's runtime directory is skipped —
the lowest-numbered one on a box with a display manager is the *greeter's*,
and its cookie authorises nothing on the user's X server, so forwarding it
would break precisely the case this repair exists for. Same rule as
`find_wayland_socket()`. uid 0 is never an answer either, from either source:
`sudo -i` run *by* root leaves `SUDO_UID=0` behind, and believing it sends the
search into `/root` — measured on a real Xfce box, that is the difference
between `sudo -i xdotool getactivewindow` printing the window name through us
and printing `Authorization required, but no authorization protocol
specified`.

**Per tool.** `wdotool`, `wwmctl` and `wxrandr` exec, always. `wdotool` has no
native X11 option worth having (no XTEST, no `XKeysymToKeycode`, no `--sync`
on X events; uinput would inject, but `getmouselocation` would report our
tracked pointer and `--clearmodifiers` could clear only what uinput
itself holds, missing anything the X server or XTEST put there — the
documented Wayland approximations on a platform that has none). `wxrandr`:
the X server's RandR is the truth, and our Mutter backend on GNOME-on-Xorg is
at best a second opinion. `wwmctl` *does* carry an X11 wire client
(`wdotool/x11_mini.py`), and it is still not enough: `-m`, `-d` viewport and
workarea, `-e` gravity math, `-r -b` state toggles, `-x` class matching and
`:SELECT:` (which needs `GrabPointer`/`QueryPointer`, not in `x11_mini`) would
all have to be reimplemented and their byte parity re-proved against the real
`wmctrl` on X11, for the sole benefit of a box with no `wmctrl` installed —
and `x11_mini` is unix-socket only, so it cannot do `ssh -X`'s
`DISPLAY=localhost:10.0` while a handover does that for free. `wxprop` is the
exception: its native X11 path is already complete and proven against a live
X server (`WXPROP.md`), and `core.Session` resolves the X plane from
`$DISPLAY` with no backend at all, so it hands over when a real `xprop`
exists and keeps running when none does (`fallback_native=True`) — no 127
from `wxprop`, ever. From a script's point of view the four behave
identically: on X11 the output is the original's.

`warandr` does **not** exec — it is not a clone of an X11 binary we are
installed over, and it already drives the real `xrandr` on X11. It only swaps
`randr.choose()`'s bare `$WAYLAND_DISPLAY` test for
`passthrough.session_kind(respect_override=False) == "wayland"` (the
`respect_override` is load-bearing: `W11_PASSTHROUGH=never` means
"do not hand over", and warandr has nothing to hand over — read as a session
type it would select `wxrandr` on an X11 box and every Apply would say
`Can't open display`, for exactly the developers the variable is documented
for), which fixes a stale
`WAYLAND_DISPLAY` selecting `wxrandr` (the GUI came up and every Apply said
`Can't open display`) and makes a thin `.desktop` environment work. It still
writes the bare word `xrandr` into `~/.screenlayout/*.sh` for arandr
compatibility; if we are installed over `/usr/local/bin/xrandr` that word
resolves to us and passes through — one extra process, correct result, no
recursion.

**Never exec'ing the test runner.** ~17 tests call `cli.main([...])`
in-process and several spawn our tools as subprocesses, so an unguarded hook
would `execve` the suite away (and `tests/test_cli_parity.py`, which shells a
shim named `xdotool` while the real one is on PATH, would compare the real
xdotool with itself and pass tautologically). Three independent belts:
`entry=False` — an explicit argv means we are being used as a library, and a
library never replaces its caller's process; `tests/conftest.py`; and the
`W11_PASSTHROUGH=never` line every `tests/test_*.py` carries (the
suite is run file by file, where conftest never loads), which a test in
`tests/test_passthrough.py` enforces for future files.

## 3. The wire clients, and their error models

Six modules speak a protocol on a socket, and there is exactly one of each. None of
them imports anything outside the standard library, and none of them spawns a helper
binary (`gdbus`, `busctl`, `swaymsg`, `hyprctl`, `xprop`) to do its talking.

| module | speaks | error model |
|---|---|---|
| `w11common/dbus_mini.py` | D-Bus, session bus or any `unix:` address | `DBusError(name, message)` for ERROR replies **and** for local failures, under `org.freedesktop.DBus.Error.` + `NoServer`/`AuthFailed`/`NoReply`/`Disconnected`. Nothing socket-level escapes: a peer that closes mid-SASL comes back as `Disconnected`, not as a bare `ConnectionResetError` |
| `w11common/wayland_mini.py` | the Wayland wire protocol | exceptions from the socket, with a deadline on every roundtrip. A wedged compositor times out and the caller degrades, rather than hanging the daemon |
| `wdotool/x11_mini.py` | the X11 core protocol against Xwayland or Xorg | two classes, and every caller treats both as "degrade gracefully": `XUnavailable` for anything connection-level (no server, bad `DISPLAY`, auth rejected, connection lost) and `X11Error` for errors the server reports (BadWindow and friends) |
| `wdotool/hypr_ipc.py` | Hyprland's request socket (`$XDG_RUNTIME_DIR/hypr/<sig>/.socket.sock`) | send the request as text, read the reply to EOF, close — one connection per request, which is the whole protocol. `j/<name>` for JSON, `dispatch`/`keyword` for the two mutating verbs, and `.socket2.sock`'s `name>>payload` line stream for events |
| `wdotool/backend_wayfire.py`'s `_WayfireIPC` | Wayfire's JSON IPC | a native-endian int32 length and a JSON body, both ways; one connection for commands and one per `watch()` |
| `wdotool/backend_detect.py` | nothing itself — it decides which window backend to build | one `ListNames` over `dbus_mini` answers the KWin, GNOME and Cinnamon questions at once, and the connection is handed to the backend that wins rather than opened twice |


**The detection order** is: `WDOTOOL_BACKEND` → the sway/i3 IPC socket → the Hyprland
IPC socket → `org.kde.KWin` / `org.gnome.Shell` / `org.Cinnamon` (one `ListNames`) →
the Wayfire IPC socket → one registry round trip, which picks `wlr`
(`zwlr_foreign_toplevel_manager_v1`) or `cosmic` (`ext_foreign_toplevel_list_v1` plus
`zcosmic_toplevel_info_v1` and no wlr manager) → rc 2. The registry is read once per
process and cached beside the `ListNames`. `WDOTOOL_BACKEND` takes `sway` (also on i3),
`hypr`, `wayfire`, `wlr`, `cosmic`, `kwin`, `gnome`, `cinnamon`; `i3` is accepted as a
spelling of `sway` and is not in the refusal's list of eight.

Three rules make that order behave under partial evidence. **The socket arms carry on
and the registry arm does not.** sway/i3, Hyprland and Wayfire catch the backend's
`CmdError` and go on down the order, because a socket is weak evidence — it can be a
stale file. The registry arm does not: the global list `session_registry()` has just
read IS the evidence that the protocol is there, so if `WlrBackend`/`CosmicBackend` then
refuses, that refusal is the answer. Before this, a wlr failure fell through to a
sentence claiming the compositor offered neither family, about a compositor that had
just advertised one. **`org.gnome.Shell` is not proof of GNOME Shell.** The GNOME arm is
taken only when one of GNOME's own two names is beside it on the session bus —
`org.gnome.Mutter.DisplayConfig` (gnome-shell's own) or `org.w11.Bridge` (which
can only be owned from inside gnome-shell). With neither, the registry decides, and a
compositor that publishes a foreign-toplevel protocol is not Mutter, which publishes
none. That is measured rather than argued: on the `resolute-budgie` golden
(2026-09-09, `busctl --user list --acquired`, recorded whole in
`tests/fixtures/live/busnames-resolute-budgie-10.10.2.txt`) `org.gnome.Shell` is owned by
**budgie-power-dialog**, pid 2697, and no `org.gnome.Mutter.*` name is on that bus at
all — so before the fix every window command on Budgie answered with a bridge hint about
logging out of a GNOME Shell that was not there. The GNOME arm is still never swallowed
where nothing below it could answer: a session that owns `org.gnome.Shell` and whose
compositor offers neither toplevel family still gets the bridge hint, because Mutter
publishes neither and that is the shape a real GNOME session has. **A backend that is
not in this install refuses in one line.** `_hypr()`, `_wayfire()` and `_cosmic()` turn a
`ModuleNotFoundError` for their *own* module into `CmdError("<name> backend: not built
into this install")`, so a slimmed install answers instead of printing a traceback; a
`ModuleNotFoundError` from deeper inside a backend that IS installed is that backend's
bug and stays visible.

`x11_mini.py` lives under `wdotool/` and not under `w11common/` on purpose: it already
imports `w11common.session`, and moving it into `w11common` would make that a cycle.
Its three callers are `wwmctl.core` (the X plane of XWayland windows: `WM_CLASS`,
`WM_CLIENT_MACHINE`, geometry, EWMH ClientMessages), `wxprop.core` (all of its
X-window work) and `wdotool.backend_kwin` (the XWayland ids KWin 6 does not export).
It carries enough of the core protocol for that and nothing more: InternAtom,
GetProperty with the long-property offset loop, ChangeProperty, SendEvent, GetGeometry
plus TranslateCoordinates, QueryTree, GetInputFocus as the post-void-request sync, and
OpenFont/QueryFont/CloseFont for `wxprop -font` — the one place it allocates a
resource id. No extensions, no big-requests, byte order `l` only. Property values of
format 32 come back as unsigned 32-bit ints (EWMH's `-1` reads as `0xFFFFFFFF`), and
`get_prop_string()` truncates at the first NUL exactly like wmctrl's `printf("%s")`.

`wayland_mini.py` is the smallest of the four: a registry, `bind`, per-object
handlers, `roundtrip`, and file-descriptor passing. Its callers are the daemon
(`wl_output` geometry, preferring `zxdg_output` logical size and position when
advertised), `backend_wlr` (foreign-toplevel), `vkbd`/`vptr` (the two virtual-device
protocols), `wxrandr`'s wlr and KWin backends and `wxrandr/gamma.py`.

```
c = WlConn(socket_path)
reg = c.get_registry()                # {name: (interface, version)} after roundtrip
oid = c.bind(name, "wl_output", min(version, 4))
c.on(oid, handler)                    # handler(opcode, Cursor, fds)
c.roundtrip()                         # dispatch until the sync callback fires
```

The rest of this section is `dbus_mini` in full, because it is the one whose wire
details a change is most likely to trip over.

### `dbus_mini` in full

Pure-stdlib D-Bus client for the session bus and any `unix:` address (QEMU's
`-display dbus` bus). No gdbus/busctl spawns, no glib, signals included. Shared like
`wayland_mini.py`: wire-level fixes are fair game, API changes need a note.

- **Wire**: `unix:path=`/`unix:abstract=`/`unix:runtime=yes` (`;` alternatives, `,`
  key=value, `%XX` unescaped); SASL `\0AUTH EXTERNAL <hex(uid)>` → `OK` →
  `NEGOTIATE_UNIX_FD` (ERROR tolerated) → `BEGIN` → `Hello`. Message = 12-byte fixed
  header + `a(yv)` fields + pad 8 + body; fields written PATH, DESTINATION, INTERFACE,
  MEMBER, ERROR_NAME, REPLY_SERIAL, SENDER, SIGNATURE, UNIX_FDS (byte-identical to
  libdbus's Hello). On read each known field must carry its fixed signature and a
  call/signal/reply/error its required fields (else ValueError); unknown field codes
  and unknown message types (5+) are ignored — such frames are dropped, fds closed. Full
  grammar `ybnqiuxtdhsog a() a{} v`; alignment relative to message start (the pad after
  an array length to the element alignment is not counted and is present for empty
  arrays); reads both endians, writes `l`. struct↔tuple, array↔list (`ay`↔bytes),
  `a{}`↔dict, `v`↔`Variant(sig, value)` on write (plain bool/int/float/str/bytes are
  guessed), plain value on read (`wrap_variants=True` keeps `Variant`). `h` = index into
  the SCM_RIGHTS fds of the same sendmsg (`socket.send_fds/recv_fds`), resolved to real
  fds on read. Max message 2^27 bytes.
- **API**: `Bus(addr=None, as_uid=None)` (addr from `session.find_user_bus()`),
  `call(dest, path, iface, member, sig='', args=(), timeout=25.0) -> tuple`,
  `get_property`/`set_property`/`get_all_properties`, `introspect`, `list_names`,
  `name_has_owner`, `get_name_owner`, `request_name`, `add_match`,
  `wait_signal(iface, member, timeout, path=None, sender=None)` (None on timeout),
  `messages(timeout)` generator of queued signals (and calls with `serve_calls=True`),
  `reply`/`error_reply`/`emit_signal`, `close()`/context manager. `DBusError(name,
  message)` for ERROR replies and local failures (`org.freedesktop.DBus.Error.` +
  `NoServer`/`AuthFailed`/`NoReply`/`Disconnected`). Method calls aimed at us get
  UnknownMethod immediately (Peer.Ping answered) unless `serve_calls`. One `Bus` per
  thread. `Bus(timeout=)` bounds connect (SO_SNDTIMEO covers the kernel wait on a full
  AF_UNIX backlog), auth, Hello and every later send — the socket stays in timeout mode
  and a send that times out closes the connection (Disconnected); `call(timeout=)`
  bounds the reply (NoReply). Fds in a received message belong to whoever takes it and
  are CLOEXEC; fds on frames the client discards (late replies, auto-answered calls,
  unknown types, anything still queued at `close()`) are closed by the client.
- **Root vs the user's bus**: stock session.conf has no `<allow user="*"/>`, so
  dbus-daemon answers root's EXTERNAL auth with `OK` and then closes the socket at the
  policy check (Hello dies with EPIPE). `Bus(as_uid=uid)` — and the automatic retry
  when euid 0 is turned away by a socket owned by another uid — forks a child that
  `setgroups/setgid/setuid`s, connects, authenticates and Hellos, then hands the live
  socket back over a socketpair with SCM_RIGHTS (`connect_as_uid`); SO_PEERCRED is
  fixed at connect, so the bus keeps attributing the connection to that uid.
  `bus.auth_path` is `'direct'` or `'fork'`. A child failure comes back under its own
  error name (NoServer for a missing socket, AuthFailed for REJECTED, AccessDenied "needs
  root" when not root); a child still silent `timeout + 5` s in is SIGKILLed and reaped
  (NoServer). Verified on Ubuntu 24.04 dbus-daemon 1.14.10: user → direct, `sudo` → fork.
- **CLI**: `python3 -m w11common.dbus_mini [--address A] [--as-uid N|owner] --names |
  --has-owner NAME | --call DEST PATH IFACE MEMBER [SIG JSON-args] | --get DEST PATH
  IFACE PROP | --get-all DEST PATH IFACE | --introspect DEST PATH | --monitor [RULE…]
  [--seconds N]`. Output is JSON (variants unwrapped, `ay` as int lists). Exit 0, 1
  (D-Bus/OS error; Ctrl-C and a closed stdout exit quietly like wwmctl), 2 (usage).
- **Tests**: `tests/test_dbus_mini.py` — byte-exact marshalling facts, the canonical
  128-byte Hello, big-endian parse, DisplayConfig `GetCurrentState`/`ApplyMonitorsConfig`
  fixtures, QEMU `SetUIInfo(qqiiuu)`, an in-process mock bus (auth, names, echo of every
  type, errors, timeouts + late replies, signals, unix fds both ways, client↔client
  calls, fork hand-off, CLOEXEC on received fds, fd release for auto-answered calls /
  unknown types / `close()`, typed header fields, connect and send timeouts against a
  full backlog / a non-reading peer, a stuck child being killed, CLI option errors and
  Ctrl-C); with `DBUS_SESSION_BUS_ADDRESS` set (`dbus-run-session --
  python3 tests/test_dbus_mini.py`) also a real dbus-daemon incl. NameOwnerChanged from
  a second connection. Verified against QEMU 8.2's display bus (`org.qemu`, Console
  properties, `SetUIInfo`).

## 4. Window backends

Eight backends implement one interface, `wdotool/backend.py:WindowBackend`, and three
tools drive them: `wdotool`'s window commands, all of `wwmctl`, and `wxprop` for
native windows. A backend is an object with these methods, and nothing above it
reaches into a backend's privates any more.

**The core**, which every backend must answer: `list()`, `find(wid)`,
`activate(wid)`, `focus(wid)`, `close(wid)`, `kill(wid)`, `move_window(wid, x, y)`,
`resize(wid, w, h)`, `minimize(wid)`, `map(wid)`, `unmap(wid)`, `raise_(wid)`,
`lower(wid)`, `set_state(wid, state, action)`, `maximize_pair_state()`,
`is_mapped(wid)`, `display_size()`, `window_desktop(wid)`,
`set_window_desktop(wid, n)`, `get_desktop()`, `set_desktop(n)`, `num_desktops()`,
`set_num_desktops(n)`, `select_window()` with its `select_window_hint`.

**The optional hooks**, which default to "not available" so a caller falls back to
`list()`/`find()`: `views()` (typed `View` records — X ids of XWayland windows,
`WM_CLASS` instance and class, app id, states), `workspaces()` (names and work
areas), `move_to_current_desktop(wid)`, `x_info()` (the compositor's own `DISPLAY`
and cookie path), `pointer()` and `events(timeout)`. These exist so that a backend
which knows more than a `Window` carries can hand it to `wwmctl`/`wxprop` without
those tools reaching into `SwayBackend._nodes()`.

**One hit-test, not three.** `Window.window_type` is filled in by
`GnomeBackend._win` and `KwinBackend._win`, and `backend.hit_test(wins, x, y)` is the
single implementation with the single `_LAYER_TYPES` table: it looks through DESKTOP
and DOCK layers for `getmouselocation`'s window field. There is no per-backend
`window_at` override and no `getattr` probe in `_window_under_pointer` any more. It
runs over `list()` and deliberately not over `views()`: `views()` is an extra round
trip and carries no workspace bit.

**One place decides how many round trips a listing costs**, and that is asserted:
`tests/test_backend_gnome.py` pins the bridge call list for a hit-test at exactly
`["ListWindows"]`.

### Window ids, per compositor

The id is what a script pipes around, so its semantics are per compositor and worth
knowing before writing one.

| backend | id | stable? | accepts an X id? |
|---|---|---|---|
| **sway** (`backend_sway.py`) | the sway node id | for the life of the window | no — an X id is not a node id |
| **i3** (`backend_sway.py`, the i3 dialect) | the window's **X id**, the same number `wmctrl -l`, `xwininfo` and `xprop -id` use | for the life of the window | yes, it is one. i3's own container ids are 47-bit pointers; the id this printed truncated to `0x5168c680` and `wxprop -id` answered `BadWindow` |
| **GNOME** (`backend_gnome.py`) | `Meta.Window.get_id()`, through the bridge | for the life of the window | XWayland windows also carry their real X id in `views()`, which is what `wwmctl -l` prints |
| **Cinnamon** (`backend_cinnamon.py`) | `get_stable_sequence()`, with the xid straight from `get_xwindow()` | for the life of the window | `get_xwindow()` is the real X id for an X client and 0 for a native one, so this backend needs no matching against `_NET_CLIENT_LIST` at all. Not `get_id()`, which is a ~3e9 counter — two windows measured 3070932382 and 2959920136 |
| **KDE** (`backend_kwin.py`) | minted: `0x40000000 \| 30 bits of internalId`, because the scripting API has no numeric window id at all | while the window lives | no. The range is deliberately outside the one Xwayland hands its clients, so a native id is never mistaken for an X id in the same listing |
| **Hyprland** (`backend_hypr.py`) | minted from the compositor's `address` with `backend.mint_id()` | for the life of the window, and the same in two processes | XWayland windows are joined to `_NET_CLIENT_LIST` through `wdotool/xid_match.py` |
| **Wayfire** (`backend_wayfire.py`) | the view's own `id` from `window-rules/list-views`, unchanged | while the view lives | nothing is minted, and nothing collides with an Xwayland id — Wayfire's ids start at 1 and count up |
| **COSMIC** (`backend_cosmic.py`) | `backend.mint_id(identifier)` over the 32-character `identifier` — `0x40000000 \| 30 bits of blake2b` | for the life of the window, across processes | no, and it is out of Xwayland's range on purpose |
| **wlr** (`backend_wlr.py`) | `1000000 + enumeration order` | within one process only — closing the first-arrived window renames the survivor | no |
| **a shadow, through `xw11`** (`xw11/shadow.py`) | `rid_base \| n` out of the 21-bit range the X server allocated to the proxy's own connection — `0x600001` for the first one on the rig | for the life of the proxy, which is the session or fifteen idle minutes, whichever ends first. An Xwayland restart re-bases every one of them | it **is** one, in the sense that matters: it is inside the range that X server promised the proxy, so it can never collide with a real Xwayland window and an upstream error for one is impossible. A toplevel the compositor reports a real X id for gets no shadow at all |

KWin's minting is 32-bit clean because every X-shaped consumer truncates there
(`wxprop -id` parses into an XID, the synthesized `_NET_CLIENT_LIST`, wmctrl's
`0x%08lx`), and two uuids colliding in those 30 bits re-mint the second window rather
than dropping it out of the listing.

The **XWayland id of a KWin 6 window** is the hardest case in the tree and is worth
reading before touching it: `x11window.h` lost every scriptable property in Plasma 6,
so `View.xid` is matched against the X server's own `_NET_CLIENT_LIST` — a pid and
`WM_CLASS` filter, then a title and geometry distance score, greedy best first. A
pair must *agree* on pid or class: an X client that publishes neither contradicts
nothing, and matching it on geometry alone would hand its id to a native window,
which would then claim to be an X11 client. Where nothing separates two candidates
they keep id 0 rather than being handed one of two ids. It runs only when an Xwayland
process already exists, because connecting to the X plane must not *start* one.

`sway`'s desktop mapping is the other one to know: wmctrl and xdotool want a dense
0-based desktop list and sway has sparse, named workspaces. `SwayBackend.workspaces()`
sorts on the raw `num` and computes `index = num - 1 if num > 0 else -1`, so a named
workspace and the scratchpad both land on `-1`, which collides with wmctrl's own
`-1` for "sticky". That is inherent to the mapping, and `-R` / `-t -1` sidestep it by
using sway's own "workspace current".

**Wayfire's desktop mapping** is the third one to know, and it is not a list at all:
each output owns a 3x3 grid of viewports (`window-rules/list-outputs` →
`workspace {x, y, grid_width: 3, grid_height: 3}`). The tools flatten it
`index = y * grid_width + x` on the focused output's grid, so `get_num_desktops` is 9 on
a stock Wayfire and `set_desktop 4` is the middle cell. A view's desktop is the viewport
its centre falls in, *relative to its own output*: view geometry is expressed against the
viewport currently in front, so a view one screen to the left of it reads a negative `x`
(measured: with the viewport at (1, 0), a view on (0, 0) of a 1280-wide output read
`x: -882`), and the output's own origin has to come off first on a multi-head layout. A
sticky view is on all nine and reports desktop -1, as it does everywhere else.

**The X-id matcher, and who has how much of it.** `wdotool/xid_match.py` is KWin's
matcher moved out of `backend_kwin.py` unchanged — pid and `WM_CLASS` are filters, title
and geometry distance are the score, the position in each list breaks a tie, and a pair
that agrees on nothing keeps xid 0. Every backend whose compositor publishes toplevels
with no X ids on them reads it, but not all of them can feed it the same keys: Hyprland
and Wayfire hand it pid, class, title and geometry, while the **wlr floor and COSMIC
have title and a lowercased `app_id` against `WM_CLASS` and nothing else** — no pid, no
geometry, no list-order tie-break — so a tie there keeps xid 0 more often.

**The desktop mapping of the wlr floor.** Desktops exist wherever the compositor
publishes `ext_workspace_manager_v1` (labwc, Budgie 10.10, Xfce 4.20 on Wayland, COSMIC)
and the refusal stands where it does not (sway 1.11, Wayfire 0.10).
`wdotool/ext_workspace.py` is the client: `activate` on the handle plus `commit` on the
manager, workspaces ordered by `(coordinates, arrival)`, which covers both COSMIC
(coordinates `[1]`, `[2]`) and labwc/Budgie (no `coordinates` event at all).
`window_desktop` stays -1 on every one of them, because neither foreign-toplevel protocol
carries a workspace association.

The per-backend measured detail — what each compositor does with maximize, shading,
raise, lower, ids, and every quirk that has a test pinning it — is
[WDOTOOL.md § Backend notes](WDOTOOL.md#backend-notes).

## 5. Input: the daemon, two injection paths, one layout decision

`wdotool/daemon.py` owns everything that injects. The first wdotool command of a
session double-forks it (`argv[1] == "__daemon"`), it creates the uinput devices once
(~600 ms of compositor hotplug latency, paid once), and it serves JSON lines on a
unix socket under `session.runtime_dir()`. Every later command is a client.

Three facts about it decide most of its code:

1. **A held key belongs to a connection.** The compositor releases whatever a client
   holds the instant that client disconnects, so `keydown ctrl` from a process that
   exits is not a hold at all. That is why there is a daemon and not a library.
2. **There are two sinks, and a hold cannot move between them.** `/dev/uinput` and
   the Wayland protocols are different devices, and only the device that pressed a
   key can release it. `_own_sink()` and `_own_pointer()` exist for exactly that.
3. **One policy picks the sink, for both halves**: `key`/`keydown`/`keyup`/`type` go
   through `zwp_virtual_keyboard_v1`, and
   `click`/`mousedown`/`mouseup`/`mousemove`/`mousemove_relative` through
   `zwlr_virtual_pointer_v1`, **when the matching kernel device cannot be opened and
   the compositor implements that protocol** — through `/dev/uinput` in every other
   case, with `--vkbd on|off` (`WDOTOOL_VKBD`) forcing either.

**And it does not live for ever.** A daemon that can no longer be reached — its
socket file deleted, which is what logging out does to `$XDG_RUNTIME_DIR`, or
replaced by a second daemon's — exits, and so does one nobody has used for fifteen
minutes; neither can touch a daemon with a client connected or a key held down. The
period, the check interval and the two off switches are in
[WDOTOOL.md § The input daemon](WDOTOOL.md#the-input-daemon).

**One layout box, from one source — except where one source cannot answer.** Every
absolute pointer coordinate is mapped across the layout bounding box the daemon reads
off the Wayland wire, so which *pixel space* that box is in decides where a move
lands. `zxdg_output_v1`'s logical geometry is that source, and it stays that
source in every state measured — including the one GNOME 46 state where it is stale
(Fractional Scaling switched on under an already-scaled monitor, after which Mutter
stops re-sending `logical_size`), because Mutter maps absolute pointer motion across
the same stale rectangle it is advertising. Reading the box from DisplayConfig there
instead, which is the obvious repair and was implemented and measured, lands every
target at twice the coordinate asked for. What that state really breaks is
`getdisplaygeometry`, which then describes a desktop that is not being drawn, and on
the wire it is byte for byte a legitimate physical-mode session where the same numbers
are right. So `wdotool/layoutbox.py` gates on the signature the two share (a head whose
logical size is its raw mode size while it claims `wl_output.scale` >= 2), asks
`org.gnome.Mutter.DisplayConfig` only then, and turns a disagreement into one
diagnostic rather than a different box. The gate is the point: the common session never
opens a bus, so the check cannot cost anything anywhere else.

**One layout decision, in one function.** `xkbmap.decide(text, group, mode)` is the
single answer to "which character table does this keystroke use?", and
`xkbmap.layout_mode(forced)` is the single answer to "which mode are we in?". Three
callers share them: `_Daemon._layout`, `keys_cmds.Layout.load` and
`xkbmap.diagnostic_main` (`wdotool __keymap`). `xkbmap.fetch(keymap=, group=)` takes
its overrides as keyword arguments rather than writing `os.environ`, which is what
made `WDOTOOL_LAYOUT=us wdotool __keymap --chars z` agree with `type` instead of
disagreeing with it.

**Which group is live, and who is asked.** `xkbmap.choose_group` settles it out of
the keymap wherever the keymap can settle it (one group, or several binding the same
symbols), and returns group 1 *flagged as assumed* where it cannot. That flag is the
seam. `fetch()` calls `xkbmap.desktop_group` at exactly that point and nowhere else,
so a plain US session, a one-source session and GNOME's `us,us` never open a bus, and
`--layout us` still runs no layout code at all. Five readers sit behind it, one per
desktop, each its own gate. The three bus readers do `NameHasOwner` before the first
call (a method call to an unowned name asks the bus to *start* that desktop, and a
GNOME box with `kwin` installed must not have one launched at it) and keep one
connection for the life of the process; the two socket readers ask `w11common.session`'s
finder instead, which is a scandir of `$XDG_RUNTIME_DIR`, and connect per question
because Hyprland's IPC is one connection per request anyway. Sockets are tried first for
that reason, so the order is hypr, wayfire, kwin, gnome, cinnamon. All five: one
reconnect and then a ten-second backoff, a bus or socket without that desktop behind it
remembered as absent so the process asks once and never again, and `None` for every
failure so the guess and its notice stand exactly as they did.

* **KDE** (`xkbmap.KwinLayouts`): `org.kde.KWin` `/Layouts`
  `org.kde.KeyboardLayouts.getLayout` answers the **0-based index** of the active
  layout in the configured list, and that list is the keymap's group order name for
  name, so the group is `index + 1`, clamped against `group_count`. Same object, same
  meaning on Plasma 6.6 and 5.27.
  **Never kded, and this is a live hazard rather than a preference.** Two other
  objects declare the same interface, `org.kde.kded6 /modules/keyboard` and the kded5
  one, and calling `getLayout` on either **crashes kded** — measured on both
  generations, every call answered `NoReply` and the bus name changed owner
  afterwards, taking the user's background service down with it. Nothing here calls
  them, there is no fallback to them, and there must never be one:
  `tests/test_xkbmap.py`'s `KdedLandmine` sits on the mock bus beside the fake KWin
  and fails the test if anything ever calls it.
* **GNOME** (`xkbmap.GnomeInputSources`): `org.gnome.desktop.input-sources`, read
  through `org.freedesktop.portal.Settings.ReadAll`. The portal is the route because
  dconf has none — `ca.desrt.dconf` publishes `Init`, `Change` and the `Notify`
  signal and **no read method** — and it is the one portal call in the tree (§ 10).
  `mru-sources[0]` is the live source, written on every switch and restored at login;
  `current` is deprecated and ignored whatever its name suggests. The mapping is
  `index % 3 + 1`, clamped the same way, and the `% 3` is Mutter's doing: it appends
  its own `us` group after the user's sources, XKB allows four groups, so past three
  sources it recompiles the keymap in chunks of three around whichever source is in
  use (`de,fr,gr,ru,es` is `de, fr, gr, us` until Spanish is picked and then
  `ru, es, us`), and the group is the source's index *within its chunk*. Per-window
  layouts, an `mru-sources` head no longer in `sources`, and a source that is not an
  `xkb` layout are refused rather than answered.
* **Hyprland** (`xkbmap.HyprLayouts`): `j/devices` on
  `$XDG_RUNTIME_DIR/hypr/<sig>/.socket.sock`, the same socket `wdotool/hypr_ipc.py`
  serves the window backend from. Hyprland keeps XKB state **per device**, so the
  question is which keyboard row to read, and the recorded `devices.json` is the trap:
  four keyboards on one `us,de` session, `main: true` on wdotool's own
  `wdotool-virtual-keyboard`, index 1 on the physical `at-translated-set-2-keyboard`,
  and a `power-button` that is a keyboard to libinput and never switches anything. The
  rule is: never one of ours (`wdotool-virtual-keyboard`, `hl-virtual-keyboard-*` —
  reading the injected device's index would be reading back the state we set), then
  `main: true`, then a name that looks like a keyboard, then the first row.
  `active_layout_index + 1`, clamped against `group_count`. That skip-ours-first clause
  is what makes the `main` clause usable at all: measured live on 2026-09-09, Hyprland's
  `main: true` follows the last keyboard USED, and it was on `wdotool-virtual-keyboard`
  before a switch and on the physical keyboard after. Nothing is ever dispatched:
  `switchxkblayout` on the injected device is what broke typing outright in the VM, and
  `tests/test_xkbmap.py` fails if the reader sends anything but `j/devices`.
* **Wayfire** (`xkbmap.WayfireLayouts`): `wayfire/get-keyboard-state` over the JSON IPC
  → `{"possible-layouts": [...], "layout": ..., "layout-index": 0}`, the same 0-based
  index into the configured list KWin's `getLayout` gives, so the group is `index + 1`
  clamped the same way. It needs `plugins = ipc ipc-rules` and not the window backend's
  whole set. **`wayfire/set-keyboard-state` is never called and must never be**: one call
  recompiles the keymap as the selected layout *duplicated* (`possible-layouts` became
  `["English (US)", "English (US)"]` and the German layout was gone until restart), which
  is a wreckage this reader has to survive reading and never to cause —
  `tests/test_xkbmap.py`'s `WayfireSetLandmine` is `KdedLandmine`'s sibling. A
  `No such method found!` is a fact about the session's `plugins` line and is remembered
  like an absent bus name.
* **Cinnamon** (`xkbmap.CinnamonInputSources`): `org.cinnamon.desktop.input-sources`,
  read through `org.Cinnamon.Eval` with one read-only program. The schema's keys are
  `sources`, `current`, `show-all-sources` and `xkb-options` and there is **no
  `mru-sources`**, so unlike GNOME the live index really is `current` and the whole
  mapping is `current + 1` clamped — no chunking, because muffin appends no group of its
  own. No portal call and no fork-and-drop-privileges dance either: Eval identifies
  nobody, so a root daemon reads what the session user reads. Two refusals, both because
  the setting then describes no single live layout: an index that is not in `sources`,
  and a source that is not an `xkb` one. The program is a constant with nothing
  interpolated into it, ever.

**COSMIC needs no reader at all**, which is why it is not in that list. cosmic-comp
publishes `zcosmic_keyboard_layout_manager_v1`, whose `group` event the protocol XML says
is "received even when the client has no focused window" — the single sentence that
separates it from `wl_keyboard.modifiers` — so `_fetch_wayland` binds it, calls
`get_keyboard_layout(new_id, wl_keyboard)` and takes the group off the wire, the way
sway's arrives. One extra round trip, and only where the interface is advertised and the
keymap has more than one group. `Snapshot.source` therefore says `wayland` on COSMIC, not
`wayland + <desktop>`. Whether the event reaches a client that has never held focus is
not measured, so a group that never arrives leaves the guess.

Two failures are told apart, because they deserve different answers. An error whose
*name* describes the session rather than the moment, meaning this compositor has no
such object, method, interface or property, or this bus will not let us at it, is
permanent: the answer would be the same next time, so the reader records that this
desktop cannot answer and never asks again for the life of the process. That is what
keeps a sway or an X11 session from paying a round trip per command for a question
with no answer. A name that merely says nobody owns it right now is deliberately not
in that set, because a compositor restarting is exactly the case that must recover,
and it is covered by the one reconnect and the ten second backoff instead.

**The GNOME read costs a fork, and the fork is not an optimisation.** Typing goes
through `/dev/uinput`, so the daemon is root whenever it was started under `sudo`,
and the portal identifies its caller by opening `/proc/<pid>/root` and answers the
session user and nobody else. So `xkbmap._read_all_as()` forks, drops to that user,
puts `PR_SET_DUMPABLE` back (`setuid()` clears it, and a process that is not dumpable
has a `/proc/self` only root may open, which is precisely what the portal is not),
connects, calls, and pipes the answer home as JSON: 6.3 ms against the session user's
1.2. `Bus(as_uid=)` exists for this shape of problem and cannot serve here, because
its child hands the socket back and exits, leaving no process for the portal to
identify (`AccessDenied: Unable to open /proc/N/root`).

The measured behaviour of all of this — the reverse map, the US bypass, the group
read and the guess behind it, the ceiling map for the absolute axis, the
unchanged-`EV_ABS` nudge, the `--clearmodifiers` kernel facts, and every defect the
live sessions found — is
[WDOTOOL.md § The input daemon](WDOTOOL.md#the-input-daemon) and
[§ Typing and clicking with no privilege](WDOTOOL.md#typing-and-clicking-with-no-privilege---vkbd).
This file does not repeat it.

**One number parser.** `wdotool/cnum.py` is C's `atoi`/`atof`/`strtol` with C's
semantics (leading space, optional sign, stop at the first character that is not a
digit, and `[0-9]` rather than `\d`, so a Unicode digit gives 0 exactly as C does).
`wwmctl/core.py` and `wxprop/cli.py` import it aliased as `_atoi`. There is one copy,
and the parity tests are what keep it honest.

**One getopt wrapper.** `cli._opts` takes the command name and parses one command's
own flags out of the remaining argv, and every chainable command uses it.

## 6. Display: six backends, one shape

`wxrandr` has six Wayland backends and one handover:

| backend | protocol or interface | picked when | persistent store |
|---|---|---|---|
| `sway` | sway/i3 IPC (`GET_OUTPUTS`, batched `output ...` commands in one `RUN_COMMAND`) | a sway or i3 IPC socket exists | — |
| `hypr` | Hyprland's own IPC: `j/monitors all` to read, one `keyword monitor NAME,WxH@Hz,XxY,SCALE[,transform,N][,mirror,OTHER]` per touched output to write | a Hyprland IPC socket exists | `hyprland.conf` |
| `wlr` | `zwlr_output_management_unstable_v1`, one atomic configuration apply | the compositor advertises it | — |
| `mutter` | `org.gnome.Mutter.DisplayConfig` on the session bus, one `ApplyMonitorsConfig` | GNOME | `~/.config/monitors.xml` |
| `cinnamon` | `org.cinnamon.Muffin.DisplayConfig` — the Mutter backend under Muffin's name | Cinnamon | `~/.config/cinnamon-monitors.xml` |
| `kwin` | `kde_output_management_v2`, with device objects found two ways | Plasma | KWin's own |
| `x11` | the real `xrandr`, by `execve` | an X11 session, or `--backend x11` | the desktop's |

The auto order is `sway, hypr, kwin, mutter, cinnamon`, with `wlr` as the fallback that
is never probed for the decision; `--backend` takes `auto, x11, sway, hypr, wlr, mutter,
cinnamon, kwin` with the aliases `gnome`→`mutter`, `kde`→`kwin`, `muffin`→`cinnamon` and
`hyprland`→`hypr`. `--backends` prints those seven rows in that order in an 8-character
name column, and warandr's *Layout > Backend* menu carries the same eight entries in the
same order. `--print-backend --verbose` says
`compositor: COSMIC (wlr-output-management)` when the registry also carries
`zcosmic_output_manager_v1`, and `compositor: wlroots (XDG_CURRENT_DESKTOP=labwc:wlroots)`
— or `Budgie`, `XFCE`, `LXQt:labwc:wlroots` — when the variable is set and is not `sway`;
the token stays `wlr` either way.

**`cinnamon` is the `mutter` backend with three names swapped.** `GetCurrentState` and
`ApplyMonitorsConfig` are byte-for-byte Mutter's signatures, `APPLY_SIG` applies
unchanged, and a real mode change applied and read back with only the bus name, the
object path and the interface changed. Muffin carries Mutter's validator with Mutter's
strings (`Logical monitors not adjacent`, `Logical monitors overlap`, `Logical monitor
scales must be identical`, `Config contains multiple primary logical monitors`), so
everything §6 says about adjacency, gaps, mirroring and one-primary applies verbatim —
and on three heads a live muffin was made to print `not adjacent` for the first time.
Eight behaviours that used to be keyed on the token `mutter` are keyed on the
implementation's flavour instead, and every GNOME string is byte-identical because the
words come off `wxrandr/mutter.py:Flavor` (`.name`, `.desktop`, `.compositor`). Muffin
also still exports `GetCrtcGamma`/`SetCrtcGamma`, which mutter's GNOME 46/50 do not; even
so `--brightness`/`--gamma` answer `--brightness/--gamma are not supported on Muffin (no
gamma LUT API)` with rc 0 rather than dying on the wlr path's `cannot set gamma: no
wayland socket`.

**`hypr` exists rather than `wlr`, and that is a measurement rather than a preference.**
Hyprland advertises `zwlr_output_manager_v1` version 4 and takes exactly one apply per
session through it: the second times out after 10 s with nothing changed and no
`[COutputConfiguration] Applying configuration` in Hyprland's own log; with a second
output present even the first one hangs; `wlr-randr`, the reference client, hangs for
ever on the same request. Measured on 0.53.3 and again on 0.56.2. `keyword monitor` on a
session that has not touched the protocol applies at once, which is the route this
backend takes, and every apply is verified by re-reading `j/monitors all`: enabled or
disabled, position, mode size and transform — but **not** the scale, because asked 1.37
Hyprland applied 1.33 and answered `ok`. `j/monitors all` and not `j/monitors`, because
a head Hyprland has disabled is not in the plain answer at all: the row vanishes rather
than gaining `disabled: true`.

**`WlrOutputs.apply` is no longer a single send.** It sends, reads the heads back, and
sends the identical configuration a second time when `_stray_head()` finds an enabled
output away from the position the apply put it at; a third disagreement is a `Fatal`
naming the output and both numbers. That second send exists for labwc, which answers
`succeeded` and then lays the heads out itself — measured on labwc 0.9.3 / wlroots
0.19.2 with three heads, where `--output Virtual-3 --off` followed by `--auto` left
`V-1 0,0  V-3 3840,0  V-2 5760,0` for an apply that asked for `V-1 0,0  V-2 1920,0
V-3 3840,0`, and three applies later a head had drifted to `+9600+1080`. It is not our
wire: the three `set_position` requests were read off it in the guest and carried exactly
the numbers asked for, and `wlr-randr` 0.4.1 produces the identical layout from the
identical starting state. Re-sending lands every head where it was asked, on the first
retry, every time it was tried, so the cost is one extra apply on a compositor that has
rearranged and nothing at all on one that has not. labwc 0.9.3 is the only compositor
measured that reaches the second send; sway 1.11 forced onto `--backend wlr` does not,
and Hyprland is not on this path at all.

**They are one shape.** Each implements `snapshot(state)`, `predicted_dims(t, state)`,
`verify(...)`, `apply(state, targets, persistent)`, `close()` and `name`, and
`cli.Session` holds exactly one of them in `self.impl`. There used to be four handles
and six name tests scattered through `Session`; there is now one attribute, and
"a backend is an object" is a true sentence about this code rather than an aspiration.

**One transform table.** `core.WL_SPEC_RANDR_VIEW` plus `to_wl_spec_transform` /
`from_wl_spec_transform` replace the twin tables that used to sit in `mutter.py` and
`kwin.py`. `core.RANDR_VIEW` remains as the alias the tests and
[WXRANDR.md](WXRANDR.md) already use.

**One mode resolver.** `core.match_mode` and
`core.resolve_real_mode(t, state, interlace_known)` are shared, and the
`interlace_known` flag is load-bearing rather than decorative: KWin's modes are
flagless and Mutter's are not, so a resolver that assumed either would pick the wrong
mode on one of them. There is a test for that divergence.

**Layout representations went from four to three.** What is on the screen is
`core.OutputState`; what was asked for is `core.Target`; what the user sees is the
rendered `--query` text. `wmirror` used to carry a fourth — its own `Output` class
and `outputs_from_heads()` — and now calls `snapshot_wlr(wlr, state=None)` instead,
which is what makes "wmirror can never disagree with `wxrandr --query`" a literal
statement about one code path rather than a claim about two.

**warandr keeps two of its own, and should.** `warandr/model.py:Layout` is the
canvas: a layout that has *not* been applied, with snapping, normalisation, clone
detection and an `overlap_refusal` sentence supplied by whichever backend is live.
`warandr/xrandr_parse.py` is the other: the text an `xrandr` or `wxrandr` invocation
renders, which is also the format of `~/.screenlayout/*.sh` and therefore has to
round-trip byte-identically, arandr's own files included. They cannot be one, because
one is an editable model of a screen that does not exist yet and the other is a
serialisation contract with a program from 2010.

### GNOME's saved display configuration is all or nothing

`~/.config/monitors.xml` holds one `<configuration>` per monitor set the user has ever
kept — the laptop alone, the laptop plus the desk monitor, the three heads at work —
and GNOME applies the entry whose monitors are plugged in. Mutter's **reader** verifies
every entry in the file with the same `meta_verify_logical_monitor_config_list()` that
`ApplyMonitorsConfig` uses, and **one failure discards the whole file**. Measured on
the 26.04 default install (GNOME 50.1, `resolute-gnome-iso`, three heads), on a file
holding a three-head layout and a two-head one, with one `<x>` in the two-head entry
edited so that its monitors overlap:

```console
$ journalctl -b | grep 'monitors config'
Failed to read monitors config file '/home/test/.config/monitors.xml': Logical monitors not adjacent
$ wxrandr --query | grep '^Virtual'      # at the next login
Virtual-1 connected primary 1920x1080+0+0      # Mutter's default row -- not the saved
Virtual-2 connected 1920x1080+1920+0           # three-head layout, which is still in
Virtual-3 connected 1920x1080+3840+0           # the file, untouched and perfectly valid
```

Nothing says so: no window, no notification, nothing on any screen. The file stays on
disk exactly as it was and every layout in it is inactive, at every login, until
somebody edits it back by hand. Mutter's **writer** verifies nothing —
`meta_monitor_config_manager_save_current()` serialises whatever configuration is
current — so a file in that state can be written by anything that reaches libmutter
without going through DisplayConfig (GNOME on Xorg derives its logical monitors from
the X layout with no verification at all; a Shell extension can ship its own typelib
and call the symbols DisplayConfig does not export). And the next confirmed save
rewrites the file **whole**, from what Mutter holds in memory, which after a discarded
read is only the layout being saved: that is the moment the other monitor sets stop
being recoverable. Measured, same rig: our confirmed `--persistent` on top of a
discarded file left one `<configuration>` where there had been three.

**No path through our own tools can put a bad entry in that file**, and that is
measured rather than argued — every route tried on both default installs (GNOME 50.1
on 26.04 and GNOME 46.0 on 24.04, three virtio heads, the "Keep changes?" dialog
confirmed with `wdotool key Return`, `sha256sum` on the file after every step):

| what was tried | what happened | the file afterwards |
|---|---|---|
| overlapping `--pos`, `--persistent` | refused: `Logical monitors not adjacent` | unchanged, byte for byte (absent on a fresh account, and still absent) |
| a gap in the row, `--persistent` | refused, same sentence | unchanged |
| a vertical overlap, `--persistent` | refused, same sentence | unchanged |
| `--same-as` between two different modes, `--persistent` | refused by us, before the bus | unchanged |
| a valid layout, `--persistent`, dialog left alone | applied, reverted after 20 s | **never written**: Mutter writes only on the confirmation |
| a valid layout, `--persistent`, the session killed while the dialog was up | the old layout at the next login | never written |
| a valid layout, `--persistent`, confirmed | applied | Mutter writes it: the entry for *this* monitor set is replaced, every other entry survives byte for byte |
| a second monitor set (one head unplugged), confirmed | applied | a second `<configuration>` appended; both verify, and plugging the head back in restores the first |
| `warandr`: Apply, from the GUI, of a layout loaded from a saved script | applied through `wxrandr`, no `--persistent` | unchanged |
| `warandr`: Save As / `--save` | writes a layout **script** (`~/.screenlayout/*.sh` shape) | unchanged; warandr never writes this file |
| any apply without `--persistent` | applied | unchanged, and the file is not even opened |

The asymmetry that makes this safe is Mutter's own: `ApplyMonitorsConfig` validates on
*every* method, method 0 included, so the only layout that can reach the writer is one
the validator has already accepted, and the writer runs only after the user confirms.
A refused `--persistent` reaches neither.

**One thing can still rot a file we caused to be written, and it is not a layout
error.** With Fractional Scaling off — GNOME 46's default — the session is in physical
layout mode, where a scaled monitor keeps its pixel width, and Mutter writes the file
with no `<layoutmode>` element at all. Turn the setting on and the same numbers are
read as logical pixels, where a `--scale 2` head is half as wide as the gap its
neighbour was saved at. Measured on 24.04, a three-head row with the scaled head first
and a two-head set saved beside it:

```console
$ gsettings set org.gnome.mutter experimental-features "['scale-monitor-framebuffer']"
$ # ... reboot ...
$ journalctl -b | grep 'monitors config'
Failed to read monitors config file '/home/test/.config/monitors.xml': Logical monitors not adjacent
```

Both entries gone. The layout was valid, Mutter validated it, Mutter wrote it; what
changed is what the numbers mean. GNOME Settings' own 200% scaling writes exactly the
same file, so this is not a wxrandr defect — but `--persistent` is the moment the user
chooses to save, and it is the moment to say so.

**What `wxrandr --persistent` therefore does** (`wxrandr/monitors_xml.py`, about 200
lines, none of it reached by a temporary apply):

1. reads the file before the apply and prints one line when Mutter has already
   discarded it — with Mutter's own verifier, in Mutter's order (adjacency first, so
   the sentence matches the one in the journal), judging an entry that names its layout
   mode in that one and an entry that does not in the session's;
2. warns, before the dialog, when the layout being saved is one that a later
   Fractional Scaling change would break — only in physical layout mode, only when
   something is scaled, and only when the row really does come apart in the other mode;
3. copies the previous bytes to `monitors.xml.wxrandr-backup` once Mutter has accepted
   the layout, so that a rewrite that drops the other monitor sets is recoverable. A
   refused apply copies nothing. GNOME keeps one generation of its own in
   `monitors.xml~` (glib writes it when Mutter replaces the file), but every save
   overwrites that one, including the save that does the damage.

It never writes `monitors.xml` itself. `tests/test_monitors_xml.py` holds the verifier
and the copy against real files from both releases, and
`tests/test_wxrandr_mutter.py:SavedConfigurationFile` holds the invariant end to end:
every refusal leaves the file byte-identical and writes no copy, an accepted persistent
apply writes the copy and still does not touch the file, and a temporary apply does not
open it — that last one enforced by making the reader explode if it is called.

### Why Mutter refuses monitors that share area

The one geometry the four backends do not agree on, and the long form the README and
the two contracts point at. Read and measured against **stock GNOME 46.0 and 50.1**,
three virtual heads, nothing patched.

**One validator, on the way in.** `meta_verify_logical_monitor_config_list()`, in
`src/backends/meta-monitor-config-utils.c`, walks the logical monitors a client
submits and requires each one to share an edge with another by *exact integer
equality*. Adjacency is tested before anything else, which is why one sentence,
`Logical monitors not adjacent`, comes back for a gap and for an overlap alike, and
why `Logical monitors overlap` needs a layout in which adjacency already holds. It is
not a permission check: gnome-control-center's Displays panel is a D-Bus client like
wxrandr and reads the same refusal.

**Nothing else in the compositor needs the invariant.** Read at both versions:

* monitor lookup by point returns the **first match**, not a unique one;
* lookup by rectangle **falls back to the primary** when nothing wins;
* pointer constraints clamp only when the pointer is in **no view at all**, not when
  it is in two;
* the screen size is a **bounding box**, computed the same way either way;
* the renderer already builds **several stage views over identical rectangles**,
  because that is exactly how mirroring is drawn.

**And Mutter already holds overlapping logical monitors in practice.** GNOME on Xorg
derives them from the X layout with no verification at all, so a plain `xrandr
--output B --pos 960x0` on a GNOME/X11 session puts an overlapping set inside the
very same data structures. The invariant is enforced at one door, not required by the
building, which is what makes this a limitation rather than a law of nature, and why
the identical layout is taken as drawn by KWin, by wlroots and by X.

**Every supported route in is closed, and one is worse than closed.**

| route | what happens |
|---|---|
| `ApplyMonitorsConfig` (D-Bus) | validates **before** it applies, on every method: 0 verify, 1 temporary, 2 persistent. `--dryrun` therefore gets exactly the answer an apply would |
| `~/.config/monitors.xml` | the parser calls the **same verifier**, and a failure discards the **entire file** — see the warning below |
| a GNOME Shell extension | **the one route that works**, and since 0.4 it is packaged, opt-in and off: it reaches the non-introspected libmutter symbols by shipping a type description of its own. Measured working on all **three** measured generations (GNOME 46, 50 and 51), shared region byte-identical. It also encodes a private struct offset and the library SONAME, and a wrong offset **writes into the compositor's heap** rather than raising an error, which on Wayland means the user loses the session. What makes that shippable is below |

**There is no Cinnamon analogue, and the route it would take is one rung lower.** The
GNOME extension exists because Mutter's `MetaMonitorsConfig` is not introspected and the
route reaches it from inside the shell; on Cinnamon `org.Cinnamon.Eval` already reaches
`MetaMonitorsConfig` with nothing installed, so the rung is 2 rather than 3 (AGENTS.md
lists Eval under rung 2, and the rule asks for the lowest rung that does the job). What
stops it being written today is the offsets: the GNOME route picks a private struct
description **by Meta typelib version**, and muffin's GIR namespace is `Meta-0` with
`libmuffin.so.0` for every release ever made, so the record would have to be measured per
*Cinnamon* release rather than per Meta generation. **Not yet**, with that route and that
cost, and `wxrandr` prints exactly that: *this is Cinnamon, whose Meta-0 typelib has no
generation to check; not yet here, and the route is org.Cinnamon.Eval reaching
MetaMonitorsConfig inside muffin with nothing installed (AGENTS.md route 2), at the cost of
an offset record measured per Cinnamon release instead of per Meta generation.* One
function produces it, so `--unsafe-gnome-overlap`, `--gnome-overlap-status` and
`--gnome-overlap-allow` all say it, which is the point of having one function behind all
three.

**And on GNOME-on-Xorg the flag has nothing to do at all**, because Mutter does not refuse
an overlap there: `wxrandr --backend mutter --output Virtual-3 --pos 1920x0` answered rc 0
with empty output and `xrandr --query` then showed Virtual-2 and Virtual-3 both at
`1920x1080+1920+0` (GNOME Shell 46.0, mutter 46.2, 2026-09-09). It really is the D-Bus
route that places it — `--print-backend --backend mutter --verbose` says `protocol:
org.gnome.Mutter.DisplayConfig (D-Bus)` on that session — so the `Logical monitors not
adjacent` string that IS in `libmutter-14` belongs to the path a Wayland Mutter takes.
`--gnome-overlap-status` already says the right thing there: `unavailable / shell: 46.0 /
reason: this session is x11, which places overlapping monitors without it`, which are the
handover branch's own words, on purpose, so that a user who asks the status and then types
`--unsafe-gnome-overlap` on the same box is told the same thing twice in the same sentence.

#### The extension, and the three properties that make it shippable

`gnome/w11-overlap@w11`, behind `wxrandr --unsafe-gnome-overlap`
([WXRANDR.md](WXRANDR.md#--unsafe-gnome-overlap-the-one-route-through)). It is a
**second, separate** extension: the bridge is feature-detected JavaScript over public
API across six Shell versions and stays that way, and this one ships a compiled
typelib pinned to one libmutter generation's private structure layout. Different kind
of thing, its own uuid, its own installer (`gnome/install-overlap.sh`) and its own enable
step. Since 0.4 the package carries its files, because a route nobody can reach from
the way almost everybody installs is not a route; what it does not carry is any step
that turns it on. Installed, it is inert: nothing enables it, nothing calls it, and
the flag that does is off. `--gnome-overlap-allow` is not a gate in front of it —
the agreement is read between the last refusal and the call, and all it decides is
whether the risk is printed in full or in one line.

`nm -D` on the shipped library lists `meta_monitor_manager_apply_monitors_config`,
`meta_monitor_manager_get_config_manager`, `meta_monitor_config_manager_get_current`,
`meta_monitor_config_manager_create_linear` and `meta_verify_monitors_config` as
ordinary dynamic symbols — libmutter is built with hidden visibility, but the
test-export macro expands to a real export. They are absent from the introspection
data, not from the library, and an extension can prepend its own search path.
`meta_monitors_config_copy` is exported on mutter 18 and **not** on mutter 14, so
there is no copy to mutate: the configuration the session is running is mutated in
place, and a refusal after the write puts the old bytes back before returning.

Three properties, and the extension is worth nothing without all three.

**1. It does nothing at login.** `enable()` exports one D-Bus object and stops: no
typelib load, no symbol touched, nothing read. It is the property the whole design
rests on, and what holds it and what was measured are in *Nothing at login, and
nothing on disk*, below.

**2. No pointer is ever dereferenced by the type system.** Every pointer in the
described structures is declared `guint64`, so reading one yields a *number*; each
step to the next struct is `g_memdup2(ptr, n)` — a bounded copy of exactly n bytes —
and `g_strndup(ptr, 63)` for the connector names, with the address range-checked
against `/proc/self/maps` first and list walks capped at 16. With pointers declared as
pointers, a wrong offset killed gnome-shell outright (measured, 50.1, a node whose
`next` was `0x1`); with them declared as numbers, all twelve wrong descriptions tried
across the three releases completed and none crashed.

**3. Every check runs before every write, never once at install**, because a
distribution upgrade can replace libmutter under a running session. The six of them,
in order and with what each one says when it fires, are two subsections below.

#### The bus interface, request by request

`org.w11.Overlap1` on `/org/w11/Overlap`, owned as
`org.w11.Overlap` on the session bus. One read-only property, `Version`
(currently 1), and two methods, each taking one JSON string and returning one. The
interface file is `org.w11.Overlap1.xml` in the extension directory, kept
beside the copy embedded in `extension.js` for reading rather than for loading.

Both methods take the same request object, and neither field is required:

* `layout_mode` — the number `GetCurrentState` reports publicly, 1 physical or 2
  logical. It decides nothing about the layout. It is checked against the value read at
  the offset this description believes, and a disagreement is a refusal, which pins the
  structure's tail a second time with a number the caller got from Mutter's public API:
  claiming logical mode on GNOME 46, whose default is physical, gets `layout_mode reads
  2 at the offset this description believes; DisplayConfig says 1`.
* `expect` — the layout the caller believes is running, one entry per logical monitor
  as `{"connectors": [...], "x": …, "y": …}`. On GNOME 46 it is also where the
  connector names in the public comparison come from, because that Shell exposes no
  way to name a monitor from JS at all and a synchronous DisplayConfig call from
  inside gnome-shell would deadlock against gnome-shell.

**`Probe(request) → result`** runs the six checks and reads the live configuration,
and writes nothing anywhere the session can see: the only write it makes at all is the
sentinel, into a throwaway `MetaMonitorsConfig` it builds for the purpose with
`meta_monitor_config_manager_create_linear()`. The reply is
`{"ok": true, "version": 1, "shell": "50.1", "libmutter": 18, "instance_size": 80,
"checks": [{"name", "ok", "detail"}, …], "monitors": [{"connectors", "x", "y", "w",
"h", "scale", "transform", "primary"}, …], "wrote": false}`. `instance_size` is the
number `GObject.type_query(MetaMonitorsConfig).instance_size` returned during the
struct-size check, and it is in the reply because the caller records an agreement
against it: what is agreed to has to be what was measured. `wxrandr --dryrun
--unsafe-gnome-overlap` prints exactly this method's `checks` and applies nothing, and
`sh gnome/install-overlap.sh --check` calls it with an empty request after printing
where the extension is installed and whether it is loaded.

**`ApplyOverlap(request) → result`** does all of that, then, with `want` in the request
in the same shape as `expect`:

1. **the request must describe this session.** `expect` must name exactly the logical
   monitors that were read, and each one must be at the position the request says it
   was, or `refused (request): Virtual-2 is at +1920+0, the request was built when it
   was at +960+0: re-read the layout and try again`. `want` must name the same
   monitors. Nothing here is addressed by pointer: a caller names connectors and
   positions, and every address comes from the extension's own bounded read.
2. **what is asked for must be a layout Mutter refuses.** `want` is run through this
   tree's reimplementation of Mutter's rule (`rules.js`, `mutterFault()`), and if
   Mutter would have *accepted* it the answer is `refused (not-an-overlap): Mutter
   accepts this layout: apply it the ordinary way, without this extension`. A layout
   GNOME will take goes down a path that validates, arms a revert timer and can be
   undone from a dialog, and there is no reason to write into a compositor's heap for
   it. This is also what stops the bus interface being a general display-configuration
   API for anything else on the bus.
3. **the digest**, `~/.config/monitors.xml` hashed with SHA-256 before anything moves.
4. **the write**: for each monitor that actually changes position, four bytes at the
   `MetaLogicalMonitorConfig`'s `x` and four at its `y`, little-endian, through
   `memcpy` declared to take its length from the array it is handed. The address is
   re-checked against `/proc/self/maps` immediately before the write even though the
   read that produced it already cleared it. A request that moves nothing is
   `refused (request): that is the layout this session already has`.
5. **the read-back**: the whole configuration is read again the same bounded way and
   must differ from what was there in exactly the requested `x` and `y`, with `w`, `h`,
   `scale`, `transform`, `primary` and the connector names unchanged.
6. **the positive control**: `meta_verify_monitors_config()` must **refuse** the
   result. If Mutter's own validator accepts what was built, the write did not land on
   the field that validator reads, and nothing is applied.
7. **the apply**: `meta_monitor_manager_apply_monitors_config(mm, cfg,
   METHOD_TEMPORARY)`. The constant is 1 and it is the only one in the file. 2 is
   `PERSISTENT`, which is what makes Mutter write `monitors.xml`, and it has to be
   unreachable rather than merely unused.
8. **the digest again**, and it is returned rather than assumed: `saved_config`
   carries the path and both digests and `unchanged`, and if it ever differs the reply
   is turned to `ok: false` on the way out.

A successful reply adds `wrote`, `applied`, `wrote_words` (two per moved monitor),
`fault` (the sentence Mutter's rule would have given), `verify` (what the validator
actually said), the re-read `monitors`, and `public`, which is Mutter's own answer
about the same monitors after the apply.

**Every refusal is a reply, not a D-Bus error**: `{"ok": false, "version": 1, "check":
"…", "reason": "…", "checks": […]}`, so a refusal reads the same wherever it came from
and the caller can print which check said no. A refusal *after* the write puts the old
words back, in reverse order, before it returns, and nothing has been applied on that
path either way. Anything that is not one of the named refusals comes back as
`check: "internal"` with the exception's message, and its stack goes to the journal.

The named checks a refusal can carry, all of them:

| refusal | when |
|---|---|
| `shell-version`, `libmutter`, `meta-typelib` | not a measured build, or its libmutter and its `Meta` typelib disagree with each other |
| `typelib`, `symbols`, `struct-size` | the shipped description is missing, will not load, has lost a symbol, or does not match this build's `MetaMonitorsConfig` size |
| `sentinel` | the tail of the structure is not where it was measured |
| `pending-dialog` | something holds a modal grab, so GNOME may be asking *Keep changes?* |
| `maps`, `current`, `bounded-read` | `/proc/self/maps` unreadable, no current configuration, or an address in the walk is not in a readable mapping |
| `layout-mode`, `public-view`, `connectors` | what was read privately does not agree with what Mutter says publicly |
| `request`, `not-an-overlap` | the caller's picture of the session is stale, names other monitors, changes nothing, or asks for a layout Mutter would have accepted |
| `write`, `read-back`, `positive-control`, `apply` | the address went away, the write did not land where it was aimed, Mutter's validator accepted the result, or the apply itself failed |
| `internal` | anything else, with the stack in the journal |

#### The private structure, and the descriptions that describe it

`MetaMonitorsConfig` is a `GObject` declared in `src/backends/meta-monitor-config-manager.h`
with no public accessors for the fields that matter. It is not in the introspection
data and it has no stable layout: the whole feature rests on knowing where two 32-bit
words are, per generation, and on proving that belief before every write. Measured on
stock GNOME 46.0 (libmutter 14) and 50.1 (libmutter 18), with `parent_config` and
`switch_config` proved by writing sentinels through the exported accessors:

| offset | libmutter 14 (GNOME 46) | libmutter 18 (GNOME 50) |
|---|---|---|
| 0 to 23 | `GObject` header (class pointer, ref count, qdata) | the same |
| 24 | `parent_config` | `parent_config` |
| 32 | `key` | `key` |
| 40 | `logical_monitor_configs` (a `GList *`) | the same, at the same offset |
| 48 | `disabled_monitor_specs` | `disabled_monitor_specs` |
| 56 | `flags` | unused |
| 60 | `layout_mode` | unused |
| 64 | `switch_config` | unused |
| 68 | padding | `layout_mode` |
| 72 | — | `switch_config` |
| 76 | — | padding |
| **size** | **72 bytes** | **80 bytes** |

The list at 40 is the one that is walked, and it is the same offset on both, which is
exactly the trap: a description written for one generation reads plausible monitors on
the other and gets the tail wrong. That is what the struct-size gate and the sentinel
are for, and it is why there is one description per generation rather than one with a
conditional in it.

The four records reached from there are the same on both generations. The sizes are
the byte counts the bounded reader copies:

| record | fields | size |
|---|---|---|
| `GList` node | `data` +0, `next` +8, `prev` +16 | 24 |
| `MetaLogicalMonitorConfig` | **`x` +0, `y` +4**, `width` +8, `height` +12, `monitor_configs` +16, `transform` +24, `scale` (float) +28, `is_primary` +32, `is_presentation` +36 | 40 |
| `MetaMonitorConfig` | `monitor_spec` +0, `mode_spec` +8, `enable_underscanning` +16 | 24 |
| `MetaMonitorSpec` | `connector` +0, `vendor` +8, `product` +16, `serial` +24 | 32 |

**`x` at +0 and `y` at +4 of a `MetaLogicalMonitorConfig` are the only bytes this
project ever writes into another program.** Eight per moved monitor, and nothing else
in the tree can write anything into gnome-shell at all.

**How the descriptions are made.** `gnome/overlap-typelib/gen-gir.py` reads the table
(*The table, and adding a GNOME generation*, below), emits one `.gir` per record and
compiles each with `g-ir-compiler`. Two properties of what it emits
matter more than the rest of the file:

* **no pointer is declared as a pointer.** Every pointer field above is `guint64`, so
  reading one yields a number that JavaScript can range-check rather than an address
  gjs will follow. The steps between structures are declared as functions: `dup_cfg`,
  `dup_node`, `dup_lmc`, `dup_mc` and `dup_ms` are all `g_memdup2` with a different
  return record each, `strn` is `g_strndup`, `addr` is `g_object_ref` returning a
  number, `type_name` is `g_type_name_from_instance` (offset 0 only, and a
  `GTypeInstance`'s first word is frozen ABI), and `wr` is `memcpy`;
* **nothing that can write `~/.config/monitors.xml` is declared.**
  `meta_monitor_config_manager_save_current()` is not in the file and neither is any
  other `*save*` symbol, so the writer is not callable from the extension even by
  mistake. `tests/test_gnome_overlap.py` asserts that against the `.gir` sources *and*
  against the compiled `.typelib` bytes, and asserts that no spelling of
  `METHOD_PERSISTENT` reaches the apply.

The compiled typelibs are checked in, because compiling one needs `g-ir-compiler` from
`libgirepository1.0-dev` and no desktop has that installed.

```sh
python3 gnome/overlap-typelib/gen-gir.py           # regenerate both .gir and .typelib
python3 gnome/overlap-typelib/gen-gir.py --check   # recompile and compare, changing nothing
```

`--check` is what notices a `.gir` edited without a rebuild, and the test suite runs
the same comparison over the checked-in bytes.

#### The table, and adding a GNOME generation

Everything that is specific to a GNOME release lives in one file,
`gnome/w11-overlap@w11/generations.json`, as one record per
generation:

```json
{ "shell_major": 50, "libmutter": "18", "soname": "libmutter-18.so.0",
  "meta_typelib": "18", "namespace": "W11Overlap18",
  "struct_size": 80, "tail_slots": 3,
  "measured_on": "Ubuntu 26.04, GNOME Shell 50.1, mutter 50.1" }

{ "shell_major": 51, "libmutter": "51", "soname": "libmutter-51.so.0",
  "meta_typelib": "51", "namespace": "W11Overlap51",
  "struct_size": 80, "tail_slots": 3,
  "measured_on": "Ubuntu 26.10, GNOME Shell 51.beta, mutter 51~beta" }
```

GNOME 51 is the first generation added by following the procedure below rather
than by writing it, and the two records above are why `struct_size` alone cannot
name a generation: 50 and 51 have the same private layout, so the two
descriptions differ only in what they are called. A forced run that picks by size
therefore picks the newest of the records that agree on `tail_slots` and says the
others describe the same bytes; records that *disagree* at one size are a refusal,
because the size cannot say which of them this build is.

**Every name is written out; none is computed.** That is the whole design change,
and mutter is why. Through GNOME 50, libmutter's API version was a counter of its
own — 46 carried `libmutter-14`, 50 carried `libmutter-18` — so
`libmutter-<major − 32>.so.0` happened to be right, and this tree derived a
soname, a typelib version and a `W11Overlap` namespace from one small integer in
four different places. mutter 51 sets `libmutter_api_version = '51'`
(`meson.build` line 10 of the `51~rc` tarball), so the next Ubuntu ships
`/usr/lib/x86_64-linux-gnu/libmutter-51.so.0` from `libmutter-51-0`, with
`Meta-51.typelib` from `gir1.2-mutter-51` beside it — checked against the archive,
not guessed. The arithmetic is gone, and a table that stores strings does not care:
a generation whose names follow no scheme at all is still one record.

There are two copies of the table and there have to be: the extension is installed
into `~/.local/share/gnome-shell/extensions` and `wxrandr` into a venv or a `.deb`,
and neither can read the other's files at run time. So `GENERATIONS` in
`wxrandr/gnome_overlap.py` carries the same records, and
`tests/test_overlap_force.py` compares them field for field and names the file to
fix. Everything else is *generated* from the table:
`gnome/overlap-typelib/gen-gir.py` writes the `.gir`, compiles the `.typelib`, and
writes `metadata.json`'s `shell-version` list, and `--check` fails if any of the
three has gone stale. `gnome/install-overlap.sh` reads the table too, for which
typelibs must be present and which shells to warn about. There is no fourth place.

**The procedure**, in order. Steps 1 and 2 are typing; step 3 is the work.

1. **Derive the numbers from the release's own source.** Not from the running
   compositor — see *Why the description is not generated from the running
   compositor* below.

   ```console
   $ apt source mutter        # or the .orig.tar.xz from the archive
   $ python3 gnome/overlap-typelib/gen-gir.py --from-header \
         mutter-52/src/backends/meta-monitor-config-manager.h --shell 52
       0  24  GObject parent
      24   8  MetaMonitorsConfig * parent_config
      32   8  MetaMonitorsConfigKey * key
      40   8  GList * logical_monitor_configs
      48   8  GList * disabled_monitor_specs
      56   8  GList * for_lease_monitor_specs
      64   4  MetaMonitorsConfigFlag flags
      68   4  MetaLogicalMonitorLayoutMode layout_mode
      72   4  MetaMonitorSwitchConfigType switch_config
     instance size 80
     the two numbers a record needs:  "struct_size": 80, "tail_slots": 3
     GNOME 52 is not in the table.  Adding it is one record in each of …
   ```

   (On a release already in the table the last line is
   `agrees with the record shipped for GNOME 51 (W11Overlap51, 80 bytes, 3 4-byte
   slots)` instead, which is how the shipped records are re-checked against upstream
   source.) It fails closed three ways: a type whose size it does not know is an
   error naming that type, a header that has moved anything in the head of the struct is
   refused outright (the description's *shape* is then wrong, not only its
   numbers), and a `--shell` it has no record for prints the record to add rather
   than inventing one. `--gen`, which used to take the libmutter generation, is now
   an error naming `--shell`: the table is keyed by GNOME major, and a script that
   takes one number when it means the other invites exactly the wrong answer.
2. **Write the record twice and regenerate.** The soname is the file
   `gnome-shell` actually maps — read it out of `/proc/$(pidof gnome-shell)/maps`,
   do not compose it — and `meta_typelib` is what `GIRepository` answers for `Meta`.

   ```console
   $ $EDITOR gnome/w11-overlap@w11/generations.json
   $ $EDITOR wxrandr/gnome_overlap.py          # GENERATIONS, the same record
   $ python3 gnome/overlap-typelib/gen-gir.py  # .gir, .typelib, metadata.json
   $ python3 gnome/overlap-typelib/gen-gir.py --check   # proves nothing is stale
   $ python3 -m unittest discover -s tests
   ```

   `metadata.json` is part of that generation and not a fourth place to edit:
   both its `shell-version` list and the sentence of its `description` that names
   the measured releases come out of the table, and `--check` fails when either
   has gone stale. `shell-version` is not cosmetic — `gnome-shell` refuses to
   *load* an extension that does not name the running Shell major, which is why,
   until a release is in the table, `install-overlap.sh` adds that major to the
   **installed** copy and says so. That is what makes the honest refusal (and
   `--unsafe-gnome-overlap-unmeasured`) reachable on a build nobody has measured;
   it changes nothing about what the extension will do once loaded.
3. **Confirm it on a live compositor before trusting it, and in this order.** The
   arithmetic agreeing with upstream source is necessary and not sufficient: two
   fields of the same size swapped upstream would pass step 1 and every static
   check here.

   1. `sh gnome/install-overlap.sh --check` — every guard against the running
      libmutter, writing nothing. All six have to pass. `struct-size` compares the
      new description's record size with `GObject.type_query()` on the live build,
      and `sentinel` writes `0x5f5a` through Mutter's own `set_switch_config` on a
      throwaway object and demands it back at the offset the description believes,
      which is what pins the *tail*.
   2. `wxrandr --dryrun --unsafe-gnome-overlap …` on a three-head VM: the same
      guards plus the bounded read and the field-by-field comparison against
      Mutter's public view, still writing nothing.
   3. Apply it, and check the pixels, not the log: crop both heads' screendumps to
      the shared region and compare the raw RGB. `vm/vmctl` builds the images, and
      the numbers the two shipped generations produced are in
      [WXRANDR.md](WXRANDR.md#--unsafe-gnome-overlap-the-one-route-through).
   4. Break it on purpose. Install a deliberately wrong description — the
      neighbouring generation's, or one with two same-size fields swapped — and
      confirm it is *refused by name* with `gnome-shell` still running. A guard
      nobody has watched fire on this release is a guard nobody has tested on it;
      that is how the first `pending-dialog` check shipped unable to fire at all.

      **Log in again between descriptions.** gjs *maps* a typelib into the
      process and keeps the mapping, so writing different bytes over a file a
      running `gnome-shell` has already loaded changes the blob under it and the
      next call through that description aborts the process — a dead session
      that says nothing about the description you were testing. Measured on
      GNOME 51, twice, before it was understood. The same hazard is a real one
      for users, not only for testing, so `install-overlap.sh` replaces a
      typelib by **rename** rather than in place: the running shell keeps the
      inode it already mapped and picks the new files up at the next login,
      which is when it was going to read them anyway.

   Only then does the record describe a supported build. Until then the honest
   state is the refusal, and `--unsafe-gnome-overlap-unmeasured` is what somebody
   who wants it anyway uses at their own risk.

#### What GNOME 51 measured, and what it changed

The procedure above was run once as written, by somebody starting from the refusal
message, against Ubuntu 26.10's `stonking-gnome` image (GNOME Shell 51.beta,
`libmutter-51.so.0` build `e13468162ed2`, mutter `51~beta-1ubuntu2`). What it produced:

* **the numbers.** `gen-gir.py --from-header` on mutter 51's own
  `meta-monitor-config-manager.h` lays `MetaMonitorsConfig` out at 80 bytes with three
  tail slots, `for_lease_monitor_specs` at 48 and `switch_config` at 72 — the same
  layout as mutter 18, under different names for everything around it. The live
  build agreed: `GObject.type_query()` reported 80, the sentinel round-tripped at the
  declared offset, the bounded read walked three logical monitors and the field by
  field comparison against Mutter's public view was identical.
* **the proof.** An overlap applied on three heads, and the shared 960 columns
  compared as raw RGB: byte identical between head 0's right and head 1's left
  (`sha256` equal, ImageMagick `AE` 0), against 1.0e6 differing pixels for the
  control crop, on a region whose standard deviation is 4951 so it is not a flat
  colour. `~/.config/monitors.xml` was never created.
* **the guards, made to fire.** A 72-byte description under the GNOME 51 name:
  `refused (struct-size)`, `gnome-shell` still running. `key` and
  `logical_monitor_configs` exchanged, which are the same size: `refused
  (bounded-read): node[1]: 0x1+24 is not in a readable mapping`, still running.
* **two things the written procedure did not say**, both of which cost a session
  before they were understood, and both now in it: a description must name no shared
  library, and a typelib must be replaced by rename rather than in place. They are
  the `shared-library` check and the `mv` in `install-overlap.sh`.
* **one thing the tool could not do at all.** Before this, no forced run could ever
  have worked on any new GNOME: `metadata.json`'s `shell-version` is generated from
  the table, `gnome-shell` will not load an extension that does not name the running
  major, so on an unmeasured build the bus name was never taken and forcing met a
  refusal it is not allowed to force. `install-overlap.sh` names the running major in
  the installed copy now.

`--unsafe-gnome-overlap-unmeasured` was then measured doing its job twice: on GNOME 51
before it was added to the table, and on a real GNOME 50.1 with the table's 80-byte
record keyed to a major that is not 50, which is the same situation from the other
side. Both applied, both proved on the pixels, neither recorded anything.

#### Forcing past the version gate

`--unsafe-gnome-overlap-unmeasured <major>`, added in 0.4 alongside the table
above, and the only thing in this repository that gets past a refusal.

**What it is.** A second flag, never a value of the first: `--unsafe-gnome-overlap`
stays the only option that turns the overlap route on at all, and typing the
unmeasured one alone is a usage error. Its argument is the GNOME Shell major of the
machine in front of you, compared with the running one out in `wxrandr` and again
inside `gnome-shell`. That argument is the design: a command line pasted out of a
forum names the poster's GNOME and is refused by number on anybody else's, which is
a property a bare `--force` cannot have. It is not a default anywhere, no
environment variable sets it, `warandr` has no way to reach it, and a forced run
neither reads nor writes the recorded agreement — so the paragraph it prints cannot
be silenced, and `--gnome-overlap-allow` refuses to be typed with it.

**What it skips: one check.** That this GNOME is in the table, and with it the tie
between the shell and the libmutter it is *supposed* to carry — on a build nobody
has measured there is no supposed to. The description to read through is then
chosen by the size the running build's own GType registry reports for
`MetaMonitorsConfig`, which is a selection and not a relaxation: the size still has
to be exactly a shipped description's, an ambiguous answer is a refusal, and a size
nothing describes is a refusal that says forcing cannot invent a description.

**What it cannot skip: everything else, and the rule is not a list of exceptions.**
A refusal here is either *cautious* — this is a build nobody has measured — or
*certain* — something is missing, or has just proved itself wrong. Only the first
kind is forceable, and there is exactly one of them. The certain ones, each for its
own reason: `--persistent` (the file it writes is read back through the validator
this exists to get past); a compositor that is not GNOME (KDE, wlroots and X place
the layout as drawn — there is nothing to buy); a shell that will not say its
version (forcing is somebody vouching for a build by naming it, and a version
nothing can read is not a build anybody can name); an extension that is not on the
bus (nothing there to talk to); an invocation that changes more than a position
(the extension writes two words and cannot do anything else); `symbols` (libmutter
has dropped an export — the code cannot run); `struct-size` (no description of this
struct exists); `sentinel`, `bounded-read`, `layout-mode`, `public-view`,
`read-back`, `positive-control` (the read or the write has just disagreed with
Mutter, which is the guard working); `shared-library` (the description names a
library that is not mapped, and calling through it would abort the process rather
than fail); and `pending-dialog`, which protects
`~/.config/monitors.xml` and is the one this must never touch.

The extension decides which of its own refusals is forceable and says so in the
reply (`forceable`); `wxrandr` reads the answer rather than parsing the wording, and
falls back to the same one-entry list only for an extension too old to say.
`tests/test_overlap_force.py` runs every certain refusal again with the flag typed
and demands it still refuses.

**What it prints before it does it**: what is skipped, what is not, that the two
words may land somewhere else on this build and take the session with them, that
nothing is recorded, and the way back from a session that will not start — the same
route as the ordinary warning, which is printed underneath it and not instead of
it.

#### Every check, in order

`Probe` and `ApplyOverlap` run the same five methods, which report six checks, in this
order and on every call. A check that passes appends a line to `checks`, which is what
`wxrandr --dryrun --unsafe-gnome-overlap` and `install-overlap.sh --check` print. Each
one names itself when it refuses, except that the second reports as `typelib` when it
passes and refuses under three different names depending on what was wrong.

| # | check | what it catches | what a failure looks like |
|---|---|---|---|
| 1 | `shell-version` | a build nobody has measured: the Shell major is a record in the table, exactly one libmutter is mapped into the process, its soname is the one that record names, and the `Meta` typelib version agrees with it. The one check `--unsafe-gnome-overlap-unmeasured` can be told to skip | `GNOME Shell 48.3: this extension knows the private layout of GNOME 46 and 50 and 51 only`, or `GNOME Shell 50.1 should carry libmutter-18, this process has [14]`, or `the Meta typelib says 14, libmutter says 18`. The tool refuses one release earlier still, from the Shell's public version property, before the bus is touched |
| 2 | `typelib` | the description is missing, will not load, has lost a symbol, or describes a structure of the wrong size. The size is read back out of *our own* typelib through `GIRepository` and compared with `GObject.type_query(MetaMonitorsConfig).instance_size`, so there is no constant in this tree to go stale | `W11Overlap18-1.0.typelib is not installed in …`, `W11Overlap18.create_linear is not callable`, or the one that matters: `this build's MetaMonitorsConfig is 72 bytes, the description shipped for libmutter-14 is 80` — **having read nothing at all** |
| 2b | `shared-library` | a description that names a shared library GIRepository cannot open. It is read statically out of the loaded namespace, before any call is made through it, because the failure it prevents is not catchable: gjs asserts and aborts the process on the first call through a namespace whose module did not load | `W11Overlap18 names the shared library libmutter-18.so.0, which is not mapped into gnome-shell ([libmutter-51.so.0] are)`. Descriptions generated here name none; this is for one left behind by an older install. Measured on GNOME 51, where the same input killed the session before this check existed |
| 3 | `sentinel` | the tail has moved even though the size has not. `0x5f5a` is written through Mutter's own exported `set_switch_config`, on a throwaway `create_linear()` object and never on the live one, and has to reappear at the offset this description believes | `switch_config reads 0 at the offset this description believes, not 24410: the tail of MetaMonitorsConfig is not where it was measured`. It pins offset 64 on mutter 14 and 72 on mutter 18, which is precisely what differs between them |
| 4 | `pending-dialog` | the one window in which a mutated configuration could reach Mutter's *writer*: confirming a *Keep changes?* makes Mutter save whatever is current, and a saved overlap poisons `monitors.xml` for ever. `Main.modalCount` must be exactly 0 | `something holds a modal grab on the shell (Main.modalCount is 1). If that is GNOME asking whether to keep a display change, …`. It fails closed: a count that is not a whole number, or is negative, refuses too. Measured refusing with the dialog on screen on both releases; measured **not** firing at all in the first cut of this check, which is its own subsection below; and measured refusing once with nothing on screen at all, seconds into a fresh session, which is under "Measured against a real update stream" |
| 5 | `bounded-read` | anything unreadable: the whole configuration is copied out with `g_memdup2` and `g_strndup`, every address checked against `/proc/self/maps` first, list walks capped at 16 monitors and connector names at 63 bytes | `node[1]: 0x1+24 is not in a readable mapping` — the wild pointer that killed a shell back when pointers were declared as pointers. `layout-mode` refuses here too, when the publicly reported layout mode is not what is read at the offset believed: `layout_mode reads 2 at the offset this description believes; DisplayConfig says 1` |
| 6 | `public-view` | everything else: count, `x`, `y`, `w`, `h`, `scale`, `primary` and the connector names against `global.display` and `MetaMonitorManager`. This is where a wrong offset that survived the size gate and the sentinel dies, because garbage does not agree with the public view on all of that at once | `private read has 1 monitors, Mutter reports 3; monitor 0: x reads -41753344, Mutter says 0 …` **and gnome-shell survived it**. The reply says which public source was used, because GNOME 46 can only supply the geometry half and takes the names from what the caller read out of DisplayConfig |

Then, on `ApplyOverlap` only, after the write and before the apply: the configuration
is re-read the same bounded way and must differ from the old one in exactly the
requested `x` and `y` (`read-back`), and `meta_verify_monitors_config()` must **refuse**
it (`positive-control`). That last one is why the tool prints `mutter's own validator
on the result: refused: Logical monitors not adjacent` and why that line is not
decoration: if Mutter's validator accepts what was built, the write did not land on the
field the validator reads, and nothing is applied.


#### The check that could not fire, and what it cost

Worth its own heading because it is the one thing here that went wrong, and because
the shape of the mistake outlives this feature. The first cut of `pending-dialog` read
`Main.wm._displayChangeDialog`. That property **does not exist** on GNOME Shell 46 or
50: `Object.getOwnPropertyNames(Main.wm)` matching `/dialog|display|confirm/` is empty
on both, with the dialog on screen and without it, because `windowManager.js` builds a
`DisplayChangeDialog` inside `_confirmDisplayChange()` and keeps no reference to it.
The guard therefore returned "nothing is open" every single time, and it did so while
printing a line saying it had checked.

What that cost, measured on **both** releases: with a valid `--persistent` armed and
*Keep these display settings?* on screen, an overlap applied through this extension and
the dialog then confirmed made Mutter save the **overlapping** layout into
`~/.config/monitors.xml`. At the next boot: `Failed to read monitors config file …
Logical monitors not adjacent` (50.1) / `… Logical monitors overlap` (46.0), the file
intact on disk and every arrangement in it inert for ever, with nothing said on any
screen. That is exactly the catastrophe the warning box below describes.

What it reads now is `Main.modalCount`, which is 0 with nothing modal and 1 while the
dialog is up (measured, 46.0 and 50.1), and the decision is `modalVerdict()` in
`rules.js`, where plain `node` can test it. It **fails closed**: a count that is not a
whole number, or is negative, is a refusal, so a shell that renames the field stops
the feature instead of silently disarming its most consequential guard. It is coarse
on purpose — any modal grab refuses, the overview and an open menu included — because
a dialog this shell will not name cannot be recognised any more precisely, and a
false refusal costs a retry while a false pass costs the file.

Re-measured with the guard alive, on both releases: the dialog on screen, the overlap
**refused** by name, and the confirmed dialog then saving the layout GNOME itself had
applied — a valid one, which the next boot read back with no journal complaint.

The general lesson, and the reason the tests are shaped the way they are: **a guard
that cannot fire is worse than no guard, because it is believed.** Every check here is
now either exercised by a test that fails when it stops firing
(`RulesJS::test_the_pending_dialog_verdict_can_actually_refuse`,
`ShippedExtension::test_the_pending_dialog_check_reads_a_field_that_exists`) or was
made to fire live against a deliberately broken description.

#### Nothing at login, and nothing on disk

Two deliberate absences, and they are the reason this is shippable at all rather than
a patch somebody keeps in a branch.

**It never acts at login.** `enable()` exports one D-Bus object, owns one bus name and
returns. It does not load a typelib, does not touch a libmutter symbol, does not read a
monitor and does not look at `/proc/self/maps`. The failure this prevents is the only
unrecoverable one available: a crash at session start, with the extension already
enabled, and no desktop to reach a setting from. Because nothing runs then, an enabled
extension that is never called is inert, which was measured over about ten logins on
GNOME 50.1 and five on GNOME 46.0, the only journal line either of them ever wrote
being `w11-overlap: enabled (idle; it acts only when called)`. The test that
keeps it that way reads the body of `enable()` and fails on any mention of a typelib,
`imports.gi`, libmutter, `get_config_manager` or the reader, so growing this method is
a failing suite rather than a bad login.

**It never persists a layout**, and four separate things hold that:

1. `--persistent` and `--unsafe-gnome-overlap` refuse each other in the tool, in either
   argument order, before a bus call is made;
2. the type description does not name `meta_monitor_config_manager_save_current` or any
   other writer, so the symbol is not callable from the extension at all, asserted
   against both the `.gir` sources and the shipped `.typelib` bytes;
3. the apply method is the constant `METHOD_TEMPORARY` (1), and `METHOD_PERSISTENT`
   appears nowhere in the extension under any spelling;
4. the file's SHA-256 is taken before and after every call and handed back in the reply,
   so the tool reports rather than assumes, and says so loudly if it ever differs.

That is not tidiness. `~/.config/monitors.xml` is read back through the very validator
this feature exists to get past, and one entry it refuses discards the **whole file**
at every boot, for ever, with the only trace a line in the journal. A persisted overlap
would not be a layout that came back, it would be every saved arrangement the user had
silently destroyed. So the layout goes at the next logout, and the way to have it again
is to run the command again. The undo is printed before the change, is built from the
layout that is running at that moment, and goes through DisplayConfig, so the way back
never depends on the dangerous half still working or on the extension still being
loaded.

One surprise worth writing down: an overlap **does** survive a monitor unplug and
replug inside the same session, because Mutter restores the layout from its own
in-memory store without validating it again. It still goes at logout.

#### What was measured, with the numbers

GNOME 50.1 (Ubuntu 26.04) and GNOME 46.0 (Ubuntu 24.04), stock images built by the
Ubuntu installer, three virtio heads, nothing patched.

* **The shared region is really shared.** Monitors at 0, 960 and 2880; both heads'
  screendumps cropped to the 960-pixel overlap and dumped as raw RGB have the **same
  SHA-256**, and ImageMagick's `AE` and `RMSE` between them are **0**, against a
  control of **507,079** differing pixels for the same head's own left and right
  halves.
* **A window inside the shared region is drawn byte-identically on both heads.**
* **The pointer crosses the whole bounding box with no jump, no dead zone and no
  duplication**, and a click in the shared region focuses the window drawn there.
* **A small monitor inside a large one works**, which is the case mirroring cannot
  express at all: a 1024x768 head at `+448+156` inside a 1920x1080 one is
  byte-identical to that sub-rectangle of its neighbour.
* **Mutter's own validator refuses the mutated configuration by name** on every apply,
  which is the positive control that the write landed on the field the validator reads.
* **`monitors.xml` never moves**: absent before and after, or present with an identical
  digest across the apply; the layout gone after a reboot with no `monitors config` line
  in the journal; and a later confirmed `--persistent` of a *valid* layout writing that
  valid layout and nothing else.
* **Five deliberate breakages were installed over the shipped extension**, across the
  two releases, and every one was refused by name: a 14-shaped description with
  mutter 18's tail (`struct-size`), the version table claiming libmutter 19 for GNOME
  50 (`libmutter`), `key` and `logical_monitor_configs` swapped, which are the same size
  (`bounded-read`, `node[1]: 0x1+24 is not in a readable mapping`), the list offset
  shifted by 8 (`public-view`, `private read has 0 monitors, Mutter reports 3`), and a
  same-size field swap that read garbage (`public-view`). **`gnome-shell` survived all
  five**: no crash, no core dump, the desktop still running afterwards. Four more went
  in during the update testing below, on both LTS releases and on other libmutter
  builds — a wrong-generation description on 24.04, and on 26.04 a 72-byte tail as
  `W11Overlap18` (`struct-size`), `layout_mode` and `switch_config` swapped at the same
  size (`sentinel`, `switch_config reads 1 at the offset this description believes, not
  24410`) and the list read out of the `key` slot (`bounded-read`). Three more went in
  with GNOME 51: a 72-byte description under the GNOME 51 name (`struct-size`), `key`
  and `logical_monitor_configs` exchanged (`bounded-read`), and a description naming a
  `libmutter` that is not mapped (`shared-library`, the guard that measuring GNOME 51
  produced, and the one input that had taken a session down before it existed).
  **Twelve in all, twelve refused by name, twelve sessions still running.**
* **The consent path was measured too**, on 50.1: first apply asks and the second says
  one line; the agreement survives a reboot while the layout does not; a recorded
  `50.0` against a live `50.1` brings the whole paragraph back; a recorded struct size
  of 96 against a measured 80 prints the quiet line *and* withdraws the agreement; a
  libmutter replaced under an unchanged Shell version withdraws it by build id, on both
  releases and on the update each release actually delivers; and,
  the decisive one, with an agreement in place and a deliberately wrong type
  description installed, the `struct-size` check refused, the exit status was 1 and
  nothing was applied.
* **The GUI half was driven on the real desktop** with `wdotool`: drag, Apply, dialog,
  box, *Apply anyway*, after which `wxrandr --query` reads `Virtual-2
  1920x1080+960+0`, the second head's screendump shows the shared 960 px and the
  consent file exists. A second overlapping Apply produced no dialog, and Cancel
  applied nothing.

#### Measured against a real update stream

The question this feature exists to fail is "what happens when GNOME moves", and
the answer stopped being a guess. Eight version pairs, on default desktops the
Ubuntu installer built, three virtio heads, the extension installed once and
never touched again: 24.04 at its ISO pair, at the newest `-updates` pair, at a
mid-life snapshot pair and with the **GA library under today's shell**; 26.04 at
GA, at its ISO pair, with the GA library under the newer shell, and with the
`-proposed` pair that nobody has received yet (gnome-shell 50.1-0ubuntu1.3,
libmutter 50.1-0ubuntu2.3).

**All eight applied, with all six checks green, and the structure did not move.**
On every one of the seven distinct libmutter builds behind them:
`GObject.type_query(MetaMonitorsConfig).instance_size` was 72 on 46 and 80 on 50
as declared; a sentinel written through Mutter's own `set_switch_config`
reappeared at the declared offset; the bounded private read matched
`global.display` field for field; and the 960-pixel shared region was
byte-identical on both heads (`AE` and `RMSE` 0, control ~1.007 M differing
pixels). `~/.config/monitors.xml` was never written on any of them, an
`apt upgrade` performed *while an overlap was on screen* left the session and the
layout alone on both releases, and nothing persisted across a reboot.

**The generation cannot move inside a release.** One `libmutter-N-0` per Ubuntu
release across release+updates+security+backports, every time: bionic 2, focal 6,
jammy 10, noble 14, plucky 16, questing 17, resolute 18, stonking 51 — mutter 51
renumbered the library to the GNOME major, so the counting stops there — and
`-backports` has never carried mutter or gnome-shell at all. The generation moves at a release
upgrade and nowhere else.

**Two independent confirmations of the offsets.** `gen-gir.py --from-header`
(below) lays out `struct _MetaMonitorsConfig` from each release's own header and
arrives at exactly the two shipped descriptions — 72/80 bytes, the list at 40,
`switch_config` at 64/72 — which is upstream source agreeing with a live
measurement. And `meta-monitor-config-manager.h` is byte-identical (sha256
`5f131fc1…`) between the mutter 46.0 and 46.2 tarballs, which is the change
24.04's shell version cannot see.

**What did go wrong, once.** On the first overlapping run after a post-update
login on 24.04, `pending-dialog` refused with nothing on screen — no dialog, no
menu, no overview, screenshot checked — and the next run seconds later applied.
It did not reproduce over a dozen probes on two ordinary reboots. Something in a
session that has just come up holds a modal grab briefly, and `Main.modalCount`
cannot distinguish that from the dialog that matters. **The guard was not
loosened**, because the costs are not symmetrical — a false refusal is a retry, a
false pass is `monitors.xml` for ever — and the message was fixed instead: it
names the count and says that a grab nobody can see releases itself.

**What the update stream did expose was a gap in the bookkeeping, not in the
guards.** The recorded agreement said it named "this build and no other", and it
did not: it named the Shell version string, libmutter's generation and the struct
size, and a routine `apt upgrade` of libmutter changes none of those. So a
`libmutter` swapped under an unchanged 46.0 or 50.1 carried the old agreement
over to a binary nobody had agreed to. The answer is a fourth recorded fact — the
GNU build id of the mapped library, read from its ELF note by the extension and
relayed like the rest — audited against the reply exactly where the other two
are. Measured on both releases: 46.2 → the GA 46.0 under one unchanged shell
version, and 50.1-0ubuntu2.2 → 2.3 from `-proposed`, each applying with every
check green and then printing `this GNOME is not the one that was agreed to
(libmutter build 286710f8eb3e, not 25d36850030c)` and deleting the record.

It is deliberately *not* a guard. The library the checks ran against is the one
being written to, so a different file on disk says nothing about the write. Which
is also why an `apt upgrade` under a live session — where `/proc/self/maps` gains
` (deleted)` and the running shell keeps the old library mapped — produces a
printed note rather than a refusal, and why the build id is reported as unknown
while that is true instead of being read from a file the answer is not about.

#### If a GNOME upgrade breaks this

For whoever finds `--unsafe-gnome-overlap` refusing after an update, in the order to
check it, and none of it needs a debugger:

1. **`sh gnome/install-overlap.sh --check`.** It runs every guard against the running
   libmutter and writes nothing. The refusal names the check, and the check names the
   cause. Everything below is that output read closely.
2. **`shell-version`** — a new GNOME. The allowlist is the table,
   `gnome/w11-overlap@w11/generations.json` and its twin
   `GENERATIONS` in `wxrandr/gnome_overlap.py`; `metadata.json` is generated from it
   and a test proves the two copies identical. The refusal prints what to add: the
   versions found, the `MetaMonitorsConfig` size this build reports, what is shipped
   to compare it against, the two files the record goes in and what to run
   afterwards. Adding a release is still not an edit to those lines, it is the
   measurement in *The table, and adding a GNOME generation* above. Somebody who
   knows their machine and wants it anyway has
   `--unsafe-gnome-overlap-unmeasured <major>`, which skips this check and no other. A recorded agreement (below) has already stopped
   applying at this point, on its own, because it names the version it was given on:
   the upgraded machine asks in full again rather than proceeding on an old yes.
3. **`struct-size`**, reported by the `typelib` check — `MetaMonitorsConfig`
   changed size. The message says both numbers.
   The fix is a new `.gir` for the new generation under `gnome/overlap-typelib/`,
   rebuilt with `python3 gnome/overlap-typelib/gen-gir.py`, whose `--check` proves the
   checked-in `.typelib` bytes match the `.gir` beside them. Nothing was read before
   this check refused. **Where the numbers come from:**

   ```console
   $ python3 gnome/overlap-typelib/gen-gir.py --from-header \
         mutter-50.1/src/backends/meta-monitor-config-manager.h --shell 50
       0  24  GObject parent
      24   8  MetaMonitorsConfig * parent_config
      32   8  MetaMonitorsConfigKey * key
      40   8  GList * logical_monitor_configs
      48   8  GList * disabled_monitor_specs
      56   8  GList * for_lease_monitor_specs
      64   4  MetaMonitorsConfigFlag flags
      68   4  MetaLogicalMonitorLayoutMode layout_mode
      72   4  MetaMonitorSwitchConfigType switch_config
     instance size 80
     the two numbers a record needs:  "struct_size": 80, "tail_slots": 3
     agrees with the record shipped for GNOME 50 (W11Overlap18, 80 bytes, 3 4-byte slots)
   ```

   It reads the release's own header, lays the struct out under the x86-64 rules, and
   **fails closed twice**: a type whose size it does not know is an error naming that
   type rather than a guess, and a header that has moved anything in the head of the
   struct is refused outright, because then the description's shape is wrong and not
   only its numbers. Both are exercised by `tests/test_gnome_overlap.py`, which also
   runs it against the mutter 46 and mutter 50 headers in `tests/fixtures/mutter/` and
   demands those two of the three shipped descriptions back — so the offsets this
   feature rests on now have upstream source and a live compositor agreeing about
   them.
4. **`sentinel`** — the size is right and the *tail* moved. This is the dangerous
   shape, the one that would write into somebody else's field, and it is why the
   sentinel goes through Mutter's own `set_switch_config` on a throwaway
   `create_linear()` object. Re-derive the offsets with `--from-header` above;
   `layout_mode` cross-checked against DisplayConfig's public value is the second
   opinion.
5. **`public-view`** — the read is nonsense. Do not fix it by adjusting offsets until
   the numbers agree, which is how a same-size field swap gets shipped. Fix it by
   deriving the layout from the release's own source, then proving it: apply an
   overlap on a three-head VM, crop both heads to the shared region, and compare the
   raw RGB. `vm/vmctl` builds the images; the measurements this feature rests on are
   in [WXRANDR.md](WXRANDR.md#--unsafe-gnome-overlap-the-one-route-through).
6. **`pending-dialog`** — `Main.modalCount` is gone or is not a whole number. Do not
   make this check pass. It fails closed for a reason, and the reason is the section
   above.
7. **`bounded-read`** on a build that passes everything above — either the addresses
   are not where this description says they are, or the list is being changed under
   the walk. Neither is fixable by retrying, and a refusal is the right answer to
   both, which is what it gives.

#### The risk that is left, and getting a session back

Stated plainly, because a reader has to be able to decide against this:

* **A wrong write is not a wrong answer, it is a dead compositor.** The checks turn
  nearly every wrong description into a refusal, and every one that was tried was
  refused, but *nearly* is the honest word. Twelve deliberate breakages caught is
  not a proof that a thirteenth would be.
* **The case the design cannot close by construction** is two fields of the same size
  swapped by an upstream change. The size gate passes, the sentinel may pass, and what
  is left is the bounded reader refusing an address that is not mapped, or the
  public-view comparison noticing that the numbers are nonsense. Both were measured
  catching exactly that, on two different swaps, and both are checks rather than
  certainties.
* **The allowlist is a claim about three builds this project measured.** A distribution
  that backports a Mutter change without moving the Shell's major version can make the
  version gate say yes to a library nobody has seen. What stands behind it then is the
  structure size, the sentinel and the public-view comparison, in that order. This is
  the live one and it is not hypothetical: the version gate said yes to seven different
  libmutter builds during the update testing above, which is what it is for, and every
  one of them happened to have the same private layout. The mechanism that would beat
  it exists — mutter 50.1-0ubuntu2.3, in `resolute-proposed`, adds a field to
  `_MetaMonitorManagerPrivate` under an unchanged 50.1 Shell version — and it happens
  not to be aimed at this struct.
* **The measurement has a shelf life.** All of it was true of the archive on the day it
  was run, on x86-64, on Ubuntu. A check that has been right twelve times is a check
  that has been right twelve times.
* **If `gnome-shell` dies, everything in the session dies with it**: not the layout, the
  browser, the editor, the unsaved buffer, the terminal it was typed in. On Wayland the
  compositor is the session and there is no restarting it in place.

**Getting a session back.** It should not come to this, because the extension does
nothing at login and a layout that was never saved cannot come back to bite anybody.
The route is printed in the warning the tool gives before every apply, and it is this,
in order:

1. **Log in again.** A `gnome-shell` that dies drops you at the login screen. Nothing
   was saved, so what comes back is the layout you started with. Measured aside, twice:
   after a `gnome-shell` killed outright, GNOME came back having set
   `org.gnome.shell disable-user-extensions` to `true` by itself, so the next session
   had this extension, and every other, inert.
2. **If no session will start**, Ctrl+Alt+F3 to a text console, log in there, and
   `gnome-extensions disable w11-overlap@w11`, then Ctrl+Alt+F1 back to
   the login screen. This works from a real text login, which has `XDG_RUNTIME_DIR`
   set. It does **not** always work from a bare shell with no session bus: it prints
   `dconf-WARNING … failed to commit` and exits **0** having changed nothing, which is a
   `gnome-extensions` behaviour and not something this project can fix.
3. **The route that always works** is deleting the directory:
   `rm -rf ~/.local/share/gnome-shell/extensions/w11-overlap@w11`. It
   is printed in the warning for exactly that reason, and
   `sh gnome/install-overlap.sh --uninstall` is the same thing with the disable step in
   front of it.
4. **Withdrawing the agreement** is `wxrandr --gnome-overlap-forget`, which builds no
   session and opens no socket, or `rm ~/.config/w11/overlap-consent.json`,
   which is the same thing and is why the record is a plain file.

None of the four needs a graphical session, and the two that matter most have a form
that is a plain `rm` of a directory or a file, so neither needs this tree, a working
bus or a shell that will start.

#### The agreement, and why it cannot skip any of the above

The paragraph `--unsafe-gnome-overlap` prints is right the first time and noise the
fiftieth, so since 0.4 it can be agreed to once, per user, in
`$XDG_CONFIG_HOME/w11/overlap-consent.json`
([WXRANDR.md § Agreeing once](WXRANDR.md#agreeing-once-and-withdrawing)). The design
rule is one sentence: **the agreement decides what is printed and nothing else.**

* it is recorded only by `--gnome-overlap-allow`, which runs the six checks first and
  writes nothing if any of them refuses, so an agreement can only ever name a build
  they have just passed on;
* what it names is what they *measured* — the Shell version string, libmutter's
  generation, `GObject.type_query(MetaMonitorsConfig).instance_size` as the `typelib`
  check read it, and the GNU build id of the mapped libmutter as the extension read it
  out of that file's ELF note — relayed to the caller as `instance_size` and
  `libmutter_build` in the extension's answer. No number in this tree, which is the
  failure that check exists to catch. The build id is there because the other three
  cannot see an `apt upgrade`: 24.04 has carried mutter 46.2 under GNOME Shell 46.0 for
  most of its life, and swapping that library was measured leaving the version string,
  the generation and the size all unchanged;
* it is read in `MutterOutputs.apply_overlap()` *after* the last thing that can refuse
  and *before* the call, and the only things the value it produces reaches are
  `warn()`, `warn_bare()` and `applied_text(quiet=…)`.
  `tests/test_overlap_consent.py` asserts that from the source, and re-runs every
  refusal in `tests/test_gnome_overlap.py` with an agreement recorded for exactly the
  build in the room. Measured live on 50.1 as well: with an agreement in place and a
  deliberately wrong type description installed, the `struct-size` check refused and
  nothing was applied;
* the version string is compared before anything is read, because that comparison has
  to decide whether the paragraph is printed *before* the write. The other three facts
  are audited against the reply afterwards, and a difference deletes the record so the
  next run asks in full — bookkeeping rather than a guard, because the extension's own
  `typelib` check makes the layout cases a refusal, and a new build of the same layout
  is not dangerous at all. Measured on both releases, on the two updates that carry a
  new libmutter under an unchanged shell version: the apply goes through with six green
  checks, and then the record is deleted by name.

`warandr` never imports any of this: it runs `wxrandr --gnome-overlap-status` and
reads the token on the first line, which is why that command reads a public property
and a bus name and nothing out of `gnome-shell`.

Nothing about this is a supported interface, so "it broke on the new GNOME" is the
expected outcome of an upgrade, not a surprise. The feature is designed to *refuse*
there and say why, and to keep refusing until somebody repeats the measurement. What
the update testing above adds to that sentence is that "an upgrade" means a *release*
upgrade: inside 24.04 and 26.04, eight libmutter builds later, it has not broken once.

#### Why the description is not generated from the running compositor

The obvious next step — have the extension build its own description at install time,
by asking the libmutter it is about to use — is the one thing here that must not be
done, and it is worth writing down why, because it looks like self-repair.

**It would spend the independence the checks are made of.** `struct-size` compares two
things that were arrived at separately: a number in a description written by a human
from upstream source, and `GObject.type_query()` on the running build. Derive the first
from the second and the check is `x == x`. `sentinel` is worse: the only way to derive
a tail offset from a live process is to write a marker and search for where it landed,
and a search that stops at the first match in an 80-byte struct is a check that
confirms whatever it finds. What is left is the public-view comparison, alone.

**And the interior is not derivable anyway.** The GType registry hands out an instance
size and nothing else about a private struct: which words are pointers, which are ints,
where a `GList` head is. Those are exactly the facts a wrong description gets wrong, and
exactly the facts a running compositor will not tell you. A generator would have to
guess them — on the build it is about to write into.

**The evidence does not transfer, either.** Twelve wrong descriptions were caught,
but every one of them was caught *by disagreeing with the build*. A self-derived
description cannot disagree with the build, so "the guards caught twelve" says
nothing about it.

The supportable half of the idea is real and is what `--from-header` is: derive the
numbers mechanically, from the release's own source, at packaging time, where a human
still has to write the record into the table and prove it on a three-head VM before
anything ships. That keeps the arithmetic honest without letting the compositor mark
its own work. Carrying descriptions for more generations is likewise a matter of
measuring them, not of writing more of them: the table takes any number of records,
everything else is generated from it, and the one copy that cannot be generated --
`GENERATIONS` in `wxrandr/gnome_overlap.py`, because wxrandr and the extension are
installed in different places -- is held to it by a test that names the file to fix.

> **The one warning worth its own line: never hand-edit `monitors.xml` to force an
> overlap.** Mutter discards the whole file on any error, so one bad entry silently
> destroys every other monitor arrangement the user had saved, on every boot, and the
> only trace is a line in the system journal. Nothing warns at the time: the session
> comes up with a layout Mutter has built from scratch, and every arrangement that
> user had saved is gone.

**The safety fact behind that warning: Mutter's own writer does not validate.**
`meta_monitor_config_manager_save_current()` will happily write an overlapping layout
into the file that the reader then rejects in full, for ever. Reader and writer
disagree, and the disagreement is silent and permanent, which is the real reason
nothing in this tree writes that file itself: `--persistent` asks *gnome-shell* for
its "Keep changes?" dialog and lets Mutter write, and that is the only route we take
([WXRANDR.md](WXRANDR.md#mutter-backend-wxrandrmutterpy)).

**So what the tools say instead.** wxrandr keeps passing the layout on unchanged and
attributing the refusal to Mutter by name; nothing here pretends to a workaround. The
substitute the documents offer is a **mirrored region**, and it is never called an
overlap: GNOME will not place two monitors so that they share area, and the closest
thing available is a region whose pixels match exactly, where the copy takes the
clicks that land on it rather than passing them to the window they came from, and
where a copy made by capture rather than by the layout lives only as long as that
capture session, which a screen lock ends. Whole-monitor mirroring is in the layout
on GNOME (`--same-as`: one logical monitor, several members, identical mode, rotation
and scale); a region is `wmirror` on wlroots ([WMIRROR.md](WMIRROR.md)), and on GNOME
and KDE only through the desktop portal, which prompts once per session and is
therefore useless from a hotkey.

## 7. Detached children, runtime paths and stdio

**One detach protocol.** `w11common/procs.py` is the whole of it, and both callers use
it: `wxrandr/gamma.py`'s holder, which keeps a `zwlr_gamma_control` alive for as long
as a brightness is set, and `wmirror/supervise.py`, which owns one `wl-mirror`.

    proc_starttime  zombie  owned_by_us  alive  wait_gone  kill_bounded  emit
    spawn_detached(child_main, deadline, on_line)

What it promises, and why each promise has code:

* **double-fork plus `setsid`**, so nothing is left in our process group and the
  child survives the shell, and the terminal, that started it;
* the child writes `pid <pid> <starttime>` up a status pipe **before it can fail**,
  and the parent acts on that line the moment it arrives. `on_line` fires **as lines
  arrive**, not when the start finishes, because a start that hangs or that the user
  interrupts halfway through must still leave a record naming a process something
  later can stop. An orphan nobody can end is the one outcome this protocol exists to
  prevent, and buffering the lines would produce it. There is a test that interrupts
  a start with a KeyboardInterrupt and then finds and stops the child.
* **liveness is `(pid, starttime)` read out of `/proc`**, so a recycled pid is never
  mistaken for the process we started, and the euid check means nothing is signalled
  that is not ours;
* **every kill is bounded** — SIGTERM, wait, SIGKILL, confirm — and never
  fire-and-forget.

`daemon._spawn` deliberately does *not* use it: the input daemon has its own
readiness handshake, its own cgroup escape and its own re-exec, and folding those in
would make one function serve two contracts.

**One runtime directory.** `session.runtime_dir()` returns `$XDG_RUNTIME_DIR` when
there is one, else the verified-0700 `/tmp/wdotool-<uid>`. Four things live there and
all four went through it in 0.3: the daemon socket, `backend_kwin`'s script lock,
`wxrandr`'s state file and `wmirror`'s state file (the last two degrading to today's
`/tmp` name on a `CmdError`). The consequence is the point: under `sudo` or from
cron, those files are private to their owner rather than world-readable in `/tmp`.
The directory *name* is pinned by `tests/test_hardening.py`.

**One stdio rule, for all six tools.** `w11common/stdio.py:flush_stdout(prog)` is the
last thing every `main()` does: flush, **close**, and one `prog: message` line on
stderr if the flush failed. Output that never reached its reader makes the exit
status 1, whatever the command itself decided. A reader that closed a pipe is silent,
because the originals die of SIGPIPE without a word. `repair_std()` at the top of
each `main()` is the other half: an fd 1 or 2 closed before the interpreter started
(`>&-`) leaves `sys.stdout` as `None`, and the work still gets done. The result is
that no tool here prints a traceback and none exits 120, which is the interpreter's
own "the exit-time flush of stdout failed". `stdio.warn()` is the same trick for the
other stream: every line a `main()` writes as its last word goes through it, because
`tool >/dev/full 2>&1` is a real case (a cron job whose log filled the disk) and the
diagnostic about the lost output cannot land there either. It swallows the failure and
**closes** stderr, since unwritten bytes left in *that* buffer are flushed again on the
way out and make the status 120 exactly as stdout's do.

## 8. The environment

Twenty-five rows below, and rather more names than rows, because some of them group.
All but seven are also written down in the README, in a tool contract or in
`gnome/README.md`; the seven in **bold** are written down only here. Five of the rows and
six of the names are the proxy's, and [XW11.md](XW11.md#variables-and-options) is where
they are explained.

| variable | read by | effect |
|---|---|---|
| `W11_PASSTHROUGH` | all six | `never` runs our own code whatever the session, `always` hands over whatever the session. `WDOTOOL_PASSTHROUGH`, `WWMCTL_PASSTHROUGH`, `WXPROP_PASSTHROUGH` and `WXRANDR_PASSTHROUGH` do the same per tool |
| `WDOTOOL_REAL_XDOTOOL` and friends | `passthrough` | where the original is. Also `WWMCTL_REAL_WMCTRL`, `WXPROP_REAL_XPROP`, `WXRANDR_REAL_XRANDR`. Set but unusable is an error naming the variable, never a silent fallback |
| `WDOTOOL_LAYOUT`, `WDOTOOL_XKB_KEYMAP` | the daemon | the character table, a keymap from a file |
| `WDOTOOL_XKB_GROUP` | the **command**, carried to the daemon on the request | the layout group to pin. Read where the user set it, like `--layout`, because the daemon keeps the environment it was spawned with and outlives it |
| `WDOTOOL_VKBD` | the daemon | `auto` / `on` / `off`, for both injection halves |
| `WDOTOOL_SYNC_TIMEOUT`, `WDOTOOL_REL_MODE`, `WDOTOOL_SELECT_TIMEOUT` | `wdotool` | the `--sync` deadline (`0` waits for ever), `abs`/`rel` for relative pointer moves, and how long KWin's window picker waits (2 minutes by default; GNOME's picker is the bridge's own, capped at 30 s inside the extension, and reads no variable) |
| `WDOTOOL_BACKEND` | `backend_detect` | force a window backend, ahead of detection |
| `WXRANDR_BACKEND`, `WXRANDR_PERSIST` | `wxrandr` | force a display backend (`--backend` beats it); make `--persistent` the default |
| `WWMCTL_WMCTRL_GENERATION` | `wwmctl` | `1.07` or `git`: which upstream `--help` text to print, instead of consulting the installed oracle |
| **`WWMCTL_NO_X`** | `wwmctl.core` | do not open the X plane at all. The listing then carries compositor ids and no X enrichment — how the "no X server" path is exercised without taking one away |
| **`WXPROP_NO_X`** | `wxprop.core` | the same for wxprop: never resolve an X plane, so `-root` answers from the compositor's synthesized set |
| **`WXPROP_ARGV0`** | `wxprop` | the program name in usage and error lines, overriding `argv[0]`. Real xprop prints the name it was invoked under, and `python -m wxprop` has none to print |
| **`WDOTOOL_UINPUT_PATH`** | `wdotool.uinput` | the device node to open, default `/dev/uinput` |
| **`WDOTOOL_FAKE_UINPUT=1`** | `wdotool.uinput` | skip the ioctls, so a regular file can stand in for the device. This is what lets the daemon's event stream be asserted byte for byte in a container that has no `/dev/uinput` |
| `WDOTOOL_DAEMON_IDLE`, `WDOTOOL_DAEMON_CHECK` | the daemon | seconds with no client before it exits (900), and how often it looks (15, and the same tick checks that its socket is still there). `0` is never; `CHECK=0` turns both checks off |
| `WDOTOOL_NO_KEYSTATE=1` | the daemon | force the `keystate.py` path (the foreign-modifier diagnostic) even where the `/dev/input` read would be skipped |
| `WDOTOOL_GNOME_AUTOLOAD=1` | `backend_gnome` | opt in to one `org.gnome.Shell.Eval` that tries to load the installed extension. Eval is a privileged interface and this is off by default |
| **`XPROPFORMATS`** | `wxprop.cli` | a format file, exactly as real xprop's `-fs` and `$XPROPFORMATS` do |
| **`DEBUG`** | `wdotool` | set to anything: print the traceback instead of the one-line error. Every `main()` catches broadly, which is right for users and wrong for whoever is debugging |
| `W11_PROXY` | the four wrapped `main()`s, through `xw11.wrap` | `auto` (the default), `never`, `always`: whether a **Wayland** session runs the ORIGINAL against the proxy. `never` is our own code; `always` is the original or exit 127. `WDOTOOL_PROXY`, `WWMCTL_PROXY`, `WXPROP_PROXY` and `WXRANDR_PROXY` do the same per tool, and each beats it. The spellings are `W11_PASSTHROUGH`'s, and `W11_PASSTHROUGH=never` already means "no proxy either" |
| `XW11_DISPLAY` | `xw11` | the display number to take, `:N`. Taken or nothing: there is no scan, and a proxy given one takes no session lock, which is what lets a second one run beside the session's |
| `XW11_LOG` | `xw11` | where the log goes; default `/tmp/xw11-<uid>.log`, opened `O_NOFOLLOW` and refused when it is not ours. The proxy's log is where a refused write's diagnostic goes, because the wire has no error for one |
| `XW11_DEBUG=1` | `xw11`, and `xw11.wrap` | one line per request and per packet down; and, in the wrapper, the one line that says why the clone ran instead of the original |
| `W11_PARITY_PROXY`, `W11_PARITY_PROXY_DISPLAY` | `scripts/parity-oracle.sh` | run the whole oracle through a pass-through proxy on `:98`, which is the framing's regression test |
| `WDOTOOL_BACKEND`, `WXRANDR_BACKEND` | also `xw11` | the proxy keeps both across its spawn, because a user who forces a backend for `wxrandr` means it for `xrandr` through the proxy too |

`WARANDR_TEST_*`, `FAKE_XRANDR_*`, `FAKE_REAL_*`, `W11_SHIM_SEAMS`, `WD_TEST_*` and
`SLOW_QUERY` are test seams and are documented where they are used, in
[WARANDR.md § Test hooks](WARANDR.md#test-hooks-env) and in the test files
themselves. They are not part of the interface.

## 9. Module → test file → fake

5174 tests, run as `python3 -m unittest discover -s tests` or file by file. Two rules
hold across all of them and are enforced by tests of their own:

* **every `tests/test_*.py` sets `W11_PASSTHROUGH=never`**, or the suite
  would `execve` itself away on an X11 box and the parity oracle would compare the
  real xdotool with itself and pass tautologically. `tests/test_passthrough.py` fails
  when a test file is missing the line. Its `SuiteGuard` holds two more rules that keep
  the three documented ways of running a file honest: the `if __name__ == "__main__"` block
  is the last statement in the file, because a class written after it never runs under
  `python3 tests/<file>.py`; and a file importing `support`, `wl_fake`, `test_dbus_mini` or
  `test_wxrandr_mutter` by its bare name also carries
  `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))`, without which
  `python3 -m unittest tests/<file>.py` dies on the import.
* **no test leaves a daemon running.** One that spawns a real daemon stops it
  through `support.stop_daemons_under()`, registered before the spawn so it runs
  however the test ends and before the runtime directory goes away;
  `tests/test_zz_daemon_leak.py` runs last and fails the suite over anything still
  alive that was not there when it started — the input daemons, the headless compositors,
  and this euid's processes holding a `wxrandr-gamma` memfd. The suite is how the rig came
  to have 161 of them.
* **shared helpers live in `tests/support.py`**, deliberately not named `test_*.py`
  so the escape-hatch guard above skips it. It holds `RecorderDev` and `abs_report`
  (the plain 3-tuple shape every uinput assertion uses), `env()`, one merged
  `FakeEvdev`, and `HeadlessSway` for the XWayland live files. It also holds what the second
  round of tests needed twice over: `documents()` (the markdown walk three files carried),
  `sh_function()`/`sh_block()` (an installer's own branch, run as itself), `fake_gnome_bin()`
  (POSIX-sh stand-ins for the GNOME command line, over one state file, so
  `packaging/common/enable-bridge` can be run without touching the runner's dconf), `js_harness()`
  (node 22 with `tests/fixtures/gjs/loader.mjs`, which maps `gi://` and
  `resource:///org/gnome/shell/` onto recording doubles so the two shipped `extension.js`
  files execute for the first time), `FakeSway` (one sway IPC socket in six failure modes,
  shared by wdotool and wxrandr), `leader_process()` (a real process with a session leader's
  name and environ) and `WL_MIRROR_STUB`.
  `tests/wl_fake.py` is the other one: the Wayland marshallers and a `Server` base,
  deliberately **not** built on `wayland_mini`, so a bug in the client cannot hide
  itself in the fake.

`scripts/parity-oracle.sh` is not one of those tests; it is what makes two of them mean
something outside the development shell. It puts the pinned oracles on PATH (the flake's
xdotool 4.20260303.1 and wmctrl 1.07 out of /nix/store, or `$W11_ORACLE_PATH`), starts an Xvfb
if there is no usable DISPLAY, and runs `tests/test_cli_parity.py` and `tests/test_wwmctl_cli.py`
against them — once against the nix wmctrl 1.07 and once against the distro's 1.07+git20240228.
It exits non-zero when either oracle is missing **and** when either unittest run is red (every
run is captured, not piped: this is `/bin/sh` with no `pipefail`), so the skip `test_cli_parity`
takes on a foreign xdotool cannot pass for a run. On Ubuntu 26.04, whose apt xdotool is
3.20160805.1, that file is a printed SKIP — which is what the script exists to turn back into a
real comparison.

| what it covers | test files | what stands in for the world |
|---|---|---|
| `cli.py`, `commands.py`, `misc_cmds.py` | `test_cli_chain`, `test_cli_misc`, `test_cli_script`, `test_cli_parity` | the real `xdotool` binary as the byte oracle (skipped outside `nix develop`) |
| `window_cmds.py`, `desktop_cmds.py` | `test_windows_cmds`, `test_windows_sway` | `FakeBackend` in-memory; a real headless sway |
| `input_cmds.py`, `daemon.py`, `uinput.py` | `test_input_cmds`, `test_input_daemon`, `test_input_uinput`, `test_daemon_lifetime`, `test_torture_regressions`, `test_hardening` | `FakeDaemon`, `RecorderDev`, and `WDOTOOL_FAKE_UINPUT=1` writing into a regular file |
| `vkbd.py`, `vptr.py` | `test_vkbd`, `test_vptr`, `test_input_protocols_sway` | `wl_fake.Server`: a real unix socket speaking the Wayland wire format; then a real headless sway 1.11, which is the only thing that can say wlroots *accepts* those bytes — every group of requests there ends in the round trip that turns a protocol error into an exception, and one deliberately bad opcode per protocol proves the round trip really reports them |
| `xkbmap.py`, `keymap.py`, `us_keymap.py` | `test_xkbmap`, `test_keymap`, `test_layout_flag` | `tests/fixtures/keymaps/*.xkb`, each a byte-for-byte capture of what a compositor handed a client, from GNOME, sway and KWin |
| `layoutbox.py` | `test_scale_spaces` | a wl_output/xdg_output fake replaying one measured scaling state per test, and `FakeMutter` on the mock bus as the second source |
| `keys_cmds.py` | `test_keys_cmds` | recorded evdev streams |
| `backend_gnome.py` | `test_backend_gnome`, `test_wwmctl_gnome`, `test_wxprop_gnome` | `MockBridge` on `dbus_mini`'s in-process mock bus — and, since `tests/test_bridge_js.py`, no longer taken on trust: the extension's `METHODS` table, `org.w11.Bridge1.xml` and `MockBridge`'s own `m_<Name>` methods are checked against each other statically (names and out signatures), and ten methods are compared answer for answer with the shipped `extension.js` running under node |
| `gnome/w11-bridge@w11/extension.js` (the shipped file, executed) | `test_bridge_js` | node 22 through `tests/fixtures/gjs/loader.mjs` (`support.js_harness`), which resolves every `gi://` namespace and every `resource:///org/gnome/shell/` import onto the recording doubles under `tests/fixtures/gjs/stubs/`; the world each case builds — `global.display`, `global.workspace_manager`, the window actors — is `test_backend_gnome.fixture_windows()` rows turned back into `Meta.Window` doubles, and the answers are compared against `MockBridge`'s for the same state |
| `backend_kwin.py`, `kwin_js.py` | `test_backend_kwin` | a fake KWin on the same mock bus, answering `loadScript`/`run`/`unloadScript` |
| `backend_sway.py` | `test_windows_sway`, `test_wire_hardening` | real sway; `FakeSway` for the hostile cases |
| `backend_wlr.py` | `test_backend_wlr`, `test_backend_wlr_workspaces`, `test_backend_wlr_rig`, `test_labwc_live`, `test_river_live` | a wire-level foreign-toplevel fake, an `ext_workspace_manager_v1` fake beside it, and real labwc and river sessions where a golden exists |
| `backend_hypr.py`, `hypr_ipc.py` | `test_backend_hypr` | `support.FakeHypr`: a real unix socket answering Hyprland's one-request-per-connection protocol, with `tests/fixtures/hypr/` as the recorded bytes |
| `backend_wayfire.py` | `test_backend_wayfire`, `test_wayfire_live` | `support.WayfireDouble` over the int32-length + JSON framing; then a real headless `wayfire 0.10.0` (`support.HeadlessWayfire`), skipped where the binary is absent |
| `backend_cinnamon.py`, `cinnamon_js.py` | `test_backend_cinnamon` | `MockCinnamon`, which parses the JS it is sent; the scripts themselves are asserted as text, because only integers are ever interpolated into one |
| `backend_cosmic.py` | `test_backend_cosmic` | a wire-level fake speaking `ext_foreign_toplevel_list_v1`, `zcosmic_toplevel_info_v1` and `zcosmic_toplevel_manager_v1`, with the capability array as a parameter |
| `xid_match.py` | `test_backend_kwin`'s `TheMatcherMoved` | the same fixtures the KWin matcher was written against, now shared by every backend whose compositor publishes no X ids |
| `ext_workspace.py` | `test_backend_wlr_workspaces` | recorded COSMIC and labwc/Budgie workspace event streams — the first with `coordinates`, the second without |
| `w11common/distro.py` | `test_distro`, `test_packaging_names` | `/etc/os-release` bodies for Debian, Fedora, Arch and NixOS through the `OS_RELEASE` and `NIXOS_MARKER` seams |
| `x11_mini.py` | `test_wwmctl_x11`, `test_wxprop_x11`, `test_wwmctl_hardening` | `FakeXServer`, and `HostileXServer` subclassing it |
| `w11common/session.py` | `test_session`, `test_session_discovery` | a temporary `/run/user` tree |
| `w11common/passthrough.py` | `test_passthrough`, `test_passthrough_exec` | a hermetic detection matrix; then a fake install tree with real processes |
| `w11common/dbus_mini.py` | `test_dbus_mini` | byte-exact fixtures plus an in-process `MockBus`; `RealBus` against a real `dbus-daemon`, started by the suite itself with `dbus-run-session` when `DBUS_SESSION_BUS_ADDRESS` is unset |
| `w11common/wayland_mini.py` | exercised by every wire test above | `wl_fake` |
| `wwmctl/` | `test_wwmctl_cli`, `test_wwmctl_live`, `test_wwmctl_hardening`, `test_wwmctl_gnome`, `test_wwmctl_kwin` | `FakeSwayBackend`, `FakeX11`; real sway with XWayland for the live file; the fake KWin of `test_backend_kwin` on the mock bus, with `_FakeX` as the Xwayland client list, for the Plasma file |
| `wxprop/` | `test_wxprop_cli`, `test_wxprop_fmt`, `test_wxprop_live`, `test_wxprop_gnome`, `test_wxprop_x11`, `test_wxprop_kwin` | captured real-xprop bytes; a live XWayland server as the oracle; the same fake KWin, whose resident event script the test plays for `-spy`; `MockBus` (an empty session bus) in `test_wxprop_cli`, so the real `backend_detect.detect()` can be driven to its no-session error |
| `wxrandr/hypr.py` | `test_wxrandr_hypr` | `support.FakeHypr` again, and `TheTwoClients`, which drives `wdotool/hypr_ipc.py` and `wxrandr/hypr.py`'s own copy of the reader against one double and insists on the same bytes and the same sentences |
| `wxrandr/mutter.py` in its MUFFIN flavour | `test_wxrandr_cinnamon` | `FakeMutter` on a `MutterMockBus(flavor=MUFFIN)` — the same fake, three names swapped |
| `wxrandr/` | `test_wxrandr_unit`, `test_wxrandr_backend`, `test_wxrandr_mutter`, `test_wxrandr_kwin`, `test_wxrandr_live`, `test_wxrandr_hostile`, `test_wxrandr_gamma`, `test_wxrandr_wlr_apply`, `test_monitors_xml` | `FakeMutter` on the mock bus; a wire-level fake KWin; real sway with real `xrandr` through XWayland as the oracle; real `monitors.xml` files from both default installs; `FakeMutter`'s `emit_signal`/`swallow_apply`/`hangup_on_apply` and `KwinOutputServer.swallow_apply` for a compositor that half-answers |
| `wxrandr/core.py`'s `SwayIPC` (the display half of the sway wire client) | `test_wxrandr_sway_wire` | `support.FakeSway` in its six modes — answering, gone mid-chain, badly framed JSON, wedged, refusing an `output` command in sway's words, and rows with no `rect` — driven through `cli.main --backend sway`, so what is asserted is the exit status and the one line the user gets |
| `wxrandr/kwin.py` on a **real KWin** (Plasma 6.6, KWin 6.6.6): backend choice, the protocol and version `--print-backend --verbose` names, `--query` against `kscreen-doctor -o`, one `--right-of` apply and the restore line it prints, `--same-as` as a `replicationSource`, and F4.1's live twin (two same-title Xwayland xterms moved by X id) | `test_wxrandr_kwin_live` | nothing is faked: the QEMU rig (`vm/vmctl`, golden `resolute-kde`) with `kscreen-doctor` as the oracle. Opt-in twice — `VMCTL_LIVE=1 WXRANDR_LIVE_KWIN=1` — and skipped unless the named instance is already running, because this host runs one VM at a time |
| `wxrandr/gnome_overlap.py` + `gnome/w11-overlap@w11/` | `test_gnome_overlap`, `test_overlap_consent`, `test_overlap_force` | the same mock bus with a mock `org.gnome.Shell` and a mock overlap extension on it, so a whole `--unsafe-gnome-overlap` run happens in-process; plain `node` running the extension's own `rules.js` against `monitors_xml.py`; and, for the shipped type descriptions, `g-ir-compiler` plus GIRepository in a subprocess per namespace — the shipped typelib and a fresh compile of the checked-in `.gir` are compared by *meaning* (namespace, no shared library, every function name and C symbol, every record's size and field offsets), because g-ir-compiler 1.86 writes 17 different reserved words per typelib than the compiler that produced the checked-in files. The consent file re-runs every refusal in the first with an agreement recorded, and asserts from the source that the agreement is read after the last one |
| `wxrandr/gnome_overlap.py` + `gnome/w11-overlap@w11/` against a compositor that is really running | `test_gnome_overlap_live` | a private headless sway (`support.HeadlessSway`) as the negative — the flag has to be a refusal off GNOME before any bus call — and, gated on `WXRANDR_LIVE_GNOME=1` *and* an `org.gnome.Shell` that owns its name, a real gnome-shell on the rig: status, the agreement against `readelf -n` of the mapped libmutter, the apply and its printed undo, the forced-`--dryrun` refusal, and whether the moved-monitors.xml branch can be reached at all |
| `warandr/` | `test_warandr_model`, `test_warandr_parse`, `test_warandr_gui`, `test_overlap_consent` | `tests/fixtures/fake_xrandr.py`, a RandR simulator (which also simulates a GNOME with the overlap extension and its agreement); Xvfb plus xdotool driving the real editor, dialog included; the fake's `FAKE_XRANDR_OVERLAP_WITHDRAW_ON_APPLY` replays wxrandr withdrawing an agreement under the running window, with wxrandr's own `consent_drift()` sentence |
| `wmirror/` | `test_wmirror_cli`, `test_wmirror_lifetime`, `test_wmirror_live` | a fake `wl-mirror` (`support.WL_MIRROR_STUB`), and the detach protocol driven for real; then a real headless sway (`swaymsg create_output` for the second head) with the same stub, where the supervisor's watch reads real zwlr_output_management events for the only time in the suite |
| `xw11/wire.py`, `xw11/client.py` | `test_xw11_wire`, `test_xw11_client`, `test_xw11_read`, `test_xw11_write` | the layouts rendered out of `/usr/share/xcb/*.xml` and pinned as fixtures, so a locally built packet is compared byte for byte and never field by field; `support.ProxyRig`'s `raw()`, a bare socket with the setup done, for pipelined bytes |
| `xw11/display.py`, `xw11/upstream.py` | `test_xw11_display`, `test_xw11_upstream` | `support.FakeUpstream`, a real AF_UNIX X server with a programmable `QueryExtension` table, per-connection resource-id bases stepping by `0x200000`, `hold_reply`/`release` to reorder deliberately and `close_when_idle` for Xwayland's `-terminate` shape; a real `/tmp/.X11-unix` for the socket-and-corpse rules, because a spawned child cannot be monkeypatched |
| `xw11/server.py`, `xw11/policy.py`, `xw11/req_read.py`, `xw11/req_write.py` | `test_xw11_server`, `test_xw11_subst`, `test_xw11_read`, `test_xw11_write` | `support.ProxyRig` — the real `Server` on a thread in a temporary socket directory — against `FakeUpstream` and `support.FakeBackend`, whose `wedge(seconds)` is how the 10 s stall bound is measured without a compositor that hangs |
| `xw11/shadow.py`, `xw11/ewmh.py` | `test_xw11_shadow`, `test_xw11_ewmh` | `FakeBackend` records every write as `(op, args)` and mutates its own list, so a routed `ClientMessage` is asserted as the backend call it became rather than as the bytes that went nowhere |
| `xw11/events.py`, `xw11/pump.py` | `test_xw11_events`, `test_xw11_pump` | `support.FakeBackend` for the packets themselves, and `support.FakeBackendEvents`, which feeds `events()` from a queue, for the pump; `has_events=False` there drives the 250 ms poll path the wlr floor and COSMIC take |
| `xw11/xtest.py` | `test_xw11_xtest` | `support.FakeDaemon`: the input daemon's line protocol on a temporary socket, every operation recorded, the pointer answering as configured, warnings settable, and a mode that refuses the connection outright |
| `xw11/randr.py` | `test_xw11_randr` | `support.RandrTables` replaying a captured `GetScreenResources`/`GetOutputInfo`/`GetCrtcInfo` set, and `support.FakeRandrBackend` recording what one apply was handed |
| `xw11/cli.py`, `xw11/wrap.py` | `test_xw11_wrap` | real forks and real `execve`s into `tests/fixtures/w11_shim.py` under an original's name, and `/proc/<pid>/cmdline` read back — which is the only way to say what argv a daemonised child was re-exec'd with |
| the whole proxy against a real X server | `test_xw11_live`, `test_xw11_replay`, `test_xw11_parity` | nothing is faked: `support.HeadlessSway` with Xwayland, one `xterm` and one `foot`, the pinned `xdotool 4.20260303.1` and `wmctrl 1.07` on PATH. `test_xw11_replay` puts eleven captured command streams through it byte for byte; `test_xw11_parity` runs `scripts/parity-oracle.sh` twice, direct and with `W11_PARITY_PROXY=1`, and diffs the two with the timing lines normalised |
| `procs.py`, `stdio.py` | `test_wmirror_lifetime`, `test_stdout_gone` | real forks; `>/dev/full`, `\| head -1`, `>&-` |
| the no-dialog guarantee | `test_no_portal` | nothing — it is a static check that no package here names PolicyKit or any portal interface but `Settings`, the one read with no consent step |
| what actually ships | `test_release_deb` | nothing — it unpacks the .deb committed in `release/` (with `unpack_deb()`, an `ar` + `compression.zstd` reader in the standard library, proved byte-identical to `dpkg-deb -x` wherever dpkg is installed) and compares its payload with the tree: every module, every non-Python file, both maintainer scripts, the typelib per generation in `generations.json`, the autostart symlink, and one version across `w11common`, pyproject, `debian/changelog`, `flake.nix` and the file name. It caught the v0.3 build still committed while 0.4 was being finished |
| `packaging/common/enable-bridge`, `gnome/install-bridge.sh`, `gnome/install-overlap.sh` | `test_install_scripts` | `support.fake_gnome_bin()`: POSIX-sh `gsettings`, `gnome-extensions`, `gdbus`, `sudo`, `runuser`, `id`, `getent` and `dpkg` over one state file, on a PATH of their own, in a temporary HOME. The shipped scripts are run whole where they can be and sliced function by function (`support.sh_function`/`sh_block`) where they cannot, so nothing here is a copy of what ships |
| `debian/w11.postinst`, `debian/w11.postrm` | `test_debian_scripts` | `DPKG_ROOT` pointing into a scratch tree, sh stubs for `modprobe`/`udevadm`/`setfacl`/`chown`/`chmod` that record and do nothing, the `os.pipe()`/`os.close(r)` broken-reader double from `test_stdout_gone`; then real `dpkg -i`/`-r`/`-P` into that tree with `--force-script-chrootless` and no-op `py3compile`/`py3clean` |
| `scripts/build-pyz.sh`, `scripts/build-deb.sh` | `test_build_scripts` | a temporary copy of the tree (never `dist/` or `release/`); `zipfile` reading the six zipapps; `w11common.passthrough.is_us()` run over what was built; sh stubs for `sudo`/`apt-get`/`dpkg-query`/`dpkg-buildpackage` that record and are asserted never to be reached |
| `vm/live-smoke.sh`, `vm/live-smoke.d/` | `test_live_smoke` | `oracle.py`'s branches over recorded bytes under `tests/fixtures/live/`, the step files' shape and `xwant` convention, the driver's per-distro package axis, and `selftest-offline.sh`'s two-pass rule |
| `flake.nix`, `nix/` | `test_flake`, `test_nixos_golden` | the flake read as text and evaluated where `nix` is on the box; `vm/build-nixos-golden.sh` sliced the way the other rig scripts are |
| `packaging/rpm/`, `packaging/arch/` | `test_rpm_spec`, `test_rpm_scripts`, `test_pkgbuild`, `test_packaging_names`, `test_release_rpm`, `test_release_pkgbuild` | the spec and the recipe as text against `debian/w11.install`, `debian/rules`, `pyproject.toml` and `generations.json` through one path map; the scriptlets sliced with `support.sh_block` and run under `sh -e` against a fake root; and, in CI, the built packages themselves through `rpm2cpio` and `bsdtar` |
| `vm/*.sh` | `test_vm_scripts` | `bash -n`; the stage-2 pipeline of `build-iso-golden.sh` sliced with a stand-in `ssh` that fails; `selftest.sh`'s own embedded kscreen parser run over `tests/fixtures/kscreen/` (Plasma 5.27 one-line and 6.x block captures, each with a head that is not there). Anything needing the rig is `skipUnless(VMCTL_LIVE)` |
| `scripts/check-docs.py` | `test_check_docs` | the script loaded as a module (`spec_from_file_location`) with `options_in_help`, `SILENT` and `ROOT` patched: a planted option nobody documents and a planted typo that is a prefix of a real option are the two blind spots it used to miss, and the unpatched tree is the positive control |
| the documented numbers | `test_docs_numbers` | `unittest.defaultTestLoader.discover` in a subprocess for the test count; `wdotool.daemon`'s two constants; `_pass('…')` in the overlap extension for the check count; `wdotool.commands.REGISTRY` for the command count; the six tools run for README's "Check it worked" block |
| cross-document links, and the rig's 38 flavor images | `test_docs_matrix` | GitHub's slug rules reimplemented and resolved against every heading; `vm/flavors/*.yaml` and `vm/vmctl`'s own `DESKTOPS` table as the fact behind vm/README's table and README's support matrix |
| `tests/support.py`, `tests/fixtures/gjs/` | `test_support_helpers` | nothing — the shared doubles tested as themselves: the markdown walker against the one `scripts/check-docs.py` carries, the sliced installer functions handed to `sh -n`, the fake GNOME command line (`gsettings`, `gnome-extensions`, `gdbus`, `dpkg`, `sudo`, `id`) over one state file, the sway IPC double driven by wxrandr's own `SwayIPC`, and node 22 running both shipped `extension.js` files through `tests/fixtures/gjs/loader.mjs` |

Two environments run these. **In the development shell** (`nix develop`), a container
with no `/dev/uinput`, `WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 sway` gives a
real Wayland compositor for the backend and protocol tests, with `swaymsg`, `foot`,
`grim`, XWayland and the real `xdotool`, `wmctrl`, `xprop` and `xrandr` alongside it
as the byte oracles. **On the rig** (`vm/vmctl`), the same tools run against real
desktops with real input, which is where `WLR_BACKENDS=headless,libinput` matters:
libinput has to be listed or sway does not pick up the uinput devices at all.

`tests/test_backend_gnome.py::ShippedFilesTests` pins the bridge's `shell-version` list against
the rig: every GNOME major a flavor in vm/README.md carries, and every generation the overlap
extension is measured on, has to be in it. "51" went in after the measured run on
`stonking-gnome` (GNOME Shell 51.beta), so the bridge and the overlap extension name the same
three releases.

**`vm/live-smoke.sh <flavor>`** is the other runner that is not a unittest, and the only one
that needs a booted image. It replays the hand smoke of 2026-09-08 per flavor: boot, install
(the `.deb`, or the working tree over it), bridge, windows, the maximize pair, input and live
layout switching, display, `--persistent` and the bridge's `ConfirmDisplayChange`, the overlap
route, `enable-bridge` under GDM, the udev rule, a root phase, the no-dialog bus recording and
`apt-get remove`. Each step prints `PASS`/`FAIL <what>`, the exit status is the number of FAILs,
and results plus a screenshot of every head per phase land in `vm/live-smoke.out/`. The steps
live in `vm/live-smoke.d/<desktop>.sh` — one per `DESKTOPS` token: gnome, kde, sway, xfce,
kde-x11, hypr, wayfire, labwc, xfce-wayland, budgie, lxqt-wayland, cosmic, river, cinnamon,
cinnamon-wayland, mate, i3, lxqt and gnome-x11 — over `common.sh`, with `oracle.py` answering
as the desktop's own display tool. Three of them (`xfce-wayland.sh`, `budgie.sh`,
`lxqt-wayland.sh`) source `labwc.sh` whole and add their desktop's own difference, because
those three desktops *are* labwc; six X11 ones (`mate.sh`, `i3.sh`, `lxqt.sh`,
`gnome-x11.sh`, `cinnamon.sh` and `kde-x11.sh`) source `xfce.sh`'s handover body for the
same reason — on a plain X11 session every tool hands over, whichever desktop drew it.

**The smoke has a package axis per distribution.** `vm/live-smoke.sh --pkg` (with `--deb`
kept as its alias) installs the flavor's own distribution package:
`release/w11_<ver>_all.deb` on Ubuntu, `$LIVE_SMOKE_RPMS/*-<ver>-*.rpm` (default
`dist/`, pinned to the version because `build-rpm.sh` never clears that directory) on Fedora,
`$LIVE_SMOKE_PKG` (default `dist/w11-<ver>-1-any.pkg.tar.zst`) on Arch, and on NixOS
nothing at all — the package is in the image, so `phase install` asserts instead that
`wdotool` resolves to a `/nix/store` path carrying `w11common.VERSION` and that
`/run/current-system` is the default specialisation. `--remove` is the mirror:
`apt-get remove` / `dnf remove` / `pacman -R` / a `switch-to-configuration test` into
`without-w11`, and on NixOS the switch BACK goes through the store path read before
the removal, because activation repoints `/run/current-system` at the specialisation it just
activated. Two phases belong to a distribution rather than to a desktop and the driver
appends them itself: `selinux` (Fedora — `getenforce` is `Enforcing` and no AVC names
`wdotool`, `python3` or `udevadm`) and `pkgverify` (Fedora `rpm -V`, Arch `pacman -Qkk`;
both must print nothing). `pkgverify` says so and stops without `--pkg`, because a tree
deploy writes the repo's extension over files the package owns and the verifier would report
a difference the smoke itself made. The "every path the package owned is gone" list has one
entry per packaging and one line that differs, the helper directory: `/usr/lib/w11`
under dpkg and pacman, `/usr/libexec/w11` under rpm,
`/run/current-system/sw/share/gnome-shell/extensions/<uuid>` on NixOS. The `/dev/uinput`
half — `root:root 0600`, no ACL entry, no `uaccess` tag — is identical everywhere and is the
point of the phase. The first-install banner is **not** universal either: dpkg's postinst and
pacman's `.install` print the same paragraph and the rpm prints none at all (Fedora
discourages chatty scriptlets; the spec ships `README.Fedora` in `%doc` instead), so
`phase_install` checks for the banner where there is one and for `README.Fedora` where there
is not.

`vm/live-smoke.d/oracle.py` answers for every `DESKTOPS` token: `hyprctl -j monitors`,
`wlr-randr` (labwc, xfce-wayland, budgie, lxqt-wayland, river), Wayfire's own
`window-rules/list-outputs` over its int32-LE + JSON IPC with `wlr-randr` as the fallback,
`cosmic-randr list --kdl`, Muffin's `GetCurrentState` under
`org.cinnamon.Muffin.DisplayConfig`, and `xrandr --query` for the X11 desktops. The two ways
of having no answer both exit 2 and name what is missing — an unknown desktop token, and a
desktop tool that is not installed on the guest — because an empty answer would otherwise
read as "no enabled output" and pass every display check by default.

The smoke gained a **`proxy` phase** with the X11 proxy, appended after `wm` in every
Wayland step file's `SMOKE_PHASES` and in no X11 one. It is the only place in the tree
where the ORIGINAL `xdotool`, `wmctrl`, `xprop` and `xrandr` are driven through `xw11`
against a real compositor: every golden installs the four as test-support packages, so
the phase points them at the proxy's display and compares their answers with the ones the
clones gave in `windows` and `wm`, ids tokenised. The X11 flavors' `passthrough` phase
carries the mirror claim, read off the pid file rather than a process pattern: no
`$XDG_RUNTIME_DIR/xw11/display` exists after the four tools have run, because on an X11
session the handover happens one line above the wrapper's call. First run on
**resolute-sway (sway 1.11, Xwayland 24.1.10) on 2026-09-11: 27 pass, 0 fail**, recorded
as `tests/fixtures/live/resolute-sway-1.11-windows-wm-proxy-replay.txt`;
`tests/fixtures/live/NOT-YET-RUN` carries a `proxy:<flavor>` line for each of the other
28 Wayland flavors until their own run.

That script has a regression of its own that needs no VM. `vm/live-smoke.d/selftest-offline.sh`
runs the `windows` and `wm` phases against `vm/live-smoke.d/fake-vmctl`, which replays
`tests/fixtures/live/noble-gnome-46.0-windows-wm-replay.txt` (recorded off GNOME 46.0), and
asserts that 17 assertions pass on the recording and that exactly one — b7a60f0's — fails when the
pre-fix geometry is put back; two further passes do the same for the `busrec` phase (T65's
session-bus recorder) against two hand-written transcripts, where it has to pass when a
`dbus-monitor` process exists and the log grows, and fail when neither is true. Two seconds in
all. `tests/fixtures/live/` holds the recordings, and its README says what each one is and which
claim rests on it.

## 10. The VM rig

`vm/` is where every "it works on GNOME" sentence in this repo comes from.
`vm/vmctl` builds and runs **38 flavors** over four distributions — 25 Ubuntu, 6 Arch,
5 Fedora, 2 NixOS — and there are three builders, not one: 34 are a *cloud* image plus a
desktop metapackage, two — `resolute-gnome-iso` and `noble-gnome-iso` — are installed from
`ubuntu-26.04.1-desktop-amd64.iso` and `ubuntu-24.04.4-desktop-amd64.iso` **by the Ubuntu
installer itself**, unattended, with every question left alone, and two are NixOS
configurations built with `nix build` out of `vm/nixos/`. CI builds all 38 on every push and 0
on demand. The cloud-image ones exist because one script gets 19 desktops out of them.
The two ISO ones exist because "it works out of the box on a default Ubuntu desktop" is a
claim about an *installed* system, and a cloud image plus `ubuntu-desktop` measurably is
not one: 226 packages a real 26.04 desktop install does not have, 55 it has and the cloud
image has not, a different kernel with no firmware at all, 8 snaps against the default 13.
Which flavor is which, and what each one's oracle is, is the table in
[vm/README.md](../vm/README.md); the yaml headers are the fact and this file does not
repeat them.

**The NixOS build counts against the one-VM rule.** `vm/build-nixos-golden.sh` runs its own
QEMU inside the nix sandbox (nixpkgs' `make-disk-image`), so it refuses to start beside a
running rig instance, and it needs `kvm` in `nix config show system-features` and refuses
without it. The image was 4.9 GiB and about ten minutes on 4 vCPU from a cold store.

Each image autologins user `test` on a multi-head virtio-vga whose monitors can be
plugged, unplugged and resized from the host at run time, with host-side screenshots
of every head. That is what makes a multi-monitor claim testable at all.

**`vm/selftest.sh` proves the rig, not the tools.** It asserts that the flavor came
up the way the flavor says it should: the right session type (logind's own `Type`
and the session's `XDG_SESSION_TYPE`, which are two different answers), autologin
landed, the heads are there, the compositor is painting,
`vmctl user` reconstructs an environment in which the desktop's own tools work. It is
the check you run after building an image and before believing anything measured on
it. The tools' own behaviour per flavor is the separate table in
[vm/README.md](../vm/README.md), *What the six tools do on each flavor*, and that is what
the README's support matrix is a summary of.

[vm/SETUP.md](../vm/SETUP.md) is how to stand the rig up on a machine of your own, and
`vm/setup-host.sh` does the mechanical part of it.

### The no-dialog measurement

The README's [no authorization dialog](../README.md#no-authorization-dialog) is a
claim about six of these images: GNOME 46 and GNOME 50 (the 26.04 default install off
the ISO among them) and Plasma 5.27, 6.6 and 6.7, with every command run three ways,
as root, as a plain user with the udev rule, and as a plain user with neither.
Watching throughout: the session bus for portal traffic, the system bus for polkit
`CheckAuthorization` and `BeginAuthentication`, the window list for windows we did not
open, and both screens compared pixel by pixel around every command. On all six, and
for the installer and the udev rule as well as the tools: **no prompt and no window we
did not open.** The same rig pointed at a real portal client and at `pkexec` produced
both dialogs on every image, so it does see one when there is one.
`tests/test_no_portal.py` is the static half of the same guarantee, and it runs
everywhere.

**One portal call is ours, and the measurement covers it too.** On GNOME `wdotool`
reads `org.gnome.desktop.input-sources` through
`org.freedesktop.portal.Settings.ReadAll`, to learn which keyboard layout is active
rather than assume it and type the wrong characters (docs/WDOTOOL.md § Keyboard
layouts). `Settings` is the interface every GTK and Qt application calls at start-up
for the colour scheme: read-only, answered with no permission check, and with no
entry in the portal's permission store to allow or deny. Re-measured on GNOME 46.0
and 50.1 with the same watchers on: the call answers in 1–3 ms, no window appears,
and no `RemoteDesktop`, `InputCapture` or `impl.portal` traffic follows it. The
static half is exempted at exactly that width — one interface, by name — and
`test_no_portal.py` has its own test for the width of the hole.

### The whole of it on every push

`.github/workflows/ci.yml` runs all of the above on GitHub, spread as wide as the runner
pool allows: one job per test file per Ubuntu release (24.04, 26.04 and 26.10, each in a
container built from `.github/ci/Dockerfile` and cached in GHCR by that file's hash, as an
unprivileged user, with a real sway, GTK, Xvfb, node and g-ir-compiler present so nothing
skips), the parity oracle under nix (`scripts/parity-oracle.sh`), the package built from the
tree and `apt install`ed on each release, and one job per flavor of the rig: KVM is on the
runner, the golden image comes from a GHCR cache keyed by the recipe (`scripts/ci-golden.sh`,
which builds it there when the key is new, ISO installs included, and pushes it for the
next run; bump `vm/golden-epoch` to force a rebuild), and `vm/live-smoke.sh --deb --remove`
runs against it with its log and screenshots kept as the job's artifact. The 26.10 jobs may
fail without failing the run; that release moves under the flavor.

## 11. Installing: what each route costs

The [README](../README.md#install) is the guide, and this is what stands behind it:
what the .deb does that a pip install does not, why each line of the pip route is the
line it is, and what a venv under `$HOME` costs when somebody else has to run the
tools. All of it was measured on a default Ubuntu 24.04 and a default 26.04 desktop
installed from the release ISOs, `noble-gnome-iso` and `resolute-gnome-iso`.

### The .deb

**One** `Architecture: all` package for **both** Ubuntu 24.04 and 26.04. Every module
here is pure standard library, so it lands in the version independent
`/usr/lib/python3/dist-packages` and your own `python3` byte compiles it at install
time, 3.12 on 24.04 and 3.14 on 26.04, from the same file. In the box: the six tools
in `/usr/bin`, the GNOME bridge extension system wide, the udev rule applied at once
with no reboot, and the `warandr` menu entry.

It does **not** replace the real `xdotool`, `wmctrl`, `xprop` or `xrandr`. Not one
path it ships is owned by their packages, so the X11 handover keeps finding them and
the symlinks over the originals stay the user's choice.

`sudo apt remove w11` takes the extension, the udev rule and the six commands
with it, `/dev/uinput` goes back to `root:root 0600`, and the session in progress
keeps running. `sudo apt purge w11` drops the last of its bookkeeping. All of
that paragraph and the one above it was run on a default Ubuntu 26.04 desktop and
written down command by command in
[vm/README.md § The package on a default
install](../vm/README.md#the-package-on-a-default-install).

Alongside a pip install of the same source, the two do not fight. The
`/usr/local/bin` symlinks the pip route makes keep winning for the six names, because
`/usr/local/bin` comes first on the Ubuntu `PATH`, and the package owns nothing under
`/usr/local`. One thing to know if the clone is what you work on: inside a
`--system-site-packages` venv, an editable install loses to the packaged modules, so
`import wdotool` finds the packaged copy. `debian/README.Debian` has the detail and
the one line that gets you back to the clone.

The built package is in the repository, at `release/w11_<version>_all.deb`,
so a clone is already installable and `git log release/` is the record of every
binary that shipped. `scripts/build-deb.sh` writes it there and replaces the one it
finds, dropping any package left over from an older version so exactly one file is in
the tree and the README can name it. What the build also produces and the repository
does not keep is the `.changes` and the `.buildinfo`: a `.buildinfo` is a description
of the machine that ran the build, down to the version of every package installed on
it, which is a build record rather than something anyone installs. Those go to
`dist/`, which `.gitignore` excludes along with the six zipapps of
`scripts/build-pyz.sh`, the `__pycache__` trees, the nix `result` links and the VM
images. Committing a binary is only tolerable because this one is
reproducible: `dpkg-buildpackage` timestamps every member from `debian/changelog`,
so two builds of the same source give the same bytes (measured: identical SHA-256
over two runs), and `git status` stays clean after a rebuild that changed nothing.

`scripts/build-deb.sh` installs its own build tools from the Ubuntu archive on first
run, nothing from a PPA:

```
dpkg-dev debhelper dh-python pybuild-plugin-pyproject python3-all python3-setuptools
```

Pass `--no-deps` to install them yourself instead. What goes where, and why the
extension and the rule are handled the way they are, is `debian/README.Debian`.

### The rpm

`sh scripts/build-rpm.sh` produces **three** noarch packages into `dist/`, and nothing is
committed, because the Python payload lands in `%{python3_sitelib}` and carries an
auto-generated `Requires: python(abi) = 3.14`. Fedora 43 and 44 both ship python3 3.14.7, so
one build covers both and rawhide (3.15) needs its own — which is what the `rpm-install`
matrix of three tags proves. The .deb's "one file for both supported releases" property has
no counterpart here.

The three are `w11`, `gnome-shell-extension-w11-bridge`
(`Supplements: (w11 and gnome-shell)`, so dnf installs it wherever both halves are
present and on no sway or KDE box) and `gnome-shell-extension-w11-overlap` (no
`Supplements` at all: nothing installs the one thing that can cost the session you are
sitting in). That split is why the README's "the package carries a second, separate
extension" is a sentence about dpkg: on Fedora it is a second, separate *package*. The GTK
stack is `Recommends: python3-gobject gtk3` rather than a hard dependency — weak deps are on
by default in dnf — which is a deliberate divergence from the .deb, and
`tests/test_release_deb.py`'s `TheGtkDependency` still carries the deb half as an
`expectedFailure`. `%post`/`%postun` are line for line
`debian/w11.postinst`/`.postrm`, and the tests compare them phrase for phrase.
`packaging/rpm/w11.rpmlintrc` is the analogue of the lintian overrides: three
accepted findings, a sentence each.

### The PKGBUILD

`sh scripts/build-pkgbuild.sh` produces one `.pkg.tar.zst` into `dist/`; it is a CI artefact
and is never committed, because Arch's site-packages is version-pinned
(`/usr/lib/python3.14`). `arch=('any')` holds only because `build()` regenerates the overlap
extension's three type descriptions with gobject-introspection — the checked-in ones are LP64
blobs (§ 6) — measured working on Arch's g-ir-compiler 1.86.0, where `gen-gir.py --check`
printed "3 compared, 0 skipped". `packaging/arch/w11.install` is the third copy of
the udev procedure (`post_install`/`post_upgrade`/`post_remove`), and
`packaging/arch/namcap.expected` is the accepted-findings list, one Python regular expression
per line, three of them.

Arch's `extra` carries xdotool 4.20260303.1, wmctrl 1.07, xorg-xprop 1.2.8 and xorg-xrandr
1.5.4 — the exact four versions these tools clone — so the README's footnote (b) about the
X11 handover landing on a tool with no `windowstate` is an Ubuntu fact and does not apply
there.

**The udev procedure is one procedure in three dialects**, and the ordering reason is the
same on all three: the trigger has to follow the reload, because both Fedora's systemd-udev
file trigger and Arch's `35-systemd-udev-reload.hook` run *after* the scriptlet. A change to
any one of the three turns the other two red.

Nothing is published to COPR, Fedora or the AUR yet, and that is the owner's call rather than
a licence problem: the tree is BSD-2-Clause, the spec says `License: BSD-2-Clause` with
`%license LICENSE` in `%files`, the PKGBUILD says `license=('BSD-2-Clause')` and `package()`
installs the text under `/usr/share/licenses/w11/` — which is what namcap wants of a
licence that is not one of `/usr/share/licenses/common/` — and both build scripts read the
identifier back out of `LICENSE` (an `SPDX-License-Identifier:` header verbatim, otherwise a
heading table) and refuse the build if the spec or the recipe disagrees with it.

One thing the project has not settled and this file will not settle for it: the upstream URL
is spelled **three** ways. `debian/copyright`'s `Source:` says github.com/zardus/w11;
`README.md`'s install section, `debian/control`'s `Homepage:`, the spec's `URL:` and the
PKGBUILD's `url=` say github.com/zardus/w11; and the flake's own invocation
— `nix run github:emolabs/w11`, in `nix/package.nix`'s `meta.homepage`, in README.md's
Nix bullet and in this file's own flake section — says emolabs. Whoever settles it should change
every one of them in the same commit — `debian/copyright`, `debian/control`, `README.md` (which
carries two of the three spellings), the spec, the PKGBUILD, `nix/package.nix` — and re-measure
the PKGBUILD's pinned `sha256sums` against the tarball at whichever host wins (today's pin is
the GitHub tarball, 3,952,338 bytes). CI never uses either line — `build-pkgbuild.sh`
rewrites the `url=` and `nix build` here is always a local path — so nothing is red today.

### The flake, and the NixOS module

`nix run github:emolabs/w11 -- --version` runs the tools without installing anything.
The flake is six packages rather than one: `w11` (five of the six tools, stdlib,
216.0 MiB of closure), `warandr` (the one GTK program, 546.9 MiB), `gnome-bridge`,
`gnome-overlap`, `udev-rules` and `x11-shadows`. The two installable ones have deliberately
disjoint file sets — `w11` owns `bin/{wdotool,wwmctl,wxprop,wxrandr,wmirror}` and the
Python tree, `warandr` owns `bin/warandr` and `share/applications` and nothing else — because
`buildEnv` resolves a collision silently in `environment.systemPackages` (first package in the
list wins) and fatally in home-manager's `home.path`, which sets `ignoreCollisions = false`.

`nixosModules.default` is `programs.w11.{enable, package, warandr.enable,
uinput.enable, gnomeBridge.enable, gnomeOverlap.enable, x11Tools.enable, shadowOriginals,
wlMirror.enable}` plus one assertion refusing `hardware.uinput.enable` beside it. On a GNOME
machine it installs the bridge and turns it on for every user through a system dconf profile,
so it is enabled at the **first** login with no logout step — the one route in the tree where
that sentence is true. `homeManagerModules.default` does the per-user half and warns, in the
module itself, that home-manager cannot grant `/dev/uinput` at all.

`programs.w11.shadowOriginals = true` puts `x11-shadows` over the real
xdotool/wmctrl/xprop/xrandr/arandr with `lib.hiPrio`, which on NixOS is the only way to say
"installed over the originals": without it the system path resolves the collision in the
**originals'** favour, silently — measured, `/run/current-system/sw/bin/xdotool` was
xdotool-3.20211022.1 on a Wayland session. And on NixOS `/dev/uinput` is `crw------- root
root` with no rule at all until something sets one, so on GNOME and KDE the input commands
need the module or root, while on sway, Hyprland, labwc, river, Wayfire and COSMIC nothing is
needed at all, because the tools inject through the compositor's virtual-input protocols
(measured both ways in NixOS VM tests).

`nix flake check` no longer passes with nothing to run — there are five of them: `nixos-sway` (sway 1.12 with the module, every
backend and input claim asserted), `nixos-gnome` (GNOME 50.4, the bridge on the bus, the
uaccess ACL), `nixos-kde` (kwin 6.7.4, the registry-object discovery path), `module-eval`
(every option on, `toplevel.drvPath` forced through `builtins.unsafeDiscardStringContext` so
nothing is built — 8.6 s and two input derivations, where without the discard the check pulled
in 3993 and built the system) and `tools` (the unittest suite as a derivation, one file per
`python3 tests/<f>.py` under `dbus-run-session`).

### Why the pip route reads the way it does

* **Why a virtual environment.** Ubuntu 24.04 and newer mark the system Python as
  externally managed, so a plain `pip install -e .`, with or without `--user`,
  refuses with `error: externally-managed-environment` and points at a venv, at pipx,
  or at `--break-system-packages`. Those are the honest options, all of them work,
  and a venv is the one that changes nothing outside its own directory.
* **Why `python3-venv` and not `python3-pip`.** A stock Ubuntu desktop has neither
  pip nor venv nor pipx, checked against both installers' own package sets, so
  `pip install` is `pip: not found` before PEP 668 gets a word in. `python3-venv` is
  all you need, because the venv brings its own pip along. (Ask for a directory
  without it installed and the error names the *versioned* package, `python3.12-venv`
  on 24.04 and `python3.14-venv` on 26.04. `python3-venv` pulls the right one on
  both. Run with no arguments at all it only prints argparse's `the following
  arguments are required: ENV_DIR`.)
* **Why `--system-site-packages`.** `warandr` is the one tool with a dependency: the
  Python GTK 3 bindings, which are the apt packages `python3-gi` and `gir1.2-gtk-3.0`
  rather than something pip should build. Every GNOME, KDE and Xfce install already
  has them, but a venv hides system packages unless it is told not to, and then
  `warandr` exits 1 with this:

  ```
  warandr: GTK 3 for Python is not available (No module named 'gi') - on Ubuntu/Debian: sudo apt install python3-gi gir1.2-gtk-3.0
  ```

  The other five tools are stdlib only and never notice.
* **Why `-e`, and keep the clone.** pip installs the six commands and nothing else.
  Not `gnome/install-bridge.sh`, not the udev rule, not `warandr.desktop`. Those are
  used from the clone, so keep it where it is and let the editable install point at
  it.

`pip install -e .` fetches setuptools, so it wants network. **On 26.04** a
`--system-site-packages` venv can use the system copy instead, because a default
install ships `python3-setuptools` 78:
`~/.venvs/w11/bin/pip install --no-build-isolation -e .` **On 24.04 that
shortcut does not work** and the ordinary line is the one to use: a default 24.04
desktop has no `python3-setuptools` at all (`ModuleNotFoundError: No module named
'setuptools'`), and where it is installed it is setuptools 68, which still needs the
separate `wheel` package (`error: invalid command 'bdist_wheel'`). Both measured on
24.04 default and cloud images, see [vm/README.md](../vm/README.md).

### A venv under `$HOME`, and other accounts

Ubuntu home directories are `0750`, so a venv under one is readable by its owner and
root only. Symlinks in `/usr/local/bin` that point into it break for *other* users,
and the message they get does not say why: `sudo` reports `unable to execute
/usr/local/bin/wdotool: Permission denied` on 24.04 and `sudo:
'/usr/local/bin/wdotool': command not found` on 26.04. For a machine wide drop-in use
one venv under `/opt`, and note the missing `-e` there: an editable install keeps
reading the source tree at run time and hits the same wall, while from a readable
copy it does not. A symlink left behind after the venv is deleted just says `No such
file or directory`, so remove the links when you remove the install.

### What the default installs said about the guide

The install guide was run on both ISO installs *verbatim*, as a reader would:
`sudo apt install git python3-venv`, clone, venv, `pip install -e .`, the
`/usr/local/bin` symlinks, `gnome/install-bridge.sh`, one logout, `--udev`. Every
command worked as written on both, and the stock facts the guide leans on hold on a
real default install of either release (no pip, no venv, no pipx, no `git`, no
`curl`, while `python3-gi`, `gir1.2-gtk-3.0`, `acl`, `x11-utils` and
`x11-xserver-utils` are all present, so `warandr`'s GUI comes up with nothing extra
installed). All six tools then behaved **identically to the matching cloud image
flavor**, as the desktop user and as root over ssh with an empty environment. The
24.04 run corrected three sentences of the guide: the *optional*
`--no-build-isolation` line, the bare `python3 -m venv` error, and the
`wxrandr --print-backend --verbose` block in *Check it worked*, which had been one
line short of what the tool prints since the day it was written.

Two things about a default install are worth knowing before trusting a script on one,
and neither is visible on the cloud image flavors, which switch both off:

* **it locks itself.** `idle-delay 300` and `lock-enabled true` are the defaults on
  24.04 and 26.04 alike, and GNOME Shell disables extensions behind the lock screen,
  so five idle minutes turn every window command into `gnome backend: the w11
  bridge is unavailable while the screen is locked`, rc 1 (rc 2 for `wdotool`). It
  also switches the outputs off, so a screenshot taken then is black. `wxrandr`,
  `warandr` and input injection keep working, and injecting the password is a way
  back in. Five minutes is less than the guide itself takes: on the 24.04 default
  install `sudo apt install git python3-venv` alone ran for three and a half minutes
  and the session was locked by the time the bridge was installed.
* **it has no `xdotool` and no `wmctrl`**, so the X11 handover has nothing to hand to
  until `sudo apt install xdotool wmctrl`. On a Wayland session nothing hands over,
  so this only bites on an X11 session or under `W11_PASSTHROUGH=always`.
  The exit 127 line names whichever of the two it is: on an X11 session it says so,
  and on a Wayland one it says a handover was asked for, because the session it is
  refusing to hand over in is not an X11 session at all.

## 12. The threat model in full

The [README](../README.md#threat-model) states this in short. Here it is with the
mechanics, because "granted once and standing" is the design and not an accident.

**What installing the pieces grants, and to whom.**

* **The GNOME bridge extension** grants **every process that can reach your session
  bus**, including a sandboxed app allowed to talk to `org.gnome.Shell`, because the
  object answers there too, the ability to list every window with its title, class,
  pid, geometry and workspace (stock GNOME withholds that:
  `org.gnome.Shell.Introspect` is sender-allowlisted), to move, resize, restack,
  close and **SIGKILL** any window, to learn `DISPLAY` and the path of Mutter's
  Xwayland cookie, to take the shell's modal input grab for the length of one window
  pick, and to confirm a pending display-configuration change. There is no partial
  mode and no caller check. The bridge never evaluates code and never injects input.
  Flatpak and Snap apps without session bus access cannot reach it.
* **The udev rule** grants `/dev/uinput`, which is the ability to type as you, to the
  user of the **active seat session**, through a logind ACL, and to nobody else: no
  group, no standing channel. The grant is checked at `open()`, so the daemon
  re-checks it before every injection and destroys its devices when the seat moves to
  another session, and a user who switches away therefore stops being able to type
  into the session they left. That is about the *kernel* device, which is global to
  the machine. On wlroots the Wayland route still works while the seat is elsewhere,
  because it reaches only the compositor whose socket it connected to, which is your
  own.
* **`zwp_virtual_keyboard_v1` and `zwlr_virtual_pointer_v1` on wlroots grant nothing
  that was not already granted**, and that is the note. sway advertises both
  protocols to **every client of your Wayland socket** and restricts them to none:
  any of them could already upload a keymap and type as you, or move your cursor and
  click, with or without us. wdotool installs nothing to use them and asks nobody for
  permission. It is the compositor's grant, to everything that can open your
  compositor's socket, which is the same-uid boundary below. Two consequences worth
  spelling out: on sway, injecting input needs neither root nor the udev rule at all,
  and the lock-screen note applies to these routes as much as to the kernel one.
  Mutter and KWin implement neither, so nothing changes there.
* **KDE needs nothing installed**, which is itself the note: any client of a Plasma
  session bus can already load a script into KWin, with or without us.
* **Running as root** (`sudo wdotool`) is the alternative to the udev rule. Then the
  tools find the graphical session by scanning `/run/user/*` and logind, and talk to
  that user's compositor as root.

**What is never asked at run time.** Nothing here asks the desktop portal for a
capability, so GNOME's and KDE's *Remote Desktop* consent dialog never appears, and
nothing here uses PolicyKit, so no polkit agent window does either. (The one portal
call anywhere is `Settings.ReadAll` on GNOME, a read of the desktop's own settings
with no consent step and nothing granted; see § 10.) That is a deliberate choice, and
this section is its cost: with no per-use prompt, everything is granted once and
standing, by the bullets above, whether that is the udev rule, the bridge extension
or `sudo`. The only prompt any of it can raise is GNOME's *Keep these display
settings?*, on an explicit `wxrandr --persistent`.

**What is deliberately not defended against.** Anyone who can already run code as
you: they can type through the daemon, read the same files and talk to the same
buses, and a same-uid boundary is not one we can enforce, so we do not pretend to. A
hostile compositor (you are already inside it). The lock screen: injected keystrokes
reach it, because the kernel does not know they are injected. Scripts you saved and
run later (`warandr`'s layout scripts are shell scripts, so read one before running
it, as with any script). And nothing here is a sandbox: the tools do not confine what
a command they hand over to, `xdotool` on X11, then does.

**What is defended against**, and stays that way: another local user. The daemon
socket, its lock and the wxrandr state file are private to their owner and validated
before they are believed, the daemon refuses to talk to a socket somebody else is
listening on, a state file that is not ours is ignored rather than obeyed, the
real-tool search never looks in the current directory, and a root run with no session
never hands a planted X server another user's cookie.

## 13. The X11 proxy

`xw11/` is the one package here that is not a clone. It is an X11 **display**: it binds a
display number, forwards every byte to the session's X server, and answers the requests
that server has no answer for, because the windows they are about are native Wayland
toplevels that were never X windows. The user-facing contract, every variable and the
**What differs** table AGENTS.md asks for are [XW11.md](XW11.md); this section is the
shape of the thing, for whoever changes it.

**One process per session, one loop.** A single `selectors` loop owns every socket: the
two listening sockets of its own display number (abstract name and filesystem path, the
abstract one first, because libxcb tries that first and a proxy that owns only the path is
silently bypassed), one upstream socket per client, one connection of the proxy's own, and
one pipe the event pump writes a byte on. Reads are `recvmsg` with a 64-byte ancillary
buffer, because a plain `recv()` takes the payload and drops any `SCM_RIGHTS` descriptor
silently. Buffers are watermarked at 4 MiB and 1 MiB: a client that stops reading stops
being read *from*, and nothing is dropped. An EOF on either side reaches the other side
**after** the bytes that came before it — the read half is shut down, the write half keeps
flushing, and the pair closes when both buffers are empty.

**The proxy's own connection** is opened at the first client accept and never at startup.
Its five steps cost **65 requests in two round trips, measured at 0.9-1.3 ms** against a
live X server on this box: it selects `PropertyChange | SubstructureNotify` on the root,
asks `QueryExtension` for eight extensions, interns 54 atom names, reads
`GetKeyboardMapping(8, 248)` whole and reads the root's `_NET_SUPPORTED`. The first open of
a *process*, with the imports in it, is 13.3 ms. That connection is what the shadow ids are
minted out of, and it is held while any client is connected and for `_IDLE_SECONDS = 900.0`
after the last one leaves, checked every `_CHECK_SECONDS = 15.0`.

No extension major number is written down anywhere in `xw11`. They are per server — RANDR
is 139 on Xwayland and 140 on Xvfb 21.1.22 — and the same proxy against a fake that answers
152 for XTEST resolves 152. Atoms are interned once on that connection and never per
client: they are server-global (the same name came back `0x187` on three independent
connections), so `InternAtom` and `GetAtomName` are forwarded untouched and the ids in a
synthesized property are the ids the client resolves.

**Five classes.** Every request is PASS (forwarded, reply untouched), EDIT (forwarded,
reply rewritten on the way back), CONSUME (nothing for the client), ANSWER (the proxy
synthesizes the reply) or BATCH (a RandR write inside a grab, applied when the grab
closes). The whole table is [XW11.md § The request policy](XW11.md#the-request-policy).

**The sequence axiom**, which is why CONSUME and ANSWER send something upstream rather than
nothing. X sequence numbers are the client's own request count, and the client is counting;
so a request the proxy handles itself still has to consume exactly one sequence upstream.
CONSUME sends `NoOperation` (opcode 127) and ANSWER sends `GetInputFocus` (opcode 43). The
second is not only a counter: libxcb sets `request_completed = request_read - 1` on *any*
packet it reads, so a locally written reply for request N+1 that goes out before the
forwarded reply for N arrives makes the client read `NULL` for N. The `GetInputFocus`
reply comes back **in stream order** carrying the substituted request's sequence, and the
proxy writes the stored packet in its place — exact ordering with no opcode-has-reply
table, no timer and 32 bytes of state. It costs one round trip, about **70 µs** on this box,
per locally answered reply-bearing request: under 2 ms for a whole `xdotool search`.

The same axiom runs backwards for events. An event stamped above a reply that has not gone
out marks that request completed with no reply, and the reply arriving afterwards with a
lower 16-bit sequence is widened by 65536 and the connection tears down. So an event
carries not the client's request count but the count held back behind anything it is still
owed. One half is open and is a known risk rather than a behaviour: a reply the proxy
*forwarded* is invisible to that watermark, because knowing one is in flight needs the
opcode-has-reply table the proxy does not keep. Nothing measured has hit it; the route if
something does is that table, built from `/usr/share/xcb/*.xml`, which this tree already
parses in its tests.

**What it costs to forward.** A Python byte forwarder moved 549 MB/s on this box, a
round trip through one is about 70 µs, and a 1 MB reply streams with a peak buffer under
128 KiB. The whole parity oracle runs through a pass-through proxy and differs from a
direct run only in the `Ran N tests in` lines — `W11_PARITY_PROXY=1 sh
scripts/parity-oracle.sh` is that check, and `tests/test_xw11_parity.py` runs both and
diffs them with the timing normalised.

**Where the window answers come from.** The registry re-reads the compositor when its last
read is older than **20 ms** or the event pump has invalidated it; `list()` is 0.08 ms on
sway and `get_desktop()` 0.02 ms. Backend calls are made on the loop thread under one lock,
so a compositor that hangs delays every other client by at most that backend's own timeout,
10 s, and loses no byte. A write the proxy consumes arms a re-read at **+50 ms** and
**+250 ms**, because sway sends no event at all for a floating `move position`: measured on
headless sway 1.11, the new rect was in `get_tree` on the first 5 ms poll of all ten runs,
0.9 to 3.6 ms after the IPC call returned, so the first poll is fourteen times the measured
worst case and the second is the backstop for a compositor an order of magnitude slower.
One RandR apply through the sway backend takes 1 to 44 ms over eleven invocations; Mutter's
`ApplyMonitorsConfig` waits up to 5 s for `MonitorsChanged`, which is why the apply runs on
a worker thread and only the applying client's reads are paused.

**The wrapper.** `xw11/wrap.py` is the second hook every wrapped `main()` calls, right after
`passthrough.maybe_exec_real()`: on a Wayland session with the original installed, the
process is replaced by the original with `DISPLAY` pointing at the proxy. Six rules decide
it and the first five cost no process at all; they are listed in
[XW11.md § The wrapper rules](XW11.md#the-wrapper-rules). The import is *inside* `main()`
and guarded with `try/except ImportError`, for two load-bearing reasons: on an X11 session
`maybe_exec_real` has already replaced the process one line above, so the import never
happens there; and the four zipapps carry no `xw11/` at all, so the guard is what keeps
`dist/wdotool` working. `tests/test_passthrough.py` holds an AST scan that keeps it that
way.

The spawn is `wdotool/daemon.py`'s, not re-invented: fork, setsid, fork, stdio to the log,
every inherited descriptor closed, out of the launcher's transient scope, cwd `/`, re-exec
as `xw11 __serve`. The wrapper then polls the display file for two seconds at 50 ms;
measured on 2026-09-11, six runs, **53, 105, 104, 105, 109 and 203 ms** from the fork to a
display file a client can dial. Ten wrappers starting at once leave exactly one `__serve`,
because the session lock is an abstract socket bind and a starter killed between the bind
and the display file leaves nothing to recognise.
