"""warandr parser tests: real ``xrandr --query``/``--verbose`` bytes captured
under Xvfb (one screen), a realistic xrandr 1.5.4 laptop capture with a
disconnected output, a rotated one, an off one and an X11 ``--scale``
transform, and wxrandr's own renderers over synthetic 1-4 output layouts
(rotation, reflection, scale, inactive, primary).  No display needed."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from warandr import xrandr_parse as xp
from warandr.model import Layout
from hacks.display import core

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

# -- Xvfb 21.1.12, xrandr 1.5.2 (Ubuntu 24.04), `Xvfb :91 -screen 0
#    1280x1024x24`: no rotations list, a 0.00 rate, verbose "normal (normal)"
XVFB_QUERY = """\
Screen 0: minimum 1 x 1, current 1280 x 1024, maximum 1280 x 1024
screen connected 1280x1024+0+0 0mm x 0mm
   1280x1024      0.00* 
"""

XVFB_VERBOSE = """\
Screen 0: minimum 1 x 1, current 1280 x 1024, maximum 1280 x 1024
screen connected 1280x1024+0+0 (0x3a) normal (normal) 0mm x 0mm
\tIdentifier: 0x3c
\tTimestamp:  10200530
\tSubpixel:   unknown
\tGamma:      1.0:1.0:1.0
\tBrightness: 0.0
\tClones:    
\tCRTC:       0
\tCRTCs:      0
\tTransform:  1.000000 0.000000 0.000000
\t            0.000000 1.000000 0.000000
\t            0.000000 0.000000 1.000000
\t           filter: 
\tnon-desktop: 0 
\t\tsupported: 0, 1
  1280x1024 (0x3a)  0.000MHz *current
        h: width  1280 start    0 end    0 total    0 skew    0 clock   0.00KHz
        v: height 1024 start    0 end    0 total    0           clock   0.00Hz
"""

# -- xrandr 1.5.4 shape on a laptop with a docked portrait monitor, a
#    disconnected HDMI port and a connected-but-off DP-2
LAPTOP_QUERY = """\
Screen 0: minimum 8 x 8, current 3000 x 1920, maximum 32767 x 32767
eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 344mm x 194mm
   1920x1080     60.02*+  60.01    59.97    59.96    59.93  
   1680x1050     59.95    59.88  
   1280x720      60.00    59.99    59.86    59.74  
HDMI-1 disconnected (normal left inverted right x axis y axis)
DP-1 connected 1080x1920+1920+0 left (normal left inverted right x axis y axis) 527mm x 296mm
   1920x1080     60.00*+  50.00    59.94  
   1280x1024     75.02    60.02  
   1024x768      75.03    60.00  
DP-2 connected (normal left inverted right x axis y axis)
   2560x1440     59.95 +
   1920x1080     60.00  
"""

LAPTOP_VERBOSE = """\
Screen 0: minimum 8 x 8, current 4800 x 1920, maximum 32767 x 32767
eDP-1 connected primary 2880x1620+0+0 (0x47) normal (normal left inverted right x axis y axis) 344mm x 194mm
\tIdentifier: 0x42
\tTimestamp:  27059
\tSubpixel:   unknown
\tGamma:      1.0:1.0:1.0
\tBrightness: 1.0
\tClones:    
\tCRTC:       0
\tCRTCs:      0 1 2
\tTransform:  1.500000 0.000000 0.000000
\t            0.000000 1.500000 0.000000
\t            0.000000 0.000000 1.000000
\t           filter: bilinear
\tEDID: 
\t\t00ffffffffffff0006af3d5700000000
\t\t001c0104a51f11780238e5985e578f28
\t\t1e505400000001010101010101010101
\tscaling mode: Full aspect 
\t\tsupported: Full, Center, Full aspect
\tnon-desktop: 0 
\t\tsupported: 0, 1
  1920x1080 (0x47) 138.700MHz +HSync -VSync *current +preferred
        h: width  1920 start 1968 end 2000 total 2080 skew    0 clock  66.68KHz
        v: height 1080 start 1083 end 1088 total 1111           clock  60.02Hz
  1920x1080 (0x48) 148.500MHz +HSync +VSync
        h: width  1920 start 2008 end 2052 total 2200 skew    0 clock  67.50KHz
        v: height 1080 start 1084 end 1089 total 1125           clock  60.00Hz
  1280x720 (0x49)  74.250MHz +HSync +VSync
        h: width  1280 start 1390 end 1430 total 1650 skew    0 clock  45.00KHz
        v: height  720 start  725 end  730 total  750           clock  60.00Hz
