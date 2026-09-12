#!/usr/bin/env python3
"""The oracle, through the proxy: stage 1's gate, and the regression test for
every stage after it.

`scripts/parity-oracle.sh` runs the two byte-parity files against the pinned
xdotool 4.20260303.1 and the nix wmctrl 1.07. With `W11_PARITY_PROXY=1` it puts
`python3 -m xw11` between them and the display first. The claim is the whole of
stage 1: **the two transcripts differ only in how long the runs took.**

recon/env.md 7 measured exactly this shape with a 61-line forwarder -- 139+139
wmctrl tests and the xdotool parity file, byte-identical except two `Ran N tests
in X.Xs` lines. What this file adds is the proxy that has framing, sockets,
auth, a lock and a lifetime, and the promise that it changed nothing.

Both runs share ONE Xvfb, so the display the script prints is the same string in
both transcripts and there is nothing to normalise but the timings and the one
line the proxy run prints about itself.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

from wdotool import cli, x11_mini                                   # noqa: E402
from xw11 import display as display_mod                             # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "parity-oracle.sh")
#: `Ran 139 tests in 1.594s` -- the only line a rerun of the same thing changes.
RAN = re.compile(r"^Ran (\d+) tests in [\d.]+s$")
#: The line the proxy run prints about itself, and nothing else prints.
THROUGH = "parity-oracle: through xw11 --passthrough on DISPLAY="


def oracle_prefix():
    """The same search `scripts/parity-oracle.sh` does, so that this file skips
    for exactly the reasons the script would die for -- and never turns a real
    failure into a skip."""
    got = os.environ.get("W11_ORACLE_PATH")
    if got:
        return got
    for name in (os.environ.get("W11_ORACLE_PATH_FILE"),
                 os.path.join(ROOT, "scripts", "nixpath")):
        if name and os.path.isfile(name):
            with open(name, encoding="utf-8") as f:
                return f.read().strip()
    import glob
    parts = []
    for pattern, exe in (("/nix/store/*-xdotool-%s/bin" % cli.XDO_VERSION, "xdotool"),
                         ("/nix/store/*-wmctrl-1.07/bin", "wmctrl")):
        for d in sorted(glob.glob(pattern)):
            if os.access(os.path.join(d, exe), os.X_OK):
                parts.append(d)
                break
    return ":".join(parts)


def why_not():
    """The reason this box cannot run the oracle, or None."""
    prefix = oracle_prefix()
    if not prefix:
        return "no oracle path: set $W11_ORACLE_PATH, or build them with nix"
    path = prefix + ":" + os.environ.get("PATH", "")
    xdo = shutil.which("xdotool", path=path)
    wmc = shutil.which("wmctrl", path=path)
    if not xdo or not wmc:
        return "no xdotool/wmctrl on PATH after adding %s" % prefix
    got = subprocess.run([xdo, "version"], capture_output=True, text=True,
                         timeout=60).stdout.split()
    if not got or got[-1] != cli.XDO_VERSION:
        return "xdotool on PATH is %s, not the pinned %s" % (got and got[-1],
                                                             cli.XDO_VERSION)
    helped = subprocess.run([wmc, "--help"], capture_output=True, timeout=60).stdout
    if len(helped) != 6801:
        return "wmctrl --help is %d bytes, the nix 1.07 oracle is 6801" % len(helped)
    stripped = ":".join(p for p in os.environ.get("PATH", "").split(":")
                        if p not in prefix.split(":"))
    if not shutil.which("wmctrl", path=stripped):
        return "no second wmctrl outside %s for the distro generation" % prefix
    if not shutil.which("Xvfb"):
        return "no Xvfb"
    return None


SKIP = why_not()


def normalise(text):
    """The transcript with the two things a rerun is allowed to change taken
    out: the elapsed time of each unittest run, and the line the proxy run
    prints to say it is the proxy run."""
    out = []
    for line in text.splitlines():
        if line.startswith(THROUGH):
            continue
        out.append(RAN.sub(r"Ran \1 tests in <time>", line))
    return "\n".join(out)


class OracleThroughProxy(unittest.TestCase):
    """One Xvfb, two runs of the same script."""

    @unittest.skipIf(SKIP, SKIP or "")
    def test_the_two_transcripts_differ_only_in_the_timing_lines(self):
        self.maxDiff = None
        upstream = display_mod.allocate()
        num = upstream.num
        upstream.release()
        xvfb = subprocess.Popen(
            ["Xvfb", ":%d" % num, "-screen", "0", "1280x720x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(_reap, xvfb)
        self.assertTrue(_wait_for_display(num), "Xvfb never came up")

        proxy_display = display_mod.allocate()
        proxy_num = proxy_display.num
        proxy_display.release()          # the proxy the script starts takes it

        env = dict(os.environ, DISPLAY=":%d" % num,
                   W11_ORACLE_PATH=oracle_prefix(), W11_PASSTHROUGH="never")
        direct = subprocess.run(["sh", SCRIPT], env=env, cwd=ROOT, timeout=900,
                                capture_output=True, text=True)
        self.assertEqual(direct.returncode, 0,
                         "the DIRECT run failed, so there is nothing to compare "
                         "the proxy against:\n%s\n%s" % (direct.stdout, direct.stderr))
        env["W11_PARITY_PROXY"] = "1"
        env["W11_PARITY_PROXY_DISPLAY"] = ":%d" % proxy_num
        through = subprocess.run(["sh", SCRIPT], env=env, cwd=ROOT, timeout=900,
                                 capture_output=True, text=True)
        self.assertEqual(through.returncode, 0,
                         "the PROXY run failed:\n%s\n%s"
                         % (through.stdout, through.stderr))

        self.assertIn("%s:%d" % (THROUGH, proxy_num), through.stdout)
        self.assertIn("parity-oracle: all oracles ran", direct.stdout)
        self.assertIn("parity-oracle: all oracles ran", through.stdout)
        self.assertEqual(normalise(direct.stdout), normalise(through.stdout))
        self.assertEqual(normalise(direct.stderr), normalise(through.stderr))

        # ...and the proxy took itself away with it.
        self.assertFalse(os.path.exists(display_mod.sock_path(proxy_num)))
        self.assertFalse(os.path.exists(display_mod.lock_path(proxy_num)))

    def test_the_normaliser_keeps_a_real_difference(self):
        """The negative twin for `normalise`: it may take out the clock and the
        proxy's own line, and nothing else. A normaliser that flattened the
        counts would let a run that lost half its tests through."""
        a = "Ran 139 tests in 2.066s\nok"
        b = "Ran 139 tests in 1.571s\nok"
        self.assertEqual(normalise(a), normalise(b))
        self.assertNotEqual(normalise(a), normalise("Ran 39 tests in 2.066s\nok"))
        self.assertNotEqual(normalise(a), normalise("Ran 139 tests in 2.066s\nFAILED"))
        self.assertEqual(normalise(THROUGH + ":98\nok"), "ok")

    def test_the_script_documents_the_variable_it_gained(self):
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("W11_PARITY_PROXY=1", text)
        self.assertIn("W11_PARITY_PROXY_DISPLAY", text)
        self.assertIn("python3 -m xw11", text)
        # the proxy is killed by the trap the Xvfb already had
        self.assertIn('[ -n "$proxy_pid" ] && kill "$proxy_pid"', text)


def _reap(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:      # pragma: no cover - a wedged Xvfb
            proc.kill()
            proc.wait(timeout=5)


def _wait_for_display(num, timeout=20.0):
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        try:
            s.connect("\0%s/X%d" % (x11_mini._SOCK_DIR, num))
            return True
        except OSError:
            pass
        finally:
            s.close()
        time.sleep(0.1)
    return False



# -- the native half: the originals through the proxy vs the clones -----------
#
# `OracleThroughProxy` above is the framing's gate and runs on an Xvfb with the
# proxy in pass-through. This half is the other direction: a REAL headless sway
# with one native `foot` toplevel, the pinned xdotool 4.20260303.1 and wmctrl
# 1.07 pointed at the proxy, and `wdotool`/`wwmctl`/`wxprop` pointed at the same
# compositor -- two completely different routes to the same eleven answers
# (design section 9.3, recon/tools.md 11 rows 1-7, 32-36, 47-50).
#
# Ids are tokenised because the two routes CANNOT agree on them and must not:
# the clone prints sway's own con id and the proxy prints `rid_base | n` out of
# the range the X server allocated it [recon/wire.md 7.1]. Everything else --
# the title, the WM_CLASS pair, the pid, the rect, the desktop, the property
# types -- is the same fact read twice, and a difference is a bug in one of
# them.

import shlex                                                        # noqa: E402

from support import HeadlessSway                                    # noqa: E402

FOOT_TITLE = "WXL-Foot"
FOOT_APP_ID = "footw"


def _native_skip():
    """Why this box cannot run the native half, or None."""
    if not (shutil.which("sway") and shutil.which("Xwayland")):
        return "needs sway and Xwayland"
    if not shutil.which("foot"):
        return "needs foot (the native toplevel)"
    prefix = oracle_prefix()
    if not prefix:
        return "no oracle path: set $W11_ORACLE_PATH, or build them with nix"
    path = prefix + ":" + os.environ.get("PATH", "")
    if not shutil.which("xdotool", path=path) or not shutil.which("wmctrl", path=path):
        return "no pinned xdotool/wmctrl after adding %s" % prefix
    return None


NATIVE_SKIP = _native_skip()


@unittest.skipIf(NATIVE_SKIP, NATIVE_SKIP or "")
class NativeParity(unittest.TestCase):
    """One sway, one foot, one proxy; the original and the clone, side by side."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.rig = HeadlessSway("xw11-parity-")
        try:
            cls.swaymsg("exec foot --app-id %s --title %s sh -c 'sleep 600'"
                        % (FOOT_APP_ID, FOOT_TITLE))
            if not cls.wait(lambda: cls.foot_node() is not None):
                raise unittest.SkipTest("foot window never appeared")
            cls.start_proxy()
            cls.oracle_path = oracle_prefix() + ":" + os.environ.get("PATH", "")
            cls.con_id = cls.foot_node()["id"]
            cls.shadow = cls.find_shadow()
        except BaseException:
            cls.stop_proxy()
            cls.rig.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.stop_proxy()
        cls.rig.stop()

    # -- the rig --------------------------------------------------------------

    @classmethod
    def swaymsg(cls, cmd, kind=None):
        argv = ["swaymsg", "-s", cls.rig.sock]
        if kind:
            argv += ["-t", kind]
        argv.append(cmd)
        return subprocess.run(argv, env=cls.rig.env, capture_output=True,
                              text=True, timeout=20)

    @classmethod
    def foot_node(cls):
        tree = json.loads(cls.swaymsg("", kind="get_tree").stdout)
        found = []

        def walk(node):
            if node.get("app_id") == FOOT_APP_ID:
                found.append(node)
            for kid in (node.get("nodes") or []) + (node.get("floating_nodes") or []):
                walk(kid)

        walk(tree)
        return found[0] if found else None

    @classmethod
    def wait(cls, ready, timeout=30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready():
                return True
            time.sleep(0.2)
        return False

    @classmethod
    def start_proxy(cls):
        cls.log_path = os.path.join(cls.rig.rtdir, "xw11.log")
        env = dict(cls.rig.env, XW11_LOG=cls.log_path, W11_PASSTHROUGH="never",
                   LC_ALL="C")
        env.pop("XW11_DISPLAY", None)
        cls.proxy = subprocess.Popen(
            [sys.executable, "-m", "xw11", "--foreground",
             "--upstream", cls.rig.display],
            env=env, cwd=ROOT, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        path = os.path.join(cls.rig.rtdir, "xw11", "display")
        if not cls.wait(lambda: os.path.exists(path), timeout=30):
            raise unittest.SkipTest("the proxy never announced a display")
        with open(path, encoding="utf-8") as f:
            cls.proxy_display = f.read().split()[0]

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
    def find_shadow(cls):
        got = subprocess.run(
            ["xdotool", "search", "--class", FOOT_APP_ID],
            env=dict(cls.rig.env, DISPLAY=cls.proxy_display,
                     PATH=oracle_prefix() + ":" + os.environ.get("PATH", ""),
                     W11_PASSTHROUGH="never", LC_ALL="C"),
            capture_output=True, text=True, timeout=60)
        ids = got.stdout.split()
        if len(ids) != 1:
            raise unittest.SkipTest("the proxy did not shadow the foot: %r"
                                    % got.stdout)
        return int(ids[0])

    # -- the two routes -------------------------------------------------------

    def original(self, argv):
        """The pinned binary, pointed at the proxy."""
        env = dict(self.rig.env, DISPLAY=self.proxy_display,
                   PATH=self.oracle_path, W11_PASSTHROUGH="never", LC_ALL="C")
        return subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=90)

    def clone(self, module, argv, **extra):
        """The clone, pointed at the same compositor. `WDOTOOL_BACKEND=sway` and
        the rig's `SWAYSOCK` are what keep it off the detection path a second
        compositor on this box would confuse."""
        env = dict(self.rig.env, WDOTOOL_BACKEND="sway",
                   W11_PASSTHROUGH="never", LC_ALL="C")
        env.update(extra)
        return subprocess.run([sys.executable, "-m", module, *argv], env=env,
                              capture_output=True, text=True, timeout=90,
                              cwd=ROOT)

    def tokenise(self, text):
        """Every form the two routes print an id in, replaced by one token:
        decimal (xdotool), `0x%08x` (wmctrl) and `0x%x` (xprop)."""
        for wid in (self.shadow, self.con_id):
            for form in ("0x%08x" % wid, "0x%x" % wid, "%d" % wid):
                text = text.replace(form, "<WIN>")
        return text

    def parity(self, argv, module, clone_argv=None, note=""):
        """The original through the proxy and the clone side by side, ids
        tokenised. Returns both, so a caller can pin what is left over."""
        got = self.original(argv)
        mine = self.clone(module, clone_argv or argv[1:])
        self.assertEqual(got.returncode, mine.returncode,
                         "%s: rc %d original, %d clone\n%s\n%s%s"
                         % (shlex.join(argv), got.returncode, mine.returncode,
                            got.stderr, mine.stderr, note))
        return got, mine

    def assert_same(self, argv, module, clone_argv=None):
        got, mine = self.parity(argv, module, clone_argv)
        self.assertEqual(self.tokenise(got.stdout), self.tokenise(mine.stdout),
                         shlex.join(argv))
        return got, mine

    # -- rows 1-7: xdotool ----------------------------------------------------

    def test_search_finds_one_window_by_class_and_by_name(self):
        """recon/tools.md 11 rows 1 and 2. The proxy answers a tree walk and
        the clone answers `list()`; the two must name the same one window."""
        for argv in (["xdotool", "search", "--class", FOOT_APP_ID],
                     ["xdotool", "search", "--name", FOOT_TITLE]):
            got, mine = self.assert_same(argv, "wdotool")
            self.assertEqual(got.stdout.split(), [str(self.shadow)])
            self.assertEqual(mine.stdout.split(), [str(self.con_id)])

    def test_getactivewindow_and_the_identity_reads_agree(self):
        """Rows 3, 4, 5 and 7."""
        self.swaymsg("[app_id=%s] focus" % FOOT_APP_ID)
        self.assertTrue(self.wait(
            lambda: self.original(["xdotool", "getactivewindow"]).stdout.strip()
            == str(self.shadow), timeout=10), "the focus never settled")
        self.assert_same(["xdotool", "getactivewindow"], "wdotool")
        for cmd in ("getwindowname", "getwindowpid", "getwindowclassname"):
            got, mine = self.parity(["xdotool", cmd, str(self.shadow)], "wdotool",
                                    clone_argv=[cmd, str(self.con_id)])
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(got.stdout, mine.stdout, cmd)

    def test_getwindowgeometry_prints_the_same_rect(self):
        """Row 6. The clone reads sway's rect and the original reads
        `GetGeometry` on the shadow, which the proxy answers from the same
        rect -- at ratio 1.0, which R8 measured [M 2026-09-10]."""
        self.assert_same(["xdotool", "getwindowgeometry", str(self.shadow)],
                         "wdotool",
                         clone_argv=["getwindowgeometry", str(self.con_id)])

    def test_getdisplaygeometry_agrees(self):
        """Not a shadow read at all: Xinerama passes straight through
        [recon/tools.md 4.2], and the clone reads `display_size()`. They are
        the same two numbers or one of the two is wrong."""
        self.assert_same(["xdotool", "getdisplaygeometry"], "wdotool")

    # -- rows 32-36: wmctrl ---------------------------------------------------

    def test_the_four_listing_forms_print_the_same_rows(self):
        """Rows 32-35. wmctrl sends a byte-identical request stream for all
        four and differs only in what it PRINTS [recon/tools.md 5]."""
        for flag in ("-l", "-lx", "-lp", "-lG"):
            got, mine = self.parity(["wmctrl", flag], "wwmctl")
            self.assertEqual(got.returncode, 0, got.stderr)
            self.assertEqual(self.tokenise(got.stdout),
                             self.tokenise(mine.stdout), flag)

    def test_wmctrl_d_agrees_except_in_the_workarea_column(self):
        """Row 36, measured.

        `DG:` is `display_size()` on both sides -- the clone prints it from
        `wwmctl/core.py:780` and the proxy publishes `_NET_DESKTOP_GEOMETRY`
        from the same call, which is why that name is in the root set at all.

        * `VP:` AGREES, and the agreement is a property: the proxy publishes
          `_NET_DESKTOP_VIEWPORT` as one `0,0` pair per desktop
          (xw11/shadow.py:root_props) so real wmctrl's `[2i]` indexing prints
          `0,0` on every row, and `wwmctl/core.py:803` prints `0,0` on every
          row when there is nothing to read -- both are what an X WM with
          viewports prints [M 2026-09-11, this box, headless sway with two
          workspaces; before the property existed the original printed
          `VP: N/A` on every row against the clone's `0,0` on the current one].
        * `WA:` is the one column that still differs from what X would print
          on both routes at once: both print `N/A`, because nobody publishes
          `_NET_WORKAREA` and `wwmctl/core.py:800` has no workarea to print.
          Not yet: one more root property off the usable area the backend can
          name (rung 2 on sway, `get_workspaces`/`get_outputs` over the IPC
          socket the registry already holds, minus the layer-shell exclusive
          zones). A gap in our work, not a policy.
        The NAME column AGREES, and measured rather than assumed: the proxy
        publishes `_NET_DESKTOP_NAMES` wherever the backend has a
        `workspaces()`, and `backend_sway.py:441` has one -- both routes print
        sway's workspace name (`1`) [M 2026-09-10, this rig].
        """
        got, mine = self.parity(["wmctrl", "-d"], "wwmctl")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(mine.returncode, 0, mine.stderr)
        original = got.stdout.splitlines()
        clone = mine.stdout.splitlines()
        self.assertEqual(len(original), len(clone), "%r vs %r" % (original, clone))
        size = json.loads(self.swaymsg("", kind="get_outputs").stdout)[0]["rect"]
        for line in original:
            self.assertIn("DG: %dx%d" % (size["width"], size["height"]), line)
            self.assertIn("VP: 0,0", line)
            self.assertIn("WA: N/A", line)
        for line in clone:
            self.assertIn("DG: %dx%d" % (size["width"], size["height"]), line)
            self.assertIn("VP: 0,0", line)
        self.assertEqual(original[0].split()[:2], clone[0].split()[:2])
        spaces = json.loads(self.swaymsg("", kind="get_workspaces").stdout)
        self.assertEqual(len(original), len(spaces), "one row per workspace")
        for row, mine_row, ws in zip(original, clone, spaces):
            self.assertEqual(row.split()[-1], ws["name"], row)
            self.assertEqual(row.split()[-1], mine_row.split()[-1])

    # -- rows 47-50: xprop ----------------------------------------------------

    def test_the_client_list_names_one_window_on_both_routes(self):
        """Row 48. Both print one id and the ids are the two routes' own."""
        got = self.original(["xprop", "-root", "_NET_CLIENT_LIST"])
        mine = self.clone("wxprop", ["-root", "_NET_CLIENT_LIST"],
                          WXPROP_NO_X="1")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(mine.returncode, 0, mine.stderr)
        self.assertEqual(self.tokenise(got.stdout.lower()),
                         self.tokenise(mine.stdout.lower()))

    def test_xprop_id_agrees_on_every_name_both_synthesize(self):
        """Row 49, restricted the way design section 9.3 restricts it: the
        proxy's set adds three names the clone does not have (`WM_STATE` on a
        non-`views()` backend, `_NET_FRAME_EXTENTS`, `WM_PROTOCOLS`), so the
        comparison is over the intersection -- and the intersection has to
        agree byte for byte, types and all."""
        got = self.original(["xprop", "-id", str(self.shadow)])
        mine = self.clone("wxprop", ["-id", str(self.con_id)], WXPROP_NO_X="1")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(mine.returncode, 0, mine.stderr)
        a = _named_lines(got.stdout)
        b = _named_lines(mine.stdout)
        shared = sorted(set(a) & set(b))
        self.assertTrue(shared, "the two routes share no property at all")
        for name in shared:
            self.assertEqual(self.tokenise(a[name]), self.tokenise(b[name]), name)
        self.assertEqual(sorted(set(a) - set(b)),
                         ["WM_PROTOCOLS", "WM_STATE", "_NET_FRAME_EXTENTS"])
        self.assertEqual(sorted(set(b) - set(a)), [],
                         "the clone synthesizes a name the proxy does not")

    def test_the_fullscreen_state_agrees_on_a_backend_with_no_views(self):
        """`_NET_WM_STATE_FULLSCREEN` is one of the two states design section
        4.4 does NOT bracket as rich, and sway is one of the two backends with
        no `views()` to read a state off at all.

        `wxprop` reads it off the tree node (`fullscreen_mode`,
        wxprop/core.py:544) and so does the registry, through the same
        `_nodes()` walk it pairs xids with. Measured before it did: the clone
        printed `_NET_WM_STATE(ATOM) = _NET_WM_STATE_FULLSCREEN` and the proxy
        printed `_NET_WM_STATE(ATOM) =` for the same fullscreened foot
        [M 2026-09-10, this rig]."""
        self.addCleanup(self.swaymsg,
                        "[app_id=%s] fullscreen disable" % FOOT_APP_ID)
        self.swaymsg("[app_id=%s] fullscreen enable" % FOOT_APP_ID)
        self.assertTrue(self.wait(
            lambda: (self.foot_node() or {}).get("fullscreen_mode"), timeout=10),
            "sway never fullscreened the foot")
        got = self.original(["xprop", "-id", str(self.shadow), "_NET_WM_STATE"])
        mine = self.clone("wxprop", ["-id", str(self.con_id), "_NET_WM_STATE"],
                          WXPROP_NO_X="1")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("_NET_WM_STATE_FULLSCREEN", got.stdout)
        self.assertEqual(got.stdout, mine.stdout)

    def test_the_proxy_and_the_clone_name_the_same_title_and_class(self):
        """The two facts every one of the eleven rows above is really about,
        read once through each route with no formatting in the way."""
        got = self.original(["xprop", "-id", str(self.shadow), "WM_CLASS",
                             "_NET_WM_NAME"])
        mine = self.clone("wxprop", ["-id", str(self.con_id), "WM_CLASS",
                                     "_NET_WM_NAME"], WXPROP_NO_X="1")
        self.assertEqual(got.stdout, mine.stdout)
        self.assertIn('WM_CLASS(STRING) = "%s", "%s"' % (FOOT_APP_ID, FOOT_APP_ID),
                      got.stdout)


def _named_lines(text):
    """`{NAME: line}` for the `NAME(TYPE) = value` lines of an xprop dump.
    A multi-line value (`WM_STATE`'s) keeps its continuation lines."""
    out = {}
    name = None
    for line in text.splitlines():
        got = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(", line)
        if got:
            name = got.group(1)
            out[name] = line
        elif name is not None:
            out[name] += "\n" + line
    return out


if __name__ == "__main__":
    unittest.main()
