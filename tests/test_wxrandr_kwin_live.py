#!/usr/bin/env python3
"""wxrandr against a REAL KWin: Plasma 6.6 (KWin 6.6.6) on the QEMU rig.

Everything here was measured by hand on `resolute-kde` on 2026-09-08 (package
route, nothing else installed) and is asserted against the compositor's own
tool, `kscreen-doctor -o`, never against another wxrandr answer: the discovery
path (`kde_output_device_v2` + `kde_output_device_registry_v2`), one apply that
has to land edge to edge, the mirror that shows up as a `replicationSource` in
`~/.config/kwinoutputconfig.json`, and F4.1's live twin -- two Xwayland windows
with the SAME title, where `wwmctl -i -r <xid> -e` has to move the one whose X
id it was given and not the first match by name.

Opt-in twice over, because it drives a virtual machine:

    VMCTL_LIVE=1 WXRANDR_LIVE_KWIN=1 python3 tests/test_wxrandr_kwin_live.py

and the instance has to be up already (`vm/vmctl start kde66`, or
`--flavor resolute-kde --heads 2 --fresh` for a new one; name it in
WXRANDR_LIVE_KWIN_VM, default `kde66`).  Nothing here starts or stops a VM:
this host caps the guest at 20 GiB and runs one VM at a time, so who is
running is the caller's decision, not a test's.  The whole-desktop version of
these steps is `vm/live-smoke.sh resolute-kde` (the `kwin` phase).

Plasma 6.7 (`stonking-kde`) is the follow-on T67 names: KWin 6.7 stopped
advertising `kde_output_device_v2` as a plain global and hands the devices out
through `kde_output_device_registry_v2` instead (registry version 23, `removed`
opcode 36), which is the one release where there is no other way to see the
outputs at all.  That golden is not built on this host, so that case skips on a
CONDITION -- the golden file plus an instance of it -- and turns itself on the
day `vm/vmctl build stonking-kde` runs, rather than being an unconditional skip
with an empty body that could never fail or ever come back."""

import json
import math
import os
import re
import subprocess
import sys
import time
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

VMCTL = os.path.join(ROOT, "vm", "vmctl")
INSTANCE = os.environ.get("WXRANDR_LIVE_KWIN_VM", "kde66")
LIVE = bool(os.environ.get("VMCTL_LIVE")) and bool(os.environ.get("WXRANDR_LIVE_KWIN"))
WHY = ("set VMCTL_LIVE=1 WXRANDR_LIVE_KWIN=1 and start the Plasma instance first "
       "(vm/vmctl start %s --flavor resolute-kde --heads 2)" % INSTANCE)

# T67, the Plasma 6.7 half: it needs the `stonking-kde` golden, which is not
# built on this host (the brief forbids building one -- the disk was at 93%),
# and an instance of it.  The condition is the golden on disk plus
# WXRANDR_LIVE_KWIN_VM pointing at an instance of that flavor, so the case
# stops being a permanent skip the day `vm/vmctl build stonking-kde` runs
# instead of having to be edited then.
GOLDEN67 = os.path.join(os.environ.get("VMDATA") or os.path.expanduser("~/vm-data"),
                        "golden", "stonking-kde.qcow2")
PLASMA67 = LIVE and os.path.exists(GOLDEN67) and "67" in INSTANCE
WHY67 = ("Plasma 6.7 needs the stonking-kde golden (%s: %s) and WXRANDR_LIVE_KWIN_VM naming a 6.7 instance "
         "(got %r): vm/vmctl build stonking-kde, ~7 min, then vm/vmctl start kde67 --flavor stonking-kde"
         % (GOLDEN67, "present" if os.path.exists(GOLDEN67) else "not built", INSTANCE))