HDMI-1 disconnected (normal left inverted right x axis y axis)
\tIdentifier: 0x43
\tTimestamp:  27059
\tSubpixel:   unknown
\tClones:    
\tCRTCs:      0 1 2
\tTransform:  1.000000 0.000000 0.000000
\t            0.000000 1.000000 0.000000
\t            0.000000 0.000000 1.000000
\t           filter: 
\tnon-desktop: 0 
\t\tsupported: 0, 1
DP-1 connected 1080x1920+2880+0 (0x48) left X axis (normal left inverted right x axis y axis) 527mm x 296mm
\tIdentifier: 0x44
\tTimestamp:  27059
\tSubpixel:   unknown
\tGamma:      1.0:1.0:1.0
\tBrightness: 1.0
\tClones:    
\tCRTC:       1
\tCRTCs:      0 1 2
\tTransform:  1.000000 0.000000 0.000000
\t            0.000000 1.000000 0.000000
\t            0.000000 0.000000 1.000000
\t           filter: 
\tnon-desktop: 0 
\t\tsupported: 0, 1
  1920x1080 (0x48) 148.500MHz +HSync +VSync *current +preferred
        h: width  1920 start 2008 end 2052 total 2200 skew    0 clock  67.50KHz
        v: height 1080 start 1084 end 1089 total 1125           clock  60.00Hz
  fancy (0x4a) 74.500MHz -HSync +VSync
        h: width  1280 start 1344 end 1472 total 1664 skew    0 clock  44.77KHz
        v: height  720 start  723 end  728 total  748           clock  59.86Hz
DP-2 connected (normal left inverted right x axis y axis)
\tIdentifier: 0x45
\tTimestamp:  27059
\tSubpixel:   unknown
\tClones:    
\tCRTCs:      0 1 2
\tTransform:  1.000000 0.000000 0.000000
\t            0.000000 1.000000 0.000000
\t            0.000000 0.000000 1.000000
\t           filter: 
\tnon-desktop: 0 
\t\tsupported: 0, 1
  2560x1440 (0x4b) 241.500MHz +HSync -VSync +preferred
        h: width  2560 start 2608 end 2640 total 2720 skew    0 clock  88.79KHz
        v: height 1440 start 1443 end 1448 total 1481           clock  59.95Hz
