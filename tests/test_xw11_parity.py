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
THROUGH = "parity-oracle: through xw11 on DISPLAY="


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


if __name__ == "__main__":
    unittest.main()
