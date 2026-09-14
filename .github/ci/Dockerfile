# The unit-test image, one per Ubuntu release the package claims (24.04, 26.04, 26.10).
# Everything a test file can need on this host, so nothing skips for want of a tool:
# a real sway with Xwayland for the live files, GTK for warandr, Xvfb, the distro's
# own xdotool/wmctrl/xprop/xrandr (the parity oracle is a separate job, see ci.yml),
# the four window managers of the headless harnesses in tests/support.py (see the
# second RUN, which installs them on one release and not on the other), g-ir-compiler
# for the typelib tests, dbus-run-session for the RealBus tests, the Debian build
# tools for scripts/build-deb.sh, and node 22 for tests/test_bridge_js.py.
# Built by the `image` job of .github/workflows/ci.yml and cached in GHCR by the hash
# of this file, so a push that does not touch it pays nothing for it.
ARG BASE=ubuntu:26.04
FROM ${BASE}
ARG NODE=v22.22.1
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-gi gir1.2-gtk-3.0 gir1.2-gtk-4.0 \
        sway xwayland xterm foot xvfb xauth x11-utils x11-xserver-utils xdotool wmctrl \
        dbus dbus-bin dbus-daemon libglib2.0-bin libgirepository1.0-dev \
        dpkg-dev debhelper dh-python pybuild-plugin-pyproject python3-all python3-setuptools fakeroot \
        ca-certificates curl xz-utils zstd git procps sudo \
    && rm -rf /var/lib/apt/lists/*
# The four window managers HeadlessLabwc, HeadlessWayfire, HeadlessI3 and
# HeadlessOpenbox drive, and the one place in these images where the RELEASE decides
# what goes in.  26.04 packages labwc 0.9.3, wayfire 0.10.0, i3 4.25.1 and openbox
# 3.6.1, which are the versions tests/test_labwc_live.py and its three siblings were
# measured against.  24.04 packages labwc 0.7.1-1build1, wayfire 0.8.0+git20240110-2.1build1
# and i3-wm 4.23-1build2 (Launchpad, noble, Published, read 2026-09-09): labwc grew
# ext_workspace_manager_v1 at 0.9 on wlroots 0.19, which is the shape HeadlessLabwc's
# desktops come from, and the live files gate on `shutil.which` alone -- so installing
# these on noble would not skip those files, it would run them against a compositor
# nobody has measured.  openbox is 3.6.1 on both and could stay, but it rides with the
# other three rather than being a second arm for one duplicate of coverage 26.04 and
# 26.10 already carry.  The route to running the four on 24.04 as well is a version
# gate in each live file (`skipUnless labwc >= 0.9`) and then one arm here.
# tests/test_ci_images.py::TheLiveCompositors parses this instruction.
RUN . /etc/os-release && case "$VERSION_ID" in \
        24.04) echo "live compositors left out on $VERSION_ID: labwc 0.7.1, wayfire 0.8.0, i3 4.23" ;; \
        *) apt-get update \
           && apt-get install -y --no-install-recommends labwc wayfire i3 openbox \
           && rm -rf /var/lib/apt/lists/* ;; \
    esac
RUN curl -fsSL "https://nodejs.org/dist/${NODE}/node-${NODE}-linux-x64.tar.xz" | tar -xJ -C /opt \
    && ln -s "/opt/node-${NODE}-linux-x64/bin/node" /usr/local/bin/node && node --version
# The suite is not written to run as root (the root daemon listens on /run and a few
# tests skip themselves there). uid 1000 because the sudo fixtures name SUDO_UID=1000
# and the handover resolves it through passwd; the checkout is chowned to it in the job.
# The 24.04+ base images ship an `ubuntu` user on uid 1000; it goes so ci can have it.
RUN if id ubuntu >/dev/null 2>&1; then userdel -r ubuntu; fi \
    && useradd -m -u 1000 -s /bin/bash ci && install -d -m 0700 -o ci -g ci /tmp/xdg-ci
# the directory X servers and the xw11 proxy bind their filesystem socket in: on a real
# system the first X server creates it 1777 as root; a container has no such first server
RUN install -d -m 1777 /tmp/.X11-unix
