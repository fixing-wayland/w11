"""What the real keyboards are holding down, read from evdev (EVIOCGKEY).

Why this exists: `--clearmodifiers` has to say something useful when the modifier it was asked to clear is held
on a keyboard that is not ours. It cannot clear that modifier -- the kernel drops an `EV_KEY` release for a code
the emitting device does not hold, so a key-up sent from our uinput keyboard for someone else's key produces no
event at all -- and it must not press it back either, because that press would be ours and the user's release
would never clear it (see wdotool/daemon.py, "modifiers around an injection"). What is left is to tell the user,
and for that the kernel is the only source: `EVIOCGKEY` on an `/dev/input/event*` node returns that device's
current key bitmap, with no event stream to read and no device to grab.

Nothing here decides what to inject. Reading is a diagnostic only.

**Access.** Reading an event node needs read permission on it, and on a normal desktop the seat user does not
have it: measured on Ubuntu 24.04 (GNOME 46) and 26.04 (GNOME 50, KDE, sway), `/dev/input/event*` is
`crw-rw---- root:input` with *no* ACL -- logind's `uaccess` tag reaches `/dev/uinput` (which is how this repo's
udev rule lets the seat user inject) but not the keyboards. So this reads only as root, `held()` answers None --
"unknown" -- everywhere else, and `--clearmodifiers` behaves identically either way: the diagnostic is all that
is lost. This repo deliberately ships no udev rule granting read on `event*`: that would hand every process of
the seat user a system-wide keylogger.

**Never our own device.** A wdotool virtual keyboard is excluded by its `/dev/input/eventN` path
(`UI_GET_SYSNAME` on the uinput fd) and, belt and braces, by its device name: the modifiers we injected
ourselves are not the user holding a key.

Nothing here writes, and every device is opened read-only, non-blocking, and closed again inside the call -- no
fd is held between samples.
"""

import fcntl
import os

from hacks.input.uinput import EV_KEY

INPUT_DIR = "/dev/input"

KEY_MAX = 0x2FF
_KEY_BYTES = (KEY_MAX + 8) // 8   # 96: the bitmap ioctls' buffer size
_NAME_BYTES = 256

# Our own virtual devices all start with this (uinput.py device names).
OWN_NAME_PREFIX = "wdotool "


def _ior(letter: str, nr: int, size: int) -> int:
    """_IOR(letter, nr, size): dir(2=read)<<30 | size<<16 | letter<<8 | nr."""
    return 0x80000000 | (size << 16) | (ord(letter) << 8) | nr


EVIOCGNAME = _ior("E", 0x06, _NAME_BYTES)          # device name
EVIOCGKEY = _ior("E", 0x18, _KEY_BYTES)            # current key state
EVIOCGBIT_KEY = _ior("E", 0x20 + EV_KEY, _KEY_BYTES)  # EV_KEY capabilities


def bit(bits, code: int) -> bool:
    """Is `code` set in a kernel bitmap (little-endian, byte per 8 codes)?"""
    idx = code >> 3
    return idx < len(bits) and bool(bits[idx] & (1 << (code & 7)))


class Evdev:
    """The five syscalls this module needs, in one object so a test can swap the whole evdev layer for a fake
    (there is no evdev to speak of in a container, and a real one would answer with the *runner's* keyboard)."""

    def __init__(self, input_dir: str = INPUT_DIR):
        self.input_dir = input_dir

    def paths(self) -> "list[str]":
        try:
            names = os.listdir(self.input_dir)
        except OSError:
            return []
        return [os.path.join(self.input_dir, n)
                for n in sorted(names) if n.startswith("event")]

    def open(self, path: str) -> int:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)

    def close(self, fd: int):
        os.close(fd)

    def name(self, fd: int) -> str:
        buf = bytearray(_NAME_BYTES)
        fcntl.ioctl(fd, EVIOCGNAME, buf)
        return buf.split(b"\0")[0].decode("utf-8", "replace")

    def key_caps(self, fd: int) -> bytes:
        """EV_KEY capability bitmap: which keys this device can report."""
        buf = bytearray(_KEY_BYTES)
        fcntl.ioctl(fd, EVIOCGBIT_KEY, buf)
        return bytes(buf)

    def key_state(self, fd: int) -> bytes:
        """Key-state bitmap: which of them are down right now."""
        buf = bytearray(_KEY_BYTES)
        fcntl.ioctl(fd, EVIOCGKEY, buf)
        return bytes(buf)


class Reader:
    """Reads key state from the real keyboards.

    `exclude_paths` are our own uinput device nodes; `exclude_names` is the fallback for a kernel too old for
    UI_GET_SYSNAME (and for a stale device left by a killed daemon, whose "held" modifier would be a ghost)."""

    def __init__(self, evdev: "Evdev | None" = None, exclude_paths=(),
                 exclude_names=(OWN_NAME_PREFIX,)):
        self.evdev = evdev if evdev is not None else Evdev()
        self.exclude_paths = set(exclude_paths)
        self.exclude_names = tuple(exclude_names)

    def _ours(self, path: str, name: str) -> bool:
        return (path in self.exclude_paths
                or any(name.startswith(p) for p in self.exclude_names if p))

    def held(self, codes) -> "set[int] | None":
        """The keys from `codes` currently held on any *readable* keyboard that is not ours -- the union,
        because a modifier held on either of two keyboards is held.

        None means "could not be read at all" (no permission, no such device, no keyboard among them), which is
        not the same answer as the empty set: one is "nothing is held", the other "nothing is known"."""
        codes = list(codes)
        if not codes:
            return set()
        out = None
        for path in self.evdev.paths():
            if path in self.exclude_paths:
                continue
            try:
                fd = self.evdev.open(path)
            except OSError:
                continue          # not readable by this uid: not our keyboard
            try:
                try:
                    if self._ours(path, self.evdev.name(fd)):
                        continue
                    caps = self.evdev.key_caps(fd)
                    if not any(bit(caps, c) for c in codes):
                        continue  # not a keyboard, or not one with these keys
                    state = self.evdev.key_state(fd)
                except OSError:
                    continue      # device vanished / rejected the ioctl
            finally:
                try:
                    self.evdev.close(fd)
                except OSError:
                    pass
            if out is None:
                out = set()
            for c in codes:
                if bit(state, c):
                    out.add(c)
        return out
