# Setting up the rig

This guide prepares a Linux host to run the VM test environment. See
[vm/README.md](README.md) for the available desktop configurations, commands and
coverage. The setup script installs prerequisites and reports any remaining work.

Clone or unpack the repository in your home directory and run the following from
its root. The setup uses `~/vm-data` and `~/images` for VM files, in addition to the
system packages it installs.

```console
$ vm/setup-host.sh            # check prerequisites, install packages and create directories
$ vm/setup-host.sh --no-apt   # check prerequisites without installing packages
```

You can rerun the script after resolving a reported problem. It exits 0 when the
host is ready and 1 when a step still needs attention, printing the command needed
for that step. Package installation uses `apt-get` and requires root; the script
announces this before running it.

## The machine

Any Linux machine on which `/dev/kvm` works: a physical box, or a virtual machine
whose hypervisor passes hardware virtualization through to it.

Hardware virtualization is the CPU feature KVM is built on (Intel VT-x or AMD-V;
`vmx` or `svm` in the `flags` line of `/proc/cpuinfo`). A physical machine has it
whenever the firmware has it switched on. Inside a virtual machine the feature is
only there if the hypervisor underneath chooses to show it to its guests, that is
KVM *nested* inside a virtual machine: your rig host runs KVM, and the rig's guests
run one level further down. Providers of virtual machines offer it on some kinds of
machine and not on others, and some need it switched on per machine, so on a rented
machine check before anything else:

```console
$ grep -c -E '^flags.*\b(vmx|svm)\b' /proc/cpuinfo   # one line per CPU when the feature is there, 0 when not
$ ls -l /dev/kvm                          # crw-rw---- root kvm: the kvm module is loaded
$ systemd-detect-virt                     # none on a physical machine; otherwise the hypervisor's name
```

Without it nothing here boots as written. QEMU can run these guests without KVM
(`-accel tcg`, its software emulation), many times slower; the scripts do not fall back
to it. `vmctl` and `build-iso-golden.sh` both pass `-enable-kvm -cpu host`, and
QEMU then stops at once, ```
Could not access KVM kernel module: Permission denied
qemu-system-x86_64: failed to initialize kvm: Permission denied
```

(`No such file or directory` when there is no `/dev/kvm` at all), rather than start a
build or a self-test whose timeouts (240 s for ssh in `vmctl start`, 180 s for the
desktop in `vmctl session`) are set for KVM speeds. `Permission denied` on a machine
that *has* `/dev/kvm` is the other common case: Ubuntu's udev rules make it
`0660 root:kvm` and hand an ACL to whoever is logged in at the machine's own screen,
which an ssh login is not, so a user who works over ssh needs
`sudo usermod -aG kvm $USER` and a new login.

One level is not the limit. This document, `vm/setup-host.sh`, a `noble-gnome` golden
image and a passing `vm/selftest.sh` were all checked on a machine that was itself a
guest of a KVM host, with the rig's own guests one level below that again: slower at
each level, but not different. What matters is that `/dev/kvm` works where you are,
not how many machines are stacked above you.

## Sizing

What the scripts ask for, per virtual machine (all of it changeable with `--cpus`,
`--mem`, `--size`):

| what | vCPU | memory | disk | from |
|---|---|---|---|---|
| a golden build (`vmctl build`) | 4 | 6 GB | 30 GB overlay, grows to the image's size | `vmctl build` defaults |
| the ISO install (`build-iso-golden.sh`) | 2 | 4 GB | 30 GB, no backing file | its defaults |
| the NixOS build (`build-nixos-golden.sh`) | the sandbox's own | the sandbox's own | 12 GB `virtualisation.diskSize`, ~9.2 GiB peak in the build directory, plus the golden | `vm/nixos/common.nix` |
| a desktop instance (`vmctl start`) | 3 | 4 GB | overlay on the golden | `vmctl start` defaults |

A guest's QEMU process grows towards its `--mem` as the guest touches memory, not
beyond it: measured, 4.9 GB resident for a 6 GB build VM while the desktop was
installing, 2.4 GB for a 4 GB GNOME instance after the self-test, 4.1 GB for the
4 GB ISO installer. A machine with 4 CPUs and 16 GB runs one build or two desktop
instances at a time comfortably; 8 CPUs and 32 GB run four (`vm/README.md`).

QEMU asks for the whole `--mem` when it starts and does not settle for less, so on a
machine smaller than the defaults it is the *defaults* that fail, before anything
boots:

