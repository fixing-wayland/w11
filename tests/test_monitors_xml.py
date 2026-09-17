"""`hacks/display/monitors_xml.py`: reading GNOME's saved display configuration, judging it
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

import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

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
        snap = mx.snapshot(self.path)
        self.addCleanup(snap.close)
        self.assertEqual((snap.path, snap.data), (self.path, data))
        self.assertEqual(tuple(snap), (self.path, data))     # still the pair it used to be
        st = os.stat(self.path)
        self.assertEqual(snap.owner, (st.st_uid, st.st_gid))     # read off the descriptor
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

    def test_a_snapshot_is_consumed_once(self):
        """It holds an open directory descriptor, so it is not a value that can be kept:
        `keep_backup()` closes it, closing it a second time is a no-op, and a snapshot
        handed over twice copies nothing rather than writing through a descriptor number
        that by then belongs to some other file."""
        self.write()
        snap = mx.snapshot(self.path)
        self.assertGreaterEqual(snap.dfd, 0)
        self.assertEqual(mx.keep_backup(snap), self.path + mx.BACKUP_SUFFIX)
        self.assertEqual(snap.dfd, -1)
        snap.close()
        self.assertIsNone(mx.keep_backup(snap))

    def test_a_backup_that_cannot_be_written_is_not_a_failure(self):
        self.write()
        snap = mx.snapshot(self.path)
        # read-only directory.  The snapshot is taken first because it is the snapshot
        # that opens the directory; the descriptor it holds is not a way around the
        # permissions on it, which is the whole of what this asserts -- creating the
        # temp with `dir_fd=` is refused exactly as creating it by name was.
        os.chmod(self.tmp, stat.S_IRUSR | stat.S_IXUSR)
        self.assertIsNone(mx.keep_backup(snap))
        os.chmod(self.tmp, 0o700)
        self.assertEqual(os.listdir(self.tmp), ["monitors.xml"])   # no half-written temp


class PlantedPaths(unittest.TestCase):
    """The cross-account case the module exists for, from the other side.

    `snapshot()`/`keep_backup()` run as root against paths inside the session user's
    home whenever wxrandr reconfigures somebody else's session (hacks/display/mutter.py
    calls `snapshot(uid=wsession.session_uid(), ...)` on every `--persistent` apply, and
    `default_path()` resolves that uid's `pw_dir`).  `monitors.xml`, its backup and the
    backup's `.tmp` are all fixed names in a directory that account owns, so each of
    them can be a symlink by the time we arrive.  These tests plant one at each of the
    three names.  `ROOT-TARGET` stands in for the file outside the directory that must
    not be read, written or created -- `/etc/shadow` and `/etc/cron.d/w11` in the
    report; here it is a sibling, because the test has no root to lose."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-mx-")
        self.path = os.path.join(self.tmp, "monitors.xml")
        self.target = os.path.join(self.tmp, "ROOT-TARGET")

    def tearDown(self):
        for f in os.listdir(self.tmp):
            os.unlink(os.path.join(self.tmp, f))    # links included; nothing here is a directory
        os.rmdir(self.tmp)

    def write(self, data=b"<monitors version=\"2\"/>\n"):
        with open(self.path, "wb") as f:
            f.write(data)
        return data

    def victim(self, data=b"IMPORTANT\n"):
        with open(self.target, "wb") as f:
            f.write(data)
        return data

    def test_a_symlink_planted_at_the_temp_name_is_not_written_through(self):
        untouched = self.victim()
        data = self.write()
        os.symlink(self.target, self.path + mx.BACKUP_SUFFIX + ".tmp")
        backup = mx.keep_backup(mx.snapshot(self.path))
        self.assertEqual(backup, self.path + mx.BACKUP_SUFFIX)
        with open(self.target, "rb") as f:
            self.assertEqual(f.read(), untouched)       # not truncated, not overwritten
        self.assertFalse(os.path.islink(backup))        # and the link did not survive the rename
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertFalse(os.path.lexists(self.path + mx.BACKUP_SUFFIX + ".tmp"))

    def test_a_dangling_symlink_at_the_temp_name_creates_nothing(self):
        self.write()
        os.symlink(self.target, self.path + mx.BACKUP_SUFFIX + ".tmp")
        self.assertEqual(mx.keep_backup(mx.snapshot(self.path)),
                         self.path + mx.BACKUP_SUFFIX)
        # the O_CREAT through the link is what used to bring the target into being
        self.assertFalse(os.path.lexists(self.target))

    def test_a_symlink_at_the_backup_name_is_replaced_not_followed(self):
        untouched = self.victim()
        data = self.write()
        os.symlink(self.target, self.path + mx.BACKUP_SUFFIX)
        backup = mx.keep_backup(mx.snapshot(self.path))
        self.assertEqual(backup, self.path + mx.BACKUP_SUFFIX)
        with open(self.target, "rb") as f:
            self.assertEqual(f.read(), untouched)
        self.assertFalse(os.path.islink(backup))        # os.replace renamed over the link
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_a_symlink_at_the_config_path_is_not_read(self):
        self.victim(b"secret-bytes\n")
        os.symlink(self.target, self.path)
        before = sorted(os.listdir(self.tmp))
        self.assertIsNone(mx.snapshot(self.path))       # ELOOP, not the target's bytes
        self.assertEqual(sorted(os.listdir(self.tmp)), before)

    def test_a_directory_at_the_config_path_is_not_read(self):
        # the non-regular case that can be tested without a second process: a FIFO would
        # need a writer before the open returns, which is exactly what the O_NONBLOCK in
        # snapshot() is there to make moot
        os.mkdir(self.path)
        try:
            self.assertIsNone(mx.snapshot(self.path))
        finally:
            os.rmdir(self.path)

    def test_the_chown_goes_to_the_descriptor(self):
        data = self.write()
        calls = []
        theirs = (os.geteuid() + 1, os.getgid())
        with mock.patch.object(mx, "_fowner", return_value=theirs), \
                mock.patch("hacks.display.monitors_xml.os.fchown",
                           side_effect=lambda *a: calls.append(a)):
            backup = mx.keep_backup(mx.snapshot(self.path, uid=theirs[0]))
        self.assertEqual(backup, self.path + mx.BACKUP_SUFFIX)
        (fd, uid, gid), = calls
        self.assertIsInstance(fd, int)                  # the open descriptor, never the path
        self.assertEqual((uid, gid), theirs)
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_the_chown_goes_to_the_owner_the_read_saw_and_not_to_the_name(self):
        """The owner is `snapshot()`'s `fstat` of the file it read, carried on the
        snapshot, and not a second `os.stat` of the path.

        The two answers are allowed to differ, and the gap between them is as wide as
        the apply: hacks/display/mutter.py takes the snapshot at :1025 and calls
        `keep_backup` at :1055, with the whole `ApplyMonitorsConfig` round trip and
        Mutter's own "Keep changes?" dialog in between, during which the name is one the
        session user can replace with a symlink to anybody's file.  Here `os.stat` is
        made to report a third account for the path; what the chown is given is still
        what the descriptor said."""
        theirs = (os.geteuid() + 1, os.getgid())
        by_name = (os.geteuid() + 2, os.getgid() + 2)
        self.write()
        with mock.patch.object(mx, "_fowner", return_value=theirs):
            snap = mx.snapshot(self.path, uid=theirs[0])
        self.assertEqual(snap.owner, theirs)
        calls = []
        faked = os.stat_result((0o100644, 1, 1, 1, by_name[0], by_name[1], 0, 0, 0, 0))
        with mock.patch("hacks.display.monitors_xml.os.stat", return_value=faked), \
                mock.patch("hacks.display.monitors_xml.os.fchown",
                           side_effect=lambda *a: calls.append(a)):
            self.assertEqual(mx._owner(self.path), by_name)      # what the name says now
            backup = mx.keep_backup(snap)
        self.assertEqual(backup, self.path + mx.BACKUP_SUFFIX)
        (_fd, uid, gid), = calls
        self.assertEqual((uid, gid), theirs)

    def test_a_chown_we_are_not_allowed_to_make_still_leaves_no_copy(self):
        self.write()
        err = io.StringIO()
        theirs = (os.geteuid() + 1, os.getgid())
        with mock.patch.object(mx, "_fowner", return_value=theirs), \
                mock.patch("hacks.display.monitors_xml.os.fchown",
                           side_effect=PermissionError(1, "Operation not permitted")), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(mx.keep_backup(mx.snapshot(self.path, uid=theirs[0])))
        self.assertFalse(os.path.lexists(self.path + mx.BACKUP_SUFFIX))
        self.assertFalse(os.path.lexists(self.path + mx.BACKUP_SUFFIX + ".tmp"))
        self.assertIn("no copy of %s could be kept" % self.path, err.getvalue())

    def test_a_file_that_grows_after_it_is_opened_is_still_refused(self):
        """`fstat` says how big the file was when it was asked, and the account that
        owns it goes on writing: the size on the descriptor is not a promise about what
        `read()` will return.  Here the `fstat` is made to report a few bytes for a file
        that is over the limit, which is what an appender racing the open produces; the
        bound that has to hold is the one on what actually arrived."""
        self.write(b"<monitors>" + b"x" * mx.MAX_BYTES)
        real = os.fstat

        def small(fd):
            st = real(fd)
            if not stat.S_ISREG(st.st_mode):
                return st
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink,
                                   st.st_uid, st.st_gid, 10, 0, 0, 0))

        with mock.patch("hacks.display.monitors_xml.os.fstat", side_effect=small):
            self.assertIsNone(mx.snapshot(self.path))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["monitors.xml"])


