#!/usr/bin/env python3
"""`wxrandr --persistent` on sway: the layout in a file sway `include`s.

xrandr has no --persistent -- it is wxrandr's own flag -- so there is no oracle for WHERE a sway layout is
kept, only AGENTS.md's rule that a layout X could save is one we save too and the measured fact that sway
keeps NOTHING of an IPC apply on disk (docs/WXRANDR.md's state-restoration table: "nothing on disk; only
~/.config/sway/config makes a layout stick").  So --persistent writes `~/.config/sway/w11-outputs.conf`, one
`output ...` line per output it touched, and makes sway's config `include` it so the layout comes back at the
next start (AGENTS.md route 2).  The live apply has already landed over the IPC, so nothing here reloads the
running session.

This pins the file's bytes, the once-never-twice `include`, that a hand-edited config is left byte-for-byte
alone once the `include` is there, and that a run WITHOUT --persistent writes nothing at all.

Bare `python3 tests/test_wxrandr_sway_persistent.py` and under the suite both work; no display, no compositor
-- the file half is `hacks/display/core`'s and is exercised directly, and the one end-to-end case drives it
through `SwayBackend.apply` over a `support.FakeSway`.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from hacks.display import core  # noqa: E402


def mk_fresh(name, w, h, x=0, y=0, refresh=60000, transform="normal", scale=1.0, active=True):
    """One post-apply OutputState, the shape `persist_sway_layout` reads its lines off."""
    o = core.OutputState(name=name, active=active, x=x, y=y, transform=transform, scale=scale)
    if active:
        o.w, o.h = w, h
        o.current = core.Mode(w=w, h=h, refresh_mhz=refresh)
    return o


def mk_target(name, changed=True):
    return core.Target(output=core.OutputState(name=name, active=True), stanza=None, changed=changed)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="wxr-sway-cfg-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.xdg = os.path.join(self.home, "config")
        self.confdir = os.path.join(self.xdg, "sway")
        os.makedirs(self.confdir)
        self.conf = os.path.join(self.confdir, "config")
        self.rules = os.path.join(self.confdir, "w11-outputs.conf")
        patcher = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.xdg, "HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The session is ours unless a test below says otherwise.  `session_uid()` scans /run/user/* and
        # logind, so on a host seated by a DIFFERENT account -- a builder, a shared machine, the rig's own
        # session under a service user -- `SwayBackend.apply` would take the skip branch and every file
        # assertion in this file would be about a file nothing wrote.
        uidp = mock.patch.object(core.session, "session_uid", return_value=os.geteuid())
        uidp.start()
        self.addCleanup(uidp.stop)

    def persist(self, targets, fresh):
        """Run the file half and return its stderr (the warnings `warn()` emits)."""
        err = io.StringIO()
        with redirect_stderr(err):
            core.persist_sway_layout(targets, fresh)
        return err.getvalue()

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def rule_lines(self):
        return [ln for ln in self.read(self.rules).splitlines() if ln.startswith("output ")]


class TheLine(Base):
    """`_sway_persist_line`: the applied end state of one output as one sway config line."""

    def test_mode_position_and_refresh(self):
        line = core._sway_persist_line(mk_fresh("Virtual-2", 1280, 1024, x=1920, y=0))
        self.assertEqual(line, "output Virtual-2 mode 1280x1024@60.000Hz position 1920 0")

    def test_transform_and_scale(self):
        line = core._sway_persist_line(mk_fresh("Virtual-1", 1200, 1920, transform="90", scale=2.0))
        self.assertEqual(line, "output Virtual-1 mode 1200x1920@60.000Hz position 0 0 transform 90 scale 2")

    def test_a_disabled_output_is_a_disable_line(self):
        self.assertEqual(core._sway_persist_line(mk_fresh("Virtual-3", 0, 0, active=False)),
                         "output Virtual-3 disable")

    def test_a_custom_mode_keeps_its_custom_keyword(self):
        """A --newmode mode came back nameless from sway and matched the state file's custom Mode; sway only
        takes it back with `mode --custom`, so the persisted line must carry it or sway refuses it at start."""
        o = mk_fresh("Virtual-1", 1600, 900, refresh=60000)
        o.current = core.Mode(w=1600, h=900, refresh_mhz=60000, custom=True)
        self.assertEqual(core._sway_persist_line(o),
                         "output Virtual-1 mode --custom 1600x900@60.000Hz position 0 0")


class TheFile(Base):
    """The file's bytes and the `include` in sway's config."""

    def test_writes_the_header_and_one_line_per_touched_output(self):
        err = self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1280, 1024, x=1920)])
        self.assertEqual(self.read(self.rules), core.SWAY_CONF_HEADER
                         + "output Virtual-2 mode 1280x1024@60.000Hz position 1920 0\n")
        self.assertIn("--persistent: the layout is in %s" % self.rules, err)
        self.assertIn("no `swaymsg reload` is sent", err)

    def test_only_touched_outputs_are_written(self):
        targets = [mk_target("Virtual-1"), mk_target("Virtual-2", changed=False)]
        fresh = [mk_fresh("Virtual-1", 1280, 1024), mk_fresh("Virtual-2", 1280, 720, x=1280)]
        self.persist(targets, fresh)
        self.assertEqual(self.rule_lines(), ["output Virtual-1 mode 1280x1024@60.000Hz position 0 0"])

    def test_a_second_run_merges_rather_than_replaces(self):
        self.persist([mk_target("Virtual-1")], [mk_fresh("Virtual-1", 1280, 1024)])
        self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1280, 720, x=1280)])
        self.assertEqual(self.rule_lines(), [
            "output Virtual-1 mode 1280x1024@60.000Hz position 0 0",
            "output Virtual-2 mode 1280x720@60.000Hz position 1280 0"])

    def test_a_second_run_for_the_same_output_replaces_its_line(self):
        self.persist([mk_target("Virtual-1")], [mk_fresh("Virtual-1", 1280, 1024)])
        self.persist([mk_target("Virtual-1")], [mk_fresh("Virtual-1", 1920, 1080)])
        self.assertEqual(self.rule_lines(), ["output Virtual-1 mode 1920x1080@60.000Hz position 0 0"])


class TheInclude(Base):
    """The `include` line: added once, never twice, and never touching what is already there."""

    def test_include_appended_to_an_existing_config(self):
        original = "# my sway config\nbindsym $mod+Return exec foot\n"
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(original)
        err = self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1280, 1024)])
        text = self.read(self.conf)
        self.assertTrue(text.startswith(original), "the user's config is kept, our line only appended")
        self.assertEqual(text.count("include %s" % self.rules), 1)
        self.assertIn("added an `include %s` line to %s" % (self.rules, self.conf), err)

    def test_include_added_once_never_twice(self):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write("bindsym $mod+Return exec foot\n")
        self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1280, 1024)])
        after_first = self.read(self.conf)
        err = self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1920, 1080)])
        self.assertEqual(self.read(self.conf), after_first, "a config that already includes us is untouched")
        self.assertEqual(self.read(self.conf).count("include "), 1)
        self.assertNotIn("added an `include", err)

    def test_a_config_that_already_includes_us_is_kept_byte_for_byte(self):
        hand_edited = ("set $mod Mod4\n"
                       "include %s\n"
                       "bindsym $mod+q kill\n") % self.rules
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(hand_edited)
        self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1280, 1024)])
        self.assertEqual(self.read(self.conf), hand_edited)

    def test_include_matched_by_basename_not_full_path(self):
        """A user who wrote the include with `~` or a relative path already has us: don't add a second."""
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write("include ~/.config/sway/w11-outputs.conf\n")
        self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1280, 1024)])
        self.assertEqual(self.read(self.conf).count("include "), 1)

    def test_a_missing_config_is_created_including_the_system_config_first(self):
        """No user config anywhere: create $XDG_CONFIG_HOME/sway/config that includes the system config first
        (so the next session keeps every default binding) and our layout file last -- never a near-empty file
        that shadows /etc/sway/config wholesale."""
        self.assertFalse(os.path.exists(self.conf))
        sysconf = os.path.join(self.home, "etc-sway-config")
        with open(sysconf, "w", encoding="utf-8") as fh:
            fh.write("bindsym $mod+Return exec foot\n")
        with mock.patch.object(core, "SWAY_SYSTEM_CONFIG", sysconf):
            err = self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1280, 1024)])
        text = self.read(self.conf)
        self.assertLess(text.index("include %s" % sysconf), text.index("include %s" % self.rules),
                        "the system config is included before our layout, so the defaults load then we win")
        self.assertIn("`include`s %s (the system config" % sysconf, err)

    def test_a_missing_config_with_no_system_config_holds_only_our_include(self):
        """No user config and no system config either: the created file carries only our `include`, and the
        note says so rather than claiming a system config that is not there."""
        missing = os.path.join(self.home, "nope-sway-config")
        with mock.patch.object(core, "SWAY_SYSTEM_CONFIG", missing):
            err = self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1280, 1024)])
        text = self.read(self.conf)
        self.assertIn("include %s" % self.rules, text)
        self.assertNotIn("include %s" % missing, text)
        self.assertIn("no %s either" % missing, err)

    def test_a_legacy_dot_sway_config_gets_the_include(self):
        """sway reads ~/.sway/config before ~/.config/sway/config; the include goes into the file sway opens,
        and nothing is created under ~/.config."""
        legacy_dir = os.path.join(self.home, ".sway")
        os.makedirs(legacy_dir)
        legacy = os.path.join(legacy_dir, "config")
        original = "set $mod Mod4\nbindsym $mod+Return exec foot\n"
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write(original)
        self.assertFalse(os.path.exists(self.conf), "setUp leaves ~/.config/sway/config absent")
        err = self.persist([mk_target("Virtual-2")], [mk_fresh("Virtual-2", 1280, 1024)])
        legacy_text = self.read(legacy)
        self.assertTrue(legacy_text.startswith(original), "the legacy config is kept, our line appended")
        self.assertEqual(legacy_text.count("include %s" % self.rules), 1)
        self.assertFalse(os.path.exists(self.conf), "nothing created under ~/.config when the legacy file wins")
        self.assertIn("added an `include %s` line to %s" % (self.rules, legacy), err)


