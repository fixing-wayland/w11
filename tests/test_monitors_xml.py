"""`wxrandr/monitors_xml.py`: reading GNOME's saved display configuration, judging it
the way Mutter's reader judges it, and keeping a copy of it.

The fixtures are real files: `tests/fixtures/monitors-gnome50.xml` and
`monitors-gnome46.xml` were written by Mutter itself on the 26.04 and 24.04 default
installs (`vm/vmctl start ... resolute-gnome-iso` / `noble-gnome-iso`), by a confirmed
`wxrandr --persistent`, one entry per monitor set -- a three-head layout with one head
rotated left, and a two-head layout with one head at scale 2.

The fact these tests are here to keep true, measured on both releases: **the file is
all or nothing.**  Mutter's reader verifies every `<configuration>` in it and one
failure discards the lot --

    Failed to read monitors config file '/home/test/.config/monitors.xml':
    Logical monitors not adjacent

-- after which the session comes up in a default row and every other monitor set the
user had saved is inactive, silently, at every login.  Mutter's *writer* verifies
nothing, so a file in that state can be written by anything that reaches libmutter
behind DisplayConfig's back, and it is then rewritten -- whole, from what Mutter holds
in memory, i.e. without the discarded entries -- by the next confirmed save.
`tests/test_wxrandr_mutter.py:SavedConfigurationFile` is the other half of this: that
no path through our own tools can write the file at all.
"""

import os
import stat
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The suite never hands a tool over to the real X11 one: see tests/conftest.py.
os.environ["W11_PASSTHROUGH"] = "never"

from hacks.display import monitors_xml as mx

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def xml(*configurations):
    return "<monitors version=\"2\">\n" + "\n".join(configurations) + "\n</monitors>\n"


def cfg(*monitors, layoutmode="logical"):
    body = "".join(monitors)
    mode = "<layoutmode>%s</layoutmode>" % layoutmode if layoutmode else ""
    return "  <configuration>%s%s</configuration>" % (mode, body)


def monitor(connector, w=1920, h=1080):
    return ("<monitor><monitorspec><connector>%s</connector><vendor>V</vendor>"
            "<product>P</product><serial>S</serial></monitorspec>"
            "<mode><width>%d</width><height>%d</height><rate>60.000</rate></mode>"
            "</monitor>" % (connector, w, h))


def lm(connectors, x, y, scale=1, w=1920, h=1080, primary=False, rotation=None):
    """One <logicalmonitor>; several connectors is what a mirrored pair looks like."""
    if isinstance(connectors, str):
        connectors = [connectors]
    rot = ("<transform><rotation>%s</rotation><flipped>no</flipped></transform>"
           % rotation) if rotation else ""
    return ("<logicalmonitor><x>%d</x><y>%d</y><scale>%s</scale>%s%s%s"
            "</logicalmonitor>"
            % (x, y, scale, "<primary>yes</primary>" if primary else "", rot,
               "".join(monitor(c, w, h) for c in connectors)))


