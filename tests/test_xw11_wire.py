#!/usr/bin/env python3
"""xw11/wire.py against the bytes it was written from.

Every claim here is a capture, not a reading of the specification: the request
stream is the 31-request prologue `xdotool` really sends
(tests/fixtures/xw11/prologue-xdotool.hex, out of recon/tools/caps/raw.jsonl),
the GenericEvent is a real XI_Motion off an Xvfb, the two setup replies come off
a live Xvfb 21.1.22 and a live Xwayland 24.1.10 under sway, and the BadDrawable
packet is the one recon/wire.md 3.1 recorded from a GetGeometry on 0xdeadbeef.

The setup parser is cross-checked against `x11_mini._handshake`'s own loop on
the same bytes, because that loop has been reading real servers in this tree for
two releases and disagreeing with it would mean one of the two is wrong.
"""

import ast
import binascii
import os
import struct
import sys
import tempfile
import unittest
from xml.etree import ElementTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path:
# running this file by path puts it there for free, `python3 -m unittest
# tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wdotool import x11_mini                                       # noqa: E402
from xw11 import policy                                           # noqa: E402
from xw11 import wire                                             # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py. This line is what covers
# `python3 tests/<file>.py`, where conftest is not loaded, and it reaches every
# subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "xw11")

#: The BadDrawable packet of recon/wire.md 3.1, off a live server: a
#: GetGeometry on drawable 0xdeadbeef as request 4.
BAD_DRAWABLE = binascii.unhexlify(
    "000904" "00" "efbeadde" "0000" "0e" + "00" * 21)


def packets(name):
    """The hex lines of a fixture, `#` comments dropped."""
    out = []
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(binascii.unhexlify(line))
    return out


#: `ast.TryStar` is 3.11's node: on the 3.10 interpreter this scanner exists to
#: protect, `ast.TryStar` is an AttributeError and the scanner would die before
#: it scanned a line. `isinstance` takes an empty tuple and matches nothing,
#: which is exactly right -- a 3.10 parser cannot produce an `except*` node
#: anyway, it raises SyntaxError on the source.
_TRY_STAR = getattr(ast, "TryStar", ())