class ThroughTheBackend(Base):
    """The flag threaded through `SwayBackend.apply`: --persistent writes the file, its absence writes
    nothing.  A `support.FakeSway` answers the two-phase apply; its static GET_OUTPUTS is the post-apply
    snapshot the file is built from."""

    OUTPUTS = [
        {"id": 2, "name": "Virtual-2", "make": "Unknown", "model": "headless", "serial": "Unknown",
         "active": True, "scale": 1.0, "subpixel_hinting": "unknown", "transform": "normal",
         "rect": {"x": 0, "y": 0, "width": 1280, "height": 1024},
         "current_mode": {"width": 1280, "height": 1024, "refresh": 60000},
         "modes": [{"width": 1280, "height": 1024, "refresh": 60000}]},
    ]

    def apply(self, persistent):
        srv = support.FakeSway("ok", outputs=self.OUTPUTS)
        self.addCleanup(srv.close)
        self.srv = srv          # the request log, for the cases that ask what the compositor was sent
        ipc = core.SwayIPC(srv.path)
        backend = core.SwayBackend(ipc)
        self.addCleanup(backend.close)
        state = core.State("test", path=os.path.join(self.home, "state.json"))
        t = core.Target(output=core.OutputState(name="Virtual-2", active=True,
                                                current=core.Mode(w=1280, h=1024, refresh_mhz=60000)),
                        stanza=None, enabled=True, mode=core.Mode(w=1280, h=1024, refresh_mhz=60000),
                        sway_tf="normal", scale=1.0, changed=True)
        err = io.StringIO()
        with redirect_stderr(err):
            backend.apply(state, [t], persistent=persistent)
        return err.getvalue()

    def test_persistent_writes_the_file(self):
        self.apply(persistent=True)
        self.assertEqual(self.rule_lines(), ["output Virtual-2 mode 1280x1024@60.000Hz position 0 0"])

    def test_without_persistent_nothing_is_written(self):
        self.apply(persistent=False)
        self.assertFalse(os.path.exists(self.rules), "no --persistent, no file")
        self.assertFalse(os.path.exists(self.conf), "and no include")