"""


def wx_output(name, w, h, x=0, y=0, active=True, transform="normal",
              scale=1.0, modes=None, ident=0x40):
    o = core.OutputState(name=name, active=active, x=x, y=y, scale=scale,
                         transform=transform, ident=ident)
    if modes is None:
        modes = [(w, h, 60000)]
    o.modes = [core.Mode(w=mw, h=mh, refresh_mhz=mr) for mw, mh, mr in modes]
    o.modes[0].preferred = True
    if active:
        o.current = o.modes[0]
        o.w, o.h = core.logical_size(w, h, transform, scale)
    return o


def wx_state(primary=None):
    st = core.State("test", path=os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "warandr-parse-test-%d.json"
        % os.getpid()))
    st.primary = primary
    return st


def wx_render(outputs, primary=None, verbose=False):
    return "\n".join(core.render_query(outputs, wx_state(primary),
                                       verbose=verbose)) + "\n"


class ScreenLine(unittest.TestCase):
    def test_xvfb(self):
        s = xp.parse(XVFB_QUERY)
        self.assertEqual((s.number, s.min, s.current, s.max),
                         (0, (1, 1), (1280, 1024), (1280, 1024)))

    def test_laptop(self):
        s = xp.parse(LAPTOP_QUERY)
        self.assertEqual(s.max, (32767, 32767))
        self.assertEqual(s.current, (3000, 1920))

    def test_malformed(self):
        with self.assertRaises(xp.ParseError):
            xp.parse("Screen 0: bogus\n")
        with self.assertRaises(xp.ParseError):
            xp.parse("Screen 0: minimum 8 x 8, current 1 x 1, maximum 2 x 2\n"
                     "DP-1\n")


class NoCurrentRate(unittest.TestCase):
    """An active output whose mode table carries no ``*``.  Real xrandr always
    stars the running mode (the repo's own Xvfb capture above prints
    ``0.00*``), but a hand-written or truncated listing need not, and the
    parser derives nothing from the header on its own: ``ParsedOutput.current``
    (xrandr_parse.py:98) reads the ``*`` flag and nothing else.  The gap is
    filled one level up, by ``Layout.from_screen`` (model.py:172-183), and
    tests/test_warandr_model.py pins which mode it picks."""

    TEXT = ("Screen 0: minimum 16 x 16, current 1280 x 720, "
            "maximum 32767 x 32767\n"
            "VGA-0 connected 1280x720+0+0 (normal left inverted right x axis "
            "y axis) 338mm x 190mm\n"
            "   1280x720      60.00\n")

    def test_active_but_no_current_mode(self):
        s = xp.parse(self.TEXT)
        (o,) = s.outputs
        self.assertTrue(o.connected and o.active)
        self.assertEqual((o.w, o.h, o.x, o.y), (1280, 720, 0, 0))
        self.assertEqual(o.current, (None, None))
        (m,) = o.modes
        self.assertEqual((m.name, m.w, m.h), ("1280x720", 1280, 720))
        self.assertFalse(m.rates[0].current)

    def test_the_model_still_lands_on_a_mode(self):
        lay = Layout.from_screen(xp.parse(self.TEXT))
        self.assertEqual(lay.get("VGA-0").mode.name, "1280x720")


class XvfbCapture(unittest.TestCase):
    def test_query(self):
        s = xp.parse(XVFB_QUERY)
        self.assertFalse(s.verbose)
        (o,) = s.outputs
        self.assertEqual(o.name, "screen")
        self.assertTrue(o.connected and o.active)
        self.assertEqual((o.w, o.h, o.x, o.y), (1280, 1024, 0, 0))
        self.assertEqual(o.rotation, "normal")
        self.assertEqual(o.rotations, {"normal"})   # no rotations list
        self.assertEqual((o.mm_w, o.mm_h), (0, 0))
        (m,) = o.modes
        self.assertEqual((m.name, m.w, m.h), ("1280x1024", 1280, 1024))
        self.assertEqual(len(m.rates), 1)
        self.assertEqual(m.rates[0].hz, 0.0)
        self.assertTrue(m.rates[0].current)
        self.assertFalse(m.rates[0].preferred)

    def test_verbose(self):
        s = xp.parse(XVFB_VERBOSE)
        self.assertTrue(s.verbose)
        (o,) = s.outputs
        self.assertEqual(o.mode_xid, 0x3a)
        self.assertEqual(o.ident, 0x3c)
        self.assertEqual(o.crtc, 0)
        self.assertEqual(o.transform, (1.0, 1.0))
        self.assertEqual(o.rotation, "normal")
        (m,) = o.modes
        self.assertEqual((m.w, m.h), (1280, 1024))
        self.assertEqual(m.xids, [0x3a])
        self.assertTrue(m.rates[0].current)

    def test_layout_command(self):
        lay = Layout.from_screen(xp.parse(XVFB_VERBOSE))
        self.assertEqual(lay.args(), ["--output", "screen", "--mode",
                                      "1280x1024", "--pos", "0x0", "--rotate",
                                      "normal"])
        self.assertEqual(lay.screen_max, (1280, 1024))


class LaptopCapture(unittest.TestCase):
    def test_query_outputs(self):
        s = xp.parse(LAPTOP_QUERY)
        names = [o.name for o in s.outputs]
        self.assertEqual(names, ["eDP-1", "HDMI-1", "DP-1", "DP-2"])
        edp, hdmi, dp1, dp2 = s.outputs
        self.assertTrue(edp.primary and edp.active)
        self.assertEqual(edp.rotations, set(xp.ROTATIONS))
        self.assertEqual(edp.reflections, {"x", "y"})
        self.assertEqual((edp.mm_w, edp.mm_h), (344, 194))
        self.assertFalse(hdmi.connected)
        self.assertFalse(hdmi.active)
        self.assertEqual(hdmi.modes, [])
        self.assertTrue(dp1.active)
        self.assertEqual(dp1.rotation, "left")
        self.assertEqual((dp1.w, dp1.h, dp1.x, dp1.y), (1080, 1920, 1920, 0))
        self.assertTrue(dp2.connected)
        self.assertFalse(dp2.active)
        self.assertEqual(dp2.current, (None, None))

    def test_query_rates(self):
        s = xp.parse(LAPTOP_QUERY)
        edp = s.get("eDP-1")
        m = edp.mode_named("1920x1080")
        self.assertEqual([r.hz for r in m.rates],
                         [60.02, 60.01, 59.97, 59.96, 59.93])
        self.assertEqual([r.current for r in m.rates],
                         [True, False, False, False, False])
        self.assertEqual([r.preferred for r in m.rates],
                         [True, False, False, False, False])
        self.assertEqual([r.hz for r in edp.mode_named("1280x720").rates],
                         [60.00, 59.99, 59.86, 59.74])
        dp2 = s.get("DP-2")
        r = dp2.mode_named("2560x1440").rates[0]
        self.assertTrue(r.preferred)
        self.assertFalse(r.current)

    def test_verbose_scale_reflection_custom_mode(self):
        s = xp.parse(LAPTOP_VERBOSE)
        edp = s.get("eDP-1")
        self.assertEqual(edp.transform, (1.5, 1.5))
        self.assertEqual((edp.w, edp.h), (2880, 1620))
        self.assertEqual(edp.crtc, 0)
        self.assertEqual(edp.ident, 0x42)
        m, r = edp.current
        self.assertEqual(m.name, "1920x1080")
        self.assertEqual(r.hz, 60.02)
        self.assertTrue(r.preferred)
        # the two 1920x1080 modelines group into one name with two rates
        self.assertEqual([x.hz for x in m.rates], [60.02, 60.00])
        self.assertEqual(m.xids, [0x47, 0x48])
        dp1 = s.get("DP-1")
        self.assertEqual(dp1.rotation, "left")
        self.assertEqual(dp1.reflection, "x")
        fancy = dp1.mode_named("fancy")
        self.assertEqual((fancy.w, fancy.h), (1280, 720))
        self.assertEqual(fancy.rates[0].hz, 59.86)
        hdmi = s.get("HDMI-1")
        self.assertFalse(hdmi.connected)
        self.assertEqual(hdmi.transform, (1.0, 1.0))

    def test_layout_from_verbose(self):
        lay = Layout.from_screen(xp.parse(LAPTOP_VERBOSE))
        edp = lay.get("eDP-1")
        self.assertEqual(edp.scale, 1.5)
        self.assertEqual(edp.size(), (2880, 1620))   # X11: fb scale grows
        self.assertEqual(edp.rate, 60.02)
        dp1 = lay.get("DP-1")
        self.assertEqual(dp1.size(), (1080, 1920))
        self.assertEqual(dp1.reflection, "x")
        self.assertEqual(dp1.mode_named("fancy").label, "fancy (1280x720)")
        # the X11 transform is written back as --scale: what is drawn is
        # what xrandr is told (arandr would leave it implicit)
        self.assertEqual(lay.args(), [
            "--output", "eDP-1", "--primary", "--mode", "1920x1080",
            "--pos", "0x0", "--rotate", "normal", "--scale", "1.5x1.5",
            "--output", "HDMI-1", "--off",
            "--output", "DP-1", "--mode", "1920x1080", "--pos", "2880x0",
            "--rotate", "left", "--reflect", "x",
            "--output", "DP-2", "--off"])

    def test_layout_from_query(self):
        lay = Layout.from_screen(xp.parse(LAPTOP_QUERY))
        self.assertFalse(lay.get("HDMI-1").connected)
        self.assertFalse(lay.get("HDMI-1").active)
        self.assertTrue(lay.get("DP-2").connected)
        self.assertFalse(lay.get("DP-2").active)
        self.assertEqual(lay.get("DP-2").preferred_mode().name, "2560x1440")
        self.assertEqual(lay.get("eDP-1").scale, 1.0)


class WxrandrRenders(unittest.TestCase):
    """Text straight from hacks.display.core's byte-parity renderers."""

    def test_one_output(self):
        text = wx_render([wx_output("HEADLESS-1", 1280, 720)])
        s = xp.parse(text)
        (o,) = s.outputs
        self.assertEqual((o.w, o.h), (1280, 720))
        self.assertEqual(o.rotations, set(xp.ROTATIONS))
        self.assertEqual(o.mode_named("1280x720").rates[0].hz, 60.0)
        lay = Layout.from_screen(s, hidpi=True, command_word="wxrandr")
        self.assertEqual(lay.command_line(),
                         "wxrandr --output HEADLESS-1 --mode 1280x720 "
                         "--pos 0x0 --rotate normal")

    def test_two_outputs_rotation_and_scale(self):
        outs = [wx_output("DP-1", 1920, 1080, scale=1.5),
                wx_output("DP-2", 1920, 1080, x=1280, transform="270")]
        s = xp.parse(wx_render(outs, primary="DP-2"))
        a, b = s.outputs
        self.assertEqual((a.w, a.h), (1280, 720))      # 1920/1.5 logical
        self.assertEqual(b.rotation, "left")
        self.assertEqual((b.w, b.h, b.x), (1080, 1920, 1280))
        self.assertTrue(b.primary)
        lay = Layout.from_screen(s, hidpi=True, command_word="wxrandr")
        self.assertEqual(lay.get("DP-1").scale, 1.5)
        self.assertEqual(lay.get("DP-1").size(), (1280, 720))
        self.assertEqual(lay.get("DP-2").size(), (1080, 1920))
        self.assertEqual(lay.args(), [
            "--output", "DP-1", "--mode", "1920x1080", "--pos", "0x0",
            "--rotate", "normal", "--scale", "1.5x1.5",
            "--output", "DP-2", "--primary", "--mode", "1920x1080",
            "--pos", "1280x0", "--rotate", "left"])

    def test_three_outputs_l_shape_verbose(self):
        outs = [wx_output("HEADLESS-1", 1280, 720, ident=1),
                wx_output("HEADLESS-2", 1024, 768, x=1280, ident=2),
                wx_output("HEADLESS-3", 800, 600, y=720, ident=3)]
        s = xp.parse(wx_render(outs, verbose=True))
        self.assertTrue(s.verbose)
        self.assertEqual(s.current, (2304, 1320))
        self.assertEqual([(o.x, o.y) for o in s.outputs],
                         [(0, 0), (1280, 0), (0, 720)])
        for o in s.outputs:
            self.assertEqual(o.transform, (1.0, 1.0))
            self.assertIsNotNone(o.ident)
            self.assertEqual(len(o.modes), 1)
            self.assertEqual(o.modes[0].rates[0].hz, 60.0)
            self.assertTrue(o.modes[0].rates[0].current)
            self.assertTrue(o.modes[0].rates[0].preferred)
        self.assertEqual([o.crtc for o in s.outputs], [0, 1, 2])

    def test_four_outputs_inactive_reflection_rates(self):
        outs = [wx_output("A", 1920, 1080, transform="flipped",
                          modes=[(1920, 1080, 60000), (1920, 1080, 50000),
                                 (1280, 720, 59940)]),
                wx_output("B", 1280, 1024, x=1920),
                wx_output("C", 1280, 720, active=False),
                wx_output("D", 800, 600, x=1920, y=1024, scale=2.0)]
        s = xp.parse(wx_render(outs, primary="A"))
        a, b, c, d = s.outputs
        self.assertEqual(a.reflection, "x")
        self.assertEqual(a.rotation, "normal")
        m = a.mode_named("1920x1080")
        self.assertEqual([r.hz for r in m.rates], [60.0, 50.0])
        self.assertEqual([r.current for r in m.rates], [True, False])
        self.assertEqual(a.mode_named("1280x720").rates[0].hz, 59.94)
        self.assertTrue(c.connected)
        self.assertFalse(c.active)
        self.assertEqual(len(c.modes), 1)
        self.assertEqual((d.w, d.h), (400, 300))
        lay = Layout.from_screen(s, hidpi=True)
        self.assertEqual(lay.get("D").scale, 2.0)
        self.assertEqual(lay.get("A").reflection, "x")
        self.assertFalse(lay.get("C").active)
        args = lay.args()
        d_stanza = args[args.index("D"):]
        self.assertEqual(d_stanza, ["D", "--mode", "800x600", "--pos",
                                    "1920x1024", "--rotate", "normal",
                                    "--scale", "2x2"])
        self.assertEqual(args[args.index("C"):args.index("C") + 2],
                         ["C", "--off"])

    def test_verbose_and_query_agree(self):
        outs = [wx_output("A", 1920, 1080,
                          modes=[(1920, 1080, 60000), (1280, 720, 59940)]),
                wx_output("B", 1280, 1024, x=1920, transform="90")]
        q = Layout.from_screen(xp.parse(wx_render(outs)), hidpi=True)
        v = Layout.from_screen(xp.parse(wx_render(outs, verbose=True)),
                               hidpi=True)
        self.assertEqual(q.args(), v.args())
        self.assertEqual(q.get("B").rotation, "right")
        self.assertEqual([m.rates for m in q.get("A").modes],
                         [m.rates for m in v.get("A").modes])