class ParseRealFiles(unittest.TestCase):
    def test_gnome50_file_has_both_saved_monitor_sets(self):
        configs = mx.parse(fixture("monitors-gnome50.xml").decode())
        self.assertEqual([c.connectors for c in configs],
                         [["Virtual-1", "Virtual-2", "Virtual-3"],
                          ["Virtual-1", "Virtual-2"]])
        self.assertEqual([c.layout_mode for c in configs], ["logical", "logical"])

    def test_a_rotated_head_swaps_its_pixels(self):
        three = mx.parse(fixture("monitors-gnome50.xml").decode())[0]
        rotated = three.regions[-1]                   # Virtual-3, --rotate left
        self.assertEqual((rotated.w, rotated.h), (1080, 1920))
        self.assertEqual(rotated.rect("logical"), (3840, 0, 1080, 1920))

    def test_scale_divides_only_in_logical_layout_mode(self):
        two = mx.parse(fixture("monitors-gnome50.xml").decode())[1]
        scaled = two.regions[-1]                      # Virtual-2, --scale 2
        self.assertEqual(scaled.rect("logical"), (1920, 0, 960, 540))
        self.assertEqual(scaled.rect("physical"), (1920, 0, 1920, 1080))

    def test_the_primary_flag_is_read(self):
        first = mx.parse(fixture("monitors-gnome50.xml").decode())[0]
        self.assertEqual([r.primary for r in first.regions], [True, False, False])

    def test_gnome46_writes_no_layout_mode_at_all(self):
        """Its default is physical, and Mutter writes <layoutmode> only for logical --
        which is why an entry from 24.04 means whatever the session means today."""
        configs = mx.parse(fixture("monitors-gnome46.xml").decode())
        self.assertEqual([c.layout_mode for c in configs], [None, None])
        self.assertEqual([c.connectors for c in configs],
                         [["Virtual-1", "Virtual-2"],
                          ["Virtual-1", "Virtual-2", "Virtual-3"]])

    def test_the_real_file_that_rots_when_fractional_scaling_goes_on(self):
        """monitors-gnome46-scaled.xml is the file 24.04 wrote for `--scale 2` on the
        first head of a row: valid as saved, and discarded whole at the next login once
        Fractional Scaling is on (`Logical monitors not adjacent` in the journal --
        measured, and the reason for the warning wxrandr prints when it saves one)."""
        configs = mx.parse(fixture("monitors-gnome46-scaled.xml").decode())
        self.assertEqual(mx.problems(configs, mx.PHYSICAL), [])
        problem, = mx.problems(configs, mx.LOGICAL)
        self.assertIn("not adjacent", problem)

    def test_a_real_file_has_nothing_wrong_with_it(self):
        """Judged in the layout mode of the session that wrote it: every one of these
        came out of a real Mutter, so a complaint here would be ours, not GNOME's."""
        for name in sorted(os.listdir(FIXTURES)):
            if name.startswith("monitors-"):
                mode = mx.LOGICAL if "gnome50" in name else mx.PHYSICAL
                configs = mx.parse(fixture(name).decode())
                self.assertEqual(mx.problems(configs, mode), [], name)


