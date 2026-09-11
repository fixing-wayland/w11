#!/usr/bin/env python3
"""The proxy in front of a real Xwayland, under a real headless sway.

Stage 1's live claim is small and it is the one everything after it rests on:
an X client that works against Xwayland works against the proxy in front of it,
and says the same things about itself. recon/env.md 2.2 measured the baseline --
the pinned xdotool against an X client under sway already works, unmodified --
and the proxy must never regress it.

Two halves. `BootAndConnect` is the boot, the connection and the bytes a client
reads about the DISPLAY, with the proxy on a thread of this process. Everything
after it is the shadow side -- a native `foot` toplevel that the unmodified X
tools have to see -- and runs the proxy as a SUBPROCESS, because the shadow
registry is a Wayland client of the rig's sway and needs the rig's
`XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY` and `SWAYSOCK`.

Skips cleanly without sway, Xwayland or the X tools, the way the four other
live files do.
"""

import json
import os
import shutil
import struct
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
        """One upstream connection per client, so each is handed a range of its
        own [recon/wire.md 1.3]. That is the claim the proxy needs, and it is
        the only one this can make.

        It used to assert the difference was EXACTLY `mask + 1`, and that went
        red about one run in four. Measured, 2026-09-10, scratchpad
        b3/flake3.py on this same rig: open three connections, close the middle
        one, open two more --

            open 0x600000 | open 0x400000 | open 0x800000
            close the middle one
            open 0x400000 | open 0xa00000        -> a gap of 3 * (mask + 1)

        -- the server hands a FREED range out again, and out of order. Every
        test above this one opens and closes connections, so whether the pool
        has a hole in it when this one runs is a matter of when the last
        `close()` landed. recon/wire.md 1.3's "stepping by mask + 1" was
        measured on three SIMULTANEOUS connections, which is a different
        question, and recon/env.md 2.5 already recorded the reuse (0x600000
        then 0x400000 on two consecutive connections).

        So: the two bases differ, they carry the same mask, each is aligned to
        `mask + 1`, and the ranges do not overlap. A proxy that shared one
        upstream connection between two clients would hand them the SAME base
        and fail the first line.
        """
        first = x11_mini.X11Conn(self.display.name)
        self.addCleanup(first.close)
        second = x11_mini.X11Conn(self.display.name)
        self.addCleanup(second.close)
        self.assertNotEqual(first._rid_base, second._rid_base)
        self.assertEqual(first._rid_mask, second._rid_mask)
        step = first._rid_mask + 1
        for base in (first._rid_base, second._rid_base):
            self.assertEqual(base % step, 0, "0x%x is not aligned to a range" % base)
        self.assertEqual(abs(second._rid_base - first._rid_base) % step, 0)
        self.assertFalse(first._rid_base <= second._rid_base
                         <= first._rid_base + first._rid_mask,
                         "the two clients were handed overlapping id ranges")

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


# -- the shadow half: a native toplevel that the X tools can see ---------------
#
# Batch 1's class above proves the proxy changes nothing about the X server it
# is in front of. What follows is the point of the whole thing: with one `foot`
# (native Wayland) and one `xterm` (Xwayland) side by side under the same sway,
# the unmodified X tools have to see BOTH, and the xterm has to look exactly as
# it looked without the proxy. recon/env.md 2.7 is the before: with a foot
# focused and no X client, `xdotool search --class foot` prints nothing and
# exits 1, `getactivewindow` exits 1, `wmctrl -l` prints "Cannot get client list
# properties." and exits 1, and the root has no `_NET_CLIENT_LIST` at all.
#
# The proxy runs as a SUBPROCESS here rather than on a thread of this one, and
# that is not decoration: the shadow registry is a Wayland client of the rig's
# sway, so the process that runs it needs the rig's `XDG_RUNTIME_DIR`,
# `WAYLAND_DISPLAY` and `SWAYSOCK` -- which are not this process's, and must not
# become this process's. It is also the shape the wrappers will start in
# (design section 8.2).

HAVE_XTERM = bool(shutil.which("xterm"))
HAVE_FOOT = bool(shutil.which("foot"))
HAVE_XDOTOOL = bool(shutil.which("xdotool"))
HAVE_WMCTRL = bool(shutil.which("wmctrl"))
HAVE_XPROP = bool(shutil.which("xprop"))

XTERM_TITLE = "WXL-Xterm"
FOOT_TITLE = "WXL-Foot"
FOOT_APP_ID = "footw"

#: design section 4.4's set, which is what `xprop -id <shadow>` prints. The
#: five names an X twin has and a shadow does not (`WM_HINTS`,
#: `WM_NORMAL_HINTS`, `_NET_WM_ICON`, `_NET_WM_USER_TIME`,
#: `_NET_WM_ALLOWED_ACTIONS`) are rows in the docs with their routes.
SHADOW_PROPERTIES = ("_NET_WM_NAME", "WM_NAME", "WM_CLASS", "_NET_WM_PID",
                     "WM_CLIENT_MACHINE", "_NET_WM_DESKTOP", "_NET_WM_STATE",
                     "_NET_WM_WINDOW_TYPE", "WM_STATE", "_NET_FRAME_EXTENTS",
                     "WM_PROTOCOLS")


def _normalize_serials(text: str) -> str:
    """Xlib's default error handler prints two serial numbers and the proxy
    consumes none [recon/wire.md 3.2a] -- but a direct run and a proxied run of
    the same command are two different connections to two different servers, and
    a connection's own request count is not a promise. The same normaliser
    `tests/test_wxprop_live.py` uses."""
    import re

    return re.sub(r"(Serial number of failed request:|Current serial number"
                  r" in output stream:)\s+\d+", r"\1 N", text)


def _walk(node):
    yield node
    for kid in list(node.get("nodes") or []) + list(node.get("floating_nodes") or []):
        yield from _walk(kid)