class PlantedDirectories(unittest.TestCase):
    """The same account, one component further up: `~/.config` itself.

    `O_NOFOLLOW` guards the last name in a path and nothing above it, and `default_path()`
    builds `<pw_dir>/.config/<name>` (hacks/display/monitors_xml.py), so when root
    reconfigures somebody else's session every component from `.config` down is a name
    that account owns and can replace.  A directory symlink planted there sent the read,
    the copy and the chown into any directory root can traverse that holds a
    `monitors.xml` -- the file itself never being a symlink, so nothing in the leaf
    checks had a word to say about it.  `snapshot()` therefore opens the directory first,
    refuses one that does not belong to the account whose file this is, and hands
    `keep_backup()` that descriptor instead of the path.

    Following the link is not the hazard and is not refused: `~/.config ->
    ~/dotfiles/config` is a setup people really run, and a directory of their own is a
    directory of their own however it is spelled.  What is refused is a directory that
    is not theirs.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wxrandr-mx-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.theirs = os.path.join(self.tmp, "dotfiles-config")     # where the link points
        self.elsewhere = os.path.join(self.tmp, "elsewhere")        # where it points later
        os.mkdir(self.theirs)
        os.mkdir(self.elsewhere)
        self.config = os.path.join(self.tmp, ".config")
        os.symlink(self.theirs, self.config)
        self.path = os.path.join(self.config, "monitors.xml")       # the name as it is typed
        self.data = b'<monitors version="2"><!--theirs--></monitors>\n'
        with open(os.path.join(self.theirs, "monitors.xml"), "wb") as f:
            f.write(self.data)

    def names(self, d):
        return sorted(os.listdir(d))

    def test_a_config_directory_that_is_a_link_to_their_own_is_read_and_copied(self):
        snap = mx.snapshot(self.path)
        self.assertEqual(snap.data, self.data)
        self.assertEqual(mx.keep_backup(snap), self.path + mx.BACKUP_SUFFIX)
        self.assertEqual(self.names(self.theirs),
                         ["monitors.xml", "monitors.xml.wxrandr-backup"])
        with open(os.path.join(self.theirs, "monitors.xml.wxrandr-backup"), "rb") as f:
            self.assertEqual(f.read(), self.data)
        self.assertEqual(self.names(self.elsewhere), [])
        self.assertEqual(snap.dfd, -1)              # and the descriptor did not outlive the copy

    def test_a_config_directory_that_is_not_theirs_is_not_read_from_at_all(self):
        """The directory really belongs to this runner; `uid` says whose session it is.
        That is the shape of the cross-account run -- root resolving `<pw_dir>/.config`
        for the seated user and arriving somewhere that account does not own -- without
        needing a root the test does not have."""
        self.assertIsNone(mx.snapshot(self.path, uid=os.geteuid() + 1))
        self.assertEqual(self.names(self.theirs), ["monitors.xml"])
        self.assertEqual(self.names(self.elsewhere), [])

    def test_the_copy_lands_in_the_directory_the_bytes_came_from(self):
        """The window the descriptor closes, played out: the link is re-pointed after
        the read and before the copy, which is a `ln -sfn` the session user can run at
        any moment of the apply (hacks/display/mutter.py snapshots at :1025 and copies at
        :1055, with the whole ApplyMonitorsConfig round trip and Mutter's own "Keep
        changes?" dialog in between).  The copy still goes where the bytes came from,
        because `keep_backup()` has no path left to walk."""
        snap = mx.snapshot(self.path)
        os.unlink(self.config)
        os.symlink(self.elsewhere, self.config)
        self.assertEqual(mx.keep_backup(snap), self.path + mx.BACKUP_SUFFIX)
        self.assertEqual(self.names(self.theirs),
                         ["monitors.xml", "monitors.xml.wxrandr-backup"])
        self.assertEqual(self.names(self.elsewhere), [])


if __name__ == "__main__":
    unittest.main()
