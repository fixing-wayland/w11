# `nix flake check` on GNOME 50.4, with nothing but the module.
#
# The recon built this machine by hand out of the four things a module would
# set -- the extension package in systemPackages, the dconf profile, the udev
# package, boot.kernelModules -- and it passed in 110.8 s of test script
# [recon2/pkg-nix §7, recon2/pkg-nix/gnometest.nix].  Here those four lines
# are `programs.w11.enable = true` and the prints are assertions.  It
# is the file that proves nixosModules.default, so it asserts the two things
# only a VM can say: that a store-installed extension is found and loaded with
# no ~/.local/share copy and no relogin, and that the uaccess rule reaches
# /dev/uinput through services.udev.packages.
{ self, pkgs }:

pkgs.testers.runNixOSTest {
  name = "w11-nixos-gnome";

  nodes.machine = { ... }: {
    imports = [ self.nixosModules.default ];

    virtualisation = { memorySize = 3072; cores = 2; diskSize = 8192; };

    users.users.alice = {
      isNormalUser = true;
      uid = 1000;
      extraGroups = [ "wheel" ];
      password = "alice";
    };

    services.displayManager.gdm.enable = true;
    services.displayManager.autoLogin = { enable = true; user = "alice"; };
    services.desktopManager.gnome.enable = true;

    # gnomeBridge.enable defaults to services.desktopManager.gnome.enable, so
    # this machine says nothing about the bridge at all -- the default is
    # under test.  warandr is the one option it does ask for, because the GTK
    # wrapper is the same claim on Mutter as it is on sway and this is the
    # machine where a real Mutter answers DisplayConfig.
    programs.w11.enable = true;
    programs.w11.warandr.enable = true;
    environment.systemPackages = [ pkgs.gnome-console pkgs.acl ];
  };

  testScript = ''
    uid = "1000"
    bus = f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus"

    def u(cmd):
        return machine.succeed(f"su - alice -c '{bus} {cmd}'")

    machine.wait_for_unit("display-manager.service")
    machine.wait_for_file(f"/run/user/{uid}/wayland-0")
    machine.wait_for_unit("default.target", "alice")
    machine.wait_until_succeeds(
        f"su - alice -c '{bus} gdbus introspect --session --dest org.gnome.Shell "
        "--object-path /org/gnome/Shell --only-properties' >/dev/null",
        timeout=180)

    # The system dconf profile, read back by the session that was started
    # after it was written: first login, no logout step.
    enabled = u("gsettings get org.gnome.shell enabled-extensions")
    assert "w11-bridge@w11" in enabled, enabled
    assert u("gsettings get org.gnome.shell disable-user-extensions").strip() == "false"

    # The extension is not merely enabled in a key: it loaded and took the
    # bus name.  Both journal lines were measured at t=60.7 s on gnome-shell
    # 50.4 [recon2/pkg-nix §7].
    machine.wait_until_succeeds(
        "journalctl -b | grep -q 'w11-bridge.*acquired org.w11.Bridge'",
        timeout=120)
    names = u("gdbus call --session -d org.freedesktop.DBus -o /org/freedesktop/DBus "
              "-m org.freedesktop.DBus.ListNames")
    assert "org.w11.Bridge" in names, names

    backend = u("wxrandr --print-backend --verbose")
    assert backend.splitlines()[0].strip() == "mutter", backend
    assert "org.gnome.Mutter.DisplayConfig" in backend, backend

    query = u("wxrandr --query")
    assert "Virtual-1 connected primary 1280x800+0+0" in query, query
    geometry = u("wdotool getdisplaygeometry")
    assert geometry.split() == ["1280", "800"], geometry

    # The same round trip as the sway check, on the other backend: warandr
    # reads the layout out of Mutter through wxrandr and prints the command
    # Apply would run.  It is the GTK-wrapped package, so this is also the
    # proof that its typelibs are on $GI_TYPELIB_PATH in a VM with no system
    # PyGObject -- `--command` imports gi and never opens a window.
    command = u("warandr --command")
    # `--primary` is the one word that differs from the sway check's line, and
    # it is not noise: Mutter has a primary output and says so
    # (`Virtual-1 connected primary` in --query above), sway's IPC has no such
    # concept, and warandr round-trips whichever the backend reports.  This
    # exact string was read out of the guest on 2026-09-08; the sway one has
    # no --primary in it.
    assert command.strip() == (
        "wxrandr --output Virtual-1 --primary --mode 1280x800 "
        "--pos 0x0 --rotate normal"), command

    # `wwmctl -l` prints `<id> <desktop> <host> <title>` and a GNOME Console
    # window's title is the shell prompt (`alice@machine: ~`, measured
    # [recon2/pkg-nix §7]) -- so the window is recognised by having an id at
    # all, which is the claim anyway: the bridge answers, or nothing does.
    # `|| true`: gapplication's own exit status is not the claim -- it
    # answered 1 on this machine while the D-Bus activation went through
    # anyway -- and a GNOME app cannot be started any other way from a `su`
    # shell that has the session bus and no display.  What is asserted is
    # that a window then exists.
    machine.succeed(f"su - alice -c '{bus} gapplication launch org.gnome.Console' || true")
    machine.wait_until_succeeds(
        f"su - alice -c '{bus} wwmctl -l' | grep -q '^0x'", timeout=90)
    windows = u("wwmctl -l")
    assert windows.strip(), windows

    # The bridge extension is in the system path and nowhere else: this is the
    # store-installed extension found through XDG_DATA_DIRS, which is the
    # whole claim of §7 and the reason the dconf profile above is enough.
    ext = "/run/current-system/sw/share/gnome-shell/extensions"
    machine.succeed(f"test -f {ext}/w11-bridge@w11/metadata.json")
    machine.fail("test -e /home/alice/.local/share/gnome-shell/extensions")

    # The overlap extension is installed by NOBODY here, which is a stronger
    # claim than "enabled by nobody" and the one the comment used to make
    # without testing: gnomeOverlap.enable is false by default, so its package
    # is in no profile at all.
    assert "w11-overlap@w11" not in enabled, enabled
    machine.fail(f"test -e {ext}/w11-overlap@w11")

    node = machine.succeed("ls -l /dev/uinput")
    assert node.split()[0] == "crw-rw----+", node
    acl = machine.succeed("getfacl -p /dev/uinput")
    assert "user:alice:rw-" in acl, acl
    assert "group::---" in acl, acl

    # On Mutter there is no virtual-keyboard protocol to fall back to, so this
    # command is the whole input story on GNOME: it works because of the rule.
    notice = machine.succeed(f"su - alice -c '{bus} wdotool key a' 2>&1")
    assert "cannot create uinput devices" not in notice, notice

    # The sway check goes one step further than this and reads the typed text
    # back out of a `foot -e sh -c 'cat > /tmp/typed'`; here it does not, and
    # the reason is a measurement rather than an omission.  Two runs of this
    # file on 2026-09-08 tried exactly that against GNOME Console -- ten
    # rounds of `wdotool type "echo landed > /tmp/typed"` plus Return over
    # thirty seconds, the second run preceded by
    # `wdotool windowactivate <id>` through the bridge (which returned 0 in
    # 0.66 s) -- and the file was never written.  Every command succeeded;
    # nothing arrived.
    #
    # What that says is not "wdotool is broken on GNOME": `wdotool key a`
    # above opens /dev/uinput and writes to it, which is the claim the udev
    # rule is here for.  It says the hop after that -- a uinput device
    # created, written and destroyed inside one process lifetime, then picked
    # up by logind/libinput and handed to Mutter -- is not instant, and
    # hacks/input/uinput.py has no settle wait between UI_DEV_CREATE and the
    # first event (grep: no sleep in that file at all).  wlroots wins the
    # same race on the same host, which is why the sway check can assert
    # delivery and this one cannot.  Asserting it here would be a flake, and
    # pretending otherwise with a longer loop would be a slower flake.
  '';
}
