# nixos-gnome: GNOME 50.4 through GDM, with the bridge on.
#
# `# vmctl-desktop: gnome` is the DESKTOPS row.  Two things about it are NixOS
# facts rather than Ubuntu ones and both are already handled by vm/vmctl:
#
#   * the display-manager unit is `display-manager.service`, not `gdm.service`
#     -- nixpkgs' gdm module sets systemd.services.gdm.enable = false
#     (gdm.nix:287) and defines display-manager instead [recon2/nixos §7.2];
#     the DESKTOPS `dm` field is used in cmd_session's failure hint only.
#   * gnome-shell here is a WRAPPED program, so its /proc/comm is
#     `.gnome-shell-wr` -- fifteen characters, truncated -- and both halves of
#     PAINT_SCRIPT's gnome branch are comm matches.  vm/vmctl's pg() builds
#     that name from the plain one and matches either [recon2/nixos §6.1].
#
# The bridge is not named here on purpose: programs.w11.gnomeBridge
# defaults to services.desktopManager.gnome.enable, so this flavor is also the
# rig's test of that default.
{ pkgs, lib, ... }:

{
  services.displayManager.gdm.enable = true;
  services.displayManager.gdm.autoLogin.delay = 0;
  services.displayManager.autoLogin = { enable = true; user = "test"; };
  services.desktopManager.gnome.enable = true;
  services.getty.autologinUser = lib.mkForce null;

  # ---- the quiet GNOME the rig needs, which cost this flavor ten checks ----
  #
  # Every other GNOME golden is a cloud image dressed by vm/build-image.sh, and
  # its desktop_gnome arm (build-image.sh:557-583) does two things before the
  # first login: `gschema_quiet gnome` writes 90_vmctl.gschema.override -- no
  # lock, no blank, no idle sleep, and welcome-dialog-last-shown-version='999'
  # (build-image.sh:474-505) -- and it drops the gnome-initial-setup markers.
  # This flavor got neither, and CI run 34628777544 measured the bill:
  # nixos-gnome 63 pass, 10 FAIL, every `wdotool type` and `wdotool key` check
  # reading an EMPTY editor [goal2/recon/flavors.md §7], while `mousemove 300
  # 300 click 1`, the Super+Space source switch and `getfacl /dev/uinput`
  # (user ACL present) all passed in the same run -- injection worked, the keys
  # went somewhere else.
  #
  # Where: that run's live-smoke-nixos-gnome artefact, attempt
  # nixos-gnome-20260911-180712 -- the job log kept under goal2/ci is the
  # 175927 attempt of the same run id, 63 pass 10 fail either way, and each
  # attempt's log prints its own shot paths, so pair a shot with the log
  # beside it.  windows-0.png and wm-0.png show gnome-shell's own first-login
  # welcome dialog ("Welcome to NixOS 26.05 (Yarara)" / "If you want to learn
  # your way around, check out the tour" / Skip | Take Tour) drawn over the
  # editor -- a shell ModalDialog, which holds the keyboard grab and keeps the
  # overview from hiding when the editor maps -- and by proxy-0.png and
  # input-0.png the text the smoke typed is sitting in the shell's OWN search
  # entry ("us: yz@"), not in ~/w11-smoke.txt, for the rest of a session that
  # never reboots before the udev phase.  arch-gnome, the same GNOME 50 with
  # build-image.sh's override, is 108 pass 0 fail [flavors.md §8], and
  # resolute-gnome-iso types byte-exact
  # [goal2/ci/rig-resolute-gnome-iso.log:561-567].
  #
  # dconf and not services.desktopManager.gnome.extraGSettingsOverrides: the
  # profile route is the one this image has already been measured taking --
  # nix/module.nix enables the bridge through programs.dconf.profiles.user and
  # the bridge was ACTIVE at the first login of that same run -- while the
  # override route recompiles the schemas with `glib-compile-schemas --strict`
  # and would need gnome-settings-daemon added to
  # extraGSettingsOverridePackages for the power block.  dconf validates no key
  # name, so these names are held to build-image.sh's list key for key by
  # tests/test_flake.py, and that list IS schema-checked on every other golden.
  # user-db:user still comes first in the profile (dconf.nix, enableUserDb
  # defaults true), so these are defaults a session may still overwrite.
  programs.dconf.profiles.user.databases = [{
    settings = {
      "org/gnome/shell" = {
        welcome-dialog-last-shown-version = "999";
      };
      "org/gnome/desktop/screensaver" = {
        lock-enabled = false;
        idle-activation-enabled = false;
      };
      "org/gnome/desktop/session" = {
        idle-delay = lib.gvariant.mkUint32 0;
      };
      "org/gnome/settings-daemon/plugins/power" = {
        sleep-inactive-ac-type = "nothing";
        sleep-inactive-battery-type = "nothing";
        idle-dim = false;
      };
    };
  }];

  # The other half of desktop_gnome, said in nix: build-image.sh writes
  # ~/.config/gnome-initial-setup-done and hides the first-login autostart so
  # no setup wizard ever comes up in front of the editor.  On NixOS the module
  # wants gnome-initial-setup-first-login.service from gnome-session and writes
  # that stamp file only for a stateVersion older than 20.03 (nixpkgs
  # gnome-initial-setup.nix:20-44, 78-82); ours is 26.05, so the unit would run
  # at the first login of every golden.  Off is the whole fix, and it is the
  # option nixpkgs itself defaults (mkDefault true, gnome.nix:386).
  services.gnome.gnome-initial-setup.enable = false;

  environment.systemPackages = with pkgs; [ gnome-console ];
}
