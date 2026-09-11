"""Unit tests for wdotool.input_cmds using a recording fake daemon/backend."""

import contextlib
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# ...and the tests directory itself, for the bare `import support` below: running this file by path puts it
# on sys.path for free, `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import RecorderDev
from w11common.errors import CmdError
from wdotool import cli, daemon, input_cmds, keymap, uinput
from wdotool.backend import Window, WindowBackend
from wdotool.ctx import Context

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

# B13: the injection tests pin the *fixed US table* as the source of
# keycodes. Without this a developer running the suite inside a German or
# Dvorak session would have the daemon read that session's real keymap and
# type through it, and every keycode assertion here would be wrong.
os.environ.setdefault("WDOTOOL_LAYOUT", "us")



class _Stdin:
    """A stand-in for sys.stdin holding bytes: a text layer over a real
    buffer, exactly the shape `type --file -` reads through."""

    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)

    def read(self):
        return self.buffer.read().decode()      # strict, like the real one


def _cm(clearmods):
    """A "+clearmods" tail on the recorded call, so a test can see that the
    flag travelled *with* the injection instead of as a clear call, an
    injection and a restore call around it (three requests, two gaps another
    process can inject into)."""
    return ("+clearmods",) if clearmods else ()


class FakeDaemon:
    def __init__(self, held=(), known=True):
        self.calls = []
        self.pos = (0, 0)
        # The third element of the real pointer() answer (B6): False is a
        # daemon whose `pos` is the tablet's untouched axis state rather than
        # a position anything established. Default True, because every test
        # written before that flag existed meant "and it knows".
        self.known = known
        # What the daemon reports it was holding when clear_modifiers() is
        # called on its own -- the frozen API no command uses any more.
        self.held = list(held)

    def type_text(self, text, delay_ms, clearmods=False):
        self.calls.append(("type", text, delay_ms) + _cm(clearmods))

    def clear_modifiers(self):
        self.calls.append(("clearmods",))
        return list(self.held)

    def restore_modifiers(self, held):
        self.calls.append(("restoremods", list(held)))

    def key(self, spec, direction, delay_ms, clearmods):
        self.calls.append(("key", spec, direction, delay_ms, clearmods))

    def mousemove_abs(self, x, y, clearmods=False):
        self.calls.append(("abs", x, y) + _cm(clearmods))
        self.pos = (x, y)
        self.known = True          # a warp puts the pointer where it says

    def mousemove_rel(self, dx, dy, clearmods=False):
        # `known` deliberately untouched: the real daemon does not promote a
        # delta applied to a position it never knew (daemon.py, B6).
        self.calls.append(("rel", dx, dy) + _cm(clearmods))
        self.pos = (self.pos[0] + dx, self.pos[1] + dy)

    def seed_pointer(self, x, y):
        self.calls.append(("seed", x, y))
        self.pos = (x, y)
        self.known = True          # the compositor just said where it is

    def button(self, btn, down, clearmods=False):
        self.calls.append(("button", btn, down) + _cm(clearmods))

    def click(self, btn, repeat, delay_ms, clearmods=False):
        self.calls.append(("click", btn, repeat, delay_ms) + _cm(clearmods))

    def pointer(self):
        return (self.pos[0], self.pos[1], self.known)

    def geometry(self):
        return (1920, 1080)

    def geometry_full(self):
        return (0, 0, 1920, 1080)



class FakeBackend(WindowBackend):
    name = "fake"

    def __init__(self, wins=()):
        self.wins = list(wins)
        self.activated = []

    def list(self):
        return self.wins

    def activate(self, wid):
        self.activated.append(wid)


def make_ctx(wins=(), held=()):
    ctx = Context()
    ctx._daemon = FakeDaemon(held)
    ctx._backend = FakeBackend(wins)
    return ctx