class SomebodyElsesSession(Base):
    """`sudo wxrandr --persistent` against the SEATED user's sway: the live apply lands and the file half
    does not happen.

    Root over ssh and `sudo` are documented ways to drive this tool (docs/Technical.md section 12) and
    `w11common/session.py`'s socket scan finds sway across uids, so the IPC half crosses the boundary and
    works.  `$HOME` does not cross it: it is still the caller's, so the file the old code wrote was
    /root/.config/sway/w11-outputs.conf -- a file that sway, running as uid 1000, has never opened -- and
    the run said the layout was saved.  Now the file half is skipped and the note names the path it would
    have needed, whose uid owns it, and what writing it would take.

    Writing into their `~/.config` as root is the deferred half: a plain write there follows a symlink
    planted in it, which is the hazard `monitors_xml.keep_backup` closes for the one such write we do make.

    Whose session it is is read off the owner of the IPC socket the apply was sent down, so what these cases
    move is that owner and not `session_uid()`; `TheOwnerOfTheSocket` below is the same question without a
    patched stat in the way.
    """

    #: the same double and the same one-output apply as `ThroughTheBackend`; what differs is who owns the
    #: IPC socket the apply is sent down
    OUTPUTS = ThroughTheBackend.OUTPUTS
    apply = ThroughTheBackend.apply

    def setUp(self):
        super().setUp()
        # Whose session it is comes from the owner of the sway IPC socket these phases were sent down
        # (`core.foreign_session_uid`), and a unit test's socket sits in the test's own temp directory, so
        # it is owned by the test and no non-root process can chown it elsewhere.  What moves instead is the
        # stat, which `core._socket_owner` exists to be the single site of.  Base's `session_uid` patch is
        # deliberately left in place saying `os.geteuid()`: every case below is then also the assertion that
        # the socket outranks the seated-session scan, which is the whole of what changed here.
        ownp = mock.patch.object(core, "_socket_owner", return_value=os.geteuid() + 1)
        ownp.start()
        self.addCleanup(ownp.stop)

    def test_the_file_half_is_skipped_with_a_note(self):
        err = self.apply(persistent=True)
        self.assertFalse(os.path.exists(self.rules), "nothing is written into the caller's own home")
        self.assertFalse(os.path.exists(self.conf), "and no config is created there either")
        self.assertIn("belongs to uid %d and this command runs as uid %d" % (os.geteuid() + 1, os.geteuid()),
                      err)
        self.assertIn("/.config/sway/w11-outputs.conf", err)
        self.assertIn("run `wxrandr --persistent` as that user", err)
        self.assertNotIn("AGENTS", err, "the user is owed the route, not our own file names")

    def test_the_live_apply_still_lands(self):
        """The half that DOES cross the boundary is untouched: the same `output` command reaches sway."""
        self.apply(persistent=True)
        sent = [body for _mtype, body in self.srv.requests if body.startswith("output ")]
        self.assertTrue(sent, "the layout was never applied")
        self.assertTrue(any("Virtual-2" in body for body in sent), sent)

    def test_without_persistent_there_is_no_note(self):
        """The flag is what asks for the file; a run that did not ask is not told about one."""
        err = self.apply(persistent=False)
        self.assertNotIn("belongs to uid", err)
        self.assertFalse(os.path.exists(self.rules))

    def test_the_socket_owner_decides_it_and_not_the_seated_scan(self):
        """The test the two branches hang off, both ways round -- and against a `session_uid()` that
        disagrees, because the two are different searches over the same runtime dirs.

        `session.session_uid()` (w11common/session.py:392) is `find_wayland_socket()` (:218), the first
        `wayland-*` in `runtime_dir_candidates()` order; `find_sway_socket()` (:248) walks the very same
        candidates for `sway-ipc.*.sock`.  With uid 1000 seated on GNOME and uid 1001 running a
        headless sway they stop in different directories, so the scan would name 1000's home for a file half
        belonging to 1001's sway -- and the mirror case, our own sway on a box somebody else is seated at,
        would skip a write the caller was entitled to make."""
        self.assertEqual(core.foreign_session_uid("/run/user/1001/sway-ipc.1001.42.sock"), os.geteuid() + 1,
                         "Base's session_uid() says the session is ours; the socket says otherwise and wins")
        with mock.patch.object(core, "_socket_owner", return_value=os.geteuid()):
            with mock.patch.object(core.session, "session_uid", return_value=os.geteuid() + 1):
                self.assertIsNone(core.foreign_session_uid("/run/user/1000/sway-ipc.1000.7.sock"),
                                  "the sway we are driving is our own, so its file half is ours to write")

    def test_the_seated_path_is_named_from_passwd(self):
        """The path in the note comes from the passwd database, not from `$HOME` -- which is exactly the
        variable that is wrong here -- and when there is no passwd entry to ask (a minimal container) it
        degrades to `~<uid>` rather than to a path that would be a lie."""
        with mock.patch("hacks.display.monitors_xml.home_of", return_value="/home/seated"):
            self.assertEqual(core._seated_config_path(4242, "sway", "w11-outputs.conf"),
                             "/home/seated/.config/sway/w11-outputs.conf")
        with mock.patch("hacks.display.monitors_xml.home_of", return_value=None):
            self.assertEqual(core._seated_config_path(4242, "sway", "w11-outputs.conf"),
                             "~4242/.config/sway/w11-outputs.conf")