```
qemu-system-x86_64: warning: Number of SMP cpus requested (4) exceeds the recommended cpus supported by KVM (2)
qemu-system-x86_64: cannot set up guest memory 'pc.ram': Cannot allocate memory
```

Give it what the machine has instead, `vmctl build <flavor> --cpus 2 --mem 3G` and
so on. `vm/setup-host.sh` reports the machine's vCPU count and memory and prints the
pair it can take in the command it suggests.

Disk, measured on the finished images (`du -sh ~/vm-data/golden/*.qcow2`):

* a prepared VM image is **5.4 to 7.6 GB** for a flavor built from a distro desktop
  metapackage (`noble-gnome` the smallest, `resolute-kde-x11` the largest), **3.5 GB**
  for `stonking-kde` (the bare Plasma session, no metapackage), **0.7 GB** for
  `resolute-sway`, and **8.7 GB** (`resolute-gnome-iso`) and **10 GB**
  (`noble-gnome-iso`) for the two the Ubuntu installer builds; all thirteen together,
  80 GB;
* the base cloud images they are overlays on: 0.6 GB (24.04), 0.8 GB (26.04) and
  0.8 GB (26.10, `stonking-kde` and `stonking-gnome`), in `~/images`; and a desktop ISO per installer
  flavor, 6.1 GB (26.04) and 6.2 GB (24.04);
* an instance is an overlay on its golden: a few hundred kilobytes when created,
  173 MB after one self-test, tens to a few hundred MB after a day of use. It never
  has to be bigger than what the guest wrote;
* a build's working directory (`~/vm-data/build/<flavor>/`) grows to the golden's size
  and is moved into `golden/` on success, a rename when `build/` and `golden/` share
  a filesystem, a copy otherwise.

So: 10 GB per prepared VM image you intend to keep, plus 2 GB for the three base cloud
images and 6 GB for each desktop ISO you build from, plus a few GB for instances.
Swap is not needed for the rig itself, a host with enough memory for the guests it
runs barely touches it, tens of MB over days of builds and instances, but a swap file
the size of one guest turns a memory shortfall into a slowdown rather than a killed
QEMU.

**The NixOS build counts as one of those virtual machines.** `vm/build-nixos-golden.sh`
does not drive QEMU itself; `nix build` does, inside the nix sandbox, through nixpkgs'
`make-disk-image`. So it is a VM by any measure that matters to this host and the script
refuses to start beside a running rig instance, one VM at a time means one, the NixOS
build included. It also needs `kvm` in `nix config show system-features` and refuses
without it. Measured: 4.9 GiB of image and about ten minutes on 4 vCPU from a cold store.

## Packages

`nix` on the host is no longer optional for the whole rig: the two NixOS flavors are built
with `nix build` (2.34.8 here), which wants `experimental-features = nix-command flakes`
and the `kvm` system feature above. Everything else on the host is apt, as follows.

On Ubuntu 24.04 or 26.04, `vm/setup-host.sh` runs exactly this, after saying so:

```console
$ sudo apt-get install -y qemu-system-x86 qemu-system-modules-opengl qemu-utils cloud-image-utils \
      genisoimage dbus libglib2.0-bin openssh-client python3 imagemagick
```

On a machine with nothing on it but the distribution's server install that is 214
packages, 149 MB to download and 662 MB on disk, measured, three minutes at
0.7 MB/s. Most of it is dependencies QEMU declares and the rig never uses: GTK,
SDL2, SPICE, GStreamer, PipeWire, PulseAudio. Nothing here opens a window on the
host; the guests are watched over D-Bus and QMP.

Which program each one is there for, so the list can be translated for another
distribution:

