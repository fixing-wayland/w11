"""--layout: force the character table from the command line.

`--layout us` is the one that promises something -- the compositor's keymap is
not read and even the "is this plain US?" check does not run -- so these tests
watch xkbmap for calls rather than only checking the result.
"""
import io
import threading
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# every test file carries this itself: the suite is run file by file, where
# conftest.py never loads, and a tool that hands itself over would not be
# the code under test
os.environ["W11_PASSTHROUGH"] = "never"

from wdotool import cli, ctx as ctxmod, daemon
from hacks.input import xkbmap


class _Spy:
    """Record every Context built by a run, and what it was told."""

    def __init__(self):
        self.made = []

    def __enter__(self):
        self._orig = ctxmod.Context.__init__
        spy = self

        def init(this):
            spy._orig(this)
            spy.made.append(this)

        ctxmod.Context.__init__ = init
        return self

    def __exit__(self, *exc):
        ctxmod.Context.__init__ = self._orig
        return False

    @property
    def mode(self):
        return self.made[-1].layout_mode if self.made else "no context built"


def run(*argv):
    """cli.main with output captured; returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["wdotool"] + list(argv))
    return rc, out.getvalue(), err.getvalue()


class TheFlagReachesTheContext(unittest.TestCase):
    def test_separate_argument(self):
        with _Spy() as spy:
            rc, _, _ = run("--layout", "us", "sleep", "0")
        self.assertEqual(rc, 0)
        self.assertEqual(spy.mode, "us")

    def test_joined_argument(self):
        with _Spy() as spy:
            run("--layout=xkb", "sleep", "0")
        self.assertEqual(spy.mode, "xkb")

    def test_aliases_and_case(self):
        for given, want in (("US", "us"), ("Fixed", "fixed"), ("AUTO", "auto")):
            with _Spy() as spy:
                run("--layout", given, "sleep", "0")
            self.assertEqual(spy.mode, want, given)

    def test_absent_means_detection(self):
        with _Spy() as spy:
            run("sleep", "0")
        self.assertIsNone(spy.mode)

    def test_it_is_stripped_before_the_command_sees_it(self):
        # `sleep` would reject an unknown option, so reaching rc 0 with the
        # argument consumed is the assertion.
        rc, _, err = run("--layout", "us", "sleep", "0")
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")

    def test_it_reaches_script_mode(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".wdo", delete=False) as f:
            f.write("sleep 0\n")
            path = f.name
        try:
            with _Spy() as spy:
                rc, _, _ = run("--layout", "us", path)
            self.assertEqual(rc, 0)
            self.assertEqual(spy.mode, "us")
        finally:
            os.unlink(path)


class BadArguments(unittest.TestCase):
    def test_unknown_value(self):
        rc, _, err = run("--layout", "klingon", "sleep", "0")
        self.assertEqual(rc, 1)
        self.assertIn("invalid argument", err)
        # every spelling the parser accepts is named, "fixed" included
        self.assertIn("us, fixed, auto, xkb", err)

    def test_missing_value(self):
        rc, _, err = run("--layout")
        self.assertEqual(rc, 1)
        self.assertIn("requires an argument", err)

    def test_a_bad_value_changes_nothing(self):
        with _Spy() as spy:
            run("--layout", "klingon", "sleep", "0")
        self.assertEqual(spy.made, [], "the run must stop before doing work")


class ForcingUsReadsNothing(unittest.TestCase):
    """The promise: no keymap is fetched and the bypass check never runs."""

    def setUp(self):
        self.calls = []
        self._fetch, self._plain = xkbmap.fetch, xkbmap.active_group_is_plain_us

        def fetch(*a, **k):
            self.calls.append("fetch")
            raise AssertionError("the keymap must not be read")

        def plain(*a, **k):
            self.calls.append("bypass-check")
            return True

        xkbmap.fetch, xkbmap.active_group_is_plain_us = fetch, plain
        self.d = daemon._Daemon.__new__(daemon._Daemon)
        self.env = os.environ.get("WDOTOOL_LAYOUT")
        os.environ.pop("WDOTOOL_LAYOUT", None)

    def tearDown(self):
        xkbmap.fetch, xkbmap.active_group_is_plain_us = self._fetch, self._plain
        if self.env is None:
            os.environ.pop("WDOTOOL_LAYOUT", None)
        else:
            os.environ["WDOTOOL_LAYOUT"] = self.env

    def test_us(self):
        self.assertIsNone(self.d._layout(None, "us"))
        self.assertEqual(self.calls, [])

    def test_fixed_is_the_same_promise(self):
        self.assertIsNone(self.d._layout(None, "fixed"))
        self.assertEqual(self.calls, [])

    def test_the_flag_beats_the_environment(self):
        os.environ["WDOTOOL_LAYOUT"] = "xkb"
        self.assertIsNone(self.d._layout(None, "us"))
        self.assertEqual(self.calls, [], "xkb in the environment must not win")

    def test_without_the_flag_the_environment_still_decides(self):
        os.environ["WDOTOOL_LAYOUT"] = "us"
        self.assertIsNone(self.d._layout(None, None))
        self.assertEqual(self.calls, [])

    def test_auto_does_consult_the_compositor(self):
        # The control for the two tests above: without the promise the keymap
        # IS read. A bare _Daemon has none of the state the fallback needs,
        # and that is fine -- reaching the read at all is the assertion, so
        # what happens after it is not this test's business.
        self.d._xkb_backoff = 0.0
        self.d._xkb_mods_wait = 0
        try:
            self.d._layout([], "auto")
        except Exception:
            pass
        self.assertIn("fetch", self.calls)


class TheWireCarriesIt(unittest.TestCase):
    def test_client_methods_pass_it_on(self):
        sent = {}

        class FakeClient(daemon.DaemonClient):
            def __init__(self):
                pass

            def _rpc(self, **kw):
                sent.update(kw)
                return {}

        c = FakeClient()
        c.type_text("hi", 12, clearmods=False, layout_mode="us")
        self.assertEqual(sent["layout_mode"], "us")
        c.key("a", "press", 12, False, layout_mode="xkb")
        self.assertEqual(sent["layout_mode"], "xkb")

    def test_handle_hands_it_to_the_ops(self):
        """The class is named for the flag travelling the wire, so it has to
        watch the flag travel: an earlier version asserted only that the two
        ops had a parameter of that name, which passed identically whether
        handle() forwarded it or not -- and it did not."""
        seen = []

        class Spy(daemon._Daemon):
            def __init__(self):
                self.lock = threading.RLock()

            def op_type(self, text, delay_ms, clearmods, session=None,
                        layout_mode=None, vkbd_mode=None, warnings=None,
                        xkb_group=None):
                seen.append(("type", layout_mode))
                return []

            def op_key(self, spec, direction, delay_ms, clearmods,
                       session=None, layout_mode=None, vkbd_mode=None,
                       warnings=None, xkb_group=None):
                seen.append(("key", layout_mode))
                return []

        d = Spy()
        self.assertEqual(d.handle({"op": "type", "text": "z",
                                   "layout_mode": "us"})["ok"], True)
        self.assertEqual(d.handle({"op": "key", "spec": "a",
                                   "layout_mode": "xkb"})["ok"], True)
        self.assertEqual(seen, [("type", "us"), ("key", "xkb")])

    def test_a_request_without_the_flag_still_means_detection(self):
        seen = []

        class Spy(daemon._Daemon):
            def __init__(self):
                self.lock = threading.RLock()

            def op_type(self, text, delay_ms, clearmods, session=None,
                        layout_mode=None, vkbd_mode=None, warnings=None,
                        xkb_group=None):
                seen.append(layout_mode)
                return []

        Spy().handle({"op": "type", "text": "z"})
        self.assertEqual(seen, [None])


def _slurp(path):
    with open(path) as f:
        return f.read()


class ItIsALeadingOptionOnly(unittest.TestCase):
    """The scan used to walk the whole command line, so the flag was eaten
    wherever it appeared -- and, since the scan runs before the X11 handover,
    the real xdotool was handed the mangled argv too."""

    @staticmethod
    def _sink():
        """A temp file and the shell words that write this chain's arguments
        into it, so the child's stdout is captured rather than printed."""
        import tempfile

        fd, path = tempfile.mkstemp(prefix="layout-args-")
        os.close(fd)
        return path, ["sh", "-c", 'printf "%s\\n" "$@" > ' + path, "--"]

    def test_it_is_not_taken_from_a_commands_arguments(self):
        path, words = self._sink()
        try:
            with _Spy() as spy:
                rc, _o, err = run("exec", "--sync", *words,
                                  "a", "--layout", "us", "b")
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(_slurp(path), "a\n--layout\nus\nb\n")
            self.assertIsNone(spy.mode, "the child's arguments are not ours")
        finally:
            os.unlink(path)

    def test_a_script_keeps_it_as_a_positional_parameter(self):
        import tempfile

        path, words = self._sink()
        with tempfile.NamedTemporaryFile("w", suffix=".wdo",
                                         delete=False) as f:
            f.write("exec --sync %s $1\n" % " ".join(
                w if " " not in w else "'%s'" % w for w in words))
            script = f.name
        try:
            with _Spy() as spy:
                rc, _o, err = run(script, "--layout")
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(_slurp(path), "--layout\n")
            self.assertIsNone(spy.mode, "the script's $1 is not our flag")
        finally:
            os.unlink(script)
            os.unlink(path)

    def test_the_flag_still_works_before_the_command(self):
        with _Spy() as spy:
            rc, _o, _e = run("--layout", "us", "sleep", "0")
        self.assertEqual((rc, spy.mode), (0, "us"))

    def test_handle_hands_it_to_the_op(self):
        """The half that was missing: the ops took the argument and the client
        sent the field, but handle() dropped it, so --layout only ever worked
        through WDOTOOL_LAYOUT in the daemon's own environment."""
        d = daemon._Daemon.__new__(daemon._Daemon)
        d.lock = threading.RLock()
        seen = {}

        def op_type(text, delay_ms, clearmods, session=None, layout_mode=None,
                    vkbd_mode=None, warnings=None, xkb_group=None):
            seen["type"] = layout_mode
            return []

        def op_key(spec, direction, delay_ms, clearmods, session=None,
                   layout_mode=None, vkbd_mode=None, warnings=None,
                   xkb_group=None):
            seen["key"] = layout_mode
            return []

        d.op_type, d.op_key = op_type, op_key
        r = d.handle({"op": "type", "text": "hi", "layout_mode": "us"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(seen["type"], "us")
        r = d.handle({"op": "key", "spec": "a", "layout_mode": "xkb"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(seen["key"], "xkb")
        # absent stays None, so WDOTOOL_LAYOUT still decides
        d.handle({"op": "type", "text": "hi"})
        self.assertIsNone(seen["type"])

    def test_handle_rejects_an_unknown_mode(self):
        """The socket is a trust boundary: _layout() lower-cases what it gets
        and anything unrecognised would read as `auto`."""
        d = daemon._Daemon.__new__(daemon._Daemon)
        d.lock = threading.RLock()
        d.op_type = lambda *a, **k: []
        # serve_client turns this into {"ok": false, "error": ...}, exactly as
        # it does for every other bad request field.
        with self.assertRaises(RuntimeError) as cm:
            d.handle({"op": "type", "text": "hi", "layout_mode": "klingon"})
        self.assertIn("layout_mode", str(cm.exception))
        with self.assertRaises(RuntimeError):
            d.handle({"op": "type", "text": "hi", "layout_mode": 7})

    def test_handle_normalises_case_and_space(self):
        d = daemon._Daemon.__new__(daemon._Daemon)
        d.lock = threading.RLock()
        seen = {}
        d.op_type = lambda *a, **k: seen.update(m=k.get("layout_mode")
                                                or (a[4] if len(a) > 4 else None)) or []
        d.handle({"op": "type", "text": "hi", "layout_mode": "  US "})
        self.assertEqual(seen["m"], "us")


class ParityIsUntouched(unittest.TestCase):
    def test_the_flag_is_not_in_xdotools_help(self):
        _, out, _ = run("--help")
        self.assertNotIn("--layout", out)
        self.assertNotIn("layout", out.lower().split("keyboard")[0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