class Verify(unittest.TestCase):
    """mutter's meta_verify_logical_monitor_config_list(), on file contents."""

    def problems(self, text):
        return mx.problems(mx.parse(text))

    def test_a_row_is_fine(self):
        self.assertEqual(self.problems(xml(cfg(lm("A", 0, 0, primary=True),
                                               lm("B", 1920, 0)))), [])

    def test_an_overlap_is_named_with_its_configuration(self):
        """Mutter checks adjacency first, so a half-overlapping pair is refused as
        "not adjacent" -- the sentence that then turns up in the journal."""
        text = xml(cfg(lm("A", 0, 0, primary=True), lm("B", 1920, 0)),
                   cfg(lm("A", 0, 0, primary=True), lm("B", 100, 0)))
        problem, = self.problems(text)
        self.assertTrue(problem.startswith("configuration 2 (A, B): "), problem)
        self.assertIn("not adjacent", problem)
        self.assertIn("an overlap counts", problem)

    def test_a_region_that_touches_an_edge_and_still_overlaps(self):
        """The other refusal: everything is adjacent to something, and two of them are
        in the same place anyway."""
        text = xml(cfg(lm("A", 0, 0, primary=True), lm("B", 1920, 0), lm("C", 1920, 0)))
        self.assertEqual(self.problems(text),
                         ["configuration 1 (A, B, C): logical monitors overlap"])

    def test_a_gap_is_not_adjacent_just_as_mutter_says(self):
        text = xml(cfg(lm("A", 0, 0, primary=True), lm("B", 2000, 0)))
        self.assertEqual(len(self.problems(text)), 1)
        self.assertIn("not adjacent", self.problems(text)[0])

    def test_corner_contact_is_not_adjacency(self):
        text = xml(cfg(lm("A", 0, 0, primary=True), lm("B", 1920, 1080)))
        self.assertIn("not adjacent", self.problems(text)[0])

    def test_a_layout_that_does_not_start_at_the_origin(self):
        text = xml(cfg(lm("A", 100, 0, primary=True), lm("B", 2020, 0)))
        self.assertIn("not anchored at 0,0", self.problems(text)[0])

    def test_one_monitor_alone_needs_no_neighbour(self):
        self.assertEqual(self.problems(xml(cfg(lm("A", 0, 0, primary=True)))), [])

    def test_a_mirrored_pair_is_one_region_at_one_position(self):
        mirror = lm(["A", "B"], 0, 0, primary=True)
        self.assertEqual(self.problems(xml(cfg(mirror))), [])
        configs = mx.parse(xml(cfg(mirror)))
        self.assertEqual(configs[0].connectors, ["A", "B"])
        self.assertEqual(len(configs[0].regions), 1)

    def test_the_scale_is_applied_before_the_geometry_is_judged(self):
        # A at scale 2 is 960 wide in logical layout mode, so B at 960 is adjacent
        # there and overlapping in physical: the file says which one Mutter used.
        row = (lm("A", 0, 0, scale=2, primary=True), lm("B", 960, 0))
        self.assertEqual(self.problems(xml(cfg(*row, layoutmode="logical"))), [])
        self.assertIn("not adjacent",
                      self.problems(xml(cfg(*row, layoutmode="physical")))[0])

    def test_a_file_without_a_layout_mode_is_only_faulted_when_both_modes_fault(self):
        row = (lm("A", 0, 0, scale=2, primary=True), lm("B", 960, 0))
        self.assertEqual(self.problems(xml(cfg(*row, layoutmode=None))), [])
        bad = (lm("A", 0, 0, primary=True), lm("B", 4000, 0))
        self.assertIn("not adjacent", self.problems(xml(cfg(*bad, layoutmode=None)))[0])

    def test_every_bad_entry_is_listed_even_though_one_is_enough(self):
        text = xml(cfg(lm("A", 0, 0, primary=True), lm("B", 100, 0)),
                   cfg(lm("A", 0, 0, primary=True), lm("B", 5000, 0)))
        self.assertEqual(len(self.problems(text)), 2)


class TheSessionsLayoutMode(unittest.TestCase):
    """GNOME 46 writes no <layoutmode> (its default is physical) and GNOME 50 writes
    `logical`, so an entry without one means "whatever the session is in now" -- which
    is how a file that was valid when it was written stops being valid when the user
    turns Fractional Scaling on.  Measured on 24.04: a scale-2 head at 0,0 with its
    neighbour saved at 1920 comes back as

        Failed to read monitors config file '...': Logical monitors not adjacent

    and the whole file, both saved monitor sets, is discarded.
    """

    def setUp(self):
        # valid in physical layout mode (A is 1920 wide there), a 960 gap in logical
        self.text = xml(cfg(lm("A", 0, 0, scale=2, primary=True), lm("B", 1920, 0),
                            layoutmode=None))

    def test_judged_in_the_layout_mode_the_session_is_in(self):
        self.assertEqual(mx.problems(mx.parse(self.text), mx.PHYSICAL), [])
        problem, = mx.problems(mx.parse(self.text), mx.LOGICAL)
        self.assertIn("not adjacent", problem)

    def test_unknown_session_mode_keeps_its_mouth_shut(self):
        self.assertEqual(mx.problems(mx.parse(self.text)), [])

    def test_an_entry_that_names_its_mode_is_judged_in_that_one(self):
        stated = xml(cfg(lm("A", 0, 0, scale=2, primary=True), lm("B", 1920, 0),
                         layoutmode="physical"))
        self.assertEqual(mx.problems(mx.parse(stated), mx.LOGICAL), [])

    def test_fault_reads_plain_rectangles(self):
        self.assertIsNone(mx.fault([(0, 0, 1920, 1080), (1920, 0, 1920, 1080)]))
        self.assertIn("not adjacent",
                      mx.fault([(0, 0, 960, 540), (1920, 0, 1920, 1080)]))
        self.assertIsNone(mx.fault([]))


