# w11.spec -- the Fedora counterpart of debian/.
#
# An RPM of this tree is a translation, not a port: every payload path the .deb
# ships exists on Fedora under the same name (/usr/share/gnome-shell/extensions/
# <uuid>, /usr/lib/udev/rules.d, /usr/lib/modules-load.d, /etc/xdg/autostart),
# and the two dpkg maintainer scripts translate line for line into %post and
# %postun.  Measured 2026-09-08 on the Ubuntu 26.04 guest, which has the same
# rpm 6.0.1 Fedora 44 has: a portable variant of this file (the %%pyproject_*
# steps replaced by a hand copy, because those macros are Fedora-only and
# rpmbuild here dies at "%%pyproject_buildrequires: not found") built three
# noarch packages -- w11 556,673 B, -bridge 28,507 B, -overlap
# 32,388 B -- which installed into a scratch root and erased again leaving
# nothing under /usr or /etc but empty directories [recon2/pkg-rpm.md 4].
#
# Everything below that differs from debian/ differs for a measured reason and
# says which.  The first real build with the %%pyproject_* macros is the CI
# `rpm` job in fedora:44; tests/test_rpm_spec.py and tests/test_rpm_scripts.py
# hold every claim that can be held without rpmbuild.
%global bridge_uuid  w11-bridge@w11
%global overlap_uuid w11-overlap@w11
# brp-mangle-shebangs rewrites #!/bin/sh into #!/usr/bin/sh; the helper ships
# the bytes debian/ ships (tests/test_release_rpm.py compares them to the tree)
# and /bin/sh is the same file on Fedora.
%global __brp_mangle_shebangs_exclude_from ^/usr/libexec/w11/

Name:           w11
Version:        0.4.0
Release:        1%{?dist}
Summary:        X11 power tools as drop-in clones for Wayland

# BSD-2-Clause, from the LICENSE the owner added in HEAD 375d815 -- whose first
# line is the SPDX-License-Identifier header this id is copied from, and which
# pyproject.toml (license = {text = "BSD-2-Clause"}) and debian/copyright now
# say too.  Fedora requires a valid SPDX expression; this is one, so nothing
# here is unsubmittable on the licence side any more and the rpmlintrc has no
# invalid-license filter.
#
# scripts/build-rpm.sh reads LICENSE's first line and refuses the build if it
# disagrees with this tag (spdx_id() there is the reader), so a relicence that
# edits one file and not the other cannot ship.
License:        BSD-2-Clause
URL:            https://github.com/fixing-wayland/w11
Source0:        %{name}-%{version}.tar.gz

# noarch, and one build covers Fedora 43 and 44: both ship python3 3.14.7, so
# both satisfy the auto-generated `Requires: python(abi) = 3.14` [measured from
# mdapi, recon2/pkg-rpm.md 2].  rawhide is 3.15 and needs its own build, which
# is why the rpm-install CI matrix names three tags and lets rawhide fail.
BuildArch:      noarch

BuildRequires:  python3-devel
# %%{_udevrulesdir} and %%{_modulesloaddir}
BuildRequires:  systemd-rpm-macros

# warandr is one tool of seven and the only importer of gi.  Weak deps are on by
# default in dnf, so the GUI still arrives for everyone who has not turned them
# off, and `dnf install w11` on a sway box drags in no toolkit.  This is
# the deb's documented gap -- tests/test_release_deb.py TheGtkDependency, an
# expectedFailure -- taken the other way here; a divergence, and the owner's to
# settle (design decision 5).
Recommends:     python3-gobject
Recommends:     gtk3
Suggests:       wl-mirror
# What the handover execve()s -- on an X11 session directly, and on a Wayland
# one through xw11's display, which is why these four are Recommends and not
# Suggests: with them installed the tools the user already knows are what runs,
# and W11_PROXY=never asks for the clones instead.  Fedora ships one binary per
# package (measured: /usr/bin/xprop -> xprop, /usr/bin/xrandr -> xrandr), where
# Debian has x11-utils and x11-xserver-utils, so this names four packages and
# not two.  w11common/distro.py's fedora row is these same names, and
# tests/test_packaging_names.py is what keeps the two tables one table.
Recommends:     xdotool
Recommends:     wmctrl
Recommends:     xprop
Recommends:     xrandr

