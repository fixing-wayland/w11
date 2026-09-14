#!/usr/bin/env python3
"""The three shell scripts that run on somebody else's desktop.

`packaging/common/enable-bridge` runs inside the user's first GNOME session
after the package is installed; `gnome/install-bridge.sh` and
`gnome/install-overlap.sh` are what a clone is installed with.  Between them
they are the only code in this project that edits another program's
configuration, and until now not one line of any of them was executed by this
suite: every branch is chosen by what `gsettings`, `gnome-extensions` and
`gdbus` answer, none of which exists in a container, and the ones on a
developer's machine would write into that developer's own dconf.

So they are run for real -- the shipped files, sliced function by function
where a whole run is not possible -- against `support.fake_gnome_bin()`, a
GNOME command line over one state file the test seeds and reads back.  What
that found, and what the tests below pin:

* enable-bridge wrote gdm's dconf when the autostart entry fired in the
  greeter session, enabling the extension for a user who never logs in and
  stamping the real user's turn as done (finding F0.7; the two markers the
  guards read, XDG_SESSION_CLASS=greeter and a GNOME-Greeter component in
  XDG_CURRENT_DESKTOP, are the values gdm sets for its own greeter session --
  they come from that finding and not from a run on the rig, which has
  autologin and so has no greeter session to look at);
* a failing `gsettings set` (read-only dconf) was silent and still left the
  stamp, so the next login skipped the setting too;
* neither enable-bridge nor install-overlap.sh removed the uuid from
  `disabled-extensions`, and gnome-shell lets that list win: an extension in
  both lists never loads.  install-bridge.sh has cleared it since 0.3, so the
  three routes disagreed about what "enabled" means;
* the typelib rename rule in install-overlap.sh's copy_files() -- rewriting a
  typelib in place under a gnome-shell that has mapped it kills the session on
  Wayland (measured on GNOME 51) -- had no test at all.

Everything here runs in a temporary HOME.  The only files outside it any of
these tests read are the shipped scripts themselves.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# (which covers pytest) and tests/test_passthrough.py.  This line is what
# covers `python3 tests/<file>.py`, where conftest is not loaded.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` is a bare import: it resolves only with the tests directory
# on sys.path, which `python3 tests/<file>.py` gives for free and
# `python3 -m unittest tests/<file>.py` does not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                    # noqa: E402

# packaging/common/, not debian/: the enabler and its .desktop are a per-user
# gsettings enable and an XDG autostart entry, which the .deb, the rpm and the
# PKGBUILD need identically -- an Arch recipe reaching into a directory named
# for dpkg is what said they were in the wrong place [recon2/pkg-arch.md 7].
# The bytes did not move, only the directory; ThePackagingsShareTheEnabler at
# the end of this file is what keeps it that way.
ENABLE_BRIDGE = os.path.join(ROOT, "packaging", "common", "enable-bridge")
ENABLE_BRIDGE_DESKTOP = os.path.join(ROOT, "packaging", "common",
                                     "enable-bridge.desktop")
INSTALL_BRIDGE = os.path.join(ROOT, "gnome", "install-bridge.sh")
INSTALL_OVERLAP = os.path.join(ROOT, "gnome", "install-overlap.sh")

BRIDGE_UUID = "w11-bridge@w11"
OVERLAP_UUID = "w11-overlap@w11"
ENABLED = "org.gnome.shell/enabled-extensions"
DISABLED = "org.gnome.shell/disabled-extensions"

#: The real tools the scripts need underneath the fakes (sed, grep, cp, ...).
#: Not os.environ["PATH"]: a developer's PATH may hold a real gsettings, and
#: the whole point of the fakes is that no test can reach one.
REAL_PATH = "/usr/bin:/bin"


class ShellCase(unittest.TestCase):
    """A temporary HOME, a fake GNOME command line, and a log of every call."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="w11-sh-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.p("home")
        self.bin = self.p("bin")
        self.state = self.p("state")
        self.log = self.p("calls.log")
        os.makedirs(self.home)
        support.fake_gnome_bin(self.bin, self.state)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def seed(self, **kw):
        """support.fake_gnome_state() with this case's state file."""
        return support.fake_gnome_state(self.state, **kw)

    def base_env(self, **extra):
        env = {
            "PATH": self.bin + ":" + REAL_PATH,
            "HOME": self.home,
            "FAKE_LOG": self.log,
            "LC_ALL": "C",
        }
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def calls(self, prefix=None):
        """Every command line the fakes were asked to run, in order."""
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
        if prefix is None:
            return lines
        return [ln for ln in lines if ln.startswith(prefix)]

    def run_sh(self, argv, env=None, cwd=None, timeout=60):
        got = subprocess.run(["sh"] + argv, env=env or self.base_env(), cwd=cwd or self.tmp,
                             capture_output=True, text=True, timeout=timeout)
        return got

    def script(self, body, name="case.sh"):
        """Write a runnable script: our prelude plus a slice of a shipped one."""
        path = self.p(name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def enabled(self):
        return support.gnome_list(self.state, ENABLED)

    def disabled(self):
        return support.gnome_list(self.state, DISABLED)


# -- packaging/common/enable-bridge -------------------------------------------

class EnableBridge(ShellCase):
    """Every branch of the script /etc/xdg/autostart runs in the user's first
    session after the .deb goes in."""

    def go(self, env=None, **extra):
        e = self.base_env(
            XDG_CONFIG_HOME=self.p("config"),
            W11_SYSTEM_STAMP=self.p("installed"),
            **extra)
        if env:
            e.update(env)
        return self.run_sh([ENABLE_BRIDGE], env=e)

    def system_stamp(self, when=None):
        path = self.p("installed")
        with open(path, "w"):
            pass
        if when is not None:
            os.utime(path, (when, when))
        return path

    def user_stamp(self, when=None):
        path = self.p("config", "w11", "bridge-enabled")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w"):
            pass
        if when is not None:
            os.utime(path, (when, when))
        return path

    def test_the_script_parses(self):
        got = self.run_sh(["-n", ENABLE_BRIDGE], env={"PATH": REAL_PATH})
        self.assertEqual((got.returncode, got.stderr), (0, ""))

    def test_an_at_as_empty_list_becomes_a_one_item_list(self):
        """`gsettings get` prints `@as []` for an unset key -- the empty-array
        GVariant, not `[]` -- and that is what a desktop where nobody has ever
        touched an extension answers.  Both spellings are in the script's case
        because both happen; this is the one a fresh install takes."""
        self.seed(settings={ENABLED: "@as []"})
        self.system_stamp()
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.enabled(), (BRIDGE_UUID,))
        self.assertEqual(self.calls("gsettings set"),
                         ["gsettings set org.gnome.shell enabled-extensions "
                          "['%s']" % BRIDGE_UUID])

    def test_a_plain_empty_list_becomes_a_one_item_list_too(self):
        self.seed(settings={ENABLED: "[]"})
        self.system_stamp()
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.enabled(), (BRIDGE_UUID,))

    def test_an_existing_list_is_appended_to_and_not_replaced(self):
        """The append is `"${cur%]}, '$UUID']"`, so somebody else's extension
        has to survive it."""
        self.seed(settings={ENABLED: "['a@example.com']"})
        self.system_stamp()
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.enabled(), ("a@example.com", BRIDGE_UUID))

    def test_a_uuid_already_there_is_not_set_again_and_still_stamps(self):
        self.seed(settings={ENABLED: "['%s']" % BRIDGE_UUID})
        self.system_stamp()
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.calls("gsettings set"), [])
        self.assertTrue(os.path.exists(self.p("config", "w11", "bridge-enabled")))

    def test_the_running_shell_is_asked_first_and_the_fallback_is_skipped(self):
        """`gnome-extensions enable` is the supported front end and is tried
        first; it only works for a uuid the running shell has already loaded,
        which after a relogin it has."""
        self.seed(settings={ENABLED: "@as []"}, loaded=[BRIDGE_UUID])
        self.system_stamp()
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.calls("gnome-extensions"),
                         ["gnome-extensions enable %s" % BRIDGE_UUID])
        self.assertEqual(self.calls("gsettings set"), [])
        self.assertEqual(self.enabled(), (BRIDGE_UUID,))

    def test_a_session_without_the_shell_schema_leaves_no_stamp(self):
        """Not a GNOME session: the autostart entry is OnlyShowIn=GNOME, but a
        session that ignores that must not be touched -- and must not be
        stamped either, or the user's next GNOME login skips the setting."""
        self.seed(schemas=("org.gnome.desktop.interface",))
        self.system_stamp()
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertFalse(os.path.exists(self.p("config", "w11", "bridge-enabled")))
        self.assertEqual(self.calls("gsettings set"), [])

    def test_a_failing_gsettings_set_says_so_and_leaves_no_stamp(self):
        """Fix 6, finding F0.3.  A read-only or locked-down dconf makes `set`
        fail; the script ran under `set -eu` with the failure swallowed by
        nothing at all, so it fell through to the stamp and reported success.
        The next login then skipped the whole thing."""
        self.seed(settings={ENABLED: "@as []"})
        self.system_stamp()
        got = self.go(FAKE_GSETTINGS_SET_FAILS="failed to commit changes to dconf")
        self.assertEqual(got.returncode, 1, (got.returncode, got.stdout, got.stderr))
        self.assertIn("enable-bridge: gsettings set failed", got.stderr)
        self.assertFalse(os.path.exists(self.p("config", "w11", "bridge-enabled")))

    def test_a_user_stamp_newer_than_the_installation_stops_the_script_dead(self):
        """"Once per user per installation": after a run the user is in charge
        of the setting, and nothing but a re-install takes that back."""
        self.seed(settings={ENABLED: "@as []"})
        self.system_stamp(when=1000000)
        self.user_stamp(when=2000000)
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_newer_installation_makes_the_setting_again(self):
        """postrm drops the system stamp and postinst writes a fresh one, so a
        system stamp newer than the user's is a different installation -- whose
        removal probably took the setting with it."""
        self.seed(settings={ENABLED: "@as []"})
        self.user_stamp(when=1000000)
        self.system_stamp(when=2000000)
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.enabled(), (BRIDGE_UUID,))

    def test_the_greeter_session_is_left_alone_by_session_class(self):
        """Fix 6, finding F0.7.  gdm runs the greeter as its own user with its
        own dconf, and /etc/xdg/autostart applies there too: the script enabled
        the extension for gdm, where no tool of ours can reach the bridge it
        loads, and stamped gdm's config as done.  logind sets
        XDG_SESSION_CLASS=greeter for that session."""
        self.seed(settings={ENABLED: "@as []"})
        self.system_stamp()
        got = self.go(XDG_SESSION_CLASS="greeter")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.p("config", "w11", "bridge-enabled")))

    def test_the_greeter_session_is_left_alone_by_desktop_component(self):
        """The other marker gdm sets, and the only one on a session where
        XDG_SESSION_CLASS did not survive: XDG_CURRENT_DESKTOP is a
        colon-separated component list, so the match has to be on the whole
        component and not on a substring of `GNOME`."""
        self.seed(settings={ENABLED: "@as []"})
        self.system_stamp()
        got = self.go(XDG_CURRENT_DESKTOP="GNOME-Greeter:GNOME")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.p("config", "w11", "bridge-enabled")))

    def test_an_ordinary_gnome_desktop_component_is_not_the_greeter(self):
        """The premise of the two above: `ubuntu:GNOME`, which is what a real
        Ubuntu session sets, must still be set up."""
        self.seed(settings={ENABLED: "@as []"})
        self.system_stamp()
        got = self.go(XDG_CURRENT_DESKTOP="ubuntu:GNOME")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.enabled(), (BRIDGE_UUID,))

    def test_the_uuid_is_taken_out_of_disabled_extensions(self):
        """Fix 6, finding F6.0.  gnome-shell reads both lists and
        disabled-extensions wins, so an extension in both never loads.  The
        state is reached by disabling the extension once and then
        re-installing the package: the removal drops the system stamp, the
        re-install writes a new one, this script runs again, appends the uuid
        to enabled-extensions -- and used to stop there."""
        self.seed(settings={ENABLED: "@as []", DISABLED: "['%s']" % BRIDGE_UUID})
        self.system_stamp()
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.enabled(), (BRIDGE_UUID,))
        self.assertEqual(self.disabled(), ())

    def test_other_disabled_extensions_survive_that_removal(self):
        """Three sed forms, for the first, middle and only item of the list;
        the neighbours are somebody else's choice."""
        self.seed(settings={ENABLED: "@as []",
                            DISABLED: "['a@x', '%s', 'b@x']" % BRIDGE_UUID})
        self.system_stamp()
        got = self.go()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.disabled(), ("a@x", "b@x"))