@unittest.skipUnless(HAVE_SWAY, "needs sway and Xwayland")
@unittest.skipUnless(HAVE_FOOT, "needs foot (the native toplevel)")
class ProxyLive(unittest.TestCase):
    """One sway, one proxy subprocess, one foot; an xterm where the subclass
    asks for one."""

    prefix = "xw11-shadow-"
    extra_conf = ""
    proxy_env = {}
    want_xterm = True
    #: how many heads the rig comes up with. More than one needs the wlroots
    #: headless backend to make them (`WLR_HEADLESS_OUTPUTS`), which is what
    #: `HeadlessSway(outputs=)` sets; batch 7's RandR rows are the only ones
    #: that ask for two.
    outputs = 1
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessSway(cls.prefix, extra_conf=cls.extra_conf,
                               outputs=cls.outputs)
        try:
            if cls.want_xterm:
                if not HAVE_XTERM:
                    raise unittest.SkipTest("needs xterm (the X twin)")
                cls.swaymsg("exec xterm -T %s -e sh -c 'sleep 600'" % XTERM_TITLE)
                if not cls.wait(lambda: cls.node(name=XTERM_TITLE) is not None):
                    raise unittest.SkipTest("xterm never appeared (XWayland?)")
            cls.swaymsg("exec foot --app-id %s --title %s sh -c 'sleep 600'"
                        % (FOOT_APP_ID, FOOT_TITLE))
            if not cls.wait(lambda: cls.node(app_id=FOOT_APP_ID) is not None):
                raise unittest.SkipTest("foot window never appeared")
            cls.start_proxy()
        except BaseException:
            cls.rig.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.stop_proxy()
        cls.rig.stop()

    # -- the proxy ------------------------------------------------------------

    @classmethod
    def start_proxy(cls):
        cls.log_path = os.path.join(cls.rig.rtdir, "xw11.log")
        env = dict(cls.rig.env, XW11_LOG=cls.log_path, W11_PASSTHROUGH="never",
                   LC_ALL="C")
        env.update(cls.proxy_env)
        env.pop("XW11_DISPLAY", None)
        cls.proxy = subprocess.Popen(
            [sys.executable, "-m", "xw11", "--foreground",
             "--upstream", cls.rig.display],
            env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cls.proxy_display = cls._wait_proxy_display()
        if cls.proxy_display is None:
            cls.stop_proxy()
            raise unittest.SkipTest("the proxy never announced a display")

    @classmethod
    def _wait_proxy_display(cls, timeout=30.0):
        """The display file the proxy writes after it binds (design section
        2.1). Waiting on the FILE and not on a sleep is what keeps this from
        being a race on a slow box."""
        path = os.path.join(cls.rig.rtdir, "xw11", "display")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls.proxy.poll() is not None:
                return None
            try:
                with open(path, encoding="utf-8") as f:
                    got = f.read().split()
            except OSError:
                got = []
            if got and got[0].startswith(":"):
                return got[0]
            time.sleep(0.1)
        return None

    @classmethod
    def stop_proxy(cls):
        proc = getattr(cls, "proxy", None)
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:       # pragma: no cover - a wedged loop
            proc.kill()
            proc.wait(timeout=5)

    @classmethod
    def proxy_log(cls):
        try:
            with open(cls.log_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    @classmethod
    def rid_base(cls):
        """The `resource-id-base` the proxy's own connection was handed, off
        its own log line -- the one number a shadow id is built from
        (design section 4.3)."""
        import re

        got = re.search(r"ids 0x([0-9a-f]+)\|n", cls.proxy_log())
        return int(got.group(1), 16) if got else None

    # -- the compositor -------------------------------------------------------

    @classmethod
    def swaymsg(cls, cmd, kind=None):
        argv = ["swaymsg", "-s", cls.rig.sock]
        if kind:
            argv += ["-t", kind]
        argv.append(cmd)
        got = subprocess.run(argv, env=cls.rig.env, capture_output=True,
                             text=True, timeout=20)
        return got

    @classmethod
    def tree(cls):
        return json.loads(subprocess.run(
            ["swaymsg", "-s", cls.rig.sock, "-t", "get_tree"], env=cls.rig.env,
            capture_output=True, text=True, timeout=20).stdout)

    @classmethod
    def node(cls, **match):
        for n in _walk(cls.tree()):
            if not n.get("pid"):
                continue
            if all(n.get(k) == v for k, v in match.items()):
                return n
        return None

    @classmethod
    def workspace_of(cls, **match):
        """The name of the workspace a matching node sits under, walked out of
        `get_tree` -- which is where sway keeps the scratchpad too: a
        scratchpad window is a node of the workspace named `__i3_scratch`, so
        "minimized" and "on workspace 2" are the same question asked once."""
        for ws in _walk(cls.tree()):
            if ws.get("type") != "workspace":
                continue
            for kid in _walk(ws):
                if kid.get("pid") and all(kid.get(k) == v
                                          for k, v in match.items()):
                    return ws.get("name")
        return None

    @classmethod
    def wait(cls, ready, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready():
                return True
            time.sleep(0.2)
        return False

    # -- the tools ------------------------------------------------------------

    def env(self, through=True, **kw):
        got = dict(self.rig.env, W11_PASSTHROUGH="never", LC_ALL="C")
        if through:
            got["DISPLAY"] = self.proxy_display
        got.update(kw)
        return got

    def tool(self, argv, through=True, timeout=60, **kw):
        return subprocess.run(argv, env=self.env(through, **kw),
                              capture_output=True, text=True, timeout=timeout)

    def shadow_id(self):
        """The foot's shadow, through the tool that has to find it."""
        got = self.tool(["xdotool", "search", "--class", FOOT_APP_ID])
        self.assertEqual(got.returncode, 0, got.stderr)
        ids = [int(x) for x in got.stdout.split()]
        self.assertEqual(len(ids), 1, "search printed %r" % got.stdout)
        return ids[0]


@unittest.skipUnless(HAVE_XDOTOOL and HAVE_XPROP, "needs xdotool and xprop")
class XTwinUntouched(ProxyLive):
    """recon/env.md 2.2's baseline must not regress: against an X client the
    unmodified tools already work on sway, and every byte they print about the
    xterm through the proxy is the byte they print without one."""

    prefix = "xw11-twin-"

    def setUp(self):
        node = self.node(name=XTERM_TITLE)
        self.assertIsNotNone(node, "the xterm left the tree")
        self.xterm = str(node["window"])

    def both(self, argv):
        return self.tool(argv, through=False), self.tool(argv, through=True)

    def assert_identical(self, argv):
        direct, through = self.both(argv)
        self.assertEqual(direct.returncode, through.returncode,
                         "%r: rc %d direct, %d through"
                         % (argv, direct.returncode, through.returncode))
        self.assertEqual(_normalize_serials(direct.stdout),
                         _normalize_serials(through.stdout), "%r stdout" % (argv,))
        self.assertEqual(_normalize_serials(direct.stderr),
                         _normalize_serials(through.stderr), "%r stderr" % (argv,))
        return direct

    def test_the_identity_reads_print_the_same_bytes(self):
        """recon/tools.md 11 rows 4, 5, 7: name, pid, class name.
        `getwindowclassname` is xdotool 4.x's and the distro 3.x on this box
        answers "Unknown command" -- which is itself a thing both runs have to
        say identically, so it is compared and then not required to succeed."""
        for argv in (["xdotool", "getwindowname", self.xterm],
                     ["xdotool", "getwindowpid", self.xterm]):
            got = self.assert_identical(argv)
            self.assertEqual(got.returncode, 0, "%r: %s" % (argv, got.stderr))
        got = self.assert_identical(["xdotool", "getwindowclassname", self.xterm])
        if got.returncode:
            self.assertIn("Unknown command", got.stderr)

    def test_the_geometry_read_prints_the_same_bytes(self):
        """Row 6, and the one that pipelines `GetWindowAttributes` with
        `GetGeometry` and then sends `QueryTree` and `TranslateCoordinates`
        [recon/tools.md 4.2] -- four of the seven rows this batch turned on,
        every one of them PASSing because the window is real."""
        self.assert_identical(["xdotool", "getwindowgeometry", self.xterm])

    def test_xprop_id_on_the_x_twin_prints_the_same_bytes(self):
        """Row 49. `ListProperties` on a real window PASSes, so the xterm's own
        thirteen properties come back in the server's own order."""
        got = self.assert_identical(["xprop", "-id", self.xterm])
        self.assertIn("WM_NORMAL_HINTS", got.stdout)

    def test_search_by_class_and_by_name_find_the_same_window(self):
        """Rows 1 and 2. `search` walks `QueryTree`, which IS edited on the
        root -- and the answer for a pattern only the xterm matches is
        unchanged, because the shadow is appended and not substituted."""
        self.assert_identical(["xdotool", "search", "--class", "xterm"])
        self.assert_identical(["xdotool", "search", "--name", XTERM_TITLE])

    def test_the_writes_on_the_x_twin_print_the_same_bytes(self):
        """The write half of the same baseline: every one of these names a REAL
        X id, so the row is PASS and the bytes are Xwayland's own
        [recon/tools.md 11 rows 9-12, 39, 40]. They are run twice -- once
        direct, once through -- which is also the only way to compare them."""
        for argv in (["xdotool", "windowfocus", self.xterm],
                     ["xdotool", "windowraise", self.xterm],
                     ["xdotool", "windowmove", self.xterm, "100", "100"],
                     ["xdotool", "windowsize", self.xterm, "400", "300"],
                     ["wmctrl", "-i", "-r", self.xterm, "-e", "0,10,10,300,200"],
                     ["wmctrl", "-i", "-r", self.xterm, "-b",
                      "add,maximized_vert"],
                     ["wmctrl", "-i", "-r", self.xterm, "-b",
                      "remove,maximized_vert"]):
            if argv[0] == "wmctrl" and not HAVE_WMCTRL:
                continue
            self.assert_identical(argv)

    def test_windowminimize_on_the_x_twin_stays_on_the_xwms_plane(self):
        """`WM_CHANGE_STATE` is ICCCM's, and no `_NET_SUPPORTED` list names it
        [recon/env.md 2.1 lists wlroots' nineteen] -- so the dual-plane rule
        cannot ask that list about it and the message goes to the xwm, which
        reads it on its own path (`xwm_handle_wm_change_state` -> sway's
        `handle_request_minimize`).

        The bytes are equal either way (a `ClientMessage` is answered with
        nothing), so this asserts the PLANE and not only the wire: sway's
        `backend.minimize` is `move scratchpad` (backend_sway.py:373), and a
        proxy that routed this would move an X window somebody else manages to
        the scratchpad where a direct run leaves it exactly where it is."""
        before = self.workspace_of(name=XTERM_TITLE)
        self.assertIsNotNone(before, "the xterm is on no workspace at all")
        self.assert_identical(["xdotool", "windowminimize", self.xterm])
        self.assertFalse(self.wait(
            lambda: self.workspace_of(name=XTERM_TITLE) != before, timeout=3.0),
            "the xterm moved from %r to %r"
            % (before, self.workspace_of(name=XTERM_TITLE)))

    def test_a_property_written_on_the_x_twin_is_the_x_servers_own(self):
        """`xprop -set` on a real window PASSes and the value comes back out of
        Xwayland, not out of an overlay [recon/tools.md 6, rows 52 and 53].
        The proxy's overlay exists for windows the X server has never heard
        of."""
        self.tool(["xprop", "-id", self.xterm, "-f", "XW11_TWIN", "8s",
                   "-set", "XW11_TWIN", "hello"])
        self.addCleanup(self.tool,
                        ["xprop", "-id", self.xterm, "-remove", "XW11_TWIN"])
        direct = self.tool(["xprop", "-id", self.xterm, "XW11_TWIN"],
                           through=False)
        self.assertIn("hello", direct.stdout)
        self.assert_identical(["xprop", "-id", self.xterm, "XW11_TWIN"])

    def test_a_dead_window_still_draws_the_servers_own_error(self):
        """recon/tools.md 9: an id nobody knows PASSes and upstream answers
        `BadWindow` itself, so Xlib's default handler prints the same lines. The
        proxy inventing that error would be more code for the same bytes."""
        got = self.assert_identical(["xdotool", "getwindowname", "0x1234567"])
        self.assertEqual(got.returncode, 1)
        self.assertIn("BadWindow", got.stderr)


@unittest.skipUnless(HAVE_XDOTOOL and HAVE_WMCTRL and HAVE_XPROP,
                     "needs xdotool, wmctrl and xprop")
class NativeExists(ProxyLive):
    """The foot has no Xwayland window at all, and every one of these commands
    is one recon/env.md 2.7 measured failing."""

    prefix = "xw11-native-"

    def test_search_by_class_finds_the_foot_at_an_id_out_of_the_proxys_range(self):
        """The id is `rid_base | n` out of the range the SERVER allocated to the
        proxy's own connection [recon/wire.md 7.1], so it can never collide with
        a real Xwayland id -- which are `(client << 21) | serial`
        [recon/seams.md 3]."""
        shadow = self.shadow_id()
        base = self.rid_base()
        self.assertIsNotNone(base, "the proxy never logged its own base")
        self.assertEqual(shadow & ~0x1FFFFF, base)
        self.assertTrue(shadow & 0x1FFFFF, "the bare base is never handed out")

    def test_the_name_the_pid_and_the_geometry_are_the_compositors(self):
        shadow = self.shadow_id()
        got = self.tool(["xdotool", "getwindowname", str(shadow)])
        self.assertEqual((got.returncode, got.stdout.strip()), (0, FOOT_TITLE),
                         got.stderr)
        node = self.node(app_id=FOOT_APP_ID)
        got = self.tool(["xdotool", "getwindowpid", str(shadow)])
        self.assertEqual((got.returncode, got.stdout.strip()),
                         (0, str(node["pid"])), got.stderr)
        got = self.tool(["xdotool", "getwindowgeometry", str(shadow)])
        rect = node["rect"]
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("Geometry: %dx%d" % (rect["width"], rect["height"]),
                      got.stdout)
        self.assertIn("Position: %d,%d" % (rect["x"], rect["y"]), got.stdout)

    def test_wmctrl_lists_both_planes_with_the_foots_own_pid(self):
        """`wmctrl -lp` reads `_NET_CLIENT_LIST` and then six properties per
        window [recon/tools.md 5]; without the proxy it prints "Cannot get
        client list properties." and exits 1 [recon/env.md 2.7]."""
        got = self.tool(["wmctrl", "-lp"])
        self.assertEqual(got.returncode, 0, got.stderr)
        shadow = self.shadow_id()
        node = self.node(app_id=FOOT_APP_ID)
        rows = {int(line.split()[0], 16): line for line in got.stdout.splitlines()}
        self.assertIn(shadow, rows)
        self.assertIn(str(node["pid"]), rows[shadow].split())
        self.assertIn(FOOT_TITLE, rows[shadow])
        xterm = self.node(name=XTERM_TITLE)
        self.assertIn(xterm["window"], rows, "the X twin left the list")

    def test_xprop_on_the_shadow_prints_the_property_table(self):
        shadow = self.shadow_id()
        got = self.tool(["xprop", "-id", str(shadow)])
        self.assertEqual(got.returncode, 0, got.stderr)
        printed = [line.split("(")[0].split(" ")[0]
                   for line in got.stdout.splitlines() if line[:1].isalnum()
                   or line.startswith("_")]
        for name in SHADOW_PROPERTIES:
            self.assertIn(name, printed, got.stdout)
        self.assertIn('WM_CLASS(STRING) = "%s", "%s"' % (FOOT_APP_ID, FOOT_APP_ID),
                      got.stdout)

    def test_the_root_client_list_names_both_planes(self):
        """The property sway DELETES when no X client is mapped
        [recon/env.md 2.7]."""
        got = self.tool(["xprop", "-root", "_NET_CLIENT_LIST"])
        self.assertEqual(got.returncode, 0, got.stderr)
        printed = got.stdout.lower()
        self.assertIn("0x%x" % self.shadow_id(), printed)
        self.assertIn("0x%x" % self.node(name=XTERM_TITLE)["window"], printed)

    def test_getactivewindow_follows_the_compositors_focus(self):
        shadow = self.shadow_id()
        xterm = self.node(name=XTERM_TITLE)["window"]
        for cmd, want in (("[app_id=%s] focus" % FOOT_APP_ID, shadow),
                          ("[title=%s] focus" % XTERM_TITLE, xterm),
                          ("[app_id=%s] focus" % FOOT_APP_ID, shadow)):
            self.swaymsg(cmd)
            self.assertTrue(self.wait(
                lambda w=want: self.tool(["xdotool", "getactivewindow"]).stdout
                .strip() == str(w), timeout=5),
                "getactivewindow never followed %r" % cmd)

    def test_the_five_commands_that_exit_one_today_exit_zero(self):
        """recon/tools.md 4.10: sway publishes no `_NET_NUMBER_OF_DESKTOPS` and
        no `_NET_CURRENT_DESKTOP`, so all five print "Your windowmanager claims
        not to support ..." and exit 1. Synthesizing the pair AND naming them in
        `_NET_SUPPORTED` is what turns them green."""
        for argv in (["xdotool", "get_desktop"],
                     ["xdotool", "get_num_desktops"],
                     ["xdotool", "set_desktop", "0"],
                     ["wmctrl", "-d"]):
            got = self.tool(argv)
            self.assertEqual(got.returncode, 0, "%r: %s" % (argv, got.stderr))
            self.assertNotIn("claims not to support", got.stderr)

    def test_wmctrl_d_prints_one_row_per_workspace(self):
        """One row per workspace sway really has, named the way sway names it,
        and the two columns measured and pinned AS FOUND: the `VP:` and `WA:`
        columns are the clone's own rule (wwmctl/core.py:800, 828) and the
        proxy comes at them from the other side -- it publishes no
        `_NET_DESKTOP_VIEWPORT` and no `_NET_WORKAREA`, so real wmctrl prints
        `N/A` for both. Closing the `VP:` gap is one root property, which is a
        row in the docs and not a policy.

        The NAME column is not one of those gaps: `_NET_DESKTOP_NAMES` comes
        from `workspaces()` and `backend_sway.py:441` has one, so wmctrl prints
        sway's own workspace name here [M 2026-09-10, this rig]."""
        got = self.tool(["wmctrl", "-d"])
        self.assertEqual(got.returncode, 0, got.stderr)
        rows = got.stdout.splitlines()
        spaces = json.loads(self.swaymsg("", kind="get_workspaces").stdout)
        self.assertTrue(spaces, "sway reported no workspaces at all")
        self.assertEqual(len(rows), len(spaces), got.stdout)
        for row, ws in zip(rows, spaces):
            self.assertEqual(row.split()[-1], ws["name"], row)
            self.assertIn("VP: N/A", row)
            self.assertIn("WA: N/A", row)
            self.assertIn("DG: ", row)
        current = [i for i, ws in enumerate(spaces) if ws["focused"]]
        self.assertEqual(rows[current[0]].split()[1], "*")

    def test_the_fullscreen_state_reaches_a_reader_on_a_view_less_backend(self):
        """sway has no `views()`, and `_NET_WM_STATE_FULLSCREEN` is one of the
        two states design section 4.4 does not bracket as rich: the source is
        the tree node's `fullscreen_mode`, which the registry already walks for
        the xid pairing. Measured before it did: `xprop -id <shadow>
        _NET_WM_STATE` printed an EMPTY list for a fullscreened foot while
        `wxprop` printed `_NET_WM_STATE_FULLSCREEN` [M 2026-09-10, this rig].
        The state goes away again when sway takes it away, which is the half a
        cached property table gets wrong."""
        shadow = self.shadow_id()
        self.addCleanup(self.swaymsg,
                        "[app_id=%s] fullscreen disable" % FOOT_APP_ID)
        self.swaymsg("[app_id=%s] fullscreen enable" % FOOT_APP_ID)
        self.assertTrue(self.wait(
            lambda: "_NET_WM_STATE_FULLSCREEN" in self.tool(
                ["xprop", "-id", str(shadow), "_NET_WM_STATE"]).stdout,
            timeout=10), "the fullscreen state never reached xprop")
        self.swaymsg("[app_id=%s] fullscreen disable" % FOOT_APP_ID)
        self.assertTrue(self.wait(
            lambda: "_NET_WM_STATE_FULLSCREEN" not in self.tool(
                ["xprop", "-id", str(shadow), "_NET_WM_STATE"]).stdout,
            timeout=10), "the state stayed after sway took it away")

    def test_wmctrl_m_still_names_the_wm_upstream_advertises(self):
        """`_NET_SUPPORTING_WM_CHECK` is not one of ours: it PASSes, and
        `wmctrl -m` reads `Name: wlroots wm` off sway's own 0x200004
        [recon/tools.md 5] whether or not the proxy is there."""
        direct = self.tool(["wmctrl", "-m"], through=False)
        through = self.tool(["wmctrl", "-m"])
        self.assertEqual(direct.stdout.splitlines()[0], "Name: wlroots wm")
        self.assertEqual(direct.stdout, through.stdout)


@unittest.skipUnless(HAVE_XDOTOOL and HAVE_WMCTRL, "needs xdotool and wmctrl")
class WritesLand(ProxyLive):
    """Design section 9.3's third claim: the writes land.

    Every command here is one of recon/tools.md 11's rows aimed at a toplevel
    that has no X window at all, and every one of them is checked against
    `swaymsg -t get_tree` / `get_workspaces` rather than against the proxy's
    own answer -- the compositor is the oracle, because the compositor is what
    the clone would have moved.

    The foot is floated first: sway refuses an absolute move or resize of a
    TILED window with a `SoftCmdError` naming the dialect
    (backend_sway.py:341, 355), which through the proxy is silence on the wire
    and one line in the log (design section 3.3) -- the case
    `test_a_tiled_move_is_silence_and_one_line` pins on purpose.
    """

    prefix = "xw11-writes-"
    want_xterm = False

    def setUp(self):
        node = self.node(app_id=FOOT_APP_ID)
        self.assertIsNotNone(node, "the foot left the tree")
        self.shadow = self.shadow_id()

    def float_it(self):
        self.swaymsg("[app_id=%s] floating enable" % FOOT_APP_ID)
        self.assertTrue(self.wait(
            lambda: (self.node(app_id=FOOT_APP_ID) or {}).get("type")
            == "floating_con", timeout=10), "the foot never floated")
        self.addCleanup(self.swaymsg,
                        "[app_id=%s] floating disable" % FOOT_APP_ID)

    def rect(self):
        return (self.node(app_id=FOOT_APP_ID) or {}).get("rect") or {}

    def settle_rect(self, timeout=10.0):
        """Wait until the compositor's rect stops moving: sway floats a
        container at the size it had tiled and the client then answers the
        configure with the size it wants, so a rect read the instant after
        `floating enable` is a rect that is about to change on its own."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            now = self.rect()
            if now and now == last:
                return now
            last = now
            time.sleep(0.2)
        return self.rect()

    def geometry(self):
        """What `xdotool getwindowgeometry` prints for the shadow: the
        registry's own read-back, which is the other half of a write landing.
        """
        got = self.tool(["xdotool", "getwindowgeometry", str(self.shadow)])
        self.assertEqual(got.returncode, 0, got.stderr)
        return got.stdout

    def test_windowmove_puts_the_toplevel_where_the_compositor_says(self):
        """`xdotool windowmove <w> 100 100` is one `ConfigureWindow` with
        `{x, y}` [recon/tools.md 4.3, 11 row 10]."""
        self.float_it()
        self.settle_rect()
        got = self.tool(["xdotool", "windowmove", str(self.shadow), "100", "100"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout, "", "a write printed something")
        self.assertTrue(self.wait(lambda: self.rect().get("x") == 100,
                                  timeout=10),
                        "sway put the foot at %r" % (self.rect(),))
        self.assertTrue(self.wait(
            lambda: "Position: 100,100" in self.geometry(), timeout=10),
            "the proxy read back %r for sway's %r"
            % (self.geometry(), self.rect()))

    def test_windowsize_resizes_the_toplevel(self):
        """Row 11: `{width, height}`.

        The size that lands is the compositor's and not the request's, and the
        assertion says so: `foot` sizes in whole character cells, so a request
        of 500x400 arrived as 496x399 on this rig [M 2026-09-10] -- which is
        what a Wayland client does with a configure and what an X client with
        size hints does with `XResizeWindow`. What the proxy owes is that the
        request reached the compositor and that the read-back afterwards is the
        compositor's own number, to the pixel."""
        self.float_it()
        self.settle_rect()
        got = self.tool(["xdotool", "windowsize", str(self.shadow), "500", "400"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: abs((self.rect().get("width") or 0) - 500) < 30, timeout=10),
            "sway sized the foot %r" % (self.rect(),))
        now = self.rect()
        self.assertLess(abs(now["height"] - 400), 30, now)
        self.assertTrue(self.wait(
            lambda: "Geometry: %dx%d" % (self.rect()["width"],
                                         self.rect()["height"])
            in self.geometry(), timeout=10),
            "the proxy read back %r for sway's %r" % (self.geometry(), now))

    def test_a_tiled_move_is_silence_on_the_wire_and_one_line_in_the_log(self):
        """sway refuses an absolute move of a tiled window (backend_sway.py:341
        -- `resize set` on a tiled container moves the split ratio instead).
        X gives a client no error when the window manager ignores a
        `ConfigureWindow`, so neither does this: rc 0, nothing on either
        stream, and the backend's own sentence in the proxy's log."""
        self.swaymsg("[app_id=%s] floating disable" % FOOT_APP_ID)
        before = len(self.proxy_log())
        got = self.tool(["xdotool", "windowmove", str(self.shadow), "70", "70"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual((got.stdout, got.stderr), ("", ""))
        said = self.proxy_log()[before:]
        self.assertIn("cannot move a tiled window", said)
        self.assertIn("silence on the wire", said)

    def test_windowactivate_focuses_the_native_toplevel(self):
        """Row 8. Two `ClientMessage`s through the proxy -- the desktop one and
        `_NET_ACTIVE_WINDOW` -- because the union puts `_NET_CURRENT_DESKTOP`
        back into `_NET_SUPPORTED` [recon/env.md 2.1]."""
        self.swaymsg("[app_id=%s] focus" % FOOT_APP_ID)
        self.swaymsg("focus left")
        got = self.tool(["xdotool", "windowactivate", str(self.shadow)])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: (self.node(app_id=FOOT_APP_ID) or {}).get("focused"),
            timeout=10), "sway did not focus the foot")

    def test_windowminimize_puts_it_in_the_scratchpad(self):
        """Row 13: `WM_CHANGE_STATE [3]`, which is `IconicState`
        [recon/tools.md 4.3]. sway's `minimize` is its `unmap` -- the
        scratchpad (backend_sway.py:373) -- and that is what "minimized" means
        on a compositor with no taskbar."""
        self.addCleanup(self.swaymsg,
                        "[app_id=%s] scratchpad show" % FOOT_APP_ID)
        got = self.tool(["xdotool", "windowminimize", str(self.shadow)])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: self.workspace_of(app_id=FOOT_APP_ID) == "__i3_scratch",
            timeout=10),
            "sway has the foot on %r" % (self.workspace_of(app_id=FOOT_APP_ID),))

    def test_wmctrl_r_e_moves_and_resizes_through_the_ewmh_message(self):
        """Row 39. Against a bare Xwayland wmctrl falls back to
        `ConfigureWindow`, because wlroots' xwm does not name
        `_NET_MOVERESIZE_WINDOW` in `_NET_SUPPORTED` [M
        recon/tools/caps/xwl.jsonl MARK 39]; through the proxy the union names
        it and this is the `_NET_MOVERESIZE_WINDOW [0xf00, 10, 10, 300, 200]`
        path of design section 3.4."""
        self.float_it()
        self.settle_rect()
        got = self.tool(["wmctrl", "-r", FOOT_TITLE, "-e", "0,10,10,300,200"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: (self.rect().get("x"), self.rect().get("width")) == (10, 300),
            timeout=10), "sway put the foot at %r" % (self.rect(),))

    def test_wmctrl_b_add_fullscreen_reaches_the_compositor(self):
        """Row 40's shape with the one state sway has a verb for:
        `_NET_WM_STATE [1, atom]` -> `set_state(FULLSCREEN, add)`
        (backend_sway.py:408)."""
        self.addCleanup(self.swaymsg,
                        "[app_id=%s] fullscreen disable" % FOOT_APP_ID)
        got = self.tool(["wmctrl", "-r", FOOT_TITLE, "-b", "add,fullscreen"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: (self.node(app_id=FOOT_APP_ID) or {}).get("fullscreen_mode"),
            timeout=10), "sway did not fullscreen the foot")

    def test_wmctrl_b_add_maximized_vert_is_silence_and_one_line(self):
        """The same row with a state sway has no word for: a plain `CmdError`
        naming the route (backend_sway.py:427 -- "not yet here, and the route
        is a patched compositor (AGENTS.md route 6)"). The client is told
        nothing, because X tells a client nothing when the window manager
        ignores its message."""
        before = len(self.proxy_log())
        got = self.tool(["wmctrl", "-r", FOOT_TITLE, "-b", "add,maximized_vert"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual((got.stdout, got.stderr), ("", ""))
        said = self.proxy_log()[before:]
        self.assertIn("windowstate MAXIMIZED_VERT is not supported", said)
        self.assertIn("not yet", said)

    def test_wmctrl_s_switches_the_compositors_workspace(self):
        """Row 42: `_NET_CURRENT_DESKTOP [1]` to the root -> `set_desktop(1)`,
        which is sway's `workspace number 2` (desktop N is workspace N+1,
        backend_sway.py:481)."""
        self.addCleanup(self.tool, ["wmctrl", "-s", "0"])
        got = self.tool(["wmctrl", "-s", "1"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(self.on_workspace_two, timeout=10),
                        "sway stayed on %r" % (self.workspace_names(),))

    def test_set_desktop_switches_it_too(self):
        """Rows 25 and 26, the pair that exits 1 on a bare Xwayland because the
        atom is missing from `_NET_SUPPORTED` [recon/tools.md 4.10]."""
        self.addCleanup(self.tool, ["xdotool", "set_desktop", "0"])
        got = self.tool(["xdotool", "set_desktop", "1"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(self.on_workspace_two, timeout=10),
                        "sway stayed on %r" % (self.workspace_names(),))

    def workspace_names(self):
        return [(ws["name"], ws["focused"])
                for ws in json.loads(self.swaymsg("", kind="get_workspaces").stdout)]

    def on_workspace_two(self):
        return ("2", True) in self.workspace_names()


@unittest.skipUnless(HAVE_XDOTOOL and HAVE_WMCTRL, "needs xdotool and wmctrl")
class WritesThatClose(ProxyLive):
    """The two commands that end a window, each on a foot of its own: a class
    whose first test closed the shared one would leave the rest of the class
    testing an empty tree."""

    prefix = "xw11-close-"
    want_xterm = False

    #: the second foot, closed by the test that opens it
    VICTIM = "footdoomed"

    def victim(self, title):
        self.swaymsg("exec foot --app-id %s --title %s sh -c 'sleep 600'"
                     % (self.VICTIM, title))
        self.assertTrue(self.wait(lambda: self.node(app_id=self.VICTIM)),
                        "the second foot never appeared")
        got = self.tool(["xdotool", "search", "--class", self.VICTIM])
        self.assertEqual(got.returncode, 0, got.stderr)
        ids = [int(x) for x in got.stdout.split()]
        self.assertEqual(len(ids), 1, "search printed %r" % got.stdout)
        return ids[0]

    def test_windowclose_closes_the_toplevel(self):
        """Row 31. `xdotool windowclose` is `DestroyWindow` and not
        `KillClient` [recon/tools.md 4.3], and `backend.close` is the polite
        close every clone sends."""
        shadow = self.victim("WXL-Doomed-1")
        got = self.tool(["xdotool", "windowclose", str(shadow)])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(lambda: not self.node(app_id=self.VICTIM),
                                  timeout=15), "the foot is still in the tree")

    def test_wmctrl_c_closes_the_toplevel(self):
        """Row 46: `_NET_CLOSE_WINDOW [0, ...]` sent to the ROOT with the
        window in the event's own field [M recon/tools/caps/xwl.jsonl MARK
        46]."""
        self.victim("WXL-Doomed-2")
        got = self.tool(["wmctrl", "-c", "WXL-Doomed-2"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(lambda: not self.node(app_id=self.VICTIM),
                                  timeout=15), "the foot is still in the tree")


@unittest.skipUnless(HAVE_XDOTOOL, "needs xdotool")
class SearchPipelined(ProxyLive):
    """R1: xdotool pipelines `GetWindowAttributes` with `GetGeometry` and
    blocks on the SECOND reply [recon/tools.md 4.1]. libxcb sets
    `request_completed = request_read - 1` on any packet it reads, so a local
    reply written ahead of a forwarded one makes it read NULL for the earlier
    request and the command hangs for ever."""

    prefix = "xw11-pipelined-"

    def test_a_whole_tree_walk_over_shadows_and_real_windows_returns(self):
        deadline = time.monotonic() + 5.0
        got = self.tool(["xdotool", "search", "--class", "."], timeout=5)
        self.assertLess(time.monotonic(), deadline + 1.0)
        self.assertEqual(got.returncode, 0, got.stderr)
        ids = [int(x) for x in got.stdout.split()]
        self.assertIn(self.shadow_id(), ids)
        self.assertIn(self.node(name=XTERM_TITLE)["window"], ids)

    def test_the_onlyvisible_predicate_agrees_with_the_compositor(self):
        """`map_state` is `Window.visible`, which is the predicate
        `wdotool search --onlyvisible` uses (wdotool/window_cmds.py:173)."""
        got = self.tool(["xdotool", "search", "--onlyvisible", "--class",
                        FOOT_APP_ID], timeout=10)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.split(), [str(self.shadow_id())])


@unittest.skipUnless(HAVE_XDOTOOL, "needs xdotool")
class PythonXlib(ProxyLive):
    """R14: "every X library binding, with no port". recon/env.md 9 measured
    python-xlib working through a pure forwarder; this is the same read against
    a proxy that is synthesizing the answer."""

    prefix = "xw11-xlib-"
    want_xterm = False

    def test_python_xlib_reads_the_client_list_and_the_shadows_wm_class(self):
        try:
            import Xlib                                          # noqa: F401
        except ImportError:
            self.skipTest("python3-xlib is not installed")
        script = (
            "import sys\n"
            "from Xlib import display, X\n"
            "d = display.Display(sys.argv[1])\n"
            "r = d.screen().root\n"
            "lst = r.get_full_property(d.intern_atom('_NET_CLIENT_LIST'),\n"
            "                          X.AnyPropertyType)\n"
            "print(' '.join('0x%x' % v for v in lst.value))\n"
            "w = d.create_resource_object('window', lst.value[0])\n"
            "print(w.get_wm_class())\n"
            "g = w.get_geometry()\n"
            "print(g.width, g.height)\n")
        got = subprocess.run([sys.executable, "-c", script, self.proxy_display],
                             env=self.env(), capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        lines = got.stdout.splitlines()
        self.assertEqual(lines[0], "0x%x" % self.shadow_id())
        self.assertEqual(lines[1], "('%s', '%s')" % (FOOT_APP_ID, FOOT_APP_ID))
        rect = self.node(app_id=FOOT_APP_ID)["rect"]
        self.assertEqual(lines[2], "%d %d" % (rect["width"], rect["height"]))


@unittest.skipUnless(HAVE_XDOTOOL and HAVE_XTERM, "needs xdotool and xterm")
class HiDpi(ProxyLive):
    """R8, measured before anything was written on it: with `output HEADLESS-1
    scale 2` sway's tree rect for the xterm and the xterm's own `xwininfo` BOTH
    read 640x360 -- Xwayland's screen shrinks with the logical size, so the
    ratio of X device pixels to compositor logical pixels is **1.0** and
    `Shadows` needs no `_x_ratio` [M 2026-09-10, scratchpad b3/r8_hidpi.py, the
    same rig this class boots]. A compositor that starts scaling Xwayland
    instead breaks this test rather than shipping a wrong rect."""

    prefix = "xw11-hidpi-"
    extra_conf = "output HEADLESS-1 scale 2\n"

    def test_one_x_pixel_is_one_compositor_pixel_on_a_scaled_output(self):
        got = self.tool(["xdotool", "getdisplaygeometry"])
        self.assertEqual(got.returncode, 0, got.stderr)
        outputs = json.loads(self.swaymsg("", kind="get_outputs").stdout)
        rect = outputs[0]["rect"]
        self.assertEqual(outputs[0]["scale"], 2.0, "sway did not take the scale")
        self.assertEqual(got.stdout.split(), [str(rect["width"]),
                                              str(rect["height"])])

    def test_the_shadows_rect_is_the_compositors_rect_unscaled(self):
        shadow = self.shadow_id()
        node = self.node(app_id=FOOT_APP_ID)
        got = self.tool(["xdotool", "getwindowgeometry", str(shadow)])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("Geometry: %dx%d"
                      % (node["rect"]["width"], node["rect"]["height"]),
                      got.stdout)

    def test_the_x_twins_rect_is_the_same_number_on_both_planes(self):
        """The measurement itself: the xterm is a REAL X window, so this
        compares Xwayland's own idea of its size against sway's, with the proxy
        only forwarding."""
        node = self.node(name=XTERM_TITLE)
        got = self.tool(["xdotool", "getwindowgeometry", str(node["window"])])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("Geometry: %dx%d"
                      % (node["rect"]["width"], node["rect"]["height"]),
                      got.stdout)


@unittest.skipUnless(HAVE_XDOTOOL, "needs xdotool")
class WlrFloor(ProxyLive):
    """R15: the same sway, driven through the wlr floor instead of its own IPC.

    Two things are being claimed. The floor's `XPlaneViews._x11()`
    (backend_wlr.py:198) opens an X connection of its own to pair toplevels with
    Xwayland windows, and it must reach XWAYLAND and not the proxy -- a backend
    that dialled the loop it is running inside would wait for an answer only
    that loop can write (design section 2.4). And a backend with no geometry at
    all (`NO_GEOMETRY`, backend_wlr.py:97) still yields a findable shadow, with
    a rect of zero rather than an invented one."""

    prefix = "xw11-wlr-"
    proxy_env = {"WDOTOOL_BACKEND": "wlr"}
    want_xterm = False

    def test_the_shadow_is_found_and_its_rect_is_the_whole_output(self):
        """Measured, and it is NOT the zero rect design section 4.5 predicted:
        `zwlr_foreign_toplevel_management_v1` carries no geometry, and
        `backend_wlr.py:414` answers the OUTPUT's rect with "geometry unknown"
        written beside it rather than zeros. The proxy reports what the backend
        reports, which is also what `wwmctl -lG` prints for the same window --
        the parity target [M 2026-09-10, this rig]."""
        got = self.tool(["xdotool", "search", "--class", FOOT_APP_ID])
        self.assertEqual(got.returncode, 0, got.stderr)
        ids = [int(x) for x in got.stdout.split()]
        self.assertEqual(len(ids), 1, got.stdout)
        got = self.tool(["xdotool", "getwindowname", str(ids[0])])
        self.assertEqual((got.returncode, got.stdout.strip()), (0, FOOT_TITLE),
                         got.stderr)
        outputs = json.loads(self.swaymsg("", kind="get_outputs").stdout)
        rect = outputs[0]["rect"]
        got = self.tool(["xdotool", "getwindowgeometry", str(ids[0])])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("Geometry: %dx%d" % (rect["width"], rect["height"]),
                      got.stdout)
        self.assertIn("Position: 0,0", got.stdout)

    def test_the_shadow_has_no_desktop_on_a_backend_with_no_workspaces(self):
        """`_NET_WM_DESKTOP` is `0xFFFFFFFF` where the backend cannot say, and
        the refusal is one log line naming the route and never an X error
        (design section 3.3): the wlr floor answers `get_desktop` with "this
        compositor publishes no ext_workspace_manager_v1"."""
        shadow = self.shadow_id()
        got = self.tool(["xprop", "-id", str(shadow), "_NET_WM_DESKTOP"])
        self.assertEqual(got.returncode, 0, got.stderr)
        # the floor's desktops come from ext_workspace_manager_v1, which sway 1.11
        # publishes on some builds (Arch's, Ubuntu 26.10's) and not on others
        # (Ubuntu 26.04's) -- CI run 34563917823 measured both; the registry decides
        reg = subprocess.run(
            [sys.executable, "-c", "from wdotool import backend_detect as b; "
             "print(' '.join(sorted(b.session_registry() or {})))"],
            env=self.env(through=False), capture_output=True, text=True, timeout=30)
        if "ext_workspace_manager_v1" in reg.stdout.split():
            # the floor knows the workspaces but the protocol carries no window ->
            # workspace mapping, so the answer is whatever the CLONE says for the
            # same window on the same backend: -1 there is 0xFFFFFFFF here
            clone = self.tool([sys.executable, "-m", "wwmctl", "-l"], through=False, WDOTOOL_BACKEND="wlr")
            self.assertEqual(clone.returncode, 0, clone.stderr)
            rows = [ln.split(None, 3) for ln in clone.stdout.splitlines()]
            desk = [r[1] for r in rows if len(r) == 4 and r[3].strip() == FOOT_TITLE]
            self.assertEqual(len(desk), 1, clone.stdout)
            want = "4294967295" if desk[0] == "-1" else desk[0]
            self.assertEqual(got.stdout.strip(), "_NET_WM_DESKTOP(CARDINAL) = %s" % want)
        else:
            self.assertEqual(got.stdout.strip(),
                             "_NET_WM_DESKTOP(CARDINAL) = 4294967295")
            self.assertIn("ext_workspace_manager_v1", self.proxy_log())

    def test_the_proxys_own_environment_points_at_xwayland(self):
        """R15 in the log: the proxy says which upstream its own connection
        went to, and it is the rig's Xwayland and never its own display. The
        tool call first is not decoration -- the own connection is opened at the
        FIRST ACCEPT and not at startup, so a proxy nobody has used pins no
        Xwayland (design section 2.6)."""
        self.assertNotIn("own connection", self.proxy_log())
        self.shadow_id()
        self.assertIn("own connection to %s" % self.rig.display, self.proxy_log())
        self.assertNotIn("own connection to %s" % self.proxy_display,
                         self.proxy_log())


@unittest.skipUnless(HAVE_XDOTOOL, "needs xdotool")
class TerminateAndReturn(ProxyLive):
    """R16: Xwayland restarts under a live proxy.

    Root, atoms and extension majors survive a restart and XIDs do not
    [recon/env.md 2.5, 6], so the registry is re-based when the proxy's own
    connection comes back -- and an id a script saved before the restart names
    nothing (design section 2.6's row).

    The restart is forced by closing the proxy's own connection rather than by
    waiting out Xwayland's `-terminate`: the proxy HOLDS a connection for
    exactly as long as it lives, which is what keeps that timer from firing at
    all, and holding it is the point rather than an accident. What the test
    then proves is the half that is ours -- that a re-opened connection
    re-mints, that the new ids are in the new base, and that `search` works
    across it."""

    prefix = "xw11-terminate-"
    want_xterm = False

    def test_the_ids_are_re_based_and_search_works_across_the_restart(self):
        first = self.shadow_id()
        base = self.rid_base()
        self.assertEqual(first & ~0x1FFFFF, base)
        self.restart_own()
        second = self.shadow_id()
        # a base the server handed out: mask-aligned and non-zero. Not "the
        # base the next connection gets": the server reissues freed bases out
        # of order (batch 3's measurement), and on a CI runner the proxy's own
        # reconnection and this test's probe were handed different ones
        # (run 34562929804: 0x400000 against 0x600000).
        self.assertEqual(second & 0x1FFFFF, second - (second & ~0x1FFFFF))
        self.assertTrue(second & ~0x1FFFFF)
        self.assertEqual((second & ~0x1FFFFF) % 0x200000, 0)
        self.assertTrue(second & 0x1FFFFF)
        got = self.tool(["xdotool", "getwindowname", str(second)])
        self.assertEqual((got.returncode, got.stdout.strip()), (0, FOOT_TITLE),
                         got.stderr)

    def restart_own(self):
        """Close every client, then make the proxy's own connection go. The
        only lever from outside the process is the X server itself, so this
        kills the proxy's connection by killing what it is connected to: sway
        relaunches Xwayland lazily from the `-listenfd`s it kept
        [recon/env.md 6]."""
        import signal as signal_mod

        pids = subprocess.run(["pgrep", "-x", "Xwayland"], capture_output=True,
                              text=True, timeout=20).stdout.split()
        mine = []
        for pid in pids:
            try:
                with open("/proc/%s/environ" % pid, "rb") as f:
                    if self.rig.rtdir.encode() in f.read():
                        mine.append(int(pid))
            except OSError:
                continue
        if not mine:
            self.skipTest("no Xwayland of this rig's to restart")
        for pid in mine:
            os.kill(pid, signal_mod.SIGTERM)
        self.assertTrue(self.wait(
            lambda: "is gone" in self.proxy_log(), timeout=20),
            "the proxy never noticed its own connection go")
        self.assertTrue(self.wait(
            lambda: self.tool(["xdotool", "search", "--class",
                              FOOT_APP_ID]).returncode == 0, timeout=30),
            "the proxy never came back")

@unittest.skipUnless(HAVE_XDOTOOL and HAVE_XPROP, "needs xdotool and xprop")
class WmStateTombstone(ProxyLive):
    """A rig of its own, because the one test in it takes a property away for
    good: a tombstone lives on the registry entry for as long as the entry does
    (design section 4.7), and there is no request in THIS batch that puts a
    synthesized name back -- `ChangeProperty` on a shadow is batch 4's."""

    prefix = "xw11-wmstate-"
    want_xterm = False

    def test_getwindowfocus_does_not_climb_when_wm_state_is_absent(self):
        """The fact design section 4.4's `WM_STATE` row rests on, measured
        rather than repeated -- and it is not what the row says.

        The row reads: `xdotool getwindowfocus` climbs from the focus window
        until it finds `WM_STATE`, so a shadow without it answers the root. It
        DOES read the property (`GetInputFocus, InternAtom WM_STATE,
        GetProperty WM_STATE` [recon/tools.md 4.2]) and it does NOT climb on an
        absent one: with `WM_STATE` tombstoned out of the live shadow, the same
        binary prints the same id and exits 0 [M 2026-09-10, this rig].

        `WM_STATE` stays on every backend regardless, because Mutter writes it
        on every X11 window it manages and a shadow that answers "is this
        minimized?" the same way is a script that keeps working across the two
        planes. An xdotool that starts climbing changes this test, which is the
        point of pinning the measurement instead of the belief.
        """
        shadow = self.shadow_id()
        self.swaymsg("[app_id=%s] focus" % FOOT_APP_ID)
        self.assertTrue(self.wait(
            lambda: self.tool(["xdotool", "getwindowfocus"]).stdout.strip()
            == str(shadow), timeout=10), "the focus never settled on the foot")
        conn = x11_mini.X11Conn(self.proxy_display)
        self.addCleanup(conn.close)
        atom = conn.atom("WM_STATE")
        # A full `GetProperty` with `delete = 1` is the one way a client can
        # take a synthesized name off a shadow (design section 4.7): the
        # overlay records a tombstone, and the synthesis does not put it back.
        seq = conn._send(wire.OP_GET_PROPERTY, 1,
                         struct.pack("<IIIII", shadow, atom, 0, 0, 0xFFFFFFFF))
        pkt, _body = conn._wait_reply(seq)
        self.assertEqual(struct.unpack_from("<II", pkt, 12), (0, 2),
                         "the read that deletes has to be the one that finished")
        got = self.tool(["xprop", "-id", str(shadow)])
        self.assertNotIn("WM_STATE(WM_STATE)", got.stdout)
        got = self.tool(["xdotool", "getwindowfocus"])
        self.assertEqual((got.returncode, got.stdout.strip()), (0, str(shadow)),
                         got.stderr)



# -- RandR: the write side, on a compositor that really moves ------------------
#
# recon/tools.md 11 rows 56-62 measured `xrandr --output HEADLESS-1 --mode`,
# `--off` and `--rotate` exiting **1** against a bare Xwayland, every one of them
# dying at `RRSetScreenSize` (minor 7) with BadMatch and the server still
# grabbed [recon/env.md 2.4, recon/tools.md 7]. What follows is the same
# commands through the proxy, where they exit 0 and sway's own outputs move --
# measured on this box on 2026-09-10, and pinned here so they stay that way.
#
# Every test restores the rig's layout through `swaymsg` rather than through the
# thing under test, so a failure in one leaves the next one a screen to work on.

HAVE_XRANDR = bool(shutil.which("xrandr"))

#: sway's own reset. Not `xrandr --auto`: an output the compositor has switched
#: off is gone from Xwayland's list altogether, so xrandr cannot name it any
#: more (measured -- see `test_auto_cannot_bring_back_an_output_that_is_gone`).
RESET = ("output HEADLESS-1 enable mode 1280x720 position 0 0 "
         "transform normal scale 1")


@unittest.skipUnless(HAVE_XRANDR, "needs xrandr")
class RandrWrites(ProxyLive):
    """One head, and the seven write paths."""

    prefix = "xw11-randr-"
    want_xterm = False

    def setUp(self):
        self.reset()

    def tearDown(self):
        self.reset()

    def reset(self):
        got = self.swaymsg(RESET)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(lambda: self.output("HEADLESS-1") is not None
                                  and self.output("HEADLESS-1")["active"],
                                  timeout=10), "the rig never came back")

    # -- what the compositor says ---------------------------------------------

    def outputs_now(self):
        got = self.swaymsg("", kind="get_outputs")
        return json.loads(got.stdout)

    def output(self, name):
        for o in self.outputs_now():
            if o["name"] == name:
                return o
        return None

    def rect(self, name):
        o = self.output(name)
        return None if o is None else (o["rect"]["x"], o["rect"]["y"],
                                       o["rect"]["width"], o["rect"]["height"])

    def xrandr(self, *args, through=True):
        got = self.tool(["xrandr"] + list(args), through=through)
        return got

    def query_line(self, name="HEADLESS-1", through=True):
        got = self.xrandr("-q", through=through)
        self.assertEqual(got.returncode, 0, got.stderr)
        for line in got.stdout.splitlines():
            if line.startswith(name + " "):
                return line
        raise AssertionError("%s is not in `xrandr -q`:\n%s"
                             % (name, got.stdout))

    def starred_mode(self, through=True):
        """The mode `xrandr -q` marks current with a `*`."""
        got = self.xrandr("-q", through=through)
        self.assertEqual(got.returncode, 0, got.stderr)
        for line in got.stdout.splitlines():
            if "*" in line and line.startswith("   "):
                return line.split()[0]
        return None

    # -- the read side --------------------------------------------------------

    def test_query_through_the_proxy_is_byte_identical_to_the_direct_run(self):
        """The read side is Xwayland's, passed through untouched (design
        section 7.2). Nothing on it is synthesized, so this is the whole claim
        and it is an equality of bytes."""
        direct = self.xrandr("-q", through=False)
        through = self.xrandr("-q", through=True)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(through.returncode, 0, through.stderr)
        self.assertEqual(direct.stdout, through.stdout)
        self.assertEqual(direct.stderr, through.stderr)

    def test_listmonitors_is_byte_identical_too(self):
        direct = self.xrandr("--listmonitors", through=False)
        through = self.xrandr("--listmonitors", through=True)
        self.assertEqual(through.returncode, 0, through.stderr)
        self.assertEqual(direct.stdout, through.stdout)

    # -- the write side -------------------------------------------------------

    def test_mode_exits_zero_and_sway_and_the_query_both_follow(self):
        """recon/tools.md 11 row 56: exit 1 without the proxy, and the reason is
        a BadMatch at minor 7 [recon/env.md 2.4]."""
        direct = self.xrandr("--output", "HEADLESS-1", "--mode", "800x600",
                             through=False)
        self.assertEqual(direct.returncode, 1,
                         "Xwayland stopped refusing --mode on its own; this "
                         "test's premise is gone")
        self.assertIn("BadMatch", direct.stderr)
        self.reset()
        got = self.xrandr("--output", "HEADLESS-1", "--mode", "800x600")
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        self.assertTrue(self.wait(lambda: self.rect("HEADLESS-1")
                                  == (0, 0, 800, 600), timeout=10),
                        "sway is at %r" % (self.rect("HEADLESS-1"),))
        self.assertEqual(self.starred_mode(), "800x600",
                         "`xrandr -q` did not follow the compositor")

    def test_a_headless_output_cannot_be_put_back_to_a_larger_mode_by_name(self):
        """A row, measured 2026-09-10, and it is Xwayland's and not the proxy's.

        A headless output has no EDID, so Xwayland MAKES its mode list from the
        output's current size: at 1280x720 it lists sixteen modes down to
        640x350, and after the compositor moves it to 800x600 it lists ten,
        none of them larger than 800x600 (the refreshes change too -- 800x600
        reads 59.86 before and 59.47 after, because the modeline is a new one).
        So `--mode 1280x720` then answers `xrandr: cannot find mode 1280x720`
        client-side, before any request goes out, and exits 1. On a real
        monitor the list is the EDID's and does not move.

        **Not yet**, and the route is rung 1 plus a read-side edit: the
        compositor's own output management knows the sizes a virtual head can
        take (it is why `wxrandr` marks such an output `virtual_modes` and
        accepts any WxH), so the proxy can add them to the
        `GetScreenResources` and `GetOutputInfo` replies -- at the cost of the
        read side no longer being byte-identical to Xwayland's, which design
        section 7.2 spends its whole length on. `wxrandr --output HEADLESS-1
        --mode 1280x720` does it today, because it never asks the X server what
        the output can do.
        """
        got = self.xrandr("--output", "HEADLESS-1", "--mode", "800x600")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(lambda: self.rect("HEADLESS-1")
                                  == (0, 0, 800, 600), timeout=10))
        listed = self.xrandr("-q").stdout
        self.assertNotIn("1280x720", listed,
                         "Xwayland kept the larger mode in the list")
        back = self.xrandr("--output", "HEADLESS-1", "--mode", "1280x720")
        self.assertEqual(back.returncode, 1)
        self.assertIn("cannot find mode 1280x720", back.stderr)
        # and the route named in the docstring really is one
        wx = self.tool([sys.executable, "-m", "wxrandr", "--output",
                        "HEADLESS-1", "--mode", "1280x720"], through=True)
        self.assertEqual(wx.returncode, 0, wx.stderr)
        self.assertTrue(self.wait(lambda: self.rect("HEADLESS-1")
                                  == (0, 0, 1280, 720), timeout=10),
                        "wxrandr could not put it back either")

    def test_pos_moves_the_output(self):
        got = self.xrandr("--output", "HEADLESS-1", "--pos", "0x0")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.rect("HEADLESS-1"), (0, 0, 1280, 720))

    def test_primary_exits_zero_and_the_query_reads_it_back(self):
        """The one write path that already worked [recon/env.md 2.4]: the
        request PASSes to Xwayland, which keeps the flag, and the proxy records
        it into `wxrandr`'s own state so the two agree (design section 7.3)."""
        got = self.xrandr("--output", "HEADLESS-1", "--primary")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("primary", self.query_line())

    def test_rotate_left_turns_sway_to_270_and_normal_turns_it_back(self):
        """recon/tools.md 11 row 61: exit 1 without the proxy, and the grab
        leaked with it. `left` is RandR rotation 2, which `RANDR_VIEW` reads as
        sway's `270` (wxrandr/core.py:109)."""
        direct = self.xrandr("--output", "HEADLESS-1", "--rotate", "left",
                             through=False)
        self.assertEqual(direct.returncode, 1, "the premise of row 61 is gone")
        self.reset()
        got = self.xrandr("--output", "HEADLESS-1", "--rotate", "left")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: (self.output("HEADLESS-1") or {}).get("transform") == "270",
            timeout=10), "sway's transform is %r"
            % ((self.output("HEADLESS-1") or {}).get("transform"),))
        self.assertEqual(self.rect("HEADLESS-1"), (0, 0, 720, 1280))
        back = self.xrandr("--output", "HEADLESS-1", "--rotate", "normal")
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertTrue(self.wait(
            lambda: (self.output("HEADLESS-1") or {}).get("transform")
            == "normal", timeout=10))

    def test_scale_gives_the_client_the_desktop_x_gives_it(self):
        """recon/tools.md 11 row 60: exit 1 without the proxy, `BadValue` at
        minor 26 [recon/env.md 2.4].

        `--scale 2x2` on X does not make things bigger -- it makes the crtc read
        a 2560x1440 rectangle of the framebuffer through a 1280x720 mode, so the
        screen GROWS and everything on it shrinks (which is why xrandr sends
        `SetScreenSize 2560x1440` with it). The compositor's `scale` is the
        opposite: measured here 2026-09-10, `swaymsg output HEADLESS-1 scale 2`
        leaves the output rect 640x360 and `scale 0.5` leaves it 2560x1440. So
        the proxy hands the compositor 1/s and the client is left looking at the
        2560x1440 X would have given it. `wxrandr --scale` keeps the
        compositor's own meaning -- that is the flag's own front end, and this
        is xrandr's."""
        got = self.xrandr("--output", "HEADLESS-1", "--scale", "2x2")
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        self.assertTrue(self.wait(lambda: self.rect("HEADLESS-1")
                                  == (0, 0, 2560, 1440), timeout=10),
                        "sway is at %r with scale %r"
                        % (self.rect("HEADLESS-1"),
                           (self.output("HEADLESS-1") or {}).get("scale")))
        self.assertAlmostEqual((self.output("HEADLESS-1") or {})["scale"], 0.5)
        self.assertIn("2560x1440+0+0", self.query_line(),
                      "the client was not given the desktop X gives it")
        # `--scale 1x1` afterwards exits 0 and puts sway's scale back to 1.0,
        # but the head stays 2560x1440 (measured): a headless output's mode list
        # is regenerated from its CURRENT logical size, so by then the crtc's
        # current mode IS 2560x1440 and that is what xrandr re-sends. Same row
        # as `test_a_headless_output_cannot_be_put_back_to_a_larger_mode_by_name`
        # and the same route; on a real monitor the list is the EDID's and does
        # not move. `tearDown`'s reset is what puts this rig back.
        back = self.xrandr("--output", "HEADLESS-1", "--scale", "1x1")
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertAlmostEqual((self.output("HEADLESS-1") or {})["scale"], 1.0)

    def test_a_bare_fb_exits_zero_and_the_log_names_its_route(self):
        """`xrandr --fb 2560x1440` is a grab, a `SetScreenSize` and an ungrab
        with no crtc change in it: on X the screen really grows past its
        outputs and the pointer pans around it. Not yet here -- and the exit
        code stays the 0 X gave it rather than becoming an error X never wrote.
        The route is rung 1, a scale below 1 on the head (the test above shows
        it working), at the cost of scaling the outputs instead of leaving a
        larger screen behind them; a real panning framebuffer is rung 6."""
        got = self.xrandr("--fb", "2560x1440")
        self.assertEqual(got.returncode, 0, got.stderr + got.stdout)
        self.assertEqual(self.rect("HEADLESS-1"), (0, 0, 1280, 720))
        self.assertIn("`--fb`", self.proxy_log())
        self.assertIn("route 1", self.proxy_log())

    def test_xrandr_and_wxrandr_print_the_same_geometry_after_an_apply(self):
        """The two front ends have to agree about the screen they just changed:
        one asked the compositor directly, the other asked it through an X
        server and a proxy."""
        got = self.xrandr("--output", "HEADLESS-1", "--mode", "800x600")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(lambda: self.rect("HEADLESS-1")
                                  == (0, 0, 800, 600), timeout=10))
        mine = self.tool([sys.executable, "-m", "wxrandr", "-q"], through=True)
        self.assertEqual(mine.returncode, 0, mine.stderr)
        theirs = self.query_line()
        line = [ln for ln in mine.stdout.splitlines()
                if ln.startswith("HEADLESS-1 ")]
        self.assertTrue(line, mine.stdout)
        self.assertIn("800x600+0+0", line[0])
        self.assertIn("800x600+0+0", theirs)

    def test_a_created_mode_is_accepted_and_applied(self):
        """`--newmode` and `--addmode` both already succeed on Xwayland
        [recon/env.md 2.4]; both PASS. What the proxy adds is the
        `SetCrtcConfig` that names the new id afterwards: it resolves the id
        from the snapshot's own modeline to a size and a rate, which is
        `resolve_real_mode`'s rule (design section 7.3)."""
        made = self.xrandr("--newmode", "xw11live", "38.25", "800", "832",
                           "912", "1024", "600", "603", "607", "624")
        self.assertEqual(made.returncode, 0, made.stderr)
        add = self.xrandr("--addmode", "HEADLESS-1", "xw11live")
        self.assertEqual(add.returncode, 0, add.stderr)
        self.assertIn("xw11live", self.xrandr("-q").stdout)
        got = self.xrandr("--output", "HEADLESS-1", "--mode", "xw11live")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(lambda: self.rect("HEADLESS-1")
                                  == (0, 0, 800, 600), timeout=10),
                        "sway is at %r" % (self.rect("HEADLESS-1"),))


