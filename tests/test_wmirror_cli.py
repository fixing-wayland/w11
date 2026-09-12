"""wmirror: detection, policy, the wl-mirror command line, and the queries.

The policy is the point of this file. wl-mirror costs a resident process and
a core's worth of compositing, so it may only run for the pictures the
layout cannot produce: a region, or a whole output onto one of a different
logical size. Two same-sized outputs at one position already mirror on
wlroots, byte for byte, and that must stay the answer -- measured on sway
1.11, see docs/WMIRROR.md."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# ...and the tests directory itself, for the bare `import wl_fake` below: running this file by path puts it
# on sys.path for free, `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wl_fake
from w11common import distro
from w11common.wayland_mini import WlConn
from w11common import passthrough
from w11common import session
from w11common import procs
from wmirror import cli, core
from wmirror import supervise
from wxrandr import core as wxcore

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
os.environ["W11_PASSTHROUGH"] = "never"


def out(name, enabled=True, x=0, y=0, w=1920, h=1080):
    return wxcore.OutputState(name=name, active=enabled, x=x, y=y, w=w, h=h)


SOCKET = "/run/user/1000/wayland-0"


def run(argv, outputs=None, helper="/usr/bin/wl-mirror", capture=True,
        wayland=SOCKET):
    """cli.main() with the compositor faked out. -> (rc, stdout, stderr).

    `wayland=None` is a session with no wayland socket at all -- an X11
    login, where what is missing is not a package."""
    conn = mock.Mock()
    hit = (1000, "user", wayland) if wayland else None
    patches = [
        mock.patch.object(core, "open_conn", return_value=conn),
        mock.patch.object(core, "read_outputs", return_value=outputs or []),
        mock.patch.object(core, "find_helper", return_value=helper),
        mock.patch.object(session, "find_wayland_socket", return_value=hit),
    ]
    if capture:
        patches.append(mock.patch.object(
            core, "require_capture",
            return_value=[(core.SCREENCOPY, 3)]))
    else:
        patches.append(mock.patch.object(
            core, "require_capture",
            side_effect=core.Refusal(core.no_capture_lines())))
    o, e = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stdout(o))
        stack.enter_context(contextlib.redirect_stderr(e))
        rc = cli.main(argv)
    return rc, o.getvalue(), e.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wmirror-test-")
        self.addCleanup(shutil.rmtree, self.tmp,
                        ignore_errors=True)
        self.env = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)

    def state_file(self):
        return os.path.join(self.tmp, "wmirror-state.json")


# -- the command line we build ------------------------------------------------

class Argv(Base):
    def test_whole_output(self):
        self.assertEqual(
            core.build_argv("DP-1", "DP-2"),
            ["wl-mirror", "--fullscreen-output", "DP-2",
             "--scaling", "fit", "DP-1"])

    def test_scaling_and_helper_path(self):
        self.assertEqual(
            core.build_argv("DP-1", "DP-2", scaling="cover",
                            helper="/opt/bin/wl-mirror"),
            ["/opt/bin/wl-mirror", "--fullscreen-output", "DP-2",
             "--scaling", "cover", "DP-1"])

    def test_region_is_slurp_syntax_without_the_output_name(self):
        """wl-mirror errors when a region names an output different from the
        argument (src/options.c); leaving the name out of the region makes
        that impossible, and the positional output still decides."""
        argv = core.build_argv("DP-1", "DP-2", region=(1400, 100, 500, 300))
        self.assertEqual(argv, ["wl-mirror", "--fullscreen-output", "DP-2",
                                "--scaling", "fit",
                                "--region", "1400,100 500x300", "DP-1"])

    def test_options_come_before_the_positional(self):
        """wl-mirror stops parsing options at the first non-dash argument,
        so the output it mirrors is always last."""
        for kwargs in ({}, {"region": (0, 0, 10, 10)},
                       {"scaling": "exact"}, {"scaling": None}):
            argv = core.build_argv("DP-1", "DP-2", **kwargs)
            self.assertEqual(argv[-1], "DP-1", kwargs)
            last_flag = max(i for i, a in enumerate(argv)
                            if a.startswith("--"))
            self.assertLess(last_flag, len(argv) - 1, kwargs)


class RegionSyntax(Base):
    def test_x11_geometry_order(self):
        self.assertEqual(core.parse_region("500x300+1400+100"),
                         (1400, 100, 500, 300))
        self.assertEqual(core.parse_region("500x300-40-10"),
                         (-40, -10, 500, 300))

    def test_bad_forms_are_one_line_each(self):
        for bad in ("", "500x300", "1400,100 500x300", "500+300+0+0",
                    "axb+0+0", "500x300+1400"):
            with self.assertRaises(core.Refusal) as cm:
                core.parse_region(bad)
            self.assertIn("WxH+X+Y", cm.exception.lines[0])

    def test_empty_rectangle(self):
        with self.assertRaises(core.Refusal) as cm:
            core.parse_region("0x300+0+0")
        self.assertIn("> 0", cm.exception.lines[0])

    def test_round_trip_formatting(self):
        self.assertEqual(core.fmt_region((1400, 100, 500, 300)),
                         "500x300+1400+100")
        self.assertEqual(core.fmt_region((-40, -10, 500, 300)),
                         "500x300-40-10")


# -- the policy ---------------------------------------------------------------

class Policy(Base):
    def test_same_size_same_place_is_already_a_mirror(self):
        """Measured on sway: two 1920x1080 heads at one position are
        byte-identical, wallpaper included. Nothing to start."""
        d = core.decide([out("A"), out("B")], "A", "B")
        self.assertEqual(d.verdict, core.DONE)
        self.assertIn("already shows", d.lines[0])
        self.assertIn("no helper", " ".join(d.lines))

    def test_same_size_apart_is_sent_to_the_layout(self):
        d = core.decide([out("A"), out("B", x=1920)], "A", "B")
        self.assertEqual(d.verdict, core.REFUSE)
        self.assertIn("wxrandr --output B --same-as A", " ".join(d.lines))

    def test_keep_layout_is_the_way_to_ask_for_it_anyway(self):
        d = core.decide([out("A"), out("B", x=1920)], "A", "B",
                        keep_layout=True)
        self.assertEqual(d.verdict, core.RUN)

    def test_a_different_shape_is_what_the_helper_is_for(self):
        """A shared position CROPS onto a smaller head (measured: 1280x1024
        showed the top-left 1280x1024 and lost the rest), and wlroots has no
        replication request to fix it the way KWin does."""
        d = core.decide([out("A"), out("B", x=1920, w=1280, h=1024)],
                        "A", "B")
        self.assertEqual(d.verdict, core.RUN)

    def test_a_region_is_never_expressible_as_geometry(self):
        d = core.decide([out("A"), out("B", x=1920)], "A", "B",
                        region=(100, 100, 500, 300))
        self.assertEqual(d.verdict, core.RUN)

    def test_a_region_must_lie_inside_the_source(self):
        """wl-mirror clamps a region that runs off the output and says
        nothing (measured). We refuse instead, quoting the source."""
        d = core.decide([out("A"), out("B", x=1920)], "A", "B",
                        region=(1700, 100, 500, 300))
        self.assertEqual(d.verdict, core.REFUSE)
        self.assertIn("1920x1080+0+0", " ".join(d.lines))
        self.assertIn("clamp", " ".join(d.lines))

    def test_a_region_inside_a_moved_source_is_in_layout_coordinates(self):
        d = core.decide([out("A", x=3840), out("B")], "A", "B",
                        region=(4000, 150, 500, 300))
        self.assertEqual(d.verdict, core.RUN)
        d = core.decide([out("A", x=3840), out("B")], "A", "B",
                        region=(100, 150, 500, 300))
        self.assertEqual(d.verdict, core.REFUSE)

    def test_overlapping_outputs_would_capture_themselves(self):
        """The self-capture guard: over a shared rectangle sway draws a
        window on BOTH heads, so a fullscreen mirror window on the target is
        drawn on the source too."""
        d = core.decide([out("A"), out("B", x=960, w=1280, h=1024)],
                        "A", "B")
        self.assertEqual(d.verdict, core.REFUSE)
        joined = " ".join(d.lines)
        self.assertIn("share pixels", joined)
        self.assertIn("capture itself", joined)
        self.assertIn("--right-of", joined)

    def test_a_region_over_a_shared_rectangle_is_refused_too(self):
        d = core.decide([out("A"), out("B")], "A", "B",
                        region=(0, 0, 500, 300))
        self.assertEqual(d.verdict, core.REFUSE)
        self.assertIn("capture itself", " ".join(d.lines))

    def test_an_output_cannot_mirror_itself(self):
        d = core.decide([out("A")], "A", "A")
        self.assertEqual(d.verdict, core.REFUSE)
        self.assertIn("itself", d.lines[0])

    def test_unknown_outputs_are_named_with_what_there_is(self):
        for src, dst, missing in (("X", "B", "X"), ("A", "X", "X")):
            d = core.decide([out("A"), out("B", x=1920)], src, dst)
            self.assertEqual(d.verdict, core.REFUSE)
            self.assertIn("no output named %s" % missing, d.lines[0])
            self.assertIn("A, B", d.lines[1])

    def test_a_disabled_output_is_not_a_mirror(self):
        d = core.decide([out("A"), out("B", enabled=False)], "A", "B")
        self.assertEqual(d.verdict, core.REFUSE)
        self.assertIn("is off", d.lines[0])
        self.assertIn("--auto", d.lines[1])

    def test_one_mirror_per_target(self):
        running = {"B": {"source": "C", "target": "B"}}
        d = core.decide([out("A"), out("B", x=1920, w=1280, h=1024)],
                        "A", "B", running=running)
        self.assertEqual(d.verdict, core.REFUSE)
        self.assertIn("--replace", " ".join(d.lines))
        self.assertIn("wmirror --stop B", " ".join(d.lines))

    def test_two_mirrors_pointing_at_each_other(self):
        running = {"A": {"source": "B", "target": "A"}}
        d = core.decide([out("A"), out("B", x=1920, w=1280, h=1024)],
                        "A", "B", running=running)
        self.assertEqual(d.verdict, core.REFUSE)
        self.assertIn("capture each other", d.lines[0])


class Watch(Base):
    """What ends a running mirror. wl-mirror only handles the first of
    these itself; the others are the supervisor's."""

    def test_nothing_wrong(self):
        self.assertIsNone(core.watch_reason(
            [out("A"), out("B", x=1920)], "A", "B"))

    def test_source_gone(self):
        self.assertIn("source output A is gone",
                      core.watch_reason([out("B", x=1920)], "A", "B"))

    def test_target_gone(self):
        self.assertIn("target output B is gone",
                      core.watch_reason([out("A")], "A", "B"))

    def test_either_switched_off(self):
        self.assertIn("turned off", core.watch_reason(
            [out("A", enabled=False), out("B", x=1920)], "A", "B"))
        self.assertIn("turned off", core.watch_reason(
            [out("A"), out("B", x=1920, enabled=False)], "A", "B"))

    def test_the_layout_moved_them_on_top_of_each_other(self):
        """A `wxrandr --same-as` under a running mirror would make it
        capture its own window."""
        self.assertIn("capture", core.watch_reason(
            [out("A"), out("B", x=100)], "A", "B"))

    def test_a_whole_output_mirror_follows_its_source_anywhere(self):
        """Moving the source is not a reason to end a mirror OF the source:
        the picture is still the picture that was asked for."""
        self.assertIsNone(core.watch_reason(
            [out("A", x=3840), out("B", x=1920)], "A", "B"))

    def test_the_source_moving_ends_a_region_mirror(self):
        """A region is a rectangle of the LAYOUT, resolved against the
        source once, when wl-mirror starts. Move that output afterwards and
        the same rectangle is a different picture -- and wl-mirror says
        nothing about it, which is why the start refuses a region that does
        not fit. The promise has to hold for as long as the mirror does."""
        why = core.watch_reason([out("A", x=3840), out("B", x=1920)],
                                "A", "B", region=(100, 100, 500, 300),
                                src_rect=(0, 0, 1920, 1080))
        self.assertIn("moved or changed size", why)
        self.assertIn("500x300+100+100", why)

    def test_the_source_shrinking_under_a_region_ends_it(self):
        """The same rectangle, now off the end of a smaller source: what
        wl-mirror would do here is clamp, silently."""
        why = core.watch_reason([out("A", w=400, h=300), out("B", x=1920)],
                                "A", "B", region=(100, 100, 500, 300),
                                src_rect=(0, 0, 400, 300))
        self.assertIn("no longer inside A", why)
        self.assertIn("400x300+0+0", why)

    def test_a_region_mirror_whose_layout_did_not_move(self):
        self.assertIsNone(core.watch_reason(
            [out("A"), out("B", x=1920)], "A", "B",
            region=(100, 100, 500, 300), src_rect=(0, 0, 1920, 1080)))

    def test_a_region_record_read_back_from_json_is_a_list(self):
        """The rectangle in the state file comes back as a list, and a list
        is never equal to the tuple /proc gives us."""
        self.assertIsNone(core.watch_reason(
            [out("A"), out("B", x=1920)], "A", "B",
            region=[100, 100, 500, 300], src_rect=[0, 0, 1920, 1080]))