def py310_offenders(path):
    """Syntax and names this project's Python 3.10 floor does not have
    (pyproject.toml:10 `requires-python = ">=3.10"`); R12. `match` and
    `except*` are 3.10/3.11 syntax, `tomllib` is 3.11's module and
    `typing.Self` is 3.11's name."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Match):
            found.append("match at line %d" % node.lineno)
        elif isinstance(node, _TRY_STAR):
            found.append("except* at line %d" % node.lineno)
        elif isinstance(node, ast.Import):
            found += ["import %s" % a.name for a in node.names if a.name == "tomllib"]
        elif isinstance(node, ast.ImportFrom):
            if node.module == "tomllib":
                found.append("from tomllib")
            if node.module == "typing":
                found += ["typing.%s" % a.name for a in node.names
                          if a.name in ("Self", "ExceptionGroup")]
        elif isinstance(node, ast.Attribute) and node.attr in ("Self", "ExceptionGroup"):
            found.append("%s at line %d" % (node.attr, node.lineno))
        elif isinstance(node, ast.Name) and node.id == "ExceptionGroup":
            found.append("ExceptionGroup at line %d" % node.lineno)
    return found


class Splitter(unittest.TestCase):
    """Byte 2-3 of a request is the whole framing (recon/wire.md 2)."""

    def test_the_whole_xdotool_prologue_splits_at_the_captured_boundaries(self):
        """31 requests, concatenated into one stream, split back into exactly
        the 31 the capture recorded -- same lengths, same opcodes, same order.
        BIG-REQUESTS is enabled at request 2 in the real stream and the splitter
        is told so at the same point."""
        reqs = packets("prologue-xdotool.hex")
        self.assertEqual(len(reqs), 31)
        stream = bytearray(b"".join(reqs))
        bigreq = False
        got = []
        while stream:
            frame = wire.split_request(stream, bigreq)
            self.assertIsInstance(frame, tuple, "mis-framed after %d requests" % len(got))
            nbytes, opcode, byte1 = frame
            got.append((nbytes, opcode, byte1))
            if opcode == 133 and byte1 == 0:      # BIG-REQUESTS.Enable, major 133 here
                bigreq = True
            del stream[:nbytes]
        self.assertEqual([n for n, _o, _b in got], [len(r) for r in reqs])
        self.assertEqual([o for _n, o, _b in got], [r[0] for r in reqs])

    def test_the_first_two_requests_are_querying_and_enabling_big_requests(self):
        """recon/tools.md 2: every libX11 connection opens with a 20-byte
        QueryExtension("BIG-REQUESTS") and a 4-byte Enable, in that order, and
        the proxy sees the zero-length form from connection one onwards."""
        reqs = packets("prologue-xdotool.hex")
        self.assertEqual(wire.split_request(reqs[0], False), (20, 98, 0))
        self.assertEqual(reqs[0][8:20], b"BIG-REQUESTS")
        self.assertEqual(wire.split_request(reqs[1], False), (4, 133, 0))

    def test_the_two_xkeyboard_frames_the_capture_names_log_under_that_name(self):
        """Requests 16 and 17 of the capture are major 135 minor 1, and the
        logger that recorded them called both XKEYBOARD.SelectEvents
        (tests/fixtures/xw11/prologue-xdotool.hex, its own per-line comment).
        policy.request_name has to reach the same name off the same two bytes,
        because those bytes are all the debug log ever gets."""
        reqs = packets("prologue-xdotool.hex")
        for index in (15, 16):
            frame = wire.split_request(reqs[index], False)
            self.assertIsInstance(frame, tuple)
            _nbytes, opcode, byte1 = frame
            self.assertEqual((opcode, byte1), (135, 1))
            self.assertEqual(policy.request_name(opcode, byte1, "XKEYBOARD"),
                             "XKEYBOARD.SelectEvents")
        # And the neighbours the same table names, so a row shifted by one is
        # not a pass: 12 and 15 are GetMap (minor 8), 14 is GetNames (17).
        self.assertEqual(policy.request_name(135, reqs[11][1], "XKEYBOARD"),
                         "XKEYBOARD.GetMap")
        self.assertEqual(policy.request_name(135, reqs[13][1], "XKEYBOARD"),
                         "XKEYBOARD.GetNames")

    def test_a_one_byte_trickle_never_mis_frames(self):
        """The stream is a stream: a request delivered one byte at a time must
        come out at exactly the same boundary as one delivered whole."""
        reqs = packets("prologue-xdotool.hex")[:6]
        buf = bytearray()
        frames = []
        for byte in b"".join(reqs):
            buf.append(byte)
            while True:
                got = wire.split_request(buf, False)
                if not isinstance(got, tuple):
                    break
                frames.append(got[0])
                del buf[:got[0]]
        self.assertEqual(frames, [len(r) for r in reqs])
        self.assertEqual(bytes(buf), b"")

    def test_a_zero_length_with_big_requests_enabled_reads_the_u32(self):
        big = struct.pack("<BBHI", 20, 0, 0, 6) + b"\0" * 16      # GetProperty, 24 bytes
        self.assertEqual(wire.split_request(big, True), (24, 20, 0))
        self.assertIsNone(wire.split_request(big[:23], True))
        self.assertIsNone(wire.split_request(big[:7], True))

    def test_a_zero_length_without_big_requests_is_the_measured_bad_length(self):
        """R3: xtrace treats ANY zero length as big and a real server does not
        (recon/wire.md 2). tests/fixtures/xw11/badlength-nobigreq.hex is what
        Xvfb answered."""
        req, _answer = packets("badlength-nobigreq.hex")
        self.assertEqual(req, b"\x2b\x00\x00\x00")
        self.assertEqual(wire.split_request(req, False), wire.BAD_LENGTH)
        self.assertEqual(wire.split_request(req, True), None)     # waits for the u32

    def test_a_big_form_shorter_than_its_own_header_is_refused(self):
        self.assertEqual(wire.split_request(struct.pack("<BBHI", 20, 0, 0, 1), True),
                         wire.BAD_LENGTH)


class Framer(unittest.TestCase):
    """32 bytes, plus 4*u32le[4] when byte 0 is 1 or byte 0 & 0x7F is 35
    (recon/wire.md 3.1)."""

    def test_the_captured_generic_event_frames_as_136_bytes(self):
        (ge,) = packets("geprobe-136.hex")
        self.assertEqual(len(ge), 136)
        self.assertEqual(ge[0] & 0x7F, 35)
        self.assertEqual(wire.split_server(ge), 136)

    def test_an_error_frames_as_exactly_32(self):
        self.assertEqual(wire.split_server(BAD_DRAWABLE), 32)
        self.assertEqual(wire.split_server(BAD_DRAWABLE + b"\xff" * 40), 32)

    def test_a_200kb_reply_frames_exactly(self):
        """The size xprop's own read of a big property produces
        (recon/tools.md 6)."""
        body = b"\xa5" * 200000
        pkt = wire.reply(7, 8, b"\0" * 24, body)
        self.assertEqual(len(pkt), 32 + 200000)
        self.assertEqual(wire.split_server(pkt), 32 + 200000)
        self.assertEqual(wire.split_server(pkt[:32]), 32 + 200000)

    def test_less_than_a_head_is_not_a_frame(self):
        self.assertIsNone(wire.split_server(BAD_DRAWABLE[:31]))

    def test_one_word_over_the_cap_raises(self):
        """The 16 MiB cap is on the length FIELD of a reply, never on what a
        client may ask for -- request 4 of every connection asks for 400 MB
        (recon/tools.md 2)."""
        head = struct.pack("<BBHI", 1, 0, 3, x11_mini._MAX_REPLY_WORDS) + b"\0" * 24
        self.assertEqual(wire.split_server(head), 32 + 4 * x11_mini._MAX_REPLY_WORDS)
        over = struct.pack("<BBHI", 1, 0, 3, x11_mini._MAX_REPLY_WORDS + 1) + b"\0" * 24
        with self.assertRaises(wire.WireError):
            wire.split_server(over)


class SetupParse(unittest.TestCase):
    """The two setup replies, off the two real servers (recon/tools.md 8)."""

    def setup(self, name):
        head, body = packets(name)
        return head, body, wire.parse_setup_reply(head, body)

    def test_xvfb(self):
        head, body, s = self.setup("setup-xvfb.hex")
        self.assertEqual(len(head) + len(body), 2596)
        self.assertEqual(s.status, 1)
        self.assertEqual(s.rid_base, 0x200000)
        self.assertEqual(s.rid_mask, 0x1FFFFF)
        self.assertEqual(s.max_words, 65535)
        self.assertEqual(s.roots, [0x21F])
        self.assertEqual((s.min_keycode, s.max_keycode), (8, 255))
        self.assertEqual((s.root_depth, s.root_visual), (24, 0x21))

    def test_xwayland(self):
        head, body, s = self.setup("setup-xwayland.hex")
        self.assertEqual(len(head) + len(body), 2612)
        self.assertEqual(s.rid_mask, 0x1FFFFF)
        self.assertEqual(s.max_words, 65535)
        self.assertEqual(s.roots, [0x234])
        self.assertEqual((s.min_keycode, s.max_keycode), (8, 255))
        # The base is per connection and steps by mask + 1 (recon/wire.md 1.3),
        # so the value is not pinned -- the alignment is.
        self.assertEqual(s.rid_base % (s.rid_mask + 1), 0)

    def test_the_screen_walk_consumes_exactly_the_body(self):
        """recon/wire.md 1.2 validated every offset this way: `total parsed ==
        body length`. A walk that stopped early or ran on would still return a
        root and would still look healthy."""
        for name in ("setup-xvfb.hex", "setup-xwayland.hex"):
            with self.subTest(name):
                _head, body, s = self.setup(name)
                self.assertEqual(s.parsed, len(body))

    def test_our_parser_and_x11_minis_agree_on_the_same_bytes(self):
        """`x11_mini._handshake` has been reading real servers for two
        releases. Two parsers over one capture that disagree mean one of them
        is wrong, and this is the only place that can say which."""
        for name in ("setup-xvfb.hex", "setup-xwayland.hex"):
            with self.subTest(name):
                head, body, s = self.setup(name)
                # The real loop, fed the captured bytes rather than a socket:
                # _handshake reads the 8-byte head and then the body, in that
                # order, and writes the setup request first.
                conn = x11_mini.X11Conn.__new__(x11_mini.X11Conn)
                chunks = [head, body]
                conn._recv_exact = lambda n, sock=None: chunks.pop(0)
                sent = []
                ok, why = conn._handshake(type("S", (), {"sendall": sent.append})(),
                                          b"", b"")
                self.assertEqual(len(sent), 1)
                self.assertTrue(ok, why)
                self.assertEqual(conn._rid_base, s.rid_base)
                self.assertEqual(conn._rid_mask, s.rid_mask)
                self.assertEqual(conn._roots, s.roots)
                self.assertEqual(conn._max_req_words, max(4096, s.max_words))

    def test_a_failed_setup_parses_without_screens(self):
        head = struct.pack("<BBHHH", 0, 4, 11, 0, 1)
        s = wire.parse_setup_reply(head, b"nope")
        self.assertEqual((s.status, s.roots, s.parsed), (0, [], 0))

    def test_a_screen_count_pointing_past_the_end_raises(self):
        _head, body, _s = self.setup("setup-xvfb.hex")
        lying = bytearray(body)
        lying[20] = 9                                   # nine screens in a one-screen body
        with self.assertRaises(wire.WireError):
            wire.parse_setup_reply(b"\1\0\x0b\0\0\0\0\0", bytes(lying))


class SetupRequestParse(unittest.TestCase):
    def test_the_cookie_less_and_the_mit_cookie_shapes(self):
        """recon/wire.md 1.1 measured both: 12 bytes with no auth, and 48 with
        MIT-MAGIC-COOKIE-1 (12 + pad4(18) + 16)."""
        bare = struct.pack("<BxHHHHxx", 0x6C, 11, 0, 0, 0)
        self.assertEqual(wire.parse_setup_request(bare), (0x6C, 0, 0, 12))
        cooked = struct.pack("<BxHHHHxx", 0x6C, 11, 0, 18, 16)
        self.assertEqual(wire.parse_setup_request(cooked), (0x6C, 18, 16, 48))

    def test_the_msb_first_byte_comes_back_as_it_stands(self):
        msb = struct.pack("<BxHHHHxx", 0x42, 11, 0, 0, 0)
        self.assertEqual(wire.parse_setup_request(msb)[0], 0x42)

    def test_a_short_head_is_not_a_setup(self):
        self.assertIsNone(wire.parse_setup_request(b"l\0\x0b\0"))


class ErrorBytes(unittest.TestCase):
    def test_the_packer_reproduces_the_captured_bad_drawable(self):
        """recon/wire.md 3.1, a GetGeometry on 0xdeadbeef answered as request 4:
        `00 09 0400 efbeadde 0000 0e` and 21 bytes of pad."""
        self.assertEqual(wire.error(9, 4, 0xDEADBEEF, 14, 0), BAD_DRAWABLE)

    def test_the_sequence_is_the_low_sixteen_bits(self):
        pkt = wire.error(3, 0x1_0004, 0, 15, 0)
        self.assertEqual(struct.unpack_from("<H", pkt, 2)[0], 4)

    def test_a_reply_carries_its_body_length_in_words(self):
        pkt = wire.reply(9, 8, b"\1" * 24, b"abcd" * 3)
        self.assertEqual(struct.unpack_from("<I", pkt, 4)[0], 3)
        self.assertEqual(len(pkt), 44)

    def test_a_body_that_is_not_a_multiple_of_four_is_padded(self):
        pkt = wire.reply(1, 0, b"\0" * 24, b"abc")
        self.assertEqual(len(pkt), 36)
        self.assertEqual(struct.unpack_from("<I", pkt, 4)[0], 1)

    def test_a_fixed_part_that_is_not_24_bytes_is_a_wire_error(self):
        with self.assertRaises(wire.WireError):
            wire.reply(1, 0, b"\0" * 20)

    def test_keymap_notify_is_the_one_event_whose_sequence_is_not_patched(self):
        """Code 11 has no sequence field: bytes 1..31 are the keymap, and
        patching bytes 2-3 would corrupt two of them (recon/wire.md 3.1)."""
        keymap = bytes([11]) + bytes(range(31))
        self.assertEqual(wire.event_seq(keymap, 0xBEEF), keymap)
        other = bytes([28, 0, 0, 0]) + b"\0" * 28
        self.assertEqual(struct.unpack_from("<H", wire.event_seq(other, 0xBEEF), 2)[0],
                         0xBEEF)


class Substitutes(unittest.TestCase):
    def test_the_two_substitute_requests_are_the_bytes_they_claim(self):
        """`NoOperation` is opcode 127 and `GetInputFocus` is 43, each one word
        long -- the two shapes recon/wire.md 3.3 ran against real clients."""
        self.assertEqual(wire.NOOP, bytes([127, 0, 1, 0]))
        self.assertEqual(wire.split_request(wire.NOOP, False), (4, 127, 0))
        self.assertEqual(wire.GET_INPUT_FOCUS, bytes([43, 0, 1, 0]))
        self.assertEqual(wire.split_request(wire.GET_INPUT_FOCUS, False), (4, 43, 0))


class ExtensionNames(unittest.TestCase):
    """policy.CORE_NAMES and policy.EXT_NAMES against the xcb protocol
    descriptions in /usr/share/xcb (xcb-proto 1.17.0 on this box).

    The tables' only job is the XW11_DEBUG log, so a wrong row is invisible
    until the one moment someone reads the log to find out what a client sent.
    Two rows were wrong when this file was first written -- XKEYBOARD minor 11
    was called SelectEvents (11 is SetCompatMap, SelectEvents is 1) and
    XINERAMA had QueryScreens and IsActive swapped -- which is why the
    comparison is a test and not a reading."""

    XML = "/usr/share/xcb"
    FILES = {"BIG-REQUESTS": "bigreq.xml", "XTEST": "xtest.xml",
             "XKEYBOARD": "xkb.xml", "Generic Event Extension": "ge.xml",
             "XINERAMA": "xinerama.xml", "RANDR": "randr.xml",
             "DRI3": "dri3.xml"}

    def requests(self, name):
        """`{opcode: name}` out of one xcb XML file. The product stays stdlib
        and never reads these; the test may, and xml.etree is stdlib too."""
        root = ElementTree.parse(os.path.join(self.XML, name)).getroot()
        return {int(e.get("opcode")): e.get("name") for e in root.findall("request")}

    def setUp(self):
        if not os.path.isdir(self.XML):
            self.skipTest("no /usr/share/xcb on this box (xcb-proto not installed)")
        for name in ["xproto.xml"] + sorted(set(self.FILES.values())):
            if not os.path.exists(os.path.join(self.XML, name)):
                self.skipTest("no %s" % name)

    def test_every_core_opcode_is_the_name_xproto_xml_gives_it(self):
        """120 rows, 120 requests in xproto.xml, and the names agree."""
        xml = self.requests("xproto.xml")
        wrong = {op: (ours, xml.get(op)) for op, ours in policy.CORE_NAMES.items()
                 if xml.get(op) != ours}
        self.assertEqual(wrong, {})
        self.assertEqual(len(policy.CORE_NAMES), len(xml))

    def test_every_extension_minor_is_the_name_its_own_xml_gives_it(self):
        """Each EXT_NAMES row against the extension's own description: minors
        this table does not carry are fine, minors it carries under another
        request's name are not."""
        wrong = {}
        for ext, table in policy.EXT_NAMES.items():
            self.assertIn(ext, self.FILES, "no xcb file mapped for %s" % ext)
            xml = self.requests(self.FILES[ext])
            for minor, ours in table.items():
                if xml.get(minor) != ours:
                    wrong["%s.%d" % (ext, minor)] = (ours, xml.get(minor))
        self.assertEqual(wrong, {})

    def test_the_comparison_would_catch_the_two_rows_that_were_wrong(self):
        """The negative twin: the pre-2026-09-10 XKEYBOARD and XINERAMA rows,
        fed to the same comparison, come back named."""
        xml = self.requests("xkb.xml")
        self.assertNotEqual(xml.get(11), "SelectEvents")
        self.assertEqual(xml.get(11), "SetCompatMap")
        self.assertNotEqual(xml.get(16), "LatchLockState")
        xml = self.requests("xinerama.xml")
        self.assertNotEqual(xml.get(4), "QueryScreens")
        self.assertEqual(xml.get(4), "IsActive")