class TestParse(unittest.TestCase):
    LONG = [("clearmodifiers", False), ("delay", True), ("help", False)]
    MAP = {"c": "clearmodifiers", "d": "delay", "h": "help"}

    def parse(self, args):
        """_parse prints the usage itself now, so every caller here catches
        it and test_help_* below check what came out."""
        self.printed = io.StringIO()
        with contextlib.redirect_stdout(self.printed):
            return input_cmds._parse("key", args, "usage\n", "cd:h", self.LONG,
                                     self.MAP)

    def test_prefix_match(self):
        opts, i, h = self.parse(["--clear", "x"])
        self.assertEqual((opts, i, h), ({"clearmodifiers": True}, 1, False))

    def test_inline_value(self):
        opts, i, h = self.parse(["--delay=5", "x"])
        self.assertEqual((opts, i), ({"delay": "5"}, 1))

    def test_separate_value(self):
        opts, i, h = self.parse(["--delay", "5", "x"])
        self.assertEqual((opts, i), ({"delay": "5"}, 2))

    def test_single_dash_long(self):
        opts, i, h = self.parse(["-delay", "5"])
        self.assertEqual((opts, i), ({"delay": "5"}, 2))

    def test_short_options(self):
        opts, i, h = self.parse(["-c", "1"])
        self.assertEqual((opts, i), ({"clearmodifiers": True}, 1))
        opts, i, h = self.parse(["-d5", "x"])
        self.assertEqual((opts, i), ({"delay": "5"}, 1))

    def test_double_dash_terminator(self):
        opts, i, h = self.parse(["--", "-20"])
        self.assertEqual((opts, i), ({}, 1))

    def test_unknown_option(self):
        with self.assertRaises(CmdError) as cm:
            self.parse(["--bogus"])
        self.assertIn("unrecognized option '--bogus'", str(cm.exception))
        self.assertIn("usage", str(cm.exception))

    def test_missing_argument(self):
        with self.assertRaises(CmdError):
            self.parse(["--delay"])

    def test_stops_at_positional(self):
        opts, i, h = self.parse(["5", "--delay", "1"])
        self.assertEqual((opts, i), ({}, 0))

    def test_help_detected(self):
        opts, i, h = self.parse(["--help", "x"])
        self.assertTrue(h)
        self.assertEqual((i, self.printed.getvalue()), (2, "usage\n"))

    def test_help_before_bad_option_wins(self):
        opts, i, h = self.parse(["--help", "--bogus"])
        self.assertTrue(h)
        self.assertEqual(self.printed.getvalue(), "usage\n")

    def test_short_h_is_help(self):
        opts, i, h = self.parse(["-h"])
        self.assertTrue(h)
        self.assertEqual(self.printed.getvalue(), "usage\n")

    def test_bad_option_before_help_raises(self):
        with self.assertRaises(CmdError):
            self.parse(["--bogus", "--help"])