# -- detection ----------------------------------------------------------------

class Detection(Base):
    def test_helper_found_on_path(self):
        binpath = os.path.join(self.tmp, "bin")
        os.makedirs(binpath)
        stub = os.path.join(binpath, core.HELPER)
        with open(stub, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(stub, 0o755)
        self.assertEqual(core.find_helper(path=binpath), stub)

    def test_helper_absent_names_the_package(self):
        self.assertIsNone(core.find_helper(path=self.tmp))
        lines = core.missing_helper_lines()
        self.assertIn("wl-mirror is not installed", lines[0])
        self.assertIn(distro.hint("wl-mirror"), lines[1])

    def test_helper_version_never_raises(self):
        """It says so in the docstring, but text=True decodes strict: a
        banner that is not the locale's encoding raised UnicodeDecodeError
        out of subprocess.run, past the except, and out of `wmirror --check`.
        """
        binpath = os.path.join(self.tmp, "bin2")
        os.makedirs(binpath)
        stub = os.path.join(binpath, core.HELPER)
        with open(stub, "wb") as f:
            f.write(b"#!/bin/sh\nprintf 'wl-mirror \\303(0.19.0\\n'\n")
        os.chmod(stub, 0o755)
        self.assertEqual(core.helper_version(stub), "wl-mirror \ufffd(0.19.0")
        _rc, out, _err = run(["--check"], helper=stub)
        self.assertIn("helper:   %s (wl-mirror \ufffd(0.19.0)" % stub, out)

    def test_capture_globals_are_read_from_the_registry(self):
        conn = mock.Mock()
        conn.find_global.side_effect = lambda i: (7, 3) if i == \
            core.SCREENCOPY else None
        self.assertEqual(core.capture_support(conn), [(core.SCREENCOPY, 3)])
        conn.find_global.side_effect = lambda i: None
        self.assertEqual(core.capture_support(conn), [])
        with self.assertRaises(core.Refusal):
            core.require_capture(conn)

    def test_export_dmabuf_alone_is_not_enough(self):
        """It is what `auto` picks for a whole output, but a region cannot
        use it (measured: a region falls back to shm), so it never counts as
        capture support on its own."""
        self.assertNotIn("zwlr_export_dmabuf_manager_v1", core.CAPTURE_GLOBALS)

    def test_no_capture_names_the_protocol_and_the_portal(self):
        lines = core.no_capture_lines()
        joined = " ".join(lines)
        self.assertIn(core.SCREENCOPY, joined)
        self.assertIn(core.EXTCOPY, joined)
        self.assertIn("GNOME and KDE", joined)
        self.assertIn("portal", joined)

    def test_the_second_capture_protocol_is_not_a_footnote(self):
        """The line used to read `wl-mirror needs wlroots (sway, hyprland, ...) or a compositor with
        ext-image-copy-capture-v1`, which reads as "wlroots, or something exotic". It is not exotic:
        `wmirror --check` passed on COSMIC with `capture: ext_image_copy_capture_manager_v1 v1` and no
        screencopy manager anywhere in cosmic-comp's 53 globals, and labwc and sway 1.12 publish both
        [M recon2/cosmic.md §3, labwc.md §2, nixos.md]."""
        second = core.no_capture_lines()[1]
        self.assertIn(core.SCREENCOPY, second)
        self.assertIn(core.EXTCOPY, second)
        self.assertIn("COSMIC", second)
        self.assertIn("labwc", second)
        self.assertNotIn("needs wlroots", second)

    def test_a_registry_with_extcopy_alone_qualifies(self):
        """The code path already accepted it; nothing had ever asked it against a compositor that really
        has one protocol and not the other. cosmic-comp is that compositor, and its recorded registry is
        the peer here -- a real Wayland socket, our own client, no mock."""
        srv = wl_fake.registry_server("cosmic")
        self.addCleanup(srv.close)
        ifaces = {i for i, _v in wl_fake.registry_fixture("cosmic")}
        self.assertNotIn(core.SCREENCOPY, ifaces)
        conn = WlConn(srv.path)
        self.addCleanup(conn.close)
        conn.get_registry()
        self.assertEqual(core.capture_support(conn), [(core.EXTCOPY, 1)])
        self.assertEqual(core.require_capture(conn), [(core.EXTCOPY, 1)])

    def test_a_registry_with_neither_is_still_the_refusal(self):
        """The control: a Mutter-shaped registry (Cinnamon's muffin, 23 globals, neither protocol) still
        raises, so the test above is about the registry and not about the call always succeeding."""
        srv = wl_fake.registry_server("cinnamon")
        self.addCleanup(srv.close)
        conn = WlConn(srv.path)
        self.addCleanup(conn.close)
        conn.get_registry()
        self.assertEqual(core.capture_support(conn), [])
        with self.assertRaises(core.Refusal):
            core.require_capture(conn)

    def test_the_install_hint_follows_the_distribution(self):
        """`apt install wl-mirror` was printed on Fedora, on Arch and on a NixOS box with no apt at all
        [M recon2/fedora.md, arch.md, nixos.md]. The Debian bytes are unchanged, which is what keeps
        docs/WMIRROR.md and the two tests above true."""
        want = {"debian": "on Ubuntu/Debian: sudo apt install wl-mirror",
                "fedora": "on Fedora: sudo dnf install wl-mirror",
                "arch": "on Arch: sudo pacman -S wl-mirror",
                "nixos": "on NixOS: sudo nix-env -iA nixpkgs.wl-mirror",
                None: "on Ubuntu/Debian: sudo apt install wl-mirror"}
        for fam, line in want.items():
            with mock.patch.object(distro, "family", return_value=fam):
                self.assertEqual(core.install_hint(), line, fam)
                self.assertEqual(core.missing_helper_lines()[1], line, fam)

    def test_an_x11_session_is_told_it_is_one(self):
        from w11common import passthrough
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)
        with mock.patch.object(passthrough, "session_kind",
                               return_value="x11"):
            lines = core.no_session_lines()
        self.assertIn("X11 session", lines[0])
        self.assertIn("--same-as", " ".join(lines))

    def test_every_refusal_that_names_a_gap_names_its_route(self):
        """AGENTS.md, applied to the three lines wmirror prints when it can do nothing.  A region mirror on
        X11 and a capture on GNOME/KDE are both things this toolbox does not do YET, and a user who reads
        `there is no route` stops looking -- so each line ends in the work that would close it and what that
        work costs, and none of them stops at what a compositor lacks."""
        from w11common import passthrough
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)
        with mock.patch.object(passthrough, "session_kind", return_value="x11"):
            x11 = " ".join(core.no_session_lines())
        self.assertIn("not done here yet", x11)
        self.assertIn("XShm", x11)
        self.assertNotIn("no route", x11)

        capture = " ".join(core.no_capture_lines())
        self.assertIn("AGENTS.md route 4", capture)
        self.assertIn("not wired up here yet", capture)

        with mock.patch.object(wxcore, "WlrOutputs", side_effect=wxcore.Fatal("no manager")):
            with self.assertRaises(core.Refusal) as cm:
                core.read_outputs(mock.Mock())
        layout = " ".join(cm.exception.lines)
        self.assertIn(core.OUTPUT_MANAGER, layout)
        self.assertIn("AGENTS.md route 2", layout)
        self.assertIn("not yet here", layout)

    def test_the_escape_hatch_does_not_decide_this(self):
        """W11_PASSTHROUGH is about handing over to an X11 original,
        and wmirror has none -- it must not turn an X11 box into a Wayland
        one here (the reasoning warandr's randr.choose() uses)."""
        from w11common import passthrough
        passthrough.reset_cache()
        self.addCleanup(passthrough.reset_cache)
        with mock.patch.object(passthrough, "session_kind") as sk:
            sk.return_value = None
            core.no_session_lines()
        self.assertEqual(sk.call_args.kwargs.get("respect_override"), False)