@unittest.skipUnless(HAVE_XRANDR, "needs xrandr")
class RandrFailure(ProxyLive):
    """Design section 7.5: an apply that does not happen is an X ERROR, not a
    log line and a zero exit.

    The failure is arranged the way a real one arrives -- a session whose
    output-management backend cannot be used -- by pinning the proxy's
    `WXRANDR_BACKEND` to `kwin` on a box running sway. `chosen_backend` picks
    it, the probe finds no `kde_output_management_v2`, and every commit raises
    the `Fatal` the CLI would have printed. Nothing about the compositor is
    disturbed, which is why this is its own rig: a layout sway itself refuses
    would leave the screen in whatever state the refusal stopped at.
    """

    prefix = "xw11-randr-fail-"
    want_xterm = False
    proxy_env = {"WXRANDR_BACKEND": "kwin"}

    def test_a_failed_apply_prints_the_x_error_block_and_exits_one(self):
        got = self.tool(["xrandr", "--output", "HEADLESS-1", "--mode",
                         "800x600"])
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        self.assertIn("X Error of failed request", got.stderr)
        self.assertIn("BadMatch", got.stderr)
        self.assertIn("Minor opcode of failed request:  7 (RRSetScreenSize)",
                      got.stderr)
        self.assertIn("Serial number of failed request", got.stderr)
        self.assertIn("Current serial number in output stream", got.stderr)
        self.assertIn("was not applied", self.proxy_log())

    def test_the_compositor_is_left_exactly_as_it_was(self):
        """A refused layout is a layout that did not happen -- not half of
        one."""
        before = json.loads(self.swaymsg("", kind="get_outputs").stdout)
        self.tool(["xrandr", "--output", "HEADLESS-1", "--mode", "800x600"])
        after = json.loads(self.swaymsg("", kind="get_outputs").stdout)
        self.assertEqual([(o["name"], o["rect"]) for o in before],
                         [(o["name"], o["rect"]) for o in after])


