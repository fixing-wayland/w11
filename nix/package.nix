# The .deb's payload, as nix outputs.
#
# One derivation shipped everything until now: `packages.default` was a
# buildPythonApplication with gobject-introspection, wrapGAppsHook3, gtk3 and
# pygobject3 in it, and its $out held the tools, five shadow symlinks and
# no share/ at all -- no extension, no udev rule, no warandr.desktop
# (measured: 546.2 MiB closure, 139 store paths, `find -maxdepth 4 -type d`
# naming only lib/python3.14/site-packages [recon2/pkg-nix §1]).  Two facts
# broke that arrangement:
#
#   * the four CLI clones are stdlib and import none of the GTK stack, which
#     is 330.9 MiB of the closure: the same source without
#     gobject-introspection/wrapGAppsHook3/gtk3/pygobject3 measures 215.1 MiB
#     against 546.2 with [recon2/nixos §8], and the split built here comes out
#     at 216.0 MiB for packages.w11 and 546.9 for packages.warandr
#     (`nix path-info -Sh`, 2026-09-08).  Only warandr needs `gi`.
#   * the five shadow symlinks made `packages.default` collide with
#     xdotool/xorg.xprop/xorg.xrandr/arandr in environment.systemPackages.
#     system-path builds with ignoreCollisions = true, so nothing fails --
#     the ORIGINALS win silently, and /run/current-system/sw/bin/xdotool
#     resolved to xdotool-3.20211022.1 on a Wayland session [recon2/nixos].
#     They are their own package now, and `programs.w11.shadowOriginals`
#     is the lib.hiPrio that makes them win on purpose.
#
# Everything here is derived from `src`: the extensions, the udev rule and the
# .desktop file are the tree's own bytes, never a second copy carried in nix/.
{ lib
, runCommand
, writeText
, python3Packages
, gobject-introspection
, wrapGAppsHook3
, gsettings-desktop-schemas
, gtk3
, src
, version
}:

let
  homepage = "https://github.com/fixing-wayland/w11";

  commonMeta = {
    inherit homepage;
    # lib.platforms.linux, not the flake's old silence: every backend here is
    # a Wayland protocol, an X11 connection or /dev/uinput, and none of the
    # three exists on darwin or freebsd.
    platforms = lib.platforms.linux;
    # LICENSE at the repo root, whose first line is
    # `SPDX-License-Identifier: BSD-2-Clause`.  It was lib.licenses.unfree
    # while there was no such file, which is a marking nix enforces by
    # refusing to evaluate the package at all -- see flake.nix.
    license = lib.licenses.bsd2;
  };

  bridgeUuid = "w11-bridge@w11";
  overlapUuid = "w11-overlap@w11";

  # `@` is not a legal character in a nix path literal, so the extension
  # directories are never named as one: `../gnome/w11-bridge@w11`
  # does not parse, and interpolating `src + "/gnome/<uuid>"` copies that one
  # directory into the store under its own name, where nix refuses it --
  # `name 'w11-bridge@w11' contains illegal character '@'`.
  # So the whole source goes in and the shell walks into it.
  gnome = name: src + ("/gnome/" + name);

  mkExtension = { pname, uuid, description }:
    runCommand "${pname}-${version}"
      {
        passthru.extensionUuid = uuid;
        # nixpkgs' buildGnomeExtension convention, and the attribute
        # home-manager's programs.gnome-shell.extensions reads to default the
        # `id` of an entry it is given as `{ package = ...; }`
        # (modules/programs/gnome-shell.nix) [recon2/pkg-nix §3].
        meta = commonMeta // { inherit description; };
      }
      ''
        mkdir -p "$out/share/gnome-shell/extensions"
        cp -r ${src}/gnome/${uuid} "$out/share/gnome-shell/extensions/${uuid}"
        chmod -R u+w "$out/share/gnome-shell/extensions/${uuid}"
      '';

  base = {
    inherit version src;
    pyproject = true;
    build-system = [ python3Packages.setuptools ];
  };

  w11 = python3Packages.buildPythonApplication (base // {
    pname = "w11";

    # No GTK anywhere in this one.  bin/ is SIX of the seven console scripts of
    # pyproject.toml [project.scripts] -- the four clones, wmirror and xw11,
    # the X11 proxy the first four run the originals against: the shadow names
    # are packages.x11-shadows, the .desktop file comes with packages.warandr,
    # and so does bin/warandr.
    #
    # buildPythonApplication installs all seven -- [project.scripts] is what it
    # reads -- so warandr is removed here, and that removal is half of what
    # keeps the two installable packages disjoint (the other half is
    # packages.warandr carrying no lib/, below).  Left in, bin/warandr exists
    # twice on a machine that installs both, and this one is the copy with no
    # GTK behind it: it dies on `import gi` with warandr/cli.py:20's Debian
    # apt line.  environment.systemPackages would pick between the two
    # silently -- system-path's buildEnv has ignoreCollisions = true and keeps
    # whichever package the module list names first -- and home-manager's
    # home.path, which does not, would refuse to build at all.  Two packages,
    # no path in common, no order to get right: measured by building
    # `pkgs.buildEnv` over both WITHOUT ignoreCollisions, in both orders
    # (tests/test_flake.py:Live).

    meta = commonMeta // {
      description = "xdotool, wmctrl, xprop and xrandr as drop-in clones for Wayland, and the X11 proxy";
      # `nix run .` died with `unable to execute .../bin/w11: No such
      # file or directory` because pname is w11 and no script is
      # [recon2/pkg-nix §1 defect 1].
      mainProgram = "wdotool";
    };

    # postInstall and not postFixup: the script is removed BEFORE
    # wrapPythonPrograms walks $out/bin, so there is no `.warandr-wrapped`
    # left behind either.  (Removing both after the fact was the first
    # attempt and it fails the other way round -- at postInstall time the
    # wrapper does not exist yet: `rm: cannot remove
    # '.../bin/.warandr-wrapped': No such file or directory`.)
    postInstall = ''
      rm "$out"/bin/warandr
    '';
  });

  # The GUI, built whole -- six scripts, GTK wrapping, its own copy of the
  # Python tree -- and then never installed as it is.  packages.warandr below
  # is a bin/ and a .desktop pointing INTO this, and the reason is the same
  # collision the file's header is about, one level deeper than bin/: two
  # buildPythonApplications of the same source both install
  # lib/python3.14/site-packages/w11common, and `pkgs.buildEnv` without
  # ignoreCollisions -- which is what home-manager's home.path is -- refuses
  # the pair outright:
  #
  #   pkgs.buildEnv error: two given paths contain a conflicting subpath:
  #     `...-w11-0.4.0/lib/python3.14/site-packages/w11common/
  #      __pycache__/__init__.cpython-314.pyc' and
  #     `...-w11-warandr-app-0.4.0/lib/.../__init__.cpython-314.pyc'
  #
  # (measured here on 2026-09-08, building buildEnv over the two packages).
  # environment.systemPackages hides it -- ignoreCollisions = true -- by
  # picking one at random and moving on, which is how the .pyc of one build
  # would end up beside the wrapper of the other.  So only one of the two
  # installable packages carries lib/ at all.
  warandrApp = python3Packages.buildPythonApplication (base // {
    pname = "w11-warandr-app";

    # The GTK arrangement is the old flake's, unchanged, because it was right:
    # gobject-introspection collects every buildInput's typelib directory into
    # $GI_TYPELIB_PATH, wrapGAppsHook3 turns that plus the XDG_DATA_DIRS entry
    # for the GSettings schemas into wrapper arguments, gtk3 + schemas are what
    # there is to collect, and pygobject3 is `gi` itself.  dontWrapGApps stops
    # the hook at assembling $gappsWrapperArgs in preFixup so
    # buildPythonApplication's own wrapper (postFixup) carries them and nothing
    # is wrapped twice.  Measured working from the store, with a real window in
    # `wwmctl -lGx` and no system PyGObject involved [recon2/pkg-nix §2a].
    nativeBuildInputs = [ gobject-introspection wrapGAppsHook3 ];
    buildInputs = [ gsettings-desktop-schemas gtk3 ];
    dependencies = [ python3Packages.pygobject3 ];
    dontWrapGApps = true;
    makeWrapperArgs = [ "\${gappsWrapperArgs[@]}" ];

    # The old flake denied, over this phase and in as many words, that it
    # prefixed PATH at all, and that was not true: buildPythonApplication
    # prefixes PATH
    # regardless, and the built wrapper's makeCWrapper line reads
    # `--prefix 'PATH' ':' '...python3-3.14.7/bin:...w11-0.4.0/bin:
    # ...gobject-introspection-wrapped-1.86.0-dev/bin:...glib-2.88.3-dev/bin:
    # ...gettext-1.0/bin:...glib-2.88.3-bin/bin'` (read out of the compiled
    # wrapper [recon2/pkg-nix §1 defect 3]).  The intent survives for two
    # reasons and they are worth writing down, because the next package added
    # to buildInputs could end both: none of those six store bins carries an
    # xrandr/xdotool/wmctrl/xprop, so the handover still finds the user's own
    # (measured: the store wdotool exec'd /usr/bin/xdotool 3.20160805.1
    # [recon2/pkg-nix §2a]); and passthrough.is_us() guard 2 stops the loop if
    # one ever did.
    #
    # The six CLI scripts go: this package exists for the one script that
    # needs the 330.9 MiB, and a second copy of wdotool in $out/bin would be
    # a second wdotool with the whole GTK closure behind it.  xw11 is on the
    # list for the same reason and one more: two packages with a bin/xw11
    # would each spawn their own proxy for the session, and only one of them
    # would hold the display file.
    postInstall = ''
      rm "$out"/bin/wdotool "$out"/bin/wwmctl "$out"/bin/wxprop \
         "$out"/bin/wxrandr "$out"/bin/wmirror "$out"/bin/xw11
    '';

    meta = commonMeta // {
      description = "The Screen Layout Editor of arandr, over wxrandr (GTK 3)";
      mainProgram = "warandr";
    };
  });

  # ... and this is what gets installed: one symlink and one desktop entry,
  # disjoint from packages.w11 down to the last path.  The symlink
  # target is warandrApp's compiled C wrapper, which execs
  # `.warandr-wrapped` by ABSOLUTE path with $GI_TYPELIB_PATH and
  # $XDG_DATA_DIRS already set, so nothing is lost by reaching it through a
  # link.  The closure is warandrApp's -- the 546.9 MiB is still here, it is
  # just no longer in the way of the 216.0 MiB package.
  warandr = runCommand "w11-warandr-${version}"
    {
      passthru.app = warandrApp;
      meta = warandrApp.meta;
    }
    ''
      mkdir -p "$out/bin"
      ln -s ${warandrApp}/bin/warandr "$out/bin/warandr"
      install -D -m 644 ${src + "/warandr.desktop"} \
        "$out/share/applications/warandr.desktop"
    '';
