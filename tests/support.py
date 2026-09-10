"""Doubles the test files share, in one place.

Not named `test_*.py` on purpose: the escape-hatch guard in
`tests/test_passthrough.py` walks the test files and would count this one,
and no runner should try to collect it. It is imported bare (`import
support`) rather than as `tests.support`, because every way the suite is
run -- `python3 -m unittest discover -s tests`, `python3 -m pytest
tests/x.py`, `python3 tests/x.py` -- puts the tests directory itself on
sys.path, and eight of the files that need it carry no repo-root bootstrap.

What lives here is only what was written more than once and byte-identical
in behaviour: the recorder uinput device and its tablet reader, the
environment context manager, the faked evdev layer, the headless sway
rig the four XWayland live files boot, and stopping a spawned input
daemon -- which three files do, all three differently and none of them
reliably (see the daemon section below). Doubles that differ between their
callers stay where they are -- `make_daemon` (three shapes of `geom`),
`FakeDaemon` (two protocols), `FakeBackend`, the compositor fakes.

Added for the second round of tests, under the same rule -- each one was
about to be written twice: the markdown walker three files carry
(`documents`), the shell-script slicer (`sh_block`/`sh_function`), the fake
GNOME command line the install scripts need (`fake_gnome_bin`), the node
harness that runs the two GNOME extensions (`js_harness`, with
tests/fixtures/gjs/), the sway IPC double (`FakeSway`), the session leader
stand-in (`leader_process`) and the wl-mirror stub (`WL_MIRROR_STUB`).
tests/test_support_helpers.py is where each of them is proved.

Added for the second design round, same rule and the same file to prove them
in: the Hyprland IPC double and its event socket (`FakeHypr`,
`FakeHyprEvents`), the Wayfire IPC double (`FakeWayfire`), `FakeSway`'s i3
dialect, and the five headless compositors the live tests boot
(`HeadlessLabwc`, `HeadlessWayfire`, `HeadlessRiver`, `HeadlessI3`,
`HeadlessOpenbox`) beside `HeadlessSway`. Every payload one of them replays
is a recording under tests/fixtures/, named where it is used.
"""

import contextlib
import errno
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from wdotool import keystate, uinput


# -- recorded fixtures --------------------------------------------------------

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture_json(*parts):
    """A recorded JSON fixture under tests/fixtures/, parsed. Every double here that replays bytes replays them
    from one of these rather than from a literal, so the file and the report that produced it are the only
    place a payload can be edited."""
    with open(os.path.join(FIXTURE_DIR, *parts), encoding="utf-8") as fh:
        return json.load(fh)


# -- environment --------------------------------------------------------------