@unittest.skipUnless(HAVE_XRANDR, "needs xrandr")
class TwoHeads(ProxyLive):
    """R2, live. Design section 7.7 inferred the multi-output batch from
    `apply()`'s source and said batch 7 had to measure it before the coalescing
    rule was trusted for more than one crtc."""

    prefix = "xw11-randr2-"
    want_xterm = False
    outputs = 2

    def setUp(self):
        for cmd in ("output HEADLESS-1 enable mode 1280x720 position 0 0 "
                    "transform normal scale 1",
                    "output HEADLESS-2 enable mode 1280x720 position 1280 0 "
                    "transform normal scale 1"):
            self.swaymsg(cmd)
        self.assertTrue(self.wait(lambda: len(self.heads()) == 2, timeout=10),
                        "the second head never came up")

    def heads(self):
        got = json.loads(self.swaymsg("", kind="get_outputs").stdout)
        return {o["name"]: (o["rect"]["x"], o["rect"]["y"], o["rect"]["width"],
                            o["rect"]["height"]) for o in got if o["active"]}

    def output_of(self, name):
        for o in json.loads(self.swaymsg("", kind="get_outputs").stdout):
            if o["name"] == name:
                return o
        return None

    def rect_of(self, name):
        o = self.output_of(name)
        return None if o is None else (o["rect"]["x"], o["rect"]["y"],
                                       o["rect"]["width"], o["rect"]["height"])

    def xrandr(self, *args, through=True):
        return self.tool(["xrandr"] + list(args), through=through)

    def test_right_of_lays_the_two_heads_out_side_by_side(self):
        got = self.tool(["xrandr", "--output", "HEADLESS-2", "--right-of",
                         "HEADLESS-1"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: self.heads().get("HEADLESS-1", (None,))[0] == 0
            and self.heads().get("HEADLESS-2", (None,))[0] == 1280,
            timeout=10), "sway has %r" % (self.heads(),))

    def test_left_of_swaps_them(self):
        got = self.tool(["xrandr", "--output", "HEADLESS-2", "--left-of",
                         "HEADLESS-1"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: self.heads().get("HEADLESS-2", (None,))[0] == 0
            and self.heads().get("HEADLESS-1", (None,))[0] == 1280,
            timeout=10), "sway has %r" % (self.heads(),))

    def test_a_mode_and_a_relation_in_one_invocation_are_one_apply(self):
        """Two `--output` blocks, two crtcs, one grab -- and the proxy applies
        it once, which is the claim the log line carries."""
        before = self.proxy_log().count("applied a RandR batch")
        got = self.tool(["xrandr", "--output", "HEADLESS-1", "--mode",
                         "800x600", "--output", "HEADLESS-2", "--right-of",
                         "HEADLESS-1"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: self.heads().get("HEADLESS-1") == (0, 0, 800, 600)
            and self.heads().get("HEADLESS-2", (None,))[0] == 800,
            timeout=10), "sway has %r" % (self.heads(),))
        after = [ln for ln in self.proxy_log().splitlines()
                 if "applied a RandR batch" in ln]
        self.assertEqual(len(after) - before, 1,
                         "the grab was applied %d times" % (len(after) - before))
        self.assertIn("batch of 2 crtc(s)", after[-1])