class TestKey(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_key(ctx, ["ctrl+t"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls, [("key", "ctrl+t", "press", 12, False)])

    def test_stops_at_next_command(self):
        ctx = make_ctx()
        n = input_cmds.cmd_key(ctx, ["ctrl+t", "BackSpace", "mousemove", "3", "4"])
        self.assertEqual(n, 2)
        self.assertEqual([c[1] for c in ctx._daemon.calls], ["ctrl+t", "BackSpace"])

    def test_command_detection_case_insensitive(self):
        ctx = make_ctx()
        n = input_cmds.cmd_key(ctx, ["a", "MouseMove", "1", "2"])
        self.assertEqual(n, 1)

    def test_delay_and_clearmodifiers(self):
        ctx = make_ctx()
        input_cmds.cmd_key(ctx, ["--clearmodifiers", "--delay", "5", "a", "b"])
        self.assertEqual(ctx._daemon.calls, [
            ("key", "a", "press", 5, True),
            ("key", "b", "press", 5, False),  # cleared once
        ])

    def test_repeat(self):
        ctx = make_ctx()
        n = input_cmds.cmd_key(ctx, ["--repeat", "3", "a"])
        self.assertEqual(n, 3)
        self.assertEqual(len(ctx._daemon.calls), 3)

    def test_invalid_repeat(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_key(ctx, ["--repeat", "0", "a"])

    def test_no_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_key(ctx, [])

    def test_window_flag_activates(self):
        ctx = make_ctx([Window(id=42)])
        input_cmds.cmd_key(ctx, ["--window", "42", "a"])
        self.assertEqual(ctx._backend.activated, [42])
        self.assertEqual(ctx._daemon.calls, [("key", "a", "press", 12, False)])

    def test_stack_default_window(self):
        ctx = make_ctx([Window(id=7)])
        ctx.stack = [7]
        input_cmds.cmd_key(ctx, ["a"])
        self.assertEqual(ctx._backend.activated, [7])

    def test_keydown_keyup_directions(self):
        ctx = make_ctx()
        input_cmds.cmd_keydown(ctx, ["ctrl"])
        input_cmds.cmd_keyup(ctx, ["ctrl"])
        self.assertEqual([c[2] for c in ctx._daemon.calls], ["down", "up"])

    def test_invalid_sequence_aborts_chain(self):
        ctx = make_ctx()

        def bad_key(spec, direction, delay_ms, clearmods):
            raise CmdError(f"Error: Invalid key sequence '{spec}'")

        ctx._daemon.key = bad_key
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(cli.ChainAbort) as cm:
                input_cmds.cmd_key(ctx, ["ctrl-x"])
        # Byte-parity with xdotool 4.x: the sequence is converted once per
        # press pass and once per release pass, each failing pass printing
        # BOTH diagnostics and adding 1 to the exit status (B12).
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "Error: Invalid key sequence 'ctrl-x'\n"
            "Failure converting key sequence 'ctrl-x' to keycodes\n"
            "Error: Invalid key sequence 'ctrl-x'\n"
            "Failure converting key sequence 'ctrl-x' to keycodes\n"
            "xdo_send_keysequence_window reported an error for string 'ctrl-x'\n",
        )

    def test_invalid_sequence_keydown_single_pass(self):
        ctx = make_ctx()

        def bad_key(spec, direction, delay_ms, clearmods):
            raise CmdError(f"Error: Invalid key sequence '{spec}'")

        ctx._daemon.key = bad_key
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(cli.ChainAbort) as cm:
                input_cmds.cmd_keydown(ctx, ["ctrl-x"])
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "Error: Invalid key sequence 'ctrl-x'\n"
            "Failure converting key sequence 'ctrl-x' to keycodes\n"
            "xdo_send_keysequence_window reported an error for string 'ctrl-x'\n",
        )



class TestType(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["hello world"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls, [("type", "hello world", 12)])

    def test_consumes_everything_even_command_names(self):
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["hello", "key", "a"])
        self.assertEqual(n, 3)
        self.assertEqual([c[1] for c in ctx._daemon.calls], ["hello", "key", "a"])

    def test_args_limit(self):
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["--args", "1", "hello", "key", "a"])
        self.assertEqual(n, 3)  # 2 flag tokens + 1 typed arg
        self.assertEqual([c[1] for c in ctx._daemon.calls], ["hello"])

    def test_terminator(self):
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["--terminator", "END", "a", "b", "END", "key", "x"])
        self.assertEqual(n, 5)  # flags + a b + terminator
        self.assertEqual([c[1] for c in ctx._daemon.calls], ["a", "b"])

    def test_args_and_terminator_conflict(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_type(ctx, ["--args", "1", "--terminator", "X", "a"])

    def test_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("line1\nline2\n")
            path = f.name
        self.addCleanup(os.unlink, path)
        ctx = make_ctx()
        n = input_cmds.cmd_type(ctx, ["--file", path])
        self.assertEqual(n, 2)
        self.assertEqual(ctx._daemon.calls, [("type", "line1\nline2\n", 12)])

    def test_file_dash_reads_stdin(self):
        ctx = make_ctx()
        old, sys.stdin = sys.stdin, _Stdin(b"line1\nline2\n")
        try:
            n = input_cmds.cmd_type(ctx, ["--file", "-"])
        finally:
            sys.stdin = old
        self.assertEqual(n, 2)
        self.assertEqual(ctx._daemon.calls,
                         [("type", "line1\nline2\n", 12)])

    def test_file_dash_with_bytes_that_are_not_utf8(self):
        """`printf 'caf\\xe9' | wdotool type --file -` ended in a
        UnicodeDecodeError traceback: sys.stdin decodes strictly under a
        UTF-8 locale.  `--file PATH` beside it has always replaced
        undecodable bytes, and typing U+FFFD is what the user asked for
        far more than a traceback is."""
        ctx = make_ctx()
        old, sys.stdin = sys.stdin, _Stdin(b"caf\xe9 \xff\n")
        try:
            input_cmds.cmd_type(ctx, ["--file", "-"])
        finally:
            sys.stdin = old
        self.assertEqual(ctx._daemon.calls,
                         [("type", "caf\ufffd \ufffd\n", 12)])

    def test_file_dash_with_no_stdin_at_all(self):
        """`wdotool type --file - <&-`: fd 0 closed before the
        interpreter started leaves sys.stdin None, where the C fread()
        just reads nothing."""
        ctx = make_ctx()
        old, sys.stdin = sys.stdin, None
        try:
            input_cmds.cmd_type(ctx, ["--file", "-"])
        finally:
            sys.stdin = old
        self.assertEqual(ctx._daemon.calls, [("type", "", 12)])

    def test_missing_file(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_type(ctx, ["--file", "/nonexistent/x"])

    def test_no_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_type(ctx, [])

    def test_clearmodifiers(self):
        # One request: the daemon clears, types and puts back what it was
        # holding without letting go of its injection lock. Sending a clear
        # call and a restore call around the injection instead would let a
        # second wdotool process inject with the modifiers down.
        ctx = make_ctx(held=[29])
        input_cmds.cmd_type(ctx, ["--clearmodifiers", "hi"])
        self.assertEqual(ctx._daemon.calls,
                         [("type", "hi", 12, "+clearmods")])

    def test_without_the_flag_nothing_is_cleared(self):
        ctx = make_ctx(held=[29])
        input_cmds.cmd_type(ctx, ["hi"])
        self.assertEqual(ctx._daemon.calls, [("type", "hi", 12)])

    def test_no_stack_default(self):
        # unlike `key`, `type` ignores the window stack (cmd_type.c)
        ctx = make_ctx([Window(id=7)])
        ctx.stack = [7]
        input_cmds.cmd_type(ctx, ["x"])
        self.assertEqual(ctx._backend.activated, [])



class RealTypingDaemon:
    """`type` all the way down to the keycodes, instead of down to a recorded string.

    FakeDaemon above records the text and proves the argument handling; it cannot say anything about which
    key a character reaches, because it never resolves one. This one is the real `daemon._Daemon` with
    support.RecorderDev where /dev/uinput would be, so `cmd_type` -> `type_text` -> `op_type` ->
    `keymap.char_to_key` runs for real and the assertion is the evdev keycodes that came out."""

    def __init__(self):
        self.d = daemon._Daemon()
        self.d.kb = RecorderDev()
        self.d.dev_error = None
        self.d._reader = None           # no key-state reads in a test
        self.warnings = []

    def type_text(self, text, delay_ms, clearmods=False):
        self.warnings += self.d.op_type(text, delay_ms, clearmods, None, None, "off")

    def pressed(self):
        return [code for kind, code, value in self.d.kb.events if kind == "KEY" and value == 1]


class KeybitProbe(uinput.UinputDevice):
    """The real `uinput.keyboard()` stopped at its argument list: which keybits does it ask the kernel to
    register? A subclass rather than a mock because the question is about the real function's own numbers,
    and it must not open /dev/uinput (this box has none) -- so `__init__` records and does not call up."""

    asked = ()

    def __init__(self, name, keys=(), rels=(), abs_axes=(), vendor=0, product=0):
        KeybitProbe.asked = tuple(keys)


def kernel_keyboard_keybits():
    """The keycodes `wdotool`'s kernel keyboard registers. Every accepted code must be registered or the
    kernel drops the event silently (wdotool/uinput.py:160, its own comment)."""
    real, uinput.UinputDevice = uinput.UinputDevice, KeybitProbe
    try:
        uinput.keyboard()
    finally:
        uinput.UinputDevice = real
    return KeybitProbe.asked


class TypingACharacterOnlyTheUploadedKeymapBinds(unittest.TestCase):
    """B2 / goal2/recon/gaps.md §3b. `wdotool type EUR+X` used to put one keystroke on the wire and a
    "Can't type character" warning on stderr, because the built-in character table stopped at ASCII while
    the keymap wdotool uploads binds `key <I443> { [ 0x20ac ] }` = evdev 435.

    What this file can pin is the CLI's half: `cmd_type` -> `op_type` -> `keymap.char_to_key` resolves the
    character instead of warning about it, on the sink these cases run (`--vkbd off`, the kernel device).
    Whether that keycode then reaches a WINDOW is the sink's half and is pinned where the sink is real:
    tests/test_vkbd.py::UnderANonUsSessionLayout.test_the_euro_sign_goes_out_as_the_keycode_the_uploaded_keymap_binds
    is the virtual keyboard on a live socket, and that is the path measured live on sway (`--vkbd on type`
    landed the three UTF-8 bytes). On the kernel device 435 is resolved and then dropped, because the
    device registered keybits 1..255 -- the expected failure below is that gap, not a passing claim."""

    def ctx(self):
        ctx = Context()
        ctx._daemon = RealTypingDaemon()
        ctx._backend = FakeBackend()
        return ctx

    def test_the_euro_resolves_through_the_table_instead_of_the_skip_warning(self):
        """The resolution only: RecorderDev records whatever code it is handed, so the claim here stops at
        "the CLI asked for evdev 435 and issued no warning", which is what used to be false."""
        ctx = self.ctx()
        input_cmds.cmd_type(ctx, ["\u20acX"])
        # 435 = KEY_EURO, then Shift + 45 for the capital X
        self.assertEqual(ctx._daemon.pressed(), [435, keymap.KEY_LEFTSHIFT, 45])
        self.assertEqual(ctx._daemon.warnings, [])

    def test_the_kernel_device_registers_every_code_the_character_table_can_ask_for(self):
        """`uinput.keyboard()` registers keybits 1..255 plus every code the uploaded keymap binds above them
        (wdotool/uinput.py:160) and the kernel silently drops an event on a code that was not registered, so
        PLUS-MINUS (118) arrives on this sink and EUR (435) does not: `wdotool type EUR` on a plain `us`
        session with /dev/uinput -- which is what `--vkbd auto`, the default, picks -- presses a key nobody
        is listening for. Route 4, the kernel device we already create: register the UPLOADED_EXTRA_KEYS
        codes too, at the cost of one UI_SET_KEYBIT each and one measurement nobody has made (evdev 435
        through /dev/uinput into a native window on a `us` session). Filed in goal2/requests-batch-2.md
        against wdotool/uinput.py, which batch 2 does not own; when it lands this test passes unexpectedly
        and the decorator comes off."""
        bits = kernel_keyboard_keybits()
        self.assertIn(118, bits)
        for ch, (code, _shifted) in sorted(keymap.UPLOADED_EXTRA_KEYS.items()):
            self.assertIn(code, bits, "%r types through evdev %d and the device never registered it"
                                      % (ch, code))

    def test_a_character_no_keymap_here_binds_is_still_named_and_skipped(self):
        """The other half stays honest: a snowman is on neither the US block nor the uploaded keymap, so it
        is skipped with the character and the layout named. X types it (the pinned xdotool 4.20260303.1 put
        `é☃X` into an xterm on Xvfb byte for byte, by rebinding a scratch keycode), so this is our gap and
        vkbd.py names the rung-1 route that closes it."""
        ctx = self.ctx()
        input_cmds.cmd_type(ctx, ["a\u2603b"])
        self.assertEqual(ctx._daemon.pressed(), [30, 48])
        self.assertEqual(ctx._daemon.warnings,
                         ["Can't type character '\u2603' (not on the US layout). Skipping."])


class TestClick(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_click(ctx, ["1"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls, [("click", 1, 1, 100)])

    def test_repeat_delay(self):
        ctx = make_ctx()
        input_cmds.cmd_click(ctx, ["--repeat", "2", "--delay", "50", "3"])
        self.assertEqual(ctx._daemon.calls, [("click", 3, 2, 50)])

    def test_invalid_repeat(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_click(ctx, ["--repeat", "0", "1"])

    def test_no_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_click(ctx, [])

    def test_window_implies_clearmodifiers(self):
        ctx = make_ctx([Window(id=9)], held=[56])
        input_cmds.cmd_click(ctx, ["--window", "9", "1"])
        self.assertEqual(ctx._backend.activated, [9])
        self.assertEqual(ctx._daemon.calls,
                         [("click", 1, 1, 100, "+clearmods")])

    def test_ignores_stack(self):
        ctx = make_ctx([Window(id=9)])
        ctx.stack = [9]
        input_cmds.cmd_click(ctx, ["1"])
        self.assertEqual(ctx._backend.activated, [])



class TestMouseUpDown(unittest.TestCase):
    def test_mousedown(self):
        ctx = make_ctx()
        n = input_cmds.cmd_mousedown(ctx, ["3"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls, [("button", 3, True)])

    def test_mouseup(self):
        ctx = make_ctx()
        input_cmds.cmd_mouseup(ctx, ["1"])
        self.assertEqual(ctx._daemon.calls, [("button", 1, False)])

    def test_no_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_mousedown(ctx, [])



class TestMousemove(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_mousemove(ctx, ["100", "200"])
        self.assertEqual(n, 2)
        self.assertEqual(ctx._daemon.calls, [("abs", 100, 200)])

    def test_restore(self):
        ctx = make_ctx()
        input_cmds.cmd_mousemove(ctx, ["100", "200"])  # saves (0, 0) first
        n = input_cmds.cmd_mousemove(ctx, ["restore"])
        self.assertEqual(n, 1)
        self.assertEqual(ctx._daemon.calls[-1], ("abs", 0, 0))

    def test_restore_without_previous(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_mousemove(ctx, ["restore"])

    def test_window_relative(self):
        ctx = make_ctx([Window(id=7, x=50, y=60, w=100, h=100)])
        n = input_cmds.cmd_mousemove(ctx, ["--window", "7", "10", "20"])
        self.assertEqual(n, 4)
        self.assertEqual(ctx._daemon.calls, [("abs", 60, 80)])

    def test_polar_screen_center(self):
        ctx = make_ctx()
        input_cmds.cmd_mousemove(ctx, ["--polar", "90", "100"])
        self.assertEqual(ctx._daemon.calls[-1], ("abs", 1060, 540))

    def test_wrong_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_mousemove(ctx, ["100"])



class TestMousemoveRelative(unittest.TestCase):
    def test_basic(self):
        ctx = make_ctx()
        n = input_cmds.cmd_mousemove_relative(ctx, ["--", "-20", "-15"])
        self.assertEqual(n, 3)
        self.assertEqual(ctx._daemon.calls, [("rel", -20, -15)])

    def test_zero_move_is_noop(self):
        ctx = make_ctx()
        n = input_cmds.cmd_mousemove_relative(ctx, ["0", "0"])
        self.assertEqual(n, 2)
        self.assertEqual(ctx._daemon.calls, [])

    def test_polar(self):
        ctx = make_ctx()
        input_cmds.cmd_mousemove_relative(ctx, ["--polar", "90", "100"])
        self.assertEqual(ctx._daemon.calls, [("rel", 100, 0)])

    def test_wrong_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError):
            input_cmds.cmd_mousemove_relative(ctx, ["5"])



class TestGetmouselocation(unittest.TestCase):
    def test_output_and_stack(self):
        ctx = make_ctx([
            Window(id=5, x=0, y=0, w=1000, h=1000),
            Window(id=7, x=250, y=350, w=200, h=200),
        ])
        ctx._daemon.pos = (300, 400)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(n, 0)
        self.assertEqual(out.getvalue(), "x:300 y:400 screen:0 window:7\n")
        self.assertEqual(ctx.stack, [7])

    def test_focused_window_wins(self):
        ctx = make_ctx([
            Window(id=5, x=0, y=0, w=1000, h=1000, focused=True),
            Window(id=7, x=250, y=350, w=200, h=200),
        ])
        ctx._daemon.pos = (300, 400)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertIn("window:5", out.getvalue())

    def test_no_window_under_pointer(self):
        ctx = make_ctx()
        ctx._daemon.pos = (10, 10)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:10 y:10 screen:0 window:0\n")

    def test_shell_output(self):
        ctx = make_ctx()
        ctx._daemon.pos = (880, 443)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, ["--shell"])
        self.assertEqual(out.getvalue(), "X=880\nY=443\nSCREEN=0\nWINDOW=0\n")
        self.assertEqual(ctx.stack, [])  # --shell does not update the stack

    def test_shell_prefix(self):
        ctx = make_ctx()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, ["--shell", "--prefix", "M_"])
        self.assertTrue(out.getvalue().startswith("M_X="))

    def test_silent_when_not_last(self):
        ctx = make_ctx()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = input_cmds.cmd_getmouselocation(ctx, ["key", "a"])
        self.assertEqual(n, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(ctx.stack, [0])  # stack still updated



class TestBehaveScreenEdge(unittest.TestCase):
    def test_unsupported(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError) as cm:
            input_cmds.cmd_behave_screen_edge(ctx, ["bottom-left", "key", "a"])
        self.assertIn("not supported", str(cm.exception))

    def test_bad_edge(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError) as cm:
            input_cmds.cmd_behave_screen_edge(ctx, ["middle", "key", "a"])
        self.assertIn("Invalid edge or corner", str(cm.exception))

    def test_too_few_args(self):
        ctx = make_ctx()
        with self.assertRaises(CmdError) as cm:
            input_cmds.cmd_behave_screen_edge(ctx, ["left"])
        self.assertIn("Invalid number of arguments", str(cm.exception))



class PointerBackend(WindowBackend):
    """A backend that can be asked where the pointer is (GNOME's bridge)."""

    name = "pointer-fake"

    def __init__(self, pos=(2880, 540), wins=(), fail=None):
        self.pos = pos
        self.wins = list(wins)
        self.fail = fail
        self.queries = 0

    def list(self):
        return self.wins

    def activate(self, wid):
        pass

    def pointer(self):
        self.queries += 1
        if self.fail is not None:
            raise self.fail
        return self.pos


class TestRealPointer(unittest.TestCase):
    """B6/B1: the compositor is the source of truth for the pointer, and the
    daemon's model is corrected from it before a relative move."""

    def ctx(self, backend):
        ctx = Context()
        ctx._daemon = FakeDaemon()
        ctx._backend = backend
        return ctx

    def test_getmouselocation_reports_the_compositor(self):
        b = PointerBackend(pos=(2880, 540),
                           wins=[Window(id=9, x=2000, y=500, w=1000, h=200)])
        ctx = self.ctx(b)
        ctx._daemon.pos = (0, 0)          # a daemon that has injected nothing
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:2880 y:540 screen:0 window:9\n")
        self.assertEqual(b.queries, 1)
        # ... and the daemon's model is corrected from it
        self.assertEqual(ctx._daemon.calls, [("seed", 2880, 540)])
        self.assertEqual(ctx._daemon.pos, (2880, 540))

    def test_relative_move_counts_from_the_real_position(self):
        b = PointerBackend(pos=(1662, 601))   # a physical mouse moved it here
        ctx = self.ctx(b)
        ctx._daemon.pos = (1200, 601)         # what this daemon last injected
        input_cmds.cmd_mousemove_relative(ctx, ["500", "0"])
        self.assertEqual(ctx._daemon.calls,
                         [("seed", 1662, 601), ("rel", 500, 0)])
        self.assertEqual(ctx._daemon.pos, (2162, 601))

    def test_restore_captures_the_real_position(self):
        b = PointerBackend(pos=(77, 88))
        ctx = self.ctx(b)
        input_cmds.cmd_mousemove(ctx, ["100", "200"])
        input_cmds.cmd_mousemove(ctx, ["restore"])
        self.assertEqual(ctx._daemon.calls[-1], ("abs", 77, 88))

    def test_a_backend_without_a_pointer_query_keeps_the_model(self):
        ctx = make_ctx()                      # FakeBackend: no pointer()
        ctx._daemon.pos = (300, 400)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:300 y:400 screen:0 window:0\n")
        self.assertNotIn("seed", [c[0] for c in ctx._daemon.calls])

    def test_no_session_falls_back_to_the_model(self):
        b = PointerBackend(fail=CmdError("gnome backend: the bridge vanished"))
        ctx = self.ctx(b)
        ctx._daemon.pos = (5, 6)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:5 y:6 screen:0 window:0\n")

    def test_nobody_knows_where_the_pointer_is_and_the_terminal_is_told(self):
        """F3.3, fix 38. A compositor with no pointer query (sway/i3 IPC has
        none, and zwlr_virtual_pointer_v1 delivers no events at all) plus a
        daemon that has injected no motion: there is no position, and 0,0 is
        the tablet's untouched axis state. It used to be printed as one --
        `x:0 y:0 screen:0 window:0`, rc 0 -- while docs/WDOTOOL.md:81 said
        getmouselocation "refuses with that reason rather than guessing when
        wdotool has not moved it". Now it refuses, with the daemon's own
        POINTER_UNKNOWN text, and prints no coordinate line at all."""
        ctx = Context()
        ctx._daemon = FakeDaemon(known=False)     # nothing has moved it
        ctx._backend = FakeBackend()              # no pointer() at all
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(CmdError) as caught:
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(str(caught.exception), daemon.POINTER_UNKNOWN)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("x:", out.getvalue())

    def test_the_same_daemon_answers_once_the_pointer_has_been_moved(self):
        """The other half of the refusal: it is the *unknown* that is
        refused, not the sway path. One mousemove and the daemon knows
        exactly where it put the cursor, which is what the refusal's own
        text tells the user to do."""
        ctx = Context()
        ctx._daemon = FakeDaemon(known=False)
        ctx._backend = FakeBackend()
        input_cmds.cmd_mousemove(ctx, ["10", "20"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:10 y:20 screen:0 window:0\n")

    def test_a_move_never_needs_the_answer_it_cannot_have(self):
        """`mousemove` and `mousemove_relative` ask where the pointer is only
        to remember it and to seed the model; neither NEEDS it, so the
        refusal above must not take the moves down with it.

        The absolute move is the half that carries the behaviour change:
        cmd_mousemove asks _pointer_opt for the position it will hand back to
        `mousemove restore`, and before fix 38 that question was answered
        `(0, 0)` -- so `mousemove 500 500` followed by `mousemove restore`
        warped the cursor to the top-left corner of the screen, confidently,
        having never known where it started. Now the unknown stays unknown:
        _last_mouse is None and restore says so. (cmd_mousemove_relative
        never assigns _last_mouse at all -- input_cmds.py discards
        _pointer_opt's result there, it asks only to seed the model -- so the
        `("rel", 5, 5)` below is what pins the shrug for that one.)"""
        ctx = Context()
        ctx._daemon = FakeDaemon(known=False)
        ctx._backend = FakeBackend()
        input_cmds.cmd_mousemove_relative(ctx, ["5", "5"])
        self.assertEqual(ctx._daemon.calls, [("rel", 5, 5)])

        ctx = Context()
        ctx._daemon = FakeDaemon(known=False)
        ctx._backend = FakeBackend()
        input_cmds.cmd_mousemove(ctx, ["10", "20"])
        self.assertEqual(ctx._daemon.calls, [("abs", 10, 20)])
        self.assertIsNone(ctx._last_mouse, "(0, 0) was never a position")
        with self.assertRaises(CmdError) as caught:
            input_cmds.cmd_mousemove(ctx, ["restore"])
        self.assertEqual(str(caught.exception),
                         "Have no previous mouse position. Cannot restore.")
        self.assertEqual(ctx._daemon.calls, [("abs", 10, 20)],
                         "and it did not warp to 0,0 on the way out")

    def test_a_compositor_query_that_failed_says_why_it_failed(self):
        """F3.3, fix 38, the half that is not sway. _backend_pointer folds
        two Nones together: a compositor with no pointer query, and one whose
        query raised. The second is the measured GNOME 51.beta as-shipped
        state -- the bridge is marked out of date for shell 51 and every
        other command prints exactly that, `wdotool search --name .`
        included -- and a fresh daemon there has `known` false, so the
        refusal fires. It must not then hand a GNOME user sway's answer:
        "zwlr_virtual_pointer_v1 cannot be asked" names a protocol that
        session does not use and drops the one line that tells them what to
        reinstall."""
        msg = ("the w11 bridge extension is marked out of date for "
               "this GNOME Shell (['45'...'50']); reinstall a matching "
               "gnome/ from the repo")
        b = PointerBackend(fail=CmdError(msg))
        ctx = Context()
        ctx._daemon = FakeDaemon(known=False)
        ctx._backend = b
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(CmdError) as caught:
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(str(caught.exception), msg)
        self.assertNotIn("zwlr_virtual_pointer_v1", str(caught.exception))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(b.queries, 1)

    def test_the_exit_code_of_the_reason_survives_being_re_raised(self):
        """`wdotool getmouselocation` with no session at all exits 2, not 1:
        ctx.py's NoSessionError carries its own exit_code and cli.py prints
        str() and exits with it. Re-raising the backend's exception rather
        than wrapping its text is what keeps that -- a script that greps rc
        for "there is no session" sees the same number from
        getmouselocation as from search."""

        class NoSession(CmdError):
            exit_code = 2

        b = PointerBackend(fail=NoSession("no wayland display and no DISPLAY"))
        ctx = Context()
        ctx._daemon = FakeDaemon(known=False)
        ctx._backend = b
        with self.assertRaises(CmdError) as caught:
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertIs(caught.exception, b.fail)

    def test_a_compositor_with_no_query_at_all_still_gets_the_sway_answer(self):
        """The control for the two above: distinguishing the reasons must not
        cost the sway path its own. `backend.WindowBackend.pointer` returns
        None and backend_sway.py does not override it -- sway's IPC carries
        no cursor position -- so there is no exception to quote and
        POINTER_UNKNOWN, which names the protocol and tells the user to move
        the pointer once, is still the right and only thing to say."""
        ctx = Context()
        ctx._daemon = FakeDaemon(known=False)
        ctx._backend = FakeBackend()
        self.assertIsNone(ctx._backend.pointer(), "the sway shape")
        with self.assertRaises(CmdError) as caught:
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(str(caught.exception), daemon.POINTER_UNKNOWN)

    def test_a_daemon_that_cannot_be_seeded_is_not_fatal(self):
        b = PointerBackend(pos=(11, 22))
        ctx = self.ctx(b)

        def boom(x, y):
            raise CmdError("cannot start wdotool daemon")

        ctx._daemon.seed_pointer = boom
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            input_cmds.cmd_getmouselocation(ctx, [])
        self.assertEqual(out.getvalue(), "x:11 y:22 screen:0 window:0\n")


class TestStrtonum(unittest.TestCase):
    """B14: C strtoul(s, NULL, 0), not Python's int(s, 0)."""

    def test_c_bases(self):
        cases = {
            "0755": 0o755,      # C octal; int(s, 0) rejects it outright
            "0b101": 0,         # strtoul stops at the 'b'; int(s, 0) says 5
            "0x1f": 31, "0X1F": 31, "0": 0, "08": 0, "  12": 12,
            "12ms": 12, "-5": -5, "+7": 7, "": 0, "abc": 0, "0xzz": 0,
            "\u0664\u0662": 0,  # not ASCII digits, so not digits
        }
        for text, want in cases.items():
            self.assertEqual(input_cmds._strtonum(text), want, text)

    def test_atoi_is_ascii(self):
        """C isspace()/isdigit() in the "C" locale, which Python's \\d and
        str.strip() are not: `click \u0664` is button 0, not button 4."""
        self.assertEqual(input_cmds._atoi("\u0664\u0662"), 0)
        self.assertEqual(input_cmds._atoi("4\u0662"), 4)
        self.assertEqual(input_cmds._atoi("\xa042"), 0)
        self.assertEqual(input_cmds._atoi("  -7x"), -7)
        self.assertEqual(input_cmds._atoi(12), 12)   # an int default

    def test_delay_option_uses_it(self):
        ctx = make_ctx()
        input_cmds.cmd_key(ctx, ["--delay", "0755", "a"])
        self.assertEqual(ctx._daemon.calls[-1][3], 0o755)
        ctx = make_ctx()
        input_cmds.cmd_key(ctx, ["--delay", "0b101", "a"])
        self.assertEqual(ctx._daemon.calls[-1][3], 0)

class TestClearModifiersIsOneRequest(unittest.TestCase):
    """Every command that takes --clearmodifiers sends it *with* the
    injection. The clear/restore pair on DaemonClient still exists (frozen
    API) but no command uses it: as two extra round trips it leaves gaps in
    which another wdotool process can inject, with the modifiers released or
    across the restore."""

    CASES = [
        ("cmd_type", ["--clearmodifiers", "hi"], ("type", "hi", 12, "+clearmods")),
        ("cmd_click", ["--clearmodifiers", "1"], ("click", 1, 1, 100, "+clearmods")),
        ("cmd_mousedown", ["--clearmodifiers", "1"], ("button", 1, True, "+clearmods")),
        ("cmd_mouseup", ["--clearmodifiers", "1"], ("button", 1, False, "+clearmods")),
        ("cmd_mousemove", ["--clearmodifiers", "10", "20"], ("abs", 10, 20, "+clearmods")),
        ("cmd_mousemove_relative", ["--clearmodifiers", "3", "4"], ("rel", 3, 4, "+clearmods")),
    ]

    def test_the_flag_travels_with_the_injection(self):
        for name, argv, expected in self.CASES:
            ctx = make_ctx(held=[29])
            getattr(input_cmds, name)(ctx, list(argv))
            calls = ctx._daemon.calls
            self.assertIn(expected, calls, name)
            self.assertNotIn(("clearmods",), calls, name)
            self.assertEqual([c for c in calls if c[0] == "restoremods"], [], name)

    def test_without_the_flag_the_call_is_plain(self):
        for name, argv, expected in self.CASES:
            ctx = make_ctx(held=[29])
            getattr(input_cmds, name)(ctx, [a for a in argv
                                            if a != "--clearmodifiers"])
            self.assertIn(expected[:-1], ctx._daemon.calls, name)
            self.assertNotIn(("clearmods",), ctx._daemon.calls, name)


if __name__ == "__main__":
    unittest.main()
