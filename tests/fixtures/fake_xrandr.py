#!/usr/bin/env python3
"""A stand-in xrandr for the warandr GUI test: a tiny RandR simulator.

Query forms (``--query``, ``-q``, ``--verbose``, ``--current``, bare) render
the simulated screen in xrandr 1.5.4's text format through wxrandr.core's
byte-parity renderers, plus one hand-written ``disconnected`` output (a
compositor never has those, real X servers do).  ``--version`` answers like
xrandr 1.5.4.

Any other argv is an *apply*: it is appended as one JSON line to
``$FAKE_XRANDR_LOG`` and folded into the simulated state kept in
``$FAKE_XRANDR_STATE`` (default: next to the log), so a reload after Apply
sees the new layout exactly like the real tools would show it.
``FAKE_XRANDR_FAIL=<message>`` makes the next apply print that message to
stderr and exit 1 without touching the state.

It also simulates wxrandr's backend options, which is how the GUI test drives
Layout ▸ Backend: a leading ``--backend NAME`` (or ``--backend=NAME``) is
accepted and stripped from every invocation, ``--print-backend`` (with
``--verbose``) and ``--backends`` answer like wxrandr's, and
``FAKE_XRANDR_BACKEND_FAIL=NAME`` makes every invocation forced to NAME fail
with one line, so a refused switch can be tested.  ``FAKE_XRANDR_AUTO_BACKEND``
(default ``x11``) is the one auto would pick, ``FAKE_XRANDR_QUERY_LOG`` gets
one JSON line per *query* — the apply log stays the applies only.

The GNOME overlap agreement is simulated too, which is how the GUI test drives
the consent dialog: ``--gnome-overlap-status``, ``--gnome-overlap-allow`` and
``--gnome-overlap-forget`` answer like wxrandr's, against a record kept in
``$FAKE_XRANDR_CONSENT``.  ``FAKE_XRANDR_OVERLAP`` says what the simulated
session can do — ``available`` (the extension is there, nothing agreed yet),
``agreed`` (a record already exists), ``unavailable``/unset (no route at all,
which is what every other test sees).  ``FAKE_XRANDR_OVERLAP_ALLOW_FAIL`` makes
``--gnome-overlap-allow`` fail with that message, and
``FAKE_XRANDR_OVERLAP_WITHDRAW_ON_APPLY=1`` makes an overlapping apply succeed
and *then* withdraw the agreement, which is what wxrandr does when the audit it
runs on the extension's reply finds a build that is not the one that was agreed
to: the record is deleted and one line says so.  The line is not written out
here -- it comes from ``gnome_overlap.consent_drift()``, so it is wxrandr's own
sentence and stays wxrandr's own sentence.
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from hacks.display import core, gnome_overlap

DISCONNECTED = ("HDMI-2 disconnected "
                "(normal left inverted right x axis y axis)")

DEFAULT = {
    "primary": "DP-1",
    "outputs": [
        {"name": "DP-1", "active": True, "x": 0, "y": 0, "transform": "normal",
         "scale": 1.0, "current": 0, "ident": 0x42, "mm": [598, 336],
         "modes": [[1920, 1080, 60000, True], [1920, 1080, 50000, False],
                   [1280, 720, 60000, False]]},
        {"name": "HDMI-1", "active": True, "x": 1920, "y": 0,
         "transform": "normal", "scale": 1.0, "current": 0, "ident": 0x43,
         "mm": [376, 301],
         "modes": [[1280, 1024, 60020, True], [1280, 1024, 75025, False],
                   [1024, 768, 60004, False]]},
        {"name": "DP-2", "active": True, "x": 3200, "y": 0,
         "transform": "normal", "scale": 1.0, "current": 0, "ident": 0x44,
         "mm": [344, 194],
         "modes": [[1280, 720, 60000, True], [800, 600, 60317, False]]},
    ],
}


#: name -> (available, reason or what makes it available)
BACKENDS = {
    "sway": (False, "no sway or i3 IPC socket ($SWAYSOCK)"),
    "hypr": (False, "no Hyprland IPC socket ($HYPRLAND_INSTANCE_SIGNATURE)"),
    "kwin": (False, "the compositor does not advertise "
                    "kde_output_management_v2"),
    "mutter": (True, "org.gnome.Mutter.DisplayConfig on the session bus"),
    "cinnamon": (False, "org.cinnamon.Muffin.DisplayConfig is not on the session bus"),
    "wlr": (False, "the compositor does not advertise zwlr_output_manager_v1"),
    "x11": (True, "/usr/bin/xrandr"),
}
WAYLAND_BACKENDS = ("sway", "hypr", "wlr", "mutter", "cinnamon", "kwin")


def auto_backend():
    return os.environ.get("FAKE_XRANDR_AUTO_BACKEND") or "x11"


def take_backend(argv):
    """Strip a leading/other ``--backend NAME`` out of argv, like wxrandr's
    own parse; returns (name or None, the rest)."""
    name, rest, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--backend" and i + 1 < len(argv):
            name = argv[i + 1]
            i += 2
        elif a.startswith("--backend="):
            name = a.split("=", 1)[1]
            i += 1
        else:
            rest.append(a)
            i += 1
    return name, rest


def print_backend(forced, verbose):
    name = forced or auto_backend()
    lines = [name]
    if verbose:
        ok, why = BACKENDS.get(name, (False, "unknown backend"))
        lines += ["session: %s" % ("wayland" if name in WAYLAND_BACKENDS
                                   else "x11"),
                  "chosen by: %s" % ("flag (--backend %s)" % forced if forced
                                     else "detection")]
        if name == "x11":
            lines += ["compositor: X server (RandR)", "real xrandr: " + why]
        else:
            lines += ["compositor: %s (fake)" % name.capitalize(),
                      "protocol: %s (fake)" % name]
        lines.append("available: %s" % ("yes" if ok else "no (%s)" % why))
    return "\n".join(lines) + "\n"


def backends_table():
    auto = auto_backend()
    out = ""
    for name in ("sway", "hypr", "kwin", "mutter", "cinnamon", "wlr", "x11"):
        ok, why = BACKENDS[name]
        out += "%s %-8s  %-11s  %s\n" % ("*" if name == auto else " ", name,
                                         "available" if ok else "unavailable",
                                         why)
    return out


def state_path():
    p = os.environ.get("FAKE_XRANDR_STATE")
    if p:
        return p
    log = os.environ.get("FAKE_XRANDR_LOG")
    if log:
        return log + ".state.json"
    return os.path.join(os.environ.get("TMPDIR", "/tmp"),
                        "fake_xrandr_%d.json" % os.getuid())


def load_default():
    """A fresh copy of the default screen (tests mutate it)."""
    return json.loads(json.dumps(DEFAULT))


def load():
    try:
        with open(state_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return load_default()


def save(st):
    with open(state_path(), "w") as f:
        json.dump(st, f)


def to_outputs(st):
    outs = []
    for d in st["outputs"]:
        o = core.OutputState(name=d["name"], active=d["active"],
                             x=d["x"], y=d["y"], scale=d["scale"],
                             transform=d["transform"], ident=d["ident"],
                             mm_w=d["mm"][0], mm_h=d["mm"][1])
        o.modes = [core.Mode(w=w, h=h, refresh_mhz=r, preferred=p)
                   for w, h, r, p in d["modes"]]
        if d["active"]:
            o.current = o.modes[d["current"]]
            o.w, o.h = core.logical_size(o.current.w, o.current.h,
                                         o.transform, o.scale)
        outs.append(o)
    return outs


def render(st, verbose):
    wx = core.State("fake", path=state_path() + ".wx")
    wx.primary = st.get("primary")
    lines = core.render_query(to_outputs(st), wx, verbose=verbose)
    lines.append(DISCONNECTED)
    return "\n".join(lines) + "\n"


def apply(st, argv):
    """Fold an xrandr command line into the simulated state (the subset
    warandr emits; anything else is an 'unrecognized option' like xrandr)."""
    stanzas = []
    cur = None
    i = 0

    def need():
        nonlocal i
        i += 1
        if i >= len(argv):
            sys.stderr.write("xrandr: %s requires an argument\n" % argv[i - 1])
            sys.exit(1)
        return argv[i]

    while i < len(argv):
        a = argv[i]
        if a == "--output":
            cur = {"name": need()}
            stanzas.append(cur)
        elif cur is None:
            sys.stderr.write("xrandr: unrecognized option '%s'\n" % a)
            sys.exit(1)
        elif a in ("--mode", "--rate", "--pos", "--rotate", "--reflect",
                   "--scale", "--same-as", "--left-of", "--right-of",
                   "--above", "--below"):
            cur[a[2:]] = need()
        elif a in ("--off", "--primary", "--auto", "--preferred"):
            cur[a[2:]] = True
        else:
            sys.stderr.write("xrandr: unrecognized option '%s'\n" % a)
            sys.exit(1)
        i += 1

    by_name = {d["name"]: d for d in st["outputs"]}
    for s in stanzas:
        d = by_name.get(s["name"])
        if d is None:
            if s["name"] != DISCONNECTED.split()[0]:
                sys.stderr.write("warning: output %s not found; ignoring\n"
                                 % s["name"])
            continue
        if s.get("off"):
            d["active"] = False
            if st.get("primary") == d["name"]:
                st["primary"] = None
            continue
        d["active"] = True
        if s.get("primary"):
            st["primary"] = d["name"]
        if "mode" in s:
            m = re.fullmatch(r"(\d+)x(\d+)", s["mode"])
            if not m:
                sys.stderr.write("xrandr: cannot find mode %s\n" % s["mode"])
                sys.exit(1)
            w, h = int(m.group(1)), int(m.group(2))
            cands = [k for k, md in enumerate(d["modes"])
                     if md[0] == w and md[1] == h]
            if not cands:
                sys.stderr.write("xrandr: cannot find mode %s\n" % s["mode"])
                sys.exit(1)
            pick = cands[0]
            for k in cands:
                if d["modes"][k][3]:
                    pick = k
                    break
            if "rate" in s:
                want = float(s["rate"])
                pick = min(cands, key=lambda k: abs(d["modes"][k][2] / 1000.0
                                                    - want))
            d["current"] = pick
        if "pos" in s:
            m = re.fullmatch(r"(-?\d+)x(-?\d+)", s["pos"])
            d["x"], d["y"] = int(m.group(1)), int(m.group(2))
        rot = s.get("rotate", core.RANDR_VIEW[d["transform"]][0])
        refl = s.get("reflect", core.RANDR_VIEW[d["transform"]][1])
        d["transform"] = core.sway_transform(rot, refl)
        if "scale" in s:
            d["scale"] = float(s["scale"].split("x")[0])
    for s in stanzas:  # relations resolve against the new geometry
        d = by_name.get(s["name"])
        if d is None or s.get("off"):
            continue
        for rel in ("same-as", "left-of", "right-of", "above", "below"):
            if rel in s and s[rel] in by_name:
                t = by_name[s[rel]]
                tw, th = size_of(t)
                dw, dh = size_of(d)
                if rel == "same-as":
                    d["x"], d["y"] = t["x"], t["y"]
                elif rel == "left-of":
                    d["x"], d["y"] = t["x"] - dw, t["y"]
                elif rel == "right-of":
                    d["x"], d["y"] = t["x"] + tw, t["y"]
                elif rel == "above":
                    d["x"], d["y"] = t["x"], t["y"] - dh
                else:
                    d["x"], d["y"] = t["x"], t["y"] + th
    # normalise like an X server would not, but wxrandr does: keep as given
    return st


def size_of(d):
    w, h, _r, _p = d["modes"][d["current"]]
    return core.logical_size(w, h, d["transform"], d["scale"])


# -- the GNOME overlap agreement ---------------------------------------------

#: what this simulated session records when it is asked to agree.  128 is not a
#: MetaMonitorsConfig size any measured build has: the live GNOME 46.0 record
#: (libmutter-14) is 72, and 80 is the plan's figure for libmutter-18, which is
#: the generation this fake calls itself.
OVERLAP_BUILD = {"shell": "50.1", "libmutter": 18, "struct_size": 80}

#: what the checks "measure" on the apply that withdraws: the same generation
#: with a different private layout, which is the one shape consent_drift() is
#: there to catch.
WITHDRAWN_FACTS = {"libmutter": OVERLAP_BUILD["libmutter"], "struct_size": 72}


def consent_file():
    return os.environ.get("FAKE_XRANDR_CONSENT") or os.path.join(
        os.path.dirname(os.environ.get("FAKE_XRANDR_LOG") or HERE), "consent.json")


def consent_read():
    try:
        with open(consent_file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def overlap_route(backend):
    """What this simulated session can do about an overlap: only GNOME can do
    anything, and only when the fixture was told the extension is there."""
    name = backend or auto_backend()
    if name != "mutter":
        return None
    want = os.environ.get("FAKE_XRANDR_OVERLAP", "")
    return want if want in ("available", "agreed") else None


def overlap_status(backend):
    route = overlap_route(backend)
    rec = consent_read()
    if route is None:
        lines = ["unavailable", "reason: the fake says there is no overlap route here"]
    else:
        lines = ["agreed" if rec else "available", "shell: %s" % OVERLAP_BUILD["shell"],
                 "extension: running"]
        if not rec:
            lines.append("asks first: nothing is recorded")
    if rec:
        lines += ["agreed on: %s" % rec.get("agreed"),
                  "agreed for: GNOME Shell %s (libmutter-%s, MetaMonitorsConfig %s bytes)"
                  % (rec.get("shell"), rec.get("libmutter"), rec.get("struct_size")),
                  "agreed by: %s" % rec.get("how")]
    lines.append("file: %s" % consent_file())
    return "".join(ln + "\n" for ln in lines)


def overlap_allow(backend):
    fail = os.environ.get("FAKE_XRANDR_OVERLAP_ALLOW_FAIL")
    if fail:
        sys.stderr.write("xrandr: --gnome-overlap-allow: %s\n" % fail)
        return 1
    if overlap_route(backend) is None:
        sys.stderr.write("xrandr: --gnome-overlap-allow: nothing to agree to here\n")
        return 1
    rec = dict(OVERLAP_BUILD, format=1, agreed="2026-01-01T00:00:00Z",
               how="wxrandr --gnome-overlap-allow")
    with open(consent_file(), "w") as f:
        json.dump(rec, f)
    sys.stdout.write("recorded in %s\n" % consent_file())
    return 0


def withdraw_after_apply():
    """The post-apply audit, when FAKE_XRANDR_OVERLAP_WITHDRAW_ON_APPLY says so:
    the layout is applied (rc 0) and the agreement is gone.  Exactly wxrandr's
    order -- the record only ever decides how much is printed, never whether the
    apply happens, so a withdrawal cannot un-apply anything."""
    rec = consent_read()
    if not rec:
        return
    drift = gnome_overlap.consent_drift(rec, WITHDRAWN_FACTS)
    if not drift:
        return
    try:
        os.unlink(consent_file())
    except OSError:
        pass
    sys.stderr.write("xrandr: %s: %s" % (gnome_overlap.FLAG, drift))


def main(argv):
    given = list(argv)          # what the log records: the flag included
    backend, argv = take_backend(argv)
    if backend and backend == os.environ.get("FAKE_XRANDR_BACKEND_FAIL"):
        sys.stderr.write("xrandr: --backend %s is not available in this "
                         "session: the fake says so\n" % backend)
        return 1
    if "--print-backend" in argv:
        sys.stdout.write(print_backend(backend, "--verbose" in argv))
        return 0
    if "--backends" in argv:
        sys.stdout.write(backends_table())
        return 0
    if "--gnome-overlap-status" in argv:
        sys.stdout.write(overlap_status(backend))
        return 0
    if "--gnome-overlap-allow" in argv:
        return overlap_allow(backend)
    if "--gnome-overlap-forget" in argv:
        try:
            os.unlink(consent_file())
            sys.stdout.write("withdrawn: %s removed\n" % consent_file())
        except OSError:
            sys.stdout.write("nothing to withdraw\n")
        return 0
    if argv in ([], ["-q"], ["--query"], ["--current"], ["--verbose"],
                ["--current", "--verbose"], ["--verbose", "--current"]):
        qlog = os.environ.get("FAKE_XRANDR_QUERY_LOG")
        if qlog:
            with open(qlog, "a") as f:
                f.write(json.dumps({"backend": backend, "argv": argv}) + "\n")
        sys.stdout.write(render(load(), "--verbose" in argv))
        return 0
    if argv in (["--version"], ["-v"]):
        sys.stdout.write("xrandr program version       1.5.4\n"
                         "Server reports RandR version 1.6\n")
        return 0
    log = os.environ.get("FAKE_XRANDR_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(given) + "\n")
    # wxrandr's own flag, and not a stanza: the log above keeps it (that is what
    # the test asserts on), the simulated screen never sees it
    argv = [a for a in argv if a != "--unsafe-gnome-overlap"]
    fail = os.environ.get("FAKE_XRANDR_FAIL")
    if fail:
        sys.stderr.write(fail if fail.endswith("\n") else fail + "\n")
        return 1
    save(apply(load(), argv))
    if (os.environ.get("FAKE_XRANDR_OVERLAP_WITHDRAW_ON_APPLY") not in (None, "", "0")
            and "--unsafe-gnome-overlap" in given):
        withdraw_after_apply()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
