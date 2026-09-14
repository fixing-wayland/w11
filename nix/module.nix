# nixosModules.default -- what the .deb's postinst does, said in nix.
#
# The .deb is six things (debian/w11.install, .links, .postinst and
# the enabler): the tools in /usr/bin, the bridge extension in
# /usr/share/gnome-shell/extensions, that extension enabled per user at the
# next login, the udev rule plus modules-load.d applied without a reboot, the
# warandr .desktop, and the overlap extension installed but enabled for
# nobody.  Every one of those has an exact NixOS expression, and all of them
# were measured working together in a NixOS 26.05 GNOME 50.4 VM test before
# this file existed [recon2/pkg-nix §7]: the store-installed extension was
# found through XDG_DATA_DIRS and loaded ("[w11-bridge] enabled
# (bridge v3, gnome-shell 50.4)" and "acquired org.w11.Bridge" in the
# guest journal at t=60.7 s), and `wdotool key a` took the /dev/uinput path
# with no fallback notice.
{ self }:
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.w11;
  w11pkgs = self.packages.${pkgs.stdenv.hostPlatform.system};
in
{
  options.programs.w11 = {
    enable = lib.mkEnableOption "the w11 tools (wdotool, wwmctl, wxprop, wxrandr, wmirror, xw11)";

    package = lib.mkOption {
      type = lib.types.package;
      default = w11pkgs.w11;
      defaultText = lib.literalExpression "w11.packages.\${system}.w11";
      description = ''
        The stdlib CLI package -- six of the seven tools, no GTK.  `warandr`
        (the one GUI) is a package of its own for the 330.9 MiB of closure it
        needs (216.0 MiB against 546.9 MiB, both built from this tree), and
        the two `bin/` directories are disjoint so that nothing has to be
        installed in the right order; {option}`programs.w11.warandr.enable`
        is how you ask for it.
      '';
    };

    warandr.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Also install `warandr`, the GTK Screen Layout Editor, and its
        `.desktop` entry.  Off by default because it is the package that
        carries gtk3 and pygobject3 -- 330.9 MiB of closure for one script,
        and nothing else in this project imports `gi`.

        This is a separate package and not a separate `bin/` name in
        {option}`programs.w11.package`: the two would collide in
        {option}`environment.systemPackages`, where the system path is built
        with `ignoreCollisions = true` and the winner is whichever module
        happens to be earlier in the list.
      '';
    };

    uinput.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Ship the project's own uaccess rule for `/dev/uinput` and load the
        `uinput` module at boot, so `wdotool`'s input injection works for the
        user at the seat without root.  On NixOS `/dev/uinput` is
        `crw------- root root` with no rule at all until something sets one
        (measured in a stock NixOS sway VM [recon2/pkg-nix §2c]).

        This is deliberately NOT {option}`hardware.uinput.enable`: that module
        is `MODE="0660", GROUP="uinput"` plus a `uinput` group, which is a
        standing keystroke-injection channel for every member of that group
        into the seat user's session, logged in or not.  The rule shipped here
        leaves the node root:root 0600 and lets logind put an ACL on it for
        the active session only.  Turning both on is refused by an assertion.
      '';
    };

    gnomeBridge.enable = lib.mkOption {
      type = lib.types.bool;
      default = config.services.desktopManager.gnome.enable;
      defaultText = lib.literalExpression "config.services.desktopManager.gnome.enable";
      description = ''
        Install the GNOME Shell bridge extension and enable it for every user
        through {option}`programs.dconf.profiles.user.databases`.  It is what
        gives `wwmctl` and `wdotool`'s window half anything to talk to on
        Mutter; it is inert on every other desktop, which is why the default
        follows GNOME.

        The system profile is written before the session starts, so the
        extension is enabled at the FIRST login and the .deb's "log out and
        back in once" banner has no NixOS equivalent (measured: the shell
        loaded it on the first boot of the VM test).  A `user-db:user` entry
        is not a lock: a user can still turn the extension off.
      '';
    };

    gnomeOverlap.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Install the overlap extension -- the route `wxrandr` uses to place
        overlapping outputs on Mutter.  It installs it and enables nothing,
        the same policy as every other packaging of this project: the .deb
        ships it and `gnome/install-overlap.sh` is a deliberate second step.
      '';
    };

    x11Tools.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Add the real `xdotool`, `wmctrl`, `xprop` and `xrandr` to the system
        path, so the handover has something to hand an X11 session to.  With
        none of them installed a NixOS session answers
        `x11  unavailable  no real xrandr on PATH (install x11-xserver-utils)`
        -- measured in the sway VM test [recon2/pkg-nix §2c].  Off by default
        because a Wayland-only machine needs none of them.
      '';
    };

    shadowOriginals = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Install `xdotool`, `wmctrl`, `xprop`, `xrandr` and `arandr` as links
        onto the clones, at {var}`lib.hiPrio`, so they win over the real ones
        in the system path.  This is the NixOS form of the README's
        "installing over the originals": there is no /usr/local/bin
        precedence here, and without hiPrio the system-path builder resolves
        the collision in the ORIGINALS' favour without a word -- measured,
        `/run/current-system/sw/bin/xdotool` was xdotool-3.20211022.1 on a
        Wayland session [recon2/nixos].
      '';
    };

    proxy.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Start `xw11 --foreground` with the graphical session, as a systemd user
        service, instead of letting the first `xdotool`/`wmctrl`/`xprop`/
        `xrandr` call start it on demand.

        Off by default because nothing needs it: the wrapper spawns the proxy
        itself on the first call (`xw11/wrap.py`), nothing is left behind on a
        session that never runs an X tool, and Xwayland keeps its own
        `-terminate` behaviour there.  Turn it on for the one thing on-demand
        cannot give you: shadow window ids that are stable for the whole
        session.  A proxy that started on demand exits fifteen minutes after
        its last client (`_IDLE_SECONDS`), and the ids it minted mean nothing
        to the next one -- so a script that saves an id, sleeps an hour and
        acts on it wants a proxy that never idles.  The cost is one process and
        one Xwayland pinned for the whole session.

        The service waits for the session's X server rather than giving up on
        it: on GNOME and KWin, Xwayland starts on demand and may not exist when
        `graphical-session.target` is reached, so the unit retries every five
        seconds with no start limit.
      '';
    };

    wlMirror.enable = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Install `wl-mirror`, the helper `wmirror` drives.  nixpkgs' is 0.18.5,
        the exact generation `wmirror --check` recognises [recon2/pkg-nix §1].
      '';
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [
    {
      assertions = [
        {
          assertion = !(cfg.uinput.enable && config.hardware.uinput.enable);
          message = ''
            programs.w11.uinput.enable and hardware.uinput.enable are
            two different grants on the same node and the weaker one wins:
            nixpkgs' module writes MODE="0660" GROUP="uinput", which hands
            every member of that group a standing input channel into whoever
            is at the seat, while this project's rule leaves /dev/uinput
            root:root 0600 with a logind uaccess ACL for the active session.
            Pick one: set programs.w11.uinput.enable = false to keep
            the group, or hardware.uinput.enable = false to keep the ACL.
          '';
        }
      ];

      environment.systemPackages =
        [ cfg.package ]
        ++ lib.optional cfg.warandr.enable w11pkgs.warandr
        ++ lib.optional cfg.shadowOriginals (lib.hiPrio w11pkgs.x11-shadows)
        ++ lib.optional cfg.gnomeBridge.enable w11pkgs.gnome-bridge
        ++ lib.optional cfg.gnomeOverlap.enable w11pkgs.gnome-overlap
        ++ lib.optional cfg.wlMirror.enable pkgs.wl-mirror
        # `pkgs.xprop or pkgs.xorg.xprop`: nixos-unstable has moved the X
        # clients out of the xorg set ("The xorg package set has been
        # deprecated, 'xorg.xprop' has been renamed to 'xprop'") while
        # 25.11 and 26.05, which a consumer may well be on, have only the
        # old spelling.  The module is evaluated against THEIR nixpkgs, so
        # it takes whichever exists.
        ++ lib.optionals cfg.x11Tools.enable [
          pkgs.xdotool
          pkgs.wmctrl
          (pkgs.xprop or pkgs.xorg.xprop)
          (pkgs.xrandr or pkgs.xorg.xrandr)
        ];
    }

    (lib.mkIf cfg.uinput.enable {
      services.udev.packages = [ w11pkgs.udev-rules ];
      # The rule's own static_node=uinput creates the node before the module
      # is loaded, but modules-load.d exists in the .deb for a reason: the
      # node then carries the permissions and the uaccess ACL from the first
      # login on rather than from the first open().
      boot.kernelModules = [ "uinput" ];
    })

    (lib.mkIf cfg.proxy.enable {
      # A user service and not an autostart .desktop entry: NixOS has no
      # /etc/xdg/autostart hook that every desktop reads, and the proxy has to
      # be there before the session's first X tool runs.  `--foreground`
      # because systemd owns the lifetime here; the on-demand spawn's double
      # fork would leave systemd with nothing to supervise.
      systemd.user.services.xw11 = {
        description = "xw11: an X11 display in front of the session's X server";
        partOf = [ "graphical-session.target" ];
        after = [ "graphical-session.target" ];
        wantedBy = [ "graphical-session.target" ];
        serviceConfig = {
          ExecStart = "${cfg.package}/bin/xw11 --foreground";
          # The proxy refuses to start when nothing answers $DISPLAY and no
          # Xwayland is running (xw11/cli.py:_refusal), and on GNOME and KWin
          # that is the NORMAL state at graphical-session.target: both start
          # Xwayland on demand, so the X server may be minutes away.  Exit 1
          # then, and systemd's default rate limit (5 starts in 10 s) would put
          # the unit in `failed` before the session's first X tool ever ran.
          # Five seconds between tries and no limit on the number: the unit
          # waits for Xwayland instead of giving up on it.
          Restart = "on-failure";
          RestartSec = 5;
        };
        # Unit-level and not in serviceConfig: systemd moved the two start-limit
        # settings out of [Service] in v229 and NixOS spells them here.
        startLimitIntervalSec = 0;
      };
    })

    (lib.mkIf cfg.gnomeBridge.enable {
      # The GNOME module turns dconf on itself, so on the machine this option
      # defaults true for, this line changes nothing.  mkDefault, and here at
      # all, so that turning the bridge on by hand on a non-GNOME machine
      # writes a profile something reads rather than one nothing does.
      programs.dconf.enable = lib.mkDefault true;
      programs.dconf.profiles.user.databases = [{
        settings."org/gnome/shell" = {
          disable-user-extensions = false;
          enabled-extensions = [ w11pkgs.gnome-bridge.passthru.extensionUuid ];
        };
      }];
    })
  ]);
}