class OutputModel(Base):
    def test_logical_size_follows_transform_and_scale(self):
        """The same arithmetic wxrandr's wlr backend uses, so wmirror and
        `wxrandr --query` can never disagree about a rectangle."""
        def head(name, enabled, x, y, transform, scale, modes, current):
            return dict(name=name, enabled=enabled, x=x, y=y,
                        transform=transform, scale=scale, current=current,
                        modes=modes, mm_w=0, mm_h=0, make="", model="",
                        serial="", id=1)

        def mode(mid, w, h):
            return {"id": mid, "w": w, "h": h, "refresh": 60000,
                    "preferred": False}

        wlr = mock.Mock()
        wlr.live_heads.return_value = [
            head("A", True, 0, 0, 0, 1.0, [mode(1, 1920, 1080)], 1),
            head("B", True, 1920, 0, 1, 2.0, [mode(2, 2560, 1440)], 2),
            head("C", False, 0, 0, 0, 1.0, [], None),
        ]
        with mock.patch.object(wxcore, "WlrOutputs", return_value=wlr):
            outs = core.read_outputs(mock.Mock())
        self.assertEqual(outs[0].geom(), "1920x1080+0+0")
        self.assertEqual(outs[1].geom(), "720x1280+1920+0")   # 90deg, scale 2
        self.assertFalse(outs[2].active)
        self.assertEqual(outs[2].geom(), "0x0+0+0")


