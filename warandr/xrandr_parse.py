"""Parser for the text xrandr prints (``--query`` and ``--verbose``, xrandr 1.5.x format — the same bytes
wxrandr renders).

One parser handles both forms: the output header line is common (``--verbose`` adds the mode xid and always
prints the rotation word), the mode section is either the grouped rate table (query) or per-mode modelines
(verbose), and the tab-indented verbose block carries the transform matrix that reveals an X11 ``--scale``.
"""

import re

ROTATIONS = ("normal", "left", "inverted", "right")
REFLECTION_WORDS = {"": "normal", "X axis": "x", "Y axis": "y", "X and Y axis": "xy"}

_SCREEN_RE = re.compile(
    r"^Screen (\d+): minimum (\d+) x (\d+), current (\d+) x (\d+), "
    r"maximum (\d+) x (\d+)")
_GEOM_RE = re.compile(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")
_XID_RE = re.compile(r"^\(0x[0-9a-fA-F]+\)$")
_MM_RE = re.compile(r"^(\d+)mm$")
_QUERY_RATE_RE = re.compile(r"\s*(\d+\.\d+)([* ]?)([+ ]?)")
_VERBOSE_MODE_RE = re.compile(          # names may contain spaces (--newmode)
    r"^  (.+?) \((0x[0-9a-fA-F]+)\)\s+([\d.]+)MHz(.*)$")
_FIRST_RATE_RE = re.compile(r"\s(?=\d+\.\d+(?:[* +]|$))")
_H_RE = re.compile(r"^h: width\s+(\d+) ")
_V_RE = re.compile(r"^v: height\s+(\d+) .*clock\s+([\d.]+)Hz")
_TRANSFORM_RE = re.compile(r"^Transform:\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)")
_NUM3_RE = re.compile(r"^([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)$")


class ParseError(Exception):
    pass


class Rate:
    __slots__ = ("hz", "current", "preferred")

    def __init__(self, hz, current=False, preferred=False):
        self.hz = hz
        self.current = current
        self.preferred = preferred

    def __repr__(self):
        return "Rate(%.2f%s%s)" % (self.hz, "*" if self.current else "", "+" if self.preferred else "")


class ParsedMode:
    """One mode *name* with all its refresh rates (xrandr groups the query
    table by name; --verbose lists one modeline per rate)."""

    def __init__(self, name, w, h):
        self.name = name
        self.w = w
        self.h = h
        self.rates = []          # list[Rate], server order
        self.xids = []

    def __repr__(self):
        return "ParsedMode(%s %dx%d %r)" % (self.name, self.w, self.h, self.rates)


class ParsedOutput:
    def __init__(self, name):
        self.name = name
        self.status = "disconnected"
        self.primary = False
        self.active = False
        self.w = self.h = self.x = self.y = 0     # as printed (transformed)
        self.mode_xid = None
        self.rotation = "normal"
        self.reflection = "normal"
        self.rotations = set()       # allowed, from the "(normal left ...)"
        self.reflections = set()
        self.mm_w = self.mm_h = 0
        self.modes = []              # list[ParsedMode]
        self.ident = None
        self.crtc = None
        self.transform = None        # (sx, sy) from --verbose
        self._matrix = []

    @property
    def connected(self):
        return self.status != "disconnected"

    def mode_named(self, name):
        for m in self.modes:
            if m.name == name:
                return m
        return None

    def _mode(self, name, w, h):
        m = self.mode_named(name)
        if m is None:
            m = ParsedMode(name, w, h)
            self.modes.append(m)
        return m

    @property
    def current(self):
        """(mode, rate) flagged current, or (None, None)."""
        for m in self.modes:
            for r in m.rates:
                if r.current:
                    return m, r
        return None, None

    def __repr__(self):
        return "<ParsedOutput %s %s%s %dx%d+%d+%d %s %d modes>" % (
            self.name, self.status, " primary" if self.primary else "",
            self.w, self.h, self.x, self.y, self.rotation, len(self.modes))


class Screen:
    def __init__(self):
        self.number = 0
        self.min = (0, 0)
        self.current = (0, 0)
        self.max = (0, 0)
        self.outputs = []            # server order
        self.verbose = False

    def get(self, name):
        for o in self.outputs:
            if o.name == name:
                return o
        return None

    def __repr__(self):
        return "<Screen %r current %r max %r outputs %r>" % (
            self.number, self.current, self.max, self.outputs)


def _parse_header(line):
    """``NAME STATUS [primary] [WxH+X+Y [(0xID)] [ROT [REFL]]] [(ROTS)]
    [MMmm x MMmm] [panning ...]``."""
    parts = line.split()
    if len(parts) < 2:
        raise ParseError("malformed output line: %r" % line)
    o = ParsedOutput(parts[0])
    i = 1
    if parts[1] == "unknown" and len(parts) > 2 and parts[2] == "connection":
        o.status = "unknown connection"
        i = 3
    elif parts[1] in ("connected", "disconnected"):
        o.status = parts[1]
        i = 2
    else:
        raise ParseError("malformed output line: %r" % line)
    seen_geometry = False
    while i < len(parts):
        tok = parts[i]
        if tok == "primary":
            o.primary = True
        elif tok.startswith("(") and not _XID_RE.match(tok):
            # "(normal left inverted right x axis y axis)" — possibly "(normal)"
            words = []
            while i < len(parts):
                words.append(parts[i].strip("()"))
                if parts[i].endswith(")"):
                    break
                i += 1
            text = " ".join(w for w in words if w)
            for r in ROTATIONS:
                if re.search(r"\b%s\b" % r, text):
                    o.rotations.add(r)
            if "x axis" in text:
                o.reflections.add("x")
            if "y axis" in text:
                o.reflections.add("y")
        elif _XID_RE.match(tok):
            o.mode_xid = int(tok.strip("()"), 16)     # --verbose only
        elif not seen_geometry and _GEOM_RE.match(tok):
            m = _GEOM_RE.match(tok)
            o.w, o.h, o.x, o.y = (int(m.group(k)) for k in (1, 2, 3, 4))
            o.active = True
            seen_geometry = True
        elif seen_geometry and tok in ROTATIONS and o.rotation == "normal" and not o._matrix:
            o.rotation = tok
            # reflection phrase follows the rotation word: "X axis",
            # "Y axis", "X and Y axis"
            refl = []
            j = i + 1
            while j < len(parts) and parts[j] in ("X", "Y", "and", "axis"):
                refl.append(parts[j])
                j += 1
            if refl:
                o.reflection = REFLECTION_WORDS.get(" ".join(refl), "normal")
                i = j - 1
            o._matrix = ["seen-rotation"]
        elif _MM_RE.match(tok):
            mm = int(_MM_RE.match(tok).group(1))
            if i + 2 < len(parts) and parts[i + 1] == "x" and _MM_RE.match(parts[i + 2]):
                o.mm_w = mm
                o.mm_h = int(_MM_RE.match(parts[i + 2]).group(1))
                i += 2
        elif tok in ("panning", "tracking", "border"):
            i += 1  # value follows; irrelevant for the layout
        i += 1
    o._matrix = []
    return o


def parse(text):
    """Parse ``xrandr --query`` / ``--verbose`` text into a Screen."""
    scr = Screen()
    cur = None
    cur_mode = None
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("Screen "):
            m = _SCREEN_RE.match(line)
            if not m:
                raise ParseError("malformed Screen line: %r" % line)
            scr.number = int(m.group(1))
            scr.min = (int(m.group(2)), int(m.group(3)))
            scr.current = (int(m.group(4)), int(m.group(5)))
            scr.max = (int(m.group(6)), int(m.group(7)))
            continue
        if line.startswith("\t"):
            scr.verbose = True
            if cur is not None:
                _parse_detail(cur, line.strip())
            continue
        if line.startswith("  "):
            if cur is None:
                continue
            stripped = line.strip()
            if stripped.startswith("h:") or stripped.startswith("v:"):
                if cur_mode is not None:
                    _parse_hv(cur_mode, stripped)
                continue
            vm = _VERBOSE_MODE_RE.match(line)
            if vm:
                scr.verbose = True
                cur_mode = _parse_verbose_mode(cur, vm)
                continue
            if scr.verbose:
                continue     # never a query row inside --verbose output
            _parse_query_row(cur, line)
            continue
        cur = _parse_header(line)
        cur_mode = None
        if cur.mode_xid is not None:
            scr.verbose = True
        scr.outputs.append(cur)
    for o in scr.outputs:
        _finish(o)
    return scr


def _parse_detail(o, s):
    if s.startswith("Identifier:"):
        try:
            o.ident = int(s.split(":", 1)[1].strip(), 16)
        except ValueError:
            pass
    elif s.startswith("CRTC:"):
        try:
            o.crtc = int(s.split(":", 1)[1].strip())
        except ValueError:
            pass
    elif s.startswith("Transform:"):
        m = _TRANSFORM_RE.match(s)
        if m:
            o._matrix = [tuple(float(m.group(k)) for k in (1, 2, 3))]
    elif o._matrix and len(o._matrix) < 3:
        m = _NUM3_RE.match(s)
        if m:
            o._matrix.append(tuple(float(m.group(k)) for k in (1, 2, 3)))
            if len(o._matrix) == 3:
                o.transform = (o._matrix[0][0], o._matrix[1][1])
        else:
            o._matrix = []


def _parse_query_row(o, line):
    """``   1920x1080     60.00*+  50.00  `` — name padded to 12, then
    `` %6.2f`` + current flag + preferred flag per rate.  The name ends where
    the first rate column starts (a custom name may contain spaces)."""
    body = line[3:]
    m = _FIRST_RATE_RE.search(body)
    if m:
        name, rest = body[:m.start()].strip(), body[m.start():]
    else:
        name, rest = body.strip(), ""
    if not name:
        return
    m = re.match(r"^(\d+)x(\d+)", name)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    else:
        w = h = 0
    mode = o._mode(name, w, h)
    for rm in _QUERY_RATE_RE.finditer(rest):
        mode.rates.append(Rate(float(rm.group(1)), rm.group(2) == "*", rm.group(3) == "+"))


def _parse_verbose_mode(o, vm):
    name = vm.group(1)
    flags = vm.group(4)
    m = re.match(r"^(\d+)x(\d+)", name)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    mode = o._mode(name, w, h)
    rate = Rate(0.0, "*current" in flags, "+preferred" in flags)
    mode.rates.append(rate)
    mode.xids.append(int(vm.group(2), 16))
    return mode


def _parse_hv(mode, s):
    m = _H_RE.match(s)
    if m:
        mode.w = int(m.group(1))
        return
    m = _V_RE.match(s)
    if m:
        mode.h = int(m.group(1))
        if mode.rates:
            mode.rates[-1].hz = float(m.group(2))


def _finish(o):
    """Derive what the header alone can't say."""
    if not o.rotations:
        o.rotations = {"normal"}
    del o._matrix
