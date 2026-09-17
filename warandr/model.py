"""Layout model: outputs with their configuration, arandr's edge snapping, origin normalisation, the xrandr
command line, and the ``#!/bin/sh`` layout-script format arandr reads.

Overlapping outputs are the *backend's* business, not ours: two active outputs may intersect freely (arandr
allows it and X11 has always drawn it), and a layout is refused here only when the backend in use refuses one --
the caller sets ``Layout.overlap_refusal`` to that backend's own sentence, which then becomes the error, so the
user reads whose limit it is."""

import math
import re
import shlex

ROTATIONS = ("normal", "right", "inverted", "left")     # arandr's menu order
REFLECTIONS = ("normal", "x", "y", "xy")
SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 3.0)
COMMAND_WORDS = ("xrandr", "wxrandr")
SHEBANG = "#!/bin/sh"
PLACEHOLDER = "%(xrandr)s"
DEFAULT_TEMPLATE = [SHEBANG, PLACEHOLDER]        # arandr's DEFAULTTEMPLATE


class LayoutError(Exception):
    """A configuration the model refuses (off-screen, unknown output/mode, or an overlap on a backend that
    refuses one) — the caller reverts and tells the user."""


def fmt_rate(hz):
    return ("%.2f" % hz).rstrip("0").rstrip(".")


def fmt_scale(s):
    return "%g" % s


class Mode:
    def __init__(self, name, w, h, rates=(), preferred=None):
        self.name = name
        self.w = w
        self.h = h
        self.rates = list(rates)
        self.preferred = preferred

    def default_rate(self):
        """The rate xrandr picks for ``--mode NAME`` alone: the preferred one,
        else the first listed."""
        if self.preferred is not None:
            return self.preferred
        return self.rates[0] if self.rates else None

    def nearest_rate(self, hz):
        if not self.rates or hz is None:
            return None
        return min(self.rates, key=lambda r: abs(r - hz))

    @property
    def label(self):
        wh = "%dx%d" % (self.w, self.h)
        return self.name if wh in self.name else "%s (%s)" % (self.name, wh)

    def __repr__(self):
        return "Mode(%s)" % self.label


class Output:
    def __init__(self, name, connected=True, modes=(), rotations=None, hidpi=False):
        self.name = name
        self.connected = connected
        self.modes = list(modes)
        self.rotations = set(rotations or ROTATIONS)
        self.hidpi = hidpi
        # configuration (what a layout script sets)
        self.active = False
        self.primary = False
        self.mode = None
        self.rate = None
        self.rotation = "normal"
        self.reflection = "normal"
        self.scale = 1.0
        # the scale the screen was running when it was read: both xrandr and wxrandr keep an existing scale when
        # --scale is not given, so returning to 1 must be said explicitly
        self.screen_scale = 1.0
        self.x = 0
        self.y = 0
        self.mirror_of = None
        # transient drag position (GUI); None when not dragging
        self.tentative = None

    def mode_named(self, name):
        for m in self.modes:
            if m.name == name:
                return m
        return None

    def preferred_mode(self):
        for m in self.modes:
            if m.preferred is not None:
                return m
        return self.modes[0] if self.modes else None

    def size(self):
        """Logical (drawn) size: scale then the rotation swap.  Wayland (wxrandr) scale is a HiDPI factor — the
        compositor truncates ``px / scale``; X11 ``--scale`` is a framebuffer factor, xrandr rounds the
        transformed rectangle outwards."""
        if self.mode is None:
            return (0, 0)
        w, h = self.mode.w, self.mode.h
        if self.scale != 1.0 and self.scale > 0 and math.isfinite(self.scale):
            if self.hidpi:
                w, h = int(w / self.scale), int(h / self.scale)
            else:
                w = int(math.ceil(w * self.scale - 1e-6))
                h = int(math.ceil(h * self.scale - 1e-6))
        if self.rotation in ("left", "right"):
            w, h = h, w
        return (w, h)

    def rect(self):
        w, h = self.size()
        return (self.x, self.y, w, h)

    def __repr__(self):
        return "<Output %s %s %r @%d,%d %s>" % (
            self.name, "on" if self.active else "off", self.mode,
            self.x, self.y, self.rotation)