# -- the command line ---------------------------------------------------------

class Cli(Base):
    def test_dry_run_prints_the_command_and_starts_nothing(self):
        with mock.patch.object(supervise, "start") as start:
            rc, o, e = run(["A", "--to", "B", "--region", "500x300+100+100",
                            "--dry-run"],
                           outputs=[out("A"), out("B", x=1920)])
        self.assertEqual(rc, 0, e)
        self.assertEqual(
            o.strip(),
            "/usr/bin/wl-mirror --fullscreen-output B --scaling fit "
            "--region '100,100 500x300' A")
        start.assert_not_called()

    def test_the_simple_case_never_reaches_the_helper(self):
        with mock.patch.object(supervise, "start") as start:
            rc, o, e = run(["A", "--to", "B"],
                           outputs=[out("A"), out("B", x=1920)])
        self.assertEqual(rc, 1)
        self.assertIn("--same-as", e)
        start.assert_not_called()

    def test_an_already_mirrored_pair_exits_zero_having_done_nothing(self):
        with mock.patch.object(supervise, "start") as start:
            rc, o, e = run(["A", "--to", "B"], outputs=[out("A"), out("B")])
        self.assertEqual(rc, 0)
        self.assertEqual(o, "")
        self.assertIn("already shows", e)
        start.assert_not_called()

    def test_a_missing_helper_is_one_line_with_the_package(self):
        with mock.patch.object(supervise, "start") as start:
            rc, o, e = run(["A", "--to", "B"], outputs=[out("A")],
                           helper=None)
        self.assertEqual(rc, 1)
        self.assertIn("wmirror: wl-mirror is not installed", e)
        self.assertIn(distro.hint("wl-mirror"), e)
        start.assert_not_called()

    def test_on_x11_the_missing_package_is_not_the_answer(self):
        """With no wayland socket there is no wl-mirror to install: X11
        mirrors whole outputs with xrandr, and a region has no route here.
        The order of the two checks is the whole of this test."""
        with mock.patch.object(passthrough, "session_kind",
                               return_value="x11"),                 mock.patch.object(supervise, "start") as start:
            rc, o, e = run(["A", "--to", "B"], outputs=[out("A")],
                           helper=None, wayland=None)
        self.assertEqual(rc, 1)
        self.assertIn("X11 session", e)
        self.assertNotIn(distro.hint("wl-mirror"), e)
        start.assert_not_called()

    @unittest.expectedFailure
    def test_capture_is_checked_before_the_missing_binary(self):
        """DEFERRED fix 40 (finding F5.1): `_cmd_start` decides what to say
        about a missing wl-mirror before it has asked the compositor whether
        wl-mirror could work here at all.

        On GNOME or KDE the registry carries neither zwlr_screencopy_manager_v1
        nor ext_image_copy_capture_manager_v1, so installing the package
        changes nothing -- the helper would start and exit. Today the order
        of the two checks (helper first, connection second) makes wmirror
        answer `sudo apt install wl-mirror` on exactly the desktops where
        that is wrong, which is the majority of them. The fix opens the
        connection and calls `require_capture()` first, so the missing
        protocol is the answer and the missing package is only ever named
        where installing it would help."""
        rc, o, e = run(["A", "--to", "B"], outputs=[out("A"), out("B", x=1920)],
                       helper=None, capture=False, wayland=SOCKET)
        self.assertEqual(rc, 1)
        self.assertIn(core.SCREENCOPY, e)
        self.assertNotIn(distro.hint("wl-mirror"), e)

    def test_check_says_what_is_wrong_before_it_says_what_is_installed(self):
        """Fix 40's `--check` half, landed as T21.  The `problem:` line used to
        be printed after `helper:` and `wayland:`, so the first thing an X11
        user read was "helper: not installed / sudo apt install wl-mirror" and
        the line saying there is no Wayland session here at all came fourth --
        `[helper:   not installed]` is the evidence the rig captured on all
        nine X11 flavors of run 34628777544 [goal2/recon/gaps.md 1b #9]."""
        o = io.StringIO()
        with mock.patch.object(core, "find_helper", return_value=None), \
                mock.patch.object(core, "open_conn",
                                  side_effect=core.Refusal(core.no_session_lines())), \
                mock.patch.object(session, "find_wayland_socket", return_value=None), \
                mock.patch.object(passthrough, "session_kind", return_value="x11"), \
                contextlib.redirect_stdout(o):
            rc = cli.main(["--check"])
        text = o.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("problem:", text)
        self.assertIn("X11 session", text)
        self.assertLess(text.index("problem:"), text.index("helper:"))

    def test_the_x11_sentence_is_the_very_first_line_check_prints(self):
        """What the rig reads, and it reads one line: `xwant` at
        vm/live-smoke.d/xfce.sh:101 takes `head -1` of `wmirror --check` and
        asks for X11 in it.  `assertLess` above would also pass with a row
        printed above the problem, so the stronger claim is pinned separately:
        line 1, no `apt` anywhere above it."""
        o = io.StringIO()
        with mock.patch.object(core, "find_helper", return_value=None), \
                mock.patch.object(core, "open_conn",
                                  side_effect=core.Refusal(core.no_session_lines())), \
                mock.patch.object(session, "find_wayland_socket", return_value=None), \
                mock.patch.object(passthrough, "session_kind", return_value="x11"), \
                contextlib.redirect_stdout(o):
            rc = cli.main(["--check"])
        lines = o.getvalue().splitlines()
        self.assertEqual(rc, 1)
        self.assertEqual(lines[0],
                         "problem:  this is an X11 session: there is no wl-mirror here")
        self.assertIn("helper:   not installed", lines)
        self.assertIn("wayland:  none", lines)

    def test_a_wayland_session_still_opens_with_the_helper_row(self):
        """The reorder is for the session that has a problem, and only that
        one: where the compositor answers, `--check` prints exactly the rows
        it always did, in the order it always did, with no `problem:` at all."""
        conn = mock.Mock()
        o = io.StringIO()
        with mock.patch.object(core, "find_helper",
                               return_value="/usr/bin/wl-mirror"), \
                mock.patch.object(core, "helper_version", return_value="wl-mirror 0.18.5"), \
                mock.patch.object(core, "open_conn", return_value=conn), \
                mock.patch.object(session, "find_wayland_socket",
                                  return_value=(1000, "user", SOCKET)), \
                mock.patch.object(core, "capture_support",
                                  return_value=[(core.SCREENCOPY, 3)]), \
                mock.patch.object(core, "read_outputs", return_value=[out("A")]), \
                contextlib.redirect_stdout(o):
            rc = cli.main(["--check"])
        lines = o.getvalue().splitlines()
        self.assertEqual(rc, 0)
        self.assertNotIn("problem:", o.getvalue())
        self.assertEqual(lines[0], "helper:   /usr/bin/wl-mirror (wl-mirror 0.18.5)")
        self.assertEqual(lines[1], "wayland:  " + SOCKET)

    def test_a_compositor_without_capture_says_which_protocol(self):
        rc, o, e = run(["A", "--to", "B"], outputs=[out("A")], capture=False)
        self.assertEqual(rc, 1)
        self.assertIn(core.SCREENCOPY, e)
        self.assertIn("portal", e)

    def test_bad_region_never_touches_the_compositor(self):
        rc, o, e = run(["A", "--to", "B", "--region", "nonsense"],
                       outputs=[out("A"), out("B", x=1920)])
        self.assertEqual(rc, 1)
        self.assertIn("WxH+X+Y", e)

    def test_list_is_empty_and_quiet_when_nothing_runs(self):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(["--list"])
        self.assertEqual((rc, o.getvalue(), e.getvalue()), (0, "", ""))

    def test_stop_says_so_when_there_is_nothing_to_stop(self):
        o, e = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
            rc = cli.main(["--stop", "B"])
        self.assertEqual(rc, 1)
        self.assertIn("no mirror is running on B", e.getvalue())

    def test_stop_all_is_idempotent(self):
        o = io.StringIO()
        with contextlib.redirect_stdout(o):
            self.assertEqual(cli.main(["--stop-all"]), 0)
        self.assertEqual(o.getvalue(), "")

    def test_a_query_takes_no_outputs(self):
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(["A", "--to", "B", "--list"])
        self.assertEqual(cm.exception.code, 2)

    def test_a_start_needs_both_ends(self):
        for argv in (["A"], ["--to", "B"]):
            with self.assertRaises(SystemExit) as cm:
                with contextlib.redirect_stderr(io.StringIO()):
                    cli.main(argv)
            self.assertEqual(cm.exception.code, 2)

    def test_check_reports_what_is_missing_and_fails(self):
        conn = mock.Mock()
        o = io.StringIO()
        with mock.patch.object(core, "find_helper", return_value=None), \
                mock.patch.object(core, "open_conn", return_value=conn), \
                mock.patch.object(core, "capture_support", return_value=[]), \
                mock.patch.object(core, "read_outputs",
                                  return_value=[out("A")]), \
                contextlib.redirect_stdout(o):
            rc = cli.main(["--check"])
        text = o.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("helper:   not installed", text)
        self.assertIn(distro.hint("wl-mirror"), text)
        self.assertIn(core.SCREENCOPY, text)
        self.assertIn("mirrors:  none", text)

    def test_check_reports_a_usable_session(self):
        conn = mock.Mock()
        o = io.StringIO()
        with mock.patch.object(core, "find_helper",
                               return_value="/usr/bin/wl-mirror"), \
                mock.patch.object(core, "helper_version",
                                  return_value="wl-mirror 0.18.5"), \
                mock.patch.object(core, "open_conn", return_value=conn), \
                mock.patch.object(core, "capture_support",
                                  return_value=[(core.SCREENCOPY, 3)]), \
                mock.patch.object(core, "read_outputs",
                                  return_value=[out("A"),
                                                out("B", enabled=False)]), \
                contextlib.redirect_stdout(o):
            rc = cli.main(["--check"])
        text = o.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("wl-mirror 0.18.5", text)
        self.assertIn("A 1920x1080+0+0", text)
        self.assertIn("off: B", text)

    def test_never_a_traceback(self):
        with mock.patch.object(core, "find_helper",
                               side_effect=RuntimeError("boom")):
            o, e = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
                rc = cli.main(["A", "--to", "B"])
        self.assertEqual(rc, 1)
        self.assertEqual(e.getvalue(), "wmirror: boom\n")