# -- the three routes into one setting ----------------------------------------

class EnableFallbacksAgree(ShellCase):
    """install-bridge.sh, install-overlap.sh and the packaged enable-bridge own
    a copy of "put the uuid in enabled-extensions".  From one starting state
    they have to reach one ending state, or "the extension is enabled" means a
    different thing depending on how it was installed."""

    START = {ENABLED: "['other@x']"}

    def run_enable_setting(self, path, uuid):
        """The shipped enable_setting(), with the four helpers it leans on."""
        body = (
            "set -eu\n"
            "UUID='%s'\n"
            "ME=1000\n"
            "TARGET_UID=1000\n"
            "DO_ENABLE=1\n"
            "SYSTEM=0\n"
            "have() { command -v \"$1\" >/dev/null 2>&1; }\n"
            "as_user() { \"$@\"; }\n"
            "setting_has() {\n"
            "    have gsettings || return 1\n"
            "    as_user gsettings get org.gnome.shell \"$1\" 2>/dev/null | grep -q \"'$UUID'\"\n"
            "}\n" % uuid
        ) + support.sh_function(path, "enable_setting") + "enable_setting\n"
        return self.run_sh([self.script(body)],
                           env=self.base_env(FAKE_GNOME_EXTENSIONS_FAILS="1"))

    def test_install_bridge_ends_with_the_uuid_enabled_and_not_disabled(self):
        self.seed(settings=dict(self.START, **{DISABLED: "['%s']" % BRIDGE_UUID}))
        got = self.run_enable_setting(INSTALL_BRIDGE, BRIDGE_UUID)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.enabled(), ("other@x", BRIDGE_UUID))
        self.assertEqual(self.disabled(), ())

    def test_install_overlap_ends_in_the_same_state(self):
        """Fix 7, finding F6.0.  It appended to enabled-extensions and stopped;
        a user who ran `--uninstall` (which puts the uuid in
        disabled-extensions) and then installed again got an extension that
        was in both lists and loaded by neither route."""
        self.seed(settings=dict(self.START, **{DISABLED: "['%s']" % OVERLAP_UUID}))
        got = self.run_enable_setting(INSTALL_OVERLAP, OVERLAP_UUID)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.enabled(), ("other@x", OVERLAP_UUID))
        self.assertEqual(self.disabled(), ())

    def test_the_autostart_script_ends_in_the_same_state(self):
        """The whole of packaging/common/enable-bridge, not a slice: it is the
        route every packaged user takes and the only one with no function to
        lift."""
        self.seed(settings=dict(self.START, **{DISABLED: "['%s']" % BRIDGE_UUID}))
        with open(self.p("installed"), "w"):
            pass
        got = self.run_sh([ENABLE_BRIDGE], env=self.base_env(
            XDG_CONFIG_HOME=self.p("config"),
            W11_SYSTEM_STAMP=self.p("installed"),
            FAKE_GNOME_EXTENSIONS_FAILS="1"))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(self.enabled(), ("other@x", BRIDGE_UUID))
        self.assertEqual(self.disabled(), ())


