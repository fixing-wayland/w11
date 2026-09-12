#!/usr/bin/env python3
"""The Wayfire backend against a real, headless Wayfire.

tests/test_backend_wayfire.py proves the bytes against a double built from recordings; what it cannot prove
is that Wayfire agrees -- that `configure-view` really moves the window, that `vswitch/set-workspace` really
walks the viewport grid, that the pointer we inject with no /dev/uinput and no root really lands where
Wayfire says it does. Every check below therefore reads the answer back through Wayfire's own IPC with a
client of this file's own (`_ipc`, twenty lines of struct and socket), never through the code under test.

Two things here exist nowhere else in the suite. `wayfire/create-headless-output` adds and removes a monitor
from inside the session, instantly, so this is the first multi-head test with no QEMU under it
[M recon2/wayfire.md §1.2: the created head came up as HEADLESS-2 at 1280,0 and was visible to
`wxrandr --query` at once]. And the input half runs with no /dev/uinput at all -- this guest has none
[M recon2/wayfire.md §2.3] -- so `mousemove` goes through `zwlr_virtual_pointer_v1` and is checked against
`window-rules/get_cursor_position`, which is a compositor-side answer no wlroots backend of ours could read
before this one.

Skips cleanly wherever wayfire (or, for the window half, foot) is not installed."""

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

import support
from support import HeadlessWayfire

#: Wayfire's `[core] plugins` REPLACES the default list rather than adding to it, and every IPC method comes
#: from a loaded plugin: with `plugins = ipc ipc-rules` the socket answers 23 methods and none of `grid/*`,
#: `vswitch/*` or `wm-actions/*` (measured here against wayfire 0.10.0). `wm-actions` is not in the stock
#: list at all (`/usr/share/wayfire/metadata/core.xml` line 10), so it has to be named here even though the
#: rest of this line is the default.
PLUGINS = ("autostart ipc ipc-rules stipc grid vswitch wm-actions window-rules "
           "move resize place foreign-toplevel")

#: two layouts that bind different symbols, which is what makes the group question real: with one layout,
#: or two that agree, `choose_group` cannot be wrong [M recon2/wayfire.md §2.7]
XKB_LAYOUT = "us,de"


class _Wayfire(HeadlessWayfire):
    """`support.HeadlessWayfire` with the plugins the window half needs, and a two-layout keyboard.

    The shared helper loads `autostart ipc ipc-rules stipc`, which is every read; the writes live in three
    more plugins (see PLUGINS above). Everything else -- the short runtime directory both labwc and Wayfire
    need, the socket wait, the `$WAYFIRE_SOCKET` export -- is the helper's."""

    def write_config(self, confdir):
        self.conf = os.path.join(confdir, "wayfire.ini")
        with open(self.conf, "w") as f:
            f.write("[core]\nplugins = %s\nxwayland = true\n"
                    "\n[input]\nxkb_layout = %s\n"
                    "\n[output:HEADLESS-1]\nmode = 1280x720\n"
                    "\n[autostart]\nrep = sh -c 'echo \"$DISPLAY\" > %s/display'\n"
                    % (PLUGINS, XKB_LAYOUT, self.rtdir))


