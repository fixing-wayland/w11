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
        self.assertEqual(second & ~0x1FFFFF, self.rid_base())
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


if __name__ == "__main__":
    unittest.main()
