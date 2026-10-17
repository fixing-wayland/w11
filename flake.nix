{
  description = "w11 - the X11 power tools (xdotool, wmctrl, xprop, xrandr) as drop-in clones for Wayland";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      version = "0.4.0";
      systems = [ "x86_64-linux" "aarch64-linux" ];

      # legacyPackages, with no config of its own.  There was an
      # `allowUnfreePredicate` here for as long as meta.license was
      # lib.licenses.unfree, and it had to be: nix refuses to EVALUATE an
      # unfree package, so a nixosSystem carrying the module answered
      # `Refusing to evaluate package 'w11-0.4.0' ... because it has
      # an unfree license` and nothing built.  LICENSE exists now
      # (BSD-2-Clause, SPDX line first), meta.license is lib.licenses.bsd2,
      # and a consumer needs no nixpkgs config at all to use this flake.
      pkgsFor = system: nixpkgs.legacyPackages.${system};

      forAll = f: nixpkgs.lib.genAttrs systems (s: f (pkgsFor s));

      # Every measurement behind the checks below was taken on x86_64-linux,
      # and a NixOS VM test on a foreign architecture is emulation, not a
      # test.  The packages build on both; the checks say only what was run.
      onX86 = f: { x86_64-linux = f (pkgsFor "x86_64-linux"); };
    in
    {
      packages = forAll (pkgs:
        let
          # `import`, not callPackage: callPackage adds `override` and
          # `overrideDerivation` to whatever it returns, and this returns a
          # plain attrset of packages, so those two would show up in
          # `nix flake show` as outputs that are not packages.
          w11pkgs = import ./nix/package.nix {
            inherit (pkgs)
              lib runCommand writeText python3Packages gobject-introspection
              wrapGAppsHook3 gsettings-desktop-schemas gtk3;
            src = ./.;
            inherit version;
          };
        in
        w11pkgs // {
          default = w11pkgs.w11;

          # scripts/parity-oracle.sh has told the reader to build the oracles
          # with `nix build .#xdotool` / `.#wmctrl` since it was written, and
          # neither attribute existed: `nix build .#xdotool` answered
          # `does not provide attribute ... Did you mean wdotool?`
          # [recon2/pkg-nix §1 defect 2].  These two lines make the sentence
          # true from this side.  They are nixpkgs' packages, unmodified, and
          # they are the generations the parity files are written against:
          # xdotool 4.20260303.1 (wdotool.cli.XDO_VERSION) and the plain
          # wmctrl 1.07 whose --help is 6801 bytes, both read out of the
          # locked nixos-unstable [recon2/pkg-nix §1].
          inherit (pkgs) xdotool wmctrl;
        });

      # `nix run github:fixing-wayland/w11` was one tool, and only one.  With
      # no `apps` output, `nix run` falls through to packages.default and runs
      # its meta.mainProgram, which is a single name (wdotool, nix/package.nix);
      # `nix run .#wwmctl` answered `does not provide attribute
      # 'apps.x86_64-linux.wwmctl', 'packages.x86_64-linux.wwmctl' or 'wwmctl'`
      # (measured on nix 2.34.8, 2026-10-16), because the four other CLI
      # scripts live in bin/ of packages.w11 under its one pname and nothing
      # named them.  One app per console script of pyproject.toml
      # [project.scripts], so every tool is `nix run github:fixing-wayland/w11#<tool>`
      # -- the six from packages.w11, and warandr from packages.warandr with
      # its GTK wrapping behind it.  `default` stays wdotool, which keeps the
      # README's `nix run ... -- --version` sentence true.  No app is named
      # `xdotool` or `wmctrl`: `nix run .#xdotool` resolves apps before
      # packages, and those two names are the nixpkgs ORACLES
      # (packages.xdotool/wmctrl, above) that scripts/parity-oracle.sh builds.
      apps = nixpkgs.lib.genAttrs systems (system:
        let
          p = self.packages.${system};
          pkgs = pkgsFor system;
          app = pkg: name: description: {
            type = "app";
            program = "${pkg}/bin/${name}";
            meta = { inherit description; };
          };
          cli = app p.w11;
        in
        {
          wdotool = cli "wdotool" "xdotool, for Wayland";
          wwmctl = cli "wwmctl" "wmctrl, for Wayland";
          wxprop = cli "wxprop" "xprop, for Wayland";
          wxrandr = cli "wxrandr" "xrandr, for Wayland";
          wmirror = cli "wmirror" "mirror one output onto another";
          xw11 = cli "xw11" "the X11 protocol proxy in front of Xwayland";
          warandr = app p.warandr "warandr" "arandr, for Wayland (GTK 3)";
          default = cli "wdotool" "xdotool, for Wayland";
          # gnome/install-bridge.sh, runnable with nothing checked out: on a
          # GNOME desktop the clones refuse without the bridge extension, and
          # the refusal used to tell a `nix run` user to run a script they had
          # nowhere (2026-10-17, Ubuntu GNOME 50.1).  The script finds the
          # extension beside itself, so the store copy of gnome/ is enough;
          # bin/ of packages.w11 goes first on PATH for `--try-unsafe`, which
          # drives Looking Glass through wdotool.  The desktop's own
          # gnome-extensions, gsettings and gdbus are taken from the host, as
          # the script documents.  Not for NixOS, where the module's
          # programs.w11.gnomeBridge.enable installs the same extension
          # system-wide and turns it on for every user.
          install-bridge = {
            type = "app";
            program = "${pkgs.writeShellScript "w11-install-bridge" ''
              PATH=${p.w11}/bin:$PATH exec ${./gnome}/install-bridge.sh "$@"
            ''}";
            meta.description = "install and enable the GNOME Shell bridge extension for this user";
          };
        });

      # `{ self }` rather than the packages themselves: a consumer whose flake
      # sets `inputs.w11.inputs.nixpkgs.follows` gets the module built
      # against THEIR nixpkgs, because self.packages is evaluated at their
      # system's attribute.  Both live NixOS releases (25.11 and 26.05) build
      # `.#default` unchanged, measured with --override-input [recon2/nixos].
      nixosModules.default = import ./nix/module.nix { inherit self; };
      homeManagerModules.default = import ./nix/home-manager.nix { inherit self; };

      checks = onX86 (pkgs: {
        nixos-sway = import ./nix/checks/nixos-sway.nix { inherit self pkgs; };
        nixos-gnome = import ./nix/checks/nixos-gnome.nix { inherit self pkgs; };
        nixos-kde = import ./nix/checks/nixos-kde.nix { inherit self pkgs; };
        module-eval = import ./nix/checks/module-eval.nix { inherit self nixpkgs pkgs; };
        tools = import ./nix/checks/tools.nix { inherit pkgs version; src = ./.; };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            python3
            # in-sandbox compositor testbed (incl. XWayland legacy-app plane)
            sway foot grim jq
            xwayland xterm xprop xwininfo xeyes xrandr
            # the real things, for parity reference (help text, manpage, behavior)
            xdotool wmctrl man
            # VM lifecycle + demo gif
            qemu_kvm xorriso cloud-utils openssh curl
            ffmpeg imagemagick gifsicle
          ];
        };
      });
    };
}