def _ipc(sockpath, method, **data):
    """One Wayfire IPC call through this file's own client: `<i` length, JSON body, both ways.

    Deliberately not `wdotool.backend_wayfire._WayfireIPC`: this is the oracle every assertion below reads
    its answer from, and an oracle that shares its wire code with the thing it is checking can only prove
    that the two agree with each other."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(sockpath)
        body = json.dumps({"method": method, "data": data}).encode()
        s.sendall(struct.pack("<i", len(body)) + body)

        def read(n):
            buf = b""
            while len(buf) < n:
                chunk = s.recv(n - len(buf))
                if not chunk:
                    raise AssertionError("wayfire closed the IPC socket")
                buf += chunk
            return buf

        (length,) = struct.unpack("<i", read(4))
        return json.loads(read(length).decode())
    finally:
        s.close()


@unittest.skipUnless(shutil.which("wayfire"), "wayfire is not installed")
class WayfireLive(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.rig = _Wayfire()
        # Cleanups run last-in-first-out, so this pair reads in the order it runs: stop whatever daemon a
        # test spawned, and only then let the rig remove the directory that daemon listens in. A daemon whose
        # socket directory is gone is unreachable, which used to mean immortal
        # (tests/test_daemon_lifetime.py). Both are registered before the first spawn.
        cls.addClassCleanup(cls.rig.stop)
        cls.addClassCleanup(support.stop_daemons_under, cls.rig.rtdir)
        cls.sock = cls.rig.sock

    # -- helpers ------------------------------------------------------------

    @classmethod
    def env(cls, **extra):
        env = dict(os.environ, XDG_RUNTIME_DIR=cls.rig.rtdir,
                   WAYLAND_DISPLAY=os.path.basename(cls.rig.wayland_socket),
                   WAYFIRE_SOCKET=cls.sock, PYTHONPATH=ROOT,
                   W11_PASSTHROUGH="never")
        # nothing of the runner's own session may leak in: a stray $SWAYSOCK would be found first, and
        # WDOTOOL_FAKE_UINPUT would replace the very path this file is here to exercise
        for name in ("SWAYSOCK", "I3SOCK", "HYPRLAND_INSTANCE_SIGNATURE", "WDOTOOL_FAKE_UINPUT",
                     "WDOTOOL_BACKEND", "DISPLAY"):
            env.pop(name, None)
        if cls.rig.display:
            env["DISPLAY"] = cls.rig.display
        env.update(extra)
        return env

    def tool(self, module, *argv, **kw):
        """One of our CLIs, in a process of its own, on the rig's session."""
        p = subprocess.run([sys.executable, "-m", module, *argv], env=self.env(**kw),
                           capture_output=True, text=True, timeout=60)
        return p

    def ok(self, module, *argv, **kw):
        p = self.tool(module, *argv, **kw)
        self.assertEqual(p.returncode, 0, "%s %s\n%s%s" % (module, argv, p.stdout, p.stderr))
        return p.stdout

    def ipc(self, method, **data):
        return _ipc(self.sock, method, **data)

    def view(self, vid):
        reply = self.ipc("window-rules/view-info", id=int(vid))
        self.assertIn("info", reply, reply)
        return reply["info"]

    def foot(self, title):
        """One real client, mapped, whose view id comes back. Skips where foot is not installed."""
        if not shutil.which("foot"):
            self.skipTest("foot is not installed")
        proc = subprocess.Popen(["foot", "-T", title, "sh", "-c", "sleep 600"],
                                env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, proc)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for v in self.ipc("window-rules/list-views"):
                if v.get("title") == title and v.get("mapped"):
                    return v["id"]
            if proc.poll() is not None:
                self.skipTest("foot exited before it mapped a window")
            time.sleep(0.2)
        self.skipTest("foot never mapped a window on this Wayfire")

    @staticmethod
    def _reap(proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    # -- detection ----------------------------------------------------------

    def test_00_detection_lands_on_the_wayfire_backend(self):
        """The socket is below the bus names and above the registry probe, and Wayfire advertises
        `zwlr_foreign_toplevel_manager_v1` -- so on a real session the registry probe is exactly what this
        arm has to beat [M recon2/wayfire.md §1.1, §2]."""
        code = ("import json, os, sys;"
                "sys.path.insert(0, %r);"
                "from wdotool import backend_detect;"
                "b = backend_detect.detect();"
                "print(json.dumps([b.name, type(b).__name__]))" % ROOT)
        p = subprocess.run([sys.executable, "-c", code], env=self.env(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), ["wayfire", "WayfireBackend"])

    # -- windows ------------------------------------------------------------

    def test_10_windowmove_lands_where_wayfire_says(self):
        """The floor could not move a window at all (`windowmove is not supported by the wlr backend`) and
        reported the output rectangle as its geometry [M recon2/wayfire.md §2.3]."""
        vid = self.foot("fwmove")
        self.ok("wdotool", "windowmove", str(vid), "40", "50")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.view(vid)["geometry"]["x"] != 40:
            time.sleep(0.1)
        geo = self.view(vid)["geometry"]
        self.assertEqual((geo["x"], geo["y"]), (40, 50))
        # and the tool reports the window, not the screen
        out = self.ok("wdotool", "getwindowgeometry", str(vid))
        self.assertIn("Position: 40,50", out)
        self.assertIn("Geometry: %dx%d" % (geo["width"], geo["height"]), out)
        self.assertNotIn("Geometry: 1280x720", out)

    def test_11_windowsize_is_the_clients_own_quantisation(self):
        """foot asked for 500x400 settles at 498x390 -- its cell grid, not Wayfire's, exactly as on sway
        [M recon2/wayfire.md §3.3]. The claim is that the tool and the compositor agree, not that the client
        obeys to the pixel."""
        vid = self.foot("fwsize")
        self.ok("wdotool", "windowsize", str(vid), "500", "400")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.view(vid)["geometry"]["width"] > 500:
            time.sleep(0.1)
        geo = self.view(vid)["geometry"]
        self.assertLessEqual(geo["width"], 500)
        self.assertGreater(geo["width"], 450)
        self.assertLessEqual(geo["height"], 400)
        out = self.ok("wdotool", "getwindowgeometry", str(vid))
        self.assertIn("Geometry: %dx%d" % (geo["width"], geo["height"]), out)

    def test_12_the_listing_carries_the_pid_and_the_real_geometry(self):
        vid = self.foot("fwlist")
        info = self.view(vid)
        geo = [info["geometry"][k] for k in ("x", "y", "width", "height")]
        out = self.ok("wwmctl", "--true-geometry", "-l", "-p", "-G")
        row = [ln for ln in out.splitlines() if ln.endswith("fwlist")]
        self.assertEqual(len(row), 1, out)
        fields = row[0].split()
        self.assertEqual(int(fields[0], 16), vid)
        self.assertEqual(int(fields[2]), info["pid"])
        self.assertEqual([int(f) for f in fields[3:7]], geo)
        # `--true-geometry` for the rectangle, because this foot is a NATIVE window and the plain `-G`
        # column is wmctrl's own arithmetic: the only original that can see a native window reads it
        # through xw11, whose shadows are children of the root, so the origin comes back doubled
        # (`wwmctl.core.Core._geometry_column`).  Both are asserted off the same view.
        doubled = self.ok("wwmctl", "-l", "-p", "-G")
        drow = [ln for ln in doubled.splitlines() if ln.endswith("fwlist")]
        self.assertEqual([int(f) for f in drow[0].split()[3:7]],
                         [geo[0] * 2, geo[1] * 2, geo[2], geo[3]],
                         "the original's doubled origin, its untouched size")

    def xterm(self, title):
        """One real X client on Wayfire's Xwayland, whose view id comes back. Skips where there is none."""
        if not shutil.which("xterm"):
            self.skipTest("xterm is not installed")
        if not self.rig.display:
            self.skipTest("this Wayfire started no Xwayland")
        proc = subprocess.Popen(["xterm", "-T", title, "-e", "sh", "-c", "sleep 600"],
                                env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, proc)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for v in self.ipc("window-rules/list-views"):
                if v.get("title") == title and v.get("mapped"):
                    return v["id"]
            if proc.poll() is not None:
                self.skipTest("xterm exited before it mapped a window")
            time.sleep(0.2)
        self.skipTest("xterm never mapped a window on this Wayfire")

    def xprop(self, *argv):
        """The distribution's own xprop on the rig's Xwayland -- the oracle for the X plane, so that no
        assertion below reads an X window id out of the code that is being checked."""
        p = subprocess.run(["xprop", "-display", self.rig.display, *argv],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_13_the_x_id_and_the_wmctrl_class_pair_come_off_a_real_xwayland(self):
        """U23, live: the whole X half against a real X server, with `$DISPLAY` taken out of the tool's
        environment, so `stipc/get_xwayland_display` is the only thing that can find it. That is also the
        one reply shape the unit tests can only hand-write -- there is no recording of that method.

        The floor printed a synthetic id and `XTerm.XTerm` on this same session, because the wlr `app_id` is
        the WM_CLASS *class* and the instance exists only here [M recon2/wayfire.md §2.4]."""
        vid = self.xterm("fwxlive")
        self.assertEqual(self.ipc("stipc/get_xwayland_display").get("display"), self.rig.display)

        ids = [int(w, 16) for w in self.xprop("-root", "_NET_CLIENT_LIST").split("#", 1)[1].split(",")]
        wanted = [i for i in ids
                  if '"xterm", "XTerm"' in self.xprop("-id", str(i), "WM_CLASS")
                  and "fwxlive" in self.xprop("-id", str(i), "WM_NAME")]
        self.assertEqual(len(wanted), 1, "xprop saw %r" % (ids,))

        env = self.env()
        env.pop("DISPLAY", None)      # only the IPC can say where Xwayland is
        p = subprocess.run([sys.executable, "-m", "wwmctl", "-l", "-x"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        row = [ln for ln in p.stdout.splitlines() if ln.endswith("fwxlive")]
        self.assertEqual(len(row), 1, p.stdout)
        fields = row[0].split(None, 3)
        self.assertEqual(int(fields[0], 16), wanted[0])
        self.assertEqual(fields[2], "xterm.XTerm")
        # and the view id the native listing prints is Wayfire's own, not the X one
        self.assertNotEqual(wanted[0], vid)

        # wwmctl re-reads WM_CLASS off X for every window that has an X id (`core._enrich_one`), so the row
        # above pins the id and the plumbing but not the backend's own pair. Ask the backend itself, in the
        # same environment: Wayfire's `app-id` for an X client is the WM_CLASS *class* alone, so an instance
        # of `xterm` can only have come off the X server this backend found through stipc.
        code = ("import json, sys;"
                "sys.path.insert(0, %r);"
                "from wdotool import backend_detect;"
                "rows = [v for v in backend_detect.detect().views() if v.window.title == 'fwxlive'];"
                "print(json.dumps([[v.xid, v.instance, v.cls, v.app_id, v.client_type] for v in rows]))"
                % ROOT)
        q = subprocess.run([sys.executable, "-c", code], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(q.returncode, 0, q.stderr)
        self.assertEqual(json.loads(q.stdout), [[wanted[0], "xterm", "XTerm", "", "x11"]])

    # -- desktops -----------------------------------------------------------

    def test_20_set_desktop_walks_the_viewport_grid(self):
        """Wayfire's desktops are a 3x3 viewport grid per output, flattened `y * grid_width + x`; the floor
        refused every desktop command [M recon2/wayfire.md §1.2, §2.3]."""
        self.addCleanup(self.ok, "wdotool", "set_desktop", "0")
        self.assertEqual(self.ok("wdotool", "get_num_desktops").strip(), "9")
        for n, cell in ((4, (1, 1)), (2, (2, 0)), (6, (0, 2)), (0, (0, 0))):
            self.ok("wdotool", "set_desktop", str(n))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ws = self.ipc("window-rules/get-focused-output")["info"]["workspace"]
                if (ws["x"], ws["y"]) == cell:
                    break
                time.sleep(0.1)
            self.assertEqual((ws["x"], ws["y"]), cell, "set_desktop %d" % n)
            self.assertEqual(self.ok("wdotool", "get_desktop").strip(), str(n))

    def test_21_a_window_reports_the_viewport_it_is_on(self):
        """Both halves are pinned against Wayfire, not against each other: a transposed `n % gw` / `n // gw`
        in `set_window_desktop` *and* in `_viewport()` would agree with itself and land the view on cell
        (2, 1) instead. The head is 1280x720 and the current viewport stays (0, 0), so a view sent to (1, 1)
        reads back one screen right and one screen down -- geometry is relative to the viewport in front."""
        vid = self.foot("fwdesk")
        self.addCleanup(self.ok, "wdotool", "set_desktop", "0")
        self.ok("wdotool", "set_desktop_for_window", str(vid), "4")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.ok("wdotool", "get_desktop_for_window", str(vid)).strip() == "4":
                break
            time.sleep(0.1)
        self.assertEqual(self.ok("wdotool", "get_desktop_for_window", str(vid)).strip(), "4")
        geo = self.view(vid)["geometry"]
        self.assertTrue(1280 <= geo["x"] < 2560 and 720 <= geo["y"] < 1440,
                        "view %d is at %r, not on viewport (1, 1) of a 1280x720 head" % (vid, geo))

    # -- the pointer, with no /dev/uinput -----------------------------------

    def test_30_mousemove_is_zero_pixels_out_against_the_compositor(self):
        """`wdotool getmouselocation` before any move is the thing no wlroots backend of ours could answer:
        the floor had to fall back to the daemon's own model of where it last put the pointer."""
        self.ok("wdotool", "mousemove", "300", "200")
        pos = self.ipc("window-rules/get_cursor_position")["pos"]
        self.assertEqual((pos["x"], pos["y"]), (300.0, 200.0))
        out = self.ok("wdotool", "getmouselocation")
        self.assertRegex(out, r"^x:300 y:200 ")

    def test_31_the_pointer_is_read_from_wayfire_and_not_remembered(self):
        """Move the cursor behind wdotool's back, through Wayfire's own stipc: a backend that answered from
        the daemon's model would still say 300,200."""
        self.ok("wdotool", "mousemove", "300", "200")
        self.ipc("stipc/move_cursor", x=640, y=480)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.ipc("window-rules/get_cursor_position")["pos"]["x"] == 640.0:
                break
            time.sleep(0.1)
        self.assertRegex(self.ok("wdotool", "getmouselocation"), r"^x:640 y:480 ")

    # -- displays: a second head with no QEMU under it ----------------------

    def test_40_wxrandr_stays_on_the_wlr_backend(self):
        """Wayfire's outputs really are `zwlr_output_manager_v1` v4, every apply confirmed against Wayfire's
        own `list-outputs`, so wxrandr gets no Wayfire backend and `--print-backend` says so
        [M recon2/wayfire.md §2.1, §3.4]."""
        self.assertEqual(self.ok("wxrandr", "--print-backend").strip(), "wlr")

    def test_41_a_second_head_appears_and_can_be_moved(self):
        reply = self.ipc("wayfire/create-headless-output", width=1920, height=1080)
        self.assertEqual(reply.get("result"), "ok", reply)
        name = reply["output"]["name"]
        self.addCleanup(self.ipc, "wayfire/destroy-headless-output", output=name)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(self.ipc("window-rules/list-outputs")) < 2:
            time.sleep(0.2)
        query = self.ok("wxrandr", "--query")
        self.assertIn("current 3200 x 1080", query)
        self.assertIn("%s connected 1920x1080+1280+0" % name, query)
        self.ok("wxrandr", "--output", name, "--below", "HEADLESS-1")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            heads = {o["name"]: o["geometry"] for o in self.ipc("window-rules/list-outputs")}
            if heads.get(name, {}).get("y") == 720:
                break
            time.sleep(0.2)
        # read back through Wayfire's own state, not through the protocol wxrandr just wrote
        self.assertEqual((heads[name]["x"], heads[name]["y"]), (0, 720))
        self.assertIn("%s connected 1920x1080+0+720" % name, self.ok("wxrandr", "--query"))
        self.assertEqual(self.ok("wdotool", "getdisplaygeometry").strip(), "1920 1800")

    # -- the keyboard layout ------------------------------------------------

    def test_50_the_layout_group_is_known_and_not_guessed(self):
        """`WayfireLayouts` (wdotool/xkbmap.py, batch 10) is what makes this pass.

        Wayfire sends no `wl_keyboard.modifiers` before focus, so with `xkb_layout = us,de` the group is
        inferred -- `group= 1 known= False` [M recon2/wayfire.md §2.7]. Harmless on the virtual-keyboard path
        (wdotool uploads its own keymap) and wrong on the uinput one, which is the compositor's keymap.
        `wayfire/get-keyboard-state` answers outright: `layout-index` 0 here, so group 1, known -- and
        still with no modifiers event, which is the point: the answer comes from the desktop, not the wire."""
        state = self.ipc("wayfire/get-keyboard-state")
        self.assertEqual(state["possible-layouts"], ["English (US)", "German"])
        code = ("import json, sys;"
                "sys.path.insert(0, %r);"
                "from wdotool import xkbmap;"
                "s = xkbmap.fetch();"
                "print(json.dumps([s.group, s.group_known, s.mods_seen]))" % ROOT)
        p = subprocess.run([sys.executable, "-c", code], env=self.env(),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), [1, True, False])


if __name__ == "__main__":
    unittest.main()
