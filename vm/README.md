# vm/ — test VMs

**`vmctl`** is the rig: full, default-configured desktops in QEMU/KVM, in **38 flavors** over
four distributions — Ubuntu, Fedora, Arch and NixOS — and 19 desktop tokens, from GNOME and
Plasma down to river with a reference window manager built from source. Most are a *cloud* image
plus a desktop metapackage; two (**`resolute-gnome-iso`**, **`noble-gnome-iso`**) are installed
from the Ubuntu 26.04 and 24.04 desktop **ISOs by the Ubuntu installer itself** — the images a
claim about "a default Ubuntu desktop install" has to rest on, one per supported LTS — and two
(**`nixos-sway`**, **`nixos-gnome`**) are NixOS configurations built with `nix build`.

Each autologins user `test` on a **multi-head virtio-vga** whose monitors are plugged,
unplugged and resized from the host at runtime, with host-side screenshots of every head.
This is the rig for testing all six tools against real Wayland *and* X11 sessions, and for
the X-parity oracles: every *cloud-image* golden also carries the real `xdotool`, `wmctrl`,
`x11-utils` and `x11-xserver-utils`. The two **ISO** goldens carry what the Ubuntu installer
installs and nothing else, which is `x11-utils` and `x11-xserver-utils` but **not** `xdotool`
or `wmctrl` — that is the point of them, and it is also what a reader of the repo README's
X11 section is told to `apt install`. What the six tools currently manage on each desktop —
including where they have no backend at all — is written down per flavor under *What the six
tools do on each flavor*, which is the measurement behind the *Desktop support* matrix in the
repo README.

## Quickstart

```console
$ vm/vmctl build noble-gnome            # ~7 min, once; golden image -> ~/vm-data/golden/
$ vm/vmctl build resolute-kde           # any of the 34 cloud-image flavors (see Flavors below)
$ vm/build-iso-golden.sh resolute-gnome-iso   # ~14 min: the real installer, off the desktop ISO
$ vm/build-iso-golden.sh noble-gnome-iso      # ~21 min: the same, off the 24.04 desktop ISO
$ vm/vmctl start gnome1 --flavor noble-gnome --heads 3
vmctl: gnome1: QEMU pid 1234, flavor noble-gnome, 3 vCPU/4G, ssh port 2400, bus ...
vmctl: gnome1: heads: 0=1920x1080, 1=1920x1080, 2=1920x1080  (guest connectors Virtual-1, Virtual-2, Virtual-3; set before the guest boots)
vmctl: gnome1: ssh up after 23s
2400
$ vm/vmctl session gnome1               # blocks until test's Wayland session is active
SESSION_ID=1
XDG_RUNTIME_DIR=/run/user/1000
WAYLAND_DISPLAY=wayland-0
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
$ vm/vmctl user gnome1 -- gdbus call --session --dest org.gnome.Mutter.DisplayConfig \
      --object-path /org/gnome/Mutter/DisplayConfig \
      --method org.gnome.Mutter.DisplayConfig.GetCurrentState
$ vm/vmctl user gnome1 -- sh -c 'loginctl show-session $XDG_SESSION_ID -p Type'   # Type=wayland
$ vm/vmctl shot gnome1 --all /tmp/gnome1     # -> /tmp/gnome1-0.png, -1.png, -2.png
$ vm/vmctl head gnome1 3 1280x1024           # 4th monitor (Virtual-4) appears in GNOME
$ vm/vmctl head gnome1 3 off                 # ...and goes away again
$ vm/vmctl heads gnome1                      # guest connectors after a forced re-probe
$ vm/vmctl scp gnome1 dist/wxrandr.pyz gnome1:/home/test/
$ vm/vmctl user gnome1 -- python3 /home/test/wxrandr.pyz --query
$ vm/vmctl stop gnome1                       # or: destroy (also deletes the overlay)
$ vm/selftest.sh noble-gnome                 # the whole thing end to end, ~40 s (see below)
$ vm/selftest.sh resolute-kde                # same check, Plasma's own tools (see below)
$ vm/selftest.sh resolute-gnome-iso          # ...and on the installer-built default installs
$ vm/selftest.sh noble-gnome-iso             # ...both of them
```

Host requirements: `qemu-system-x86_64` (8.2+, with the **dbus** display
backend and PNG screendump), `qemu-img`, `cloud-localds` (cloud-image-utils),
`dbus-daemon`, `gdbus` (libglib2.0-bin), `ssh`/`scp`/`ssh-keygen`, Python 3.10+,
KVM access, and the Ubuntu cloud images in `~/images/`
(`noble-server-cloudimg-amd64.img`, `ubuntu-26.04-server-cloudimg-amd64.img`;
override the directory with `VMIMAGES=`). `vmctl` is stdlib-only Python.
The ISO flavors additionally need `isoinfo` (genisoimage) and the desktop ISO each yaml
names, in the same `~/images/`: `ubuntu-26.04.1-desktop-amd64.iso` (6.1 GB) from
<https://releases.ubuntu.com/26.04/> and `ubuntu-24.04.4-desktop-amd64.iso` (6.2 GB) from
<https://releases.ubuntu.com/24.04/> — `vm/build-iso-golden.sh` checks each one's sha256
against the one in the flavor before it boots anything.
`selftest.sh` additionally wants ImageMagick's `identify` (to prove a screenshot is not a flat colour).

## Fresh host setup

The full version of this section — machine, sizing, packages, what `vmctl` needs from QEMU and how to check it, housekeeping — is [`vm/SETUP.md`](SETUP.md); `vm/setup-host.sh` does the mechanical part.

Any Linux box (physical, or a VM whose hypervisor exposes KVM to it,
so that `/dev/kvm` exists inside it) works. On Ubuntu 24.04:

```console
$ sudo apt-get install -y qemu-system-x86 qemu-system-gui qemu-utils cloud-image-utils \
      dbus libglib2.0-bin socat imagemagick openssh-client genisoimage
$ sudo usermod -aG kvm "$USER"          # re-login afterwards
$ mkdir -p ~/images && cd ~/images
$ curl -LO https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
$ curl -LO https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img
$ curl -LO https://releases.ubuntu.com/26.04/ubuntu-26.04.1-desktop-amd64.iso   # ISO flavor only
$ vm/vmctl build noble-gnome && vm/vmctl build resolute-gnome   # ~7 min each on 4 vCPU
$ vm/build-iso-golden.sh resolute-gnome-iso                     # ~14 min on 2 vCPU
$ vm/selftest.sh noble-gnome && vm/selftest.sh resolute-gnome
```

`qemu-system-gui` provides the dbus display module (`qemu-system-x86_64 -display help`
must list `dbus`). Golden images keep an absolute backing-file path to `~/images`, so
copying `~/vm-data/golden` and `~/images` to another host with the same home directory
layout works too. Sizing: a desktop VM needs 2 vCPU / 3 GB; a golden build 4 vCPU / 6 GB
(`--cpus 2` on small hosts). A 4 vCPU / 16 GB host runs one build or two desktop VMs at a
time comfortably; 8 vCPU / 32 GB runs four.

## Files

| path | what |
|---|---|
| `vm/vmctl` | the CLI (host side) |
| `vm/build-image.sh` | guest-side build script; embedded into the flavor's cloud-init user-data by `vmctl build`, runs once as root inside the build VM |
| `vm/build-iso-golden.sh` | host-side builder for the **ISO flavors** (`# vmctl-iso:` in the yaml): runs the real Ubuntu desktop installer off the release ISO, unattended and headless, then configures the result. `vmctl build` cannot make these and says so. |
| `vm/build-iso-image.sh` | guest-side stage 2 of that build; copied into the freshly installed system and run once as root over ssh (the installer switches cloud-init off in the target, so nothing else can run it). Deliberately tiny: an ISO flavor may not be tidied up. |
| `vm/flavors/<flavor>.yaml` | cloud-init user-data template per flavor (`@@ROOT_PUBKEY@@`, `@@BUILD_SCRIPT@@` placeholders; `# vmctl-base:` names the base cloud image, `# vmctl-desktop:` the desktop **and its session type** — `gnome`, `kde`, `kde-x11`, `xfce` or `sway`). An **ISO flavor** has `# vmctl-iso:`/`# vmctl-iso-sha256:` instead of `# vmctl-base:`, and its body is not user-data for a build VM but the **autoinstall description the live installer reads**. |
| `vm/reference/<flavor>-packages.txt` | `dpkg-query -W -f='${binary:Package}\n'` of the finished golden image: **exactly what that image contains**, nothing more. For `resolute-gnome-iso` and `noble-gnome-iso` that is also what a default Ubuntu 26.04 / 24.04 desktop install contains (one package added: `openssh-server`); for the cloud-image flavors it is *not* — those are an Ubuntu cloud image plus a desktop metapackage, which is close to but measurably not the same thing (226 packages a default install does not have, 55 it has and the cloud image has not, a different kernel with no firmware at all, and 8 snaps against the default 13 — see *The default install* below). Multi-arch names carry their `:amd64` suffix (`libei1:amd64`), so grep for exact names with `^name(:amd64)?$`. |
| `vm/selftest.sh <flavor> [name]` | end-to-end check of any flavor's golden image: boot 3 heads, autologin, monitors/primary in that desktop's own display tool, no stray first-run window, real screenshots, hotplug (details below) |
| `vm/demo/` | how the three GIFs in `media/` are recorded, on `resolute-gnome-iso`: a host-side QMP `screendump` recorder (no capture tool in the guest, so the compositor does not matter), a guest-side typist that drives a real `bash` in a pty, the take scripts and the ffmpeg/gifsicle encoder. See [vm/demo/README.md](demo/README.md). |

State (never in the repo) lives under `$VMDATA` (default `~/vm-data`):

```
golden/<flavor>.qcow2            golden image: qcow2 overlay whose backing file is the
                                 base cloud image by ABSOLUTE path (~/images/...). It does
                                 not depend on any build or instance directory.
golden/<flavor>-packages.txt     package list (copied into vm/reference/)
golden/<flavor>.build.log        serial log of the build VM
instances/<name>/disk.qcow2      overlay on golden/<flavor>.qcow2
instances/<name>/seed.img        cloud-init NoCloud seed (hostname, root key, user test)
instances/<name>/{qmp.sock,bus,dbus.pid,qemu.pid,serial.log,meta.json}
build/<flavor>/                  transient build VM (removed on success)
keys/id_ed25519[.pub]            guest root ssh key, generated once
```

## Commands