class Rows(unittest.TestCase):
    def test_query_rate_flags(self):
        o = xp.ParsedOutput("X")
        xp._parse_query_row(o, "   1920x1080     60.00*+  50.00    59.94 +")
        m = o.modes[0]
        self.assertEqual([(r.hz, r.current, r.preferred) for r in m.rates],
                         [(60.0, True, True), (50.0, False, False),
                          (59.94, False, True)])

    def test_query_row_stripped(self):
        o = xp.ParsedOutput("X")
        xp._parse_query_row(o, "   1280x1024      0.00*")
        self.assertEqual(o.modes[0].rates[0].hz, 0.0)
        self.assertTrue(o.modes[0].rates[0].current)

    def test_long_mode_name(self):
        o = xp.ParsedOutput("X")
        xp._parse_query_row(o, "   3840x2160_30.00   30.00  ")
        self.assertEqual(o.modes[0].name, "3840x2160_30.00")
        self.assertEqual((o.modes[0].w, o.modes[0].h), (3840, 2160))
        self.assertEqual(o.modes[0].rates[0].hz, 30.0)

    def test_mode_name_with_space(self):
        # `xrandr --newmode "my mode" ...` is legal; the query row pads the
        # name to 12 columns, the rates start at the first ` %6.2f`
        o = xp.ParsedOutput("X")
        xp._parse_query_row(o, "   my mode       75.28    60.00*+")
        self.assertEqual(o.modes[0].name, "my mode")
        self.assertEqual([r.hz for r in o.modes[0].rates], [75.28, 60.0])
        self.assertTrue(o.modes[0].rates[1].current)
        xp._parse_query_row(o, "   1920x1080     60.00*+")
        self.assertEqual(o.modes[1].name, "1920x1080")

    def test_verbose_mode_name_with_space(self):
        # a --newmode name with a space used to fall through to the query
        # row parser, inventing a mode and re-sizing the previous one
        text = LAPTOP_VERBOSE.replace(
            "  fancy (0x4a) 74.500MHz", "  my mode (0x4a) 74.500MHz")
        s = xp.parse(text)
        dp1 = s.get("DP-1")
        self.assertEqual([m.name for m in dp1.modes], ["1920x1080", "my mode"])
        self.assertEqual((dp1.modes[0].w, dp1.modes[0].h), (1920, 1080))
        self.assertEqual(dp1.modes[0].rates[0].hz, 60.0)
        m = dp1.mode_named("my mode")
        self.assertEqual((m.w, m.h, m.rates[0].hz), (1280, 720, 59.86))
        self.assertEqual(m.xids, [0x4a])
        lay = Layout.from_screen(s)
        self.assertEqual(lay.get("DP-1").mode_named("my mode").label,
                         "my mode (1280x720)")
        # and an unparseable 2-space line inside --verbose is ignored, not
        # taken for a query row
        text2 = text.replace("  my mode (0x4a) 74.500MHz -HSync +VSync\n",
                             "  my mode (0x4a) 74.500MHz -HSync +VSync\n"
                             "  junk line without an xid\n")
        s2 = xp.parse(text2)
        self.assertEqual([m.name for m in s2.get("DP-1").modes],
                         ["1920x1080", "my mode"])

    def test_header_variants(self):
        o = xp._parse_header("DP-1 unknown connection (normal left inverted "
                             "right x axis y axis)")
        self.assertEqual(o.status, "unknown connection")
        self.assertTrue(o.connected)
        self.assertFalse(o.active)
        o = xp._parse_header("DP-1 connected 1920x1080+0+0 inverted X and "
                             "Y axis (normal left inverted right x axis y "
                             "axis) 0mm x 0mm")
        self.assertEqual((o.rotation, o.reflection), ("inverted", "xy"))
        o = xp._parse_header("DP-1 connected 1920x1080+0+0 (0x47) right "
                             "Y axis (normal left inverted right x axis y "
                             "axis) 1mm x 2mm")
        self.assertEqual((o.rotation, o.reflection), ("right", "y"))
        self.assertEqual((o.mm_w, o.mm_h), (1, 2))
        o = xp._parse_header("VGA-1 connected 1024x768+0+0 panning "
                             "1024x768+0+0 (normal left inverted right x "
                             "axis y axis) 0mm x 0mm")
        self.assertEqual((o.w, o.h), (1024, 768))
        with self.assertRaises(xp.ParseError):
            xp._parse_header("DP-1 sideways")


if __name__ == "__main__":
    unittest.main()