class CrossUid(Base):
    """Whose runtime directory the records live in.

    wmirror is documented as working from `sudo` and from `ssh root@box`: the
    Wayland socket is found by scanning /run/user, so the tool aims at the
    session whatever uid it is running as. The state file does not follow --
    `core.state_path()` is `session.runtime_dir()`, which is the CALLER's
    XDG_RUNTIME_DIR. So root and the user who owns the session keep two
    different files about the same compositor."""

    def running_record(self, source="A", target="B"):
        """A record that survives a reap: this process as the supervisor, a
        real sleeping process as the helper."""
        helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(helper.wait)
        self.addCleanup(helper.kill)
        return {"source": source, "target": target, "scaling": "fit",
                "region": None, "pid": os.getpid(),
                "start": procs.proc_starttime(os.getpid()),
                "helper_pid": helper.pid,
                "helper_start": procs.proc_starttime(helper.pid)}

    @unittest.expectedFailure
    def test_a_mirror_the_user_started_is_the_one_root_sees(self):
        """DEFERRED fix 45 (finding F5.0): `state_path()` follows the caller's
        XDG_RUNTIME_DIR instead of the runtime directory that owns the
        Wayland socket the mirror is actually on.

        Both commands here aim at the same compositor -- `find_wayland_socket`
        answers /run/user/1000/wayland-0 for the user and for root alike,
        which is the whole point of the scan. But the user's start writes
        /run/user/1000/wmirror-state.json and root's `--list` reads
        /run/root/wmirror-state.json, so root is told nothing is mirroring
        and a second start on the same target is allowed: two wl-mirrors
        fullscreen on one output, the older one invisible behind the newer,
        each recorded in a file the other command never opens.

        The fix puts the file under the runtime directory that owns the
        socket (or refuses a cross-uid start by name, and says so under
        'Lifetime' in docs/WMIRROR.md). Either way this is the assertion:
        what one caller started, the other one can see and stop."""
        user = os.path.join(self.tmp, "user")
        root = os.path.join(self.tmp, "root")
        os.makedirs(user)
        os.makedirs(root)
        sock = os.path.join(user, "wayland-0")
        open(sock, "w").close()
        rec = self.running_record()

        hit = (1000, "user", sock)
        with mock.patch.object(session, "find_wayland_socket", return_value=hit), \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": user}):
            state = core.load_state()
            core.records(state)["B"] = rec
            state.save()
            self.assertTrue(os.path.exists(os.path.join(user, "wmirror-state.json")))

        outputs = [out("A"), out("B", x=1920, w=1280, h=1024)]
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}):
            rc, o, e = run(["--list"], wayland=sock)
            self.assertEqual(rc, 0, e)
            self.assertIn("B <- A", o)
            rc, o, e = run(["A", "--to", "B"], outputs=outputs, wayland=sock)
            self.assertEqual(rc, 1)
            self.assertIn("already mirroring", e)