in
{
  inherit w11 warandr;

  gnome-bridge = mkExtension {
    pname = "w11-gnome-bridge";
    uuid = bridgeUuid;
    description = "GNOME Shell extension: org.w11.Bridge, the window half of wwmctl on Mutter";
  };

  gnome-overlap = mkExtension {
    pname = "w11-gnome-overlap";
    uuid = overlapUuid;
    description = "GNOME Shell extension: the overlap route for wxrandr, installed and enabled for nobody";
  };

  # lib/udev/rules.d is where services.udev.packages looks
  # (nixos/modules/services/hardware/udev.nix), and lib/modules-load.d is
  # boot.extraModulePackages' shape; the module sets boot.kernelModules
  # instead, and ships this file anyway so the package says on its own what it
  # needs loaded.  The rule text is readFile'd out of gnome/, never retyped:
  # its comment is the threat model, and a second copy in nix/ would be the
  # copy that rots.  Measured through services.udev.packages on a NixOS GNOME
  # 50.4 guest: /dev/uinput came out `crw-rw----+ root root` with
  # `user:alice:rw-`, `group::---` -- the uaccess ACL and no standing group
  # channel [recon2/pkg-nix §7].
  udev-rules = runCommand "w11-udev-rules-${version}"
    { meta = commonMeta // { description = "uaccess rule and modules-load entry for /dev/uinput"; }; }
    ''
      install -D -m 644 \
        ${writeText "60-w11-uinput.rules"
          (builtins.readFile (gnome "60-w11-uinput.rules"))} \
        "$out/lib/udev/rules.d/60-w11-uinput.rules"
      install -D -m 644 \
        ${writeText "w11-uinput.conf"
          (builtins.readFile (gnome "modules-load-uinput.conf"))} \
        "$out/lib/modules-load.d/w11-uinput.conf"
    '';

  # The five names the README calls "installing over the originals".  On
  # Debian that is /usr/local/bin winning over /usr/bin; NixOS has no such
  # precedence, so the honest form is a package of its own and
  # `programs.w11.shadowOriginals = true`, which wraps it in
  # lib.hiPrio.  arandr points into packages.warandr because that is the only
  # output with a bin/warandr in it -- a shadow of arandr that refuses to
  # start would be worse than no shadow -- and that is why this package drags
  # the GTK closure in behind it.
  x11-shadows = runCommand "w11-x11-shadows-${version}"
    {
      meta = commonMeta // {
        description = "xdotool/wmctrl/xprop/xrandr/arandr, as links onto the clones";
      };
    }
    ''
      mkdir -p "$out/bin"
      ln -s ${w11}/bin/wdotool "$out/bin/xdotool"
      ln -s ${w11}/bin/wwmctl  "$out/bin/wmctrl"
      ln -s ${w11}/bin/wxprop  "$out/bin/xprop"
      ln -s ${w11}/bin/wxrandr "$out/bin/xrandr"
      ln -s ${warandr}/bin/warandr     "$out/bin/arandr"
    '';
}