%description
Seven commands that behave like the X11 tools they clone -- same commands, same
flags, same output bytes -- on a Wayland session:

  * wdotool  - xdotool: key/type/click/mousemove and the window commands
  * wwmctl   - wmctrl: list and act on windows, desktops and workspaces
  * wxprop   - xprop: properties of native and XWayland windows
  * wxrandr  - xrandr: query and reshape the monitor layout
  * warandr  - arandr: the drag-your-monitors GUI, on Wayland and on X11
  * wmirror  - mirror a region or an odd-shaped output on wlroots (wl-mirror)
  * xw11     - an X11 display in front of the session's X server, so that the
               ORIGINAL xdotool, wmctrl, xprop, xrandr and every other X client
               answer about the whole desktop

On an X11 session the first four hand over to the real xdotool, wmctrl, xprop
and xrandr with execve, argv untouched, so one script runs on both session
types.  On a Wayland session with those four installed they hand over as well,
to the original running against xw11's display.  The originals are never
replaced.

Also installed: a udev rule that lets the user at the active seat open
/dev/uinput without root, and the warandr application-menu entry.  The GNOME
Shell bridge extension the window commands need on GNOME is the
gnome-shell-extension-w11-bridge subpackage, which dnf installs
alongside this one on any machine that also has gnome-shell.

%package -n gnome-shell-extension-%{name}-bridge
Summary:        GNOME Shell extension exporting Mutter's window API on the session bus
Requires:       gnome-shell
Requires:       %{name} = %{version}-%{release}
# Installed automatically wherever both halves are present, and nowhere else.
# The .deb cannot express that: it carries the extension for every machine,
# sway boxes included.  Fedora's convention, measured against
# gnome-shell-extension-appindicator's file list [recon2/pkg-rpm.md 2].
Supplements:    (%{name} and gnome-shell)

%description -n gnome-shell-extension-%{name}-bridge
GNOME has no window-management protocol, so wdotool's window commands, wwmctl
and wxprop go through a small GNOME Shell extension that exports Mutter over
the session bus as org.w11.Bridge.  gnome-shell scans extension
directories only at login, so one log out and back in is needed after
installing this; the extension is enabled for each user in that first session
by /etc/xdg/autostart/w11-enable-bridge.desktop.

%package -n gnome-shell-extension-%{name}-overlap
Summary:        GNOME Shell extension for wxrandr --unsafe-gnome-overlap
Requires:       gnome-shell
Requires:       %{name} = %{version}-%{release}
# Deliberately NO Supplements: nothing installs this for you, nothing enables
# it, and it is the only thing here that can cost the session you are sitting
# in.  README.md, "Overlapping monitors on GNOME".

%description -n gnome-shell-extension-%{name}-overlap
The one route through Mutter's refusal to place two monitors so that they
share screen area.  Installing it turns nothing on: the two further steps are
`gnome-extensions enable %{overlap_uuid}` and
`wxrandr --unsafe-gnome-overlap`, and both refuse on a GNOME generation the
table has not measured.

%prep
%autosetup

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files w11common wdotool wwmctl wxprop wxrandr warandr wmirror xw11 hacks

install -Dpm 0644 gnome/60-w11-uinput.rules \
    %{buildroot}%{_udevrulesdir}/60-w11-uinput.rules
install -Dpm 0644 gnome/modules-load-uinput.conf \
    %{buildroot}%{_modulesloaddir}/w11-uinput.conf
install -Dpm 0644 warandr.desktop %{buildroot}%{_datadir}/applications/warandr.desktop

# The bridge extension, its per-user enabler and the autostart entry that runs
# it.  Two differences from debian/ here, both deliberate:
#
#  * the helper is %%{_libexecdir}/w11/enable-bridge, not
#    /usr/lib/w11/enable-bridge, because /usr/libexec is where Fedora
#    puts a program no user runs by hand.  The script itself is unchanged; the
#    .desktop's Exec= is rewritten below, and that is the whole of the
#    difference.
#  * the .desktop is a plain file straight into %%{_sysconfdir}/xdg/autostart.
#    debhelper registers every regular file under /etc as a conffile and a
#    conffile survives `apt remove`, which is why debian/ installs it under
#    /usr/lib and symlinks it; rpm marks nothing %%config unless the spec says
#    so, so an erase takes the file and there is no symlink to arrange.
install -d %{buildroot}%{_datadir}/gnome-shell/extensions/%{bridge_uuid}
install -pm 0644 gnome/%{bridge_uuid}/extension.js \
                 gnome/%{bridge_uuid}/metadata.json \
                 gnome/%{bridge_uuid}/org.w11.Bridge1.xml \
    %{buildroot}%{_datadir}/gnome-shell/extensions/%{bridge_uuid}/