class DocsClaims(Base):
    """What docs/WMIRROR.md 'Lifetime' says about the state file, beside what
    the code does with it. The file is documented as plain JSON in the
    runtime directory, so its contents are part of the interface: a user who
    edits it is doing something the documentation invites."""

    def doc(self):
        """WMIRROR.md with its line wrapping flattened: every claim here is a
        sentence, and the markdown breaks them at 76 columns."""
        with open(os.path.join(ROOT, "docs", "WMIRROR.md")) as f:
            return " ".join(f.read().split())

    def test_every_pid_form_the_docs_name_is_actually_rejected(self):
        text = self.doc()
        self.assertIn("only a positive integer names a process", text)
        for shown, value in (('`"4242"`', "4242"), ("`1.5`", 1.5), ("`-1`", -1)):
            self.assertIn(shown, text, "the docs stopped naming %r" % (value,))
            self.assertIsNone(procs.as_pid(value), repr(value))
        recs = {"B": {"source": "A", "pid": "4242", "helper_pid": [1]}}
        self.assertTrue(supervise.reap(recs))
        self.assertEqual(recs, {}, "the docs promise such a record is reaped")

    def test_the_no_starttime_fallback_matches_what_the_docs_claim(self):
        """The docs say a `?` record is matched on the COMMAND LINE, and name
        the three shapes the command ships in. SUPERVISOR_COMM is the hint
        that gets used, and none of it may be a bare interpreter name."""
        text = self.doc()
        self.assertIn("falls back to matching the process's own **command line**", text)
        self.assertIn("`python3 -m wmirror`", text)
        self.assertEqual(supervise.SUPERVISOR_COMM, ("wmirror",))