@contextlib.contextmanager
def env(**kw):
    """Set (a string) / unset (None) environment variables for the block,
    restoring exactly what was there -- including "was not set"."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# -- the uinput devices, recording --------------------------------------------

class RecorderDev:
    """A uinput device that records instead of writing to the kernel.

    Every event is a plain tuple in the order it was emitted: `(etype,
    code, value)` for emit(), `("KEY", code, 0|1)` for key(), `("SYN",)`
    for syn() -- which is what the assertions in these tests read."""

    def __init__(self):
        self.events = []
        self.closed = False

    def emit(self, etype, code, value):
        self.events.append((etype, code, value))

    def syn(self):
        self.events.append(("SYN",))

    def key(self, code, down):
        self.events.append(("KEY", code, 1 if down else 0))

    def close(self):
        self.closed = True


def abs_report(dev):
    """(ABS_X, ABS_Y) of the last report a recorder tablet emitted."""
    vals = {}
    for ev in dev.events:
        if ev[0] == uinput.EV_ABS:
            vals[ev[1]] = ev[2]
    return vals[uinput.ABS_X], vals[uinput.ABS_Y]


# -- the evdev layer, faked ---------------------------------------------------
#
# --clearmodifiers reads the real keyboards' key state (EVIOCGKEY) to know
# what to put back, and `keys watch` reads their event streams. A test must
# never read the *runner's* keyboard for either: it would answer differently
# depending on what the person at the keyboard happens to be holding, and
# there is no keyboard at all in a container. So keystate.Evdev -- the whole
# syscall layer -- is swapped for this.

def key_bitmap(codes) -> bytes:
    """A kernel key bitmap (EVIOCGKEY / EVIOCGBIT shape). Bytes pass
    through, so a node may carry a ready-made bitmap instead of codes."""
    if isinstance(codes, (bytes, bytearray)):
        return bytes(codes)
    buf = bytearray(keystate._KEY_BYTES)
    for c in codes:
        buf[c >> 3] |= 1 << (c & 7)
    return bytes(buf)


KEYBOARD_CAPS = key_bitmap([1, 30, 42, 100])     # Esc, a, shift, right alt
MOUSE_CAPS = key_bitmap([0x110, 0x111])          # BTN_LEFT/RIGHT only


class FakeEvdev:
    """keystate.Evdev's calls, plus read(), over scripted device nodes.

    A node is a dict: `name`, `caps` (key codes or a ready bitmap; a
    keyboard by default), `held` (the codes currently down), `data` (bytes
    read() hands out), `denied` (EACCES on open), `gone` (ENODEV on read).
    `devices` takes either that mapping, or the flat `(path, name, caps,
    held)` tuples the daemon tests write.

    `unreadable` paths are listed by paths() and raise EACCES on open,
    which is what a /dev/input/event* looks like to a uid that may not read
    it (the normal case for a desktop user -- see the module docstring of
    keystate.py). `before_read(self, nth)` runs before every key-state
    read, which is how a test makes the user let go of a key mid-sequence.
    """

    def __init__(self, devices=(), unreadable=(), before_read=None):
        if hasattr(devices, "items"):
            self.nodes = {p: dict(n) for p, n in devices.items()}
        else:
            self.nodes = {p: {"name": name, "caps": set(caps),
                              "held": set(held)}
                          for p, name, caps, held in devices}
        self.unreadable = set(unreadable)
        self.before_read = before_read
        self.reads = []          # paths whose key state was read, in order
        self.opened = []
        self.open_fds = {}
        self._next_fd = 10

    # -- keystate.Evdev interface
    def paths(self):
        return sorted(set(self.nodes) | self.unreadable)

    def open(self, path):
        node = self.nodes.get(path)
        if node is None and path not in self.unreadable:
            raise FileNotFoundError(errno.ENOENT, "no such device", path)
        if node is None or node.get("denied"):
            raise PermissionError(errno.EACCES, "Permission denied", path)
        self.opened.append(path)
        fd = self._next_fd
        self._next_fd += 1
        self.open_fds[fd] = path
        return fd

    def close(self, fd):
        self.open_fds.pop(fd, None)

    def _node(self, fd):
        return self.nodes[self.open_fds[fd]]

    def name(self, fd):
        return self._node(fd)["name"]

    def key_caps(self, fd):
        return key_bitmap(self._node(fd).get("caps", KEYBOARD_CAPS))

    def key_state(self, fd):
        path = self.open_fds[fd]
        self.reads.append(path)
        if self.before_read:
            self.before_read(self, len(self.reads))
        return key_bitmap(self.nodes[path].get("held", ()))

    def read(self, fd, n):
        node = self._node(fd)
        if node.get("gone"):
            raise OSError(errno.ENODEV, "No such device")
        data = node.get("data", b"")
        node["data"] = data[n:]
        return data[:n]

    # -- test helpers
    def press(self, path, *codes):
        self.nodes[path].setdefault("held", set()).update(codes)

    def release(self, path, *codes):
        self.nodes[path].setdefault("held", set()).difference_update(codes)


# -- the input daemon, spawned and stopped ------------------------------------
#
# Every test that spawns a real daemon stops it through these, and no test
# may spawn one any other way. A daemon nobody stops is not one stray
# process for the length of one test: its socket sits in a temporary
# runtime directory the test then deletes, and a daemon whose socket file
# is gone cannot be reached by anybody -- 161 of them were found alive at
# once on the test rig, ~3GB between them, the oldest 18 hours old.
# `wdotool/daemon.py` now notices that and exits by itself; these keep the
# suite from leaning on it.

DAEMON_ARGV = "__daemon"


def daemon_runtime_dir(pid):
    """$XDG_RUNTIME_DIR the process was started with, or None.

    /proc/<pid>/environ is the environment at exec(), which is exactly
    what the daemon computed its socket path from."""
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for kv in raw.split("\0"):
        if kv.startswith("XDG_RUNTIME_DIR="):
            return kv.split("=", 1)[1]
    return None


def daemon_pids(rtdir=None):
    """pids of this euid's wdotool input daemons; with `rtdir`, only the
    ones spawned into that runtime directory.

    Read out of /proc rather than matched with `pkill -f __daemon`: that
    pattern also matches the shell that runs it and anything else carrying
    the word, and a test that knows which directory it spawned into can
    simply name its own processes."""
    me = os.geteuid()
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            if os.stat("/proc/%d" % pid).st_uid != me:
                continue
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue           # it exited while we were reading it
        if DAEMON_ARGV not in argv:
            continue
        if rtdir is not None and daemon_runtime_dir(pid) != rtdir:
            continue
        out.append(pid)
    return sorted(out)


def stop_daemon(pid, timeout=5.0):
    """SIGTERM, then SIGKILL, and do not return until the process is
    really gone. True when it is.

    The waiting is the point. A daemon that has been signalled but has not
    gone yet is still holding its socket, and a test that removes its
    runtime directory in between leaves exactly the unreachable daemon
    this exists to prevent. (A daemon spawned by a client is double-forked
    and reparented, so it is nobody's child and there is no zombie to wait
    for: kill(pid, 0) is the whole answer.)"""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False       # not ours to kill; the caller says so
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            time.sleep(0.02)
    return False


def stop_daemons_under(rtdir, timeout=5.0):
    """Stop every daemon spawned into `rtdir`, and wait for them.

    Register it with addCleanup/addClassCleanup *before* whatever removes
    the directory (cleanups run last-in-first-out, and unittest runs
    tearDown before any of them) and before the spawn itself, so that a
    test which dies half way through its own setup still takes its daemon
    with it."""
    left = [pid for pid in daemon_pids(rtdir)
            if not stop_daemon(pid, timeout)]
    if left:
        raise AssertionError(
            "wdotool daemon(s) %s spawned into %s could not be stopped"
            % (left, rtdir))


# -- a headless sway with XWayland --------------------------------------------

class HeadlessSway:
    """A private sway on its own XDG_RUNTIME_DIR, with XWayland on.

    The four live files each booted one by hand, identically down to the
    WLR_* variables and the two 15s/10s waits. Every way it can fail to
    come up is a SkipTest here, exactly as it was there: these tests prove
    the tools against a real compositor, and a box that cannot start one
    has nothing to say about them.

    `extra_conf` is appended to the config before the line that reports
    DISPLAY. `extra_env` is merged over the environment sway is started in
    (and over `.env`, which is what a test hands to its own subprocesses);
    pass a callable to build it from the runtime directory, which only
    exists once the rig does.
    With `need_display=False` an XWayland that never announces itself is
    not fatal and `.display` comes back empty.
    """

    CONF = ("output HEADLESS-1 mode 1280x720\n"
            "xwayland enable\n"
            "default_border none\n")

    def __init__(self, prefix, extra_conf="", extra_env=None,
                 need_display=True):
        self.rtdir = tempfile.mkdtemp(prefix=prefix)
        os.chmod(self.rtdir, 0o700)
        conf = os.path.join(self.rtdir, "sway.conf")
        with open(conf, "w") as f:
            f.write(self.CONF + extra_conf
                    + "exec sh -c 'echo \"$DISPLAY\" > %s/display'\n"
                    % self.rtdir)
        self.env = dict(
            os.environ,
            XDG_RUNTIME_DIR=self.rtdir,
            WLR_BACKENDS="headless",
            WLR_LIBINPUT_NO_DEVICES="1",
            WLR_RENDERER="pixman",
            # dodge the nixpkgs sway wrapper's dbus-run-session fallback
            DBUS_SESSION_BUS_ADDRESS="unix:path=%s/no-bus" % self.rtdir,
        )
        if callable(extra_env):
            extra_env = extra_env(self.rtdir)
        self.env.update(extra_env or {})
        self.proc = subprocess.Popen(
            ["sway", "-c", conf], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.sock = self._wait_socket()
        self.env["SWAYSOCK"] = self.sock
        self.display = self._wait_display()
        if self.display:
            self.env["DISPLAY"] = self.display
        elif need_display:
            self.stop()
            raise unittest.SkipTest("sway did not announce an X DISPLAY "
                                    "(xwayland enable missing?)")

    def _wait_socket(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            socks = [n for n in os.listdir(self.rtdir)
                     if n.startswith("sway-ipc.") and n.endswith(".sock")]
            if socks:
                return os.path.join(self.rtdir, socks[0])
            if self.proc.poll() is not None:
                self.stop()
                raise unittest.SkipTest("sway exited at startup")
            time.sleep(0.2)
        self.stop()
        raise unittest.SkipTest("sway did not create an IPC socket")

    def _wait_display(self):
        dfile = os.path.join(self.rtdir, "display")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with open(dfile) as f:
                    got = f.read().strip()
            except OSError:
                got = ""
            if got:
                return got
            time.sleep(0.2)
        return ""

    def wayland_display(self, default="wayland-1"):
        """The name of the Wayland socket this sway is listening on."""
        names = [n for n in os.listdir(self.rtdir)
                 if n.startswith("wayland-") and not n.endswith(".lock")]
        return names[0] if names else default

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.rtdir, ignore_errors=True)


class _HeadlessWlroots:
    """Shared shape for the wlroots compositors that are not sway.

    HeadlessSway is the model and stays as it is; what is different here, and measured, is:

    * The runtime directory is a SHORT `tempfile.mkdtemp` name. Both labwc and Wayfire refuse a long
      `XDG_RUNTIME_DIR`: labwc dies with `File name too long` and Wayfire with `socket path ... exceeds 108
      bytes` -- the Unix socket path limit, which the suite's usual `/tmp/<long prefix>` blows through
      [M recon2/labwc.md, recon2/wayfire.md].
    * There is no IPC socket to wait for (labwc and river have none at all), so the wait is for the
      `wayland-*` socket plus, where the compositor can run one, an autostart line that writes DISPLAY.

    Every way it can fail to come up is a SkipTest, exactly as HeadlessSway does it: a box that cannot start
    the compositor has nothing to say about the tools."""

    #: the binary, and the argv that starts it with `conf`
    BINARY = None
    #: short prefix -- see the socket-path limit above
    PREFIX = "fw"

    def argv(self, confdir):
        raise NotImplementedError

    def write_config(self, confdir):
        """Write the compositor's own config into `confdir`; return nothing."""

    def __init__(self, extra_env=None, need_display=False, timeout=15):
        if not shutil.which(self.BINARY):
            raise unittest.SkipTest("%s is not installed" % self.BINARY)
        self.rtdir = tempfile.mkdtemp(prefix=self.PREFIX, dir="/tmp")
        os.chmod(self.rtdir, 0o700)
        self.confdir = os.path.join(self.rtdir, "cfg")
        os.mkdir(self.confdir)
        self.write_config(self.confdir)
        self.env = dict(
            os.environ,
            XDG_RUNTIME_DIR=self.rtdir,
            WLR_BACKENDS="headless",
            WLR_LIBINPUT_NO_DEVICES="1",
            WLR_RENDERER="pixman",
            DBUS_SESSION_BUS_ADDRESS="unix:path=%s/no-bus" % self.rtdir,
        )
        # the box's own DISPLAY must not survive into the compositor: the
        # autostart line reports "$DISPLAY", and an inherited one would be
        # written into the file as if it were this compositor's Xwayland
        self.env.pop("DISPLAY", None)
        self.env.pop("WAYLAND_DISPLAY", None)
        self.env.update(extra_env or {})
        self.sock = None
        self.proc = subprocess.Popen(
            self.argv(self.confdir), env=self.env, cwd=self.rtdir,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wayland_socket = self._wait_socket(timeout)
        self.env["WAYLAND_DISPLAY"] = os.path.basename(self.wayland_socket)
        self.after_start()
        self.display = self._wait_display()
        if self.display:
            self.env["DISPLAY"] = self.display
        elif need_display:
            self.stop()
            raise unittest.SkipTest("%s did not announce an X DISPLAY" % self.BINARY)

    def after_start(self):
        """Hook: the wayland socket is up, nothing else has been waited for."""

    def _wait_socket(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            socks = [n for n in os.listdir(self.rtdir)
                     if n.startswith("wayland-") and not n.endswith(".lock")]
            if socks:
                return os.path.join(self.rtdir, sorted(socks)[0])
            if self.proc.poll() is not None:
                self.stop()
                raise unittest.SkipTest("%s exited at startup" % self.BINARY)
            time.sleep(0.2)
        self.stop()
        raise unittest.SkipTest("%s created no wayland socket" % self.BINARY)

    def _wait_display(self):
        dfile = os.path.join(self.rtdir, "display")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with open(dfile) as f:
                    got = f.read().strip()
            except OSError:
                got = ""
            if got:
                return got
            time.sleep(0.2)
        return ""

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        shutil.rmtree(self.rtdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


class HeadlessLabwc(_HeadlessWlroots):
    """labwc with three named desktops -- the shape `ext_workspace_manager_v1` was measured on.

    Three names, because labwc with no `<desktops>` block publishes exactly one workspace called
    `Workspace 1` and a desktops test then proves nothing [M recon2/labwc.md §6a]. The autostart line reports
    DISPLAY the way HeadlessSway's `exec` does; labwc Depends on xwayland, so an X plane is normally there."""

    BINARY = "labwc"
    PREFIX = "fwlb"

    RC_XML = ("<?xml version=\"1.0\"?>\n<labwc_config>\n"
              "  <desktops><names><name>one</name><name>two</name><name>three</name></names></desktops>\n"
              "</labwc_config>\n")

    def write_config(self, confdir):
        with open(os.path.join(confdir, "rc.xml"), "w") as f:
            f.write(self.RC_XML)
        auto = os.path.join(confdir, "autostart")
        with open(auto, "w") as f:
            f.write("sh -c 'echo \"$DISPLAY\" > %s/display' &\n" % self.rtdir)
        os.chmod(auto, 0o755)

    def argv(self, confdir):
        return [self.BINARY, "-C", confdir]


class HeadlessWayfire(_HeadlessWlroots):
    """Wayfire with the three IPC plugins loaded.

    `plugins = ... ipc ipc-rules stipc` is what turns `window-rules/list-views` on; without `ipc-rules` the
    socket exists and answers `No such method found!` to everything the window backend needs, which is the
    api gate's whole reason to exist [M recon2/wayfire.md §1.2, §3.3]."""

    BINARY = "wayfire"
    PREFIX = "fwwf"

    def write_config(self, confdir):
        path = os.path.join(confdir, "wayfire.ini")
        with open(path, "w") as f:
            f.write("[core]\nplugins = autostart ipc ipc-rules stipc\n"
                    "\n[output:HEADLESS-1]\nmode = 1280x720\n"
                    "\n[autostart]\nrep = sh -c 'echo \"$DISPLAY\" > %s/display'\n" % self.rtdir)
        self.conf = path

    def argv(self, confdir):
        return [self.BINARY, "-c", os.path.join(confdir, "wayfire.ini")]

    def after_start(self, timeout=10):
        """Wait for the IPC socket and export `$WAYFIRE_SOCKET`, which is the name Wayfire itself gives its
        children. The runtime-dir name carries no pid field at all (`wayfire-<display>-.socket`, recorded as
        `wayfire-wayland-1-.socket`), so the wait is on the prefix and suffix [M recon2/wayfire.md §1.2]."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            socks = [n for n in os.listdir(self.rtdir)
                     if n.startswith("wayfire-") and n.endswith(".socket")]
            if socks:
                self.sock = os.path.join(self.rtdir, sorted(socks)[0])
                self.env["WAYFIRE_SOCKET"] = self.sock
                return
            if self.proc.poll() is not None:
                self.stop()
                raise unittest.SkipTest("wayfire exited at startup")
            time.sleep(0.2)
        self.stop()
        raise unittest.SkipTest("wayfire created no IPC socket "
                                "(is `plugins = ... ipc ipc-rules` honoured?)")


class HeadlessRiver(_HeadlessWlroots):
    """river 0.4 driven by `tinyrwm`, the reference window manager its own tests use.

    river 0.4 is a compositor with no built-in layout: without a window manager on the other end of
    `river_window_manager_v1` nothing is ever mapped, and `wwmctl -l` is empty -- which is itself one of the
    things a river test asserts. Skipped where either binary is missing; neither is packaged on any runner,
    so this only runs where somebody built them [M recon2/river.md §2]."""

    BINARY = "river"
    PREFIX = "fwrv"
    WM = "tinyrwm"

    def __init__(self, *a, **kw):
        if not shutil.which(self.WM):
            raise unittest.SkipTest("tinyrwm is not installed")
        super().__init__(*a, **kw)

    def argv(self, confdir):
        return [self.BINARY, "-c", self.WM]


class HeadlessXvfb:
    """An X server on a free display, and one X11 window manager on it.

    Xvfb rather than a nested Wayland compositor because i3 and Openbox are X11 programs and there is no other
    honest way to run them: everything measured came up in about 3 s with no root [M recon2/i3.md §5,
    recon2/openbox.md]. `.env` carries DISPLAY (and, for i3, I3SOCK read back from `i3 --get-socketpath`) for
    the tools a test spawns."""

    WM = None
    #: extra argv after the binary
    WM_ARGS = ()

    def __init__(self, screen="1920x1080x24", timeout=15):
        for binary in ("Xvfb", self.WM):
            if not shutil.which(binary):
                raise unittest.SkipTest("%s is not installed" % binary)
        self.rtdir = tempfile.mkdtemp(prefix="fwx", dir="/tmp")
        os.chmod(self.rtdir, 0o700)
        self.num = self._free_display()
        self.display = ":%d" % self.num
        self.xvfb = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", screen],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_x(timeout)
        self.env = dict(os.environ, DISPLAY=self.display,
                        XDG_RUNTIME_DIR=self.rtdir)
        self.env.pop("XAUTHORITY", None)
        self.wm = subprocess.Popen(
            [self.WM] + list(self.wm_args()), env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.after_start()

    def wm_args(self):
        return self.WM_ARGS

    def after_start(self):
        time.sleep(0.5)

    def _free_display(self):
        # a number nothing is listening on, and that no other test picked: the
        # socket's absence is the only check an X server itself makes
        for num in range(70, 120):
            if not os.path.exists("/tmp/.X11-unix/X%d" % num):
                return num
        raise unittest.SkipTest("no free X display number")

    def _wait_x(self, timeout):
        deadline = time.monotonic() + timeout
        sock = "/tmp/.X11-unix/X%d" % self.num
        while time.monotonic() < deadline:
            if os.path.exists(sock):
                return
            if self.xvfb.poll() is not None:
                self.stop()
                raise unittest.SkipTest("Xvfb exited at startup")
            time.sleep(0.1)
        self.stop()
        raise unittest.SkipTest("Xvfb never created %s" % sock)

    def stop(self):
        for proc in (getattr(self, "wm", None), getattr(self, "xvfb", None)):
            if proc is None or proc.poll() is not None:
                continue
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(self.rtdir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


class HeadlessI3(HeadlessXvfb):
    """i3 on an Xvfb, with no bar and no first-run wizard.

    No `bar` block, because i3bar is a second process with an IPC connection of its own and nothing here wants
    it; a config file at all, because `i3-config-wizard` pops a dialog when ~/.config/i3/config is missing.
    `$I3SOCK` comes from `i3 --get-socketpath` rather than from a guess -- which is also the one thing i3 does
    not put in its own environ [M recon2/i3.md §1]."""

    WM = "i3"
    CONF = "font pango:monospace 8\nfocus_follows_mouse no\n"

    def __init__(self, *a, **kw):
        self.conf = None
        super().__init__(*a, **kw)

    def wm_args(self):
        self.conf = os.path.join(self.rtdir, "i3.conf")
        with open(self.conf, "w") as f:
            f.write(self.CONF)
        return ["-c", self.conf]

    def after_start(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                out = subprocess.run([self.WM, "--get-socketpath"], env=self.env,
                                     capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                out = None
            path = (out.stdout.strip() if out and out.returncode == 0 else "")
            if path and os.path.exists(path):
                self.sock = path
                self.env["I3SOCK"] = path
                return
            if self.wm.poll() is not None:
                self.stop()
                raise unittest.SkipTest("i3 exited at startup")
            time.sleep(0.2)
        self.stop()
        raise unittest.SkipTest("i3 never wrote an IPC socket")


class HeadlessOpenbox(HeadlessXvfb):
    """Openbox on an Xvfb -- the X11 window manager the LXQt session runs, with none of LXQt around it."""

    WM = "openbox"


def compositor_pids():
    """Headless compositors this user is running, as (pid, config) pairs.

    A test that boots one names its own configuration file under a temporary
    directory, which is what distinguishes it from the compositor somebody is
    actually sitting in.  Read from /proc rather than by running ps, for the
    same reason daemon_pids does: no subprocess, and nothing to parse."""
    out = []
    uid = os.getuid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if os.stat("/proc/" + entry).st_uid != uid:
                continue
            with open("/proc/%s/cmdline" % entry, "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if not argv or os.path.basename(argv[0].decode("utf-8", "replace")) != "sway":
            continue
        conf = ""
        for i, a in enumerate(argv):
            if a in (b"-c", b"--config") and i + 1 < len(argv):
                conf = argv[i + 1].decode("utf-8", "replace")
        out.append((int(entry), conf))
    return out


def stop_compositor(pid, timeout=5.0):
    """SIGTERM, then SIGKILL, then wait. True when it is gone."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + timeout / 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
    return False


# -- the repository itself ----------------------------------------------------

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def documents(root=None):
    """Every markdown file in the tree, as relative name -> text.

    Three walkers said this: scripts/check-docs.py, test_release_deb.py and
    the anchor checker. They agreed on the skip list (.git, __pycache__,
    node_modules) and on errors="replace", which matters because the media/
    notes carry bytes no encoding claims."""
    root = ROOT if root is None else root
    out = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", "node_modules")]
        for n in names:
            if n.endswith(".md"):
                path = os.path.join(base, n)
                with open(path, encoding="utf-8", errors="replace") as f:
                    out[os.path.relpath(path, root)] = f.read()
    return out


# -- slicing a shell script ---------------------------------------------------
#
# The installers and debian/enable-bridge are POSIX sh, and the interesting
# parts of them are branches that only a real GNOME session reaches. Running
# the block itself -- sliced out of the shipped file, so it cannot drift from
# what ships -- against the fake tools below is the only way to test them
# without one. Lifted from test_overlap_force.TheInstallerSeesThePackagesCopy,
# where the pair was written by hand for two blocks of install-overlap.sh.

def sh_block(path, first, last):
    """The text of `path` from the line containing `first` through the one
    containing `last`, inclusive of both markers.

    Both are matched as plain substrings, `last` searched for after `first`:
    a marker is a line of the script quoted verbatim in the test, so a slice
    that stops matching is a script that changed under it, which is the point."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index(first)
    end = src.index(last, start) + len(last)
    return src[start:end]


def sh_function(path, name):
    """The definition of the POSIX-sh function `name` in `path`.

    The closing brace has to be the one at column 0 -- every function in these
    scripts is written that way, and a nested `}` inside a case arm is
    indented -- so this is exact rather than a guess at nesting."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    head = "%s() {" % name
    for i, line in enumerate(lines):
        if line.strip() == head or line.startswith(head):
            for j in range(i + 1, len(lines)):
                if lines[j] == "}":
                    return "\n".join(lines[i:j + 1]) + "\n"
            raise AssertionError("%s: %s() is never closed at column 0" % (path, name))
    raise AssertionError("%s: no function %s()" % (path, name))


# -- a GNOME desktop's command line, faked ------------------------------------
#
# debian/enable-bridge and the two install-*.sh scripts are the only code in
# the project that runs as the user's session starts, and every branch in them
# is chosen by what `gsettings`, `gnome-extensions` and `gdbus` answer. None of
# those exists in a container, and the ones on a developer's machine would
# write into that developer's real dconf -- which is what made these scripts
# untested until now. So they are stand-ins on a PATH of their own, over one
# state file the test seeds and reads back.
#
# The state file is lines:
#     schemas=org.gnome.shell org.gnome.desktop.interface
#     org.gnome.shell/enabled-extensions=['a@b']
#     loaded=w11-bridge@w11
# `schemas=` is what `gsettings list-schemas` prints (the check that decides
# whether this is a GNOME session at all); a `loaded=` line is an extension the
# running shell has seen, which is exactly what decides whether
# `gnome-extensions enable` works or refuses with "does not exist" -- the
# refusal that sends enable-bridge down its gsettings fallback.
#
# Every stub appends its whole command line to $FAKE_LOG when that is set, so
# "made no gsettings call at all" is an assertion and not an absence.

_FAKE_LIB = r"""# sourced by the fake gsettings and gnome-extensions
STATE='__STATE__'

log() {
    if [ -n "${FAKE_LOG:-}" ]; then
        printf '%s\n' "$*" >> "$FAKE_LOG"
    fi
    return 0
}

state_get() {           # schema key -> value on stdout, rc 1 when unset
    v=$(sed -n "s|^$1/$2=||p" "$STATE" 2>/dev/null | tail -n 1)
    [ -n "$v" ] || return 1
    printf '%s\n' "$v"
}

state_put() {           # schema key value
    tmp="$STATE.$$"
    grep -v "^$1/$2=" "$STATE" > "$tmp" 2>/dev/null || :
    printf '%s/%s=%s\n' "$1" "$2" "$3" >> "$tmp"
    mv "$tmp" "$STATE"
}

has_schema() {
    sed -n 's/^schemas=//p' "$STATE" 2>/dev/null | tr ' ' '\n' | grep -qx "$1"
}

items() {               # a GVariant array of strings -> one per line
    # printf with the newline, not without: sed keeps a missing final newline,
    # and a list whose last item had none used to be concatenated with the next
    # one ("other@x" + "w11-bridge@w11" as a single uuid).
    printf '%s\n' "$1" | tr ',' '\n' | sed -n "s/.*'\([^']*\)'.*/\1/p"
}

render() {              # items on stdin -> a GVariant array of strings
    out=''
    while IFS= read -r it; do
        [ -n "$it" ] || continue
        if [ -z "$out" ]; then out="'$it'"; else out="$out, '$it'"; fi
    done
    if [ -z "$out" ]; then printf '@as []\n'; else printf '[%s]\n' "$out"; fi
}

list_add() {            # schema key item
    cur=$(state_get "$1" "$2" || printf '@as []')
    if items "$cur" | grep -qx "$3"; then return 0; fi
    new=$({ items "$cur"; printf '%s\n' "$3"; } | render)
    state_put "$1" "$2" "$new"
}

list_del() {            # schema key item
    cur=$(state_get "$1" "$2" || printf '@as []')
    new=$(items "$cur" | grep -vx "$3" | render)
    state_put "$1" "$2" "$new"
}
"""

_FAKE_GSETTINGS = r"""#!/bin/sh
# gsettings(1), as far as the install scripts use it. FAKE_GSETTINGS_SET_FAILS
# makes `set` fail the way a locked-down or read-only dconf does.
. '__BIN__/_fakelib.sh'
log "gsettings $*"
case "${1:-}" in
    list-schemas)
        sed -n 's/^schemas=//p' "$STATE" 2>/dev/null | tr ' ' '\n' | grep -v '^$'
        exit 0
        ;;
    get)
        has_schema "$2" || { printf 'No such schema "%s"\n' "$2" >&2; exit 1; }
        state_get "$2" "$3" && exit 0
        # An unset key is NOT an error: gsettings answers the schema default.
        # Read out of org.gnome.shell.gschema.xml in gnome-shell-common
        # 50.1-0ubuntu1.2 (resolute): enabled-extensions and disabled-extensions
        # default to [] -- which gsettings prints as `@as []` -- and
        # disable-user-extensions to false.  Every caller reads with
        # `|| echo '[]'` (install-bridge.sh:208 among them), so a fake that
        # failed here steered a fresh desktop into the `[]` arm and the
        # `@as []` arm a real one produces was never taken.
        if [ "$2" = org.gnome.shell ]; then
            case "$3" in
                enabled-extensions|disabled-extensions) printf '@as []\n'; exit 0 ;;
                disable-user-extensions) printf 'false\n'; exit 0 ;;
            esac
        fi
        printf 'No such key "%s"\n' "$3" >&2
        exit 1
        ;;
    set)
        has_schema "$2" || { printf 'No such schema "%s"\n' "$2" >&2; exit 1; }
        if [ -n "${FAKE_GSETTINGS_SET_FAILS:-}" ]; then
            printf 'gsettings: %s\n' "$FAKE_GSETTINGS_SET_FAILS" >&2
            exit 1
        fi
        state_put "$2" "$3" "$4"
        exit 0
        ;;
esac
printf 'gsettings: unknown command %s\n' "${1:-}" >&2
exit 2
"""

_FAKE_GNOME_EXTENSIONS = r"""#!/bin/sh
# gnome-extensions(1). It talks to the running shell, so a uuid the shell has
# never seen is refused -- which is the whole reason the callers have a
# gsettings fallback. A `loaded=` line in the state file is that uuid.
. '__BIN__/_fakelib.sh'
log "gnome-extensions $*"
uuid="${2:-}"
case "${1:-}" in
    enable|disable)
        if [ -n "${FAKE_GNOME_EXTENSIONS_FAILS:-}" ] \
           || ! grep -qx "loaded=$uuid" "$STATE" 2>/dev/null; then
            printf 'Extension "%s" does not exist\n' "$uuid" >&2
            exit 1
        fi
        if [ "$1" = enable ]; then
            list_add org.gnome.shell enabled-extensions "$uuid"
            list_del org.gnome.shell disabled-extensions "$uuid"
        else
            list_add org.gnome.shell disabled-extensions "$uuid"
            list_del org.gnome.shell enabled-extensions "$uuid"
        fi
        exit 0
        ;;
    list)
        sed -n 's/^loaded=//p' "$STATE" 2>/dev/null
        exit 0
        ;;
    info)
        grep -qx "loaded=$uuid" "$STATE" 2>/dev/null \
            || { printf 'Extension "%s" does not exist\n' "$uuid" >&2; exit 1; }
        printf '%s\n  State: %s\n' "$uuid" "${FAKE_EXT_STATE_NAME:-ENABLED}"
        exit 0
        ;;
esac
exit 2
"""

_FAKE_GDBUS = r"""#!/bin/sh
# `gdbus call --session ...`, in the four shapes the installers parse with sed:
# NameHasOwner, the ShellVersion property, ListExtensions and GetExtensionInfo.
# Each answers from an environment variable, so a test picks the session it
# wants (FAKE_SHELL_VERSION=51.beta is stonking-gnome; unset means no shell on
# the bus at all, which is gdbus exiting 1).
. '__BIN__/_fakelib.sh'
log "gdbus $*"
case "$*" in
    *NameHasOwner*)
        printf '(%s,)\n' "${FAKE_NAME_OWNED:-false}"
        ;;
    *ShellVersion*)
        [ -n "${FAKE_SHELL_VERSION:-}" ] || exit 1
        printf "(<'%s'>,)\n" "$FAKE_SHELL_VERSION"
        ;;
    *ListExtensions*)
        [ -n "${FAKE_EXT_LOADED:-}" ] || exit 1
        printf "({'%s': <{'uuid': <'%s'>}>},)\n" "$FAKE_EXT_LOADED" "$FAKE_EXT_LOADED"
        ;;
    *GetExtensionInfo*)
        [ -n "${FAKE_EXT_STATE:-}" ] || exit 1
        printf "({'uuid': <'%s'>, 'state': <%s>},)\n" "${FAKE_EXT_LOADED:-}" "$FAKE_EXT_STATE"
        ;;
    *)
        exit 1
        ;;
esac
exit 0
"""

_FAKE_SUDO = r"""#!/bin/sh
# `sudo -u USER cmd...`: run the command right here, as this user, so the
# TARGET_USER/RUNTIME_DIR/BUS_ADDR block can be exercised without a password
# prompt or a second uid. FAKE_SUDO_FAILS makes it refuse.
. '__BIN__/_fakelib.sh'
log "sudo $*"
[ -z "${FAKE_SUDO_FAILS:-}" ] || exit 1
while [ $# -gt 0 ]; do
    case "$1" in
        -u|-g) shift 2 ;;
        -n|-H|-E|-i) shift ;;
        --) shift; break ;;
        *) break ;;
    esac
done
exec "$@"
"""

_FAKE_RUNUSER = r"""#!/bin/sh
# `runuser -u USER -- cmd...`, the same way.
. '__BIN__/_fakelib.sh'
log "runuser $*"
[ -z "${FAKE_RUNUSER_FAILS:-}" ] || exit 1
while [ $# -gt 0 ]; do
    case "$1" in
        -u|-g) shift 2 ;;
        -l|-p) shift ;;
        --) shift; break ;;
        *) break ;;
    esac
done
exec "$@"
"""

_FAKE_CHOWN = r"""#!/bin/sh
# chown(1): recorded, never done -- a test runs as one uid and has no second.
. '__BIN__/_fakelib.sh'
log "chown $*"
exit 0
"""

_FAKE_ID = r"""#!/bin/sh
# id(1), the two forms the installers use. FAKE_UID/FAKE_USER are the answer
# for anybody but root; FAKE_ID_UNKNOWN is a name that does not resolve.
. '__BIN__/_fakelib.sh'
log "id $*"
who="${2:-}"
if [ -n "${FAKE_ID_UNKNOWN:-}" ] && [ "$who" = "$FAKE_ID_UNKNOWN" ]; then
    printf "id: '%s': no such user\n" "$who" >&2
    exit 1
fi
case "${1:-}" in
    -u)
        if [ "$who" = root ]; then echo 0; else printf '%s\n' "${FAKE_UID:-1000}"; fi
        ;;
    -un|-nu)
        if [ "$who" = root ]; then echo root; else printf '%s\n' "${FAKE_USER:-tester}"; fi
        ;;
    *)
        exit 2
        ;;
esac
exit 0
"""

_FAKE_GETENT = r"""#!/bin/sh
# `getent passwd NAME`, answered out of FAKE_PASSWD (newline-separated lines).
. '__BIN__/_fakelib.sh'
log "getent $*"
case "${1:-}" in
    passwd)
        printf '%s\n' "${FAKE_PASSWD:-}" | grep "^${2:-}:" || exit 2
        exit 0
        ;;
esac
exit 2
"""

_FAKE_DPKG = r"""#!/bin/sh
# `dpkg -S PATH`: FAKE_DPKG_S is the package that owns it (the installers
# refuse to touch dpkg's payload), unset is "no path found", which is rc 1.
. '__BIN__/_fakelib.sh'
log "dpkg $*"
case "${1:-}" in
    -S|--search)
        if [ -n "${FAKE_DPKG_S:-}" ]; then
            printf '%s: %s\n' "$FAKE_DPKG_S" "${2:-}"
            exit 0
        fi
        printf 'dpkg-query: no path found matching pattern %s\n' "${2:-}" >&2
        exit 1
        ;;
esac
exit 2
"""

FAKE_GNOME_TOOLS = {
    "gsettings": _FAKE_GSETTINGS,
    "gnome-extensions": _FAKE_GNOME_EXTENSIONS,
    "gdbus": _FAKE_GDBUS,
    "sudo": _FAKE_SUDO,
    "runuser": _FAKE_RUNUSER,
    "chown": _FAKE_CHOWN,
    "id": _FAKE_ID,
    "getent": _FAKE_GETENT,
    "dpkg": _FAKE_DPKG,
}


def fake_gnome_bin(dirpath, state_path, tools=None):
    """Write the fake GNOME command line into `dirpath` over `state_path`.

    Put `dirpath` at the FRONT of PATH: `id` and `getent` shadow the real ones
    on purpose, because what the installers do with them (decide which user's
    session to talk to) has to be testable without a second account. Returns
    `dirpath`, so it composes into a PATH in one line.

    `tools` names a subset when a test wants the real thing for the rest --
    fake_gnome_bin(d, s, ["gsettings"]) leaves `id` alone."""
    os.makedirs(dirpath, exist_ok=True)
    lib = os.path.join(dirpath, "_fakelib.sh")
    with open(lib, "w", encoding="utf-8") as fh:
        fh.write(_FAKE_LIB.replace("__STATE__", state_path))
    for name in (tools if tools is not None else FAKE_GNOME_TOOLS):
        path = os.path.join(dirpath, name)
        src = FAKE_GNOME_TOOLS[name].replace("__BIN__", dirpath)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        os.chmod(path, 0o755)
    if not os.path.exists(state_path):
        fake_gnome_state(state_path)
    return dirpath


def fake_gnome_state(path, schemas=("org.gnome.shell",), loaded=(), settings=None):
    """Seed the state file fake_gnome_bin() reads.

    `schemas` is what `gsettings list-schemas` lists -- pass () for a session
    that is not GNOME at all. `loaded` is the uuids the running shell has seen,
    which are the only ones `gnome-extensions enable` will accept. `settings`
    is {"org.gnome.shell/enabled-extensions": "@as []"} -- the GVariant text,
    verbatim, because "@as []" and "[]" are different strings to the scripts
    and both happen on a real desktop."""
    lines = []
    if schemas:
        lines.append("schemas=%s" % " ".join(schemas))
    for uuid in loaded:
        lines.append("loaded=%s" % uuid)
    for key, value in sorted((settings or {}).items()):
        lines.append("%s=%s" % (key, value))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("".join(ln + "\n" for ln in lines))
    return path


def gnome_setting(state_path, key):
    """One setting back out of the state file, or None -- `key` is
    "org.gnome.shell/enabled-extensions"."""
    found = None
    with open(state_path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(key + "="):
                found = line[len(key) + 1:].rstrip("\n")
    return found


def gnome_list(state_path, key):
    """That setting parsed as a GVariant array of strings; () when unset."""
    raw = gnome_setting(state_path, key)
    if raw is None:
        return ()
    return tuple(re.findall(r"'([^']*)'", raw))


# -- node, running the two GNOME extensions -----------------------------------
#
# Not one line of either extension.js was ever executed by this suite: they
# are ES modules for gjs, importing `gi://Meta` and
# `resource:///org/gnome/shell/ui/main.js`, and there is no gjs in a container
# and no GNOME session in CI. node 22 will run them as they are once those two
# specifiers resolve, which is what tests/fixtures/gjs/loader.mjs does --
# every gi:// namespace and every shell resource maps to a recording double
# under tests/fixtures/gjs/stubs/. The shipped files are imported by their real
# path and are not copied, patched or sliced.
#
# Proven on node v22.22.1 on this host for both extensions: the module
# evaluates, the default export constructs, and `_setState` on a Meta.Window
# double produces the maximize call the v3 rule says it should.

NODE = shutil.which("node") or shutil.which("nodejs")
GJS_DIR = os.path.join(ROOT, "tests", "fixtures", "gjs")
GJS_STUBS = os.path.join(GJS_DIR, "stubs")
GJS_LOADER = os.path.join(GJS_DIR, "loader.mjs")

BRIDGE_EXT = os.path.join(ROOT, "gnome", "w11-bridge@w11")
OVERLAP_EXT = os.path.join(ROOT, "gnome", "w11-overlap@w11")

skip_without_node = unittest.skipIf(NODE is None,
                                    "no node to run the GNOME extensions")


def js_harness(module_path, case_source, arg=None, timeout=60):
    """Run `case_source` under node with the gi:// loader; return its JSON.

    `case_source` is a whole ES module. It imports what it needs -- the
    extension by its absolute path, the stubs by the same specifiers the
    extension uses (`import Meta from 'gi://Meta'` in the case is the same
    module object the extension got, so configuring one configures the other)
    -- and prints exactly one JSON document, which is what comes back.

    Four tokens are substituted before it is written, so a case need not
    rebuild paths that are already known here:

        @MODULE@   `module_path`, the extension under test
        @STUBS@    tests/fixtures/gjs/stubs
        @GJS@      tests/fixtures/gjs
        @ROOT@     the repository root

    `arg` is handed to the case as JSON in process.argv[2]. A non-zero exit or
    a stdout that is not JSON raises AssertionError carrying both streams --
    a JS exception's stack is the thing a failing case needs to show."""
    if NODE is None:
        raise unittest.SkipTest("no node to run the GNOME extensions")
    tmp = tempfile.mkdtemp(prefix="gjs-case-")
    try:
        src = (case_source.replace("@MODULE@", module_path)
               .replace("@STUBS@", GJS_STUBS)
               .replace("@GJS@", GJS_DIR)
               .replace("@ROOT@", ROOT))
        path = os.path.join(tmp, "case.mjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        rc = subprocess.run(
            [NODE, "--no-warnings", "--experimental-loader", GJS_LOADER,
             path, json.dumps(arg)],
            capture_output=True, text=True, timeout=timeout)
        if rc.returncode != 0:
            raise AssertionError("node exited %d\n--- stdout ---\n%s\n--- stderr ---\n%s"
                                 % (rc.returncode, rc.stdout, rc.stderr))
        try:
            return json.loads(rc.stdout)
        except ValueError as e:
            raise AssertionError("the case printed no JSON (%s)\n--- stdout ---\n%s\n"
                                 "--- stderr ---\n%s" % (e, rc.stdout, rc.stderr))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# -- a sway that is wedged, dying or lying ------------------------------------

class UnixServer:
    """A listening AF_UNIX socket that runs `handler(conn)` per connection.

    The same shape test_wire_hardening's private _Server has; here so that the
    sway double below can live beside the tools that need it."""

    def __init__(self, handler, prefix="fake-sock-"):
        self.dir = tempfile.mkdtemp(prefix=prefix)
        self.path = os.path.join(self.dir, "sock")
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.bind(self.path)
        self.s.listen(4)
        # A blocked accept() is not woken by close() on Linux, so the accept
        # loop polls instead: without this the thread sits in accept() for the
        # life of the interpreter and every server a run makes leaves one.
        # (Python hands the accepted connection back in blocking mode whatever
        # the listening socket's timeout is, so the handlers are unaffected.)
        self.s.settimeout(0.2)
        self.handler = handler
        self.conns = []
        # Set by close(): a handler that has nothing to answer (FakeSway's
        # wedged mode) waits on this rather than on the clock, so its thread
        # ends with the test instead of living to the end of the interpreter.
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                c, _ = self.s.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        try:
            self.handler(c)
        except OSError:
            pass

    def close(self):
        self._stop.set()
        try:
            self.s.close()
        except OSError:
            pass
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)


I3_MAGIC = b"i3-ipc"
RUN_COMMAND = 0
GET_WORKSPACES = 1
GET_TREE = 4
GET_OUTPUTS = 3
GET_VERSION = 7


def iframe(mtype, payload):
    """One i3-ipc frame: the magic, a u32 length, a u32 type, the payload."""
    return I3_MAGIC + struct.pack("<II", len(payload), mtype) + payload


# sway 1.11's GET_OUTPUTS, trimmed to the fields wxrandr and wdotool read.
# The names are the ones a headless sway hands out on this host (HEADLESS-1,
# HEADLESS-2 after `swaymsg create_output`).
SWAY_OUTPUTS = [
    {"id": 1, "name": "HEADLESS-1", "make": "Unknown", "model": "headless",
     "serial": "Unknown", "active": True, "dpms": True, "primary": False,
     "scale": 1.0, "subpixel_hinting": "unknown", "transform": "normal",
     "current_workspace": "1", "focused": True,
     "rect": {"x": 0, "y": 0, "width": 1280, "height": 720},
     "current_mode": {"width": 1280, "height": 720, "refresh": 60000},
     "modes": [{"width": 1280, "height": 720, "refresh": 60000}]},
    {"id": 2, "name": "HEADLESS-2", "make": "Unknown", "model": "headless",
     "serial": "Unknown", "active": True, "dpms": True, "primary": False,
     "scale": 1.0, "subpixel_hinting": "unknown", "transform": "normal",
     "current_workspace": "2", "focused": False,
     "rect": {"x": 1280, "y": 0, "width": 1280, "height": 1024},
     "current_mode": {"width": 1280, "height": 1024, "refresh": 60000},
     "modes": [{"width": 1280, "height": 1024, "refresh": 60000}]},
]

SWAY_WORKSPACES = [{"num": 3, "name": "3", "focused": True, "output": "HEADLESS-1"}]


class FakeSway(UnixServer):
    """A sway IPC socket that answers badly, in one of six ways.

    `mode`:
      "ok"       every request answered as sway 1.11 would
      "gone"     one answer, then the connection is closed under the client
      "badjson"  a well-framed reply whose payload is not JSON
      "wedged"   the connection is accepted and never answered again -- a
                 compositor stuck in its own event loop, which the kernel
                 accepts for; only the client's own deadline ends the wait
      "refuse"   RUN_COMMAND answers [{"success": false, "error": ...}], which
                 is what sway says about an output layout it will not take
      "partial"  GET_OUTPUTS rows with no `rect` at all -- the shape that used
                 to be a KeyError somewhere above the user

    `outputs` and `workspaces` override the two payloads. Everything else is
    answered with the workspace list, which is what the wdotool sway backend
    asks for and all the older double ever sent.

    `dialect="i3"` answers as a live i3 4.25.1 did instead, from the recordings in tests/fixtures/i3/ [M
    recon2/i3.md §1, §2b]:

      GET_VERSION    major 4 (sway is 1.x and i3 has been 4.x since 2011), `human_readable`
                     "4.25.1 (2026-02-06)"
      GET_OUTPUTS    name/active/primary/rect/current_workspace and NOTHING else -- no modes, no current_mode,
                     no transform, no scale, no make/model/serial -- plus the pseudo-output `xroot-0`
                     (`active: false`) covering the X screen
      GET_WORKSPACES the one workspace, with i3's 47-bit `id`
      GET_TREE       `get_tree_floating.json`: 47-bit con ids, `window` carrying the X id, no `app_id`, no
                     `pid`, no `visible`, and the floated `w11smoke` xterm (X id 8388621) wrapped
                     `floating_con -> con`. Pass `tree=fixture_json("i3", "get_tree.json")` for the same
                     session with nothing floating.
      RUN_COMMAND    i3's 30-token parse error to any `output ...`, which is what every wxrandr apply died
                     with, and `{"success": true}` to anything else

    A test that wants i3's parse error for a different command passes `refuse_prefix`."""

    ERROR = "Cannot apply output configuration"

    def __init__(self, mode, outputs=None, workspaces=None, dialect="sway",
                 tree=None, refuse_prefix="output"):
        self.mode = mode
        self.dialect = dialect
        self.refuse_prefix = refuse_prefix
        i3 = dialect == "i3"
        if i3:
            self.outputs = fixture_json("i3", "get_outputs.json") if outputs is None else outputs
            self.workspaces = (fixture_json("i3", "get_workspaces.json")
                               if workspaces is None else workspaces)
            self.tree = fixture_json("i3", "get_tree_floating.json") if tree is None else tree
            self.version = fixture_json("i3", "get_version.json")
            self.parse_error = fixture_json("i3", "run_output_pos.json")
        else:
            self.outputs = SWAY_OUTPUTS if outputs is None else outputs
            self.workspaces = SWAY_WORKSPACES if workspaces is None else workspaces
            self.tree = tree
            self.version = {"major": 1, "minor": 11, "patch": 0, "human_readable": "1.11"}
            self.parse_error = None
        self.requests = []
        super().__init__(self._serve, prefix="fake-sway-")

    def _payload(self, mtype, body=""):
        if mtype == RUN_COMMAND:
            if self.dialect == "i3" and body.strip().startswith(self.refuse_prefix):
                # i3 has no `output` command at all; the layout belongs to the X
                # server there, and every apply came back as this parse error
                return self.parse_error
            if self.mode == "refuse":
                return [{"success": False, "error": self.ERROR}]
            return [{"success": True}]
        if mtype == GET_VERSION:
            return self.version
        if mtype == GET_TREE and self.tree is not None:
            return self.tree
        if mtype == GET_OUTPUTS:
            if self.mode == "partial":
                return [{k: v for k, v in o.items() if k != "rect"}
                        for o in self.outputs]
            return self.outputs
        return self.workspaces

    def _serve(self, c):
        if self.mode == "wedged":
            self._stop.wait()
            return
        n, buf = 0, b""
        while True:
            data = c.recv(65536)
            if not data:
                return
            buf += data
            while len(buf) >= 14:
                ln, mt = struct.unpack("<II", buf[6:14])
                if len(buf) < 14 + ln:
                    break
                self.requests.append((mt, buf[14:14 + ln].decode("utf-8", "replace")))
                buf = buf[14 + ln:]
                n += 1
                if self.mode == "badjson":
                    c.sendall(iframe(mt, b"{not json"))
                    continue
                c.sendall(iframe(mt, json.dumps(
                    self._payload(mt, self.requests[-1][1])).encode()))
                if self.mode == "gone" and n == 1:
                    time.sleep(0.05)
                    c.close()
                    return


# -- Hyprland's IPC, faked ----------------------------------------------------

#: `hyprctl cursorpos` answered the text `640, 360` on the live 0.53.3 after a
#: `mousemove 640 360`, 0 px error twice [M recon2/hyprland.md §3]. The `j/`
#: shape below is [R]: it is what HyprCtl.cpp writes for the same two numbers,
#: read off the source and not measured -- nothing recorded `j/cursorpos`. The
#: Hyprland batch's live run records it (requests-batch-5.md), and this double
#: is corrected from that recording if the keys differ.
HYPR_CURSORPOS = {"x": 640, "y": 360}

#: The five event lines a live Hyprland wrote to `.socket2.sock` when a foot
#: window opened, in order and byte for byte [M recon2/hyprland.md §2].
HYPR_EVENT_LINES = (
    "windowtitle>>59daae69e360",
    "windowtitlev2>>59daae69e360,foot",
    "openwindow>>59daae69e360,1,foot,foot",
    "activewindow>>foot,foot",
    "activewindowv2>>59daae69e360",
)


class FakeHypr(UnixServer):
    """Hyprland's request/response socket (`.socket.sock`), in one of six moods.

    The protocol is the one a 10-line AF_UNIX client proved against a live 0.53.3: send the request as plain
    text, read the reply to EOF, and the connection is finished -- one connection per request, which is why
    every mode below is expressed as "what this one connection does" [M recon2/hyprland.md §2]. A request
    starting `j/` is answered with JSON; everything else with Hyprland's own words.

    `mode`:
      "ok"       every request answered from tests/fixtures/hypr/*.json
      "gone"     the connection is closed with nothing written -- the compositor that exited between our
                 find_hypr_socket() and our connect()
      "badjson"  a `j/` request answered with bytes that are not JSON
      "wedged"   accepted and never answered; only the client's own deadline ends it
      "short"    a JSON reply truncated mid-object, which is what a compositor killed mid-write leaves
      "refuse"   a dispatch answered `Invalid dispatcher`

    All three of the strings this double used to guess were recorded on the live resolute-hypr golden on
    2026-09-09 (Hyprland 0.53.3), and all three were right: `j/cursorpos` really answers an object with the
    keys `x` and `y`, `dispatch notaverb` really answers `Invalid dispatcher`, and an unknown `j/` request
    (and an unknown bare one) really answers `unknown request` [requests-batch-5.md items 1-3]. Only
    HYPR_CURSORPOS's numbers are the recon's session rather than that one.

    Every request is appended to `self.requests`, so a test can say which strings were sent and that nothing
    else was."""

    #: Hyprland's refusal text for an unknown dispatch verb. [R] from HyprCtl.cpp, not measured.
    INVALID = "Invalid dispatcher"

    def __init__(self, mode="ok", payloads=None):
        self.mode = mode
        self.requests = []
        self.payloads = {
            "monitors": fixture_json("hypr", "monitors.json"),
            "clients": fixture_json("hypr", "clients.json"),
            "workspaces": fixture_json("hypr", "workspaces.json"),
            "devices": fixture_json("hypr", "devices.json"),
            "activewindow": fixture_json("hypr", "activewindow.json"),
            "activeworkspace": fixture_json("hypr", "activeworkspace.json"),
            "version": fixture_json("hypr", "version.json"),
            "cursorpos": HYPR_CURSORPOS,
        }
        if payloads:
            self.payloads.update(payloads)
        super().__init__(self._serve, prefix="fake-hypr-")

    def reply_for(self, req: str) -> bytes:
        """The bytes this request is answered with (before the mode mangles them)."""
        if req.startswith("j/"):
            name = req[2:].strip()
            if name == "monitors all" and name not in self.payloads:
                # `j/monitors all` is a request of its own, not a flag: it is the only one that lists a
                # DISABLED head, where the plain form drops the row entirely (measured live on
                # resolute-hypr, Hyprland 0.53.3, 2026-09-09, after `--output Virtual-3 --off`:
                # `j/monitors` gave Virtual-1 and Virtual-2, `j/monitors all` those two plus Virtual-3 with
                # `disabled: true`, the same keys and all 26 availableModes). `all` is a superset of the
                # plain answer, so a double that sets only "monitors" answers both -- which is what keeps
                # every test written before wxrandr/hypr.py started asking for `all`. A test about a
                # disabled head sets "monitors all" itself; the recorded one is
                # tests/fixtures/hypr/monitors-all-one-disabled.json.
                name = "monitors"
            if name not in self.payloads:
                # measured live 2026-09-09: an unknown `j/` request really is answered `unknown request`
                return b"unknown request"
            return json.dumps(self.payloads[name]).encode()
        verb = req.split(" ", 1)[0]
        if verb in ("dispatch", "keyword"):
            if self.mode == "refuse" and verb == "dispatch":
                return self.INVALID.encode()
            return b"ok"
        if req.strip() in self.payloads:
            return json.dumps(self.payloads[req.strip()]).encode()
        return self.INVALID.encode()

    def _serve(self, c):
        if self.mode == "wedged":
            self._stop.wait()
            return
        data = c.recv(65536)
        if not data:
            return
        req = data.decode("utf-8", "replace")
        self.requests.append(req)
        if self.mode == "gone":
            c.close()
            return
        if self.mode == "badjson":
            c.sendall(b"{not json")
        elif self.mode == "short":
            c.sendall(self.reply_for(req)[:12])
        else:
            c.sendall(self.reply_for(req))
        c.close()


class FakeHyprEvents(UnixServer):
    """Hyprland's event socket (`.socket2.sock`): a line stream, `event>>payload`.

    Constructed with the five recorded lines; `send(line)` writes another one to every live client, so a test
    can drive an events() iterator one step at a time instead of racing a script. `hold=True` writes nothing
    until the first `send()`, which is the compositor with nothing happening on it."""

    def __init__(self, lines=HYPR_EVENT_LINES, hold=False):
        self.lines = list(lines)
        self.hold = hold
        self.conns_seen = 0
        super().__init__(self._serve, prefix="fake-hypr-ev-")

    def _serve(self, c):
        self.conns_seen += 1
        if not self.hold:
            for line in self.lines:
                c.sendall((line + "\n").encode())
        self._stop.wait()

    def send(self, line: str):
        """One more event line, to everything connected."""
        for c in list(self.conns):
            try:
                c.sendall((line + "\n").encode())
            except OSError:
                pass


# -- Wayfire's IPC, faked -----------------------------------------------------

class FakeWayfire(UnixServer):
    """Wayfire's JSON IPC socket: a little-endian `<i` length prefix and a JSON body, both ways.

    The framing is measured, byte for byte -- the recorded ping went out as the four bytes 24 00 00 00 and
    then the 0x24-byte body `{"method": "stipc/ping", "data": {}}` [M recon2/wayfire.md §1.2]. The method
    table is the recorded captures under tests/fixtures/wayfire/, whose file names are the method with `/`
    written as `_`.

    `mode`:
      "ok"             every recorded method answered
      "nomethod"       every method answered `{"error": "No such method found!"}` -- which is the shape a
                       Wayfire with no `ipc-rules` plugin gives, and the reason the backend has an api gate
      "handler-error"  the other error shape, a handler that raised (the recorded one names the method and
                       the argument it was missing)
      "silent"         framed request read, nothing written back
      "gone"           the connection is closed after the first reply

    `self.calls` is [(method, data)] for everything asked."""

    NO_METHOD = "No such method found!"
    #: the second error shape, byte for byte off the live socket: a handler that
    #: raised names the method and what it was missing
    #: [M recon2/wayfire.md §1.2]
    HANDLER_ERROR = ('Error during execution of the handler for method '
                     '"window-rules/view-info": Missing "id"')

    #: the recorded captures, keyed by the method that produced them
    CAPTURES = ("list-methods", "window-rules/list-views", "window-rules/list-outputs",
                "window-rules/list-wsets", "window-rules/get_cursor_position",
                "wayfire/get-keyboard-state", "wayfire/configuration",
                "input/list-devices", "stipc/get_display", "create-headless-output")

    def __init__(self, mode="ok", answers=None):
        self.mode = mode
        self.calls = []
        self.answers = {m: fixture_json("wayfire", m.replace("/", "_") + ".json")
                        for m in self.CAPTURES}
        if answers:
            self.answers.update(answers)
        super().__init__(self._serve, prefix="fake-wayfire-")

    @staticmethod
    def frame(obj) -> bytes:
        """One message: `<i` length, then the JSON body."""
        body = json.dumps(obj).encode()
        return struct.pack("<i", len(body)) + body

    def reply_for(self, method, data):
        if self.mode == "nomethod":
            return {"error": self.NO_METHOD, "method": method}
        if self.mode == "handler-error":
            return {"error": self.HANDLER_ERROR}
        if method in self.answers:
            return self.answers[method]
        return {"error": self.NO_METHOD, "method": method}

    def _serve(self, c):
        buf, n = b"", 0
        while True:
            data = c.recv(65536)
            if not data:
                return
            buf += data
            while len(buf) >= 4:
                (ln,) = struct.unpack("<i", buf[:4])
                if len(buf) < 4 + ln:
                    break
                try:
                    req = json.loads(buf[4:4 + ln].decode("utf-8", "replace"))
                except ValueError:
                    req = {}
                buf = buf[4 + ln:]
                method = req.get("method", "")
                self.calls.append((method, req.get("data", {})))
                n += 1
                if self.mode == "silent":
                    continue
                c.sendall(self.frame(self.reply_for(method, req.get("data", {}))))
                if self.mode == "gone" and n == 1:
                    time.sleep(0.05)
                    c.close()
                    return


# -- a session leader that carries the variables ------------------------------

class leader_process:
    """A real process whose /proc/<pid>/comm is `name`, holding `env`.

    w11common.session finds the X display and the X authority cookie by walking
    /proc for the session's leader -- SDDM writes /tmp/xauth_<random>, which is
    in nobody's runtime directory and is not ~/.Xauthority, so on Plasma X11
    the leader's own environ is the only thing that knows where the cookie is.
    Testing that ranking needs processes with those names and those variables,
    and nothing else: comm is 15 characters, and `startplasma-x11` is exactly
    15, so the truncation is part of what this proves rather than something it
    dodges.

    The stand-in is a copy of /bin/sh blocked on `read`, NOT a copy of `sleep`:
    Ubuntu 26.04 ships uutils coreutils as one multicall binary, and a copy of
    /usr/bin/sleep under any other name exits 1 with "coreutils: unknown
    program 'startplasma-x11'". A test that copied `sleep` therefore skipped
    itself on this distribution and proved nothing. Blocking on a pipe this
    process holds open is also the safer lifetime: there is no timer to
    outlive, and a stand-in whose test died gets EOF and exits by itself.

    Use it as a context manager, or addCleanup(l.stop). It skips the test if
    the stand-in never reaches the name with its environment -- comm is set by
    execve, and /proc/<pid>/environ becomes readable a little after that."""

    SHELL = "/bin/sh"

    def __init__(self, name, env=None, seconds=None):
        self.name = name
        self.env = dict(env or {})
        self.dir = tempfile.mkdtemp(prefix="leader-")
        self.path = os.path.join(self.dir, name)
        shutil.copy(self.SHELL, self.path)
        # `read` is a shell builtin, so the shell does not exec anything over
        # itself and comm stays the name we gave the copy.
        self.proc = subprocess.Popen([self.path, "-c", "read _"], env=self.env,
                                     stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.pid = self.proc.pid
        # comm and environ do not become readable at the same instant: comm
        # answers the new name while /proc/<pid>/environ is still empty (a
        # window of tens of milliseconds, measured on this host). A caller
        # handed the pid in between would scan a leader with no variables at
        # all, which is the miss these tests exist to catch -- so wait for both.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            if self.comm() == name[:15] and set(self.env) <= set(self.environ()):
                return
            time.sleep(0.01)
        self.stop()
        raise unittest.SkipTest("the stand-in never reached comm=%s with its "
                                "environment" % name[:15])

    def comm(self):
        try:
            with open("/proc/%d/comm" % self.pid) as f:
                return f.read().strip()
        except OSError:
            return None

    def environ(self):
        """/proc/<pid>/environ, parsed -- the environment at execve, which is
        what w11common.session reads out of a session leader."""
        try:
            with open("/proc/%d/environ" % self.pid, "rb") as f:
                raw = f.read().decode("utf-8", "replace")
        except OSError:
            return {}
        out = {}
        for kv in raw.split("\0"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                out[k] = v
        return out

    def stop(self):
        try:
            self.proc.kill()
        except OSError:
            pass
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


# -- wl-mirror, stubbed -------------------------------------------------------
#
# wmirror spawns wl-mirror and then supervises it: the interesting behaviour is
# what the supervisor does when the helper lives, dies at once, or is killed
# under it, and none of that needs a compositor or a GPU. The libEGL line on
# stderr is real wl-mirror output on a headless box, and it is here because
# wmirror has to not treat it as a failure.
#
#   $WMIRROR_STUB_LOG   every invocation, with the WAYLAND_DISPLAY it saw
#   $WMIRROR_STUB_FAIL  exit 1 with wl-mirror's own "output not found" wording
#   $WMIRROR_STUB_LIFE  seconds to stay up (30 by default)

WL_MIRROR_STUB = """#!/bin/sh
# stand-in for wl-mirror
if [ -n "$WMIRROR_STUB_LOG" ]; then
    printf '%s wayland=%s\\n' "$*" "$WAYLAND_DISPLAY" >> "$WMIRROR_STUB_LOG"
fi
echo "libEGL warning: DRI2: failed to authenticate" >&2
if [ -n "$WMIRROR_STUB_FAIL" ]; then
    echo "error: options::find_output(): output NOPE not found" >&2
    exit 1
fi
exec sleep "${WMIRROR_STUB_LIFE:-30}"
"""


def write_wl_mirror_stub(dirpath, name="wl-mirror"):
    """Write WL_MIRROR_STUB into `dirpath` as `name`, executable; return it."""
    os.makedirs(dirpath, exist_ok=True)
    path = os.path.join(dirpath, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(WL_MIRROR_STUB)
    os.chmod(path, 0o755)
    return path


# -- the X11 wire doubles ------------------------------------------------------
#
# Moved here from tests/test_wwmctl_x11.py:52-291 unchanged (batch 1 of the xw11
# proxy): six test files already imported `FakeXServer` back out of that file by
# name, and the proxy's own `FakeUpstream` subclasses it, so it belongs where the
# project keeps its doubles. test_wwmctl_x11.py imports all four names back and
# stays the file everything else imports them from.


def _pad4(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 4)


def _recvn(conn, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def write_xauth(path: str, entries):
    """entries: (family, address, number, name, data) — the binary
    .Xauthority format (big-endian u16 lengths)."""
    with open(path, "wb") as f:
        for family, addr, num, name, data in entries:
            f.write(struct.pack(">H", family))
            for field in (addr, num, name, data):
                f.write(struct.pack(">H", len(field)) + field)


class FakeXServer(threading.Thread):
    """Just enough X server: setup handshake + the 8 requests the client
    uses. Properties live in self.props[(win, prop_name)] =
    (type_name, format, bytes); ChangeProperty writes back into it."""

    ROOTS = [0x5A, 0x5B]  # two screens, to exercise screen selection

    def __init__(self, sockdir, num=7, cookie=None, accept_empty=True):
        super().__init__(daemon=True)
        self.cookie = cookie
        self.accept_empty = accept_empty
        self.props = {}
        self.children = []          # QueryTree children of the root
        self.geometry = {}          # win -> (x, y, w, h) for GetGeometry
        self.translate = {}         # win -> (root_x, root_y)
        self.error_windows = set()  # BadWindow on any request naming these
        self.max_chunk_units = None  # cap GetProperty chunks (force the loop)
        self.fonts = {}             # name -> [(prop name, CARD32)]
        self.colors = {}            # LookupColor name -> (r16, g16, b16)
        self._open_fonts = {}       # fid -> name
        self.setup_attempts = []    # (auth_name, auth_data) per connection
        self.log = []               # parsed requests
        self._atoms = {}
        self._names = {}
        self.path = os.path.join(sockdir, "X%d" % num)
        self._ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._ls.bind(self.path)
        self._ls.listen(8)
        self._ls.settimeout(0.2)
        self._stopped = False
        self.start()

    def intern(self, name: str) -> int:
        a = self._atoms.get(name)
        if a is None:
            a = 100 + len(self._atoms)
            self._atoms[name] = a
            self._names[a] = name
        return a

    def set_prop(self, win, name, type_name, fmt, data):
        self.intern(name)
        self.intern(type_name)
        self.props[(win, name)] = (type_name, fmt, data)

    def stop(self):
        self._stopped = True
        self._ls.close()
        self.join(timeout=5)

    # -- server internals ---------------------------------------------------

    def run(self):
        # each connection gets a thread: tests keep several open at once
        while not self._stopped:
            try:
                conn, _ = self._ls.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_one, args=(conn,),
                             daemon=True).start()

    def _serve_one(self, conn):
        try:
            self._serve(conn)
        except (EOFError, OSError, AssertionError):
            pass
        finally:
            conn.close()

    def _serve(self, conn):
        order, _maj, _min, nlen, dlen = struct.unpack(
            "<BxHHHHxx", _recvn(conn, 12))
        assert order == 0x6C
        name = _recvn(conn, len(_pad4(b"x" * nlen)))[:nlen] if nlen else b""
        data = _recvn(conn, len(_pad4(b"x" * dlen)))[:dlen] if dlen else b""
        self.setup_attempts.append((name, data))
        if not self._auth_ok(name, data):
            reason = b"Authentication rejected by fake"
            conn.sendall(struct.pack("<BBHHH", 0, len(reason), 11, 0,
                                     len(_pad4(reason)) // 4)
                         + _pad4(reason))
            return
        conn.sendall(self._setup_reply())
        seq = 0
        while True:
            opcode, dbyte, rlen = struct.unpack("<BBH", _recvn(conn, 4))
            payload = _recvn(conn, (rlen - 1) * 4)
            seq = (seq + 1) & 0xFFFF
            self._dispatch(conn, opcode, dbyte, payload, seq)

    def _auth_ok(self, name, data):
        if not name and not data:
            return self.accept_empty
        return (self.cookie is not None
                and name == b"MIT-MAGIC-COOKIE-1" and data == self.cookie)

    def _setup_reply(self):
        vendor = b"FAKE"
        screens = b""
        for root in self.ROOTS:
            depth = struct.pack("<BxH4x", 24, 1) + b"\0" * 24  # 1 visual
            screens += struct.pack("<5I6HI4B", root, 0, 0, 0, 0,
                                   1280, 720, 300, 200, 1, 1,
                                   0x21, 0, 0, 24, 1) + depth
        extra = struct.pack("<4IHH8B4x", 1, 0x400000, 0x3FFFFF, 256,
                            len(vendor), 0xFFFF, len(self.ROOTS), 0,
                            0, 0, 32, 32, 8, 255)
        extra += _pad4(vendor) + screens
        return struct.pack("<BxHHH", 1, 11, 0, len(extra) // 4) + extra

    def _error(self, conn, seq, code, major, bad=0):
        conn.sendall(struct.pack("<BBHIHB21x", 0, code, seq, bad, 0, major))

    def _dispatch(self, conn, opcode, dbyte, payload, seq):
        if opcode == 16:  # InternAtom
            (n,) = struct.unpack_from("<H", payload, 0)
            name = payload[4:4 + n].decode("latin-1")
            self.log.append(("InternAtom", name, dbyte))
            atom = self._atoms.get(name, 0)
            if not atom and not dbyte:
                atom = self.intern(name)
            conn.sendall(struct.pack("<BBHII20x", 1, 0, seq, 0, atom))
        elif opcode == 20:  # GetProperty
            win, prop, _typ, offs, length = struct.unpack("<IIIII", payload)
            pname = self._names.get(prop, "?")
            self.log.append(("GetProperty", win, pname, offs))
            if win in self.error_windows:
                return self._error(conn, seq, 3, 20, bad=win)  # BadWindow
            entry = self.props.get((win, pname))
            if entry is None:
                conn.sendall(struct.pack("<BBHIIII12x", 1, 0, seq, 0,
                                         0, 0, 0))
                return
            tname, fmt, data = entry
            if self.max_chunk_units is not None:
                length = min(length, self.max_chunk_units)
            chunk = data[offs * 4:offs * 4 + length * 4]
            after = len(data) - offs * 4 - len(chunk)
            body = _pad4(chunk)
            conn.sendall(struct.pack("<BBHIIII12x", 1, fmt, seq,
                                     len(body) // 4, self.intern(tname),
                                     after, len(chunk) // (fmt // 8)) + body)
        elif opcode == 18:  # ChangeProperty
            win, prop, typ, fmt = struct.unpack_from("<IIIB", payload, 0)
            (n,) = struct.unpack_from("<I", payload, 16)
            data = payload[20:20 + n * (fmt // 8)]
            pname = self._names.get(prop, "?")
            tname = self._names.get(typ, "?")
            self.log.append(("ChangeProperty", win, pname, tname, fmt, data))
            self.props[(win, pname)] = (tname, fmt, data)
        elif opcode == 92:  # LookupColor
            cmap, n = struct.unpack_from("<IH", payload, 0)
            name = payload[8:8 + n].decode("latin-1")
            self.log.append(("LookupColor", cmap, name))
            rgb = self.colors.get(name)
            if rgb is None:
                return self._error(conn, seq, 15, 92, bad=0)  # BadName
            r, g, b = rgb
            conn.sendall(struct.pack("<BxHIHHHHHH12x", 1, seq, 0,
                                     r, g, b, r, g, b))
        elif opcode == 45:  # OpenFont
            fid, n = struct.unpack_from("<IH", payload, 0)
            name = payload[8:8 + n].decode("latin-1")
            self.log.append(("OpenFont", fid, name))
            if name not in self.fonts:
                return self._error(conn, seq, 15, 45, bad=0)  # BadName
            self._open_fonts[fid] = name
        elif opcode == 47:  # QueryFont
            (fid,) = struct.unpack("<I", payload)
            props = self.fonts.get(self._open_fonts.get(fid), [])
            self.log.append(("QueryFont", fid))
            # the reply's fixed part is 60 bytes: 32 of header plus 28 of
            # body, with the FONTPROP count at overall offset 46
            body = bytearray(28)
            struct.pack_into("<H", body, 14, len(props))
            for n, v in props:
                body += struct.pack("<II", self.intern(n), v)
            head = struct.pack("<BxHI", 1, seq, len(body) // 4) + b"\0" * 24
            conn.sendall(head + bytes(body))
        elif opcode == 46:  # CloseFont
            (fid,) = struct.unpack("<I", payload)
            self.log.append(("CloseFont", fid))
            self._open_fonts.pop(fid, None)
        elif opcode == 19:  # DeleteProperty
            win, prop = struct.unpack("<II", payload)
            pname = self._names.get(prop, "?")
            self.log.append(("DeleteProperty", win, pname))
            self.props.pop((win, pname), None)
        elif opcode == 25:  # SendEvent
            dest, mask = struct.unpack_from("<II", payload, 0)
            self.log.append(("SendEvent", dest, mask, payload[8:40]))
        elif opcode == 14:  # GetGeometry
            (win,) = struct.unpack("<I", payload)
            if win in self.error_windows:
                return self._error(conn, seq, 3, 14, bad=win)
            x, y, w, h = self.geometry.get(win, (0, 0, 0, 0))
            conn.sendall(struct.pack("<BBHIIhhHHH10x", 1, 24, seq, 0,
                                     self.ROOTS[0], x, y, w, h, 0))
        elif opcode == 40:  # TranslateCoordinates
            win, _dst, _sx, _sy = struct.unpack("<IIhh", payload)
            rx, ry = self.translate.get(win, (0, 0))
            conn.sendall(struct.pack("<BBHIIhh16x", 1, 1, seq, 0, 0, rx, ry))
        elif opcode == 15:  # QueryTree
            body = struct.pack("<%dI" % len(self.children), *self.children)
            conn.sendall(struct.pack("<BBHIIIH14x", 1, 0, seq,
                                     len(self.children), self.ROOTS[0], 0,
                                     len(self.children)) + body)
        elif opcode == 43:  # GetInputFocus (the client's sync)
            self.log.append(("GetInputFocus",))
            conn.sendall(struct.pack("<BBHII20x", 1, 0, seq, 0, 1))
        else:
            self._error(conn, seq, 1, opcode)  # BadRequest


# -- the xw11 proxy's doubles --------------------------------------------------
#
# FakeUpstream is FakeXServer with the four things a proxy needs and a wire
# client does not: BIG-REQUESTS framing (a zero 16-bit length is an 8-byte
# header once the connection enabled it, and libX11 enables it as request 2 of
# every connection -- recon/tools.md 2), a programmable QueryExtension table
# with the majors env 2.5 measured on the two real servers, a resource-id-base
# that steps by 0x200000 per connection (recon/wire.md 1.3), and the levers a
# test needs to be unkind: hold a reply back, push an event, hand over a file
# descriptor, go away.

#: What `QueryExtension` answers, name -> (major, first_event, first_error).
#: The majors are the ones measured on Xwayland under sway [recon/env.md 2.5];
#: nothing in xw11 may hard-code them, and a test that changes this table is how
#: that is proved (RANDR is 140 on Xvfb and 139 on Xwayland).
FAKE_EXTENSIONS = {
    "BIG-REQUESTS": (133, 0, 0),
    "XTEST": (132, 0, 0),
    "RANDR": (139, 88, 145),
    "XKEYBOARD": (135, 85, 137),
    "XINERAMA": (140, 0, 0),
    "Generic Event Extension": (128, 0, 0),
    "XInputExtension": (131, 66, 129),
    "DRI3": (151, 0, 0),
}

#: The ceiling BIG-REQUESTS.Enable answers with, in 4-byte units. 4194303 words
#: = 16777212 bytes, measured on both servers [recon/wire.md 2].
BIG_REQUEST_WORDS = 4194303

#: The step between two connections' resource-id-bases: mask + 1
#: [recon/wire.md 1.3, three simultaneous connections to the box's :355].
RID_STEP = 0x200000
RID_MASK = 0x1FFFFF


class _FakeWire:
    """One connection as `FakeXServer._dispatch` sees it -- `recv` and
    `sendall` and nothing else -- with the hold queue in the middle."""

    def __init__(self, server, sock, index):
        self.server = server
        self.sock = sock
        self.index = index
        self.seq = 0
        self.bigreq = False
        self.held = []

    def recv(self, n):
        return self.sock.recv(n)

    def sendall(self, data):
        if self.seq in self.server._held:
            self.held.append((self.seq, bytes(data)))
            return
        self.sock.sendall(data)

    def release(self):
        pending, self.held = self.held, []
        for _seq, data in pending:
            self.sock.sendall(data)


class FakeUpstream(FakeXServer):
    """FakeXServer grown into something a proxy can sit in front of."""

    def __init__(self, sockdir, num=7, cookie=None, accept_empty=True,
                 extensions=None):
        self.extensions = dict(FAKE_EXTENSIONS if extensions is None else extensions)
        self.noops = 0                 # NoOperation, counted: the CONSUME substitute
        self.wires = []                # one _FakeWire per connection, in order
        self.generation = 0            # bumped by close_when_idle, like a restarted Xwayland
        self._held = set()
        self._last_request = time.monotonic()
        self._idle_seconds = None
        self._idle_thread = None
        super().__init__(sockdir, num=num, cookie=cookie,
                         accept_empty=accept_empty)

    # -- the levers -----------------------------------------------------------

    def hold_reply(self, seq):
        """Everything this server would send while answering request `seq` is
        queued instead, so a test can let a later reply overtake it."""
        self._held.add(seq)

    def release(self, seq=None):
        if seq is None:
            self._held.clear()
        else:
            self._held.discard(seq)
        for wire in list(self.wires):
            wire.release()

    def push_event(self, pkt, conn_index=0):
        """32 raw bytes onto one connection, ahead of nothing and behind
        nothing -- which is what an event is."""
        self.wires[conn_index].sock.sendall(pkt)

    def send_fds(self, payload, fds, conn_index=0):
        """A payload carrying SCM_RIGHTS. A plain `recv()` on the other end
        takes the payload and drops the descriptors with no error at all
        [recon/env.md 2.6]; this is what proves the proxy does not."""
        socket.send_fds(self.wires[conn_index].sock, [payload], list(fds))

    def big_property(self, name, nbytes, win=None, type_name="STRING", fmt=8):
        """A property big enough that its reply cannot be one packet's worth:
        xprop's own read of a 200 kB property is the measured case
        [recon/tools.md 6]."""
        win = self.ROOTS[0] if win is None else win
        self.set_prop(win, name, type_name, fmt, bytes(range(256)) * (nbytes // 256)
                      + bytes(nbytes % 256))
        return win

    def close_when_idle(self, seconds):
        """Xwayland's `-terminate` shape: the server drops every connection once
        nothing has asked it anything for `seconds`, and the next connection is
        a new generation with fresh resource-id-bases [recon/env.md 6]."""
        self._idle_seconds = seconds
        if self._idle_thread is None:
            self._idle_thread = threading.Thread(target=self._idle_watch, daemon=True)
            self._idle_thread.start()

    def _idle_watch(self):
        while not self._stopped:
            time.sleep(0.05)
            if self._idle_seconds is None or not self.wires:
                continue
            if time.monotonic() - self._last_request < self._idle_seconds:
                continue
            self.generation += 1
            for wire in list(self.wires):
                try:
                    wire.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.wires = []
            self._last_request = time.monotonic()

    # -- the wire -------------------------------------------------------------

    def _setup_reply_for(self, index):
        """The same reply FakeXServer builds, with this connection's own
        resource-id-base: the server hands each connection its own, stepping by
        mask + 1, which is why the proxy opens one upstream connection per
        client and forwards the setup verbatim [recon/wire.md 1.3]."""
        vendor = b"FAKE"
        screens = b""
        for root in self.ROOTS:
            depth = struct.pack("<BxH4x", 24, 1) + b"\0" * 24
            screens += struct.pack("<5I6HI4B", root, 0, 0, 0, 0,
                                   1280, 720, 300, 200, 1, 1,
                                   0x21, 0, 0, 24, 1) + depth
        base = RID_STEP * (1 + index)
        extra = struct.pack("<4IHH8B4x", 1, base, RID_MASK, 256,
                            len(vendor), 0xFFFF, len(self.ROOTS), 0,
                            0, 0, 32, 32, 8, 255)
        extra += _pad4(vendor) + screens
        return struct.pack("<BxHHH", 1, 11, 0, len(extra) // 4) + extra

    def _serve(self, sock):
        order, _maj, _min, nlen, dlen = struct.unpack(
            "<BxHHHHxx", _recvn(sock, 12))
        assert order == 0x6C
        name = _recvn(sock, len(_pad4(b"x" * nlen)))[:nlen] if nlen else b""
        data = _recvn(sock, len(_pad4(b"x" * dlen)))[:dlen] if dlen else b""
        self.setup_attempts.append((name, data))
        if not self._auth_ok(name, data):
            reason = b"Authentication rejected by fake"
            sock.sendall(struct.pack("<BBHHH", 0, len(reason), 11, 0,
                                     len(_pad4(reason)) // 4)
                         + _pad4(reason))
            return
        wire = _FakeWire(self, sock, len(self.wires))
        self.wires.append(wire)
        sock.sendall(self._setup_reply_for(wire.index))
        while True:
            opcode, dbyte, rlen = struct.unpack("<BBH", _recvn(sock, 4))
            if rlen == 0:
                # BIG-REQUESTS: the true length is the next u32, in 4-byte units,
                # counting the 8-byte header. A zero from a connection that never
                # enabled it is the client's error and never reaches a real
                # server's dispatch [recon/wire.md 2].
                assert wire.bigreq, "zero length without BIG-REQUESTS"
                (rlen,) = struct.unpack("<I", _recvn(sock, 4))
                payload = _recvn(sock, (rlen - 2) * 4)
            else:
                payload = _recvn(sock, (rlen - 1) * 4)
            wire.seq = (wire.seq + 1) & 0xFFFF
            self._last_request = time.monotonic()
            self._dispatch(wire, opcode, dbyte, payload, wire.seq)

    def _dispatch(self, conn, opcode, dbyte, payload, seq):
        if opcode == 98:        # QueryExtension
            (n,) = struct.unpack_from("<H", payload, 0)
            name = payload[4:4 + n].decode("latin-1")
            self.log.append(("QueryExtension", name))
            major, first_event, first_error = self.extensions.get(name, (0, 0, 0))
            conn.sendall(struct.pack("<BBHIBBBB20x", 1, 0, seq, 0,
                                     1 if major else 0, major,
                                     first_event, first_error))
            return
        if opcode == 127:       # NoOperation, the CONSUME substitute
            self.noops += 1
            self.log.append(("NoOperation",))
            return
        big = self.extensions.get("BIG-REQUESTS", (0, 0, 0))[0]
        if big and opcode == big and dbyte == 0:
            conn.bigreq = True
            self.log.append(("BigReqEnable",))
            conn.sendall(struct.pack("<BxHII20x", 1, seq, 0, BIG_REQUEST_WORDS))
            return
        super()._dispatch(conn, opcode, dbyte, payload, seq)


class ProxyRig:
    """An `xw11.server.Server` on a thread, in front of a `FakeUpstream`, in a
    socket directory of its own.

    The directory is `x11_mini._SOCK_DIR`, so the proxy's own sockets and the
    client that dials them move together -- and because the abstract name is
    `"\\0" + <that dir>/XN`, two rigs never collide even though abstract names
    are not scoped by the filesystem [recon/env.md 4].
    """

    def __init__(self, num=20, upstream_num=7, cookie=None, idle=0.0,
                 check=0.05, extensions=None, server_kwargs=None):
        from wdotool import x11_mini
        from xw11 import display as display_mod
        from xw11 import server as server_mod
        self.dir = tempfile.mkdtemp(prefix="xw11rig-")
        os.chmod(self.dir, 0o700)
        self._old_sock_dir = x11_mini._SOCK_DIR
        self._old_lock_dir = display_mod._LOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        display_mod._LOCK_DIR = self.dir
        self._x11_mini = x11_mini
        self._display_mod = display_mod
        self.upstream = FakeUpstream(self.dir, num=upstream_num, cookie=cookie,
                                     extensions=extensions)
        self.upstream_name = ":%d" % upstream_num
        self.display = display_mod.allocate(":%d" % num)
        self.name = self.display.name
        self.log = open(os.devnull, "w")
        self.server = server_mod.Server(
            self.display, self.upstream_name, log=self.log,
            idle=idle, check=check, watch_wayland=False,
            **(server_kwargs or {}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._conns = []

    def conn(self):
        """An `x11_mini.X11Conn` to the proxy -- the wire client this project
        already trusts, pointed at the thing under test."""
        c = self._x11_mini.X11Conn(self.name)
        self._conns.append(c)
        return c

    def raw(self, timeout=5.0):
        """A bare socket to the proxy with the setup done, for the tests that
        need to pipeline bytes by hand. Returns (sock, setup_body)."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect("\0" + self.display.fs_path)
        s.sendall(struct.pack("<BxHHHHxx", 0x6C, 11, 0, 0, 0))
        head = _recvn(s, 8)
        (extra,) = struct.unpack_from("<H", head, 6)
        body = _recvn(s, extra * 4)
        self._conns.append(s)
        return s, body

    def stop(self):
        for c in self._conns:
            try:
                c.close()
            except (OSError, AttributeError):
                pass
        self._conns = []
        self.server.stop("the rig said so")
        self.thread.join(timeout=5)
        self.upstream.stop()
        self.log.close()
        self._x11_mini._SOCK_DIR = self._old_sock_dir
        self._display_mod._LOCK_DIR = self._old_lock_dir
        shutil.rmtree(self.dir, ignore_errors=True)