@unittest.skipUnless(HAVE_XRANDR, "needs xrandr")
class RandrOff(ProxyLive):
    """`--off`, and what it costs. Its own rig, because an output this
    compositor has switched off is one nothing here turns back on."""

    prefix = "xw11-randr-off-"
    want_xterm = False
    outputs = 2

    def output_of(self, name):
        for o in json.loads(self.swaymsg("", kind="get_outputs").stdout):
            if o["name"] == name:
                return o
        return None

    def xrandr(self, *args, through=True):
        return self.tool(["xrandr"] + list(args), through=through)

    def test_off_turns_the_output_off(self):
        """recon/tools.md 11 row 58: exit 1 without the proxy.

        On the SECOND head, because a sway 1.11 headless session whose
        only output is switched off while a client still has a window on
        it goes away on this box (measured 2026-09-10: the IPC socket
        closes mid-command). That is the compositor's own bug and not
        this proxy's -- `swaymsg output HEADLESS-1 disable` alone survives
        it, and so does an --off with no window open -- but a suite that
        depended on it would be measuring the crash."""
        got = self.xrandr("--output", "HEADLESS-2", "--off")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: not (self.output_of("HEADLESS-2") or {}).get("active"),
            timeout=10), "sway still has the output on")

    def test_auto_cannot_bring_back_an_output_that_is_gone(self):
        """The measurement that turns into a docs row, taken 2026-09-10.

        A compositor that switches an output off drops its `wl_output`, so
        Xwayland stops listing it: `xrandr -q` no longer has a HEADLESS-2 at
        all, and `--auto` prints `warning: output HEADLESS-2 not found;
        ignoring` and exits 0 with nothing done. On X the output stays listed
        with no crtc and `--auto` brings it back.

        Not yet, and the route is rung 1: `zwlr_output_management_v1` DOES list
        disabled heads (`wxrandr`'s own `WlrOutputs` reads them), so the proxy
        can put the missing outputs back into the `GetScreenResources` and
        `GetOutputInfo` replies -- at the cost of a read-side EDIT the design
        does not have yet (section 7.2 synthesizes nothing on the read side)
        and of minting RandR ids of its own for them. `wxrandr --output
        HEADLESS-2 --auto` turns it back on today, because it asks the
        compositor and not the X server.
        """
        off = self.xrandr("--output", "HEADLESS-2", "--off")
        self.assertEqual(off.returncode, 0, off.stderr)
        self.assertTrue(self.wait(
            lambda: not (self.output_of("HEADLESS-2") or {}).get("active"),
            timeout=10))
        gone = self.xrandr("-q")
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertNotIn("HEADLESS-2", gone.stdout,
                         "Xwayland kept listing an output sway switched off")
        auto = self.xrandr("--output", "HEADLESS-2", "--auto")
        self.assertEqual(auto.returncode, 0, auto.stderr)
        self.assertIn("not found", auto.stderr)
        self.assertFalse((self.output_of("HEADLESS-2") or {}).get("active"))