class Separation(Base):
    """wmirror is additive: no other tool changed to make room for it."""

    def test_the_state_file_is_ours_alone(self):
        state = core.load_state()
        core.records(state)["B"] = {"source": "A", "target": "B"}
        state.save()
        self.assertTrue(os.path.exists(self.state_file()))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "wxrandr-state.json")))

    def test_a_query_that_found_nothing_writes_nothing(self):
        """`wmirror --list` on a session that never mirrored must not leave
        a state file behind, and must not rewrite one it did not change."""
        o = io.StringIO()
        with contextlib.redirect_stdout(o):
            self.assertEqual(cli.main(["--list"]), 0)
        self.assertFalse(os.path.exists(self.state_file()))
        self.assertFalse(supervise.reap({}))

    def test_a_hand_edited_record_of_the_wrong_type_is_ignored(self):
        state = core.load_state()
        state.d["mirrors"] = "not a dict"
        self.assertEqual(core.records(state), {})

    def test_no_other_package_knows_about_wmirror(self):
        """The feature is one new command. If a backend, the daemon or the
        GUI had to learn a new word for it, it went in the wrong place."""
        offenders = []
        for pkg in ("w11common", "wdotool", "wxrandr", "warandr", "wwmctl",
                    "wxprop"):
            base = os.path.join(ROOT, pkg)
            for name in sorted(os.listdir(base)):
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(base, name)) as f:
                    if "wmirror" in f.read():
                        offenders.append("%s/%s" % (pkg, name))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