install -Dpm 0755 packaging/common/enable-bridge \
    %{buildroot}%{_libexecdir}/%{name}/enable-bridge
install -d %{buildroot}%{_sysconfdir}/xdg/autostart
sed 's|^Exec=.*|Exec=%{_libexecdir}/%{name}/enable-bridge|' \
    packaging/common/enable-bridge.desktop \
    > %{buildroot}%{_sysconfdir}/xdg/autostart/w11-enable-bridge.desktop
chmod 0644 %{buildroot}%{_sysconfdir}/xdg/autostart/w11-enable-bridge.desktop

# The overlap extension, typelibs included: one compiled type description per
# record in generations.json, and the extension refuses to run one generation's
# against another's (docs/Technical.md section 6).
install -d %{buildroot}%{_datadir}/gnome-shell/extensions/%{overlap_uuid}/typelib
install -pm 0644 gnome/%{overlap_uuid}/extension.js \
                 gnome/%{overlap_uuid}/metadata.json \
                 gnome/%{overlap_uuid}/rules.js \
                 gnome/%{overlap_uuid}/org.w11.Overlap1.xml \
                 gnome/%{overlap_uuid}/generations.json \
    %{buildroot}%{_datadir}/gnome-shell/extensions/%{overlap_uuid}/
install -pm 0644 gnome/%{overlap_uuid}/typelib/*.typelib \
    %{buildroot}%{_datadir}/gnome-shell/extensions/%{overlap_uuid}/typelib/

# The installation stamp %post writes and the enabler reads.  The file itself
# is %ghost: it is state, not payload, and an erase takes it.
install -d %{buildroot}%{_sharedstatedir}/%{name}

%check
# The suite drives real compositors, VMs and /dev/uinput (tests/, vm/README.md);
# none of that exists in a build root.  What can run is the import check, minus
# warandr, which imports gi at module scope.
%pyproject_check_import -e 'warandr*'

%post
# The same four commands, in the same order, as debian/w11.postinst and
# `gnome/install-bridge.sh --udev`: apply the rule to the /dev/uinput that is
# already there, so injection works with no reboot.  The rule only tags the
# node `uaccess`; systemd-logind then gives the user of the active seat an ACL
# on it.  The node stays root:root 0600 and no group is involved.
#
# The reload is ours although systemd-udev carries a transfiletrigger on
# %%{_udevrulesdir} (measured: `rpm -q --filetriggers systemd-udev` on Fedora 44
# prints `transfiletriggerin ... mark-reload-system-units systemd-udevd.service`
# [recon2/pkg-rpm.md 2]).  That trigger runs at the END of the transaction,
# after this scriptlet, so without our own reload the trigger below would
# re-tag the node against the rule set that was in force before this package
# arrived.  This is the one rpm-specific ordering hazard in the file.
if [ -d /run/udev ] && command -v udevadm >/dev/null 2>&1; then
    modprobe uinput >/dev/null 2>&1 || :
    udevadm control --reload-rules >/dev/null 2>&1 || :
    udevadm trigger --name-match=uinput >/dev/null 2>&1 || :
    udevadm settle --timeout=5 >/dev/null 2>&1 || :
fi
# The stamp is the record that this installation happened; the per-user enabler
# compares its own against it.  $1 is 1 on install and 2 on upgrade, and an
# upgrade must leave it alone or every user is set up again.
if [ ! -e %{_sharedstatedir}/%{name}/installed ]; then
    mkdir -p %{_sharedstatedir}/%{name} && : > %{_sharedstatedir}/%{name}/installed
fi
exit 0

%postun
# $1 == 0 is the last erase; rpm has no purge, and $1 == 1 is an upgrade, where
# nothing here may run.  Undo what the rule granted, once rpm has taken the
# rule file away -- the reasoning is debian/w11.postrm's: udev keeps
# permissions and ACLs no rule asks it to change, and its tags are sticky in
# its own database, so dropping the node's database entry and tag links is what
# makes udev start from scratch at the node's next event.
#
# setfacl is in the `acl` package, which Fedora's cloud image does not have
# (measured: no getfacl, no setfacl [recon2/fedora.md]), so the python fallback
# is the normal path there and this package does not Require acl for it.
if [ "$1" = 0 ]; then
    rm -f %{_sharedstatedir}/%{name}/installed 2>/dev/null || :
    rmdir %{_sharedstatedir}/%{name} 2>/dev/null || :
    if [ -e /dev/uinput ] && command -v udevadm >/dev/null 2>&1; then
        udevadm control --reload-rules >/dev/null 2>&1 || :
        rm -f /run/udev/static_node-tags/uaccess/uinput 2>/dev/null || :
        maj=$(udevadm info -q property /dev/uinput 2>/dev/null | sed -n 's/^MAJOR=//p') || maj=
        min=$(udevadm info -q property /dev/uinput 2>/dev/null | sed -n 's/^MINOR=//p') || min=
        if [ -n "$maj" ] && [ -n "$min" ]; then
            rm -f "/run/udev/data/c$maj:$min" /run/udev/tags/*/"c$maj:$min" 2>/dev/null || :
        fi
        if command -v setfacl >/dev/null 2>&1; then
            setfacl -b /dev/uinput 2>/dev/null || :
        elif command -v python3 >/dev/null 2>&1; then
            python3 -c 'import os; os.removexattr("/dev/uinput", "system.posix_acl_access")' 2>/dev/null || :
        fi
        chown root:root /dev/uinput 2>/dev/null || :
        chmod 0600 /dev/uinput 2>/dev/null || :
    fi
