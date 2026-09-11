"""Pure-stdlib /dev/uinput virtual input devices (legacy uinput_user_dev setup).

Env overrides for testing:
  WDOTOOL_UINPUT_PATH  device node to open (default /dev/uinput)
  WDOTOOL_FAKE_UINPUT=1  skip ioctls so a regular file can stand in for uinput
"""

import errno
import fcntl
import os
import struct

# evdev event types (input-event-codes.h). keys_cmds decodes a recorded stream with the same numbers and
# keystate derives an ioctl from EV_KEY, so they import them from here rather than write 0x01 a third time.
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03
EV_MSC = 0x04
SYN_REPORT = 0

REL_X = 0x00
REL_Y = 0x01
REL_HWHEEL = 0x06
REL_WHEEL = 0x08
ABS_X = 0x00
ABS_Y = 0x01

BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
BTN_SIDE = 0x113
BTN_EXTRA = 0x114
BTN_FORWARD = 0x115
BTN_BACK = 0x116
BTN_TASK = 0x117

ABS_RANGE = 32767  # QEMU usb-tablet axis maximum

# ioctls (x86-64/aarch64: _IOC dir<<30 | size<<16 | 'U'<<8 | nr)
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
# UI_GET_SYSNAME(len) = _IOR('U', 44, len): the "eventN" the kernel gave our device. Linux 4.4+; older kernels
# answer EINVAL and the caller falls back to matching on the device name.
_UI_SYSNAME_LEN = 32
UI_GET_SYSNAME = 0x8000552C | (_UI_SYSNAME_LEN << 16)

BUS_USB = 0x03

_EVENT_FMT = "llHHi"  # struct input_event: timeval + type,code,value (24B on LP64)
_USER_DEV_FMT = "80sHHHHI64i64i64i64i"  # struct uinput_user_dev (1116 bytes)


def dev_path() -> str:
    return os.environ.get("WDOTOOL_UINPUT_PATH", "/dev/uinput")


def access_ok() -> bool:
    """Can this process still open the uinput node?

    The udev rule grants access through a logind ACL on the *active* seat session, and logind takes it away
    again at a VT switch or a fast user switch. The ACL is only consulted at open(), so a daemon that opened the
    node while its user was at the seat keeps its devices -- and would keep injecting into whoever owns the seat
    now. Re-checking before each injection gives the grant back its meaning. Root (which needs no grant) and the
    fake-device test mode always pass; anything but a permission error is somebody else's problem and is left to
    the real open().
    """
    if os.geteuid() == 0 or os.environ.get("WDOTOOL_FAKE_UINPUT") == "1":
        return True
    try:
        fd = os.open(dev_path(), os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        return e.errno not in (errno.EACCES, errno.EPERM)
    os.close(fd)
    return True


class UinputDevice:
    """One virtual evdev device created through /dev/uinput."""

    def __init__(self, name, keys=(), rels=(), abs_axes=(), vendor=0x0627, product=0x0001):
        path = dev_path()
        self.fake = os.environ.get("WDOTOOL_FAKE_UINPUT") == "1"
        self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        self._created = False
        try:
            if not self.fake:
                if keys:
                    fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
                    for k in keys:
                        fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)
                if rels:
                    fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                    for r in rels:
                        fcntl.ioctl(self.fd, UI_SET_RELBIT, r)
                if abs_axes:
                    fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
                    for a in abs_axes:
                        fcntl.ioctl(self.fd, UI_SET_ABSBIT, a)
            absmin = [0] * 64
            absmax = [0] * 64
            for a in abs_axes:
                absmax[a] = ABS_RANGE
            os.write(
                self.fd,
                struct.pack(
                    _USER_DEV_FMT,
                    name.encode(),
                    BUS_USB, vendor, product, 1,  # input_id
                    0,  # ff_effects_max
                    *absmax, *absmin, *([0] * 64), *([0] * 64),
                ),
            )
            if not self.fake:
                fcntl.ioctl(self.fd, UI_DEV_CREATE)
            self._created = True
        except BaseException:
            os.close(self.fd)
            raise

    def emit(self, etype: int, code: int, value: int):
        os.write(self.fd, struct.pack(_EVENT_FMT, 0, 0, etype, code, value))

    def syn(self):
        self.emit(EV_SYN, SYN_REPORT, 0)

    def key(self, code: int, down: bool):
        self.emit(EV_KEY, code, 1 if down else 0)
        self.syn()

    def sysname(self) -> str:
        """Our device's input node name ("event12"), or "" when the kernel
        will not say. Used to keep the key-state reader off our own device."""
        if self.fake or self.fd < 0:
            return ""
        buf = bytearray(_UI_SYSNAME_LEN)
        try:
            fcntl.ioctl(self.fd, UI_GET_SYSNAME, buf)
        except OSError:
            return ""
        return buf.split(b"\0")[0].decode("utf-8", "replace")

    def close(self):
        if self.fd < 0:
            return
        try:
            if self._created and not self.fake:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = -1


def keyboard() -> UinputDevice:
    # 1..255: keymap accepts numeric evdev codes up to 255 (X keycodes 9..263);
    # every accepted code must be registered or the kernel drops it silently.
    # 1..255 is every code an X keymap maps, and the kernel drops an event on a code that
    # was not registered, silently. The uploaded keymap (wdotool/us_keymap.py, what vkbd.py
    # hands the compositor) binds a few characters above that block -- the euro on evdev
    # 435 -- and the character table answers those codes on BOTH sinks, so the kernel
    # device registers them too (measured by the euro batch, 2026-09-11: without this a
    # `wdotool type €` through /dev/uinput on a plain us session typed nothing).
    from wdotool import keymap
    extra = sorted({code for code, _shifted in keymap.UPLOADED_EXTRA_KEYS.values()} - set(range(1, 256)))
    return UinputDevice("wdotool virtual keyboard", keys=tuple(range(1, 256)) + tuple(extra))


def rel_mouse() -> UinputDevice:
    return UinputDevice(
        "wdotool virtual mouse",
        keys=(BTN_LEFT, BTN_MIDDLE, BTN_RIGHT, BTN_SIDE, BTN_EXTRA,
              BTN_FORWARD, BTN_BACK, BTN_TASK),
        rels=(REL_X, REL_Y, REL_WHEEL, REL_HWHEEL),
        product=0x0002,
    )


def abs_pointer() -> UinputDevice:
    """Absolute pointer shaped like a QEMU usb-tablet (every compositor maps
    its 0..32767 axes across the whole output layout)."""
    return UinputDevice(
        "wdotool virtual tablet",
        keys=(BTN_LEFT, BTN_MIDDLE, BTN_RIGHT),
        rels=(REL_WHEEL,),
        abs_axes=(ABS_X, ABS_Y),
        product=0x0003,
    )