class Describe(unittest.TestCase):
    def test_a_good_file_says_nothing(self):
        self.assertEqual(mx.describe(("/x/monitors.xml",
                                      fixture("monitors-gnome50.xml"))), [])

    def test_no_file_at_all_says_nothing(self):
        self.assertEqual(mx.describe(None), [])

    def test_a_discarded_file_is_reported_as_gone_whole(self):
        text = xml(cfg(lm("A", 0, 0, primary=True), lm("B", 1920, 0)),
                   cfg(lm("A", 0, 0, primary=True), lm("B", 100, 0)))
        line, = mx.describe(("/x/monitors.xml", text.encode()))
        self.assertIn("GNOME has already discarded /x/monitors.xml", line)
        self.assertIn("configuration 2 (A, B): logical monitors not adjacent", line)
        self.assertIn("every layout saved in it is inactive", line)
        self.assertTrue(line.endswith("\n"))

    def test_a_file_that_is_not_xml_is_reported_too(self):
        line, = mx.describe(("/x/monitors.xml", b"<monitors><confi"))
        self.assertIn("not valid XML", line)


class SnapshotAndBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-mx-")
        self.path = os.path.join(self.tmp, "monitors.xml")

    def tearDown(self):
        os.chmod(self.tmp, 0o700)
        for f in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    def write(self, data=b"<monitors version=\"2\"/>\n"):
        with open(self.path, "wb") as f:
            f.write(data)
        return data

    def test_default_path_follows_xdg_config_home(self):
        self.assertEqual(mx.default_path({"XDG_CONFIG_HOME": "/c", "HOME": "/h"}),
                         "/c/monitors.xml")
        self.assertEqual(mx.default_path({"HOME": "/h"}), "/h/.config/monitors.xml")
        # a relative XDG_CONFIG_HOME is ignored, exactly as the spec says
        self.assertEqual(mx.default_path({"XDG_CONFIG_HOME": "rel", "HOME": "/h"}),
                         "/h/.config/monitors.xml")

    def test_snapshot_reads_and_writes_nothing(self):
        data = self.write()
        before = sorted(os.listdir(self.tmp))
        self.assertEqual(mx.snapshot(self.path), (self.path, data))
        self.assertEqual(sorted(os.listdir(self.tmp)), before)

    def test_no_file_is_not_an_error(self):
        self.assertIsNone(mx.snapshot(self.path))
        self.assertIsNone(mx.keep_backup(None))

    def test_a_file_too_big_to_be_one_is_left_alone(self):
        self.write(b"<monitors>" + b"x" * mx.MAX_BYTES)
        self.assertIsNone(mx.snapshot(self.path))

    def test_the_backup_holds_the_bytes_from_before_the_apply(self):
        data = self.write(fixture("monitors-gnome50.xml"))
        backup = mx.keep_backup(mx.snapshot(self.path))
        self.assertEqual(backup, self.path + mx.BACKUP_SUFFIX)
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), data)
        # and the file Mutter reads is untouched
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_the_backup_is_replaced_not_appended_to(self):
        self.write(b"<monitors version=\"2\"><!--one--></monitors>")
        mx.keep_backup(mx.snapshot(self.path))
        second = self.write(b"<monitors version=\"2\"><!--two--></monitors>")
        mx.keep_backup(mx.snapshot(self.path))
        with open(self.path + mx.BACKUP_SUFFIX, "rb") as f:
            self.assertEqual(f.read(), second)

    def test_a_backup_that_cannot_be_written_is_not_a_failure(self):
        snap = (self.path, self.write())
        os.chmod(self.tmp, stat.S_IRUSR | stat.S_IXUSR)     # read-only directory
        self.assertIsNone(mx.keep_backup(snap))
        os.chmod(self.tmp, 0o700)
        self.assertEqual(os.listdir(self.tmp), ["monitors.xml"])   # no half-written temp


if __name__ == "__main__":
    unittest.main()