class TheOwnerOfTheSocket(Base):
    """`foreign_session_uid` against real files: the socket the backend connected to is the session.

    No patched `os.stat` here -- the paths are files this process made, so the uid the code reads is the one
    the kernel reports.  Only the direction that needs a uid we cannot create (somebody ELSE's socket) is
    patched, and that lives in `SomebodyElsesSession` above."""

    #: the same double and the same one-output apply as `ThroughTheBackend`, for the case that asks which
    #: path the backend hands over
    OUTPUTS = ThroughTheBackend.OUTPUTS
    apply = ThroughTheBackend.apply

    def test_a_socket_this_process_owns_is_not_a_foreign_session(self):
        """Even when the seated-session scan points at another account: root running its own sway on a box
        uid 1000 is seated at used to skip a file half it was entitled to write."""
        path = os.path.join(self.home, "sway-ipc.%d.1.sock" % os.geteuid())
        open(path, "wb").close()
        self.assertEqual(core._socket_owner(path), os.geteuid())
        with mock.patch.object(core.session, "session_uid", return_value=os.geteuid() + 1):
            self.assertIsNone(core.foreign_session_uid(path))

    def test_a_socket_that_cannot_be_stat_ed_falls_back_to_the_scan(self):
        """The socket was unlinked between connect and here, or a caller passed nothing: a seated-session
        guess is better than none, and its only cost in the wrong direction is a note nobody needed."""
        gone = os.path.join(self.home, "sway-ipc.gone.sock")
        self.assertIsNone(core._socket_owner(gone))
        self.assertIsNone(core._socket_owner(None))
        with mock.patch.object(core.session, "session_uid", return_value=os.geteuid() + 1):
            self.assertEqual(core.foreign_session_uid(gone), os.geteuid() + 1)
            self.assertEqual(core.foreign_session_uid(), os.geteuid() + 1, "no socket at all: same answer")
        with mock.patch.object(core.session, "session_uid", return_value=None):
            self.assertIsNone(core.foreign_session_uid(gone),
                              "no graphical session found is not a foreign one")

    def test_the_backend_asks_about_the_socket_it_is_driving(self):
        """Not some other one: the argument `SwayBackend.apply` hands over is its own `ipc.sockpath`."""
        seen = []
        with mock.patch.object(core, "_socket_owner", side_effect=lambda p: seen.append(p)) as owner:
            self.apply(persistent=True)
        self.assertTrue(owner.called, "the socket was never consulted")
        self.assertEqual(seen, [self.srv.path])


if __name__ == "__main__":
    unittest.main()
