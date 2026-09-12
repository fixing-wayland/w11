# What every NixOS rig flavor is, minus the desktop.
#
# The other flavors are a cloud image plus cloud-init user-data: `vmctl build`
# boots the base, cloud-init makes the users and drops the ssh key in, and
# vm/build-image.sh installs a desktop with apt.  None of that applies here.
# NixOS publishes no cloud image at all -- the 26.05 release directory has two
# ISOs, nixexprs.tar.xz and nothing else [recon2/nixos §7.1] -- and NixOS runs
# no cloud-init, so the seed `vmctl start` attaches (vm/vmctl qemu_common) is
# read by nobody in the guest.  Everything cloud-init would have done is
# therefore baked into the image, and the root ssh key is the one thing that
# cannot be committed: it comes in through the environment, see rootKey below.
#
# Every option name here was verified against nixos-26.05 [recon2/nixos §7.2].
{ config, pkgs, lib, modulesPath, ... }:

let
  # vm/build-nixos-golden.sh exports VMCTL_ROOT_PUBKEY from
  # $VMDATA/keys/id_ed25519.pub and builds with --impure.  getEnv is the only
  # way a committed file can carry a key that is generated per rig host: the
  # recon's flake baked its public key literally, which works exactly once and
  # for one machine [recon2/nixos/rig/flake.nix].  An empty value is an error
  # rather than an image nobody can ssh into.
  rootKey = builtins.getEnv "VMCTL_ROOT_PUBKEY";
in
{
  imports = [ "${modulesPath}/profiles/qemu-guest.nix" ];

  assertions = [{
    assertion = rootKey != "";
    message = ''
      VMCTL_ROOT_PUBKEY is empty: this image would have no way in.  Build it
      with vm/build-nixos-golden.sh, which exports it from
      $VMDATA/keys/id_ed25519.pub and passes --impure.
    '';
  }];

  # image.modules.qemu is nixpkgs' in-tree BIOS qcow2 image
  # (nixos/modules/image/images.nix -> virtualisation/disk-image.nix with
  # image.efiSupport = false): grub on /dev/vda, ext4 root labelled nixos.
  # nixos-generators is not needed and is not used.  Legacy BIOS because
  # vm/vmctl's qemu line has no OVMF in it.
  image.modules.qemu = { };
  virtualisation.diskSize = 12288;   # MiB; peak build dir ~9.2 GiB real

  # vmctl reads the guest's console out of `-serial file:`, and `vmctl session`
  # times the first frame off it.  timeout 0 because nobody is at the grub menu.
  boot.kernelParams = [ "console=ttyS0,115200n8" "console=tty0" ];
  boot.loader.timeout = lib.mkForce 0;

  services.openssh.enable = true;
  services.openssh.settings.PermitRootLogin = "prohibit-password";
  users.users.root.openssh.authorizedKeys.keys = [ rootKey ];

  # uid 1000 and the password `test`, the same user every other flavor's
  # cloud-config makes, because vm/selftest.sh, vm/live-smoke.sh and the
  # DESKTOPS paint scripts all reach for `test` by name.
  users.users.test = {
    isNormalUser = true;
    uid = 1000;
    description = "Test User";
    extraGroups = [ "wheel" "video" "input" ];
    password = "test";
  };
  security.sudo.wheelNeedsPassword = false;
  services.getty.autologinUser = lib.mkDefault "root";

  # The tools under test, plus the X11 originals the handover hands to and the
  # oracles vm/selftest.sh reads: swaymsg/gdbus come with their desktops,
  # wlr-randr is the independent reader for the wlroots family.
  programs.w11 = {
    enable = true;
    x11Tools.enable = true;
    wlMirror.enable = true;
    # The .deb ships warandr and its .desktop; a rig image that did not would
    # be a picture of a different package.  It is the option and not the
    # package by hand because that is the route a user takes.
    warandr.enable = true;
  };
  environment.systemPackages = with pkgs; [
    foot xterm grim jq wlr-randr acl
    # python3 for the smoke's OWN second opinion, not for the tools (they are
    # wrapped and carry their interpreter).  vm/live-smoke.d/common.sh:77 runs
    # `python3 $HOME/w11-oracle.py <desktop>` for every display check, and on a
    # NixOS golden the interpreter is in the store but on nobody's PATH: CI run
    # 34628777544 started nixos-gnome with three heads and the display phase
    # printed `native oracle: sh: line 1: python3: command not found` and then
    # `one head only: the two-head steps need --heads 2` -- oracle.py answering
    # nothing makes display_pair() report one head, so the whole two-head half
    # of the phase was skipped on both NixOS flavors, silently, on every run.
    python3
  ];

  # Two specialisations, and this is what they buy: the smoke's package axis
  # on every other distro is `apt-get remove` / `dnf remove` / `pacman -R` and
  # a re-install, which has no meaning on NixOS -- the package is IN the image.
  # `switch-to-configuration test` into without-w11 reloads the udev
  # rules and swaps the system path in one step, offline, with no rebuild, and
  # switching back is the re-install.  inheritParentConfig defaults true, so
  # the only difference is the option below.
  specialisation.without-w11.configuration = {
    programs.w11.enable = lib.mkForce false;
  };

  system.stateVersion = "26.05";
}