# -- gnome/install-overlap.sh, run whole --------------------------------------

class InstallOverlapWhole(ShellCase):
    """`sh gnome/install-overlap.sh` from end to end, with a fake session bus.

    The extension it copies is the real one in gnome/; the destination is a
    temporary home, because TARGET_HOME comes from `getent passwd`, which is
    one of the faked commands."""

    def go(self, args=(), **extra):
        env = self.base_env(
            FAKE_USER="tester", FAKE_UID="1000",
            FAKE_PASSWD="tester:x:1000:1000:Tester:%s:/bin/sh" % self.home,
            XDG_RUNTIME_DIR=self.p("run"),
            **extra)
        return self.run_sh([INSTALL_OVERLAP] + list(args), env=env)

    def dest(self, *parts):
        return os.path.join(self.home, ".local/share/gnome-shell/extensions",
                            OVERLAP_UUID, *parts)

    def test_a_measured_gnome_installs_enables_and_asks_for_one_relogin(self):
        """Measured on GNOME 50.1 (resolute-gnome, package route): the files
        land, nothing is said about the shell version, and the script exits 1
        because gnome-shell scans extension directories only at login."""
        self.seed(settings={ENABLED: "@as []"})
        got = self.go(FAKE_SHELL_VERSION="50.1")
        self.assertEqual(got.returncode, 1, (got.stdout, got.stderr))
        self.assertTrue(os.path.exists(self.dest("extension.js")))
        self.assertTrue(os.path.exists(self.dest("typelib", "W11Overlap18-1.0.typelib")))
        with open(self.dest("metadata.json"), encoding="utf-8") as fh:
            packaged = fh.read()
        with open(os.path.join(ROOT, "gnome", OVERLAP_UUID, "metadata.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(packaged, fh.read())
        message = got.stderr[got.stderr.index("install-overlap.sh: log out and back in once."):]
        self.assertEqual(len(message.strip("\n").splitlines()), 6, message)
        self.assertIn("this exit status of 1 says the bus name is not up", message)
        self.assertEqual(self.enabled(), (OVERLAP_UUID,))

    def test_an_unmeasured_gnome_is_named_in_the_installed_metadata_only(self):
        """The extension will not load at all unless metadata.json names the
        running major, and an extension that does not load cannot even report
        what build it is on.  So the installed copy gains the major and the
        repository's copy -- generated from generations.json -- does not."""
        self.seed(settings={ENABLED: "@as []"})
        tree = os.path.join(ROOT, "gnome", OVERLAP_UUID, "metadata.json")
        with open(tree, encoding="utf-8") as fh:
            before = fh.read()
        got = self.go(FAKE_SHELL_VERSION="52.0")
        self.assertEqual(got.returncode, 1, (got.stdout, got.stderr))
        # the list is generations.json's own, in its order: install-overlap.sh
        # builds `$MAJORS` out of that file (install-overlap.sh:332), so a
        # generation added to the table is not a reason for this test to move
        with open(os.path.join(ROOT, "gnome", OVERLAP_UUID, "generations.json"),
                  encoding="utf-8") as fh:
            majors = [str(g["shell_major"]) for g in json.load(fh)["generations"]]
        self.assertNotIn("52", majors, "52 is measured now; pick another major")
        self.assertIn("measured on %s only" % " ".join(majors), got.stderr)
        with open(self.dest("metadata.json"), encoding="utf-8") as fh:
            installed = fh.read()
        self.assertIn('"52"', installed)
        self.assertNotIn('"52"', before)
        with open(tree, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before, "the tree's metadata.json was edited")

    def test_a_bus_name_that_is_already_up_exits_zero(self):
        """The second run, after the relogin: same command, status 0.  The
        README tells a script author to expect exactly this pair."""
        self.seed(settings={ENABLED: "@as []"})
        got = self.go(FAKE_SHELL_VERSION="50.1", FAKE_NAME_OWNED="true")
        self.assertEqual(got.returncode, 0, (got.stdout, got.stderr))
        self.assertIn("org.w11.Overlap is up", got.stdout)

    def test_no_enable_copies_the_files_and_touches_no_setting(self):
        self.seed(settings={ENABLED: "@as []"})
        got = self.go(["--no-enable"], FAKE_SHELL_VERSION="50.1")
        self.assertEqual(got.returncode, 1, (got.stdout, got.stderr))
        self.assertTrue(os.path.exists(self.dest("extension.js")))
        self.assertEqual(self.calls("gsettings set"), [])
        self.assertEqual(self.enabled(), ())

    def test_check_explains_out_of_date_and_reads_only_the_enabled_list(self):
        """State 4 is OUT_OF_DATE, which is what GNOME 51.beta reported for the
        shipped bridge before "51" was added to its metadata (measured on
        stonking-gnome).  `enabled:` is read out of enabled-extensions alone --
        a uuid sitting in disabled-extensions is not enabled, and --check says
        so."""
        self.seed(settings={DISABLED: "['%s']" % OVERLAP_UUID}, loaded=[OVERLAP_UUID])
        got = self.go(["--check"], FAKE_SHELL_VERSION="51.beta", FAKE_EXT_STATE="4",
                      FAKE_EXT_LOADED=OVERLAP_UUID)
        self.assertEqual(got.returncode, 0, (got.stdout, got.stderr))
        self.assertIn("OUT_OF_DATE means gnome-shell will not load it", got.stderr)
        self.assertIn("shell:        51.beta", got.stdout)
        self.assertNotIn("enabled:      yes", got.stdout)
        self.assertIn("enabled:      no", got.stdout)


class TypelibByRename(ShellCase):
    """copy_files() replaces every typelib by rename, never in place.

    gjs mmaps a typelib and keeps the mapping for the life of the process, so
    rewriting the bytes of one that a running gnome-shell has already loaded
    changes the blob under it and the next call through that description aborts
    the process -- which on Wayland is the session.  Measured on GNOME 51,
    re-installing over a session that had used the extension once.  A rename
    leaves the old inode mapped and the running shell keeps working."""

    def setUp(self):
        super().setUp()
        self.src = self.p("src")
        self.dst = self.p("dst")
        shutil.copytree(os.path.join(ROOT, "gnome", OVERLAP_UUID), self.src)
        shutil.copytree(self.src, self.dst)
        # a marker only the *old* copy carries, appended where no reader of a
        # typelib looks: what matters is the bytes an open fd still sees
        for name in sorted(os.listdir(os.path.join(self.dst, "typelib"))):
            with open(os.path.join(self.dst, "typelib", name), "ab") as fh:
                fh.write(b"OLD-INODE")

    def copy_files(self):
        body = ("set -eu\n"
                "SYSTEM=0\nME=1000\nTARGET_UID=1000\nDO_ENABLE=1\n"
                "SRC='%s'\nDEST='%s'\n" % (self.src, self.dst)
                + support.sh_function(INSTALL_OVERLAP, "copy_files")
                + "copy_files\n")
        return self.run_sh([self.script(body)])

    def test_an_open_fd_still_reads_the_bytes_it_was_opened_on(self):
        tls = sorted(os.listdir(os.path.join(self.dst, "typelib")))
        self.assertTrue(tls, "no typelibs shipped: fix the premise, not the test")
        held = {}
        inos = {}
        for name in tls:
            path = os.path.join(self.dst, "typelib", name)
            fh = open(path, "rb")
            self.addCleanup(fh.close)
            held[name] = fh
            inos[name] = os.stat(path).st_ino
        got = self.copy_files()
        self.assertEqual(got.returncode, 0, got.stderr)
        for name, fh in held.items():
            fh.seek(0)
            self.assertTrue(fh.read().endswith(b"OLD-INODE"),
                            "%s was rewritten under an open fd" % name)
            self.assertNotEqual(os.stat(os.path.join(self.dst, "typelib", name)).st_ino,
                                inos[name], name)

    def test_the_new_bytes_are_in_place_and_no_dot_new_file_is_left(self):
        got = self.copy_files()
        self.assertEqual(got.returncode, 0, got.stderr)
        left = [n for n in os.listdir(os.path.join(self.dst, "typelib"))
                if n.startswith(".") or n.endswith(".new")]
        self.assertEqual(left, [])
        for name in sorted(os.listdir(os.path.join(self.src, "typelib"))):
            with open(os.path.join(self.src, "typelib", name), "rb") as a:
                with open(os.path.join(self.dst, "typelib", name), "rb") as b:
                    self.assertEqual(a.read(), b.read(), name)

    def test_the_five_plain_files_are_copied_too(self):
        """The rename rule is only for the typelibs; the JSON and the JS are
        ordinary `cp -f`, and a missing one is an extension that will not
        load."""
        for name in ("metadata.json", "extension.js", "rules.js"):
            os.remove(os.path.join(self.dst, name))
        got = self.copy_files()
        self.assertEqual(got.returncode, 0, got.stderr)
        for name in ("metadata.json", "extension.js", "rules.js",
                     "generations.json", "org.w11.Overlap1.xml"):
            self.assertTrue(os.path.exists(os.path.join(self.dst, name)), name)


class SystemRefusesDpkg(ShellCase):
    """`--system` writes into /usr/share/gnome-shell/extensions, which on the
    machine almost everybody has is dpkg's payload."""

    @unittest.expectedFailure
    def test_installing_over_the_packages_files_is_refused(self):
        """Fix 8 (deferred: the author's call), finding F7.0.  `--system`
        overwrites the w11 package's own files, so dpkg's database and
        the disk disagree from then on and `apt remove w11` takes the
        replacements away.  The check is one `dpkg -S "$DEST/extension.js"`:
        when it names a package, refuse and say `apt remove w11`."""
        self.seed(settings={ENABLED: "@as []"})
        src = self.p("src")
        dst = self.p("sysdest")
        shutil.copytree(os.path.join(ROOT, "gnome", OVERLAP_UUID), src)
        os.makedirs(dst)
        with open(os.path.join(dst, "extension.js"), "w", encoding="utf-8") as fh:
            fh.write("// dpkg put this here\n")
        body = ("set -eu\n"
                "SYSTEM=1\nME=0\nTARGET_UID=0\nDO_ENABLE=1\n"
                "SRC='%s'\nDEST='%s'\nUUID='%s'\n" % (src, dst, OVERLAP_UUID)
                + "have() { command -v \"$1\" >/dev/null 2>&1; }\n"
                + support.sh_function(INSTALL_OVERLAP, "copy_files")
                + "copy_files\n")
        got = self.run_sh([self.script(body)],
                          env=self.base_env(FAKE_DPKG_S="w11"))
        self.assertNotEqual(got.returncode, 0, got.stdout)
        self.assertIn("apt remove w11", got.stdout + got.stderr)
        with open(os.path.join(dst, "extension.js"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "// dpkg put this here\n")

    def test_the_uninstall_message_names_the_package_route_today(self):
        """The premise of the test above, and the half that is already right:
        a per-user `--uninstall` that finds the package's copy in
        $SYSTEM_DIR says so and names `apt remove w11` rather than
        deleting another package's files."""
        with open(INSTALL_OVERLAP, encoding="utf-8") as fh:
            sh = fh.read()
        self.assertIn("this script does not delete another package's payload", sh)
        self.assertIn("sudo apt remove w11", sh)


class BothInstallersPickTheSameUser(ShellCase):
    """The TARGET_USER / RUNTIME_DIR / BUS_ADDR block is copied verbatim into
    both installers.  Two copies of thirteen lines is two chances to drift, and
    every gsettings and gdbus call either script makes goes through it."""

    BLOCK_FIRST = "ME=$(id -u)"
    BLOCK_LAST = "BUS_ADDR=${DBUS_SESSION_BUS_ADDRESS:-unix:path=$RUNTIME_DIR/bus}"

    def block(self, path):
        # sh_block stops at the end of the marker, which is mid-`if`: the
        # closing `fi` is the one line of these thirteen that is not quoted
        # here, and putting it back is not guessing -- an `if` that ended
        # anywhere else would not parse.
        return support.sh_block(path, self.BLOCK_FIRST, self.BLOCK_LAST) + "\nfi\n"

    def answer(self, path, **env):
        body = ("set -eu\n"
                "have() { command -v \"$1\" >/dev/null 2>&1; }\n"
                + self.block(path)
                + 'printf "%s|%s|%s|%s\\n" "$ME" "$TARGET_USER" "$RUNTIME_DIR" "$BUS_ADDR"\n')
        got = self.run_sh([self.script(body, os.path.basename(path) + ".block")],
                          env=self.base_env(
                              FAKE_USER="tester", FAKE_UID="1000",
                              FAKE_PASSWD="tester:x:1000:1000:T:%s:/bin/sh" % self.home,
                              **env))
        self.assertEqual(got.returncode, 0, got.stderr)
        return got.stdout.strip()

    def cases(self):
        return {
            "plain": {},
            "sudo_user": {"SUDO_USER": "tester", "XDG_RUNTIME_DIR": "/run/user/1000"},
            "sudo_root": {"SUDO_USER": "root", "XDG_RUNTIME_DIR": "/run/user/0"},
            "pkexec": {"PKEXEC_UID": "1000"},
            "dbus_address_set": {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/bus"},
        }

    def test_the_two_installers_answer_identically_in_every_case(self):
        for name, env in self.cases().items():
            with self.subTest(name):
                self.assertEqual(self.answer(INSTALL_BRIDGE, **env),
                                 self.answer(INSTALL_OVERLAP, **env))

    def test_a_plain_user_talks_to_their_own_bus(self):
        """Nothing is escalated when nobody asked: ME is not 0, so RUNTIME_DIR
        is $XDG_RUNTIME_DIR and the address is whatever the session set."""
        got = self.answer(INSTALL_BRIDGE, DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/bus")
        self.assertEqual(got, "1000|tester|/run/user/1000|unix:path=/tmp/bus")

    def test_sudo_user_root_is_treated_as_no_sudo_user_at_all(self):
        """`sudo -u root sh install-bridge.sh` sets SUDO_USER=root, and taking
        that as "the desktop user is root" would point every call at
        /run/user/0.  Both scripts test for it by name."""
        got = self.answer(INSTALL_BRIDGE, SUDO_USER="root")
        self.assertTrue(got.startswith("1000|tester|"), got)


# -- gnome/install-bridge.sh --udev -------------------------------------------

class InstallBridgeUdev(ShellCase):
    """`--check`'s udev report, and what `--udev --uninstall` says afterwards.

    The package installs its rule in /usr/lib/udev/rules.d and the by-hand
    route installs one in /etc/udev/rules.d; udev reads both, /etc wins, and
    the report has to name the one that is actually there.  Sliced rather than
    run whole because do_udev() escalates with sudo and reloads real udev."""

    PRELUDE = ("set -eu\n"
               "have() { return 1; }\n"          # no getfacl, no modinfo, no loginctl
               "node_has_acl() { return 1; }\n"
               "seat_user() { :; }\n"
               "can_write_uinput() { return 2; }\n"
               "UDEV_RULE='60-w11-uinput.rules'\n")

    def status(self, etc_rule=False, etc_mod=False, pkg_rule=False, pkg_mod=False):
        etc = self.p("etc")
        usr = self.p("usr")
        for d in (etc, usr):
            os.makedirs(d, exist_ok=True)
        made = {}
        for key, base, name in (("UDEV_DEST", etc, "60-w11-uinput.rules"),
                                ("MODLOAD_DEST", etc, "w11-uinput.conf"),
                                ("UDEV_PKG", usr, "60-w11-uinput.rules"),
                                ("MODLOAD_PKG", usr, "w11-uinput.conf")):
            made[key] = os.path.join(base, name)
        for key, want in (("UDEV_DEST", etc_rule), ("MODLOAD_DEST", etc_mod),
                          ("UDEV_PKG", pkg_rule), ("MODLOAD_PKG", pkg_mod)):
            if want:
                with open(made[key], "w", encoding="utf-8") as fh:
                    fh.write("# rule\n")
        body = (self.PRELUDE
                + "".join("%s='%s'\n" % (k, v) for k, v in sorted(made.items()))
                + support.sh_function(INSTALL_BRIDGE, "udev_status")
                + "udev_status\n")
        got = self.run_sh([self.script(body)], env=self.base_env())
        self.assertEqual(got.returncode, 0, got.stderr)
        out = {}
        for ln in got.stdout.splitlines():
            if ":" in ln:
                key, value = ln.split(":", 1)
                out[key.strip()] = value.strip()
        return out

    def test_only_the_packages_pair_is_reported_as_from_the_package(self):
        """What a .deb install looks like: /usr/lib/udev/rules.d and
        /usr/lib/modules-load.d, nothing in /etc.  Both lines have to say
        where they came from, or `--check` reads as "you have not run --udev"
        on a machine where the rule is already in force."""
        got = self.status(pkg_rule=True, pkg_mod=True)
        self.assertEqual(got["udev rule"],
                         "yes (%s, from the package)" % self.p("usr", "60-w11-uinput.rules"))
        self.assertEqual(got["modules-load"],
                         "yes (%s, from the package)" % self.p("usr", "w11-uinput.conf"))

    def test_the_by_hand_pair_is_reported_without_the_package(self):
        got = self.status(etc_rule=True, etc_mod=True)
        self.assertEqual(got["udev rule"],
                         "yes (%s)" % self.p("etc", "60-w11-uinput.rules"))
        self.assertNotIn("from the package", got["modules-load"])

    def test_etc_wins_when_both_are_there(self):
        """udev reads /usr/lib/udev/rules.d and /etc/udev/rules.d and the
        second shadows the first by file name, so the path reported has to be
        the one whose contents are in force."""
        got = self.status(etc_rule=True, etc_mod=True, pkg_rule=True, pkg_mod=True)
        self.assertEqual(got["udev rule"],
                         "yes (%s)" % self.p("etc", "60-w11-uinput.rules"))

    def test_neither_is_no(self):
        got = self.status()
        self.assertEqual(got["udev rule"], "no (%s)" % self.p("etc", "60-w11-uinput.rules"))
        self.assertEqual(got["modules-load"],
                         "no (%s)" % self.p("etc", "w11-uinput.conf"))

    def test_a_half_installed_pair_is_reported_per_file(self):
        """The rule from the package, the modules-load file from nowhere: two
        independent answers, not one."""
        got = self.status(pkg_rule=True)
        self.assertIn("from the package", got["udev rule"])
        self.assertTrue(got["modules-load"].startswith("no ("), got["modules-load"])

    def uninstall(self, pkg_rule=True, os_release="ID=ubuntu\nID_LIKE=debian\n"):
        """`--udev --uninstall`'s branch, run.

        Sliced like `status()` above -- the whole script escalates with sudo and
        reloads real udev -- but this is the branch itself and not a read of its
        source: the two /etc paths are in the case's tmp dir, $UDEV_PKG points at
        a file that is or is not there, and $OS_RELEASE at a planted os-release.
        udevadm is stubbed present because `have udevadm && ...` is the last
        command of an AND-OR list under `set -e`; the node work
        (forget_uinput_tags, restore_uinput_node) is stubbed because it edits
        /dev/uinput."""
        etc, usr = self.p("etc"), self.p("usr")
        for d in (etc, usr):
            os.makedirs(d, exist_ok=True)
        dest = os.path.join(etc, "60-w11-uinput.rules")
        mod = os.path.join(etc, "w11-uinput.conf")
        pkg = os.path.join(usr, "60-w11-uinput.rules")
        for path in (dest, mod):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# rule\n")
        if pkg_rule:
            with open(pkg, "w", encoding="utf-8") as fh:
                fh.write("# the package's own\n")
        rel = self.p("os-release")
        # every call plants its own, and `os_release=None` means "no such file":
        # the tmp dir is shared by every call in one test, so a leftover from the
        # previous one would answer for the box that has none
        if os.path.exists(rel):
            os.unlink(rel)
        if os_release is not None:
            with open(rel, "w", encoding="utf-8") as fh:
                fh.write(os_release)
        body = ("set -eu\n"
                "have() { return 0; }\n"
                "udevadm() { :; }\n"
                "node_has_acl() { return 1; }\n"
                "forget_uinput_tags() { :; }\n"
                "restore_uinput_node() { :; }\n"
                "ME=0\nMODE=uninstall\n"
                "UDEV_DEST='%s'\nMODLOAD_DEST='%s'\n"
                "UDEV_PKG='%s'\nMODLOAD_PKG='%s'\n"
                "OS_RELEASE='%s'\n" % (dest, mod, pkg,
                                       os.path.join(usr, "w11-uinput.conf"), rel)
                + support.sh_function(INSTALL_BRIDGE, "restored_note")
                + support.sh_function(INSTALL_BRIDGE, "pm_remove_w11")
                + support.sh_function(INSTALL_BRIDGE, "do_udev")
                + "do_udev\n")
        got = self.run_sh([self.script(body)], env=self.base_env())
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertFalse(os.path.exists(dest), "the /etc rule is still there")
        return got.stdout

    def test_the_uninstall_branch_tells_the_truth_about_the_packages_rule(self):
        """Fix 9 / T11, finding F0.6.  `--udev --uninstall` removes
        /etc/udev/rules.d/60-w11-uinput.rules and printed "removed ... ACL
        cleared" and nothing else -- but on a machine that has the package its
        own copy in /usr/lib/udev/rules.d is still there, udev reads that
        directory too, and the ACL is back at the node's next uevent.  The rig
        measured exactly that: after this branch ran, one `udevadm trigger
        --name-match=uinput` put the user's ACL back on five GNOME flavors --
        the gnome golden's check "F0.6: the ACL comes back at the next trigger
        -- --udev --uninstall does not undo the .deb's rule"
        [goal2/recon/gaps.md 1b #10].  So the branch owes the package-manager
        line that finishes the job."""
        text = self.uninstall()
        self.assertIn("apt remove w11", text)
        self.assertIn(self.p("usr", "60-w11-uinput.rules"), text)
        self.assertIn("ACL cleared", text)

    def test_the_remove_line_is_the_distributions_own_word_for_it(self):
        """`apt` on every distribution is the bug w11common/distro.py exists to
        fix: Fedora 44 has no apt and Arch has no apt.

        The nixos case is reached only through the OS_RELEASE seam this helper
        sets -- a real NixOS box has no /usr/lib/udev/rules.d for the guard to
        find, because the module hands the rule to services.udev.packages and
        udev reads it out of the store.  It is pinned so that the one thing the
        arm must never do -- call a NixOS box Debian and tell its owner to run
        apt -- stays impossible if the guard ever widens."""
        self.assertIn("dnf remove w11", self.uninstall(os_release="ID=fedora\n"))
        self.assertIn("pacman -R w11",
                      self.uninstall(os_release='ID=arch\nID_LIKE="archlinux"\n'))
        self.assertIn("programs.w11.uinput.enable = false",
                      self.uninstall(os_release="ID=nixos\n"))
        # a family nobody named, and a box with no os-release at all: Debian's
        # bytes, which is what every message in this project used to print
        self.assertIn("apt remove w11", self.uninstall(os_release="ID=solus\n"))
        self.assertIn("apt remove w11", self.uninstall(os_release=None))

    def test_the_family_walk_is_the_one_distro_py_does(self):
        """`family()` takes ID first and then each word of ID_LIKE in the order
        the file gives them, first known name wins (w11common/distro.py:115).
        Two cases separate that from a substring match over the joined fields,
        and the shell port has to get both right or a Rocky box is told to run
        pacman: Ubuntu's ID is not a family but its ID_LIKE is, and Rocky's
        ID_LIKE names three families at once with the right one first."""
        self.assertIn("apt remove w11",
                      self.uninstall(os_release="ID=ubuntu\nID_LIKE=debian\n"))
        self.assertIn("dnf remove w11",
                      self.uninstall(os_release='ID=rocky\n'
                                                'ID_LIKE="rhel centos fedora"\n'))
        # ID_LIKE naming two families: the first word wins, not the first arm
        self.assertIn("pacman -R w11",
                      self.uninstall(os_release='ID=xx\nID_LIKE="arch fedora"\n'))
        self.assertIn("dnf remove w11",
                      self.uninstall(os_release='ID=xx\nID_LIKE="fedora arch"\n'))

    def test_without_the_packages_copy_there_is_no_remove_line(self):
        """The by-hand route on a box with no package: this branch really did
        undo everything there is to undo, and a line telling that user to
        remove a package they never installed would be an invented problem."""
        text = self.uninstall(pkg_rule=False)
        self.assertIn("ACL cleared", text)
        self.assertNotIn("remove w11", text)
        self.assertNotIn("still owns", text)


# -- the enabler's home ------------------------------------------------------

class ThePackagingsShareTheEnabler(unittest.TestCase):
    """One enabler, three packagings, and nothing under debian/ that names it.

    Design decision 8.  The move is worth a test of its own because it can be
    half-undone in three ways and every one of them is silent: a copy left
    behind under debian/ that the .deb keeps installing while the rpm installs
    the other one; a debian/rules still pointing at the old path (dpkg then
    fails the build, which is loud) or at a copy (which is not); and an rpm or
    PKGBUILD that goes on reaching into debian/.  What ships is one file, and
    tests/test_release_deb.py's PAIRS table compares the .deb's copy with THIS
    path byte for byte."""

    def test_the_enabler_and_its_desktop_are_under_packaging_common(self):
        self.assertTrue(os.path.exists(ENABLE_BRIDGE), ENABLE_BRIDGE)
        self.assertTrue(os.access(ENABLE_BRIDGE, os.X_OK), "it is run, not sourced")
        self.assertTrue(os.path.exists(ENABLE_BRIDGE_DESKTOP), ENABLE_BRIDGE_DESKTOP)

    def test_nothing_under_debian_is_named_enable_bridge(self):
        """A left-behind copy is the failure this names: debian/rules would go
        on installing it, the .deb would ship a file the other two packagings
        do not have, and every test in this file would still pass because they
        all read the new path."""
        left = sorted(n for n in os.listdir(os.path.join(ROOT, "debian"))
                      if n.startswith("enable-bridge"))
        self.assertEqual(left, [])

    def test_debian_rules_installs_from_packaging_common(self):
        with open(os.path.join(ROOT, "debian", "rules"), encoding="utf-8") as fh:
            rules = fh.read()
        self.assertIn("install -D -m 755 packaging/common/enable-bridge", rules)
        self.assertIn("install -D -m 644 packaging/common/enable-bridge.desktop", rules)
        self.assertNotIn("debian/enable-bridge", rules)

    def test_the_autostart_entry_still_points_at_the_dpkg_path(self):
        """The .desktop's Exec= is /usr/lib/w11/enable-bridge, which is
        where the .deb and the PKGBUILD put the helper.  The rpm rewrites the
        line to %{_libexecdir}/w11/enable-bridge at build time, because
        /usr/libexec is Fedora's place for it -- one file, one sed, and no
        second copy of a .desktop to keep in step."""
        with open(ENABLE_BRIDGE_DESKTOP, encoding="utf-8") as fh:
            entry = fh.read()
        self.assertIn("Exec=/usr/lib/w11/enable-bridge", entry)
        self.assertIn("OnlyShowIn=GNOME;", entry)


if __name__ == "__main__":
    unittest.main()
