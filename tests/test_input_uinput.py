"""Byte-layout tests for wdotool.uinput against known-good x86-64 sizes,
using WDOTOOL_FAKE_UINPUT to write into a regular file."""

import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hacks.input import uinput

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"


class FakeUinputMixin:
    def make_device(self, factory):
        fd, path = tempfile.mkstemp(prefix="wdotool-uinput-")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        os.environ["WDOTOOL_UINPUT_PATH"] = path
        os.environ["WDOTOOL_FAKE_UINPUT"] = "1"
        self.addCleanup(os.environ.pop, "WDOTOOL_UINPUT_PATH", None)
        self.addCleanup(os.environ.pop, "WDOTOOL_FAKE_UINPUT", None)
        dev = factory()
        self.addCleanup(dev.close)
        return dev, path


class TestStructLayout(unittest.TestCase):
    def test_input_event_is_24_bytes(self):
        self.assertEqual(struct.calcsize(uinput._EVENT_FMT), 24)

    def test_uinput_user_dev_is_1116_bytes(self):
        self.assertEqual(struct.calcsize(uinput._USER_DEV_FMT), 1116)


class TestDeviceSetup(FakeUinputMixin, unittest.TestCase):
    def test_keyboard_user_dev_header(self):
        dev, path = self.make_device(uinput.keyboard)
        data = open(path, "rb").read()
        self.assertEqual(len(data), 1116)
        name = data[:80].rstrip(b"\0").decode()
        self.assertEqual(name, "wdotool virtual keyboard")
        bustype, vendor, product, version = struct.unpack_from("HHHH", data, 80)
        self.assertEqual(bustype, 0x03)  # BUS_USB
        self.assertEqual(vendor, 0x0627)
        (ff,) = struct.unpack_from("I", data, 88)
        self.assertEqual(ff, 0)
        absmax = struct.unpack_from("64i", data, 92)
        self.assertEqual(set(absmax), {0})  # keyboard advertises no abs axes

    def test_tablet_abs_ranges(self):
        dev, path = self.make_device(uinput.abs_pointer)
        data = open(path, "rb").read()
        absmax = struct.unpack_from("64i", data, 92)
        absmin = struct.unpack_from("64i", data, 92 + 256)
        self.assertEqual(absmax[uinput.ABS_X], 32767)
        self.assertEqual(absmax[uinput.ABS_Y], 32767)
        self.assertEqual(absmax[2], 0)
        self.assertEqual(set(absmin), {0})

    def test_key_event_bytes(self):
        dev, path = self.make_device(uinput.keyboard)
        dev.key(30, True)   # KEY_A down + SYN
        dev.key(30, False)  # KEY_A up + SYN
        data = open(path, "rb").read()[1116:]
        self.assertEqual(len(data), 4 * 24)
        events = [struct.unpack_from(uinput._EVENT_FMT, data, n * 24) for n in range(4)]
        # (tv_sec, tv_usec, type, code, value)
        self.assertEqual(events[0][2:], (uinput.EV_KEY, 30, 1))
        self.assertEqual(events[1][2:], (uinput.EV_SYN, uinput.SYN_REPORT, 0))
        self.assertEqual(events[2][2:], (uinput.EV_KEY, 30, 0))
        self.assertEqual(events[3][2:], (uinput.EV_SYN, uinput.SYN_REPORT, 0))

    def test_rel_and_abs_events(self):
        dev, path = self.make_device(uinput.rel_mouse)
        dev.emit(uinput.EV_REL, uinput.REL_X, -7)
        dev.emit(uinput.EV_REL, uinput.REL_Y, 3)
        dev.syn()
        data = open(path, "rb").read()[1116:]
        events = [struct.unpack_from(uinput._EVENT_FMT, data, n * 24) for n in range(3)]
        self.assertEqual(events[0][2:], (uinput.EV_REL, uinput.REL_X, -7))
        self.assertEqual(events[1][2:], (uinput.EV_REL, uinput.REL_Y, 3))
        self.assertEqual(events[2][2:], (uinput.EV_SYN, uinput.SYN_REPORT, 0))

    def test_button_constants(self):
        self.assertEqual(uinput.BTN_LEFT, 0x110)
        self.assertEqual(uinput.BTN_MIDDLE, 0x112)
        self.assertEqual(uinput.BTN_RIGHT, 0x111)
        self.assertEqual(uinput.BTN_SIDE, 0x113)
        self.assertEqual(uinput.BTN_EXTRA, 0x114)
        self.assertEqual(uinput.REL_WHEEL, 0x08)
        self.assertEqual(uinput.REL_HWHEEL, 0x06)

    def test_ioctl_constants(self):
        # _IOW('U', nr, int) and _IO('U', nr) on x86-64/aarch64
        self.assertEqual(uinput.UI_SET_EVBIT, 0x40045564)
        self.assertEqual(uinput.UI_SET_KEYBIT, 0x40045565)
        self.assertEqual(uinput.UI_SET_RELBIT, 0x40045566)
        self.assertEqual(uinput.UI_SET_ABSBIT, 0x40045567)
        self.assertEqual(uinput.UI_DEV_CREATE, 0x5501)
        self.assertEqual(uinput.UI_DEV_DESTROY, 0x5502)


if __name__ == "__main__":
    unittest.main()