| command | what |
|---|---|
| `vmctl build <flavor> [--cpus 4] [--mem 6G] [--size 30G] [--force] [--keep]` | boot a fresh overlay of the base image with the flavor's cloud-init; an **ISO flavor** is refused here with a pointer to `vm/build-iso-golden.sh <flavor>`, which is what builds those; wait for it to power itself off; keep it as the golden image. Refuses to overwrite an existing golden without `--force` (a rebuilt golden invalidates every instance overlay on it: restart those with `--fresh`). Progress lines from the guest are echoed; the full log is `golden/<flavor>.build.log`. |
| `vmctl start <name> --flavor <flavor> [--heads N] [--head-size WxH] [--mem 4G] [--cpus 3] [--fresh] [--no-wait]` | create (or reuse) the instance, start its private D-Bus, start QEMU daemonized, **size/plug heads `0..N-1` (default 1920x1080 each) before the guest boots**, wait for ssh, print the ssh port. Refuses a double start; stale pidfiles are detected. `--fresh` recreates the overlay disk and seed. Without `--flavor`/`--heads`/`--head-size` a reused instance keeps its previous values (including per-head sizes set with `vmctl head`). |
| `vmctl stop <name> [--timeout 60]` | `systemctl poweroff` over ssh (GNOME inhibits logind's power-key handling, so a bare ACPI button would only pop a dialog), falls back to QMP `system_powerdown`, then QMP `quit`/kill after the timeout; stops the dbus-daemon. |
| `vmctl destroy <name>` | stop + delete `instances/<name>`. |
| `vmctl list` / `vmctl status <name>` | instances with state/pid/port/heads; `status` also reads each QEMU console over D-Bus and says which heads are unplugged (QEMU keeps an unplugged console's last surface size, so the size alone would mislead). |
| `vmctl ssh <name> [-- cmd...]` | root ssh (`StrictHostKeyChecking=no`, known hosts `/dev/null`). A command of several words is quoted before it goes to ssh, so `vmctl ssh n -- sh -c 'a; b'` runs what it says (ssh joins the words with spaces and the guest's shell re-splits them; unquoted, that ran `sh -c a`). A single word is passed through untouched — `vmctl ssh n -- 'a \| b'` still reaches the remote shell as a command line. |
| `vmctl scp <name> <src> <dst>` | scp; write the guest side as `<name>:<path>`. Extra scp flags need a `--` first: `vmctl scp n -- -r src n:/dst`. |
| `vmctl head <name> <idx> <WxH>\|off` | `SetUIInfo` on head `idx` (0-based; `0` = Virtual-1, which can only be resized). A running GNOME picks the change up in well under a second. On an **X11 flavor** an unplug also releases the output in the X server: RandR keeps a removed output's CRTC (mode and position) until the desktop's display daemon disables it, so `off` waits ~5 s for the desktop to do it and otherwise runs `xrandr --output Virtual-N --off` in the session itself, logging `Virtual-N was still enabled in the X server after the unplug; disabled it`. |
| `vmctl heads <name>` | guest DRM connectors (status, enabled, preferred mode) after `echo detect > .../status` — the kernel's cached mode list is stale until something re-probes. |
| `vmctl shot <name> <idx> <out.png>` / `vmctl shot <name> --all <out>` | QMP `screendump` of one head (or every plugged head → `<out>-<idx>.png`). Written by QEMU straight to the host path. **The mouse cursor is not in it** (see Gotchas). |
| `vmctl session <name> [--timeout 180] [--no-paint]` | wait until `loginctl` shows an **active session of user `test` on a seat** whose `Type` is the one the flavor's desktop registers (`wayland`; `x11` for Xfce; sway's greetd session starts as `tty` and libseat turns it into `wayland`) and whose sockets are there (`/run/user/1000/wayland-N`, `/tmp/.X11-unix/X0`, sway's `sway-ipc.*.sock`), **then until the desktop has painted its first frame**: GNOME the `GNOME Shell started` journal line, the others the compositor (`kwin_wayland`, `Xorg`, `sway`) owning head 0's scanout framebuffer in `/sys/kernel/debug/dri/0/state` *plus* the shell processes (`plasmashell`; `xfce4-panel`+`xfdesktop`; `swaybar`) up and any splash (`ksplashqml`) gone — and, when the guest offers none of that, head 0's screendump changing. Whatever the guest says is then confirmed **against the pixels of every head**: a screendump of each active head (QMP `screendump` in PPM, read without any image library) must not be one flat colour (sampled standard deviation >= 0.02). Head 0 can be painted seconds before the others are — on Xfce the panel is up while heads 1 and 2 are still black, which is what `waiting for the first frame: WAIT flat head 1,2` in the log means. `--no-paint` skips the paint wait. Prints `SESSION_ID`, `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY`/`DISPLAY`/`SWAYSOCK`, `DBUS_SESSION_BUS_ADDRESS`, `XDG_SESSION_TYPE`. |
| `vmctl user <name> [-t] -- cmd...` | run `cmd` as `test` via `sudo -u test -H env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus ...` with the session's own environment reconstructed, first match wins: what the session published to the systemd user manager (`gnome-session`, `startplasma`, the X session's, sway's `dbus-update-activation-environment`), else the compositor's `/proc/<pid>/environ` (`gnome-shell`, `kwin_wayland`, `xfce4-session`, `sway`), else the sockets that are actually there. Result: `WAYLAND_DISPLAY` and `SWAYSOCK` on the Wayland flavors, `DISPLAY`+`XAUTHORITY` everywhere (Xwayland's on Wayland, Xorg's on Xfce), `XDG_CURRENT_DESKTOP`, `XDG_SESSION_ID` = logind's display/seat session of `test`, and `XDG_SESSION_TYPE` = `wayland` or `x11` as the session itself reports it (sway publishes `wayland` although greetd registered a `tty` session) — so `xrandr`/`wmctrl`/`xdotool`/`xprop`, `kscreen-doctor`, `swaymsg` and `loginctl show-session $XDG_SESSION_ID` all work in it. `-t` may follow `<name>`. |

## Flavors

**38 flavors** across four distributions: 25 Ubuntu, 6 Arch, 5 Fedora and 2 NixOS. By builder,
34 are a cloud image plus a desktop metapackage, 2 are an Ubuntu desktop ISO run through the
real installer (`resolute-gnome-iso`, `noble-gnome-iso`) and 2 are NixOS configurations built
with `nix build` (`nixos-sway`, `nixos-gnome`). CI builds 38 of them on every push and 0 on
demand. The cloud-image flavors exist because one script gets 19 desktops out of them; the
two ISO ones exist because "it works out of the box on a default Ubuntu desktop" is a claim
about an *installed* system — one per supported LTS, because the two releases install
differently — and a cloud image plus `ubuntu-desktop` is not one (26.04: 226 packages a
default install does not have, 55 it has and the cloud image has not, a different kernel with
no firmware at all, 8 snaps against the default 13; 24.04: 240 / 55 and the same story —
*The default install* and *The second default install*, below).

The 25 Ubuntu flavors are what the *Desktop support* matrix in the repo README is measured on,
minus four that stand behind that table rather than in it: the two `stonking-*` images (Ubuntu
26.10, a development release), which are probes for one protocol change each, and the two
`*-gnome-iso` ones, which are the default-install check rather than a desktop of their own. That
leaves 21 images under the matrix, which is the number the README's own sentence carries.

The 13 Fedora, Arch and NixOS flavors have no golden in CI yet: `scripts/ci-golden.sh` has no
fetch rule for them, so every one of their jobs on run 34340513060 ended before the smoke
started.

A flavor is one `vm/flavors/<flavor>.yaml`, a cloud-config whose comments carry the headers
every rig script reads with the same `sed -n 's/^#[[:space:]]*vmctl-<key>:...` idiom:

| header | values | default | who reads it |
|---|---|---|---|
| `# vmctl-desktop:` | a key of `vm/vmctl`'s `DESKTOPS` table | `gnome` | vmctl, `selftest.sh`, `live-smoke.sh` |
| `# vmctl-base:` | a cloud image in `$VMIMAGES` | — | `vmctl build` |
| `# vmctl-iso:` (+ `-iso-sha256`, `-iso-url`) | a release ISO | — | `vm/build-iso-golden.sh` |
| `# vmctl-nix:` | a `nixosConfigurations` attribute | — | `vm/build-nixos-golden.sh` |
| `# vmctl-distro:` | `ubuntu` \| `fedora` \| `arch` \| `nixos` | `ubuntu` | the host side only: the package-list shape, the archive to wait for, the display-manager hint |
| `# vmctl-ci:` | `push` \| `on-demand` | `push` | CI's plan job |
| `# vmctl-base-sha256:` | 64 hex digits | — | `scripts/ci-golden.sh` |

Exactly one of `-base`, `-iso` and `-nix` per flavor: they are three different builders, and the
header is how each recognises its own. `# vmctl-distro:` is host-side only — the guest derives
its package manager from `/etc/os-release`'s `ID`, so a yaml cannot lie to the guest about what
it is. The `# vmctl-desktop:` header is what `vmctl` and `selftest.sh` key off: which display
manager owns the session, what `loginctl` calls its `Type`, which sockets `vmctl user` must
export, which process paints the first frame, and which native tool reports monitors.
`vmctl flavors` prints the whole table — flavor, distro, desktop, CI class, what it is built
from — which is the answer to "what can I build".

The table's last two columns are `# vmctl-distro:` and `# vmctl-ci:` written out verbatim, and
`tests/test_docs_matrix.py` reads them back against each yaml cell by cell; the `release` column
beside the flavor is prose, and it is the column
`tests/test_backend_gnome.py::ShippedFilesTests` pulls `GNOME Shell <major>` out of to check the
bridge claims every shell the rig carries, so the header line is an anchor as well as a heading.

| flavor | release | desktop (as built) | display manager | session | native display tool | distro | CI |
|---|---|---|---|---|---|---|---|
| `noble-gnome` | Ubuntu 24.04 LTS | GNOME Shell 46 / mutter 46 (`ubuntu-desktop`) | GDM | Wayland | mutter's `org.gnome.Mutter.DisplayConfig.GetCurrentState` | `ubuntu` | push |
| **`noble-gnome-iso`** | Ubuntu 24.04 LTS | **GNOME Shell 46.0 / mutter 46 — installed from `ubuntu-24.04.4-desktop-amd64.iso` by the Ubuntu installer, default source `ubuntu-desktop-minimal`** | GDM | Wayland | the same | `ubuntu` | push |
| `noble-gnome-x11` | Ubuntu 24.04 LTS | GNOME Shell 46 on **Xorg** (`ubuntu-xorg.desktop`, the same `ubuntu-desktop` as `noble-gnome`) | GDM with `WaylandEnable=false` | **X11** | `xrandr`, with `org.gnome.Mutter.DisplayConfig` as a second opinion that must agree | `ubuntu` | push |
| `noble-kde` | Ubuntu 24.04 LTS | Plasma 5.27 / KWin 5.27 (`kubuntu-desktop`, `plasma-workspace-wayland`) | SDDM | Wayland | `kscreen-doctor -o` | `ubuntu` | push |
| `noble-kde-x11` | Ubuntu 24.04 LTS | the same packages, started as the **Plasma X11 session** (Xorg + `kwin_x11`) | SDDM | **X11** | `kscreen-doctor -o` (libkscreen's XRandR backend); `xrandr` | `ubuntu` | push |
| `noble-xfce` | Ubuntu 24.04 LTS | Xfce 4.18 (`xubuntu-desktop`) | LightDM | **X11** | `xrandr` | `ubuntu` | push |
| `resolute-budgie` | Ubuntu 26.04 LTS | Ubuntu Budgie 10.10.2 **on labwc** (magpie is gone) | SDDM (`Session=budgie-desktop.desktop`) | Wayland | `wlr-randr`; second opinion `org.buddiesofbudgie.*` on the session bus | `ubuntu` | push |
| `resolute-cinnamon` | Ubuntu 26.04 LTS | Cinnamon 6.4.13 on muffin 6.4.1 (`ubuntucinnamon-desktop`, `cinnamon.desktop`) | LightDM, `autologin-session=cinnamon` | **X11** | `xrandr` | `ubuntu` | push |
| `resolute-cinnamon-wayland` | Ubuntu 26.04 LTS | the same packages — Cinnamon 6.4.13 on muffin 6.4.1 — started as `cinnamon-wayland.desktop` ("Cinnamon on Wayland (Experimental)") | LightDM, `autologin-session=cinnamon-wayland` | Wayland | `org.cinnamon.Muffin.DisplayConfig.GetCurrentState` over `gdbus` | `ubuntu` | push |
| `resolute-gnome` | Ubuntu 26.04 LTS | GNOME Shell 50 / mutter 50 (`ubuntu-desktop`) | GDM | Wayland | mutter's `GetCurrentState` | `ubuntu` | push |
| **`resolute-gnome-iso`** | Ubuntu 26.04 LTS | **GNOME Shell 50.1 / mutter 50.1 — installed from `ubuntu-26.04.1-desktop-amd64.iso` by the Ubuntu installer, default source `ubuntu-desktop-minimal`** | GDM | Wayland | the same | `ubuntu` | push |
| `resolute-hypr` | Ubuntu 26.04 LTS | Hyprland 0.53.3 (aquamarine 0.10.0), Xwayland, `foot`, `wl-mirror` | greetd (`[initial_session]`, `command = "Hyprland"`) | Wayland | `hyprctl -j monitors`; `wlr-randr` 0.4.1 as a second READER only | `ubuntu` | push |
| `resolute-i3` | Ubuntu 26.04 LTS | i3 4.25.1 (`i3 i3status xterm lightdm dbus-x11`; i3 depends on no display manager) | LightDM + `lightdm-gtk-greeter`, `autologin-session=i3` | **X11** | `xrandr` (i3 has no `output` command at all) | `ubuntu` | push |
| `resolute-kde` | Ubuntu 26.04 LTS | Plasma 6.6 / KWin 6.6 (`kubuntu-desktop`) | SDDM | Wayland | `kscreen-doctor -o` | `ubuntu` | push |
| `resolute-kde-x11` | Ubuntu 26.04 LTS | Plasma 6.6 / KWin 6.6 on **X11** (`kubuntu-desktop` **plus `plasma-session-x11`, `kwin-x11`** — 26.04 installs no X11 session by itself) | SDDM | **X11** | the same | `ubuntu` | push |
| `resolute-labwc` | Ubuntu 26.04 LTS | labwc 0.9.3 (wlroots 0.19.2), Xwayland, `foot`, `swaybg`, `wl-mirror` | greetd (`[initial_session]`, `command = "labwc"`) | Wayland | `wlr-randr` 0.4.1 — a deliberately weak oracle, the same protocol `wxrandr`'s wlr backend writes through | `ubuntu` | push |
| `resolute-lxqt` | Ubuntu 26.04 LTS | LXQt 2.3 on **Openbox** (`lubuntu-desktop`, never `lxqt-core`) | SDDM (`Session=lxqt.desktop`) | **X11** | `xrandr` | `ubuntu` | push |
| `resolute-lxqt-wayland` | Ubuntu 26.04 LTS | Lubuntu's LXQt 2.3 **on labwc** (`lxqt-wayland-session` 0.3.1) | SDDM (`Session=lxqt-wayland.desktop`) | Wayland | `wlr-randr` | `ubuntu` | push |
| `resolute-mate` | Ubuntu 26.04 LTS | MATE 1.26 on marco 1.26.2 (`ubuntu-mate-desktop`, `mate.desktop`) | LightDM, `autologin-session=mate` | **X11** | `xrandr` | `ubuntu` | push |
| `resolute-sway` | Ubuntu 26.04 LTS | sway 1.11 / wlroots, Xwayland, `foot`, `grim` | greetd | Wayland | `swaymsg -t get_outputs` | `ubuntu` | push |
| `resolute-wayfire` | Ubuntu 26.04 LTS | Wayfire 0.10.0 (wlroots 0.19.1) with `plugins = ... ipc ipc-rules stipc` | greetd | Wayland | Wayfire's own `window-rules/list-outputs` over its JSON IPC, `wlr-randr` as the fallback | `ubuntu` | push |
| `resolute-xfce` | Ubuntu 26.04 LTS | Xfce 4.20 (`xubuntu-desktop`) | LightDM | **X11** | `xrandr` | `ubuntu` | push |
| `resolute-xfce-wayland` | Ubuntu 26.04 LTS | Xfce 4.20 as `startxfce4 --wayland`, i.e. **on labwc** (`xubuntu-desktop` plus `labwc`) | LightDM (`autologin-session=xfce-wayland`) | Wayland | `wlr-randr` | `ubuntu` | push |
| `stonking-gnome` | Ubuntu 26.10 | GNOME Shell 51 / mutter 51 (`ubuntu-desktop-minimal`) | GDM | Wayland | mutter's `GetCurrentState` | `ubuntu` | push |
| `stonking-kde` | Ubuntu 26.10 | Plasma 6.7 / KWin 6.7 (`kde-plasma-desktop`) | SDDM | Wayland | `kscreen-doctor -o` | `ubuntu` | push |
| `fedora43-gnome` | Fedora 43 | GNOME Shell 49 / mutter 49 (`workstation-product-environment`) | GDM (`/etc/gdm`) | Wayland | mutter's `GetCurrentState` | `fedora` | push |
| `fedora44-cosmic` | Fedora 44 | COSMIC, cosmic-comp 1.6.0-3.fc44 (Smithay), Xwayland, `foot` (`cosmic-desktop-environment`) | greetd (`command = "start-cosmic"`) | Wayland | `cosmic-randr list --kdl` | `fedora` | push |
| `fedora44-gnome` | Fedora 44 | GNOME Shell 50.4 / mutter 50.4 (`workstation-product-environment`) | GDM (`/etc/gdm`) | Wayland | mutter's `GetCurrentState` | `fedora` | push |
| `fedora44-kde` | Fedora 44 | Plasma 6.7 / KWin 6.7 (`kde-desktop-environment`) | **plasma-login-manager** — the one flavor not seated by SDDM or GDM | Wayland | `kscreen-doctor -o` | `fedora` | push |
| `fedora44-sway` | Fedora 44 | sway 1.11 / wlroots, Xwayland, `foot`, `grim` (`sway-desktop-environment`) | greetd (+ `greetd-selinux`) | Wayland | `swaymsg -t get_outputs` | `fedora` | push |
| `arch-cosmic` | Arch, rolling (20260901) | COSMIC 1:1.7.0-1 | greetd | Wayland | `cosmic-randr list --kdl` | `arch` | push |
| `arch-gnome` | Arch, rolling (20260901) | GNOME Shell 50.4 / mutter 50.4 | GDM (`/etc/gdm`) | Wayland | mutter's `GetCurrentState` | `arch` | push |
| `arch-hypr` | Arch, rolling (20260901) | Hyprland 0.56.2, `xorg-xwayland`, `foot` | greetd | Wayland | `hyprctl -j monitors`; `wlr-randr` 0.5.0 second | `arch` | push |
| `arch-kde` | Arch, rolling (20260901) | Plasma 6.7 / KWin 6.7 | SDDM | Wayland | `kscreen-doctor -o` | `arch` | push |
| `arch-river` | Arch, rolling (20260901) | river 0.4.8 + **tinyrwm built from source** (commit `5c01698c`) | greetd (`command = "river -c /usr/local/bin/vmctl-river-init"`) | Wayland | `wlr-randr` 0.5.0 (river has no output query at all) | `arch` | push |
| `arch-sway` | Arch, rolling (20260901) | sway 1:1.12 / wlroots, `xorg-xwayland`, `foot` | greetd | Wayland | `swaymsg -t get_outputs`, `wlr-randr` second | `arch` | push |
| `nixos-gnome` | NixOS 26.05 | GNOME 50.4 | `display-manager` (GDM) | Wayland | `gdbus` on `org.gnome.Mutter.DisplayConfig` | `nixos` | push |
| `nixos-sway` | NixOS 26.05 | sway 1.12 | greetd | Wayland | `swaymsg -t get_outputs` (and `wlr-randr`) | `nixos` | push |

**Four of these images are the same compositor.** labwc is the Wayland session of Xfce 4.20,
Ubuntu Budgie 10.10 and LXQt 2.x alike, so `resolute-labwc`, `resolute-xfce-wayland`,
`resolute-budgie` and `resolute-lxqt-wayland` are one compositor under four desktops:
`vm/live-smoke.d/{xfce-wayland,budgie,lxqt-wayland}.sh` each source `labwc.sh` whole for that
reason, and the registry of labwc under Xfce is byte-identical to a bare labwc's (the recon
diffed the two dumps) [recon2/xfce-wayland §1].

**A NixOS flavor is not a base image plus a metapackage.** NixOS publishes no cloud image and
runs no cloud-init, so the yaml carries headers and reasons only and the image is built from
`vm/nixos/<flavor>.nix` by `vm/build-nixos-golden.sh`. That build runs its own QEMU inside the
nix sandbox (nixpkgs' `make-disk-image`), so it counts against the one-VM rule and the script
refuses to start beside a running rig instance; it needs `kvm` in `nix config show
system-features` and refuses without it. The root ssh key is baked in rather than seeded, read
from `$VMDATA/keys/id_ed25519.pub` through `builtins.getEnv` under `--impure`. The image was
4.9 GiB and ~10 min on 4 vCPU from a cold store [recon2/nixos §7.4].
`$VMDATA/golden/nixos-*-packages.txt` — written beside the golden by the builder, not committed
under `vm/reference/` — is the store closure of `/run/current-system` with the hashes stripped,
not a dpkg list, and the file says so in its first line. Two specialisations are in every NixOS
image: the default and `without-w11`; `switch-to-configuration test` between them is the
offline analogue of `apt-get remove` and re-install.

**`vm/build-image.sh` is one file in three layers**, still embedded whole into the flavor's
cloud-init user-data by `render_flavor`:

* `pkg_*` — one implementation per package manager (apt, dnf, pacman), chosen from
  `/etc/os-release`. The five-attempt retry, the name lookup before every attempt and the "no
  automatic updates" step are per manager: `apt-daily`/snap on Ubuntu, `dnf5-makecache.timer` on
  Fedora, nothing on Arch. `dnf` treats a `DESKTOP_PKG` word ending in `-environment` as a comps
  environment (`workstation-product-environment`), which is how Fedora names a desktop.
* `dm_*` — one per display manager: `dm_gdm` (`gdm3/` or `gdm/`, whichever directory the package
  created; `WaylandEnable=false` only for an Xorg session; the session NAME resolved off the image
  — `ubuntu.desktop` on Ubuntu, `gnome.desktop` on Fedora and Arch — never from the flavor's
  distro header, which the guest does not read), `dm_sddm`, `dm_plasmalogin` (Fedora 44 replaced
  SDDM with plasma-login-manager in every KDE variant), `dm_plasma` (picks between those two by
  what is installed), `dm_greetd` (the command in both stanzas plus the `Conflicts=getty@tty1`
  drop-in) and `dm_lightdm`. `dm_enable` disables whatever already owns
  `/etc/systemd/system/display-manager.service` by READING the link rather than guessing from a
  list: cosmic-greeter is greetd, is in the `cosmic-desktop-environment` group and self-enables
  mid-transaction, and Fedora's `greetd.service` has no `WantedBy=`, so `fedora44-cosmic` died at
  `systemctl enable greetd.service` until it did.
* `desktop_*` — the quiet-desktop settings, first-run suppression and the head layout of one
  desktop.
* `select_desktop` — the dispatch table, one line per `DESKTOPS` key and nothing else.

Every file the last two layers write is recorded, and on Fedora `relabel` runs `restorecon -R`
over the list: SELinux is Enforcing there and a file written by cloud-init's `runcmd` carries
whatever label that process had.

**Head 0 = `Virtual-1` at (0,0) is the rig's contract**, and wlroots adds the initial outputs in
*reverse* enumeration order. sway is fixed by `/usr/local/bin/vmctl-sway-layout` (swaymsg);
labwc, river and the desktops on labwc by `/usr/local/bin/vmctl-wlr-layout`, the same script
written against `wlr-randr` (it steps by each head's own `(current)` mode width, so
`vmctl start --head-size` is free). Where that helper is HOOKED differs: the bare `labwc` flavor
puts it in `~/.config/labwc/autostart`, while Budgie, `startxfce4 --wayland` and
`startlxqtwayland` — three desktops that merely run ON labwc — put it in
`~/.config/autostart/vmctl-wlr-layout.desktop`, because their labwc is started with a
`--config-dir` of the desktop's own and because labwc's `environment` file overrides variables
that are already set, `XDG_CURRENT_DESKTOP` included. Wayfire needs neither
(`[output:Virtual-N] position = X,0` in `wayfire.ini` places them); Hyprland needs neither
(`monitor = Virtual-N,preferred,<x>x0,1`, with scale pinned to 1 because a runtime headless
output defaulted to 2.0); i3 arranges its heads with `xrandr` from its config and paints them
with `xsetroot -mod 8 8`, because a black root window is one flat colour and fails the rig's
first-paint check.

Three more per-desktop settings the build writes, each for one measured reason:
`desktop_wayfire` gives `wayfire.ini` `[input] xkb_layout = us,de`, which is what makes
`wdotool/xkbmap.py`'s `WayfireLayouts` reachable at all; `desktop_mate` sets
`org.mate.SettingsDaemon.plugins.xrandr turn-on-external-monitors-at-startup=true`, whose own
default is false and MATE is the only flavor where it is; and `desktop_lxqt_wayland` calls
`lxqt_wayland_desktops`, which patches `/usr/share/lxqt/wayland/labwc/rc.xml` from one desktop to
three. That last one is the system copy on purpose: measured 2026-09-09, `startlxqtwayland` copies
that directory into `$XDG_CONFIG_HOME/labwc` the first time it is missing and then runs
`labwc -C $XDG_CONFIG_HOME/labwc -S lxqt-session`, so patching the system copy is what reaches the
session, while pre-creating `~/.config/labwc` would stop LXQt's own `menu.xml`, `themerc` and
`environment` from ever being installed.

The cloud-image flavors (the two ISO flavors keep their installer's defaults instead, and the
two NixOS ones are a configuration rather than an install — see below for what that changes):

* user `test` (uid 1000, password `test`, groups `adm,sudo`, bash, `NOPASSWD` sudo); root ssh by key
  (`~/vm-data/keys/id_ed25519`); hostname = flavor name (instances re-set it to the instance name).
* the desktop metapackage installed non-interactively (`DEBIAN_FRONTEND=noninteractive`,
  `--force-confold`), so the image is as close to a default install of that Ubuntu flavor as a
  cloud image allows. Only four extra packages: `xdotool wmctrl x11-utils x11-xserver-utils`
  (the X-parity oracles; on the Wayland flavors they talk to Xwayland). The one exception is
  `resolute-kde-x11`, which also names the X11 session itself (`plasma-session-x11`,
  `kwin-x11`) because 26.04's `kubuntu-desktop` ships none. Not in the image
  (not part of a default desktop, not needed by the tools): `python3-tk`; `python3-gi` +
  `gir1.2-gtk-3.0`/`gir1.2-gtk-4.0` *are* there for test windows.
* autologin of `test` into that desktop's own session, `graphical.target` as the default target,
  and no screen lock, blanking, DPMS or idle sleep.
* no first-run/welcome window in the session — the thing `selftest.sh` asserts is absent.
* automatic apt (`apt-daily*` timers, `unattended-upgrades`, `20auto-upgrades`) and snap
  auto-refresh off; `/dev/uinput` available for the injection tools.
* `systemd-networkd-wait-online` disabled where the desktop brings NetworkManager (GNOME, Plasma,
  Xfce): NM manages the NIC via netplan and networkd's wait-online would otherwise hold
  `network-online.target` — and with it cloud-init and ssh — for its full 2-minute timeout on
  every boot. `resolute-sway` has no NetworkManager; there networkd stays in charge.
* the finished image's package list is dumped over the serial console and stored in
  `golden/<flavor>-packages.txt`, and committed as `vm/reference/<flavor>-packages.txt`
  for the flavors built on a released Ubuntu. The two 26.10 images are a
  development release whose package set moves under the flavor, so theirs stay in
  `golden/` only.

**GNOME** (`noble-gnome`, `resolute-gnome`)

* GDM: `/etc/gdm3/custom.conf` with `AutomaticLoginEnable=true`, `AutomaticLogin=test`,
  `WaylandEnable=true`; AccountsService pins the `ubuntu` (Wayland) session.
* a gschema override (`/usr/share/glib-2.0/schemas/90_vmctl.gschema.override`, compiled in) sets
  `org.gnome.desktop.screensaver lock-enabled=false`, `org.gnome.desktop.session idle-delay=0`,
  `org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type='nothing'`,
  `org.gnome.shell welcome-dialog-last-shown-version='999'`.
* no `gnome-initial-setup` dialogs for `test`: `~/.config/gnome-initial-setup-done` (first login)
  **and** `~/.config/gnome-initial-setup/upgrade-<release>-done` (the "Welcome to Ubuntu 26.04 LTS!"
  dialog of `gnome-initial-setup-upgrade-login.service`, whose unit is gated on the first marker
  existing and the second one *not* existing — so the first marker alone switches that dialog on).
  The `update-notifier` / `ubuntu-report` autostarts are hidden for `test`.

`stonking-gnome` exists for one reason too: **Ubuntu 26.10 is the first Ubuntu
carrying GNOME Shell 51**, and mutter 51 is where `libmutter_api_version` stopped
being a counter of its own and became the GNOME major, so the library is
`libmutter-51.so.0` with `Meta-51.typelib` beside it rather than the
`libmutter-19` the 46 → 14, 50 → 18 arithmetic would have produced. It is the
image `--unsafe-gnome-overlap`'s generation table, and
`--unsafe-gnome-overlap-unmeasured`, are measured against: the private
`MetaMonitorsConfig` layout, the guards passing, the guards made to fail, and
what forcing does on a build the table does not name
([docs/Technical.md § 6](../docs/Technical.md#the-table-and-adding-a-gnome-generation)).
Like `stonking-kde` it takes a smaller package set than the distro's full
metapackage, for the same reason: snapd does not work in the 26.10 cloud image, so
a deb whose postinst installs a snap blocks the build. `ubuntu-desktop-minimal`
plus a pin against the snap wrapper packages is GDM, `gnome-shell` and
`ubuntu-session`, which is the whole Wayland session.

**KDE Plasma** (`noble-kde`, `resolute-kde`, `stonking-kde`, `noble-kde-x11`,
`resolute-kde-x11`)

* SDDM: `/etc/sddm.conf.d/` autologin of `test` into the **Plasma Wayland** session —
  `plasmawayland.desktop` on 5.27, `plasma.desktop` on 6 (the build picks whichever
  `/usr/share/wayland-sessions/` file exists and fails if neither does).
* **`noble-kde-x11`** and **`resolute-kde-x11`** are the same images built with `DESKTOP=kde-x11`: the same packages, the
  same SDDM, autologin into the **X11** session from `/usr/share/xsessions/` instead
  (`plasma.desktop` on 5.27, `plasmax11.desktop` on 6), so Xorg owns the scanout and
  `kwin_x11` is a plain X window manager on top of it. The pair is a controlled experiment —
  one Plasma, one session bus, one `org.kde.KWin`, two session types. SDDM's autologin
  resolves the session *name* against `/usr/share/wayland-sessions/` **first**
  (`Display::attemptAutologin`), so a name that exists in both directories would quietly
  start the Wayland session: the build refuses to produce such an image rather than ship one
  that lies about its session type. On 26.04 the two names differ anyway
  (`plasma.desktop` is the Wayland one, `plasmax11.desktop` the X11 one), but
  `kubuntu-desktop` installs no X11 session at all there — `plasma-session-x11` and
  `kwin-x11` are in the archive, in universe, and nothing pulls them in — so
  `resolute-kde-x11` lists them explicitly and is, with `resolute-xfce-wayland`, one of the two
  Ubuntu flavors that is **not** a default install of its Ubuntu flavour. It is what a 26.04 user gets after
  `apt install plasma-session-x11`.
* `kwriteconfig5`/`kwriteconfig6` (whichever the release ships) turn off the screen locker
  (`kscreenlockerrc`) and display power management (`powerdevilrc`) for `test`.
* the welcome centre and the Discover update notifier are hidden as autostart entries — enough on
  Plasma 5.27. On **Plasma 6** the welcome centre is no autostart entry at all any more but a kded
  module (`kded_plasma_welcome.so`) that opens it whenever `plasma-welcomerc`'s `LastSeenVersion`
  is missing or older than the installed `plasma-welcome`, so the build also writes that key.
* Plasma reaches its first frame in several steps, so `vmctl session` waits for all of them:
  `kwin_wayland` owning head 0's scanout, `plasmashell` up, and `ksplashqml` gone.

`stonking-kde` exists for one reason: **Plasma 6.7 is the first release that
stopped advertising `kde_output_device_v2` as a wl_registry global** and hands
the device objects out through `kde_output_device_registry_v2` instead (kwin
commit `7e32e00c`, released in v6.7.0 and never backported — v6.6.6 still
publishes the globals). It is therefore the only image on which `wxrandr`'s
second output-discovery path runs at all — and on it, `wl_registry` carries no
`kde_output_device_v2` whatsoever, only `kde_output_device_registry_v2` v23,
`kde_output_management_v2` v21 and `kde_output_order_v1` v1. Ubuntu 26.10 (`stonking`) is the
first Ubuntu carrying it: `kwin-wayland 4:6.7.4-0ubuntu2`, against 6.6.4 in
26.04.

It is the one flavor that does **not** install its distro's desktop
metapackage, and 26.10 being a development release is why. `kubuntu-desktop`
pulls the `firefox` deb, whose postinst installs the firefox *snap* — and
snapd does not work in the 26.10 cloud image (`snapd.service` fails to start),
so that postinst parks in `Unable to contact the store, trying every minute
for the next 30 minutes` and the build never finishes. The flavor installs
`kde-plasma-desktop` instead — `kwin-wayland` + `plasma-workspace` +
`plasma-desktop`, i.e. the whole Plasma session and every display protocol
this image exists for — and pins `firefox`/`thunderbird` to −1 so no
Recommends chain can drag one back in. Everything else (SDDM autologin, the
screen-lock and power-management overrides) is the shared `kde` path in
`build-image.sh`, unchanged.

For the same reason — a development release whose cloud image
(`stonking-server-cloudimg-amd64.img`) and packages move under you — this
flavor is a probe for one protocol change, not a support target: the other
nine are what the desktop-support matrix is measured on.

**Xfce** (`noble-xfce`, `resolute-xfce`) — the X11 flavors

* LightDM autologin of `test` into the `xubuntu` (Xorg) session; `test` is added to the
  `autologin`/`nopasswdlogin` groups if the packages created them.
* **Display-manager collision.** On 26.04 `xubuntu-desktop` drags GDM in (through `gnome-shell`),
  and the first display manager to configure itself becomes *the* display manager — a golden built
  without care boots the GNOME greeter instead of Xfce. The build therefore answers debconf's
  question up front (`lightdm shared/default-x-display-manager select lightdm`), writes
  `/etc/X11/default-display-manager`, disables `gdm`/`gdm3`/`sddm`, enables `lightdm` and **fails
  the build** unless `display-manager.service` resolves to `lightdm.service`.
* xfconf channels written for `test` before the first login: no screensaver/blanking/DPMS
  (`xfce4-screensaver`, `xfce4-power-manager`, `xfce4-session`), and `displays.xml`
  `Notify=3` so `xfsettingsd` **extends** a hot-plugged output instead of popping the
  "new display detected" dialog (`Notify=1`, the default) — without it `vmctl head` would put a
  modal window on the screen and leave the new output disabled.
* `xfce4-screensaver`, `light-locker` and `update-notifier` autostarts hidden.

**sway** (`resolute-sway`)

* greetd `[initial_session]` starts `test`'s sway directly on vt 1 on the first boot (no greeter:
  the VM has no keyboard to type into one); `[default_session]` starts sway again, so a logout
  brings the desktop back. `getty@tty1` is disabled and the unit is ordered after it.
* the logind session greetd registers is `Type=tty` (greetd exports `XDG_SESSION_TYPE=tty`);
  sway's libseat then switches it to `wayland`. Both `vmctl session` and `vmctl user` accept
  either and always export `XDG_SESSION_TYPE=wayland`, which is what the session itself publishes.
  sway takes the DRM device through libseat's logind backend — no `seatd`.
* the config is the packaged default plus: `xwayland enable` (lazy Xwayland for the X-parity
  oracles), `workspace N output Virtual-N`, a `dbus-update-activation-environment` line that
  publishes `WAYLAND_DISPLAY`/`SWAYSOCK`/`DISPLAY`/`XDG_CURRENT_DESKTOP`/`XDG_SESSION_TYPE` to the
  systemd user manager (greetd starts sway with a bare environment, so without it nothing outside
  the session could find it), and `exec /usr/local/bin/vmctl-sway-layout`.
* **`vmctl-sway-layout`** runs once at session start and puts the active outputs side by side at
  `y=0` in connector order (`Virtual-1` at (0,0), `Virtual-2` to its right, ...). Stock
  sway/wlroots adds the initial outputs to the layout in reverse enumeration order, which on a
  3-head virtio-vga would put `Virtual-3` at (0,0) and `Virtual-1` at x=3840 — the rig's contract
  is head 0 = `Virtual-1` = (0,0). Nothing is watched afterwards: a test that moves outputs, or a
  head hot-plugged later (wlroots appends it on the right), is left alone.

**The default install** (`resolute-gnome-iso`)

The cloud-image flavors answer "does this work on GNOME 50 / Plasma 6 / Xfce /
sway?", and `noble-gnome-iso` asks this one's question again for 24.04 (below). This one
answers a different question — "does this work on **a default Ubuntu 26.04 desktop**, freshly
installed and updated?" — and it can only answer it by being one. Same ISO a person downloads,
same installer, same default install source, nothing added and, more importantly, **nothing
taken away**: the first-run experience, screen lock, blanking, DPMS, idle sleep, the
`apt-daily` timers, `unattended-upgrades` and snap auto-refresh are all left running, because
an image that has been tidied up cannot answer a question about an untidy one.

*How it is built* — `vm/build-iso-golden.sh resolute-gnome-iso`, 14 minutes on
2 vCPU/4 GB, no display, no keyboard, nothing to click:

1. **Install** (12.6 min). The ISO's own kernel and initrd are read straight out of the
   image (`isoinfo -R -x /casper/vmlinuz`, no remastering, no root) and booted with the ISO
   attached as a CD-ROM, the flavor yaml as a NoCloud seed drive, and `autoinstall` on the
   kernel command line. subiquity picks the description up from cloud-config — source 4 of the
   5 its `select_autoinstall()` looks at — and installs; the installer powers the VM off.
2. **Configure** (1.5 min). The installed disk is booted once and `vm/build-iso-image.sh`
   runs in it over ssh: update, GDM autologin, cloud-init re-enabled. Then power off, and the
   disk *is* the golden image — a plain qcow2 with no backing file, unlike the
   cloud-image ones.

**Why the kernel argument.** The config alone is not enough on the *desktop* installer. With
the seed and no `autoinstall` on the command line the live session comes up, the installer
reads the description, renders it — and stops on **"Ready to install / Review your choices"**,
waiting for a human to confirm, on a VM that has no keyboard. With the argument it runs
non-interactively (`interactive = False`, `/run/casper-no-prompt`) and never draws that page.
The argument also buys a serial console for the whole install, which the ISO's own `quiet
splash` entry does not.

*Every deviation from an untouched install, and why.* There are eight, four in the flavor
yaml and four in the stage-2 script, each marked `DEVIATION` in place:

| # | deviation | why |
|---|---|---|
| 1 | `refresh-installer: {update: false}` | reproducibility: the installer snap on the ISO is the one that runs, so the same ISO always installs the same way. The GUI would offer to update itself first. |
| 2 | **`openssh-server`** (`ssh: install-server: true`) | a default Ubuntu *desktop* install has no ssh server, and ssh is the rig's only way in — `vmctl ssh`, `user`, `session`, `scp` and all of `selftest.sh`. **This is the only package added to the default set** (with `openssh-sftp-server`, `ssh-import-id` and `ncurses-term`, which it pulls in, that is the whole difference from the ISO's own package manifest). |
| 3 | the rig's root ssh key in `ssh: authorized-keys:` | so stage 2 can log in without a password prompt. It lands in `/home/test/.ssh/authorized_keys`; from the next boot vmctl's per-instance seed installs it for `root` as on every flavor. |
| 4 | `shutdown: poweroff` | so the host can tell stage 1 finished. A human clicks *Restart now*; what is on disk is the same. |
| 5 | `apt-get full-upgrade` + `snap refresh` in stage 2 | "tested on a fresh, **updated** installation". The installer already applies security updates while it runs (its default); this is the rest, the way Software Updater would on day one. It upgrades what is installed and adds nothing by hand (on the day this image was built: 6 packages upgraded, 0 installed, 0 removed, and 2 snaps refreshed). |
| 6 | **GDM autologin of `test`** | the VM has no keyboard: nothing can type a password into the greeter (QEMU's D-Bus display is output only). Two keys added to the *shipped* `/etc/gdm3/custom.conf`, everything else in it untouched, and the file as the package wrote it is kept beside it as `custom.conf.stock`. |
| 7 | cloud-init re-enabled (`cloud-init clean`, plus `network: {config: disabled}`) | the installer switches cloud-init off in the target (`/etc/cloud/cloud.cfg.d/99-installer.cfg`, `/etc/cloud/cloud-init.disabled`); vmctl's per-instance seed — hostname, root key — needs it back. Undone by the installer's *own* golden-image path: `/etc/cloud/clean.d/99-installer` deletes exactly those two files and `cloud-init clean` runs it. The `network: disabled` line keeps cloud-init from taking the NIC off NetworkManager, i.e. keeps the network exactly as installed. No package is added: `cloud-init` is part of a default desktop install. |
| 8 | the first-run experience **completed**, not removed | a default install runs `gnome-initial-setup --existing-user` at the first login (measured: it did), and a VM with no keyboard cannot click through it, so every screenshot would have that window in it. The two markers a completed run leaves are written — the exact paths the two units' own `ConditionPathExists` lines name, read out of the units — and nothing else: the package, both units, `update-notifier`, `ubuntu-report` and `ubuntu-advantage-notification` all stay as installed. Writing only the first marker is not enough and the image proves it: it then came up running `gnome-initial-setup --upgrade-user` (the "Welcome to Ubuntu 26.04 LTS!" dialog) at every login instead. |

*What it does **not** do*, and this is the point — measured on the finished image:

* `apt-daily.timer`, `apt-daily-upgrade.timer`, `unattended-upgrades.service` and
  `motd-news.timer` are **enabled and active**, and snap auto-refresh is on (`timer:
  00:00~24:00/4`). The cloud-image flavors disable, mask or hold every one of them.
* screen lock `true`, screensaver idle activation `true`, `idle-delay` `300`,
  `sleep-inactive-ac-type` `'suspend'`: this desktop *will* blank, lock and suspend under a
  long test. On the cloud-image flavors those are `false`/`false`/`0`/`'nothing'` through a gschema
  override. A test that needs a desktop awake for ten minutes now has to say so itself —
  which is the honest place for it, because a user's machine will not have been told either.
* the first-run machinery is all still there: `gnome-initial-setup` installed with **both**
  its units enabled, and `update-notifier`, `ubuntu-report-on-upgrade` and
  `ubuntu-advantage-notification` autostarting (the cloud-image flavors hide all three with `Hidden=true`
  overrides). `update-notifier` really is running in the session here.
* the 13 snaps of a default install, `snap-store`, `snapd-desktop-integration` (running in
  the session), `firmware-updater`, `hwctl` and 26.04's `prompting-client` /
  `desktop-security-center` among them — AppArmor prompting, which is exactly what a tool
  that opens `/dev/uinput` and compositor sockets should be tested against. The others have
  8 snaps and none of those.
* `NetworkManager` owns the NIC through the installer's own
  `/etc/netplan/01-network-manager-all.yaml`, `systemd-networkd` disabled — the cloud-image flavors are
  the other way round (`50-cloud-init.yaml`, networkd enabled, `networkd-wait-online`
  disabled by hand to stop it holding up every boot).
* user `test` in the installer's groups (`adm cdrom sudo dip plugdev users lpadmin lxd`),
  sudo by password. (In a running *instance* vmctl's own per-instance seed then adds a
  `NOPASSWD` rule, as it does on every flavor — that is the rig, not the image.)

Everything else about the flavor is the rig as usual: `vmctl start`, `session`, `user`, `head`,
`heads`, `shot` and `vm/selftest.sh resolute-gnome-iso` work on it exactly as on the other
ten, and `vmctl build` refuses it with a pointer to `vm/build-iso-golden.sh`.

*How far a cloud-image flavor is from it.* `vm/reference/resolute-gnome-iso-packages.txt` against
`vm/reference/resolute-gnome-packages.txt` — same release, same desktop, built the two ways:

| | `resolute-gnome` | `resolute-gnome-iso` |
|---|---|---|
| built from | `ubuntu-26.04-server-cloudimg-amd64.img` + `ubuntu-desktop` | `ubuntu-26.04.1-desktop-amd64.iso`, installer, default source `ubuntu-desktop-minimal` |
| release / kernel | Ubuntu 26.04, `7.0.0-30-generic` (`linux-image-virtual`), **no firmware at all** | Ubuntu 26.04.1, `7.0.0-31-generic` (`linux-image-generic-hwe-26.04`), all 19 `linux-firmware-*` |
| packages | **1677** | **1506** |
| snaps | 8 (`bare core24 firefox gnome-46-2404 gtk-common-themes mesa-2404 snapd thunderbird`) | **13** — exactly the ISO's default source, byte for byte: the 8 minus `thunderbird`, plus `snap-store`, `snapd-desktop-integration`, `firmware-updater`, `hwctl`, `prompting-client`, `desktop-security-center` |
| session | GNOME Shell 50.1 / mutter 50.1 on Wayland, GDM 50.1, `Service=gdm-autologin`, logind `Type=wayland`, seat0/tty2 | **identical in every one of those** |
| X server | no Xorg (`xserver-xorg-core` absent); Xwayland 2:24.1.10-1 | the same — **but see below** |
| Xwayland at login | **not running**: mutter starts it on the first X client | **running from login**, `-enable-ei-portal`, with `gsd-xsettings`, `ibus-x11` and `mutter-x11-frames` alongside it |
| `/dev/uinput` | `crw------- root root`, module forced by `/etc/modules-load.d/uinput.conf` | `crw------- root root`, **no module line needed** (26.04 has it built in) |
| `/dev/input/event0` | `crw-rw---- root input`; `test` is not in `input` | identical |
| python3 / python3-gi | 3.14.3-0ubuntu2 (binary 3.14.4) / 3.56.2-1 | identical |
| GTK 3 / GTK 4 (and their typelibs) | 3.24.52 / 4.22.4 | identical |
| `libei1`, glib | 1.5.0-3, 2.88.0-1 | identical |
| X tools | `xdotool` 1:3.20160805.1, `wmctrl` 1.07, `x11-utils`, `x11-xserver-utils` | `x11-utils` and `x11-xserver-utils` only — **a default install has no `xdotool` and no `wmctrl`**, so the X11 hand-over path has nothing to hand over to until the user installs them, which is what the repo README tells them to do |
| pip / venv | absent | absent (so the install guide's `sudo apt install git python3-venv` is right — `git` and `curl` are not there either) |
| first run, lock, updates | all removed or switched off | all left on (list above) |

**226 packages the cloud-image flavor has that a default install does not**: 29 from the
server cloud image (`ubuntu-server`, `cloud-initramfs-*`, `overlayroot`, `landscape-common`,
`sos`, `open-iscsi`, `lxd-*`, `open-vm-tools`, `needrestart`, `multipath-tools`, `mdadm`,
`lvm2`, `cryptsetup`, `xfsprogs`, `btrfs-progs`, `dracut-network`, `zerofree`, `xorriso`, …),
3 for the virtual kernel, 41 because the *full* `ubuntu-desktop` metapackage is not the
default install source (24 `libreoffice-*`, `thunderbird`, `rhythmbox*`, `shotwell*`,
`remmina*`, `deja-dup`, `simple-scan`, `gnome-calendar`, `gnome-terminal`, `showtime`,
`usb-creator-*`), 2 on purpose (`xdotool`, `wmctrl`), and 151 more that are those four
groups' dependencies plus the cloud image's own toolbox (`curl`, `git`, `vim`, `htop`,
`tmux`, `restic`, `gawk`, `python3-boto3`/`twisted`, `grub-efi-*`, `shim-signed`).

**55 a default install has that the cloud-image flavor does not**: the whole `linux-firmware`
set (19 packages — the cloud image has none at all), the HWE kernel with headers and tools
(11), `amd64-microcode`, `intel-microcode`, `iucode-tool`, `thermald`, the English language
packs and input-method data (`language-pack-en*`, `language-pack-gnome-en*`, `m17n-db`,
`libm17n-0`, `libpinyin*`, `libchewing*`, `ibus-table-cangjie*`, `wbritish`), `grub-common`,
`firmware-sof-signed`.

**And against the ISO's own manifest** (`casper/minimal.en.manifest.full`, 1494 debs + 13
snaps — what the installer would put on disk), the golden is that set plus exactly:
`openssh-server`, `openssh-sftp-server`, `ssh-import-id`, `ncurses-term` (deviation 2 and
what it pulls), `wbritish` (the installer's own language-support step, not ours), and the
8 packages of the `7.0.0-30` → `7.0.0-31` kernel that the update brought in (deviation 5;
the `-30` files stay because nothing runs `autoremove`). The 13 snaps match the manifest
exactly. Nothing is missing from it.

*What a run on it actually found.* The repo README's install guide was followed on this image
verbatim — `sudo apt install git python3-venv`, `git clone`, `python3 -m venv
--system-site-packages`, `pip install -e .`, the five `/usr/local/bin` symlinks,
`warandr.desktop`, `sh gnome/install-bridge.sh`, one session restart, `sudo sh
gnome/install-bridge.sh --udev` — and then all five tools were exercised against a real
Ptyxis window on the three-head layout, as the desktop user and as root over ssh with `env
-i`. **Nothing had to be adapted.** Every stock fact the guide asserts holds here: no `pip`,
no `venv`, no `pipx`, no `git`, no `curl`; `python3-setuptools`, `python3-gi`,
`gir1.2-gtk-3.0`, `acl`, `x11-utils`, `x11-xserver-utils` present. `install-bridge.sh`
printed exactly the documented "log out and back in" text and exit 1; after the relogin
`--check` said `loaded in shell: yes` / `org.w11.Bridge owned: yes` / `uinput usable
by test: yes (logind ACL)`; `wdotool --version` … `warandr --version` printed the five
documented strings; `wxrandr --print-backend --verbose` said `mutter`; typing landed in the
terminal; the `warandr` GUI opened with `backend: mutter (Wayland)`, a monitor dragged in it
and applied with a click changed the real layout, `warandr --save` wrote an
arandr-compatible script, and that script bound to `<Ctrl><Super>F7` and pressed with
`wdotool key ctrl+super+F7` restored its layout.

The same script was then run on `resolute-gnome`. Out of 220 lines of output the two images
differ in **21**, and every one of them is the environment rather than a tool:

| what differs | `resolute-gnome-iso` | `resolute-gnome` |
|---|---|---|
| `command -v xdotool` | nothing — a default install has neither `xdotool` nor `wmctrl` | `/usr/bin/xdotool` |
| screen lock / idle / suspend | `lock-enabled true`, `idle-delay 300`, `sleep-inactive-ac-type 'suspend'` | `false`, `0`, `'nothing'` (gschema override) |
| `_NET_SUPPORTING_WM_CHECK` | `0x400001` | `0x200001` — Xwayland is already running at login here, so it hands out different ids |
| window ids, timestamps | differ per boot | differ per boot |

Everything else — the five version strings, the backend line, `wwmctl -l/-lx/-lG`,
`getdisplaygeometry` `5760 1080`, every `windowmove`/`windowsize`/`windowstate` result,
`getmouselocation`, all of `wxprop`, the dynamic-workspaces warning, all nine `wxrandr`
layout operations, `warandr --command`/`--save`, the hotkey, and the locked-screen messages
— is byte-identical. **A claim measured on `resolute-gnome` has, for these five tools, been
true of a default install.**

Two behaviours the run turned up. Both reproduce **identically on both images**, so neither is
about the default install; both are in tool code this branch deliberately does not touch, and
both want scheduling:

1. **`wwmctl -b remove,maximized_vert,maximized_horz` removes only the horizontal half, and
   corrupts the window's saved size.** Single-axis add and remove are both correct, and
   `-b add,maximized_vert,maximized_horz` correctly maximizes both. But removing both in one
   command leaves a window that was `200 150 900 600` at `200 32 900 1048` — full height —
   and from there `-b remove,maximized_vert`, `wdotool windowstate --remove MAXIMIZED_VERT`
   and even `--toggle MAXIMIZED_VERT` twice all do nothing: Mutter now reports the window
   unmaximized while its saved rectangle has kept the maximized height, so only an explicit
   `windowsize` gets it back. `wmctrl -b remove,maximized_vert,maximized_horz` is the
   documented way to unmaximize a window, so this is on a path people use. The two axes go
   out as two separate `SetState` calls (`extension.js` `setMaximized()` →
   `set_unmaximize_flags()`); the add path survives that and the remove path does not.
2. **`wdotool windowstate` honours only the last `--add`/`--remove`/`--toggle` on the line.**
   `--add MAXIMIZED_VERT --add MAXIMIZED_HORZ` maximizes horizontally only, and
   `--add MAXIMIZED_VERT --remove SHADED` attempts the `SHADED` remove alone. The cause is
   plain in `wdotool/window_cmds.py` `cmd_windowstate()`, whose option loop overwrites
   `action`/`prop` on every iteration. **Settled since, and it is parity, not a defect:**
   xdotool 4.20260303.1's own `cmd_windowstate.c` keeps one `action`/`arg_property` pair
   and overwrites both in every arm of its `getopt_long_only` switch, so the real tool drops
   the earlier options too. Nothing to fix; the loop now carries the reason, README's
   Compatibility section names the quirk beside `windowmove`'s percent-y one, and
   `tests/test_windows_cmds.py` pins it so nobody `fixes` it into a divergence.

**The second default install** (`noble-gnome-iso`)

The same question one release earlier: "does this work on **a default Ubuntu 24.04 LTS
desktop**, freshly installed and updated?", answered the only way it can be — by being one.
`ubuntu-24.04.4-desktop-amd64.iso`, the Ubuntu installer, every question left alone, nothing
taken away. The repo supports 24.04 and 26.04, so it needs a default install of each; this
is the 24.04 half, and `resolute-gnome-iso` above is the model it is built to.

*How it is built* — `vm/build-iso-golden.sh noble-gnome-iso`, **21 minutes** on 2 vCPU/4 GB
(stage 1 17.9 min, stage 2 3.7 min), no display, no keyboard, nothing to click. Same two
stages, the same **eight** deviations numbered the same way, and a flavor yaml that differs
from `resolute-gnome-iso`'s only in the ISO it names, that ISO's sha256, and the hostname.
The installer behaves the same: subiquity **revision 494** of `ubuntu-desktop-bootstrap`
(the same snap revision as on the 26.04 ISO), `autoinstall` on the kernel command line
(`boot=casper autoinstall console=ttyS0,115200`), the description picked up from cloud-config,
and — measured in `/var/log/installer/subiquity-server-debug.log` — `loaded 2 sources from
'/cdrom/casper/install-sources.yaml'`. Those two are `ubuntu-desktop-minimal` ("Ubuntu Desktop
(minimized)", `default: true`) and `ubuntu-desktop` (`default: false`): **the same two ids
with the same defaults as 26.04.1's**, so "the default install source" means the same thing
on both, and it is the minimal one.

Two things stage 2 had to learn for 24.04. Both are *reported* by the script, not worked
around:

* **`gnome-initial-setup` 46 has no `-upgrade-login` unit.** It ships only
  `gnome-initial-setup-first-login.service` and `-copy-worker.service`, and both are
  conditioned on the *same* `~/.config/gnome-initial-setup-done` marker. So deviation 8 —
  "complete the first run, do not remove it" — writes **one** marker here where 26.04 needed
  two, and the loop that reads the second unit's `ConditionPathExists` finds no such unit and
  writes nothing. The build log says so: `first-run: markers written
  (gnome-initial-setup-done )`. There is no "Welcome to Ubuntu 24.04 LTS!" dialog at the
  second login to guard against, because there is no unit that would draw one.
* **The installer leaves cloud-init off differently.** On both releases the target's *first*
  boot runs cloud-init once from `/etc/cloud/cloud.cfg.d/99-installer.cfg`
  (`datasource_list: [None]`) and that run writes `/etc/cloud/cloud-init.disabled` itself —
  so the file the installer is usually said to write is written by cloud-init, one boot
  later. What each release additionally leaves behind differs: 26.04 leaves
  `90-installer-network.cfg` (cloud-init networking disabled), **24.04 leaves the live
  session's own network configuration**, which that first run rendered into
  `/etc/netplan/50-cloud-init.yaml`. A default 24.04 desktop therefore has **two** netplan
  files — `01-network-manager-all.yaml` and `50-cloud-init.yaml` — and a second entry in
  `/etc/cloud/clean.d/`, `99-installer-use-networkmanager`. Deviation 7 undoes it exactly as
  on 26.04, with the installer's own `clean.d` scripts and `cloud-init clean`; afterwards
  NetworkManager still owns `ens3` (`netplan-ens3`), `systemd-networkd` is disabled, and
  `cloud.cfg.d` holds only `05_logging.cfg`, `90_dpkg.cfg`, `curtin-preserve-sources.cfg`,
  `README` and vmctl's own `99-vmctl-no-network.cfg`.

*What the finished image is* — measured on it, not assumed:

| | `noble-gnome-iso` |
|---|---|
| release / media | Ubuntu **24.04.4** LTS, `Ubuntu 24.04.4 LTS "Noble Numbat" - Release amd64 (20260210)` |
| kernel | **`7.0.0-31-generic`** (`linux-image-generic-hwe-24.04`), all **19** `linux-firmware-*`, `amd64-microcode`, `intel-microcode`, `thermald` |
| packages / snaps | **1510** debs, **12** snaps (`bare core22 core24 firefox firmware-updater gnome-42-2204 gnome-46-2404 gtk-common-themes mesa-2404 snap-store snapd snapd-desktop-integration`) |
| session | **GNOME Shell 46.0** / GDM 46.2 on Wayland, `Service=gdm-autologin`, logind `Type=wayland`, seat0/tty2, `XDG_CURRENT_DESKTOP=ubuntu:GNOME` |
| sessions offered | `wayland-sessions`: `ubuntu.desktop`, `ubuntu-wayland.desktop`; **`xsessions`: `ubuntu.desktop`, `ubuntu-xorg.desktop`** — a default 24.04 install still ships a GNOME **Xorg** session (and `xserver-xorg-core`, `xinit`, `x11-apps`, `xinput`); 26.04's does not |
| Python / GTK | python3 **3.12.3**-0ubuntu2.1 (binary 3.12.3), `python3-gi` 3.48.2-1, GTK 3 **3.24.41**, GTK 4 **4.14.5**, glib 2.80.0, `libei1` 1.2.1 |
| `python3-setuptools` | **absent** — see *the install guide* below; 26.04's default install has it (78.1.1) |
| X tools | `x11-utils`, `x11-xserver-utils` present; **no `xdotool`, no `wmctrl`**, as on 26.04 |
| pip / venv / pipx / git / curl | all absent, as on 26.04 |
| Xwayland | 2:23.2.6, **not running at login** — mutter starts it on the first X client, and `gsd-xsettings`, `ibus-x11` and `mutter-x11-frames` come up with it. No `-enable-ei-portal` on its command line (26.04's 24.1.10 has it) |
| `/dev/uinput` | `crw------- root root`, `CONFIG_INPUT_UINPUT=y` — **built in, no `modules-load.d` line needed**, same as 26.04 |
| shell extensions | 4 enabled: `ding`, `tiling-assistant`, `ubuntu-appindicators`, `ubuntu-dock` (26.04 has 7 — it adds `snapd-prompting`, `snapd-search-provider`, `web-search-provider`) |
| user `test` | installer groups `adm cdrom sudo dip plugdev users lpadmin` — **no `lxd`** (26.04's default install adds it); sudo by password |
| left running | `apt-daily.timer`, `apt-daily-upgrade.timer`, `unattended-upgrades.service`, `motd-news.timer` all enabled **and active**; snap auto-refresh `timer: 00:00~24:00/4`; `update-notifier` running in the session |
| lock / idle / suspend | `lock-enabled` **true**, `idle-activation-enabled` **true**, `lock-delay` 0, `idle-delay` **300**, `sleep-inactive-ac-type` **`'suspend'`** — identical to 26.04's defaults, and it bites (below) |

*How far the cloud-image flavor is from it.* `vm/reference/noble-gnome-iso-packages.txt`
against `vm/reference/noble-gnome-packages.txt` — same release, same desktop, built the two
ways: **1695** packages against **1510**. **240** the cloud-image flavor has that a default
install does not (the server cloud image's own set — `ubuntu-server`, `cloud-initramfs-*`,
`overlayroot`, `landscape-common`, `open-iscsi`, `lxd-*`, `open-vm-tools`, `needrestart`,
`multipath-tools`, `mdadm`, `lvm2`, `cryptsetup`, `btrfs-progs`, `curl`, `git`, `htop`,
`gawk`; the `linux-image-virtual` 6.8.0-138 kernel; the 24 `libreoffice-*`, `rhythmbox*`,
`deja-dup`, `gnome-calendar`, `file-roller`, `gnome-snapshot` and friends that the *full*
`ubuntu-desktop` metapackage brings and the default source does not; and `xdotool`, `wmctrl`
on purpose). **55** a default install has that it does not: the whole `linux-firmware` set
(19 packages), the `7.0.0-31` HWE kernel with headers and tools (11), `amd64-microcode`,
`intel-microcode`, `iucode-tool`, `thermald`, the English language packs and input-method
data (`language-pack-en*`, `language-pack-gnome-en*`, `m17n-db`, `libm17n-0`, `libpinyin*`,
`libchewing*`, `ibus-table-cangjie*`, `wbritish`) and `firmware-sof-signed`. The shape of the
difference is the same as 26.04's; only the names move.

*And against the ISO's own manifest.* `casper/minimal.manifest` minus what the `en` layer
removes (43 non-English language packs and input methods) is **1448** debs and **9** snaps.
The golden is that set **plus 63, minus 1**. Of the 63, **four are ours** — `openssh-server`,
`openssh-sftp-server`, `ssh-import-id`, `ncurses-term` (deviation 2 and what it pulls) — one
is the installer's own language-support step (`wbritish`), and the other **58 are what the
installer puts on disk that the live layer never carries**: the HWE kernel `7.0.0-31` with
its headers, tools and `ubuntu-kernel-accessories` (`bpfcc-tools`, `bpftrace`, `libc6-dev`,
`manpages-dev`, …), the 19 `linux-firmware-*`, both microcode packages, `thermald`, `hwdata`,
`grub-pc`/`grub2-common` for the target's boot mode, and `libclang`/`libllvm` for Mesa. The
one subtraction is `libwoff1`, dropped by the day-one update (deviation 5: 86 upgraded, 1
newly installed, 0 removed; 5 snaps refreshed, which is where `core24`, `gnome-46-2404` and
`mesa-2404` join the manifest's 9). Nothing else is missing from it.

*What a default 24.04 install has and lacks against a default 26.04 one.*
`noble-gnome-iso-packages.txt` against `resolute-gnome-iso-packages.txt` — 1510 against 1506,
12 snaps against 13, and the two sets differ by **186 / 182**. What matters for these six
tools:

| | `noble-gnome-iso` (24.04) | `resolute-gnome-iso` (26.04) |
|---|---|---|
| shell / mutter | GNOME Shell 46.0, `libmutter-14-0` | GNOME Shell 50.1, `libmutter-18-0` |
| python3 | 3.12.3 | 3.14.4 |
| terminal in the default source | **`gnome-terminal`** | **`ptyxis`** (24.04's default source has no ptyxis; 26.04's has no gnome-terminal) |
| viewers | `evince`, `eog` | `papers`, `loupe` |
| Xorg | **`xorg`, `xserver-xorg-core`, `xinit`, `x11-apps`, `xinput`, `xserver-xephyr` and a GNOME Xorg session** | none of them — 26.04's default install is Wayland-only |
| `python3-setuptools` | **absent** | present (78.1.1) |
| `sudo` | `sudo` 1.9.15p5 | `sudo-rs` |
| snaps | 12 | 13 — 24.04 has no `prompting-client`, no `desktop-security-center`, no `hwctl` (**so a default 24.04 desktop has no AppArmor prompting to test against**), and carries `core22` + `gnome-42-2204` for Firefox alongside `core24` + `gnome-46-2404` |
| shell extensions enabled | 4 | 7 |
| netplan | two files (`01-network-manager-all.yaml`, `50-cloud-init.yaml`) | one |

*What a run on it actually found.* The repo README's install guide was followed on this image
**verbatim**, as a reader would, and then all **six** tools were exercised against a real
`gnome-terminal` window on the three-head layout, with a screenshot of each looked at. The
same script was then run on the cloud-image `noble-gnome`.

This run was made on the 0.2 tree, so the two numbers below that belong to a release rather
than to the image are 0.2's; both are re-measured on the shipping tree under *The package on a
default install*, further down. Everything the guide tells you to type worked and produced what
it says it produces: `sudo apt install git python3-venv` (7 new packages, `python3.12-venv` among them), `git clone`,
`python3 -m venv --system-site-packages`, `pip install -e .` (`Successfully installed
w11-0.2.0`), the six `/usr/local/bin` symlinks, `warandr.desktop`, `sh
gnome/install-bridge.sh` printing exactly the documented "log out and back in" text and exit
1, one session restart, `sudo sh gnome/install-bridge.sh --udev` → `uinput usable by test:
yes (logind ACL)`. After the relogin `--check` said `loaded in shell: yes (state 1)` /
`org.w11.Bridge owned: yes` / `bridge version: 2` (the maximize pair has made it 3
since); the extension's own `metadata.json` lists shell versions `45`–`50`, so **46 is inside its declared range and 24.04
needs no change to it**. The six version strings printed exactly what *Check it worked* said
then, `wxrandr --print-backend --verbose` said `mutter`, typing landed in the terminal, the
`warandr` GUI opened with `backend: mutter (Wayland)`, a monitor dragged in it and applied
with a click moved the real one (`Virtual-3` from `1920,1080` to `0,1080`), `warandr --save`
wrote an arandr-compatible script, and that script bound to `<Ctrl><Super>F7` and pressed
with `wdotool key ctrl+super+F7` restored its layout. `wmirror --check` correctly refuses the
session — `capture: this compositor advertises neither zwlr_screencopy_manager_v1 nor
ext_image_copy_capture_manager_v1`, `outputs: this compositor does not advertise
zwlr_output_manager_v1`, exit 1 — which is [(k)](../README.md#desktop-support) working as
documented, on GNOME 46 as on GNOME 50.

**Three things had to be adapted, and all three are the guide's, not the tools'** (all three
are fixed in `README.md` on this branch):

1. **`pip install --no-build-isolation -e .` does not work on 24.04, on either image.** The
   guide offers it as the offline-ish shortcut "on a desktop image, which ships
   `python3-setuptools`". A default 24.04 desktop **has no `python3-setuptools`**, so it ends
   `ModuleNotFoundError: No module named 'setuptools'` → `BackendUnavailable`, exit 2. On
   `noble-gnome`, which *does* have it (68.1.2), it gets one step further and ends `error:
   invalid command 'bdist_wheel'` → `metadata-generation-failed`, exit 1, because setuptools
   68 still needs the separate `wheel` package. Plain `pip install -e .` — what the guide
   actually tells you to run — works on both.
2. **`python3 -m venv` run *bare* does not name the versioned package.** With `python3-venv`
   removed, `python3 -m venv` prints argparse's `venv: error: the following arguments are
   required: ENV_DIR` and nothing else; the `apt install python3.12-venv` line appears only
   when a directory is given. The claim was right about the package name, wrong about how to
   see it.
3. **The `wxrandr --print-backend --verbose` block in *Check it worked* is missing its last
   line**, `available: yes`. Present on `noble-gnome-iso`, `noble-gnome` and `resolute-gnome-iso`
   alike, so the block has been short a line since it was written.

And one thing a **user must accept**, which the 26.04 run recorded and this one walked
straight into: **a default install locks itself in the middle of the install guide.** `sudo
apt install git python3-venv` took 3 min 27 s on this mirror; with the rest of the clone and
venv steps that is past `idle-delay 300`, and the very next command, `wwmctl -l`, answered
`gnome backend: the w11 bridge is unavailable while the screen is locked (GNOME Shell
disables extensions behind the lock screen); unlock the session`, rc 1. The host screenshot at
that moment is not a lock screen but QEMU's `Display output is not active.` — a default
desktop switches the outputs off as well, so an unattended screenshot after five idle minutes
is black on every head. Injection is unaffected and is the way out: `wdotool key Escape`,
`wdotool type test`, `wdotool key Return` unlocked the session from the host with no keyboard,
`LockedHint=no`, and the desktop came back. `wxrandr`, `warandr` and every `wdotool` input
command keep working throughout.

*The same script on `noble-gnome`.* 251 lines of output, **19 differ**, and every one is the
environment rather than a tool:

| what differs | `noble-gnome-iso` | `noble-gnome` |
|---|---|---|
| `command -v xdotool` | nothing — a default install has neither `xdotool` nor `wmctrl` | `/usr/bin/xdotool` |
| screen lock / idle / suspend | `lock-enabled true`, `idle-delay 300`, `sleep-inactive-ac-type 'suspend'` | `false`, `0`, `'nothing'` (gschema override) |
| window ids, the `/dev/uinput` timestamp | differ per boot | differ per boot |

Everything else — the six version strings, the backend line, `wwmctl -l/-lx/-lG`,
`getdisplaygeometry` `5760 1080`, every `windowmove`/`windowsize`/`windowstate` result, both
maximize misbehaviours below, `getmouselocation`, all of `wxprop`, the dynamic-workspaces
warning, all nine `wxrandr` layout operations, `warandr --command`/`--save`, the hotkey, all
four `wmirror` answers, and the locked-screen messages — is **byte-identical**. And against
the 26.04 pair, the only tool-output line that differs at all is
`_NET_SUPPORTING_WM_CHECK`: `0x0` on **both** 24.04 images, `0x400001`/`0x200001` on the
26.04 ones. That is Xwayland's lifetime, not a tool: 24.04 starts no Xwayland at login, and
`wxprop` says `0x0` because there is no X server to own a supporting-WM window. Start one
(`xdpyinfo`) and `wxprop -root _NET_SUPPORTING_WM_CHECK` and the real `xprop -root
_NET_SUPPORTING_WM_CHECK` both answer `0x600001`, character for character.

### Tool defects these runs found

Numbering continues the two the 26.04 run found, above. All three are in tool code this
infrastructure branch deliberately does not touch, and all three want scheduling.

1. **`wwmctl -b remove,maximized_vert,maximized_horz` removes only the horizontal half and
   corrupts the saved size** — *reproduces on `noble-gnome-iso` and `noble-gnome` exactly as
   written above for 26.04*, on GNOME 46 as on GNOME 50. A window at `200 150 900 600`
   maximized both ways and then un-maximized both ways in one command lands at `200 32 894
   1039`, full height; `-b remove,maximized_vert`, `wdotool windowstate --remove
   MAXIMIZED_VERT` and `--toggle MAXIMIZED_VERT` twice then all change nothing, and only an
   explicit `windowsize` gets the height back. Four images, two GNOME releases, same
   behaviour: it is `extension.js`'s `setMaximized()` sending the two axes as two `SetState`
   calls, not anything about the desktop.
2. **`wdotool windowstate` honours only the last `--add`/`--remove`/`--toggle` on the line** —
   also identical here: `--add MAXIMIZED_VERT --add MAXIMIZED_HORZ` gives `66 150 1854 600`,
   horizontal only. Settled against the 4.20260303.1 source rather than a binary, and the
   answer is that this is faithful: upstream's `cmd_windowstate.c` overwrites its single
   `action`/`arg_property` pair in every getopt arm, so it keeps the last option too. See
   the resolution under the 26.04 run above.
3. **The locked-screen message hides the "extension not installed" one, on the exact path a
   first-time reader takes.** New here, and only a default install can show it. On
   `noble-gnome-iso`, with the extension not yet installed *and* the screen locked after the
   guide's own `apt install`, `wwmctl -l` answered

   ```
   gnome backend: the w11 bridge is unavailable while the screen is locked
   (GNOME Shell disables extensions behind the lock screen); unlock the session
   ```

   where `noble-gnome`, unlocked, answered the message the guide's troubleshooting table
   sends you to act on:

   ```
   gnome backend: the w11 bridge extension is not running in GNOME Shell;
   run gnome/install-bridge.sh and restart the session (log out and back in)
   ```

   Both statements were true; the one printed is the one the reader can do least with,
   because unlocking will not make the command work. The backend cannot tell the two apart
   *from the bus* — behind the lock screen the shell disables every extension, ours included
   — but it can tell them apart from disk, which is what `install-bridge.sh --check`
   does with its `files:` line: the extension directory under
   `~/.local/share/gnome-shell/extensions`, or the system one.

   **Fixed** (`backend_gnome.extension_installed()`, the same two places, no subprocess and
   no gsettings): with no copy on disk the actionable sentence leads and the lock follows it
   as a clause, so both are still said — `gnome backend: the w11 bridge extension is
   not running in GNOME Shell; run gnome/install-bridge.sh and restart the session (log out
   and back in); the screen is locked as well, and GNOME Shell disables extensions behind the
   lock screen, so unlocking alone will not be enough`. With a copy on disk the lock message
   is unchanged, and the GDM greeter still gets its own.

### The package on a default install

The runs above installed the tools the way a developer does, from a clone with pip. The way a
reader does is the `.deb`, and that was measured separately, on a fresh `resolute-gnome-iso`
instance with two heads at 1280x800. Baseline: no tool on `PATH`, `/dev/uinput`
`crw------- root root`, no extension, no rule.

`apt-get install -y ./w11_0.4.0_all.deb` → **rc 0**, and the package's own note about
the one logout, the udev rule and the X11 originals. Straight away, with nothing else typed:
`/dev/uinput` is `crw-rw----+` with `user:test:rw-` in its ACL and no reboot, and the six tools
answer exactly what the README's *Check it worked* block says, `Server reports RandR version
1.6` included. Before the relogin the window commands say the bridge is not running, which is
what the note promises.

One logout, taken as a reboot. `gnome-extensions info w11-bridge@w11` then says
**Version: 3, Enabled: Yes, State: ACTIVE**, enabled by the package with nothing typed, and the
bridge's own `GetVersion` over the session bus answers `(uint32 3,)`. `wxrandr --print-backend
--verbose` prints the six lines of the README block verbatim (`mutter` / `session: wayland` /
`chosen by: detection` / `compositor: Mutter` / `protocol: org.gnome.Mutter.DisplayConfig
(D-Bus)` / `available: yes`); with a `gnome-text-editor` window open, `wwmctl -l -G -p -x` lists
it with geometry, pid and class, and `wwmctl -m` ends `Window manager's "showing the desktop"
mode: OFF` (with no window open at all that last line is `N/A`, which is the honest answer and
not a defect).

`apt-get remove -y w11` → rc 0: `wdotool` is gone from `PATH`, `/dev/uinput` is back to
`crw------- root root` with no ACL, and the session in progress is still active.

Adding a flavor: copy a yaml, change `hostname`, `# vmctl-base:`, `# vmctl-desktop:` and
`/etc/vmctl-build.env` (`DESKTOP`, `DESKTOP_PKG`, `EXTRA_PKGS`). A new *desktop* additionally
needs a branch in `build-image.sh`, an entry in `vmctl`'s `DESKTOPS` table (session kind, logind
types, display-manager unit, compositor/shell/splash process names) and a case in `selftest.sh`'s
`monitors`/`layout`. The build boots the overlay with `-display none`; cloud-init's `power_state`
powers it off at the end and `vmctl build` succeeds only if the guest printed `VMCTL-BUILD-OK`.

## Self-test

`vm/selftest.sh <flavor> [name]` (default name `<flavor>-t`, screenshots in `$OUT`, default
`/tmp/vmctl-selftest-<name>`) boots a fresh 3-head instance of **any** flavor and asserts, in
order, using that desktop's own tools:

1. `vmctl session` reaches an active session of the expected kind and the desktop has painted.
2. the native display tool lists exactly `Virtual-1`, `-2`, `-3`, with **`Virtual-1` at (0,0)** —
   and *primary* where the desktop has the notion: GNOME's logical-monitor `primary` flag and
   Plasma's `priority 1` are required; sway has no primary, so its focused output stands in, and
   Xorg only marks one if something asked it to.
3. no first-run window in the session: `gnome-initial-setup`, `plasma-welcome`, or a window whose
   title looks like Xfce's "new display" dialog. On both ISO flavors this is the one assertion that needed a deviation
   to hold (number 8): a default install *does* run `gnome-initial-setup` at the first login,
   and with the first-login marker alone it then runs it again as `--upgrade-user` at every
   login after that. Both were seen on `resolute-gnome-iso` before the markers were written;
   on `noble-gnome-iso` only the first, because GNOME 46 ships no `-upgrade-login` unit.
4. `vmctl user` gives a working session environment: `XDG_SESSION_ID` whose logind `Type` is the
   flavor's (`wayland`, `x11`, or `wayland`/`tty` for sway), `XDG_SESSION_TYPE` exactly `wayland`
   or `x11`, the display sockets (`$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY`, `$SWAYSOCK`) and an X
   display `xdpyinfo` can reach — Xwayland on the Wayland flavors, Xorg on Xfce and
   `noble-kde-x11` (where `$WAYLAND_DISPLAY` must be **empty**: a Plasma X11 session has no
   compositor socket of its own, and that is the whole point of the flavor).
5. `vmctl heads` sees the DRM connectors, and `vmctl shot --all` yields one PNG per head whose
   pixel standard deviation is > 0.01 (a session that never finished starting paints every head a
   flat `#222222`). A head that is still flat is re-shot every 4 s, up to six times, before it
   counts as a failure — `vmctl session` already waits for a picture on every head, so this is
   only the safety net under it.
6. **head 0 differs from head 1** — the proof that the screendumps caught the desktop and not the
   kernel console, which fbdev mirrors onto every head. This one is flavor-aware: GNOME (top bar
   and dock), Plasma (panel on the primary output) and Xfce (`xfce4-panel` on the first monitor)
   must differ, so identical heads fail the test; sway draws the same `swaybar` on every output
   and legitimately may not differ, so there it only warns — and the per-output workspace pinning
   (`Virtual-N` shows workspace `N`) is asserted through `swaymsg` instead.
7. `vmctl head 3 1280x1024` makes the native tool report a 4th monitor and `Virtual-4` appear in
   `vmctl heads`; `vmctl head 3 off` takes it away again (up to 60 s — on X11 the removal is only
   complete once the X server has let go of the output, which `vmctl head` sees to; a failure
   prints the DRM connectors and, on Xfce, `xrandr`'s view of `Virtual-*`).

Roughly 40 s for GNOME; a desktop that starts more slowly takes correspondingly longer. The VM
is left running so a failure can be inspected — `vmctl stop <flavor>-t` when done.

## What the tools do on each flavor

The point of the extra flavors is to see where `wxrandr`, `wwmctl`, `wdotool`, `wxprop`
and `warandr` stand outside GNOME. `wmirror` has no column here because it has no
per-flavor answer to give: it is wlroots only by construction, `--check` says so on
everything else, and *Where it does not exist* in
[docs/WMIRROR.md](../docs/WMIRROR.md#where-it-does-not-exist) is the whole of it.

This is the measured state, honest gaps included: the
branch's own checkout copied into each guest and run as `python3 -m <tool>` **inside the
session** (`vmctl user`), plus a second round as root over plain `vmctl ssh` with an empty
environment (`env -i`). Every message below is verbatim. None of it is a rig defect —
`selftest.sh` passes on every flavor whose golden has been built. On the **X11 flavors** what is
measured is the
passthrough: on a plain X11 session the tools hand over to the real `xdotool`/`wmctrl`/`xprop`/
`xrandr` (repo README, *X11*), all four of which the goldens carry, so there they behave as the
originals do.

**This table and the repo README's *Desktop support* matrix are one measurement.** That one
is grouped by desktop and is what a user reads; this one is grouped by flavor and names the
image. Neither may claim what the other denies: change them together.

**What the last whole-rig run printed**, per flavor: CI run **34340513060** (commit `a544dec`,
the guest installing `release/w11_0.4.0_all.deb` built from that tree, three heads).
Every tally is the run's own `done:` line, and the XFAIL column is the `xwant` lines that
stayed expected-failures:

| flavor | checks | XFAIL lines | wall |
|---|---|---|---|
| `noble-gnome`, `noble-gnome-iso`, `resolute-gnome`, `resolute-gnome-iso`, `stonking-gnome` | 90 pass, 0 fail | 1 | 396–587 s |
| `resolute-hypr` | 77 pass, 0 fail | 3 | 327 s |
| `resolute-budgie`, `resolute-xfce-wayland` | 74 pass, 0 fail | 1 | 159–195 s |
| `resolute-labwc`, `resolute-lxqt-wayland` | 70 pass, 0 fail | 1 | 175–176 s |
| `resolute-wayfire` | 67 pass, 0 fail | 0 | 158 s |
| `resolute-cinnamon-wayland` | 65 pass, 0 fail | 7 | 459 s |
| `resolute-kde`, `stonking-kde` | 60 pass, 0 fail | 0 | 190–198 s |
| `noble-kde` | 59 pass, 0 fail | 0 | 274 s |
| `noble-gnome-x11` | 51 pass, 0 fail | 2 | 242 s |
| `resolute-sway` | 38 pass, 0 fail | 0 | 137 s |
| `resolute-i3` | 37 pass, 0 fail | 1 | 96 s |
| `resolute-mate` | 33 pass, 0 fail | 1 | 164 s |
| `resolute-cinnamon` | 32 pass, 0 fail | 1 | 106 s |
| `resolute-lxqt` | 24 pass, 0 fail | 1 | 98 s |
| `noble-kde-x11`, `noble-xfce`, `resolute-kde-x11`, `resolute-xfce` | 19 pass, 0 fail | 1 | 69–116 s |
| `arch-hypr`, `arch-sway`, `fedora44-gnome`, `fedora44-sway`, `nixos-sway` | no smoke ran | — | — |

The five that ran nothing are the five non-Ubuntu push flavors: `scripts/ci-golden.sh` has no
fetch rule for a Fedora, Arch or NixOS golden yet, so every one of those jobs ended before the
smoke started. That is the one thing this table is waiting on.

**Three flavors were also measured by hand on this host on 2026-09-09**, over the working tree
rather than the released package, and those numbers are here because two of the fixes they
prove landed after the CI run above. Each flavor gets two rows — the whole run, then the
`--reuse` rerun of the phases whose fix landed in between — because a run that stops failing
goes on to ask the checks behind the failure, and a `--reuse` run skips `install` and every
phase it is not given, so the two tallies are not summable and neither is a sum:

| flavor | run | CI run 34308982263 | 2026-09-09, this tree |
|---|---|---|---|
| `resolute-labwc` | whole run, all phases | 61 pass, 9 fail | 59 pass, 3 fail, 1 XFAIL |
| `resolute-labwc` | `--reuse --phases windows,wm,input` after the root-menu Escape | — | 31 pass, 0 fail, 1 XFAIL |
| `resolute-budgie` | whole run, all phases | 35 pass, 10 fail | 33 pass, 4 fail |
| `resolute-budgie` | `--reuse` of every phase after the detection fix | — | 56 pass, 0 fail |
| `resolute-lxqt-wayland` | whole run, all phases | 61 pass, 7 fail | 59 pass, 1 fail, 1 XFAIL |
| `resolute-lxqt-wayland` | `--reuse` of every phase after the workspace patch | — | 57 pass, 0 fail, 1 XFAIL |

The three FAILs left in the labwc whole run are the root-menu grab, fixed in the step file
afterwards and proved by the rerun under it; the four in the Budgie whole run are the detection
arm plus the layout-restore XPASS, likewise. Neither whole run was repeated after its fix — one
VM at a time on this host — so no row claims a whole-run tally nobody printed.

| flavor | `wxrandr` | `wwmctl -m` | `wwmctl -l` | `wdotool` | `wxprop -root` | `warandr` |
|---|---|---|---|---|---|---|
| `noble-gnome`, `resolute-gnome` | works | works | needs the bridge extension | `getdisplaygeometry` works; window/input commands need the bridge extension | works | works |
| `noble-kde`, `resolute-kde` | works | works | works | works | works | works |
| `noble-kde-x11`, `resolute-kde-x11` | works (hands over to `xrandr`) | works (`wmctrl`) | works (`wmctrl`) | works (`xdotool`) | works (`xprop`) | works (runs the real `xrandr`) |
| `noble-xfce`, `resolute-xfce` | works (hands over to `xrandr`) | works (`wmctrl`) | works (`wmctrl`) | works (`xdotool` — but see the version note below) | works (`xprop`) | works (runs the real `xrandr`) |
| `resolute-sway` | works | works | works | works, bar `windowstate MAXIMIZED_*` and moving, resizing, raising or lowering a *tiled* window | works in the session; **synthesized** from a root shell | works, once the image has the GTK 3 bindings |
| `resolute-hypr`, `arch-hypr` | works (`hypr`, not `wlr`) | works | works, X and native windows under their real ids | works, bar `windowminimize` and `windowlower` | works | works |
| `resolute-wayfire` | works (`wlr`) | works (`wlroots wm`) | works | works, and nothing here is skipped for tiling | works | works, once the image has the GTK 3 bindings |
| `resolute-labwc`, `resolute-xfce-wayland`, `resolute-budgie`, `resolute-lxqt-wayland` | works (`wlr`) | works (`wlroots wm`) | works, X and native windows | the wlr floor: no move, resize, raise or lower; desktops over `ext_workspace_manager_v1` | works | works |
| `arch-river` | works (`wlr`) | works (`wlroots wm`) | works | every mutating window command is accepted by river 0.4 and changes nothing, and the tool says so | works | works |
| `fedora44-cosmic`, `arch-cosmic` | works (`wlr`, named `COSMIC (wlr-output-management)`) | works (`Smithay X WM`) | works, two workspaces | activate, close and the state pair; no move, resize, raise, lower or pid | works | works |
| `resolute-cinnamon-wayland` | works (`cinnamon`) | works (`Mutter (Muffin)`) | works | works, bar the states muffin has no setter for; input is `/dev/uinput` only | works | works |
| `noble-gnome-x11`, `resolute-cinnamon`, `resolute-mate`, `resolute-i3`, `resolute-lxqt` | works (hands over to `xrandr`) | works (`wmctrl`) | works (`wmctrl`) | works (`xdotool` — but see the version note below) | works (`xprop`) | works (runs the real `xrandr`) |

Install the bridge extension on the GNOME flavors (`gnome/install-bridge.sh`, then log the
session out and in) and both of them pass every cell of this table, on 46 and on 50 —
`wwmctl -l/-d/-m`, every `wdotool` window command and `wxprop -id` on native windows, as
`test` and as root. `sudo gnome/install-bridge.sh --udev` is what lets the desktop user
open `/dev/uinput`; without it every injection command has to run as root, on GNOME, Plasma and
Cinnamon alike. On the GNOME flavors the logout is only for a shell that has never scanned the
extension directory: measured on `noble-gnome-x11` (GNOME Shell 46.0, 2026-09-09),
`gnome-extensions disable` releases `org.w11.Bridge` about 3 s later and `enable` takes
it back about 4 s later with the shell's pid unchanged, which is what the install autostart uses.
A bridge whose `extension.js` has CHANGED still needs one, and that is **not yet**: see the
Reload note in [docs/WDOTOOL.md](../docs/WDOTOOL.md).

**GNOME** — `wxrandr` prints the real listing through mutter's DisplayConfig
(`Screen 0: minimum 16 x 16, current 5760 x 1080, maximum 32767 x 32767`,
`Virtual-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 480mm x 270mm`),
`wwmctl -m` prints `Name: GNOME Shell` … `Window manager's "showing the desktop" mode: OFF`,
`wdotool getdisplaygeometry` prints `5760 1080`, and `wxprop -root _NET_SUPPORTING_WM_CHECK`
prints `_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x400001` (Xwayland's root; the id varies).
Everything that needs the window list — `wwmctl -l`, `wdotool getactivewindow` and the rest —
exits non-zero with

> `gnome backend: the w11 bridge extension is not running in GNOME Shell; run gnome/install-bridge.sh and restart the session (log out and back in)`

The goldens are stock desktops and deliberately do not carry the extension: install it in the
guest (`gnome/install-bridge.sh`) and log the session out and in to exercise those paths.
Same results as root over `vmctl ssh` — the GNOME backends need no session environment.

**KDE Plasma** — all four work, in the session and as root. `wxrandr` prints the real listing
through KWin's `kde_output_management_v2` (`Screen 0: minimum 16 x 16, current 5760 x 1080,
maximum 32767 x 32767`, `Virtual-1 connected primary 1920x1080+0+0 (normal left inverted right
x axis y axis) 480mm x 270mm`), and the window side is KWin scripting over the session bus with
**nothing installed** in the guest (`loadScript` is unprivileged on 5.27 and 6 alike).
`wwmctl -m` prints `Name: KWin` (`Class: N/A`, `PID: N/A`); `wwmctl -l` lists KWin's windows —
XWayland rows with their real X ids and native rows with the backend id KWin's uuid is minted
into (30 bits of it: `0x40000000`–`0x7FFFFFFF`) — and `-lpxG` agrees with real `wmctrl` on
id, class, pid and size for the X rows; on 5.27 the *positions* agree too (KWin 5's xwm
reparents), while on 6.6 real `wmctrl` doubles the frame offset and ours are the true ones.
`wdotool getactivewindow` returns such an id, `getdisplaygeometry` returns `5760 1080`, and
`wxprop -id` on an XWayland window is a byte-identical dump of real `xprop`'s.

**As root over plain `vmctl ssh` the KWin backend works too** (the session bus is found by
scanning `/run/user/*`), so `vmctl ssh` and `vmctl user` both drive Plasma. That was not true
while the backend used `dbus-monitor`; it is the `dbus_mini` client that made it work.

**KDE Plasma on X11** (`noble-kde-x11`, `resolute-kde-x11`) — a plain X11 session, so all four command-line
tools hand over and `warandr` drives the real `xrandr`, exactly as on Xfce. What the flavor
is *for* is the two things that look like they might make Plasma different and do not.
`kwin_x11` owns `org.kde.KWin` on the session bus just as `kwin_wayland` does — `busctl
--user list` shows it, and `wdotool.backend_detect.detect()` answers `KwinBackend` if you
ask it — but nothing of ours does ask it: the handover is decided by the session, before
any backend is detected. Measured in the session: `passthrough.session_kind()` is `x11`
for all four tools, `find_wayland_socket()` is `None` (there is no compositor socket at
all, `/run/user/1000` holds none), `wxrandr --print-backend --verbose` prints `x11` /
`session: x11` / `compositor: X server (RandR)` / `real xrandr: /usr/bin/xrandr`,
`wxrandr --backends` marks `kwin` `unavailable  no wayland socket`, and
`warandr --print-backend` prints `x11` (its status bar says `backend: xrandr (X11)`).
Every handover is an `execve`, not a subprocess: `/proc/<pid>/exe` of the process the
shell started is `/usr/bin/xdotool`. Output is the original's, byte for byte —
`xdotool getdisplaygeometry` (`1920 1080`, per screen), `getactivewindow`, `search`,
`getwindowname/geometry/pid`, `getmouselocation`; `wmctrl -m` (`Name: KWin`, `Class: N/A`,
`PID: N/A`), `-d`, `-l`, `-l -G -p -x`, `-a`; the whole of `xprop -root` and `xprop -id`;
`xrandr --query` (`Screen 0: minimum 320 x 200, current 3840 x 1080, maximum 8192 x 8192`),
`--listmonitors`, `--listproviders`. The versions are the box's own (`xdotool
3.20160805.1`, `xprop 1.2.6`, `xrandr 1.5.2`, `wmctrl 1.07`).

**And it works as root over plain `vmctl ssh` with an empty environment** — but only
since this branch. **SDDM keeps the X cookie in `/tmp/xauth_<random>`**, which is neither
`~/.Xauthority` (it does not exist on this image) nor anything in a runtime directory, so
the cookie search came back empty and every handover died with `Authorization required,
but no authorization protocol specified`. `w11common/session.py` now reads it out of the
session's own leader (`startplasma-x11`, `kwin_x11`, `plasmashell`, ... — uid-qualified
`/proc/<pid>/environ`, the same trick that already found gnome-shell's), so
`repair_x_env()` from a root shell yields `{'DISPLAY': ':0', 'XAUTHORITY':
'/tmp/xauth_...'}` and root gets the same answers `test` gets, where the originals alone
still say `Can't open display`.

**The KWin backend is still reachable here on purpose**, with
`W11_PASSTHROUGH=never` (or `WDOTOOL_BACKEND=kwin`), and it works: `loadScript`
is as unprivileged on `kwin_x11` as on `kwin_wayland`, `wwmctl -l` lists KWin's windows,
`wdotool getactivewindow`/`search`/`getmouselocation` answer. It is still a downgrade,
which is the argument for the handover being in front of it: `wdotool
getdisplaygeometry` fails there with `cannot query Wayland output geometry (no wayland
socket found)`, rc 2, and the ids depend on the KWin generation — 5.27 still exposes
`windowId` to scripts, so `wwmctl -l` prints the windows' **real X ids**, while on 6.6
that property is gone and the same command prints ids minted from KWin's uuids
(`0x54b18d30` where real `wmctrl` says `0x600014`) on a session where every window has
a perfectly good X id. Two more differences between the two releases' images, neither
ours: SDDM 0.20 (24.04) keeps the X cookie in `/tmp/xauth_<random>` and logind records
the session's `DISPLAY=:0`, while SDDM 0.21 (26.04) keeps it in
`/run/user/1000/xauth_<random>` and logind records **no** `DISPLAY` at all — so on 26.04
the display comes from the socket scan and on 24.04 the cookie comes from the session
leader. Both paths are exercised by the pair.

**Xfce** — the X11 flavors, and every tool hands over there, so the answers are the real
tools' own. `wxrandr` prints the X server's listing
(`Screen 0: minimum 320 x 200, current 5760 x 1080, maximum 8192 x 8192`,
`Virtual-1 connected 1920x1080+0+0 (normal left inverted right x axis y axis) 487mm x 274mm` —
X's numbers, not the Wayland backends'). `wwmctl -m` prints `Name: Xfwm4`, `Class: xfwm4` and
xfwm4's real `PID:`; `wwmctl -l` lists the X windows —
`0x01600003 -1 <hostname> xfce4-panel`, `0x01800017 -1 <hostname> Desktop` per monitor, and a GTK
test window as `0x01400003  0 <hostname> vmctl-probe-window`. `wxprop -root
_NET_SUPPORTING_WM_CHECK` prints `_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0xe00032`, and
`wdotool getdisplaygeometry` prints `1920 1080` — xdotool answers per screen, where the Wayland
backends report the 5760x1080 span. `wdotool getactivewindow` prints the focused window's X id
(`25165855`); with nothing focused it fails the way the original does, exit 1 with

> `XGetWindowProperty[_NET_ACTIVE_WINDOW] failed (code=1)`
> `xdo_get_active_window reported an error`

(seen on `noble-xfce` right after login — on `resolute-xfce` xfdesktop holds the focus, and once a
window is open both answer with its id). **All of it works as root over plain `vmctl ssh` too**:
the passthrough supplies the session owner's `DISPLAY` and cookie, so root gets the same output
that `test` gets — the one flavor where `vmctl ssh` is as good as `vmctl user`. The flavors keep
their other job: they are the X-parity oracles (`xrandr`, `wmctrl`, `xdotool`, `xprop` are
installed), so the same command can be run through us and through the original and compared.

**Version note.** Both releases carry **xdotool 3.20160805.1**, not the 4.20260303.1 the repo
claims parity against, so a 4.x-only verb is simply absent on an X11 session: `windowstate
--add MAXIMIZED_VERT <id>` there answers `xdotool: Unknown command: windowstate` / `Run
'xdotool help' if you want a command list`, rc 1. That is the handover working, not a defect —
but nothing may claim `windowstate` works on X11. The listings above come from one 3-head run
each: how many `Desktop` rows xfdesktop publishes, and whether `xrandr` marks `Virtual-1`
`primary`, is the desktop's own state and differs between 4.18 and 4.20.

**sway** — all five work, in the session and as root, with the two exceptions at the end of
this paragraph. `wxrandr` prints the full listing
(`Screen 0: minimum 16 x 16, current 5760 x 1080, maximum 32767 x 32767`,
`Virtual-3 connected 1920x1080+3840+0 (normal left inverted right x axis y axis) 480mm x 270mm`),
`wwmctl -m` prints `Name: wlroots wm` in the session (`Name: sway` from a root shell), and with a
window open (`swaymsg exec foot`) `wwmctl -l` prints `0x00000009  0 <hostname> foot`,
`wdotool getactivewindow` prints `9` and `getwindowname 9` prints `foot`. `wxprop -root
_NET_SUPPORTING_WM_CHECK` prints `_NET_SUPPORTING_WM_CHECK(WINDOW): window id # 0x200004`.
On an **empty** session `getactivewindow` exits 1 with

> `xdo_get_active_window reported an error`

which is xdotool's own wording for "nothing is focused", not a backend failure —
`getdisplaygeometry` (`5760 1080`) and the window commands answer normally on the same session.

Two things are **not** the same from a root shell here, and only here. wlroots starts Xwayland
with no `-auth` file (`Xwayland :0 -rootless -core -terminate 10 -listenfd … -wm …`), so only
the session user's own processes may open that display: real `xprop` over `vmctl ssh` answers
`Authorization required, but no authorization protocol specified` / `xprop: unable to open
display ':0'`, and there is no cookie anywhere for us to find and hand it. `wxprop -root` then
falls back to sway's IPC and prints a *synthesized* set — `_NET_SUPPORTING_WM_CHECK(WINDOW):
window id # 0x0`, a `_NET_CLIENT_LIST` of compositor ids, no `_XKB_RULES_NAMES` — where inside
the session it prints Xwayland's real root. GNOME, KDE and Xfce all give root the real X root,
because their Xwayland/Xorg does have a cookie file. Second: `resolute-sway` is the only golden
without `python3-gi`/`gir1.2-gtk-3.0`, so `warandr`'s GUI there says which package to install
and exits 1 until they are installed (`--print-backend`, `--command` and `--save` need none of
it and work as shipped). `wdotool windowstate MAXIMIZED_VERT|MAXIMIZED_HORZ` is unsupported by
the sway backend and says so, rc 1; `wwmctl -r … -b add,maximized_vert` is the same non-answer,
rc 0. `windowmove` and `windowsize` on a *tiled* window warn and exit 0 without changing it
(`swaymsg floating enable` first, and both land exactly); `windowraise` on a tiled window and
`windowlower` on any window warn and do nothing. `/dev/uinput` on this golden is root-only —
the udev rule that hands the seat user an ACL lives under `gnome/` but is not GNOME-specific
(`sudo sh gnome/install-bridge.sh --udev` installs it on any of the nine).

**Hyprland** (`resolute-hypr`, `arch-hypr`) — the first compositor in this tree with a
first-class backend on **both** sides (`wdotool/backend_hypr.py`, `wxrandr/hypr.py`) after
having been measured on the generic wlroots floor, so the row worth writing is the difference,
measured on 0.53.3 in the recon's VM [recon2/hyprland §3] and re-measured on 0.56.2
[recon2/arch §3.4]. Windows: real geometry and pid (`hyprctl -j clients` publishes `at`, `size`,
`pid`, `class`, `workspace`, `floating`, `fullscreen`), desktops through the workspaces, ids
minted from the compositor's `address` so they no longer move when another window closes, and
XWayland windows listed under their real X id — where the floor printed the whole output as
every rectangle, pid 0, desktop `-1`, and failed `wwmctl -d` outright. Display:
`wxrandr --print-backend` says `hypr`, not `wlr`, and that is not a preference (the wlr path is
in *Hyprland's two output routes*, below). Input: no privilege at all — both
`zwp_virtual_keyboard_manager_v1` and `zwlr_virtual_pointer_manager_v1` are advertised,
`wdotool --vkbd on type` needs neither root nor a udev rule, and `mousemove` landed with 0 px of
error twice. Mirror: Hyprland is, with sway, one of the two compositors measured where `wmirror`
works (`zwlr_screencopy_manager_v1` v3 on both versions; 0.56.2 adds
`ext_image_copy_capture_manager_v1`).

`arch-hypr` exists to bracket one of those, and half of that sentence is measured and half is
not. Measured: on 0.56.2 the `zwlr_output_management` apply is dead from the FIRST request of a
fresh single-head session, where 0.53.3's first one worked [recon2/arch §3.4]. NOT measured:
whether a fresh 0.56.2 session takes `hyprctl keyword monitor`, the route our backend uses. It
answered `ok` and changed nothing there, but only after five timed-out wlr applies had been
through that session. So `vm/live-smoke.d/hypr.sh` carries the applies as `xwant` on Arch and as
plain checks on Ubuntu, `arch-hypr`'s first run is what settles it, and nothing here may call
the 0.56.2 `keyword monitor` measured until that run exists.

**Hyprland's two output routes, measured both ways.** Three readings on `resolute-hypr`
(Hyprland 0.53.3, three heads, 2026-09-09), which correct one paragraph of [recon2/hyprland §4]:
on a VIRGIN session both wlr applies time out (10.16 s, 10.26 s) with the head unchanged, each
naming wxrandr's Hyprland clause; a timed-out wlr apply does **not** stop the session taking
`hyprctl keyword monitor` — the two applies right after it landed in 0.28 s and 0.36 s, where
the recon paragraph reads the other way; and AFTER a `keyword monitor` apply the same wlr
request stops timing out and answers rc 0 in 0.64 s having changed nothing, no stderr, the head
still where it was. That last one is a defect of ours with no rung of the ladder under it:
wxrandr's Hyprland backend re-reads what it applied and the wlr backend does not. **Not yet**;
the fix is one re-read in our own code. `vm/live-smoke.d/hypr.sh` carries the first as a `want`
at the top of `phase_display` and the third as an `xwant` at the end of it.

**Wayfire** (`resolute-wayfire`) — the second privilege-free family in the rig, and the only
flavor whose oracle is independent of the protocol under test: `vm/live-smoke.d/oracle.py
wayfire` reads Wayfire's own JSON IPC (`window-rules/list-outputs`), where `wlr-randr` and
`wxrandr`'s wlr backend are two clients of one `zwlr_output_manager_v1`. The IPC plugins ship
inside the base `wayfire` package and none of them is in the default plugin list, so the
`wayfire.ini` `vm/build-image.sh` writes — `plugins = ... ipc ipc-rules stipc` — is what turns
the window backend on; a stock Wayfire is a wlr-floor compositor. Wayfire floats by default, so
every window command is exercised here and none is skipped for tiling.

Measured on the first live run (instance `wf1`, two heads, 2026-09-08, and the 2026-09-09
re-run): the golden builds in **8.4 min** on 2 vCPU / 3G (876 packages); greetd's autologin
lands a real seat session (`XDG_SESSION_TYPE=wayland`,
`WAYFIRE_SOCKET=/run/user/1000/wayfire-wayland-1-.socket`, first frame 2 s after the session
went active), so libseat does flip logind's `Type` the way sway's does; `wf-panel` paints, and a
maximized window came back `0,52 1920x1028`, so the panel owns the top 52 px of the work area
and the grid plugin honours it; `wdotool type` through `/dev/uinput` and `wdotool --vkbd on
type` both landed byte-exact in the same session; the `[output:Virtual-N] position` stanzas in
`wayfire.ini` are enough on their own, so this flavor needs one layout helper fewer than sway;
`wwmctl -l -x` and the ORIGINAL `wmctrl -l -x` print the xterm's row byte for byte the same,
id column included (`0x0040000c  0 xterm.XTerm           wf1 smokex` from both); `wdotool
getmouselocation` answers `x:960 y:540` BEFORE anything has warped the pointer, which is
`window-rules/get_cursor_position`'s `960.0, 540.0` to the pixel, at scale 1, the only scale the
rig has measured; and with `wmirror Virtual-1 --to Virtual-2 --region 400x300+100+100` running,
the target head's screendump has standard deviation **0.0707** against the 0.02 flatness line,
so on this flavor a flat target is a FAIL and not a note.

**The labwc family** (`resolute-labwc`, `resolute-xfce-wayland`, `resolute-budgie`,
`resolute-lxqt-wayland`) — this is the **generic wlroots floor**, and until these flavors
existed nothing in the rig ran it: `resolute-sway` has an IPC socket, so `wdotool`, `wwmctl` and
`wxprop` take the sway backend there and never the generic one. labwc has no IPC socket at all —
the package is `labwc`, `labnag` and `lab-sensible-terminal` [recon2/labwc §1]. What the floor
printed before the family landed, on one headless labwc with an xterm and a foot:

```console
$ wwmctl -lGpx
0x000f4240 -1 0      0    0    1280 720  xterm.xterm    w11 xtermwin
$ wwmctl -d
get_desktop is not supported by the wlr backend                              (rc 1)
```

with that xterm's real X id `0x40000c` and the X server's own `WM_CLASS` `"xterm", "XTerm"`. A
same-compositor control settled that this is the backend and not labwc: on one headless sway
1.11 the same foot window read `0x00000005 0 85920` natively and `0x000f4240 -1 0` under
`WDOTOOL_BACKEND=wlr` [recon2/labwc §3]. Three of those columns are what these flavors measure:
desktops over `ext_workspace_manager_v1` (labwc advertises it v1 where sway 1.11 advertises no
workspace protocol at all), XWayland rows under their real X id and X `WM_CLASS`, and the
move/resize refusal naming the protocol instead of borrowing sway's tiling excuse — labwc
*stacks*. Native-window geometry stays the floor's `0,0` plus the output rectangle, and that is
**not yet**: the lowest route on the AGENTS.md ladder that would give it is 5, `ConfigureWindow`
over the X plane for XWayland windows only, and that would leave a listing where half the
windows can be moved — a decision before it is code [recon2/labwc §6c]. Everything else already
worked unmodified on the first try: wxrandr's whole apply set (pos + primary + relative chains,
rotate, fractional scale, `--same-as`, off/on, custom modelines), `warandr --command`, `wmirror`
over **both** capture protocols, and typing byte-exact through `zwp_virtual_keyboard_v1` on a
box with no `/dev/uinput` at all [recon2/labwc §3].

Budgie is worth one sentence of its own: **magpie is gone.** Budgie 10.10 dropped its Mutter
fork, `budgie-core 10.10.2-1ubuntu4.1` ships no `/usr/share/xsessions` entry and no `budgie-wm`,
and `budgie-desktop` **Depends on `xdotool`, `wlr-randr` and `wdisplays`** — the shipped default
already carries the X11 tool this project replaces [recon2/budgie §0]. Two step-file facts from
the 2026-09-09 runs belong here too: `vm/live-smoke.d/labwc.sh`'s `phase_windows` sends one
Escape after its `mousemove 300 300 click 1`, because that click lands on labwc's Root context,
whose default binding is `ShowMenu root-menu`, and the menu takes the keyboard grab — which is
why the three `input` checks read an empty file on bare labwc and only on bare labwc (Budgie,
Xfce and LXQt put a desktop window under that pixel); and `vm/live-smoke.d/budgie.sh` reads
`org.buddiesofbudgie.*` off the SESSION bus as the seated user, not the system bus as root, with
its layout-restore check a `want` now — `--output Virtual-3 --below Virtual-2` left Virtual-3 at
1920,1080 immediately and at 1920,1080 ten seconds later, with the service running.

**`resolute-xfce-wayland` is not a pure default install of its Ubuntu flavour**, and it is the
second such flavor after `resolute-kde-x11`. `xubuntu-desktop 2.272` lists neither `labwc` nor
`wayfire` in Depends or Recommends, while LightDM 1.32.0's default sessions-directory already
includes `/usr/share/wayland-sessions` — so a stock Xubuntu 26.04 *offers* "Xfce Session
(Wayland)" and picking it dies with `/usr/bin/startxfce4: Please either install labwc or specify
another compositor as argument`, rc 1. `sudo apt install labwc` is the whole fix, and a reader
is owed that sentence [recon2/xfce-wayland §1].

**COSMIC** (`fedora44-cosmic`, `arch-cosmic`) — cosmic-comp publishes **no
`zwlr_foreign_toplevel_manager_v1` at all**, which is why every window command answered `no
Wayland session found: … the compositor does not offer wlr-foreign-toplevel` on a live COSMIC
session while `wxrandr`, `warandr`, `wmirror --check` and `wdotool type` all already worked
[recon2/cosmic §3, recon2/arch §3.5]. `wdotool/backend_cosmic.py` is a middle capability tier:
real geometry (when the `geometry` event arrives — cosmic-comp sends it only alongside
`output_enter` or on a change, and in the nested rig it never arrived at all), real workspaces
over `ext_workspace_manager_v1`, activate/close/maximize/minimize/fullscreen gated on the
manager's capability array, ids minted from the 32-character `identifier`, and no move, no
resize, no pid.

**The virtio question is answered.** Whether cosmic-comp's KMS backend paints on virtio-vga with
no 3D was open in the plan and in pop-os/cosmic-comp#136. Measured 2026-09-09 on cosmic-comp
1.6.0-3.fc44, booted off the `fedora44-cosmic` build's own disk on
`-device virtio-vga,max_outputs=4,edid=on` with no host GPU and no virgl:
`/sys/kernel/debug/dri/0/state` says `allocated by = cosmic-comp`, `cosmic-comp` and
`cosmic-panel` are up, the seated session is `test seat0 tty1 Service=greetd Active=yes`, and a
QMP screendump of head 0 measures **standard deviation 0.131454** (1280x800) against the rig's
0.02 flat-colour line and the 0.06–0.10 of live GNOME heads. It paints, and there is no nested
`COSMIC_BACKEND=winit` fallback anywhere in the tree. Two more firsts from that boot: logind
reports the seated COSMIC session as `Type=tty` and not `wayland` (greetd registers a tty and
libseat did not flip it, so vmctl's `logind=("wayland","tty")` is right and would have been
wrong with `wayland` alone), and `cosmic-randr list --kdl` off a real KMS head reads
`transform "normal"` where the recon's nested winit document reads `flipped180`, with a 26-line
`modes` block in which only the current mode carries any flag. One build-cost correction while
we are here: `dnf group install cosmic-desktop-environment` resolves to **1,147 packages** and
the install alone was still running after **40 minutes** on 2 vCPU / 3 GiB, so the plan's
"~8 min" for this flavor is wrong by a lot and the CI job needs a timeout to match. On
`arch-cosmic` the flavor also pins `LP_NUM_THREADS=0` (in `vm/flavors/arch-cosmic.yaml`),
rasterising software-GL on the calling thread to route around an Arch mesa 26.2.2 llvmpipe
SIGSEGV in the per-output worker threads that otherwise crashes cosmic-comp ~13 s into a
session — a rig GL-stack workaround (route 4), not a w11 tool behaviour.

**river** (`arch-river`) — **river 0.4's wlr-foreign-toplevel is read-only, and this flavor
exists to say so.** `Window.zig` creates the handle and only ever pushes title, app_id and
activated at it; it registers no listener for any handle request. Measured on a live 0.4.8 +
tinyrwm: `windowclose` (twice, five seconds apart), `windowactivate` with another window focused,
`windowminimize` and `windowstate --add FULLSCREEN` each returned rc 0 and changed nothing, while
river-classic 0.3.17 really closed and really set fullscreen [recon2/river §2a].
`backend_wlr.py`'s verify-after-act is the answer and `vm/live-smoke.d/river.sh` asserts both
halves: the tool warned, and the window did not change. Real window control on river 0.4 is
**not yet**, and the lowest route is 1, `river_window_manager_v1` — closed by the protocol's own
exclusivity rather than by anything a client could send: binding it while a window manager holds
it answers `unavailable` as the first and only event, and a session with no window manager maps
no windows at all. Route 6, a patched river that wires `request_close` and `request_activate`
into the handle it already creates, is next and is a package nobody has costed [recon2/river
§2b, §4]. Two rig facts: river is in no Ubuntu or Debian at all (measured with `apt-cache
madison` and sources.debian.org's exact search), and **the window manager is part of the image**
— none is packaged anywhere, so `arch-river` builds upstream's own reference WM, tinyrwm (C,
meson), pinned to commit `5c01698c`, sha256
`a5ca88ee27e6e6423669e9e726b03fc32dbf0deb314b8f4ebc5ce56c0d2f9940`, in a `runcmd` step ahead of
`vmctl-build`. Every "windows on river" number in this tree is river **plus that WM**, and the
wiki lists 23 others.

**Cinnamon** (`resolute-cinnamon`, `resolute-cinnamon-wayland`) — the two flavors install the
**same package set** (`ubuntucinnamon-desktop xwayland wl-mirror` plus the four parity originals
and `xterm`) and differ in LightDM's `autologin-session` and in nothing else, so the pair is a
controlled comparison of one desktop's two session types. LightDM 1.32's built-in
`sessions-directory` covers `/usr/share/xsessions` **and** `/usr/share/wayland-sessions` (read
out of the shipped binary), which is why one autologin stanza selects either. The metapackage is
`ubuntucinnamon-desktop` 26.04.4 (universe): **`ubuntu-cinnamon-desktop` is not a package name in
this archive**, measured with `apt-cache policy` on the dev box. `xwayland` is named explicitly
because nothing in the Cinnamon stack depends on it — neither `muffin` nor `cinnamon` carries it
in Depends, and `apt-cache rdepends xwayland` names mutter, kwin-wayland, gnome-session, labwc
and hyprland but not muffin — so without it the Wayland session has no X plane at all.
`wl-mirror` is installed on both so that `wmirror --check`'s refusal is about the **compositor**
and never about a missing helper; `wlr-randr` is deliberately absent, because there is no
`zwlr_output_manager_v1` here for it to read.

On `resolute-cinnamon` (X11) everything is by handover, with no code of ours in the path:
`wwmctl -lGpx` byte-identical to `wmctrl -lGpx`, `wwmctl -m` printing `Name: Mutter (Muffin)`,
`wxrandr --print-backend` and `warandr --print-backend` answering `x11`. Two things are
Cinnamon's own and the smoke's `muffin` phase checks them: `-b add,shaded` sticks, because muffin
kept `_NET_WM_STATE_SHADED` where mutter dropped it and KWin 6 refuses it, and
`org.cinnamon.Muffin.DisplayConfig` is on the session bus **of this X11 session** answering
`GetCurrentState` with `'renderer': <'xrandr'>` — the GNOME-on-Xorg trap wearing Cinnamon's
names, which the tools survive because `passthrough.session_kind()` is asked before any bus name.

On `resolute-cinnamon-wayland` (golden built 2026-09-09, 53.1 min, 1783 packages; the final live
run is 57 pass, 0 fail, 7 XFAIL in 666 s) the window plane works: `wwmctl -m` is
`Name: Mutter (Muffin)`, `wwmctl -l -x` is
`0x00000001  0 org.gnome.Terminal.org.gnome.Terminal`, ids are small stable sequences, pid comes
from `get_client_pid` (5726) where `get_pid()` is -1, `windowmove`+`windowsize` land
`100,100 798x591` (exact position, VTE cell rounding on the size), the maximize pair restores
byte-exactly, there are four workspaces, and `getmouselocation` agrees with Cinnamon's own
`global.get_pointer()` to the pixel. Displays go through `wxrandr`'s `cinnamon` backend:
`--print-backend --verbose` says `compositor: Muffin` / `protocol:
org.cinnamon.Muffin.DisplayConfig (D-Bus)`, muffin raises Mutter's own `not adjacent` refusal on
three heads (the first time any muffin has been made to print it), and Cinnamon's Keep-changes
dialog can be answered over Eval — `global.window_manager.complete_display_change(true)` answers
`(true, '"undefined"')` and `~/.config/cinnamon-monitors.xml`, absent before the apply, is
written. Input is `/dev/uinput` only and works. `wmirror` refuses in the compositor's own words
(`advertises neither zwlr_screencopy_manager_v1 nor ext_image_copy_capture_manager_v1`), rc 1,
with `wl-mirror` installed. And across the whole smoke, 958 lines of session bus were recorded
with zero portal calls and zero PolicyKit calls: Cinnamon's Eval route asks nobody for
permission.

**The X plane of `resolute-cinnamon-wayland` works, and it took a pinned Xwayland to get there.**
Cinnamon starts fully (`Cinnamon took 2888 ms to start`, both 1920x1080 heads seen, `wayland-0`
listening); Xwayland **24.1.10** then died with `Fatal server error: Caught signal 11`, muffin
called that fatal (`Connection to xwayland lost`) and cinnamon-session gave up (`respawning too
quickly`). It was never this rig's GPU: `/dev/dri/renderD128` is inside every VM, the identical
`2:24.1.10-1` is fine under mutter and under kwin here, and forcing `-glamor off -shm` — or giving
the guest a real virgl render node — crashes at the same instruction. The fault is
`damage_report()`'s NULL dereference (guest `objdump`, offset `0x5be4a`), fixed upstream in
**Xwayland 24.1.11**, which Ubuntu 26.04 has not shipped. So the golden takes AGENTS.md route 5 at
its cheapest end: `vm/flavors/resolute-cinnamon-wayland.yaml` writes a
`/usr/local/sbin/vmctl-build-hook` that installs Debian's `xwayland_24.1.13-1_amd64.deb` (992 KB,
pinned by sha256, `deb.debian.org` with a `snapshot.debian.org` fallback) over it, and
`vm/build-image.sh`'s `build_hook` runs it after the desktop install — so a failed download stops
the build instead of caching a golden whose session cannot hold a compositor. With it, under a
minute after `systemctl restart lightdm` (44 s and 55 s, the two boots measured on 2026-09-12 --
the shipped hook's own install and the hand-install it was written from), cinnamon and Xwayland
are both alive with zero `Caught signal`, `wmctrl -l` lists the X clients, `xprop -root
_NET_CLIENT_LIST` names them and `xw11 --print-display` hands the originals a working display.
**It is a hold**: the day Ubuntu ships xwayland >= 24.1.11 in resolute, the hook and this
paragraph go. It is per-flavor because a Debian .deb belongs on no Arch, Fedora or NixOS image —
and because only this golden's cache key moves with it.

**MATE, i3, LXQt/Openbox and GNOME on Xorg** (`resolute-mate`, `resolute-i3`, `resolute-lxqt`,
`noble-gnome-x11`) — four more X11 flavors, all handover. **There is no 26.04 GNOME-on-Xorg flavor, and
there cannot be one**: that session is gone from 26.04, measured from the archive's own debs —
`ubuntu-session` 46.0-1ubuntu4 ships `/usr/share/xsessions/ubuntu-xorg.desktop` and 50.1-0ubuntu0.1
ships no xsessions file at all; `libmutter-14` carries `MetaBackendX11Cm` and
`meta-monitor-manager-xrandr.c` and `libmutter-18` carries neither; `daemon/WaylandEnable` is a
string in gdm3 46.2's binary and not in 50.1's [recon2/gnome-xorg §1]. 24.04 is where that session
lives, so that is where the flavor is. There is no MATE or i3 **Wayland** flavor either, because
neither exists in Ubuntu: upstream's `mate-wayland-session` runs Wayfire and wants
`mate-settings-daemon >= 1.29.1` against this archive's 1.26.1, and sway *is* i3's Wayland answer.
And `resolute-lxqt` installs `lubuntu-desktop`, **never `lxqt-core`**: on a clean 26.04
`apt-get install lxqt-core` dies with `trying to overwrite '/etc/xdg/lxqt/panel.conf', which is
also in package lxqt-branding-debian (0.14.0.7)`, and lxqt-panel 2.3.2-0ubuntu1 declares no
Breaks/Replaces against it. Lubuntu's own settings package is also where `window_manager=openbox`
comes from; without that key lxqt-session comes up with no WM and no panel at all.

On `resolute-mate`, three things are marco's own and the smoke's `marco` phase checks them:
`wwmctl -m` prints `Name: Metacity (Marco)` with `Class: N/A`, `PID: N/A` and the
showing-the-desktop mode `N/A` (marco's check window carries no `_NET_WM_PID` and no `WM_CLASS`,
and `_NET_SHOWING_DESKTOP` is absent right after start — three N/A columns are the answer, not a
defect); `-b add,shaded` sticks; and `wdotool windowstate` is Ubuntu's xdotool 3.20160805.1's own
`Unknown command`, which is the repo README's footnote (b) happening. MATE is also the one X11
desktop where `warandr`'s GTK 3 dependency is satisfied out of the box and `arandr` is absent, so
`warandr` is a real addition there.

`resolute-i3` was built and run: the golden took **21.4 min** on 2 vCPU/3G and carries 936
packages, the cheapest desktop in the rig. Under `W11_PASSTHROUGH=never` the sway
backend's **i3 dialect** answers, and the `i3ipc` phase pins its four measured divergences: the
socket at `/run/user/1000/i3/ipc-socket.1434` found with `$I3SOCK` unset; the X id `10485774`
where i3's con id is a 47-bit pointer (`wxprop -id` used to truncate it to `0x5168c680` and
answer `BadWindow`); `getwindowpid` -> `3603` where i3's tree carries no pid field at all; a
floating view under i3's `floating_con` wrapper moving `2640,398` -> `125,159` instead of
refusing as tiled; and `getdisplaygeometry` answering `3840 1080` across two heads instead of
"no Wayland session found". `wxrandr` names the compositor `i3 4.25.1 (2026-02-06)` and refuses an
apply in one line, because i3 has **no `output` command at all**. `gir1.2-gtk-3.0` is not on that
image (python3-gi is), so `warandr`'s GUI carries the same footnote sway's row does.

`noble-gnome-x11` is the handover under the hardest condition the tree has: `org.gnome.Shell`,
`org.gnome.Mutter.DisplayConfig` **and** `org.w11.Bridge` are all on this session's bus,
every one of them a route this toolbox knows, and the answer is still the X server's, because
`passthrough.session_kind()` is asked before any backend is detected. The final live run against
the working tree is **44 pass, 0 fail, 2 XFAIL, 2 XPASS in 296 s**. Four things only this flavor
can measure, all 2026-09-09 on GNOME Shell 46.0 / mutter 46.2: the bridge hands out the X
server's own ids, and `wwmctl -l -x` forced onto our own code is byte-identical to `wmctrl -l -x`
four windows deep; `/dev/uinput` typing reaches an X server, unprivileged, under the package's
udev rule (`xinput list` shows `wdotool virtual keyboard` beside the XTEST one); GDM keeps an
X11 session's cookie at `/run/user/1000/gdm/Xauthority`, `-rwx------ 1 test test 130`, which is
the seated session's own `$XAUTHORITY`, so root with `env -i` lists the same windows where
`/usr/bin/wmctrl` run the same way cannot open a display; and `WM_CLASS` is not the same on the
two halves of the `noble-gnome` / `noble-gnome-x11` pair — under Wayland gnome-text-editor
carries its desktop-file id (`org.gnome.TextEditor`), the same binary on the Xorg session is a
plain X client with `gnome-text-editor.gnome-text-editor`, and it owns TWO windows of that class
with only one mapped, so `search --class` needs `--onlyvisible` there.

**`gnome-shell --replace` is not a reload on a systemd-managed GNOME; it is a way to lose your
extensions.** `/usr/lib/systemd/user/org.gnome.Shell@x11.service` is `Restart=always`,
`RestartSec=0ms`, `RefuseManualStart=on`, `RefuseManualStop=on`,
`OnFailure=org.gnome.Shell-disable-extensions.service gnome-session-failed.target`. A hand-run
`--replace` makes the unit's shell exit, systemd restarts it instantly, the hand-started one
still holds the WM selection, and after four rounds the unit goes `failed (Result: protocol)` —
`Start request repeated too quickly`. That OnFailure unit is `ExecStart=gsettings set
org.gnome.shell disable-user-extensions true`, a PERSISTENT dconf key, so afterwards the bridge
reads `Enabled: No / State: INITIALIZED` in **every later session of that user** and
`org.w11.Bridge` is unowned. It does not come back on a reboot, on `gnome-extensions
enable`, or on deleting `/run/user/1000/gnome-shell-disable-extensions` — all three tried. One
thing recovers it, live and with no logout: `gsettings set org.gnome.shell
disable-user-extensions false`, after which the shell loads the extension and takes the name
within four seconds.

**Mutter's DisplayConfig does not refuse an overlapping layout on Xorg.** `wxrandr --backend
mutter --output Virtual-3 --pos 1920x0` answered rc 0 with empty output and `xrandr --query` then
showed Virtual-2 and Virtual-3 both at `1920x1080+1920+0`. It really is the D-Bus route that
places it: `wxrandr --print-backend --backend mutter --verbose` says `chosen by: flag (--backend
mutter)` / `compositor: Mutter` / `protocol: org.gnome.Mutter.DisplayConfig (D-Bus)` on that
session. The `Logical monitors not adjacent` string that IS in `libmutter-14` belongs to the path
a Wayland Mutter takes.

**Two rig limits of the virtio-vga display, both measured on `resolute-hypr` 2026-09-09.**
`vm/vmctl` passes `max_hostmem=1G` to `virtio-vga` (QEMU's default is 256 MiB): with the default,
after a handful of mode changes across three 1920x1080 heads every further modeset failed with
`drmModeSetCrtc failed: No space left on device` and the guest's dmesg filled with
`virtio_gpu_dequeue_ctrl_func: response 0x1203` (OUT_OF_MEMORY) for commands 0x103/0x104/0x105.
It is a cap and not an allocation. And a head's mode can be made SMALLER within a session and
never larger again: 1920x1080 -> 1680x1050 -> 1280x1024 all land; 1280x1024 -> 1920x1080 does
not, on either the atomic path (`atomic drm request: failed to commit: Invalid argument, flags:
ATOMIC_ALLOW_MODESET ATOMIC_TEST_ONLY`) or the legacy one (`AQ_NO_ATOMIC=1`, `drmModeSetCrtc
failed`). The compositor is not what refuses it — `hyprctl keyword monitor` answers `ok` — and
wxrandr's own re-read is what catches it, which is why `vm/live-smoke.d/hypr.sh`'s two applies
are both shrinks.

## The QEMU / D-Bus facts this rig relies on

Proven on QEMU 8.2.2 with a 24.04 (kernel 6.8) and 26.04 (kernel 7.0) guest:

* GPU: `-device virtio-vga,id=gpu0,max_outputs=4,edid=on`
  `-display dbus,addr=unix:path=<BUS>,p2p=no` where `<BUS>` is a private session bus:
  `dbus-daemon --session --fork --nopidfile --print-pid=1 --address=unix:path=<BUS>`
  (dbus-daemon has no `--pidfile`; vmctl records the printed pid so it can stop it again).
  No host GUI is needed.
* On that bus QEMU owns the name **`org.qemu`** (not `org.qemu.Display1`). Each head of gpu0
  is an object `/org/qemu/Display1/Console_<N>` (N = head index, label `gpu0.N`) with interface
  `org.qemu.Display1.Console` and method
  `SetUIInfo(q width_mm, q height_mm, i xoff, i yoff, u width, u height)`:

  ```
  gdbus call --address unix:path=<BUS> --dest org.qemu \
        --object-path /org/qemu/Display1/Console_1 \
        --method org.qemu.Display1.Console.SetUIInfo 0 0 0 0 1920 1080
  ```

  hot-plugs head 1 as guest connector **`Virtual-2`** with an EDID whose preferred mode is
  1920x1080; width/height `0 0` unplugs it. Head 0 (`Virtual-1`) is always connected and is
  resized the same way. Head N ↔ `Virtual-(N+1)`. All four `Console_<N>` objects exist from the
  moment QEMU is up (the name is acquired asynchronously; `vmctl start` polls for it).
* **`SetUIInfo` works before the guest has booted**: QEMU keeps the requested size per scanout
  and reports it (plus the EDID) on the driver's first `GET_DISPLAY_INFO`, so heads set right
  after QEMU starts are simply *there* when GDM/mutter come up — that is what `vmctl start` does.
  Plugging them after ssh-up instead lands 2-6 s after gnome-shell starts and, on a loaded host,
  raced with gnome-shell's start-up animation: every head stayed a flat `#222222` (no top bar,
  dock or wallpaper — mutter had painted each view exactly once) until the next login.
* The guest kernel's `/sys/class/drm/*/modes` stays stale until something re-probes the
  connector (`echo detect > .../status`, or any `drmModeGetConnector` — compositors do that on
  the hotplug uevent). `vmctl heads` forces the re-probe; mutter's `GetCurrentState` is the
  authoritative view (a hotplug shows up there within ~0.5 s, an unplug within ~4 s).
* Screenshots: QMP `{"execute":"screendump","arguments":{"filename":"/abs/path.png",
  "device":"gpu0","head":N,"format":"png"}}` (after `qmp_capabilities`) over
  `-qmp unix:<path>,server,nowait`. The PNG is written by QEMU on the host. `"format":"ppm"`
  (QEMU >= 7.1) writes a raw P6 instead — that is how `vmctl session` checks whether a head has
  painted without depending on an image library in the host's python.
* Guest access: cloud-init NoCloud seed (`cloud-localds`) with `disable_root: false` and the
  root key; user-mode networking with `hostfwd=tcp:127.0.0.1:<port>-:22`; `-enable-kvm -cpu host`.
* No virtual input devices are attached (`virtio-tablet/keyboard` are not needed: the tools
  inject through `uinput` inside the guest).

## What a hotkey-launched process sees

Measured on both flavors with a GNOME custom shortcut
(`org.gnome.settings-daemon.plugins.media-keys custom-keybindings`, `command='sh -c "env > /tmp/hotkey-env.txt"'`)
fired by `python3 -m wdotool key ...` from a root shell in the guest: the file appears within
1-4 s, written by a child of `gsd-media-keys` (`org.gnome.SettingsDaemon.MediaKeys.service` in the
user manager). It gets the **full graphical session environment** (46 variables on 24.04, 44 on
26.04), in particular:

```
WAYLAND_DISPLAY=wayland-0            DISPLAY=:0
XDG_RUNTIME_DIR=/run/user/1000       XAUTHORITY=/run/user/1000/.mutter-Xwaylandauth.XXXXXX
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
XDG_SESSION_TYPE=wayland             XDG_SESSION_CLASS=user
XDG_CURRENT_DESKTOP=ubuntu:GNOME     XDG_SESSION_DESKTOP=ubuntu   DESKTOP_SESSION=ubuntu
GDMSESSION=ubuntu                    GNOME_SHELL_SESSION_MODE=ubuntu (24.04)
GNOME_SETUP_DISPLAY=:1 (24.04) / unix:/tmp/.X11-unix/X1 (26.04)
SESSION_MANAGER=local/<host>:@/tmp/.ICE-unix/<pid>,unix/<host>:/tmp/.ICE-unix/<pid> (24.04)
SSH_AUTH_SOCK=/run/user/1000/keyring/ssh   GNOME_KEYRING_CONTROL=/run/user/1000/keyring
GIO_LAUNCHED_DESKTOP_FILE_PID, INVOCATION_ID, JOURNAL_STREAM, MANAGERPID, SYSTEMD_EXEC_PID,
MEMORY_PRESSURE_WATCH/WRITE (systemd user service plumbing)
QT_IM_MODULE=ibus  XMODIFIERS=@im=ibus  GTK_MODULES=gail:atk-bridge  QT_ACCESSIBILITY=1
XDG_DATA_DIRS, XDG_CONFIG_DIRS, XDG_MENU_PREFIX=gnome-, LANG=C.UTF-8
HOME=/home/test USER=test LOGNAME=test USERNAME=test SHELL=/bin/bash PWD=/home/test PATH=...:/snap/bin
```

26.04 additionally has `GNOME_DESKTOP_SESSION_ID`, `GPG_AGENT_INFO`, `IM_CONFIG_ENTRY`, `MANAGERPIDFDID`,
`QT_IM_MODULES`, `XDG_SESSION_EXTRA_DEVICE_ACCESS`. Neither has `XDG_SESSION_ID`. Notes:

* **Do not bind `<Ctrl><Alt>F1..F12`.** On GNOME Wayland mutter owns `<Primary><Alt>Fn` as
  `switch-to-session-n` (`org.gnome.mutter.wayland.keybindings`), `gsd-media-keys` logs
  `Failed to grab accelerator for keybinding custom:...` and the injected chord **switches the
  guest to ttyN**: the session goes `State=online`, the screen is a text console and nothing
  reacts until `vmctl ssh <name> -- chvt 2` (session active again, desktop back). This is GNOME's
  default, not something the rig changes. `<Super>F9` and `<Ctrl><Shift>F9` work. If a test
  needs the `Ctrl+Alt+Fn` chords, clear them for the session first:
  `vmctl user <name> -- gsettings set org.gnome.mutter.wayland.keybindings switch-to-session-9 "[]"`.
* On 26.04 `/tmp` is a tmpfs: anything a test writes there is gone after a reboot.

## What a cron @reboot process sees

`crontab -u test`: `@reboot sh -c 'sleep 20; env > /tmp/cron-env.txt'`. On both flavors cron
runs the job about 8 s after boot — **before the graphical session exists** (GDM autologin is
active ~20-28 s after boot, `systemd-analyze` 9-12 s). The process gets exactly six variables:

```
HOME=/home/test  LANG=C.UTF-8  LOGNAME=test  PWD=/home/test  SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin
```

No `WAYLAND_DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, `DISPLAY` or `XAUTHORITY`.
A cron-started tool must therefore wait for the session and set them itself:
`XDG_RUNTIME_DIR=/run/user/1000`, `WAYLAND_DISPLAY=wayland-0`,
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`, and for the X side `DISPLAY=:0` plus the
`XAUTHORITY` from `systemctl --user show-environment` — exactly what `vmctl user` does.

## Gotchas

* **The session is "active" before anything is drawn.** logind reports the session active
  a few seconds before the desktop paints; a screendump taken then still shows the kernel
  console (mirrored on every head by fbdev), and on a slow host the window is several
  seconds wide. `vmctl session` therefore also waits for the first frame (per desktop: see
  the command table), and `selftest.sh` asserts that head 0 differs from head 1 — except on
  sway, whose bar is identical on every output. **The heads do not paint at once**: on Xfce the
  panel is on head 0 while heads 1 and 2 are still black (`xfdesktop` draws the wallpaper per
  monitor, seconds later), so what the guest reports about head 0 is not enough — the wait ends
  only when no head is still a flat colour.

* `vmctl start --heads N` sizes heads `0..N-1` to 1920x1080 (`--head-size` changes it) before
  the guest boots, so Virtual-1 is the primary monitor (top bar, dock) at logical (0,0) and
  screen coordinates like `wdotool mousemove 300 200` land on it. Head 0 left at QEMU's default
  EDID (1280x800; only old instances or `--head-size` mixes get there) makes GNOME prefer the larger
  Virtual-2 as primary at (0,0), with Virtual-1 at x=1920 — confusing for coordinate tests.
  Sizes survive `stop`/`start` via `meta.json`.
* `vmctl head ... off` on head 0 is refused (virtio-vga's first scanout is always connected).
* **X11: a removed output stays in `xrandr` until someone disables it.** The unplug takes the DRM
  connector away (`vmctl heads` → `Virtual-4 disconnected`), but the X server keeps scanning the
  output out: `xrandr --query` shows `Virtual-4 disconnected 1280x1024+5760+0`, `--listmonitors`
  still counts it, and `vmctl heads` says `disconnected enabled=enabled`. Disabling it is the
  desktop's job; Xfce 4.18 does it within a second, Xfce 4.20 is unreliable — one boot released
  every removed output in 1-3 s, another held them for over a minute. `vmctl head <idx> off`
  therefore finishes the job itself on the X11 flavors (5 s of grace, then `xrandr --output
  Virtual-N --off`), which turns a minute-long stall into a 6 s removal.
* Screendumps of a head the guest has never scanned out are black.
* **The mouse cursor is not in a screendump**: virtio-gpu puts it on a hardware cursor plane that
  QEMU's `screendump` does not compose. To verify pointer motion read the KMS cursor plane in the
  guest (`/sys/kernel/debug/dri/0/state`: the 64x64 `plane-N` shows `crtc-pos=64x64+296+196` for a
  pointer at (300,200) with the 4,4 hotspot), or, on the host, register an
  `org.qemu.Display1.Listener` on each `Console_<N>` and read the `MouseSet` calls
  (`x=296 y=196 on=1` on the head that holds the pointer, `on=0` on the others). With the default
  head layout `wdotool mousemove 300 200` lands on head 0 / Virtual-1, `mousemove 2220 200` on
  head 1 / Virtual-2 at its local (300,200).
* A screenshot that is a single flat `#222222` on every head means gnome-shell never finished
  starting (see the SetUIInfo fact above); `vmctl start` avoids the known cause, and
  `selftest.sh` checks for it. Logging out/in or a reboot clears it.
* `vmctl user` runs under `sudo -u test`; anything needing `/dev/uinput` still needs root
  (`vmctl ssh`) or `sudo` from inside (`test` has NOPASSWD sudo).
* **An ISO flavor's golden has no backing file.** `vmctl build`'s goldens are overlays on a
  cloud image in `~/images/` by absolute path; `vm/build-iso-golden.sh`'s is the installed disk
  itself, so it can be copied anywhere on its own — but it is 8.7 GB, not a few hundred MB.
* **Logging out of a GNOME flavor parks it at the greeter, for good.** GDM performs an
  automatic login **once per boot** — deliberately, so that a user who logs out can reach the
  greeter — and these VMs have no keyboard to type a password into one (QEMU's D-Bus display
  is output only). So `gnome-session-quit --logout`, or anything else that ends the session,
  leaves `loginctl` showing only `gdm-greeter` on seat0 and `vmctl session` timing out with
  `no active wayland session for user test`. When a test needs the "log out and back in" that
  installing the GNOME Shell extension asks for, **reboot the instance** (`vmctl ssh <name> --
  systemctl reboot`, then `vmctl session <name>`); autologin fires again on the fresh boot.
  Measured identically on `resolute-gnome-iso` and `resolute-gnome`, so it is the rig, not the
  image — and not something a person at a real keyboard ever sees.
* **`resolute-gnome-iso` is the flavor that is allowed to be untidy.** Anything that makes a
  test on it pass by turning a default off belongs in the *test*, not in the image: quietly
  patching the image is how the cloud-image flavors came to differ from a real install by 226 packages, 55
  missing ones, a different kernel and 5 snaps without anyone noticing. Every one of its eight
  deviations is written down twice, in the flavor yaml and in the table above; keep it that way.
* **A build VM that is killed mid-boot stops at the GRUB menu on the next one.** Ubuntu's GRUB
  sets `recordfail` when a boot does not finish and then waits for a keypress — forever, on a VM
  with no keyboard, with nothing on the serial console to say so. `vm/build-iso-golden.sh`
  therefore powers its VM down over QMP rather than killing it on a failure, and while it waits
  for ssh it presses Return over QMP (`send-key`) if nothing has reached the console; QMP is also
  the reason stage 2 has a `-qmp` socket where stage 1 does not need one. The same kill also
  costs the target its ssh host keys if it happens on the first boot, before they are on disk —
  after which sshd resets every connection and the only cure is to install again.
* A build that fails leaves `~/vm-data/build/<flavor>/` (serial.log, and root ssh on the
  printed port while the VM is alive) for inspection; `vmctl list` shows it as `(build)`.
* Instances re-run cloud-init once (new instance-id) — it only sets the hostname, re-applies
  the root key/user and regenerates ssh host keys; that is why `vmctl ssh` ignores known hosts.
* Rebuilding a golden (`build --force`) replaces the backing file of every instance overlay on
  it; restart those instances with `--fresh`.
* **Xfce is the X11 flavor**: `vmctl user` exports no `WAYLAND_DISPLAY` there and the compositor
  is `Xorg`, so Wayland-only tools have nothing to talk to — that is the point of the flavor
  (it is the X-side oracle). Our four tools do not fail there: on an X11 session they hand over
  to the real `xdotool`/`wmctrl`/`xprop`/`xrandr` (see *What the four tools do on each flavor*). On 26.04 `xubuntu-desktop` also installs `gnome-shell` and GDM;
  the build forces LightDM and fails if it did not win (see Flavors).
* **Plasma 6 opens its welcome centre from a kded module**, not from an autostart entry, so
  hiding the `.desktop` file is not enough: `plasma-welcomerc`'s `LastSeenVersion` must name the
  installed version.
* **sway enumerates its initial outputs in reverse**: without the `vmctl-sway-layout` line in the
  session config, `Virtual-3` would be at (0,0) and `Virtual-1` off to the right, and workspace 1
  would land on the last output. A golden built without it breaks every coordinate test.