def vmctl(*args, timeout=120):
    """One vmctl call, output captured, never raising: a rig that is not there
    is a skip, and a guest command that fails is usually the assertion."""
    p = subprocess.run([sys.executable, VMCTL] + list(args),
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def user(cmd, timeout=120):
    """`cmd` as the seated user, with the session environment vmctl exports."""
    return vmctl("user", INSTANCE, "--", "sh", "-c", cmd, timeout=timeout)


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def kscreen_outputs(text):
    """`kscreen-doctor -o` -> {name: {"pos": (x, y), "size": (w, h), "scale": float,
    "mode": (w, h)}} for the ENABLED outputs.

    Plasma 6 prints a block per output; the state words are on the indented
    lines under `Output: <id> <name>` and the geometry is `Geometry: x,y WxH`
    in LOGICAL pixels.  The output is ANSI-coloured even through a pipe, which
    is why the escapes come off first."""
    text = ANSI.sub("", text)
    out = {}
    for chunk in re.split(r"(?m)^Output: ", text)[1:]:
        m = re.match(r"\d+\s+(\S+)", chunk)
        if not m or not re.search(r"\benabled\b", chunk) or re.search(r"\bdisabled\b", chunk):
            continue
        geo = re.search(r"Geometry:\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)", chunk)
        sca = re.search(r"Scale:\s*([\d.]+)", chunk)
        mode = re.search(r"(?m)^\s*Modes:.*?\*(\d+)x(\d+)", chunk) or \
            re.search(r"(\d+)x(\d+)@\d+\*", chunk)
        if not geo:
            continue
        out[m.group(1)] = {
            "pos": (int(geo.group(1)), int(geo.group(2))),
            "size": (int(geo.group(3)), int(geo.group(4))),
            "scale": float(sca.group(1)) if sca else 1.0,
            "mode": (int(mode.group(1)), int(mode.group(2))) if mode else None,
        }
    return out


def wxrandr_outputs(text):
    """`wxrandr --query` -> {name: (x, y, w, h)} for the connected+enabled ones.

    The header line is xrandr's: `<name> connected [primary] WxH+X+Y (normal
    left inverted ...) 520mm x 290mm`, and an output that is connected but off
    has no geometry word at all."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 3 and f[1] == "connected":
            for w in f[2:]:
                m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", w)
                if m:
                    out[f[0]] = (int(m.group(3)), int(m.group(4)), int(m.group(1)), int(m.group(2)))
                    break
    return out


CAPTURE = os.path.join(ROOT, "tests", "fixtures", "live", "noble-gnome-46.0-capture.txt")


def capture_section(path, command):
    """The output bytes recorded under `### <command>` in one of the
    tests/fixtures/live captures, or None."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    lines, out, taking = text.splitlines(True), [], False
    for line in lines:
        if line.startswith("### rc=") and taking:
            return "".join(out)
        if line.startswith("### "):
            taking = line[4:].strip() == command
            out = []
        elif taking:
            out.append(line)
    return None


class QueryParserHelperTest(unittest.TestCase):
    """A guard on THIS FILE's own oracle, and not coverage of wxrandr.

    No product code runs in these two: `wxrandr_outputs()` is the parser every
    parity assertion in KwinLiveTest reads the tool's answer with, so a bug in
    it would make those assertions compare the wrong numbers -- which is worth
    a check against bytes a real tool produced, and needs no VM.  It proves the
    parser, nothing more; the wxrandr claims of T60 are the live cases above,
    and a run that skipped them has measured nothing about wxrandr however
    green these two are.

    The bytes are the `wxrandr --query` section of
    tests/fixtures/live/noble-gnome-46.0-capture.txt (three 1920x1080 heads side
    by side on GNOME 46.0, Virtual-1 primary).

    The kscreen-doctor parser has no such recording yet: `kscreen-doctor -o` off
    Plasma 6.6 belongs in tests/fixtures/kscreen/ (T61) and until it is there
    that half is exercised live only, where the compositor itself is the
    second opinion."""

    def test_the_query_parser_reads_the_recorded_gnome46_layout(self):
        text = capture_section(CAPTURE, "wxrandr --query")
        if text is None:
            self.skipTest("no wxrandr --query section in %s" % CAPTURE)
        self.assertEqual(
            wxrandr_outputs(text),
            {"Virtual-1": (0, 0, 1920, 1080),
             "Virtual-2": (1920, 0, 1920, 1080),
             "Virtual-3": (3840, 0, 1920, 1080)})

    def test_the_query_parser_ignores_the_mode_table_under_each_output(self):
        """Every output is followed by twenty indented `1920x1080  75.00*+`
        lines; a parser that read those as geometry would answer with modes.
        The header line is the only one with a `WxH+X+Y` word on it."""
        text = capture_section(CAPTURE, "wxrandr --query")
        if text is None:
            self.skipTest("no wxrandr --query section in %s" % CAPTURE)
        self.assertNotIn("5120x2160", str(wxrandr_outputs(text)))
        self.assertEqual(len([ln for ln in text.splitlines() if " connected" in ln]), 3)


@unittest.skipUnless(LIVE, WHY)
class KwinLiveTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        rc, out, err = vmctl("status", INSTANCE, timeout=60)
        if rc != 0 or "running" not in (out + err):
            raise unittest.SkipTest("instance %s is not running: %s" % (INSTANCE, WHY))
        rc, out, _ = user("kscreen-doctor -o", timeout=90)
        if rc != 0:
            raise unittest.SkipTest("kscreen-doctor does not answer in %s (no Plasma session?)" % INSTANCE)
        cls.start = kscreen_outputs(out)
        if not cls.start:
            raise unittest.SkipTest("no enabled output in %s" % INSTANCE)
        cls.names = sorted(cls.start)

    @classmethod
    def tearDownClass(cls):
        # Put back the layout the class found, whatever a test left behind:
        # KWin saves every apply immediately (there is no monitors.xml dance
        # here), so a test that dies half way through would otherwise leave the
        # next one -- and the next run -- with a layout nobody asked for.
        # `--pos` is enough to undo a mirror as well: kwin.py:863-866 says the
        # replication source is touched for every output an invocation
        # positions, and :998-1002 sends `repl = ""` for it, so naming every
        # output with a position clears any replicationSource a dying test left
        # in kwinoutputconfig.json.  (test_same_as does it itself and checks
        # that it took; this is the net under it.)
        parts = []
        for name in cls.names:
            x, y = cls.start[name]["pos"]
            parts.append("--output %s --pos %dx%d" % (name, x, y))
        user("wxrandr " + " ".join(parts), timeout=120)

    def kscreen(self):
        rc, out, err = user("kscreen-doctor -o", timeout=90)
        self.assertEqual(rc, 0, "kscreen-doctor failed: %s" % (err or out))
        return kscreen_outputs(out)

    def settle(self, seconds=2.5):
        """KWin applies a layout on its own next frame; nothing here polls the
        compositor faster than it can answer."""
        time.sleep(seconds)

    def test_backends_lists_kwin_as_available_on_a_plasma_session(self):
        """`--backends` is one line per backend with its availability, and on
        Plasma 6.6 the kwin line is the available one (measured: the tools reach
        KWin through kde_output_management_v2 with no bridge, no portal and no
        helper of any kind installed)."""
        rc, out, err = user("wxrandr --backends")
        self.assertEqual(rc, 0, err or out)
        line = [ln for ln in out.splitlines() if "kwin" in ln]
        self.assertTrue(line, "no kwin line in --backends:\n%s" % out)
        # `unavailable` contains `available`: the whitespace before it is what
        # separates the two, and the line is
        # `* kwin    available    kde_output_management_v2 version 4`.
        self.assertRegex(line[0], r"kwin\s+available",
                         "the kwin backend is not reported available on a Plasma session: %r" % line[0])

    def test_print_backend_verbose_names_the_output_device_protocol_and_version(self):
        """`--print-backend --verbose` has to name what it actually bound, not
        what it hoped for: the probe binds
        `kde_output_management_v2` (the write side of KWin's pair) and prints it
        with the version the compositor advertised -- `protocol:
        kde_output_management_v2 version 4` on 6.6, where the XML in the tree
        describes 25.  The read side, `kde_output_device_v2` and on 6.7 the
        registry that hands the devices out, is what the --query tests below
        exercise."""
        rc, out, err = user("wxrandr --print-backend --verbose")
        text = out + err
        self.assertEqual(rc, 0, text)
        self.assertIn("kwin", text)
        self.assertIn("kde_output_management_v2", text,
                      "--print-backend --verbose does not name the protocol it bound:\n%s" % text)
        self.assertRegex(text, r"kde_output_management_v2 version \d+",
                         "no advertised interface version in --print-backend --verbose:\n%s" % text)

    def test_query_names_the_same_outputs_and_positions_as_kscreen_doctor(self):
        """The parity check that makes every other one meaningful: wxrandr's
        `--query` header lines and kscreen-doctor's `Geometry:` have to agree
        on which outputs are on and where they are.  Positions are logical
        pixels on both sides."""
        rc, out, err = user("wxrandr --query")
        self.assertEqual(rc, 0, err or out)
        mine, theirs = wxrandr_outputs(out), self.kscreen()
        self.assertEqual(sorted(mine), sorted(theirs),
                         "wxrandr --query and kscreen-doctor disagree about which outputs are enabled")
        for name in theirs:
            self.assertEqual(mine[name][:2], theirs[name]["pos"],
                             "%s: wxrandr says %s, kscreen-doctor says %s"
                             % (name, mine[name][:2], theirs[name]["pos"]))

    def test_query_logical_size_is_the_enclosing_integer_at_a_fractional_scale(self):
        """At scale 1.4 a 1920x1080 mode is 1371.43x771.43 logical pixels and
        KWin publishes the ENCLOSING integer, 1372x772 -- not the truncation.
        wxrandr prints what the compositor published, so the two agree exactly;
        this asserts the number rather than the agreement, so that a backend
        that started computing the size itself is caught even if it computed it
        the same way twice."""
        name = self.names[0]
        rc, _, err = user("kscreen-doctor output.%s.scale.1.4" % name, timeout=90)
        if rc != 0:
            self.skipTest("kscreen-doctor refused scale 1.4 on %s: %s" % (name, err))
        try:
            self.settle(3.0)
            theirs = self.kscreen()[name]
            if abs(theirs["scale"] - 1.4) > 0.01:
                self.skipTest("KWin did not take scale 1.4 (it is at %s)" % theirs["scale"])
            rc, out, err = user("wxrandr --query")
            self.assertEqual(rc, 0, err or out)
            mine = wxrandr_outputs(out)[name]
            self.assertEqual(mine[2:], theirs["size"],
                             "%s at scale 1.4: wxrandr %sx%s, kscreen-doctor %s" % (name, mine[2], mine[3],
                                                                                    theirs["size"]))
            if theirs["mode"]:
                want = (math.ceil(theirs["mode"][0] / 1.4), math.ceil(theirs["mode"][1] / 1.4))
                self.assertEqual(theirs["size"], want,
                                 "the logical size is not the enclosing integer of %s / 1.4" % (theirs["mode"],))
        finally:
            user("kscreen-doctor output.%s.scale.1" % name, timeout=90)
            self.settle(3.0)

    def test_right_of_lands_edge_to_edge_and_the_restore_line_replays(self):
        """`--output B --right-of A` has to put B's left edge exactly on A's
        right edge in kscreen-doctor's own numbers, and the restore command
        wxrandr prints with KWin's save notice has to put the layout back when
        it is pasted -- it is the only undo there is here, since KWin writes
        every apply to kwinoutputconfig.json as it applies it."""
        if len(self.names) < 2:
            self.skipTest("needs two outputs (vm/vmctl start %s --heads 2)" % INSTANCE)
        a, b = self.names[0], self.names[1]
        before = self.kscreen()
        rc, out, err = user("wxrandr --output %s --right-of %s" % (b, a))
        self.assertEqual(rc, 0, err or out)
        self.settle()
        now = self.kscreen()
        self.assertEqual(now[b]["pos"][0], now[a]["pos"][0] + now[a]["size"][0],
                         "%s is not edge to edge with %s: %s vs %s" % (b, a, now[b]["pos"], now[a]))
        self.assertEqual(now[b]["pos"][1], now[a]["pos"][1], "--right-of moved it vertically too")
        # The line is PROSE with a command in it, not a command: kwin.py:1154
        # prints `to restore the previous layout: %s` through core.warn(), which
        # prefixes `xrandr: `, and restore_command() starts the command itself
        # at the word `wxrandr` (kwin.py:_undo_word, which never says `xrandr`
        # because a KDE image has the real one).  Running the whole line hands
        # the shell `xrandr: to restore ...` -> `sh: to: not found`, rc 127,
        # which is a failure of this test and not of the tool.
        line = [ln for ln in (out + err).splitlines() if "wxrandr" in ln and "--output" in ln]
        self.assertTrue(line, "no restore command line printed with KWin's save notice:\n%s" % (out + err))
        m = re.search(r"(wxrandr\s+--output\s.*)$", line[0].strip())
        self.assertTrue(m, "the restore line does not start a command at `wxrandr --output`: %r" % line[0])
        rc, out2, err2 = user(m.group(1))
        self.assertEqual(rc, 0, err2 or out2)
        self.settle()
        after = self.kscreen()
        self.assertEqual({n: after[n]["pos"] for n in after}, {n: before[n]["pos"] for n in before},
                         "the printed restore line did not replay the layout it was printed for")

    def test_same_as_is_recorded_as_a_replication_source(self):
        """A mirror is not a position on Plasma: KWin records it as
        `replicationSource` in ~/.config/kwinoutputconfig.json, which is the
        file it saves every apply into.  So `--same-as` is only right if that
        key appears -- two outputs that merely share a position are two
        outputs, and would look identical in any geometry check."""
        if len(self.names) < 2:
            self.skipTest("needs two outputs (vm/vmctl start %s --heads 2)" % INSTANCE)
        a, b = self.names[0], self.names[1]
        rc, out, err = user("wxrandr --output %s --same-as %s" % (b, a))
        self.assertEqual(rc, 0, err or out)
        self.settle()
        try:
            rc, cfg, err = user("cat ~/.config/kwinoutputconfig.json")
            self.assertEqual(rc, 0, err or cfg)
            self.assertIn("replicationSource", cfg,
                          "no replicationSource in kwinoutputconfig.json after --same-as:\n%s" % cfg[:2000])
            try:
                json.loads(cfg)
            except ValueError as e:
                self.fail("kwinoutputconfig.json is not JSON after our apply: %s" % e)
        finally:
            # End the mirror here rather than leaving it for the class teardown
            # to imply.  A replica is out of the layout entirely -- no wl_output,
            # nothing in kde_output_order_v1 (kwin.py:120-134) -- so "the mirror
            # ended" is not a JSON key, it is the compositor listing the two
            # outputs again at rectangles of their own.  What clears the
            # replication source is positioning the replica: kwin.py:863-866 and
            # :998-1002 send `repl = ""` for every output this invocation gives a
            # relation or a --pos, which is also why tearDownClass's --pos line
            # is enough to undo whatever a dying test left.
            x, y = self.start[b]["pos"]
            user("wxrandr --output %s --pos %dx%d" % (b, x, y))
            self.settle()
            back = self.kscreen()
            self.assertIn(b, back, "%s did not come back into the layout after the mirror ended" % b)
            self.assertNotEqual(back[b]["pos"], back[a]["pos"],
                                "%s still shares %s's rectangle: the mirror was not ended by --pos" % (b, a))

    def test_two_same_title_xterms_move_by_x_id_not_by_title(self):
        """F4.1, live: two xterms with the SAME title on Xwayland.  `wwmctl -i
        -r <xid> -e` names a window by its X id, so it has to move THAT window;
        a matcher that falls back to the first title match moves the wrong one
        and the geometry of the one that was named never changes.  Run at the
        session's own scale, with 'Apply scaling themselves' left alone -- the
        scale is what makes the two id spaces (Xwayland's and KWin's) diverge."""
        rc, _, _ = user("command -v xterm")
        if rc != 0:
            self.skipTest("no xterm on this golden (apt-get install -y xterm in the guest to run this)")
        user("pkill -f 'xterm -T fwtwin'", timeout=60)
        user("setsid nohup xterm -T fwtwin >/dev/null 2>&1 </dev/null & "
             "setsid nohup xterm -T fwtwin >/dev/null 2>&1 </dev/null & sleep 4; true", timeout=90)
        try:
            rc, out, err = user("wdotool search --name '^fwtwin$'")
            self.assertEqual(rc, 0, err or out)
            ids = [w for w in out.split() if w.isdigit()]
            self.assertEqual(len(ids), 2, "expected two same-title windows, got %r" % (out,))
            target = ids[1]
            rc, before, err = user("wdotool getwindowgeometry %s" % target)
            self.assertEqual(rc, 0, err or before)
            rc, out, err = user("wwmctl -i -r %s -e 0,700,120,600,400" % target)
            self.assertEqual(rc, 0, err or out)
            time.sleep(1.5)
            rc, after, err = user("wdotool getwindowgeometry %s" % target)
            self.assertEqual(rc, 0, err or after)
            self.assertNotEqual(before.split(), after.split(),
                                "the window named by its X id did not move (F4.1): %r" % after)
            self.assertRegex(after, r"Position:\s*700,120",
                             "-e 0,700,120,600,400 did not land at 700,120: %r" % after)
        finally:
            user("pkill -f 'xterm -T fwtwin'", timeout=60)

    @unittest.skipUnless(PLASMA67, WHY67)
    def test_plasma_67_discovers_outputs_through_the_registry(self):
        """Plasma 6.7 is the one release where there is no other way to see the
        outputs: `kde_output_device_v2` stopped being a plain wl_registry global
        and the devices arrive as new_ids on `kde_output_device_registry_v2`
        (version 23, `removed` opcode 36 -- hacks/display/kwin.py's REGISTRY table).
        So on 6.7, and only there, `--print-backend --verbose` has to name the
        registry, and `--query` has to see the outputs through it.

        This turns itself on the moment the golden exists: it runs when
        WXRANDR_LIVE_KWIN_VM names an instance of the 6.7 flavor AND that
        flavor's golden is on disk, and skips with the build line otherwise."""
        rc, out, err = user("wxrandr --print-backend --verbose")
        text = out + err
        self.assertEqual(rc, 0, text)
        self.assertIn("kde_output_device_registry_v2", text,
                      "6.7 has no plain kde_output_device_v2 global: the registry is the only discovery "
                      "path and --print-backend --verbose does not name it:\n%s" % text)
        self.assertRegex(text, r"kde_output_device_registry_v2 version \d+")
        rc, out, err = user("wxrandr --query")
        self.assertEqual(rc, 0, err or out)
        self.assertTrue(wxrandr_outputs(out),
                        "--query saw no output through the 6.7 registry:\n%s" % out)


if __name__ == "__main__":
    unittest.main()