| the rig calls | package (24.04 and 26.04) |
|---|---|
| `qemu-system-x86_64` | `qemu-system-x86` |
| `ui-dbus.so`, the `-display dbus` backend | `qemu-system-modules-opengl` (`qemu-system-gui` depends on it; the module needs GLib's gio, and QEMU built with libpng for PNG screendumps) |
| `qemu-img` | `qemu-utils` |
| `cloud-localds` | `cloud-image-utils` (uses `genisoimage`) |
| `isoinfo` | `genisoimage`, `build-iso-golden.sh` only |
| `dbus-daemon` | `dbus` |
| `gdbus` | `libglib2.0-bin` |
| `ssh`, `scp`, `ssh-keygen` | `openssh-client` |
| `python3` 3.10 or newer | `python3` (`vmctl` is stdlib-only) |
| `identify` | `imagemagick`, `selftest.sh` only, to prove a screenshot is not one flat colour; without it the check is skipped, not failed |

Nothing else: no libvirt, no bridge, no root after this step. The guests reach the
network through QEMU's user-mode networking, and the host reaches them through one
forwarded port each on the loopback interface (`2400–2499` for `vmctl` instances and builds,
`2500–2599` for an ISO build, `2222` for the old sway rig).

## What vmctl needs from QEMU

Four things, proven on QEMU 8.2.2 (`vm/README.md`, *The QEMU / D-Bus facts this rig
relies on*) and each checkable without a guest:

1. **The dbus display backend.** `qemu-system-x86_64 -display help` must list `dbus`.
   This is how heads are plugged, unplugged and resized: QEMU owns `org.qemu` on a
   private session bus and exports one `/org/qemu/Display1/Console_<N>` per head.
2. **virtio-vga with four heads.** `qemu-system-x86_64 -device help | grep virtio-vga`;
   `max_outputs=4` is what gives the guest `Virtual-1` … `Virtual-4`.
3. **`SetUIInfo` on a console**, accepted before the guest has booted, that is what
   lets `vmctl start` size every head before GDM comes up.
4. **QMP `screendump` with `"format": "png"`** (and `"ppm"`, which `vmctl session`
   uses to tell a painted head from a flat one without an image library). The
   `format` argument exists since QEMU 7.1; PNG needs a QEMU built with libpng.

`vm/setup-host.sh` checks all four the direct way: it starts a private
`dbus-daemon` and a **paused, diskless** QEMU (`-S`, no KVM, 128 MB) with
`-display dbus` and `virtio-vga,max_outputs=4`, waits for `Console_0` … `Console_3`
to appear on the bus, calls `SetUIInfo` on `Console_1`, takes a PNG screendump of
head 0 over QMP, and quits, a fraction of a second. To do it by hand:

```console
$ dbus-daemon --session --fork --nopidfile --print-pid=1 --address=unix:path=/tmp/bus
$ qemu-system-x86_64 -S -nodefaults -m 128 -display dbus,addr=unix:path=/tmp/bus,p2p=no \
      -device virtio-vga,id=gpu0,max_outputs=4,edid=on -qmp unix:/tmp/qmp,server,nowait -daemonize
$ gdbus introspect --address unix:path=/tmp/bus --dest org.qemu --object-path /org/qemu/Display1 | grep Console_
$ gdbus call --address unix:path=/tmp/bus --dest org.qemu --object-path /org/qemu/Display1/Console_1 \
      --method org.qemu.Display1.Console.SetUIInfo 0 0 0 0 1920 1080
```

and, over the QMP socket, `{"execute":"qmp_capabilities"}` then
`{"execute":"screendump","arguments":{"filename":"/tmp/shot.png","device":"gpu0","head":0,"format":"png"}}`, a 640x480 PNG of a machine that has never run.

## Where things go

Nothing of this is in the repository. `vmctl` and the build scripts keep their state
under `$VMDATA` (default `~/vm-data`) and look for the Ubuntu images in `$VMIMAGES`
(default `~/images`); both are created on first use, `setup-host.sh` just does it
earlier:

```
~/images/                          base cloud images and the desktop ISOs, as downloaded
~/vm-data/golden/<flavor>.qcow2    the golden images; a cloud-image flavor's is an overlay whose
                                   backing file is the base image in ~/images BY ABSOLUTE PATH
~/vm-data/golden/<flavor>-packages.txt, <flavor>.build.log
~/vm-data/instances/<name>/        one directory per instance: disk.qcow2 (overlay on the golden),
                                   seed.img, meta.json, qmp.sock, bus, dbus.pid, qemu.pid, serial.log
~/vm-data/build/<flavor>/          a build in progress; removed on success, kept on failure
~/vm-data/keys/id_ed25519[.pub]    the guest root key, generated once and used by every guest
```

Two consequences: do not move or delete `~/images` while cloud-image prepared images exist
(only the two installer-built prepared images stand on their own), and a copy of `~/images`
plus `~/vm-data/golden` to another machine with the same home directory is a working
set of prepared images.

The images the flavors expect, by the names in their `# vmctl-base:` / `# vmctl-iso:`
headers:

```console
$ cd ~/images
$ curl -LO https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img          # the noble-* flavors
$ curl -LO https://cloud-images.ubuntu.com/releases/26.04/release/ubuntu-26.04-server-cloudimg-amd64.img   # resolute-*
$ curl -LO https://releases.ubuntu.com/26.04/ubuntu-26.04.1-desktop-amd64.iso                    # resolute-gnome-iso only
$ curl -LO https://releases.ubuntu.com/24.04/ubuntu-24.04.5.1-desktop-amd64.iso                    # noble-gnome-iso only
$ curl -LO https://cloud-images.ubuntu.com/stonking/current/stonking-server-cloudimg-amd64.img    # stonking-kde and stonking-gnome (26.10, a development release: this image moves)
```

`build-iso-golden.sh` checks the ISO's sha256 against the one in the flavor before it
boots anything; the cloud images are whatever `current`/`release` points at when you
download them, which is why every golden's package list is kept next to it.

<a id="the-first-golden-image"></a>

## The first prepared VM image

```console
$ vm/vmctl build noble-gnome
vmctl: generated guest root ssh key ~/vm-data/keys/id_ed25519
vmctl: build noble-gnome: base noble-server-cloudimg-amd64.img, 4 vCPU/6G, ssh ... (root, debugging only)
vmctl: [  20s] vmctl-build: flavor noble-gnome (gnome): Ubuntu 24.04.4 LTS, kernel 6.8.0-138-generic
vmctl: [  50s] vmctl-build: installing ubuntu-desktop (expect 5-20 minutes)
vmctl: [ 380s] vmctl-build: GDM: autologin of user test on Wayland, graphical.target
vmctl: [ 390s] vmctl-build: golden image build finished
vmctl: build noble-gnome: done in 6.6 min -> ~/vm-data/golden/noble-gnome.qcow2 (1695 packages listed in ...)
```

That run, a fresh `$VMDATA`, the default 4 vCPU / 6 GB, with another build running
beside it on the same cores, took 6.6 minutes and produced a 5.4 GB image. The build
logs of the other flavors (`golden/<flavor>.build.log`, the guest's uptime at
power-off) put a distro desktop metapackage between 6.3 minutes (`noble-xfce`, 376 s)
and 10.7 minutes (`resolute-kde`, 641 s), the GNOME builds at the default 4 vCPU / 6 GB,
the others at 3 vCPU / 5 GB, with `stonking-kde` at 3.2 minutes and `resolute-sway` at
one minute; almost all of it is the desktop packages downloading and unpacking, so the
network matters as much as the CPU. `build-iso-golden.sh` is 14 minutes for
`resolute-gnome-iso` and 21 for `noble-gnome-iso`, both on its default 2 vCPU / 4 GB
(12.6 of the 14 are the installer's). Builds run one at
a time per flavor and refuse to overwrite a golden without `--force`; two *different*
flavors can build side by side on a machine with the memory for it.

The same build with `--cpus 2 --mem 3G`, on two cores and 3.8 GB with KVM one level
further down, took 17.2 minutes, and produced the same image: 5.5 GB against the
roomy run's 5.4, the same 1695 packages. A smaller machine is slower, not less able.

## The self-test

```console
$ vm/selftest.sh noble-gnome
== [0s] vmctl start noble-gnome-t --flavor noble-gnome --heads 3 --fresh   (desktop gnome, native tool: GetCurrentState)
vmctl: noble-gnome-t: ssh up after 25s
== [25s] vmctl session noble-gnome-t
vmctl: noble-gnome-t: wayland session 3 of user test (gnome) active after 5s
vmctl: noble-gnome-t: desktop painted its first frame 6s later
== [35s] GetCurrentState: expect Virtual-1..3, Virtual-1 at 0,0, no first-run window
...
== [52s] PASS: noble-gnome-t (noble-gnome, gnome) running, ssh port 2403, screenshots in /tmp/vmctl-selftest-noble-gnome-t
```

52 seconds for GNOME with three other instances competing for the same cores, 35
seconds on an idle host (`vm/README.md` says roughly 40; a desktop that starts more
slowly takes correspondingly longer); 143 seconds at 2 vCPU / 3 GB, 108 of them the
guest's own boot.

The self-test starts its instance with `vmctl start`'s defaults, 3 vCPU, 4 GB, and
takes no options of its own, so a machine with less than that has to say so:

```console
$ SELFTEST_VM_ARGS='--cpus 2 --mem 3G' vm/selftest.sh noble-gnome
```

What it asserts, step by step, is under *Self-test* in `vm/README.md`; the two things
that are easy to miss: it leaves the instance **running** for inspection
(`vm/vmctl stop noble-gnome-t`, or `destroy`), and its three screenshots (1.3 MB each)
stay in `/tmp/vmctl-selftest-<name>/`.
A machine that passes it for one flavor passes it for the others once their prepared images
are built, the rig is the same; only the desktop differs.

## The unit suite and the guest

The suite (`tests/`, pytest) is a host-side thing: `python3 -m pytest tests -q` on the
machine you develop on, or in the nix dev shell, with no desktop session of these
kinds around. On such a machine it is `2028 passed, 61 skipped in 152.39s`, the whole
suite the repo README counts, in under three minutes (172 s with three instances
running beside it, when one timing-sensitive lifecycle test failed and then passed on
its own).

Run it *inside* a guest and it does not pass, by construction. Measured on the
`noble-gnome` self-test instance, once as `test` in the live session and once as root
over `vmctl ssh` with `env -i`:

```console
$ git archive HEAD | vm/vmctl ssh noble-gnome-t -- 'mkdir -p /home/test/w11 && tar -x -C /home/test/w11 && chown -R test:test /home/test/w11'
$ vm/vmctl ssh noble-gnome-t -- apt-get install -y python3-pytest        # the goldens have no pytest
$ vm/vmctl user noble-gnome-t -- sh -c 'cd /home/test/w11 && python3 -m pytest tests -q -p no:cacheprovider --ignore=tests/test_cli_parity.py'
...
164 failed, 1822 passed, 98 skipped, 40 warnings in 106.66s (0:01:46)
```

(162 failed, 1801 passed, 105 skipped, 16 errors as root.) The same two modules that
carry most of the failures pass on the host in eight seconds. What fails is every test
that asserts the *absence* of a compositor, they set `XDG_RUNTIME_DIR=/nonexistent`
and expect an error, or drive a mock session bus, because the tools find the guest's
real session anyway by scanning `/run/user/*`, exactly as `vm/README.md` documents for
root over `vmctl ssh`. On a machine that *is* a running desktop, "no compositor" is
false. `tests/test_cli_parity.py` is left out for a different reason: it compares
against the real `xdotool` on `PATH` at import time, and the prepared images carry Ubuntu's
3.20160805.1 where parity is claimed against 4.20260303.1 (*Version note* in
`vm/README.md`), so on a golden it fails at collection instead of skipping.

So the division of labour is: the suite on the host, and the guest for what only it
has, a real session of a real desktop. That is `vm/vmctl user <name> -- python3 -m
wxrandr --query` and friends, the `repro/` scripts, and the per-flavor measurements
under *What the five tools do on each flavor* in `vm/README.md`.

## Housekeeping

Instances are disposable and prepared images are not; everything under `~/vm-data/instances`
can be recreated in half a minute from the golden it overlays.

* `vm/vmctl list` shows every instance and build directory with its state, pid, port
  and heads; `du -sh ~/vm-data/*` shows where the space went. A stopped instance
  still holds its directory (tens to a few hundred MB) and its ssh port; `vm/vmctl destroy <name>`
  frees both.
* `vm/selftest.sh` creates `<flavor>-t` every time and leaves it running; `destroy`
  it when done, and clear `/tmp/vmctl-selftest-*` now and then.
* A failed build leaves `~/vm-data/build/<flavor>/` (up to a golden's worth of disk)
  for inspection; `vmctl list` shows it as `(build)`. Delete the directory once you
  have read `serial.log`.
* Rebuilding a golden (`build --force`) replaces the backing file of every instance
  on it; restart those with `--fresh` (or destroy them first).
* After a host reboot nothing needs cleaning: pidfiles are checked against `/proc`
  and a stale one is removed on the next `vmctl` command; the private `dbus-daemon`
  of each instance is restarted with it.
* `~/vm-data/golden/*.build.log` and `*-packages.txt` are small (under 300 KB) and
  worth keeping: they say what each image contains and how it was made.
* Nothing here runs as root or leaves anything outside `$VMDATA`, `$VMIMAGES` and
  `/tmp`, so retiring the rig is `rm -rf ~/vm-data ~/images` after `vm/vmctl destroy`
  of whatever is running.