fi
exit 0

%files -f %{pyproject_files}
# %%license, not %%doc: rpm marks these files so that a `--nodocs` install still
# keeps the licence, which is what Fedora's guidelines require of every package
# that ships one.
%license LICENSE
%doc README.md CHANGELOG.md docs/ packaging/rpm/README.Fedora
# The seven console scripts, listed by hand beside `%%files -f %%{pyproject_files}`
# -- which is what the Fedora Packaging Guidelines' own Python example does,
# because %%pyproject_save_files claims what lands under %%{python3_sitelib} and
# not what lands in %%{_bindir}.  This is the one line in the file nobody could
# run here (the %%pyproject_* macros are Fedora-only, and rpmbuild on this guest
# dies at "%%pyproject_buildrequires: not found"), so the CI `rpm` job in
# fedora:44 is what settles it -- and it settles it safely either way, because
# Fedora sets %%_duplicate_files_terminate_build, so a name claimed twice fails
# the build rather than shipping something wrong.  Without these seven lines
# the package would have no commands in it and nothing would say so.
# tests/test_rpm_spec.py pins the list against pyproject.toml [project.scripts].
%{_bindir}/wdotool
%{_bindir}/wwmctl
%{_bindir}/wxprop
%{_bindir}/wxrandr
%{_bindir}/warandr
%{_bindir}/wmirror
%{_bindir}/xw11
%{_udevrulesdir}/60-w11-uinput.rules
%{_modulesloaddir}/w11-uinput.conf
%{_datadir}/applications/warandr.desktop
%dir %{_sharedstatedir}/%{name}
# %%attr, because a %%ghost with no mode is 000 and rpmlint says
# zero-perms-ghost about it; the file %post writes is 0644.
%ghost %attr(0644,root,root) %{_sharedstatedir}/%{name}/installed

%files -n gnome-shell-extension-%{name}-bridge
%{_datadir}/gnome-shell/extensions/%{bridge_uuid}/
%{_libexecdir}/%{name}/
%{_sysconfdir}/xdg/autostart/w11-enable-bridge.desktop

%files -n gnome-shell-extension-%{name}-overlap
%{_datadir}/gnome-shell/extensions/%{overlap_uuid}/

%changelog
* Sun Sep 06 2026 Antonio Bianchi <antonio.bianchi.333@gmail.com> - 0.4.0-1
- Same payload as w11 (0.4.0) in debian/changelog.