def _intersects(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


class Layout:
    def __init__(self, hidpi=False, screen_max=(32767, 32767),
                 screen_min=(8, 8), command_word="xrandr",
                 overlap_refusal=None):
        self.hidpi = hidpi
        self.screen_max = screen_max
        self.screen_min = screen_min
        self.command_word = command_word
        #: None when the backend takes overlapping outputs (X11, KWin,
        #: wlroots/sway: all three draw the shared region on both screens);
        #: otherwise the backend's own reason, raised as the LayoutError.
        self.overlap_refusal = overlap_refusal
        self.outputs = []
        self.template = list(DEFAULT_TEMPLATE)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_screen(cls, screen, hidpi=False, command_word="xrandr", overlap_refusal=None):
        """Build a layout from a parsed ``xrandr --query``/``--verbose``."""
        lay = cls(hidpi=hidpi, screen_max=screen.max, screen_min=screen.min,
                  command_word=command_word,
                  overlap_refusal=overlap_refusal)
        for po in screen.outputs:
            modes = []
            for pm in po.modes:
                rates = [r.hz for r in pm.rates]
                pref = None
                for r in pm.rates:
                    if r.preferred:
                        pref = r.hz
                        break
                modes.append(Mode(pm.name, pm.w, pm.h, rates, pref))
            o = Output(po.name, connected=po.connected, modes=modes, rotations=po.rotations, hidpi=hidpi)
            o.active = po.active
            o.primary = po.primary
            o.rotation = po.rotation if po.rotation in ROTATIONS else "normal"
            o.reflection = po.reflection
            o.x, o.y = po.x, po.y
            cm, cr = po.current
            if po.active:
                # ``ParsedOutput.current`` (xrandr_parse.py:98) reports a mode only when some rate carries the
                # literal ``*``, and ``Rate.current`` is set nowhere else — so an active output whose table
                # has no star arrives here as ``(None, None)``.  Real xrandr always stars the running mode and
                # xrandr_parse.py:296/305 read it; the starless shape is a hand-written or truncated listing,
                # or a verbose one whose current rate the parser never saw.  This is the one place that gap is
                # filled, and the rule is ``preferred_mode()`` (model.py:94-98): the first mode carrying
                # ``+``, else ``modes[0]``.  The parser derives nothing of its own; it used to carry a comment
                # promising "the first mode is it", a rule it never applied and which disagrees with this one.
                if cm is None and modes:
                    cm = o.preferred_mode()
                if cm is not None:
                    o.mode = o.mode_named(cm.name)
                    o.rate = cr.hz if cr is not None else o.mode.default_rate()
                    o.scale = _derive_scale(po, o, hidpi)
                    o.screen_scale = o.scale
            lay.outputs.append(o)
        lay._mark_clones()
        return lay

    def _mark_clones(self):
        """An active output sharing its origin with an earlier active one is a clone of it (what ``--same-as``
        produces, whatever the sizes): record it as *Mirror of* so it is drawn as one, follows its target and
        round-trips as ``--same-as``."""
        anchors = []
        for o in self.active_outputs():
            if o.mode is None:
                continue
            for a in anchors:
                if (a.x, a.y) == (o.x, o.y):
                    o.mirror_of = a.name
                    break
            else:
                anchors.append(o)

    def add(self, output):
        output.hidpi = self.hidpi
        self.outputs.append(output)
        return output

    def get(self, name):
        for o in self.outputs:
            if o.name == name:
                return o
        raise LayoutError("Not a known output: %s" % name)

    def has(self, name):
        return any(o.name == name for o in self.outputs)

    def active_outputs(self):
        return [o for o in self.outputs if o.active]

    def names(self):
        return [o.name for o in self.outputs]

    # -- geometry -----------------------------------------------------------

    def bounding_box(self):
        """(x0, y0, x1, y1) over active outputs; zeros when none."""
        act = self.active_outputs()
        if not act:
            return (0, 0, 0, 0)
        rects = [o.rect() for o in act]
        return (min(r[0] for r in rects), min(r[1] for r in rects),
                max(r[0] + r[2] for r in rects),
                max(r[1] + r[3] for r in rects))

    def normalize(self):
        """Shift everything so the layout's top-left corner is (0, 0) — xrandr accepts negative --pos, arandr
        refuses them; we never emit them.  Mirrors follow their targets."""
        self._sync_mirrors()
        x0, y0, _, _ = self.bounding_box()
        if x0 or y0:
            for o in self.active_outputs():
                o.x -= x0
                o.y -= y0

    def _sync_mirrors(self):
        """Mirrors take their target's position.  A script may chain them
        (``A --same-as B --output B --same-as C``): xrandr iterates positions to a fixpoint, we flatten every
        chain onto its root so the model only ever holds one level; a cycle is an error."""
        for o in self.outputs:
            if not o.mirror_of:
                continue
            seen = [o.name]
            t = o
            while t.mirror_of:
                if t.mirror_of in seen:
                    raise LayoutError("%s mirrors itself%s" % (
                        o.name, (" through " + " -> ".join(seen[1:]))
                        if len(seen) > 1 else ""))
                seen.append(t.mirror_of)
                try:
                    t = self.get(t.mirror_of)
                except LayoutError:
                    t = None
                    break
                if not t.active:
                    t = None
                    break
            if t is None or t is o:
                o.mirror_of = None
                continue
            o.mirror_of = t.name
            o.x, o.y = t.x, t.y

    def overlaps(self):
        """Pairs of active outputs that intersect at *different* origins — a partial overlap, i.e. a region two
        screens both draw.  Outputs at the same origin are a clone (xrandr's ``--same-as``, whatever their
        sizes) and are not one."""
        pairs = []
        act = self.active_outputs()
        for i, a in enumerate(act):
            for b in act[i + 1:]:
                ra, rb = a.rect(), b.rect()
                if not _intersects(ra, rb):
                    continue
                if (ra[0], ra[1]) == (rb[0], rb[1]):
                    continue
                pairs.append((a.name, b.name))
        return pairs

    def shared_region(self, a, b):
        """The rectangle two outputs both draw, ``(x, y, w, h)`` in layout pixels — what a partial overlap
        actually mirrors.  Empty (w or h 0) when they do not intersect."""
        ax, ay, aw, ah = self.get(a).rect()
        bx, by, bw, bh = self.get(b).rect()
        x, y = max(ax, bx), max(ay, by)
        return (x, y, max(0, min(ax + aw, bx + bw) - x), max(0, min(ay + ah, by + bh) - y))

    def check(self):
        """Raise LayoutError for outputs beyond the server's maximum screen size, and — only where the backend
        refuses one, `overlap_refusal` holding its reason — for a partial overlap.  arandr allows overlaps and
        X11 has always drawn them, so refusing one is never our own policy: the sentence names the compositor
        that says no."""
        if self.overlap_refusal and self.overlaps():
            raise LayoutError(self.overlap_refusal)
        x0, y0, x1, y1 = self.bounding_box()
        if x1 - x0 > self.screen_max[0] or y1 - y0 > self.screen_max[1]:
            raise LayoutError("A part of an output is outside the virtual screen.")

    def snap(self, name, x, y, tolerance):
        """arandr's edge snapping: within `tolerance` layout pixels of another active output's (or the virtual
        screen's) left/right/top/bottom edge, the same edge of the dragged output, or its centre line, the
        coordinate snaps there.  The nearest candidate wins."""
        o = self.get(name)
        w, h = o.size()
        xs, ys = set(), set()
        boxes = [(0, 0, self.screen_max[0], self.screen_max[1])]
        for p in self.active_outputs():
            if p is o or p.mirror_of == name:
                continue
            boxes.append(p.rect())
        for bx, by, bw, bh in boxes:
            xs.update((bx, bx + bw, bx - w, bx + bw - w, bx + bw / 2 - w / 2))
            ys.update((by, by + bh, by - h, by + bh - h, by + bh / 2 - h / 2))
        cx = [v for v in xs if abs(v - x) < tolerance]
        cy = [v for v in ys if abs(v - y) < tolerance]
        if cx:
            x = min(cx, key=lambda v: abs(v - x))
        if cy:
            y = min(cy, key=lambda v: abs(v - y))
        return int(round(x)), int(round(y))

    def free_spot(self, output):
        """Where a newly activated output goes: right of the layout."""
        _, _, x1, _ = self.bounding_box()
        return (x1, 0)

    # -- edits (validated; reverted on failure like arandr) -----------------

    def _edit(self, fn):
        saved = [(o, dict(vars(o))) for o in self.outputs]
        try:
            fn()
            self._sync_mirrors()
            self.check()
        except LayoutError:
            for o, d in saved:
                o.__dict__.update(d)
            raise
        self.normalize()

    def move(self, name, x, y):
        o = self.get(name)
        if o.mirror_of:
            raise LayoutError("%s mirrors %s; move that one" % (name, o.mirror_of))

        def do():
            o.x, o.y = int(x), int(y)
        self._edit(do)

    def set_active(self, name, active):
        o = self.get(name)

        def do():
            if active and not o.active:
                if o.mode is None:
                    o.mode = o.preferred_mode()
                    if o.mode is None:
                        raise LayoutError("%s has no modes" % name)
                    o.rate = o.mode.default_rate()
                    o.rotation = "normal"
                    o.reflection = "normal"
                    o.scale = 1.0
                o.mirror_of = None
                o.x, o.y = self.free_spot(o)
                o.active = True
            elif not active and o.active:
                o.active = False
                o.primary = False
                # its mirrors stay a clone group: the first becomes the
                # anchor, the others mirror that one
                anchor = None
                for p in self.outputs:
                    if p.mirror_of == name:
                        p.mirror_of = anchor.name if anchor else None
                        anchor = anchor or p
        self._edit(do)

    def set_primary(self, name, primary):
        o = self.get(name)

        def do():
            if primary:
                for p in self.outputs:
                    p.primary = False
                o.primary = True
            else:
                o.primary = False
        self._edit(do)

    def set_mode(self, name, mode_name):
        o = self.get(name)
        m = o.mode_named(mode_name)
        if m is None:
            raise LayoutError("Not a known mode: %s" % mode_name)

        def do():
            o.mode = m
            o.rate = m.nearest_rate(o.rate) if o.rate else m.default_rate()
        self._edit(do)

    def set_rate(self, name, hz):
        o = self.get(name)

        def do():
            o.rate = o.mode.nearest_rate(hz) if o.mode else None
        self._edit(do)

    def set_rotation(self, name, rotation):
        if rotation not in ROTATIONS:
            raise LayoutError("No such rotation: %s" % rotation)
        o = self.get(name)

        def do():
            o.rotation = rotation
        self._edit(do)

    def set_reflection(self, name, reflection):
        if reflection not in REFLECTIONS:
            raise LayoutError("No such reflection: %s" % reflection)
        o = self.get(name)

        def do():
            o.reflection = reflection
        self._edit(do)

    def set_scale(self, name, scale):
        if scale <= 0:
            raise LayoutError("scaling factors must be positive")
        o = self.get(name)

        def do():
            o.scale = float(scale)
        self._edit(do)

    def set_mirror(self, name, target):
        o = self.get(name)
        if target is not None:
            t = self.get(target)
            if t is o:
                raise LayoutError("%s cannot mirror itself" % name)
            if not t.active:
                raise LayoutError("%s is not active" % target)
            if t.mirror_of:
                raise LayoutError("%s is itself a mirror of %s" % (target, t.mirror_of))

        def do():
            o.mirror_of = target
            if target is not None:
                for p in self.outputs:
                    if p.mirror_of == name:
                        p.mirror_of = target
            else:
                o.x, o.y = self.free_spot(o)
        self._edit(do)

    # -- command line -------------------------------------------------------

    def args(self):
        """One stanza per output, arandr's order and shape
        (``--output N [--primary] --mode M --pos XxY --rotate R`` / ``--output N --off``) plus ``--rate``,
        ``--same-as``, ``--reflect``, ``--scale`` only when they carry information.  ``--scale`` is written
        whenever the output is scaled *or was scaled when the screen was read* (``1x1`` then): xrandr and
        wxrandr both keep an existing scale when it is not mentioned."""
        args = []
        for o in self.outputs:
            args += ["--output", o.name]
            if not o.active or o.mode is None:
                args.append("--off")
                continue
            if o.primary:
                args.append("--primary")
            args += ["--mode", o.mode.name]
            if o.rate is not None and o.mode.rates and abs(o.rate - o.mode.default_rate()) >= 0.005:
                args += ["--rate", fmt_rate(o.rate)]
            if o.mirror_of:
                args += ["--same-as", o.mirror_of]
            else:
                args += ["--pos", "%dx%d" % (o.x, o.y)]
            args += ["--rotate", o.rotation]
            if o.reflection != "normal":
                args += ["--reflect", o.reflection]
            if abs(o.scale - 1.0) >= 1e-6 or abs(o.screen_scale - 1.0) >= 1e-6:
                args += ["--scale", "%sx%s" % (fmt_scale(o.scale), fmt_scale(o.scale))]
        return args

    def command_line(self, word=None):
        return " ".join([word or self.command_word] + [shlex.quote(a) for a in self.args()])

    # -- scripts ------------------------------------------------------------

    def to_script(self, word=None, notes=None):
        """The layout script.  `notes` is warandr's comment header — one line per note, about a *forced* backend
        and about what an overlap in this layout means on it.  It goes only into the default template, because a
        loaded file's own template is written back untouched (arandr's rule), and it is only ever a comment:
        `sh script.sh` on a plain X11 box must not care which backend the window used."""
        lines = list(self.template)
        if PLACEHOLDER not in lines:
            lines.append(PLACEHOLDER)
        if isinstance(notes, str):
            notes = [notes]
        if notes and lines == list(DEFAULT_TEMPLATE):
            for i, note in enumerate(notes):
                lines.insert(1 + i, "# " + note)
        cmd = self.command_line(word)
        return "\n".join(cmd if ln == PLACEHOLDER else ln for ln in lines) + "\n"

    def load_script(self, text):
        """Apply a layout script (arandr's or ours) on top of this layout — which must hold the *current*
        outputs and modes, like arandr's load_from_string does after load_from_x.  The other lines become the
        template that to_script() writes back around the new command."""
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if not lines or lines[0].strip() != SHEBANG:
            raise LayoutError("Not a shell script.")
        found = [i for i, ln in enumerate(lines) if _command_word(ln) is not None]
        if not found:
            raise LayoutError("No recognized xrandr command in this shell script.")
        if len(found) > 1:
            raise LayoutError("More than one xrandr line in this shell script.")
        idx = found[0]
        try:
            argv = shlex.split(lines[idx].strip())
        except ValueError as e:
            raise LayoutError("Unparseable xrandr line: %s" % e)
        self.apply_args(argv[1:])
        template = list(lines)
        template[idx] = PLACEHOLDER
        self.template = template
        return template

    def apply_args(self, argv):
        """Fold ``--output ...`` stanzas into the configuration (validated as
        a whole; the layout is untouched on error)."""
        stanzas = _parse_stanzas(argv)

        def do():
            if any(s.get("primary") for s in stanzas):
                # xrandr --primary *moves* the primary: a script that only
                # mentions the new one must not leave the old one flagged
                for o in self.outputs:
                    o.primary = False
            for s in stanzas:
                o = self.get(s["name"])
                o.primary = False
                if s.get("off"):
                    o.active = False
                    o.mirror_of = None
                    continue
                if "mode" in s:
                    m = o.mode_named(s["mode"])
                    if m is None:
                        raise LayoutError("Not a known mode: %s" % s["mode"])
                    o.mode = m
                    o.rate = m.default_rate()
                elif o.mode is None:
                    o.mode = o.preferred_mode()
                    if o.mode is None:
                        raise LayoutError("%s has no modes" % o.name)
                    o.rate = o.mode.default_rate()
                if "rate" in s:
                    o.rate = o.mode.nearest_rate(float(s["rate"]))
                if "pos" in s:
                    o.x, o.y = s["pos"]
                if "rotate" in s:
                    o.rotation = s["rotate"]
                if "reflect" in s:
                    o.reflection = s["reflect"]
                if "scale" in s:
                    o.scale = s["scale"]
                o.mirror_of = s.get("same-as")
                o.primary = bool(s.get("primary"))
                o.active = True
            for s in stanzas:  # placements against the *new* geometry
                if s.get("off") or "relation" not in s:
                    continue
                o = self.get(s["name"])
                rel, other = s["relation"]
                t = self.get(other)
                tw, th = t.size()
                ow, oh = o.size()
                o.x, o.y = {"left-of": (t.x - ow, t.y),
                            "right-of": (t.x + tw, t.y),
                            "above": (t.x, t.y - oh),
                            "below": (t.x, t.y + th)}[rel]
            for o in self.outputs:
                if o.mirror_of and not self.has(o.mirror_of):
                    raise LayoutError("Not a known output: %s" % o.mirror_of)
            if len([o for o in self.outputs if o.primary]) > 1:
                raise LayoutError("More than one primary output.")
        self._edit(do)


def _command_word(line):
    s = line.strip()
    for w in COMMAND_WORDS:
        if s == w or s.startswith(w + " "):
            return w
    return None


def _parse_stanzas(argv):
    stanzas = []
    cur = None
    i = 0
    while i < len(argv):
        a = argv[i]

        def value():
            if i + 1 >= len(argv):
                raise LayoutError("%s requires an argument" % a)
            return argv[i + 1]
        if a == "--output":
            cur = {"name": value()}
            stanzas.append(cur)
            i += 2
            continue
        if cur is None:
            raise LayoutError("%s must be used after --output" % a)
        if a == "--off":
            cur["off"] = True
            i += 1
        elif a == "--primary":
            cur["primary"] = True
            i += 1
        elif a in ("--auto", "--preferred"):
            i += 1
        elif a == "--mode":
            cur["mode"] = value()
            i += 2
        elif a in ("--rate", "--refresh", "-r"):
            try:
                cur["rate"] = float(value())
            except ValueError:
                raise LayoutError("failed to parse '%s' as a rate" % value())
            i += 2
        elif a == "--pos":
            m = re.fullmatch(r"(-?\d+)x(-?\d+)", value())
            if not m:
                raise LayoutError("failed to parse '%s' as a position" % value())
            cur["pos"] = (int(m.group(1)), int(m.group(2)))
            i += 2
        elif a == "--rotate":
            if value() not in ROTATIONS:
                raise LayoutError("No such rotation: %s" % value())
            cur["rotate"] = value()
            i += 2
        elif a == "--reflect":
            if value() not in REFLECTIONS:
                raise LayoutError("No such reflection: %s" % value())
            cur["reflect"] = value()
            i += 2
        elif a == "--scale":
            cur["scale"] = _parse_scale(value())
            i += 2
        elif a == "--same-as":
            cur["same-as"] = value()
            i += 2
        elif a in ("--left-of", "--right-of", "--above", "--below"):
            cur["relation"] = (a[2:], value())
            i += 2
        else:
            raise LayoutError("Unsupported option in layout: %s" % a)
    return stanzas


def _parse_scale(text):
    """``--scale`` out of a layout script.  The old ``[0-9.]+`` also matched ``1.2.3`` and ``.``, whose bare
    float() raised a ValueError that warandr's top level does not catch (it handles
    RandrError/LayoutError/OSError), so a hand-edited script ended in a traceback; and a parsed ``0`` was
    accepted here although set_scale refuses it, then divided by in Output.size(). Both spellings now give the
    one ``warandr:`` line xrandr would."""
    m = re.fullmatch(r"(\d+(?:\.\d*)?|\.\d+)(?:x(\d+(?:\.\d*)?|\.\d+))?", text)
    if not m:
        raise LayoutError("failed to parse '%s' as a scaling factor" % text)
    scale = float(m.group(1))
    if scale <= 0:
        raise LayoutError("scaling factors must be positive")
    return scale


def _derive_scale(po, o, hidpi):
    """Scale from the query: X11 prints it in the --verbose transform matrix; Wayland compositors report
    identity there, but the logical geometry is ``mode / scale`` (truncated) — recover the factor and snap it to
    a menu value when it is within rounding of one."""
    if po.transform is not None and abs(po.transform[0] - 1.0) > 1e-6 and not hidpi:
        return round(po.transform[0], 4)
    if o.mode is None or not po.w or not po.h:
        return 1.0
    mw, mh = o.mode.w, o.mode.h
    if o.rotation in ("left", "right"):
        mw, mh = mh, mw
    if not mw:
        return 1.0
    if hidpi:
        s = mw / float(po.w)
    else:
        s = po.w / float(mw)
    if abs(s - 1.0) < 0.002:
        return 1.0
    for c in SCALES:
        if abs(s - c) < 0.012:
            return c
    return round(s, 2)