class Py310Syntax(unittest.TestCase):
    """R12: pyproject.toml declares `>=3.10` and Arch/NixOS pythons are not
    measured here, so nothing in xw11 may need 3.11."""

    def files(self):
        d = os.path.join(ROOT, "xw11")
        return [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".py")]

    def test_the_package_is_there_to_scan(self):
        self.assertGreaterEqual(len(self.files()), 7)

    def test_no_module_uses_anything_newer_than_3_10(self):
        bad = {}
        for path in self.files():
            found = py310_offenders(path)
            if found:
                bad[os.path.basename(path)] = found
        self.assertEqual(bad, {})

    def test_the_scanner_finds_a_planted_one(self):
        """The negative twin: without this, a scanner that walked nothing would
        pass the test above for ever. `except*` is planted only where the
        running interpreter can parse it -- on the 3.10 floor the source itself
        is a SyntaxError, which is the check by another route."""
        src = ("import tomllib\n\n\ndef f(x):\n"
               "    match x:\n        case 1:\n            return 2\n")
        if sys.version_info >= (3, 11):
            src += ("\n\ndef g():\n    try:\n        pass\n"
                    "    except* ValueError:\n        pass\n")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "planted.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(src)
            found = py310_offenders(path)
        self.assertIn("import tomllib", found)
        self.assertTrue(any(s.startswith("match at line") for s in found), found)
        if sys.version_info >= (3, 11):
            self.assertTrue(any(s.startswith("except* at line") for s in found), found)


if __name__ == "__main__":
    unittest.main()
