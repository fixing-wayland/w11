#!/usr/bin/env python3
"""The proxy in front of a real Xwayland, under a real headless sway.

Stage 1's live claim is small and it is the one everything after it rests on:
an X client that works against Xwayland works against the proxy in front of it,
and says the same things about itself. recon/env.md 2.2 measured the baseline --
the pinned xdotool against an X client under sway already works, unmodified --
and the proxy must never regress it.

The shadow half of this file (native toplevels, `search --class foot`, the
client list) belongs to batch 3; what is here is the boot, the connection and
the bytes a client reads about the display itself.

Skips cleanly without sway, Xwayland or the X tools, the way the four other
live files do.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import HeadlessSway                                    # noqa: E402
from wdotool import x11_mini                                       # noqa: E402
from xw11 import display as display_mod                            # noqa: E402
from xw11 import server as server_mod                              # noqa: E402
from xw11 import wire                                              # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

HAVE_SWAY = bool(shutil.which("sway") and shutil.which("Xwayland"))
HAVE_XDPYINFO = bool(shutil.which("xdpyinfo"))


@unittest.skipUnless(HAVE_SWAY, "needs sway and Xwayland")
class BootAndConnect(unittest.TestCase):
    """One sway, one Xwayland, one proxy, one client."""

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessSway("xw11-live-")
        try:
            cls.display = display_mod.allocate()
            cls.log = open(os.devnull, "w")
            cls.server = server_mod.Server(
                cls.display, cls.rig.display, log=cls.log, idle=0.0, check=0.05,
                watch_wayland=False)
            cls.thread = threading.Thread(target=cls.server.serve_forever,
                                          daemon=True)
            cls.thread.start()
            cls.upstream_num, _screen = x11_mini._parse_display(cls.rig.display)
        except BaseException:
            cls.rig.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.server.stop("the test is over")
        cls.thread.join(timeout=10)
        cls.log.close()
        cls.display.release()
        cls.rig.stop()

    def env(self, **kw):
        got = dict(self.rig.env, DISPLAY=self.display.name,
                   W11_PASSTHROUGH="never")
        got.update(kw)
        return got

    def run_tool(self, argv, **kw):
        return subprocess.run(argv, env=self.env(**kw), capture_output=True,
                              text=True, timeout=60)

    def test_the_rig_really_is_xwayland_and_not_the_proxy(self):
        """The premise: without this, every comparison below could be the proxy
        talking to itself."""
        self.assertTrue(self.rig.display.startswith(":"))
        self.assertNotEqual(self.rig.display, self.display.name)
        self.assertNotEqual(self.upstream_num, self.display.num)

    @unittest.skipUnless(HAVE_XDPYINFO, "needs xdpyinfo")
    def test_xdpyinfo_through_the_proxy_says_the_proxys_own_name(self):
        got = self.run_tool(["xdpyinfo"])
        self.assertEqual(got.returncode, 0, got.stderr)
        first = got.stdout.splitlines()[0]
        self.assertEqual(first, "name of display:    %s" % self.display.name)

    @unittest.skipUnless(HAVE_XDPYINFO, "needs xdpyinfo")
    def test_everything_but_the_display_name_is_what_xwayland_said(self):
        """The lines that describe the SERVER have to be identical: the setup
        reply is forwarded verbatim, so the vendor, the release, the screen and
        the extension list are Xwayland's own bytes."""
        direct = self.run_tool(["xdpyinfo"], DISPLAY=self.rig.display)
        through = self.run_tool(["xdpyinfo"])
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(through.returncode, 0, through.stderr)
        skip = ("name of display:", "default screen number", "focus:",
                "number of extensions")
        a = [ln for ln in direct.stdout.splitlines()
             if not ln.startswith(skip)]
        b = [ln for ln in through.stdout.splitlines()
             if not ln.startswith(skip)]
        self.assertEqual(a, b)

    @unittest.skipUnless(HAVE_XDPYINFO, "needs xdpyinfo")
    def test_dri3_is_the_one_extension_that_is_answered_differently(self):
        """And it is answered "absent", never hidden from the list: xdpyinfo
        reads `ListExtensions`, which passes untouched. What changes is what a
        client that asks `QueryExtension("DRI3")` is told."""
        direct = self.run_tool(["xdpyinfo"], DISPLAY=self.rig.display)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        conn = x11_mini.X11Conn(self.display.name)
        self.addCleanup(conn.close)
        name = b"DRI3"
        seq = conn._send(wire.OP_QUERY_EXTENSION, 0,
                         wire.pad4(len(name).to_bytes(2, "little") + b"\0\0" + name))
        pkt, _body = conn._wait_reply(seq)
        self.assertEqual(pkt[8], 0, "DRI3 came back present through the proxy")
        self.assertEqual(len(pkt), 32)

    def test_a_stdlib_client_reads_the_root_and_a_property(self):
        """The wire client this project already ships, pointed at the proxy: the
        setup, an InternAtom, a GetProperty and the reply framing, end to end."""
        conn = x11_mini.X11Conn(self.display.name)
        self.addCleanup(conn.close)
        self.assertNotEqual(conn.root(), 0)
        direct = x11_mini.X11Conn(self.rig.display)
        self.addCleanup(direct.close)
        self.assertEqual(conn.root(), direct.root(),
                         "the proxy handed out a root that is not Xwayland's")

    def test_two_clients_get_two_resource_id_bases(self):
        """One upstream connection per client, so each is handed its own base
        [recon/wire.md 1.3]."""
        first = x11_mini.X11Conn(self.display.name)
        self.addCleanup(first.close)
        second = x11_mini.X11Conn(self.display.name)
        self.addCleanup(second.close)
        self.assertNotEqual(first._rid_base, second._rid_base)
        self.assertEqual(first._rid_mask, second._rid_mask)
        self.assertEqual(abs(second._rid_base - first._rid_base),
                         first._rid_mask + 1)

    def test_the_proxy_answers_on_both_of_its_socket_names(self):
        import socket
        for target in ("\0" + self.display.fs_path, self.display.fs_path):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(target)
            s.close()

    def test_the_client_leaving_does_not_end_the_proxy(self):
        conn = x11_mini.X11Conn(self.display.name)
        conn.close()
        time.sleep(0.3)
        self.assertTrue(self.thread.is_alive())
        again = x11_mini.X11Conn(self.display.name)
        self.addCleanup(again.close)
        self.assertTrue(again.root())


if __name__ == "__main__":
    unittest.main()