# -- the event rows: the two commands that exit 124 today ----------------------
#
# `xdotool behave 6291468 mouse-enter true` and `xprop -spy -root
# _NET_ACTIVE_WINDOW` are rows 29 and 51 of recon/tools.md 11: 32 and 14
# requests, then a block on the socket, then RC 124 when the harness's 6 s
# timeout killed them -- on Xvfb+openbox as well as on Xwayland+sway, because
# what they wait for is an event about a window neither server was ever told
# about. These are the rows going green.


def _reap(proc):
    """A command left running by a failed assertion, ended. Nothing in this
    suite may outlive its test (`tests/support.py`'s rule for daemons, and the
    same one for the tools)."""
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
    for pipe in (proc.stdout, proc.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except OSError:                          # pragma: no cover
            pass


class Lines:
    """A subprocess's stdout, read on a thread.

    `xprop -spy` and `xdotool behave` never exit: a test that read their pipe
    synchronously would be the 6 s timeout recon measured. The thread turns
    "print a line when told" into something a deadline can be asserted about.
    """

    def __init__(self, proc):
        self.proc = proc
        self.lines = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def wait_for(self, count, timeout=10.0):
        """`count` lines, or an AssertionError naming what did arrive. Answers
        the seconds it took, which is what the report's latency is."""
        started = time.monotonic()
        deadline = started + timeout
        while time.monotonic() < deadline:
            if len(self.lines) >= count:
                return time.monotonic() - started
            if self.proc.poll() is not None and len(self.lines) < count:
                raise AssertionError("the command exited (%s) with %r"
                                     % (self.proc.returncode, self.lines))
            time.sleep(0.02)
        raise AssertionError("only %d line(s) in %gs: %r"
                             % (len(self.lines), timeout, self.lines))

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:    # pragma: no cover
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.thread.join(timeout=5)
        for pipe in (self.proc.stdout, self.proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:                      # pragma: no cover
                pass


@unittest.skipUnless(HAVE_XDOTOOL and HAVE_XPROP, "needs xdotool and xprop")
class SpyRows(ProxyLive):
    """`xprop -spy` on a shadow, on the root, and on the X twin."""

    prefix = "xw11-spy-"
    want_xterm = True

    #: A second foot, running `cat` on a fifo: whatever is written into the
    #: fifo is printed on its terminal, so an OSC-2 escape retitles it. sway has
    #: no rename verb and no backend in the tree has one, so the compositor's
    #: own title change has to come from the client.
    SPY_APP_ID = "footspy"
    SPY_TITLE = "WXL-Spy"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.fifo = os.path.join(cls.rig.rtdir, "retitle")
            os.mkfifo(cls.fifo)
            cls.swaymsg("exec foot --app-id %s --title %s sh -c "
                        "'while :; do cat %s; done'"
                        % (cls.SPY_APP_ID, cls.SPY_TITLE, cls.fifo))
            if not cls.wait(lambda: cls.node(app_id=cls.SPY_APP_ID) is not None):
                raise unittest.SkipTest("the second foot never appeared")
        except BaseException:
            cls.stop_proxy()
            cls.rig.stop()
            raise

    @classmethod
    def retitle(cls, text):
        """The escape xterm and foot both answer: `ESC ] 2 ; <text> BEL`."""
        with open(cls.fifo, "w") as fh:
            fh.write("\033]2;%s\007" % text)
            fh.flush()

    def spy_shadow(self):
        got = self.tool(["xdotool", "search", "--class", self.SPY_APP_ID])
        self.assertEqual(got.returncode, 0, got.stderr)
        ids = [int(x) for x in got.stdout.split()]
        self.assertEqual(len(ids), 1, "search printed %r" % got.stdout)
        return ids[0]

    def xterm_id(self):
        node = self.node(name=XTERM_TITLE)
        self.assertIsNotNone(node, "the xterm is gone")
        return node["window"]

    def spy(self, argv):
        proc = subprocess.Popen(argv, env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        got = Lines(proc)
        self.addCleanup(got.stop)
        return got

    def test_xprop_spy_on_a_shadow_prints_the_new_title(self):
        """The shadow half of recon/tools.md 6: the mask is
        `StructureNotify|PropertyChange` on the target, the re-read is a fresh
        full `GetProperty`, and the compositor's title change is what has to
        reach it. sway's `title` event arrived +17.5 ms after the window
        started on this box (2026-09-10), so a second here is a ceiling and
        not a target."""
        shadow = self.spy_shadow()
        spy = self.spy(["xprop", "-spy", "-id", str(shadow), "WM_NAME"])
        spy.wait_for(1)
        self.assertIn(self.SPY_TITLE, spy.lines[0])
        want = "WXL-Renamed-%d" % int(time.monotonic() * 1000 % 100000)
        self.retitle(want)
        # The compositor first: a rename that sway never saw is this rig's
        # fifo and not the proxy's event path, and the two failures read
        # nothing alike.
        self.assertTrue(
            self.wait(lambda: (self.node(app_id=self.SPY_APP_ID)
                               or {}).get("name") == want, timeout=15),
            "sway never saw the rename (the rig, not the proxy)")
        took = spy.wait_for(2, timeout=5.0)
        self.assertIn(want, spy.lines[1])
        # One second, not the five the wait allows: sway's own `title` event
        # arrived +17.5 ms and +19.2 ms after the window started on this box
        # (two runs, 2026-09-10), and the proxy's re-list is 0.08 ms on top of
        # it. A bound equal to the wait cannot fail -- `wait_for` raises first.
        self.assertLess(took, 1.0, "the rename took %gs to reach xprop, "
                        "against +17.5 ms measured for sway's own event" % took)

    def test_xprop_spy_on_the_root_prints_on_a_focus_change(self):
        """Row 51, which is RC 124 without this. The root's
        `_NET_ACTIVE_WINDOW` is synthesized from the compositor and upstream's
        own copy of the name is dropped on the way down (design section 4.6),
        so what xprop prints is one line per focus change and not two.

        The window to focus is chosen from what xprop's FIRST line says is
        active, not from a focus this test set beforehand: sway is free to move
        the focus for its own reasons between two `swaymsg` calls (measured
        here 2026-09-11, 4 runs in 5 -- the second foot had it back before
        xprop had started), and focusing the window that already has it is
        correctly no event at all.
        """
        import re

        ids = {FOOT_APP_ID: self.shadow_id(),
               self.SPY_APP_ID: self.spy_shadow()}
        spy = self.spy(["xprop", "-spy", "-root", "_NET_ACTIVE_WINDOW"])
        spy.wait_for(1)
        got = re.search(r"0x[0-9a-f]+", spy.lines[0])
        active = int(got.group(0), 16) if got else 0
        app_id = [name for name, wid in ids.items() if wid != active][0]
        self.swaymsg("[app_id=%s] focus" % app_id)
        took = spy.wait_for(2, timeout=5.0)
        self.assertIn("0x%x" % ids[app_id], spy.lines[1])
        self.assertLess(took, 2.0, "the focus took %gs to reach xprop" % took)
        # One line per change: a proxy that let upstream's root PropertyNotify
        # through as well would print this twice.
        time.sleep(0.5)
        self.assertEqual(len(spy.lines), 2, spy.lines)

    @unittest.skipUnless(HAVE_XTERM, "needs xterm (the X twin)")
    def test_the_x_twin_is_spied_on_exactly_once_per_rename(self):
        """A real X window's per-window events are Xwayland's own and pass
        untouched; the proxy adds none of its own for them (design section
        5.3). Two renames, two lines -- not four."""
        xterm = self.xterm_id()
        spy = self.spy(["xprop", "-spy", "-id", str(xterm), "WM_NAME"])
        spy.wait_for(1)
        for name in ("WXL-Twin-1", "WXL-Twin-2"):
            got = self.tool(["xdotool", "set_window", "--name", name,
                             str(xterm)])
            self.assertEqual(got.returncode, 0, got.stderr)
            spy.wait_for(1 + int(name[-1]), timeout=5.0)
        time.sleep(0.5)
        self.assertEqual(len(spy.lines), 3, spy.lines)
        self.assertIn("WXL-Twin-1", spy.lines[1])
        self.assertIn("WXL-Twin-2", spy.lines[2])

    def test_search_sync_returns_when_a_new_foot_appears(self):
        """`search --sync` POLLS [recon/tools.md 4.1] -- it is the read side's
        row, not the event side's -- and it is here because what it polls is
        the registry the event pump keeps invalidating."""
        app_id = "footsync"
        proc = subprocess.Popen(["xdotool", "search", "--sync", "--class",
                                 app_id], env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        self.addCleanup(_reap, proc)
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), "search --sync returned before the "
                                       "window existed")
        self.swaymsg("exec foot --app-id %s --title WXL-Sync sh -c "
                     "'sleep 600'" % app_id)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err)
        ids = [int(x) for x in out.split()]
        self.assertEqual(len(ids), 1, out)
        self.swaymsg("[app_id=%s] kill" % app_id)

    def test_a_python_xlib_watcher_on_the_root_sees_a_window_come_and_go(self):
        """R14's other half: a window watcher is a client that selects
        `SubstructureNotify` on the root and is told. Nothing in the four tools
        does this -- it is what devilspie2, arbtt and every status bar do."""
        try:
            import Xlib                                          # noqa: F401
        except ImportError:
            self.skipTest("python3-xlib is not installed")
        script = (
            "import sys, time\n"
            "from Xlib import display, X\n"
            "d = display.Display(sys.argv[1])\n"
            "r = d.screen().root\n"
            "r.change_attributes(event_mask=X.SubstructureNotifyMask)\n"
            "d.sync()\n"
            "print('ready', flush=True)\n"
            "end = time.time() + float(sys.argv[2])\n"
            "while time.time() < end:\n"
            "    if d.pending_events():\n"
            "        print(d.next_event().__class__.__name__, flush=True)\n"
            "    else:\n"
            "        time.sleep(0.02)\n")
        proc = subprocess.Popen([sys.executable, "-c", script,
                                 self.proxy_display, "30"], env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        watcher = Lines(proc)
        self.addCleanup(watcher.stop)
        watcher.wait_for(1)
        self.assertEqual(watcher.lines[0], "ready")
        app_id = "footwatch"
        self.swaymsg("exec foot --app-id %s --title WXL-Watch sh -c "
                     "'sleep 600'" % app_id)
        self.assertTrue(self.wait(
            lambda: "MapNotify" in watcher.lines, timeout=15))
        self.assertIn("CreateNotify", watcher.lines)
        self.assertLess(watcher.lines.index("CreateNotify"),
                        watcher.lines.index("MapNotify"),
                        "X creates before it maps")
        self.swaymsg("[app_id=%s] kill" % app_id)
        self.assertTrue(self.wait(
            lambda: "DestroyNotify" in watcher.lines, timeout=15))
        self.assertLess(watcher.lines.index("UnmapNotify"),
                        watcher.lines.index("DestroyNotify"),
                        "X unmaps before it destroys")


@unittest.skipUnless(HAVE_SWAY and HAVE_FOOT and HAVE_XDOTOOL,
                     "needs sway, foot and xdotool")
class PointerRows(unittest.TestCase):
    """`xdotool behave <w> mouse-enter` -- row 29, RC 124 today.

    The proxy runs IN THIS PROCESS here, which no other live class does, and
    for one reason: the pointer source design section 5.6 falls back on is the
    proxy's own last ROUTED position, and until batch 6 lands XTEST there is no
    way to route one from outside. A test that cannot set it cannot make this
    row green at all, so the compositor's environment is borrowed for the
    length of the class -- `backend_detect` caches per process and is reset on
    the way in and out -- and `server.pointer_model` is written straight.

    sway's IPC carries no cursor [recon/seams.md 2.3], so on sway this IS the
    source. A physical mouse is not yet seen: the route is AGENTS.md route 4,
    evdev, at the cost of read access to /dev/input.
    """

    @classmethod
    def setUpClass(cls):
        from wdotool import backend_detect

        cls.rig = HeadlessSway("xw11-pointer-", need_display=True)
        cls.saved = {k: os.environ.get(k)
                     for k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY",
                               "SWAYSOCK", "DISPLAY")}
        try:
            os.environ.update(XDG_RUNTIME_DIR=cls.rig.rtdir,
                              WAYLAND_DISPLAY=cls.rig.wayland_display(),
                              SWAYSOCK=cls.rig.sock)
            backend_detect.reset()
            cls.swaymsg("exec foot --app-id %s --title %s sh -c 'sleep 600'"
                        % (FOOT_APP_ID, FOOT_TITLE))
            if not cls.wait(lambda: cls.node(app_id=FOOT_APP_ID) is not None):
                raise unittest.SkipTest("foot window never appeared")
            cls.display = display_mod.allocate()
            cls.log = open(os.devnull, "w")
            cls.server = server_mod.Server(
                cls.display, cls.rig.display, log=cls.log, idle=0.0,
                check=0.05, watch_wayland=False)
            cls.thread = threading.Thread(target=cls.server.serve_forever,
                                          daemon=True)
            cls.thread.start()
        except BaseException:
            cls._restore()
            cls.rig.stop()
            raise

    @classmethod
    def _restore(cls):
        from wdotool import backend_detect

        for key, value in cls.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        backend_detect.reset()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop("the test is over")
        cls.thread.join(timeout=10)
        cls.log.close()
        cls.display.release()
        cls._restore()
        cls.rig.stop()

    @classmethod
    def swaymsg(cls, cmd):
        return subprocess.run(["swaymsg", "-s", cls.rig.sock, cmd],
                              env=cls.rig.env, capture_output=True, text=True,
                              timeout=20)

    @classmethod
    def node(cls, **match):
        tree = json.loads(subprocess.run(
            ["swaymsg", "-s", cls.rig.sock, "-t", "get_tree"], env=cls.rig.env,
            capture_output=True, text=True, timeout=20).stdout)
        for n in _walk(tree):
            if n.get("pid") and all(n.get(k) == v for k, v in match.items()):
                return n
        return None

    @classmethod
    def wait(cls, ready, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready():
                return True
            time.sleep(0.2)
        return False

    def env(self):
        return dict(self.rig.env, DISPLAY=self.display.name,
                    W11_PASSTHROUGH="never", LC_ALL="C")

    def shadow_id(self):
        got = subprocess.run(["xdotool", "search", "--class", FOOT_APP_ID],
                             env=self.env(), capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        ids = [int(x) for x in got.stdout.split()]
        self.assertEqual(len(ids), 1, "search printed %r" % got.stdout)
        return ids[0]

    def test_behave_mouse_enter_fires_when_the_pointer_enters_the_rect(self):
        """Row 29 going green: 32 requests, the last of them
        `ChangeWindowAttributes(EventMask = EnterWindow)`
        [M recon/tools.md 4.7], and then a line on stdout instead of a
        timeout."""
        shadow = self.shadow_id()
        rect = self.node(app_id=FOOT_APP_ID)["rect"]
        self.server.pointer_model = (rect["x"] + rect["width"] + 50,
                                     rect["y"] + rect["height"] + 50)
        proc = subprocess.Popen(["xdotool", "behave", str(shadow),
                                 "mouse-enter", "exec", "echo", "hit"],
                                env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        got = Lines(proc)
        self.addCleanup(got.stop)
        # The mask has to be in before the pointer moves: `behave` selects it
        # with its last request and then blocks.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.server.pointer_wanted():
            time.sleep(0.05)
        self.assertTrue(self.server.pointer_wanted(),
                        "behave never selected EnterWindow")
        self.server.pointer_model = (rect["x"] + rect["width"] // 2,
                                     rect["y"] + rect["height"] // 2)
        took = got.wait_for(1, timeout=10.0)
        self.assertEqual(got.lines[0], "hit")
        self.assertLess(took, 3.0, "the crossing took %gs" % took)

    def test_and_not_while_the_pointer_stays_outside(self):
        """The counter-case: a proxy that fired `EnterNotify` on every sample
        would make the row above meaningless."""
        shadow = self.shadow_id()
        rect = self.node(app_id=FOOT_APP_ID)["rect"]
        self.server.pointer_model = (rect["x"] + rect["width"] + 400,
                                     rect["y"] + rect["height"] + 400)
        proc = subprocess.Popen(["xdotool", "behave", str(shadow),
                                 "mouse-enter", "exec", "echo", "hit"],
                                env=self.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)
        got = Lines(proc)
        self.addCleanup(got.stop)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.server.pointer_wanted():
            time.sleep(0.05)
        time.sleep(1.0)
        self.assertEqual(got.lines, [])



# -- the input path (design section 6) ----------------------------------------
#
# `/usr/bin/xdotool` on this box is **3.20160805.1**, the apt one -- the
# generation Debian and Ubuntu ship, which moves the pointer with core
# `WarpPointer` and reads it with `QueryPointer` [recon/wire.md 5.3]. The
# pinned 4.20260303.1 moves it with `FakeInput MotionNotify`. Both are on this
# box and the rows below run both, because a symlink over /usr/bin/xdotool
# replaces the 3.x and that is the one most users have.


def _pinned_xdotool():
    """The 4.20260303.1 the tree pins, or None. Same search
    `tests/test_xw11_parity.py` does."""
    import glob

    from wdotool import cli as wdo_cli
    got = os.environ.get("W11_ORACLE_PATH", "")
    for d in got.split(":") + sorted(
            glob.glob("/nix/store/*-xdotool-%s/bin" % wdo_cli.XDO_VERSION)):
        exe = os.path.join(d, "xdotool")
        if d and os.access(exe, os.X_OK):
            return exe
    return None


PINNED_XDOTOOL = _pinned_xdotool()


def _distro_xdotool_version():
    """The version of the `xdotool` on `PATH`, or "".

    `xdotool --version` prints `xdotool version 3.20160805.1`. Probed once, at
    import: which generation the distro ships decides which of the two pointer
    rows can run, and the two write the pointer differently -- 3.x with core
    `WarpPointer`, 4.x with `FakeInput MotionNotify` [recon/wire.md 5.3].
    """
    if not HAVE_XDOTOOL:
        return ""
    try:
        got = subprocess.run(["xdotool", "--version"], capture_output=True,
                             text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):       # pragma: no cover
        return ""
    parts = got.stdout.split()
    return parts[-1] if parts else ""


DISTRO_XDOTOOL_VERSION = _distro_xdotool_version()

#: recon/env.md 2.3's line, which real xdotool puts into a UTF-8 xterm
#: byte for byte through Xwayland.
UNICODE_LINE = "ünï €ur ß λ 日"


@unittest.skipUnless(HAVE_XDOTOOL, "needs xdotool")
class InputLands(ProxyLive):
    """`type`, `key`, `click`, `mousemove` and `getmouselocation` through the
    proxy, into a native toplevel and into an X one.

    This is the gap the whole proxy exists to close, measured: with one `foot`
    focused and no X client anywhere, `xdotool type` writes an EMPTY FILE
    [recon/env.md 2.7]. Through the proxy the same command writes the text.
    """

    prefix = "xw11-input-"
    want_xterm = True
    CAT_FOOT = "catfoot"
    CAT_XTERM = "WXL-CatX"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            why = cls.why_no_injection()
            if why:
                raise unittest.SkipTest("this rig cannot inject input: %s" % why)
            cls.foot_file = os.path.join(cls.rig.rtdir, "typed-foot.txt")
            cls.xterm_file = os.path.join(cls.rig.rtdir, "typed-xterm.txt")
            cls.swaymsg("exec foot --app-id %s --title WXL-Cat sh -c 'cat > %s'"
                        % (cls.CAT_FOOT, cls.foot_file))
            if not cls.wait(lambda: cls.node(app_id=cls.CAT_FOOT) is not None):
                raise unittest.SkipTest("the cat foot never appeared")
            env8 = dict(cls.rig.env, LC_ALL="C.UTF-8")
            cls.xterm_proc = subprocess.Popen(
                ["xterm", "-u8", "-T", cls.CAT_XTERM, "-e", "sh", "-c",
                 "cat > %s" % cls.xterm_file], env=env8,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not cls.wait(lambda: cls.node(name=cls.CAT_XTERM) is not None):
                raise unittest.SkipTest("the cat xterm never appeared")
        except BaseException:
            cls.stop_proxy()
            cls.rig.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        # The daemon the proxy spawned lives in the rig's runtime directory and
        # outlives the proxy: stop it before the directory goes, the way every
        # daemon test in this suite does.
        try:
            import support

            support.stop_daemons_under(cls.rig.rtdir)
        except Exception:                    # never lose the rig teardown
            pass
        super().tearDownClass()

    @classmethod
    def why_no_injection(cls):
        """Why this box cannot inject at all, or None.

        A capability check and never an outcome one: a proxy bug that stopped
        every keystroke must fail these rows, not skip them. The daemon takes
        `/dev/uinput` when it can open it and the compositor's
        `zwp_virtual_keyboard_v1`/`zwlr_virtual_pointer_v1` when it cannot
        [docs/WDOTOOL.md's table; recon/seams.md 5.3 step 4], so either one is
        enough.
        """
        if os.access("/dev/uinput", os.W_OK):
            return None
        path = os.path.join(cls.rig.rtdir, cls.rig.wayland_display())
        try:
            from wdotool import vkbd, vptr
        except ImportError as e:             # pragma: no cover - a broken tree
            return str(e)
        for opener in (vkbd.VirtualKeyboard, vptr.VirtualPointer):
            try:
                got = opener.open(socket_path=path)
            except Exception as e:           # VkbdError / VptrError
                return str(e)
            got.close()
        return None

    # -- the windows ----------------------------------------------------------

    def focus(self, **match):
        """Give one toplevel the compositor's focus and wait for it: a
        keystroke goes where the focus is, on both halves."""
        node = self.node(**match)
        self.assertIsNotNone(node, "no toplevel matching %r" % (match,))
        self.swaymsg("[con_id=%d] focus" % node["id"])
        self.assertTrue(self.wait(lambda: (self.node(**match) or {}).get("focused"),
                                  timeout=10), "the focus never moved")
        return node

    def lines(self, path, timeout=8.0):
        """The lines the shell's line discipline has flushed into a file. It
        flushes one per Return, so the file IS the wire record
        [recon/env.md 2.3]."""
        deadline = time.monotonic() + timeout
        got = []
        while time.monotonic() < deadline:
            try:
                with open(path, "rb") as f:
                    got = f.read().decode("utf-8", "replace").splitlines()
            except OSError:
                got = []
            if got:
                return got
            time.sleep(0.2)
        return got

    def type_line(self, text, delay=20, utf8=False, xdotool=None):
        """One `type` and the `Return` that flushes it."""
        exe = xdotool or "xdotool"
        kw = {"LC_ALL": "C.UTF-8"} if utf8 else {}
        got = self.tool([exe, "type", "--delay", str(delay), text], **kw)
        self.assertEqual(got.returncode, 0, got.stderr)
        got = self.tool([exe, "key", "Return"], **kw)
        self.assertEqual(got.returncode, 0, got.stderr)

    def warm_up(self, path, **match):
        """One line typed and thrown away.

        recon/env.md 2.3 measured the first character of the first `type` after
        a fresh focus going missing once, unbisected (R6), and did every
        Unicode measurement after a warm-up line for that reason. Measured here
        on 2026-09-11: the first-ever type into a freshly created xterm wrote
        `armup` for `warmup`; ten types after ten fresh focuses wrote all ten
        byte for byte, into the foot and into the xterm alike. So it is a
        first-USE race, not a per-focus one, and this is the same warm-up the
        recon did.
        """
        self.focus(**match)
        n = len(self.lines(path, timeout=0.1))
        self.type_line("warmup")
        self.assertTrue(self.wait(lambda: len(self.lines(path, 0.1)) > n,
                                  timeout=15),
                        "nothing at all arrived in %s" % path)

    # -- what lands -----------------------------------------------------------

    def test_type_into_the_native_toplevel_lands_in_its_file(self):
        """The one measurement this whole package exists for: `xdotool type`
        with a `foot` focused writes an EMPTY FILE today [recon/env.md 2.7]."""
        self.warm_up(self.foot_file, app_id=self.CAT_FOOT)
        self.type_line("hello world 123")
        self.assertTrue(self.wait(
            lambda: self.lines(self.foot_file, 0.1)[-1:] == ["hello world 123"],
            timeout=15), self.lines(self.foot_file, 0.1))

    def test_a_ctrl_chord_erases_the_line_it_typed(self):
        """`xdotool key a b c ctrl+u x y z Return` -> `xyz`: the modifier
        chords are right, and the modifier is HELD across the key it modifies
        even though xdotool sends three presses of it [recon/tools.md 4.5,
        design section 6.3]."""
        self.warm_up(self.foot_file, app_id=self.CAT_FOOT)
        before = len(self.lines(self.foot_file, 0.1))
        got = self.tool(["xdotool", "key", "a", "b", "c", "ctrl+u",
                         "x", "y", "z", "Return"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.wait(
            lambda: len(self.lines(self.foot_file, 0.1)) > before, timeout=15))
        self.assertEqual(self.lines(self.foot_file, 0.1)[-1], "xyz")

    def test_type_into_the_xterm_is_byte_perfect_for_the_layouts_own_text(self):
        """The baseline that must not regress: against an X client the
        unmodified tools already work on sway [recon/env.md 2.3]."""
        self.warm_up(self.xterm_file, name=self.CAT_XTERM)
        self.type_line("hello world 123", utf8=True)
        self.assertTrue(self.wait(
            lambda: self.lines(self.xterm_file, 0.1)[-1:] == ["hello world 123"],
            timeout=15), self.lines(self.xterm_file, 0.1))

    def test_the_unicode_line_goes_through_xwaylands_own_remap(self):
        """`ünï €ur ß λ 日` into a UTF-8 xterm.

        A keystroke for an X window is forwarded to Xwayland untouched (design
        section 6.1 as measured, `xtest.Engine.x_owns_the_keyboard`), so what
        types these characters is xdotool's own spare-keycode remap of
        Xwayland's map -- the route recon/env.md 2.3 measured byte-perfect and
        the input daemon cannot take, because the compositor's layout is US and
        none of these is on it.

        The assertion is a SUBSEQUENCE and not the whole line, because the
        remap path itself is racy on this rig and is racy WITHOUT the proxy
        too: measured 2026-09-11, six direct runs at `--delay 60` interleaved
        with six proxied ones wrote the line whole 3/6 times direct and 2/6
        times proxied, losing one character each other time -- the xterm's own
        keymap cache against the `MappingNotify` pair xdotool sends around
        every character. A row in the docs, not a claim hidden here.
        """
        self.warm_up(self.xterm_file, name=self.CAT_XTERM)
        before = len(self.lines(self.xterm_file, 0.1))
        self.type_line(UNICODE_LINE, delay=60, utf8=True)
        self.assertTrue(self.wait(
            lambda: len(self.lines(self.xterm_file, 0.1)) > before, timeout=20))
        got = self.lines(self.xterm_file, 0.1)[-1]
        # Every character that arrived is one of the line's, in its order: the
        # race loses characters, it never invents or reorders them.
        rest = iter(UNICODE_LINE)
        self.assertTrue(all(ch in rest for ch in got),
                        "%r is not a subsequence of %r" % (got, UNICODE_LINE))
        wanted = [c for c in UNICODE_LINE if ord(c) > 127]
        arrived = [c for c in got if ord(c) > 127]
        # The floor is the WORST loss ever measured on this rig and not a
        # guess. Two measurements, both on 2026-09-11: six direct runs at
        # `--delay 60` interleaved with six proxied ones lost at most ONE of
        # the six non-ASCII characters; a whole-file run of this suite, with
        # the rig under the load of every other class in it, lost TWO --
        # `ünï €ur ß  ` for `ünï €ur ß λ 日`, the trailing pair gone and the
        # rest byte for byte. Tightening this to one turned the row amber in
        # one whole-file run out of four, which is asserting the race.
        self.assertGreaterEqual(
            len(arrived), len(wanted) - 2,
            "%d of %d non-ASCII characters arrived (%r): the measured envelope "
            "is 4..6 of 6 and this is below it -- a new fact, not the known "
            "race [M 2026-09-11, both runs above]"
            % (len(arrived), len(wanted), got))

    def test_r6_ten_types_after_ten_fresh_focuses_into_the_native_toplevel(self):
        """R6. recon/env.md 2.3 saw the first character of the first `type`
        after a fresh focus go missing, once, unbisected, and recon/env.md 11.5
        says so in as many words. Ten runs here, each after focusing the other
        window and back; every byte is recorded and a loss FAILS this rather
        than being explained away.

        Measured 2026-09-11 through this proxy on headless sway: 10/10 byte
        for byte into the foot, twice over.
        """
        self.warm_up(self.foot_file, app_id=self.CAT_FOOT)
        got, want = [], []
        for i in range(10):
            self.focus(name=self.CAT_XTERM)
            self.focus(app_id=self.CAT_FOOT)
            line = "ABCDEFG%d" % i
            want.append(line)
            before = len(self.lines(self.foot_file, 0.1))
            self.type_line(line)
            self.assertTrue(self.wait(
                lambda n=before: len(self.lines(self.foot_file, 0.1)) > n,
                timeout=15), "run %d wrote nothing at all" % i)
            got.append(self.lines(self.foot_file, 0.1)[-1])
        self.assertEqual(got, want)

    def test_r6_ten_types_after_ten_fresh_focuses_into_the_xterm(self):
        """R6's other half, on the X plane. Measured 2026-09-11: 10/10 byte
        for byte."""
        self.warm_up(self.xterm_file, name=self.CAT_XTERM)
        got, want = [], []
        for i in range(10):
            self.focus(app_id=self.CAT_FOOT)
            self.focus(name=self.CAT_XTERM)
            line = "abcdefg%d" % i
            want.append(line)
            before = len(self.lines(self.xterm_file, 0.1))
            self.type_line(line, utf8=True)
            self.assertTrue(self.wait(
                lambda n=before: len(self.lines(self.xterm_file, 0.1)) > n,
                timeout=15), "run %d wrote nothing at all" % i)
            got.append(self.lines(self.xterm_file, 0.1)[-1])
        self.assertEqual(got, want)

    # -- the pointer ----------------------------------------------------------

    def location(self, exe="xdotool"):
        got = self.tool([exe, "getmouselocation"])
        self.assertEqual(got.returncode, 0, got.stderr)
        return dict(part.split(":", 1) for part in got.stdout.split())

    @unittest.skipUnless(DISTRO_XDOTOOL_VERSION.startswith("3."),
                         "the distro xdotool is not the 3.x generation")
    def test_the_apt_three_x_moves_the_pointer_and_reads_it_back(self):
        """`xdotool` on `PATH` is 3.20160805.1 here: it moves with core
        `WarpPointer` and reads with `QueryPointer` [recon/wire.md 5.3]. The
        proxy owns both, so this row is the 3.x generation end to end. A box
        whose distro package is 4.x skips it rather than failing it -- the 4.x
        generation has a row of its own below."""
        got = self.tool(["xdotool", "mousemove", "10", "10"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.location()["x"], "10")
        self.assertEqual(self.location()["y"], "10")

    @unittest.skipUnless(PINNED_XDOTOOL, "needs the pinned xdotool 4.x")
    def test_the_pinned_four_x_moves_the_pointer_and_reads_it_back(self):
        """The same command on the generation that moves with
        `FakeInput MotionNotify` instead."""
        got = self.tool([PINNED_XDOTOOL, "mousemove", "21", "22"])
        self.assertEqual(got.returncode, 0, got.stderr)
        where = self.location(PINNED_XDOTOOL)
        self.assertEqual((where["x"], where["y"]), ("21", "22"))

    def test_getmouselocation_names_the_shadow_under_the_pointer(self):
        """`window:` is the compositor's toplevel, by the id the proxy minted
        -- which is the whole point: without one it names an X window or
        nothing at all."""
        node = self.node(app_id=self.CAT_FOOT)
        x = node["rect"]["x"] + 20
        y = node["rect"]["y"] + 20
        self.tool(["xdotool", "mousemove", str(x), str(y)])
        where = self.location()
        self.assertEqual((where["x"], where["y"]), (str(x), str(y)))
        ids = self.tool(["xdotool", "search", "--class",
                         self.CAT_FOOT]).stdout.split()
        self.assertEqual(len(ids), 1, "search printed %r" % ids)
        self.assertEqual(where["window"], ids[0])

    def test_click_moves_the_compositors_focus_to_the_window_under_it(self):
        """A click has to move the SEAT's pointer and press the SEAT's button:
        that is what raises and focuses a window. Xwayland's own warp moves
        only Xwayland's pointer -- measured 2026-09-11, a raw `WarpPointer` to
        (321, 123) straight at Xwayland moved its own `QueryPointer` there and
        the compositor's cursor not at all."""
        self.focus(name=self.CAT_XTERM)
        node = self.node(app_id=self.CAT_FOOT)
        self.tool(["xdotool", "mousemove", str(node["rect"]["x"] + 20),
                   str(node["rect"]["y"] + 20)])
        got = self.tool(["xdotool", "click", "1"])
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(
            self.wait(lambda: (self.node(app_id=self.CAT_FOOT) or {}).get("focused"),
                      timeout=10),
            "the click did not reach the compositor")



if __name__ == "__main__":
    unittest.main()
