#!/usr/bin/env python3
"""Check hacks/display/kwin.py's wire constants against the upstream protocol XML.

Run this on the *host*, with network: it fetches plasma-wayland-protocols'
kde-output-device-v2.xml and kde-output-management-v2.xml from invent.kde.org
at the refs given (default: the newest release tag and master) and compares
every opcode, `since` and interface version the KWin backend hard-codes
against what the XML actually says.

    python3 repro/kde-outreg-conformance.py                 # v1.21.0 + master
    python3 repro/kde-outreg-conformance.py v1.20.0
    python3 repro/kde-outreg-conformance.py --xml a.xml b.xml   # local copies

It exists because the second discovery path -- `kde_output_device_registry_v2`,
which is how Plasma 6.7 and newer publish outputs -- is the one part of the
backend no image here can run: the KDE golden images are Plasma 5.27 and 6.6,
which still export the devices as wl_registry globals. Until a 6.7 image
exists, this is the check that stands in for one, so run it when a new Plasma
lands and before believing the version table in hacks/display/kwin.py.

Exit status 0 if every constant matches, 1 otherwise.
"""

import argparse
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from wxrandr import kwin                                       # noqa: E402

PROJECT = "libraries%2Fplasma-wayland-protocols"
RAW = ("https://invent.kde.org/api/v4/projects/%s/repository/files/"
       "src%%2Fprotocols%%2F%s/raw?ref=%s")


def fetch(fname, ref):
    url = RAW % (PROJECT, fname, ref)
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def parse(data):
    """{interface: {"version", "events", "requests"}}, each as
    {name: (opcode, since)} -- opcodes are positional, per the wire format."""
    root = ET.fromstring(data)
    out = {}
    for i in root.findall("interface"):
        out[i.get("name")] = {
            "version": int(i.get("version")),
            "events": {e.get("name"): (n, int(e.get("since") or 1))
                       for n, e in enumerate(i.findall("event"))},
            "requests": {e.get("name"): (n, int(e.get("since") or 1))
                         for n, e in enumerate(i.findall("request"))},
        }
    return out


class Check:
    def __init__(self):
        self.bad = 0
        self.n = 0

    def __call__(self, label, ours, upstream, rel="=="):
        """rel "==" for a wire fact that must match exactly; ">=" for a
        ceiling we bind at, which only has to cover what the XML can send."""
        self.n += 1
        ok = ours >= upstream if rel == ">=" else ours == upstream
        if not ok:
            self.bad += 1
        print("  %-44s ours %s %-5s xml=%-5s %s"
              % (label, rel, ours, upstream, "ok" if ok else "*** MISMATCH ***"))


def run(dev, mgmt, label):
    print("== %s" % label)
    c = Check()
    reg = dev["kde_output_device_registry_v2"]
    d = dev["kde_output_device_v2"]["events"]
    m = dev["kde_output_device_mode_v2"]["events"]
    cfg = mgmt["kde_output_configuration_v2"]

    # the registry: the whole reason this file exists
    c("registry `output` opcode", 1, reg["events"]["output"][0])
    c("registry `finished` opcode", 0, reg["events"]["finished"][0])
    c("REG_MIN (registry `output` since)", kwin.REG_MIN,
      reg["events"]["output"][1])
    # REG_WANT is a ceiling, not a wire fact: we bind min(advertised, REG_WANT), so it only has to cover
    # everything this XML can send. It failing means the protocol grew events we have never looked at.
    c("REG_WANT covers the device interface", kwin.REG_WANT,
      dev["kde_output_device_v2"]["version"], ">=")

    # device events the backend decodes, by opcode
    for name, op in [("geometry", 0), ("current_mode", 1), ("mode", 2),
                     ("done", 3), ("scale", 4), ("edid", 5), ("enabled", 6),
                     ("uuid", 7), ("serial_number", 8), ("eisa_id", 9),
                     ("capabilities", 10), ("name", 14),
                     ("replication_source", 27), ("priority", 34),
                     ("removed", 36)]:
        c("device event %s" % name, op, d[name][0])
    for name, op in [("size", 0), ("refresh", 1), ("preferred", 2),
                     ("removed", 3)]:
        c("mode event %s" % name, op, m[name][0])

    # configuration requests the backend sends
    for name, const in [("enable", "REQ_ENABLE"), ("mode", "REQ_MODE"),
                        ("transform", "REQ_TRANSFORM"),
                        ("position", "REQ_POSITION"), ("scale", "REQ_SCALE"),
                        ("apply", "REQ_APPLY"), ("destroy", "REQ_DESTROY"),
                        ("set_primary_output", "REQ_SET_PRIMARY"),
                        ("set_priority", "REQ_SET_PRIORITY"),
                        ("set_replication_source", "REQ_SET_REPLICATION")]:
        c("config request %s" % name, getattr(kwin, const),
          cfg["requests"][name][0])
    for name, op in [("applied", 0), ("failed", 1), ("failure_reason", 2)]:
        c("config event %s" % name, op, cfg["events"][name][0])

    # the `since` gates every optional feature is keyed on
    c("name since (DEV_WANT floor)", 2, d["name"][1])
    c("REPL_DEV", kwin.REPL_DEV, d["replication_source"][1])
    c("PRIORITY_DEV", kwin.PRIORITY_DEV, d["priority"][1])
    c("device `removed` since", kwin.REG_MIN, d["removed"][1])
    c("PRIMARY_MGMT", kwin.PRIMARY_MGMT,
      cfg["requests"]["set_primary_output"][1])
    c("PRIORITY_MGMT", kwin.PRIORITY_MGMT, cfg["requests"]["set_priority"][1])
    c("REASON_MGMT", kwin.REASON_MGMT, cfg["events"]["failure_reason"][1])
    c("REPL_MGMT", kwin.REPL_MGMT,
      cfg["requests"]["set_replication_source"][1])
    c("CUSTOM_MODES_MGMT", kwin.CUSTOM_MODES_MGMT,
      cfg["requests"]["set_custom_modes"][1])
    print("  -> %d checked, %d mismatched\n" % (c.n, c.bad))
    return c.bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("refs", nargs="*", default=None,
                    help="git refs of plasma-wayland-protocols to check")
    ap.add_argument("--xml", nargs=2, metavar=("DEVICE", "MANAGEMENT"),
                    help="two local XML files instead of fetching")
    a = ap.parse_args()
    bad = 0
    if a.xml:
        with open(a.xml[0], "rb") as f:
            dev = parse(f.read())
        with open(a.xml[1], "rb") as f:
            mgmt = parse(f.read())
        bad += run(dev, mgmt, " ".join(a.xml))
    else:
        for ref in (a.refs or ["v1.21.0", "master"]):
            dev = parse(fetch("kde-output-device-v2.xml", ref))
            mgmt = parse(fetch("kde-output-management-v2.xml", ref))
            bad += run(dev, mgmt, "plasma-wayland-protocols %s" % ref)
    print("CONFORMANT" if not bad else "%d MISMATCHES" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
