#!/usr/bin/env python3
"""The shared doubles, tested as themselves.

`tests/support.py` is imported by two dozen files, so a double that lies
there is a whole afternoon of chasing the wrong bug.  Everything added for
the second round of tests is proved here before anything leans on it: the
markdown walker, the shell-script slicer, the fake GNOME command line the
install scripts are run against, the node harness that executes the two
GNOME Shell extensions, the sway IPC double, the session-leader stand-in and
the wl-mirror stub.

Where a helper stands in for a real thing, it is checked against that thing
rather than against itself: the sliced shell blocks are handed to `sh`, the
sway double is driven by wxrandr's own `SwayIPC` client over the real i3-ipc
framing, and the node harness runs the two `extension.js` files that ship --
by their real path, not a copy.
"""

import importlib.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
os.environ["W11_PASSTHROUGH"] = "never"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import support
import wl_fake
from w11common import session
from w11common.wayland_mini import WlConn
from hacks.window import ext_workspace
from hacks.display.core import Fatal, SwayIPC


def _rmtree(path):
    shutil.rmtree(path, ignore_errors=True)


def _resolve_sleep():
    """Where `sleep` really lives, and whether it is a multicall binary.

    On Ubuntu 26.04 /usr/bin/sleep is a symlink into
    /usr/lib/cargo/bin/coreutils/, one binary that dispatches on argv[0]; on a
    GNU-coreutils host it is a program of its own.  Which one this is decides
    whether the test below has anything to assert."""
    path = shutil.which("sleep") or "/bin/sleep"
    real = os.path.realpath(path)
    return real, "coreutils" in real


_SLEEP_REAL, _SLEEP_IS_MULTICALL = _resolve_sleep()


class Documents(unittest.TestCase):
    """`documents()` -- the markdown walker scripts/check-docs.py,
    test_release_deb.py and the anchor checker each carried a copy of."""

    def test_it_finds_the_documents_by_their_path_relative_to_the_root(self):
        docs = support.documents()
        self.assertIn("README.md", docs)
        self.assertIn(os.path.join("docs", "Technical.md"), docs)
        self.assertIn("# w11", docs["README.md"])

    def test_it_agrees_with_the_walker_check_docs_carries(self):
        """The one that has to keep working: scripts/check-docs.py reports 0
        disagreements over exactly this set, and a walker that saw a different
        set would move that number without anybody editing a document."""
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        try:
            spec = importlib.util.spec_from_file_location(
                "check_docs_for_test", os.path.join(ROOT, "scripts", "check-docs.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.pop(0)
        self.assertEqual(set(mod.documents()), set(support.documents()))

    def test_it_walks_past_git_and_pycache(self):
        with tempfile.TemporaryDirectory() as tmp:
            for sub in (".git", "__pycache__", "node_modules", "docs"):
                os.makedirs(os.path.join(tmp, sub))
                with open(os.path.join(tmp, sub, "x.md"), "w") as fh:
                    fh.write("# x\n")
            self.assertEqual(set(support.documents(tmp)), {os.path.join("docs", "x.md")})

    def test_bytes_no_encoding_claims_do_not_stop_the_walk(self):
        """errors='replace' is not decoration: the walk covers media/ and
        vm/, where a note may hold anything."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "bad.md"), "wb") as fh:
                fh.write(b"# t\n\xff\xfe not utf-8\n")
            self.assertIn("not utf-8", support.documents(tmp)["bad.md"])


class ShellSlices(unittest.TestCase):
    """`sh_function()` and `sh_block()` -- the installers' branches are only
    reachable inside a GNOME session, so the block itself is run instead."""

    OVERLAP = os.path.join(ROOT, "gnome", "install-overlap.sh")
    BRIDGE = os.path.join(ROOT, "gnome", "install-bridge.sh")

    def test_a_function_comes_out_whole_and_sh_accepts_it(self):
        src = support.sh_function(self.OVERLAP, "enable_setting")
        self.assertTrue(src.startswith("enable_setting() {"), src[:40])
        self.assertTrue(src.endswith("}\n"), src[-40:])
        self.assertIn("enabled-extensions", src)
        r = subprocess.run(["sh", "-n"], input=src, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_function_both_installers_define_can_be_sliced(self):
        """Not one name written down here: whatever the scripts define is what
        gets sliced, so a rename cannot quietly stop being covered."""
        for path in (self.OVERLAP, self.BRIDGE):
            with open(path, encoding="utf-8") as fh:
                names = re.findall(r"^(\w+)\(\) \{$", fh.read(), re.M)
            self.assertGreater(len(names), 5, path)
            for name in names:
                src = support.sh_function(path, name)
                self.assertTrue(src.startswith("%s() {" % name), (path, name))
                r = subprocess.run(["sh", "-n"], input=src, capture_output=True,
                                   text=True, timeout=30)
                self.assertEqual(r.returncode, 0, (path, name, r.stderr))

    def test_a_function_that_is_not_there_says_so(self):
        with self.assertRaises(AssertionError) as cm:
            support.sh_function(self.OVERLAP, "no_such_function")
        self.assertIn("no function no_such_function()", str(cm.exception))

    def test_a_block_is_inclusive_of_both_markers(self):
        block = support.sh_block(self.OVERLAP, "FOUND=\n", 'FOUND=$SYSTEM_DIR/$UUID\nfi')
        self.assertTrue(block.startswith("FOUND=\n"))
        self.assertTrue(block.endswith("FOUND=$SYSTEM_DIR/$UUID\nfi"))
        self.assertIn("$DEST", block)

    def test_a_marker_that_no_longer_matches_is_a_failure_not_an_empty_slice(self):
        """A slice that silently came back empty would be a test that passes
        over a script it never read."""
        with self.assertRaises(ValueError):
            support.sh_block(self.OVERLAP, "FOUND=\n", "this text is not in the script")


class FakeGnomeCommandLine(unittest.TestCase):
    """`fake_gnome_bin()` -- gsettings, gnome-extensions, gdbus and the rest,
    over one state file.  Every branch of debian/enable-bridge is chosen by
    what these answer, and the real ones would write into the runner's dconf."""

    UUID = "w11-bridge@w11"
    KEY = "org.gnome.shell/enabled-extensions"
    DISABLED = "org.gnome.shell/disabled-extensions"
    OVERLAP = os.path.join(ROOT, "gnome", "install-overlap.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fake-gnome-")
        self.addCleanup(_rmtree, self.tmp)
        self.state = os.path.join(self.tmp, "state")
        self.log = os.path.join(self.tmp, "log")
        self.bin = support.fake_gnome_bin(os.path.join(self.tmp, "bin"), self.state)

    def run_tool(self, *argv, **env):
        e = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                 FAKE_LOG=self.log)
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run(list(argv), capture_output=True, text=True,
                              env=e, timeout=30)

    def logged(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln.rstrip("\n") for ln in fh]

    def test_list_schemas_is_what_decides_this_is_a_gnome_session(self):
        """`gsettings list-schemas | grep -qx org.gnome.shell` is the line in
        debian/enable-bridge that makes it a no-op on Plasma or sway."""
        support.fake_gnome_state(self.state, schemas=("org.gnome.shell",
                                                      "org.gnome.desktop.interface"))
        r = self.run_tool("gsettings", "list-schemas")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(r.stdout.split()),
                         ["org.gnome.desktop.interface", "org.gnome.shell"])
        support.fake_gnome_state(self.state, schemas=())
        self.assertEqual(self.run_tool("gsettings", "list-schemas").stdout, "")

    def test_get_returns_the_variant_text_verbatim(self):
        """`@as []` and `[]` are different strings to these scripts -- an empty
        list reads back as `@as []` from a real gsettings, and the case arm in
        enable-bridge names both -- so the double must not normalise either."""
        for value in ("@as []", "[]", "['a@x']"):
            support.fake_gnome_state(self.state, settings={self.KEY: value})
            r = self.run_tool("gsettings", "get", "org.gnome.shell", "enabled-extensions")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), value)

    def test_an_unknown_schema_or_key_fails_the_way_gsettings_does(self):
        support.fake_gnome_state(self.state, schemas=("org.gnome.shell",))
        r = self.run_tool("gsettings", "get", "org.example.nope", "k")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No such schema", r.stderr)
        r = self.run_tool("gsettings", "get", "org.gnome.shell", "never-set")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No such key", r.stderr)

    def test_set_is_read_back_and_logged(self):
        support.fake_gnome_state(self.state, settings={self.KEY: "@as []"})
        r = self.run_tool("gsettings", "set", "org.gnome.shell",
                          "enabled-extensions", "['%s']" % self.UUID)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(support.gnome_list(self.state, self.KEY), (self.UUID,))
        self.assertIn("gsettings set org.gnome.shell enabled-extensions ['%s']" % self.UUID,
                      self.logged())

    def test_set_can_be_made_to_fail(self):
        """A dconf that will not commit is the branch enable-bridge is silent
        about today; a test needs it to be reachable."""
        support.fake_gnome_state(self.state, settings={self.KEY: "@as []"})
        r = self.run_tool("gsettings", "set", "org.gnome.shell", "enabled-extensions",
                          "['x']", FAKE_GSETTINGS_SET_FAILS="failed to commit changes")
        self.assertEqual(r.returncode, 1)
        self.assertIn("failed to commit changes", r.stderr)
        self.assertEqual(support.gnome_setting(self.state, self.KEY), "@as []")

    def test_an_unseeded_key_reads_back_the_schema_default(self):
        """A fresh desktop has never written enabled-extensions, and gsettings
        answers the schema default for it -- `@as []`, exit 0 -- not `No such
        key`.  It matters because every caller reads with `|| echo '[]'`
        (install-bridge.sh:208 for disabled-extensions): a double that failed
        here steered every unseeded run into the `[]` arm of enable-bridge's
        case and the `@as []` arm a real GNOME produces was never taken.
        Defaults read out of org.gnome.shell.gschema.xml in gnome-shell-common
        50.1-0ubuntu1.2."""
        support.fake_gnome_state(self.state, settings={})
        for key, default in (("enabled-extensions", "@as []"),
                             ("disabled-extensions", "@as []"),
                             ("disable-user-extensions", "false")):
            r = self.run_tool("gsettings", "get", "org.gnome.shell", key)
            self.assertEqual(r.returncode, 0, (key, r.stderr))
            self.assertEqual(r.stdout.strip(), default, key)

    def test_a_key_the_schema_does_not_have_is_still_an_error(self):
        """The default is per key, not a blanket "anything reads empty": a
        misspelled key has to fail the way gsettings fails, or a test would
        pass over a typo in the script it is checking."""
        support.fake_gnome_state(self.state, settings={})
        r = self.run_tool("gsettings", "get", "org.gnome.shell", "enabled-extension")
        self.assertEqual(r.returncode, 1)
        self.assertIn('No such key "enabled-extension"', r.stderr)
        self.assertEqual(r.stdout, "")

    def test_gnome_extensions_refuses_a_uuid_the_shell_has_not_seen(self):
        """The refusal that sends every caller down its gsettings fallback: on
        a fresh install, before the relogin, the running shell has never
        scanned the directory the package wrote into."""
        support.fake_gnome_state(self.state, settings={self.KEY: "@as []"})
        r = self.run_tool("gnome-extensions", "enable", self.UUID)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr.strip(), 'Extension "%s" does not exist' % self.UUID)
        self.assertEqual(support.gnome_list(self.state, self.KEY), ())

    def test_enabling_a_loaded_uuid_moves_it_between_the_two_lists(self):
        """What the real front end does: EnableExtension adds to
        enabled-extensions AND removes from disabled-extensions.  A uuid left
        in the second list is an extension that never loads."""
        support.fake_gnome_state(self.state, loaded=(self.UUID,),
                                 settings={self.KEY: "['other@x']",
                                           self.DISABLED: "['%s']" % self.UUID})
        r = self.run_tool("gnome-extensions", "enable", self.UUID)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(support.gnome_list(self.state, self.KEY),
                         ("other@x", self.UUID))
        self.assertEqual(support.gnome_list(self.state, self.DISABLED), ())
        r = self.run_tool("gnome-extensions", "disable", self.UUID)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(support.gnome_list(self.state, self.KEY), ("other@x",))
        self.assertEqual(support.gnome_list(self.state, self.DISABLED), (self.UUID,))

    def test_an_emptied_list_renders_as_gsettings_renders_it(self):
        support.fake_gnome_state(self.state, loaded=(self.UUID,),
                                 settings={self.KEY: "['%s']" % self.UUID})
        self.run_tool("gnome-extensions", "disable", self.UUID)
        self.assertEqual(support.gnome_setting(self.state, self.KEY), "@as []")

    #: install-overlap.sh's own gdbus parsers, and the three helpers they are
    #: built on.  Sliced rather than re-typed: a test that carried its own copy
    #: of the script's sed would keep passing while the script changed.
    PARSERS = ("have", "as_user", "gd", "shell_version", "name_owned",
               "ext_loaded", "ext_state")

    def run_parsers(self, body, **env):
        """Run `body` after install-overlap.sh's own gdbus functions, with the
        fake command line on PATH and the non-root arm of as_user taken."""
        src = [support.sh_block(self.OVERLAP, "UUID='w11-overlap@w11'",
                                "EXT_IFACE='org.gnome.Shell.Extensions'"),
               "ME=%d\nTARGET_UID=%d\nTARGET_USER=nobody\nBUS_ADDR=\nRUNTIME_DIR=\n"
               % (os.getuid(), os.getuid())]
        src += [support.sh_function(self.OVERLAP, name) for name in self.PARSERS]
        src.append(body)
        e = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                 FAKE_LOG=self.log)
        e.update({k: str(v) for k, v in env.items()})
        return subprocess.run(["sh", "-s"], input="\n".join(src), capture_output=True,
                              text=True, env=e, timeout=60)

    def test_gdbus_answers_what_the_installers_own_parsers_read(self):
        """The installers parse gdbus with sed, so the shape matters as much
        as the value -- `(<'51.beta'>,)` for the ShellVersion property,
        `(true,)` for NameHasOwner, `'state': <4>` for GetExtensionInfo.  The
        parsers here are gnome/install-overlap.sh:146, :153, :159 and :164
        themselves, sliced out and run over the fake, so the fake is checked
        against the script rather than against a copy of its sed."""
        support.fake_gnome_state(self.state)
        r = self.run_parsers("shell_version", FAKE_SHELL_VERSION="51.beta")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "51.beta")
        r = self.run_parsers("name_owned && echo owned || echo free")
        self.assertEqual(r.stdout.strip(), "free")
        r = self.run_parsers("name_owned && echo owned || echo free",
                             FAKE_NAME_OWNED="true")
        self.assertEqual(r.stdout.strip(), "owned")
        r = self.run_parsers("ext_state", FAKE_EXT_STATE="4", FAKE_EXT_LOADED=self.UUID)
        self.assertEqual(r.stdout.strip(), "4")
        r = self.run_parsers("ext_loaded && echo yes || echo no",
                             FAKE_EXT_LOADED="w11-overlap@w11")
        self.assertEqual(r.stdout.strip(), "yes")
        r = self.run_parsers("ext_loaded && echo yes || echo no",
                             FAKE_EXT_LOADED="somebody-else@x")
        self.assertEqual(r.stdout.strip(), "no")

    def test_the_installers_own_parsers_read_nothing_when_no_shell_answers(self):
        """No shell on the bus is gdbus exiting 1 with nothing on stdout, and
        both parsers then print nothing at all: install-overlap.sh's `-` arm
        belongs to a host with no gdbus, not to a host whose gdbus fails, so a
        double that printed an error there would send --check down the wrong
        branch."""
        support.fake_gnome_state(self.state)
        self.assertEqual(self.run_parsers("ext_state").stdout, "")
        self.assertEqual(self.run_parsers("shell_version").stdout, "")
        r = self.run_parsers("v=$(shell_version); echo \"[$v]\"")
        self.assertEqual(r.stdout.strip(), "[]")

    def test_no_shell_on_the_bus_is_gdbus_exiting_one(self):
        support.fake_gnome_state(self.state)
        r = self.run_tool("gdbus", "call", "--method",
                          "org.freedesktop.DBus.Properties.Get", "ShellVersion")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")

    def test_dpkg_S_answers_who_owns_a_path(self):
        """`--system` refuses to write over dpkg's payload; the test of that
        needs a dpkg that says the package owns the file, and one that does
        not know it."""
        support.fake_gnome_state(self.state)
        r = self.run_tool("dpkg", "-S", "/usr/share/gnome-shell/extensions/x",
                          FAKE_DPKG_S="w11")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(),
                         "w11: /usr/share/gnome-shell/extensions/x")
        r = self.run_tool("dpkg", "-S", "/tmp/x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no path found", r.stderr)

    def test_sudo_and_runuser_run_the_command_here(self):
        """The TARGET_USER/RUNTIME_DIR/BUS_ADDR block is what is under test in
        those scripts, not privilege: `sudo -u u env A=B cmd` has to reach cmd
        with A=B and no password prompt."""
        support.fake_gnome_state(self.state)
        r = self.run_tool("sudo", "-u", "someone", "env", "FOO=bar", "sh", "-c",
                          'printf %s "$FOO"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "bar")
        r = self.run_tool("runuser", "-u", "someone", "--", "sh", "-c", "echo hi")
        self.assertEqual(r.stdout.strip(), "hi")
        self.assertTrue([ln for ln in self.logged() if ln.startswith("sudo -u someone")])

    def test_id_and_getent_answer_for_a_user_that_does_not_exist_here(self):
        support.fake_gnome_state(self.state)
        self.assertEqual(self.run_tool("id", "-u", "root").stdout.strip(), "0")
        self.assertEqual(self.run_tool("id", "-u", "someone", FAKE_UID=1234).stdout.strip(),
                         "1234")
        self.assertEqual(self.run_tool("id", "-un", "someone",
                                       FAKE_USER="tester").stdout.strip(), "tester")
        r = self.run_tool("id", "-u", "ghost", FAKE_ID_UNKNOWN="ghost")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no such user", r.stderr)
        r = self.run_tool("getent", "passwd", "someone",
                          FAKE_PASSWD="someone:x:1234:1234::/home/someone:/bin/sh")
        self.assertEqual(r.stdout.strip(),
                         "someone:x:1234:1234::/home/someone:/bin/sh")
        self.assertEqual(self.run_tool("getent", "passwd", "ghost").returncode, 2)

    def test_every_call_is_logged_so_no_call_at_all_can_be_asserted(self):
        support.fake_gnome_state(self.state, settings={self.KEY: "@as []"})
        self.assertEqual(self.logged(), [])
        self.run_tool("gsettings", "get", "org.gnome.shell", "enabled-extensions")
        self.assertEqual(self.logged(),
                         ["gsettings get org.gnome.shell enabled-extensions"])

    def test_the_tools_are_shell_scripts_sh_itself_accepts(self):
        for name in support.FAKE_GNOME_TOOLS:
            path = os.path.join(self.bin, name)
            self.assertTrue(os.access(path, os.X_OK), path)
            r = subprocess.run(["sh", "-n", path], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, (name, r.stderr))

    def test_a_subset_leaves_the_real_tools_alone(self):
        d = support.fake_gnome_bin(os.path.join(self.tmp, "few"), self.state,
                                   ["gsettings"])
        self.assertEqual(sorted(n for n in os.listdir(d) if not n.startswith("_")),
                         ["gsettings"])


@support.skip_without_node
class NodeHarness(unittest.TestCase):
    """`js_harness()` -- the loader that lets node run the two extensions.

    Neither extension.js had ever been executed by anything but a GNOME
    session: they import `gi://Meta` and
    `resource:///org/gnome/shell/ui/main.js`, which node refuses before
    reading a line.  Proven on node v22.22.1 on this host."""

    def test_a_case_gets_its_argument_and_its_answer_comes_back_parsed(self):
        got = support.js_harness(support.BRIDGE_EXT, """
            const input = JSON.parse(process.argv[2]);
            console.log(JSON.stringify({doubled: input.n * 2, kind: typeof input}));
        """, {"n": 21})
        self.assertEqual(got, {"doubled": 42, "kind": "object"})

    def test_the_four_path_tokens_are_substituted(self):
        got = support.js_harness(support.OVERLAP_EXT, """
            console.log(JSON.stringify({module: '@MODULE@', stubs: '@STUBS@',
                                        gjs: '@GJS@', root: '@ROOT@'}));
        """)
        self.assertEqual(got, {"module": support.OVERLAP_EXT, "stubs": support.GJS_STUBS,
                               "gjs": support.GJS_DIR, "root": ROOT})

    def test_the_shipped_bridge_evaluates_and_constructs(self):
        """The shipped file, at its own path -- not a copy with the imports
        cut out, which is what the older static tests had to do."""
        got = support.js_harness(support.BRIDGE_EXT, """
            import Ext from '@MODULE@/extension.js';
            const e = new Ext({uuid: 'w11-bridge@w11'});
            console.log(JSON.stringify({
                name: Ext.name,
                methods: Object.getOwnPropertyNames(Object.getPrototypeOf(e)),
            }));
        """)
        self.assertEqual(got["name"], "W11Bridge")
        for m in ("enable", "disable", "_setState", "_windowInfo", "_allWindows"):
            self.assertIn(m, got["methods"])

    def test_the_shipped_overlap_evaluates_and_keeps_the_path_it_was_given(self):
        """`this.path` is where generations.json and typelib/ are read from, so
        an extension constructed with the real directory reads the real table."""
        got = support.js_harness(support.OVERLAP_EXT, """
            import Ext from '@MODULE@/extension.js';
            const e = new Ext({uuid: 'w11-overlap@w11', path: '@MODULE@'});
            console.log(JSON.stringify({name: Ext.name, path: e.path,
                methods: Object.getOwnPropertyNames(Object.getPrototypeOf(e))}));
        """)
        self.assertEqual(got["name"], "W11Overlap")
        self.assertEqual(got["path"], support.OVERLAP_EXT)
        for m in ("Probe", "ApplyOverlap", "Version", "_apply"):
            self.assertIn(m, got["methods"])

    def test_a_case_that_throws_carries_the_stack_into_the_failure(self):
        """A JS exception with no stack is an afternoon; the harness must not
        swallow node's stderr."""
        with self.assertRaises(AssertionError) as cm:
            support.js_harness(support.BRIDGE_EXT,
                               "throw new Error('deliberate: the case broke');")
        self.assertIn("deliberate: the case broke", str(cm.exception))
        self.assertIn("node exited", str(cm.exception))

    def test_a_case_that_prints_nothing_says_so_rather_than_raising_valueerror(self):
        with self.assertRaises(AssertionError) as cm:
            support.js_harness(support.BRIDGE_EXT, "console.error('not json');")
        self.assertIn("printed no JSON", str(cm.exception))
        self.assertIn("not json", str(cm.exception))


@support.skip_without_node
class GjsStubs(unittest.TestCase):
    """tests/fixtures/gjs/stubs -- recording doubles, not a swallowing Proxy.

    Each one is configured through the same specifier the extension imports,
    so `import Meta from 'gi://Meta'` in a case is the object the extension
    got, and what it asked for is readable afterwards."""

    def case(self, body, arg=None, module=None):
        return support.js_harness(module or support.BRIDGE_EXT, body, arg)

    def test_a_window_double_answers_a_listwindows_row_back(self):
        """The row is the bridge's own JSON shape (test_backend_gnome
        .fixture_windows()): the xterm, XWayland, focused, at 100,80 640x480."""
        got = self.case("""
            import Meta from 'gi://Meta';
            const row = JSON.parse(process.argv[2]);
            const w = Meta.makeWindow(row);
            console.log(JSON.stringify({
                title: w.get_title(), cls: w.get_wm_class(), pid: w.get_pid(),
                type: w.get_window_type(), rect: w.get_frame_rect(),
                client: w.get_client_type(), desc: w.get_description(),
                focus: w.has_focus(), ws: w.get_workspace().index(),
            }));
        """, {"id": 1, "title": "test@vm: ~", "wm_class": "XTerm", "pid": 1201,
              "window_type": "NORMAL", "x": 100, "y": 80, "width": 640, "height": 480,
              "client_type": "x11", "xid": 0x400005, "focused": True, "workspace": 0,
              "maximized_h": False, "maximized_v": False})
        self.assertEqual(got["title"], "test@vm: ~")
        self.assertEqual(got["cls"], "XTerm")
        self.assertEqual(got["pid"], 1201)
        self.assertEqual(got["type"], 0)          # Meta.WindowType.NORMAL
        self.assertEqual(got["rect"], {"x": 100, "y": 80, "width": 640, "height": 480})
        self.assertEqual(got["client"], 1)        # WindowClientType.X11
        self.assertTrue(got["desc"].startswith("0x400005 "))
        self.assertTrue(got["focus"])
        self.assertEqual(got["ws"], 0)

    def test_a_row_with_no_workspace_at_all_is_workspace_zero(self):
        """`fixture_windows()` rows carry `workspace`, but the hostile and
        minimal rows a case writes by hand do not, and a Workspace double whose
        index() answered `undefined` came back out of the bridge as JSON null
        where MockBridge answers a number.  Only a negative workspace is "no
        workspace" -- what a dock, or a window being unmanaged, has."""
        got = self.case("""
            import Meta from 'gi://Meta';
            const base = {id: 7, title: 't', wm_class: 'c', window_type: 'NORMAL',
                          x: 0, y: 0, width: 10, height: 10, client_type: 'wayland'};
            const none = Meta.makeWindow(base).get_workspace();
            const neg = Meta.makeWindow({...base, workspace: -1}).get_workspace();
            const two = Meta.makeWindow({...base, workspace: 2}).get_workspace();
            console.log(JSON.stringify({
                none: none === null ? null : none.index(),
                neg: neg === null ? null : neg.index(),
                two: two === null ? null : two.index(),
            }));
        """)
        self.assertEqual(got, {"none": 0, "neg": None, "two": 2})

    def test_the_enum_numbers_are_the_ones_the_typelibs_carry(self):
        """Both extensions read these symbolically today, so no product code
        notices a wrong number -- but a case that asserts what Main.pushModal()
        was handed, or that builds an event by type number, compares against
        them, and a guessed number would make that case agree with nothing.
        Read out of the shipped typelibs here on 2026-09-08: Shell-14.typelib
        (gnome-shell 46.0-0ubuntu5, noble) and Shell-18.typelib (50.1-0ubuntu1.2,
        resolute) both put ActionMode.POPUP at 128 and SYSTEM_MODAL at 32;
        Clutter-14/-18 (gir1.2-mutter-14 46.0-1ubuntu9, gir1.2-mutter-18
        50.1-0ubuntu2.2) put BUTTON_PRESS at 6 and BUTTON_RELEASE at 7, because
        ENTER and LEAVE sit between MOTION and the buttons; Meta-14 has
        Cursor.CROSSHAIR 17 (Meta-18 has no Cursor enum at all -- mutter 18
        moved those names to Clutter.CursorType, where CROSSHAIR is 9); and
        this host's own GLib answers FileTest.EXISTS 16, IS_REGULAR 1."""
        got = self.case("""
            import Shell from 'gi://Shell';
            import Clutter from 'gi://Clutter';
            import Meta from 'gi://Meta';
            import GLib from 'gi://GLib';
            console.log(JSON.stringify({
                popup: Shell.ActionMode.POPUP,
                system_modal: Shell.ActionMode.SYSTEM_MODAL,
                normal: Shell.ActionMode.NORMAL,
                press: Clutter.EventType.BUTTON_PRESS,
                release: Clutter.EventType.BUTTON_RELEASE,
                touch_begin: Clutter.EventType.TOUCH_BEGIN,
                pad_press: Clutter.EventType.PAD_BUTTON_PRESS,
                crosshair: Meta.Cursor.CROSSHAIR,
                cursor_default: Meta.Cursor.DEFAULT,
                css_crosshair: Clutter.CursorType.CROSSHAIR,
                exists: GLib.FileTest.EXISTS,
                is_regular: GLib.FileTest.IS_REGULAR,
                sha256: GLib.ChecksumType.SHA256,
            }));
        """)
        self.assertEqual(got, {"popup": 128, "system_modal": 32, "normal": 1,
                               "press": 6, "release": 7, "touch_begin": 9,
                               "pad_press": 18, "crosshair": 17, "cursor_default": 1,
                               "css_crosshair": 9, "exists": 16, "is_regular": 1,
                               "sha256": 2})

    def test_the_window_shapes_are_the_three_mutter_api_generations(self):
        """46-48 take maximize(flags); 49+ moved the directions to
        set_maximize_flags() and maximize() there takes no argument at all;
        a window with neither boolean property still answers through
        get_maximize_flags().  Which one is in front of the bridge is the
        compositor-version axis, and it is `shape` here."""
        got = self.case("""
            import Meta from 'gi://Meta';
            const row = {id: 5, title: 't', wm_class: 'c', window_type: 'NORMAL',
                         x: 0, y: 0, width: 10, height: 10, client_type: 'wayland',
                         maximized_h: false, maximized_v: false};
            const out = {};
            for (const shape of ['46', '49', 'flags', 'hostile']) {
                const w = Meta.makeWindow(row, {shape});
                out[shape] = {
                    props: 'maximized_horizontally' in w,
                    setters: typeof w.set_maximize_flags === 'function',
                    flags: typeof w.get_maximize_flags === 'function',
                    throws: (() => { try { w.get_title(); return false; }
                                     catch (e) { return true; } })(),
                };
            }
            console.log(JSON.stringify(out));
        """)
        self.assertEqual(got["46"], {"props": True, "setters": False, "flags": False,
                                     "throws": False})
        self.assertEqual(got["49"], {"props": True, "setters": True, "flags": True,
                                     "throws": False})
        self.assertEqual(got["flags"], {"props": False, "setters": False, "flags": True,
                                        "throws": False})
        self.assertTrue(got["hostile"]["throws"])

    def test_the_window_double_records_every_call_in_order(self):
        got = self.case("""
            import Meta from 'gi://Meta';
            import * as H from '@STUBS@/harness.mjs';
            const w = Meta.makeWindow({id: 9, window_type: 'NORMAL', x: 0, y: 0,
                width: 1, height: 1, client_type: 'wayland',
                maximized_h: false, maximized_v: true});
            w.get_title();
            w.maximize(1);
            const before = w.minimized;
            console.log(JSON.stringify({names: H.names(),
                                        state: [w.state.h, w.state.v], before}));
        """)
        self.assertEqual(got["names"], ["w9.get_title", "w9.maximize", "w9.get:minimized"])
        self.assertEqual(got["state"], [True, True])
        self.assertFalse(got["before"])

    def test_gobject_type_query_answers_the_size_a_case_registers(self):
        """72 bytes for MetaMonitorsConfig is what GNOME 46.0's registry
        reports on noble-gnome (libmutter-14, build 9e23feb34618); 80 on 50
        and 51.  An unregistered type is a type_from_name of 0, which is the
        `MetaMonitorsConfig is not a registered GType` refusal."""
        got = self.case("""
            import GObject from 'gi://GObject';
            GObject.setType('MetaMonitorsConfig', 72);
            const t = GObject.type_from_name('MetaMonitorsConfig');
            const n = GObject.type_query(t).instance_size;
            const missing = GObject.type_from_name('MetaNotAType');
            console.log(JSON.stringify({n, missing}));
        """)
        self.assertEqual(got, {"n": 72, "missing": 0})

    def test_the_shell_version_and_the_modal_count_are_live_bindings(self):
        """Both are read through a namespace import in the extensions, so a
        setter is enough -- no module has to be reloaded between cases."""
        got = self.case("""
            import * as Config from 'resource:///org/gnome/shell/misc/config.js';
            import * as Main from 'resource:///org/gnome/shell/ui/main.js';
            const before = [Config.PACKAGE_VERSION, Main.modalCount];
            Config.setPackageVersion('51.beta');
            Main.setModalCount(1);
            console.log(JSON.stringify({before,
                after: [Config.PACKAGE_VERSION, Main.modalCount]}));
        """)
        self.assertEqual(got["before"], ["50.1", 0])
        self.assertEqual(got["after"], ["51.beta", 1])

    def test_girepository_spellings_can_be_taken_away_one_at_a_time(self):
        """`get_shared_library` is glib <= 2.84, `get_shared_libraries` after
        it, and each exists both on the repository and as a static.  The
        overlap extension asks all four because it must not care; the double
        has to be able to answer as any one of those glibs."""
        got = self.case("""
            import GIRepository from 'gi://GIRepository';
            const out = {};
            GIRepository.configure({sharedLibraries: {W11Overlap14: ['libmutter-14.so.0']}});
            const repo = GIRepository.Repository.dup_default();
            out.modern = [typeof repo.get_shared_libraries, typeof repo.get_shared_library,
                          repo.get_shared_libraries('W11Overlap14')];
            GIRepository.configure({spelling: {instanceSharedLibraries: 'missing',
                                               instanceSharedLibrary: 'ok'}});
            const old = GIRepository.Repository.dup_default();
            out.old = [typeof old.get_shared_libraries, old.get_shared_library('W11Overlap14')];
            GIRepository.configure({spelling: {instanceSharedLibrary: 'throw'}});
            const hostile = GIRepository.Repository.dup_default();
            out.hostile = (() => { try { hostile.get_shared_library('W11Overlap14');
                                         return 'answered'; }
                                   catch (e) { return 'threw'; } })();
            console.log(JSON.stringify(out));
        """)
        self.assertEqual(got["modern"], ["function", "undefined", ["libmutter-14.so.0"]])
        self.assertEqual(got["old"], ["undefined", "libmutter-14.so.0"])
        self.assertEqual(got["hostile"], "threw")

    def test_glib_reads_the_files_a_case_puts_there_and_hashes_them_for_real(self):
        """The digest is the overlap extension's whole "did monitors.xml move
        under us" check, so a counter for a hash would make that comparison
        meaningless.  sha256 of "x" is 2d711642...b6e2fd4f."""
        got = self.case("""
            import GLib from 'gi://GLib';
            GLib.setFile('/proc/self/maps', 'abc');
            const [ok, bytes] = GLib.file_get_contents('/proc/self/maps');
            const [gone] = GLib.file_get_contents('/nowhere');
            console.log(JSON.stringify({
                ok, text: new TextDecoder().decode(bytes), gone,
                exists: GLib.file_test('/proc/self/maps', GLib.FileTest.EXISTS),
                sha: GLib.compute_checksum_for_data(GLib.ChecksumType.SHA256,
                                                    new TextEncoder().encode('x')),
                joined: GLib.build_filenamev(['a', 'b', 'c']),
            }));
        """)
        self.assertTrue(got["ok"])
        self.assertEqual(got["text"], "abc")
        self.assertFalse(got["gone"])
        self.assertTrue(got["exists"])
        self.assertEqual(
            got["sha"],
            "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881")
        self.assertEqual(got["joined"], "a/b/c")

    def test_the_overlap_extension_runs_far_enough_to_refuse_an_unknown_shell(self):
        """End to end through the stubs, and the answer is checked against the
        rig: on GNOME 46.0 the table says libmutter-14.so.0, W11Overlap14 and
        MetaMonitorsConfig 72 bytes, and a shell major nobody measured is the
        one refusal `--unsafe-gnome-overlap-unmeasured` can get past."""
        got = self.case("""
            import fs from 'node:fs';
            import GLib from 'gi://GLib';
            import GObject from 'gi://GObject';
            import * as Config from 'resource:///org/gnome/shell/misc/config.js';
            const Ext = (await import('@MODULE@/extension.js')).default;
            GLib.setFile('@MODULE@/generations.json',
                         fs.readFileSync('@MODULE@/generations.json', 'utf8'));
            GLib.setFile('/proc/self/maps',
                '55d0c0000000-55d0c0100000 r--p 0 00:1b 1 /usr/bin/gnome-shell\\n' +
                '7f1200000000-7f1200400000 r-xp 0 00:1b 2 /usr/lib/libmutter-14.so.0\\n');
            GObject.setType('MetaMonitorsConfig', 72);
            Config.setPackageVersion('52.0');
            const e = new Ext({uuid: 'u', path: '@MODULE@'});
            console.log(e.Probe(JSON.stringify({})));
        """, module=support.OVERLAP_EXT)
        self.assertFalse(got["ok"])
        self.assertEqual(got["check"], "shell-version")
        self.assertTrue(got["forceable"])
        self.assertEqual(got["checks"], [])
        self.assertEqual(got["found"]["instance_size"], 72)
        self.assertEqual(got["found"]["sonames"], ["libmutter-14.so.0"])

    def test_the_lib_double_carries_every_symbol_the_extension_demands(self):
        """The seventeen entry points the `symbols` check refuses without --
        the list is the extension's own, read back out of the shipped file so
        the double cannot drift from it."""
        got = self.case("""
            import {makeLib, LIB_SYMBOLS} from '@STUBS@/gjs-globals.mjs';
            import * as H from '@STUBS@/harness.mjs';
            const lib = makeLib({impl: {type_name: () => 'MetaMonitorsConfig'},
                                 drop: ['wr']});
            const name = lib.type_name(4096);
            lib.strn(0, 0);
            console.log(JSON.stringify({symbols: LIB_SYMBOLS, name,
                dropped: typeof lib.wr, calls: H.calls}));
        """)
        with open(os.path.join(support.OVERLAP_EXT, "extension.js"), encoding="utf-8") as fh:
            src = fh.read()
        block = src[src.index("for (const fn of ["):src.index("'verify', 'apply', 'wr']")]
        declared = re.findall(r"'(\w+)'", block + "'verify', 'apply', 'wr'")
        self.assertEqual(got["symbols"], declared)
        self.assertEqual(got["name"], "MetaMonitorsConfig")
        self.assertEqual(got["dropped"], "undefined")
        self.assertEqual([c["what"] for c in got["calls"]], ["lib.type_name", "lib.strn"])
        self.assertEqual(got["calls"][1]["args"], [0, 0])


class SwayDouble(unittest.TestCase):
    """`FakeSway` -- one sway IPC socket, six ways of behaving badly.

    Driven by wxrandr's own SwayIPC client rather than by a hand-rolled
    reader, so what is proved is that a real client sees what the mode
    promises."""

    def sway(self, mode, **kw):
        srv = support.FakeSway(mode, **kw)
        self.addCleanup(srv.close)
        return srv

    def client(self, srv):
        c = SwayIPC(sockpath=srv.path)
        self.addCleanup(c.close)
        return c

    def test_ok_answers_get_outputs_as_sway_1_11_does(self):
        """The two headless outputs a `swaymsg create_output` session has on
        this host: HEADLESS-1 at 0,0 1280x720 and HEADLESS-2 beside it."""
        c = self.client(self.sway("ok"))
        outs = c.get_outputs()
        self.assertEqual([o["name"] for o in outs], ["HEADLESS-1", "HEADLESS-2"])
        self.assertEqual(outs[1]["rect"], {"x": 1280, "y": 0, "width": 1280,
                                           "height": 1024})
        self.assertEqual(c.run("output HEADLESS-2 pos 1280 0"), [{"success": True}])

    def test_refuse_is_the_error_sway_gives_for_a_layout_it_will_not_take(self):
        srv = self.sway("refuse")
        c = self.client(srv)
        with self.assertRaises(Fatal) as cm:
            c.run("output HEADLESS-2 pos 640 0")
        self.assertIn("Cannot apply output configuration", str(cm.exception))
        self.assertEqual(srv.requests, [(support.RUN_COMMAND,
                                         "output HEADLESS-2 pos 640 0")])

    def test_partial_leaves_the_rect_out_entirely(self):
        outs = self.client(self.sway("partial")).get_outputs()
        self.assertEqual(len(outs), 2)
        for o in outs:
            self.assertNotIn("rect", o)
            self.assertIn("name", o)

    def test_badjson_is_a_well_framed_reply_with_a_payload_that_is_not_json(self):
        """The framing has to be right, or what is being tested is the frame
        reader and not the parser above it."""
        with self.assertRaises(json.JSONDecodeError):
            self.client(self.sway("badjson")).get_outputs()

    def test_gone_answers_once_and_then_the_connection_is_closed(self):
        """A peer that has gone gives ECONNRESET on the read and EPIPE on the
        next write -- not a clean EOF -- which is what made this the failure
        that used to reach the user as a traceback.  Either shape counts here:
        which one a client sees depends on whether its own read was already
        posted when the socket went."""
        c = self.client(self.sway("gone"))
        self.assertEqual([o["name"] for o in c.get_outputs()],
                         ["HEADLESS-1", "HEADLESS-2"])
        with self.assertRaises((ConnectionResetError, BrokenPipeError, Fatal)):
            for _ in range(3):
                c.get_outputs()

    def test_wedged_accepts_the_connection_and_never_answers(self):
        """The kernel accepts for a compositor stuck in its own event loop, so
        connecting proves nothing and only the client's deadline ends it."""
        c = self.client(self.sway("wedged"))
        c.sock.settimeout(0.4)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            c.get_outputs()
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_close_ends_the_wedged_handler_instead_of_leaking_it(self):
        """A wedged handler has nothing to answer, so it sleeps -- on the
        server's own stop event, not on the clock.  Sleeping on the clock is
        what the double did where it was lifted from: close() shuts the
        sockets but a `while True: sleep(0.5)` thread lives to the end of the
        interpreter, and T32 opens several wedged sockets per run.  The accept
        loop is counted here too: closing a listening socket does not wake a
        thread blocked in accept() on Linux, so it polls on the same event."""
        # Let the threads of whatever ran before this settle first: several
        # doubles in this file park a handler on their own stop event, and a
        # thread still winding down when `base` is sampled makes the count
        # below say something about the previous test rather than this one.
        base = threading.active_count()
        for _ in range(200):
            time.sleep(0.01)
            now = threading.active_count()
            if now == base:
                break
            base = now
        srv = support.FakeSway("wedged")
        c = SwayIPC(sockpath=srv.path)
        c.sock.settimeout(0.2)
        with self.assertRaises(TimeoutError):
            c.get_outputs()
        for _ in range(200):                       # the handler thread may not have started yet under load
            if threading.active_count() > base:
                break
            time.sleep(0.01)
        self.assertGreater(threading.active_count(), base, "no handler thread ran")
        c.close()
        srv.close()
        for _ in range(200):                       # 2 s, generous for a wakeup
            if threading.active_count() <= base:
                break
            time.sleep(0.01)
        self.assertLessEqual(threading.active_count(), base,
                             "the wedged handler outlived its server")

    def test_the_outputs_can_be_replaced_wholesale(self):
        outs = self.client(self.sway("ok", outputs=[{"name": "Virtual-1"}])).get_outputs()
        self.assertEqual(outs, [{"name": "Virtual-1"}])

    def test_it_records_what_was_asked_of_it(self):
        srv = self.sway("ok")
        c = self.client(srv)
        c.get_outputs()
        c.run("output HEADLESS-1 disable")
        self.assertEqual(srv.requests, [(support.GET_OUTPUTS, ""),
                                        (support.RUN_COMMAND, "output HEADLESS-1 disable")])


class SessionLeader(unittest.TestCase):
    """`leader_process` -- a real process with a session leader's name and a
    session leader's environment, which is the only way to test the /proc walk
    in w11common.session."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="leader-check-")
        self.addCleanup(_rmtree, self.tmp)

    def test_the_stand_in_reaches_the_name_we_look_for(self):
        """`startplasma-x11` is exactly the 15 characters the kernel keeps
        (TASK_COMM_LEN is 16 with the NUL), so it arrives whole."""
        with support.leader_process("startplasma-x11", {"DISPLAY": ":7"}) as leader:
            self.assertEqual(leader.comm(), "startplasma-x11")

    def test_a_longer_name_is_truncated_the_way_the_kernel_truncates_it(self):
        """The other half of the same fact, and the one that can be wrong:
        the walk in w11common.session matches on /proc/<pid>/comm, which the
        kernel cuts at 15 characters.  `gnome-shell-calendar-server` -- a real
        name on every GNOME session here -- reaches /proc as
        `gnome-shell-cal`, so the helper must not wait for more than that."""
        name = "gnome-shell-calendar-server"
        with support.leader_process(name, {}) as leader:
            self.assertEqual(leader.comm(), "gnome-shell-cal")
            self.assertEqual(len(leader.comm()), 15)

    def test_the_environment_is_readable_out_of_proc(self):
        """/proc/<pid>/environ is the environment at execve, which is exactly
        what session.find_x_display reads."""
        cookie = "/tmp/xauth_sddm_test"
        with support.leader_process("kwin_wayland",
                                    {"DISPLAY": ":1", "XAUTHORITY": cookie}) as leader:
            with open("/proc/%d/environ" % leader.pid, "rb") as fh:
                env = fh.read().decode().split("\0")
            self.assertIn("DISPLAY=:1", env)
            self.assertIn("XAUTHORITY=" + cookie, env)

    def test_an_empty_environment_is_an_empty_environment(self):
        """The Plasma Wayland ranking turns on this: kwin_wayland is started
        with nothing in it, and a helper that leaked the runner's DISPLAY in
        would make the test that ranks it pass for the wrong reason."""
        with support.leader_process("kwin_wayland", {}) as leader:
            with open("/proc/%d/environ" % leader.pid, "rb") as fh:
                raw = fh.read()
            self.assertEqual(raw.strip(b"\0"), b"")

    @unittest.skipUnless(_SLEEP_IS_MULTICALL,
                         "this host's sleep is not the uutils multicall binary")
    def test_a_copy_of_sleep_is_not_a_stand_in_on_this_distribution(self):
        """Ubuntu 26.04 ships uutils coreutils as ONE multicall binary
        (/usr/bin/sleep resolves to /usr/lib/cargo/bin/coreutils here) which
        dispatches on argv[0]: a copy named `startplasma-x11` exits 1 with
        "coreutils: unknown program 'startplasma-x11'".  It still leaves a
        corpse whose /proc/<pid>/comm reads the name, so a helper built on
        that copy looks like it worked and hands out a pid with an empty
        environ -- which is why leader_process is a blocked /bin/sh and waits
        for the environment too.  (Measured here: tests/test_backend_gnome.py's
        own copy of the sleep stand-in fails when that file is run on its own,
        `find_xauthority` answering None.)  The skip guard is the precondition,
        not a way out: on a GNU-coreutils host this name is not a true claim
        and there is nothing here to assert."""
        copy = os.path.join(self.tmp, "startplasma-x11")
        shutil.copy(_SLEEP_REAL, copy)
        r = subprocess.run([copy, "0.1"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("coreutils: unknown program", r.stderr)
        self.assertIn("startplasma-x11", r.stderr)

    def test_the_stand_in_is_alive_and_carries_its_environment(self):
        """What the copy of sleep above could not give: a process still
        running when the caller gets the pid, with the variables readable."""
        with support.leader_process("startplasma-x11", {"DISPLAY": ":7"}) as leader:
            self.assertIsNone(leader.proc.poll(), "the stand-in has to be alive")
            self.assertEqual(leader.environ()["DISPLAY"], ":7")
            self.assertEqual(leader.comm(), "startplasma-x11")

    def test_stop_leaves_nothing_running_and_nothing_behind(self):
        leader = support.leader_process("gnome-shell", {})
        path = leader.path
        leader.stop()
        self.assertFalse(os.path.exists(path))
        for _ in range(250):
            if leader.comm() is None:
                break
            time.sleep(0.02)
        self.assertIsNone(leader.comm())


class WlMirrorStub(unittest.TestCase):
    """`WL_MIRROR_STUB` -- wl-mirror's stand-in, so wmirror's supervisor can
    be tested with no compositor and no GPU."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wl-mirror-stub-")
        self.addCleanup(_rmtree, self.tmp)
        self.log = os.path.join(self.tmp, "log")
        self.stub = support.write_wl_mirror_stub(self.tmp)

    def run_stub(self, *argv, **env):
        e = dict(os.environ, WMIRROR_STUB_LOG=self.log, WMIRROR_STUB_LIFE="0.1",
                 WAYLAND_DISPLAY="wayland-9")
        e.update(env)
        return subprocess.run([self.stub, *argv], capture_output=True, text=True,
                              env=e, timeout=30)

    def test_it_logs_its_arguments_and_the_display_it_saw(self):
        """Which WAYLAND_DISPLAY the helper was started with is the thing
        wmirror gets wrong when it forks: the log is how that is read back."""
        r = self.run_stub("--fullscreen", "HEADLESS-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.log) as fh:
            self.assertEqual(fh.read().strip(),
                             "--fullscreen HEADLESS-1 wayland=wayland-9")

    def test_the_libegl_warning_is_on_stderr_of_a_run_that_succeeded(self):
        """Real wl-mirror prints it on a headless box, and wmirror must not
        read a warning as a failure."""
        r = self.run_stub("HEADLESS-1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("libEGL warning", r.stderr)

    def test_the_failure_knob_is_wl_mirrors_own_wording(self):
        r = self.run_stub("NOPE", WMIRROR_STUB_FAIL="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("options::find_output(): output NOPE not found", r.stderr)

    def test_it_stays_up_for_the_life_it_is_given(self):
        started = time.monotonic()
        self.run_stub("HEADLESS-1", WMIRROR_STUB_LIFE="0.5")
        self.assertGreaterEqual(time.monotonic() - started, 0.45)

    def test_it_is_the_same_text_test_wmirror_lifetime_runs(self):
        """The stub moved here; a second copy that drifted would be two
        different wl-mirrors under one name.  Either that file still carries
        its own copy, in which case the two texts have to be equal, or T52's
        owner has replaced it with the one here, in which case the file has to
        say so -- there is no third state where nothing is checked."""
        with open(os.path.join(ROOT, "tests", "test_wmirror_lifetime.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        m = re.search(r'^STUB = """(.*?)"""$', src, re.S | re.M)
        if m is not None:
            self.assertEqual(eval('"""%s"""' % m.group(1)), support.WL_MIRROR_STUB)
            return
        self.assertRegex(src, r"support\.(WL_MIRROR_STUB|write_wl_mirror_stub)",
                         "the local copy is gone and support's is not used")


class SwayDoubleI3Dialect(unittest.TestCase):
    """`FakeSway(dialect="i3")` -- the same socket answering as a live i3 4.25.1 did.

    Every payload is a recording under tests/fixtures/i3/, taken off an i3 4.25.1-1 under Xvfb
    [M recon2/i3.md §1, §2b]. What the dialect is for is the four places i3 differs from sway on the same
    wire, each of which was a lie the sway backend told when forced onto i3: a major version of 4, an outputs
    list with no modes and a pseudo-output, 47-bit container ids with the X id in `window`, and a parse error
    in place of every `output ...`."""

    def sway(self, **kw):
        srv = support.FakeSway("ok", dialect="i3", **kw)
        self.addCleanup(srv.close)
        return srv

    def client(self, srv):
        c = SwayIPC(sockpath=srv.path)
        self.addCleanup(c.close)
        return c

    def test_get_version_is_i3s_and_not_sways(self):
        """sway is 1.x and i3 has been 4.x since 2011, so the major version alone tells them apart -- which is
        what the backend reads instead of guessing from a socket name."""
        v = self.client(self.sway()).msg(support.GET_VERSION)
        self.assertEqual(v["major"], 4)
        self.assertEqual(v["human_readable"], "4.25.1 (2026-02-06)")

    def test_get_outputs_has_the_pseudo_output_and_no_modes(self):
        """i3 reports an `xroot-0` covering the X screen with `active: false`, and carries none of
        modes/current_mode/transform/scale/make/model/serial -- so a wxrandr that invents a mode table here is
        inventing it."""
        outs = self.client(self.sway()).get_outputs()
        self.assertEqual([o["name"] for o in outs], ["xroot-0", "screen"])
        self.assertFalse(outs[0]["active"])
        self.assertIsNone(outs[0]["current_workspace"])
        for o in outs:
            for absent in ("modes", "current_mode", "transform", "scale",
                           "make", "model", "serial", "id"):
                self.assertNotIn(absent, o, (o["name"], absent))

    def test_the_default_tree_is_the_one_with_the_floating_window(self):
        """The interesting recording is the default: a double whose tree has nothing floating in it lets the
        floating defect through without a word."""
        srv = self.sway()
        self.assertEqual(srv.tree, support.fixture_json("i3", "get_tree_floating.json"))
        plain = support.FakeSway("ok", dialect="i3",
                                 tree=support.fixture_json("i3", "get_tree.json"))
        self.addCleanup(plain.close)
        self.assertEqual(self.client(plain).msg(support.GET_TREE)["id"], srv.tree["id"])

    def test_the_tree_carries_47_bit_ids_and_the_x_id_in_window(self):
        """A con id truncated into a 32-bit XID was measured reaching the X server as `0x5168c680` and coming
        back BadWindow; `window` is the X id and is what a wmctrl/xprop clone may print."""
        tree = self.client(self.sway()).msg(support.GET_TREE)

        def walk(node):
            yield node
            for key in ("nodes", "floating_nodes"):
                for child in node.get(key, ()):
                    yield from walk(child)

        nodes = list(walk(tree))
        self.assertTrue(any(n["id"] > 0xFFFFFFFF for n in nodes),
                        "no 47-bit container id in the recorded tree")
        views = [n for n in nodes if n.get("window")]
        self.assertTrue(views)
        for n in views:
            self.assertLess(n["window"], 1 << 32)
            self.assertNotIn("app_id", n)
            self.assertNotIn("pid", n)
            self.assertNotIn("visible", n)

    def test_a_floating_view_is_wrapped_in_a_floating_con(self):
        """i3 wraps a floating view in a `floating_con` and the walk resets the floating flag on the way down,
        which is why every floating window looked tiled and `windowmove` refused."""
        tree = self.client(self.sway()).msg(support.GET_TREE)

        def find(node, pred):
            if pred(node):
                return node
            for key in ("nodes", "floating_nodes"):
                for child in node.get(key, ()):
                    hit = find(child, pred)
                    if hit is not None:
                        return hit
            return None

        wrapper = find(tree, lambda n: n.get("type") == "floating_con")
        self.assertIsNotNone(wrapper, "the recorded tree has no floating_con")
        self.assertTrue(wrapper["nodes"], "the floating_con wraps nothing")
        self.assertTrue(any(c.get("window") for c in wrapper["nodes"]))

    def test_every_output_command_is_i3s_parse_error(self):
        """i3 has no `output` command at all: the X server owns the layout there, and every apply came back as
        a 30-token "Expected one of these tokens" list."""
        srv = self.sway()
        c = self.client(srv)
        with self.assertRaises(Fatal):
            c.run("output screen position 0 0")
        self.assertEqual(srv.requests[-1],
                         (support.RUN_COMMAND, "output screen position 0 0"))
        # ...and only `output`: everything else i3 really does have still works
        self.assertEqual(c.run("[con_id=97479943571072] focus"), [{"success": True}])

    def test_the_sway_dialect_is_untouched(self):
        """The default is still sway 1.11's payloads, byte for byte: two headless outputs with modes, and a
        RUN_COMMAND that succeeds."""
        srv = support.FakeSway("ok")
        self.addCleanup(srv.close)
        c = SwayIPC(sockpath=srv.path)
        self.addCleanup(c.close)
        self.assertEqual([o["name"] for o in c.get_outputs()],
                         ["HEADLESS-1", "HEADLESS-2"])
        self.assertEqual(c.run("output HEADLESS-2 pos 1280 0"), [{"success": True}])


class HyprDouble(unittest.TestCase):
    """`FakeHypr` and `FakeHyprEvents` -- Hyprland's two sockets.

    The protocol was proved with a 10-line AF_UNIX client against a live 0.53.3 before any of this was
    written: send the request as text, read the reply to EOF, one connection per request
    [M recon2/hyprland.md §2]. This drives the double the same way, so what is checked is that a plain socket
    client sees what each mode promises."""

    def hypr(self, mode="ok", **kw):
        srv = support.FakeHypr(mode, **kw)
        self.addCleanup(srv.close)
        return srv

    @staticmethod
    def ask(srv, request, timeout=5.0):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(timeout)
        try:
            c.connect(srv.path)
            c.sendall(request.encode())
            out = b""
            while True:
                chunk = c.recv(65536)
                if not chunk:
                    return out
                out += chunk
        finally:
            c.close()

    def test_every_recorded_payload_comes_back_as_json(self):
        """Against the files on disk, not against `srv.payloads`: the table is what the double loads, so
        comparing the wire to it would pass with the loader broken. `cursorpos` has no file -- it is
        support.HYPR_CURSORPOS, an [R] shape read off HyprCtl.cpp -- and is checked as its two numbers."""
        srv = self.hypr()
        for name in ("monitors", "clients", "workspaces", "devices",
                     "activewindow", "activeworkspace", "version"):
            path = os.path.join(ROOT, "tests", "fixtures", "hypr", name + ".json")
            with open(path, encoding="utf-8") as fh:
                want = json.load(fh)
            self.assertEqual(json.loads(self.ask(srv, "j/" + name)), want, name)
        self.assertEqual(json.loads(self.ask(srv, "j/cursorpos")), {"x": 640, "y": 360})
        self.assertEqual(srv.requests, ["j/" + n for n in
                                        ("monitors", "clients", "workspaces", "devices",
                                         "activewindow", "activeworkspace", "version",
                                         "cursorpos")])

    def test_the_payloads_are_the_recorded_session(self):
        """Not a hand-written shape: the clients list is the four windows a live 0.53.3 had, with the xmessage
        row that is an XWayland client (pid 11920, class Xmessage, at 300,200 size 400x300) and the foot row
        the geometry defect was measured on (at 100,120 size 800x600, where the wlr floor reported the whole
        output)."""
        srv = self.hypr()
        clients = srv.payloads["clients"]
        self.assertEqual(len(clients), 4)
        foot = clients[0]
        self.assertEqual((foot["at"], foot["size"]), ([100, 120], [800, 600]))
        self.assertEqual(foot["address"], "0x59daae6de8f0")
        xmsg = [c for c in clients if c["xwayland"]]
        self.assertEqual(len(xmsg), 1)
        self.assertEqual((xmsg[0]["pid"], xmsg[0]["class"]), (11920, "Xmessage"))
        self.assertEqual(srv.payloads["monitors"][0]["refreshRate"], 74.998)
        self.assertEqual(srv.payloads["version"]["version"], "0.53.3")

    def test_a_dispatch_is_ok_and_refuse_is_hyprlands_own_words(self):
        srv = self.hypr()
        self.assertEqual(self.ask(srv, "dispatch focuswindow address:0x59daae6de8f0"), b"ok")
        srv2 = self.hypr("refuse")
        self.assertEqual(self.ask(srv2, "dispatch banana"), b"Invalid dispatcher")
        # `keyword` is not a dispatch and is not refused by that mode
        self.assertEqual(self.ask(srv2, "keyword monitor Virtual-1,disable"), b"ok")

    def test_the_socket_is_closed_after_one_reply(self):
        """The reply is delimited by EOF and by nothing else, so a client that keeps the connection for a
        second request would block for ever against a real Hyprland."""
        srv = self.hypr()
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5.0)
        self.addCleanup(c.close)
        c.connect(srv.path)
        c.sendall(b"j/version")
        data = b""
        while True:
            chunk = c.recv(65536)
            if not chunk:
                break
            data += chunk
        self.assertTrue(json.loads(data))
        self.assertEqual(c.recv(4096), b"")

    def test_the_broken_modes_are_each_a_different_failure(self):
        self.assertEqual(self.ask(self.hypr("gone"), "j/monitors"), b"")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self.ask(self.hypr("badjson"), "j/monitors"))
        short = self.ask(self.hypr("short"), "j/monitors")
        self.assertEqual(len(short), 12)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(short)

    def test_wedged_answers_nothing_until_the_double_is_closed(self):
        """A compositor stuck in its own event loop: the kernel accepts for it, so only the client's own
        deadline ends the wait. The double must not answer late either."""
        srv = self.hypr("wedged")
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(0.4)
        c.connect(srv.path)
        c.sendall(b"j/monitors")
        with self.assertRaises(TimeoutError):
            c.recv(4096)

    def test_the_event_socket_replays_the_five_recorded_lines(self):
        srv = support.FakeHyprEvents()
        self.addCleanup(srv.close)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(5.0)
        c.connect(srv.path)
        got = b""
        while got.count(b"\n") < 5:
            got += c.recv(65536)
        self.assertEqual(got.decode().splitlines(), [
            "windowtitle>>59daae69e360",
            "windowtitlev2>>59daae69e360,foot",
            "openwindow>>59daae69e360,1,foot,foot",
            "activewindow>>foot,foot",
            "activewindowv2>>59daae69e360",
        ])

    def test_a_held_event_socket_says_nothing_until_told_to(self):
        srv = support.FakeHyprEvents(hold=True)
        self.addCleanup(srv.close)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(0.3)
        c.connect(srv.path)
        with self.assertRaises(TimeoutError):
            c.recv(4096)
        for _ in range(20):
            if srv.conns:
                break
            time.sleep(0.05)
        srv.send("closewindow>>59daae69e360")
        c.settimeout(5.0)
        self.assertEqual(c.recv(4096), b"closewindow>>59daae69e360\n")


class WayfireDouble(unittest.TestCase):
    """`FakeWayfire` -- the `<i`-length-prefixed JSON socket, and its two error shapes.

    The framing is the measured one: a `stipc/ping` went out as four length bytes and then a 0x24-byte body
    [M recon2/wayfire.md §1.2]. Every answer is a recorded capture."""

    def wayfire(self, mode="ok", **kw):
        srv = support.FakeWayfire(mode, **kw)
        self.addCleanup(srv.close)
        return srv

    def call(self, srv, method, data=None, timeout=5.0):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(timeout)
        self.addCleanup(c.close)
        c.connect(srv.path)
        body = json.dumps({"method": method, "data": data or {}}).encode()
        c.sendall(struct.pack("<i", len(body)) + body)
        head = c.recv(4)
        if len(head) < 4:
            return None
        (n,) = struct.unpack("<i", head)
        out = b""
        while len(out) < n:
            chunk = c.recv(n - len(out))
            if not chunk:
                break
            out += chunk
        return json.loads(out.decode())

    def test_the_frame_is_a_four_byte_length_and_a_json_body(self):
        body = b'{"method": "stipc/ping", "data": {}}'
        self.assertEqual(len(body), 0x24)
        self.assertEqual(support.FakeWayfire.frame({"method": "stipc/ping", "data": {}}),
                         struct.pack("<i", len(body)) + body)

    def test_the_recorded_views_come_back(self):
        srv = self.wayfire()
        views = self.call(srv, "window-rules/list-views")
        self.assertTrue(views)
        self.assertEqual(srv.calls, [("window-rules/list-views", {})])
        roles = {v.get("role") for v in views}
        self.assertIn("toplevel", roles)

    def test_two_heads_and_a_three_by_three_grid_are_in_the_recording(self):
        """Wayfire's desktops are a 3x3 viewport grid per output, and the capture has two outputs so the
        `output-id` rule has something to be wrong about."""
        outs = self.call(self.wayfire(), "window-rules/list-outputs")
        self.assertEqual([o["name"] for o in outs], ["HEADLESS-1", "HEADLESS-2"])
        self.assertEqual(outs[0]["workspace"]["grid_width"], 3)
        self.assertEqual(outs[0]["workspace"]["grid_height"], 3)
        self.assertEqual(outs[1]["id"], 9)

    def test_the_data_of_every_call_is_recorded(self):
        srv = self.wayfire()
        self.call(srv, "vswitch/set-workspace", {"x": 1, "y": 0, "output-id": 1})
        self.assertEqual(srv.calls,
                         [("vswitch/set-workspace", {"x": 1, "y": 0, "output-id": 1})])

    def test_nomethod_is_the_shape_a_wayfire_without_ipc_rules_gives(self):
        """A socket that exists and answers this to `window-rules/list-views` is Wayfire 0.8, or 0.9 without
        `plugins = ipc ipc-rules`; the backend has an api gate for exactly that and must not fall through to
        the wlr floor instead."""
        got = self.call(self.wayfire("nomethod"), "window-rules/list-views")
        self.assertEqual(got, {"error": "No such method found!",
                               "method": "window-rules/list-views"})

    def test_a_method_the_recording_does_not_have_is_the_same_error(self):
        got = self.call(self.wayfire(), "window-rules/banana")
        self.assertEqual(got["error"], "No such method found!")

    def test_handler_error_is_the_other_shape(self):
        """Recorded whole: a Wayfire handler that raised answers with a sentence naming the method and the
        argument, and no `method` key of its own -- so a client that reads `error` and prints it is right and
        one that reads `method` is not."""
        got = self.call(self.wayfire("handler-error"), "window-rules/list-views")
        self.assertEqual(got, {"error": 'Error during execution of the handler for '
                                        'method "window-rules/view-info": Missing "id"'})
        self.assertNotIn("method", got)

    def test_silent_reads_the_frame_and_writes_nothing(self):
        """The connection stays up and the request is parsed; only the answer never comes, which is the
        failure a client can only end with a deadline of its own."""
        srv = self.wayfire("silent")
        with self.assertRaises(TimeoutError):
            self.call(srv, "window-rules/list-views", timeout=0.4)
        self.assertEqual(srv.calls, [("window-rules/list-views", {})])


class RegistryFixtures(unittest.TestCase):
    """`wl_fake.registry_server` -- a compositor that is nothing but its recorded global list.

    Detection is a question about the registry and about nothing else, so a detection test that hand-writes
    two interfaces is testing its own opinion. Each fixture under tests/fixtures/registries/ is a real dump:
    the `#` header says which compositor, which version and which recon report it came out of."""

    def globals_of(self, fixture):
        srv = wl_fake.registry_server(fixture)
        self.addCleanup(srv.close)
        c = WlConn(srv.path)
        self.addCleanup(c.close)
        return {iface: ver for iface, ver in c.get_registry().values()}

    def test_the_file_is_what_arrives_on_the_wire(self):
        """Read the .txt here rather than through `registry_fixture()`: one parser on both sides of the
        assertion would let a parser bug through unseen, and the file is what the recon report recorded."""
        for fixture in ("cosmic", "labwc", "hyprland", "cinnamon"):
            with self.subTest(fixture):
                path = os.path.join(ROOT, "tests", "fixtures", "registries", fixture + ".txt")
                want = {}
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        if line.startswith("#") or not line.strip():
                            continue
                        iface, ver = line.split()
                        want[iface] = int(ver)
                self.assertTrue(want)
                self.assertEqual(self.globals_of(fixture), want)

    def test_cosmic_has_no_wlr_toplevel_manager_and_labwc_does(self):
        """The single fact the COSMIC detection step exists for: cosmic-comp's 53 globals carry
        `ext_foreign_toplevel_list_v1` and `zcosmic_toplevel_info_v1` and no
        `zwlr_foreign_toplevel_manager_v1` at all [M recon2/cosmic.md §2, §4]."""
        cosmic = self.globals_of("cosmic")
        self.assertNotIn("zwlr_foreign_toplevel_manager_v1", cosmic)
        self.assertEqual(cosmic["ext_foreign_toplevel_list_v1"], 1)
        self.assertEqual(cosmic["zcosmic_toplevel_info_v1"], 3)
        self.assertEqual(cosmic["zcosmic_output_manager_v1"], 3)
        self.assertNotIn("zwlr_virtual_pointer_manager_v1", cosmic)
        self.assertEqual(self.globals_of("labwc")["zwlr_foreign_toplevel_manager_v1"], 3)

    def test_muffin_has_neither_family(self):
        """23 globals, which is why the Cinnamon bus name may never be swallowed by the registry step
        [M recon2/cinnamon.md §2.1]."""
        cin = self.globals_of("cinnamon")
        self.assertEqual(len(cin), 23)
        self.assertNotIn("zwlr_foreign_toplevel_manager_v1", cin)
        self.assertNotIn("ext_foreign_toplevel_list_v1", cin)
        self.assertNotIn("zwlr_output_manager_v1", cin)

    def test_the_workspace_protocol_is_where_the_reports_found_it(self):
        """labwc, Budgie and Xfce-on-Wayland publish it and Wayfire 0.10 does not, which is why the wlr
        backend's desktop refusal has to stay for the compositors that have no answer."""
        for fixture in ("labwc", "budgie", "xfce-labwc", "cosmic", "hyprland"):
            self.assertIn("ext_workspace_manager_v1", self.globals_of(fixture), fixture)
        self.assertNotIn("ext_workspace_manager_v1", self.globals_of("xfce-wayfire"))

    def test_it_binds_nothing_and_offers_no_manager(self):
        srv = wl_fake.registry_server("cosmic")
        self.addCleanup(srv.close)
        c = WlConn(srv.path)
        self.addCleanup(c.close)
        c.get_registry()
        self.assertEqual(srv.binds, [])
        self.assertIsNone(srv.mgr_name)


class WorkspaceDouble(unittest.TestCase):
    """`wl_fake.WorkspaceServer` and the `ext_workspace_manager_v1` client that reads it.

    Three recorded sets: labwc's three configured names with no `coordinates` event at all, Budgie's four, and
    COSMIC's two with coordinates `[1]` and `[2]` [M recon2/labwc.md §6a, budgie.md, cosmic.md §4]. The
    ordering rule has to cover both shapes at once, which is why it is one comparison and not two branches."""

    def compositor(self, workspaces=wl_fake.LABWC_WORKSPACES):
        srv = wl_fake.WorkspaceCompositor(workspaces)
        self.addCleanup(srv.close)
        return srv

    def client(self, srv):
        c = WlConn(srv.path)
        self.addCleanup(c.close)
        c.get_registry()
        ws = ext_workspace.WorkspaceClient.bind(c)
        self.assertIsNotNone(ws)
        c.roundtrip()
        return ws

    def test_labwcs_three_names_and_the_active_bit(self):
        ws = self.client(self.compositor())
        self.assertEqual([w.name for w in ws.workspace_list()], ["one", "two", "three"])
        self.assertEqual([w.active for w in ws.workspace_list()], [True, False, False])
        self.assertEqual(ws.count(), 3)
        self.assertEqual(ws.active_index(), 0)

    def test_budgies_four(self):
        ws = self.client(self.compositor(wl_fake.BUDGIE_WORKSPACES))
        self.assertEqual([w.name for w in ws.workspace_list()],
                         ["Workspace 1", "Workspace 2", "Workspace 3", "Workspace 4"])
        self.assertEqual(ws.active_index(), 0)

    def test_cosmics_two_are_ordered_by_their_coordinates(self):
        """COSMIC sends `coordinates`; labwc sends the event not at all. One rule -- (coordinates, arrival) --
        covers both, and a client that ordered by arrival alone would be right by luck here and wrong the day
        cosmic-comp announces them in another order."""
        srv = self.compositor(tuple(reversed(wl_fake.COSMIC_WORKSPACES)))
        ws = self.client(srv)
        self.assertEqual([w.name for w in ws.workspace_list()], ["1", "2"])
        self.assertEqual(ws.active_index(), -1)      # neither is active in the recording

    def test_no_global_is_none_rather_than_an_error(self):
        """sway 1.11 and Wayfire 0.10 publish no workspace protocol; the backend keeps its own refusal for
        them and must not have to catch anything to find that out."""
        srv = wl_fake.registry_server("xfce-wayfire")
        self.addCleanup(srv.close)
        c = WlConn(srv.path)
        self.addCleanup(c.close)
        c.get_registry()
        self.assertIsNone(ext_workspace.WorkspaceClient.bind(c))

    def test_activate_is_the_handle_then_a_commit_on_the_manager(self):
        """`activate` alone changes nothing: ext-workspace is double buffered and the manager's `commit` is
        what applies the batch [M recon2/labwc.md §6a]."""
        srv = self.compositor()
        ws = self.client(srv)
        self.assertTrue(ws.activate(2))
        ws.c.roundtrip()
        self.assertEqual(srv.ws_calls, [("activate", 2), ("commit", None)])

    def test_the_active_bit_moves_when_the_compositor_answers(self):
        """Recording the request is not enough: an ignored request looks the same from this side, and the
        state event is the only evidence the compositor took it."""
        srv = self.compositor()
        ws = self.client(srv)
        ws.activate(2)
        srv.set_active(2)
        ws.c.roundtrip()
        self.assertEqual(ws.active_index(), 2)
        self.assertEqual([w.active for w in ws.workspace_list()], [False, False, True])

    def test_an_index_out_of_range_sends_nothing(self):
        srv = self.compositor()
        ws = self.client(srv)
        for bad in (-1, 3, 99):
            self.assertFalse(ws.activate(bad), bad)
        ws.c.roundtrip()
        self.assertEqual(srv.ws_calls, [])

    def test_a_workspace_that_cannot_be_activated_is_refused_not_asked(self):
        """The capability array is the protocol's own statement about what it will accept; sending the request
        anyway would be a silent no-op the caller reports as a success."""
        rows = (("one", 1, 1, None), ("two", 0, 0, None))
        srv = self.compositor(rows)
        ws = self.client(srv)
        self.assertTrue(ws.can_activate(0))
        self.assertFalse(ws.can_activate(1))
        self.assertFalse(ws.activate(1))
        ws.c.roundtrip()
        self.assertEqual(srv.ws_calls, [])


class CosmicDouble(unittest.TestCase):
    """`wl_fake.CosmicCompositor` -- the two toplevel protocols cosmic-comp has and the wlr one it does not.

    Everything replayed here was driven by hand against a live cosmic-comp first
    (`recon2/cosmic/cosmic_probe.py`, `cosmic_act.py`): three toplevels with 32-character identifiers,
    `capabilities [1,2,3,4,6]`, and `set_maximized` -> `state [0]`, `unset_maximized` -> `[]`,
    `activate` -> `[2]` [M recon2/cosmic.md §4]."""

    def compositor(self, **kw):
        srv = wl_fake.CosmicCompositor(**kw)
        self.addCleanup(srv.close)
        return srv

    def connect(self, srv):
        c = WlConn(srv.path)
        self.addCleanup(c.close)
        c.get_registry()
        return c

    def toplevels(self, srv, c):
        """Bind the list and the info manager, and collect (identifier, title, app_id, states)."""
        rows = {}
        g = c.find_global(wl_fake.EXT_TOPLEVEL_LIST)
        lst = c.bind(g[0], wl_fake.EXT_TOPLEVEL_LIST, 1)
        handles = []

        def on_list(op, cur, fds):
            if op == 0:
                oid = cur.u32()
                handles.append(oid)
                rec = {"oid": oid, "title": "", "app_id": "", "identifier": "",
                       "states": [], "closed": False}
                rows[oid] = rec
                c.on(oid, lambda o, cu, f, r=rec: on_handle(r, o, cu))

        def on_handle(rec, op, cur):
            if op == 0:
                rec["closed"] = True
            elif op == 2:
                rec["title"] = cur.string()
            elif op == 3:
                rec["app_id"] = cur.string()
            elif op == 4:
                rec["identifier"] = cur.string()

        def on_cosmic(rec, op, cur):
            if op == 0:
                rec["closed"] = True
            elif op == 8:
                arr = cur.array()
                rec["states"] = list(struct.unpack("<%dI" % (len(arr) // 4), arr))
            elif op == 9:
                rec["geometry"] = (cur.u32(), cur.i32(), cur.i32(), cur.i32(), cur.i32())

        c.on(lst, on_list)
        c.roundtrip()
        gi = c.find_global(wl_fake.COSMIC_INFO)
        info = c.bind(gi[0], wl_fake.COSMIC_INFO, 3)
        c.on(info, lambda op, cur, fds: None)
        for oid in handles:
            cid = c.alloc()
            rec = rows[oid]
            c.send(info, 1, [("u", cid), ("u", oid)])
            c.on(cid, lambda o, cu, f, r=rec: on_cosmic(r, o, cu))
            rec["cosmic"] = cid
        c.roundtrip()
        return [rows[oid] for oid in handles]

    def test_the_three_recorded_toplevels_arrive_with_their_identifiers(self):
        srv = self.compositor()
        rows = self.toplevels(srv, self.connect(srv))
        self.assertEqual([r["title"] for r in rows],
                         ["cosmicterm", "typedtest", "cosmicxterm"])
        self.assertEqual([r["app_id"] for r in rows], ["foot", "foot", "XTerm"])
        self.assertEqual(rows[0]["identifier"], "LsUebsS7Qe8NowEoh7IP065Bbw8xd69L")
        for r in rows:
            self.assertEqual(len(r["identifier"]), 32)

    def test_the_registry_is_cosmics_and_not_wlrs(self):
        srv = self.compositor()
        c = self.connect(srv)
        ifaces = {i for i, _v in c.get_registry().values()}
        self.assertIn(wl_fake.EXT_TOPLEVEL_LIST, ifaces)
        self.assertIn(wl_fake.COSMIC_INFO, ifaces)
        self.assertIn("ext_workspace_manager_v1", ifaces)
        self.assertNotIn("zwlr_foreign_toplevel_manager_v1", ifaces)
        self.assertNotIn("zwlr_virtual_pointer_manager_v1", ifaces)

    def test_the_manager_announces_the_recorded_capability_array(self):
        """[1,2,3,4,6] = close, activate, maximize, minimize, move_to_workspace. No 5 (fullscreen) and no 7
        (sticky), which is what a backend has to gate on rather than guess."""
        srv = self.compositor()
        c = self.connect(srv)
        g = c.find_global(wl_fake.COSMIC_MGR)
        mgr = c.bind(g[0], wl_fake.COSMIC_MGR, 4)
        caps = []

        def on_mgr(op, cur, fds):
            if op == 0:
                arr = cur.array()
                caps.extend(struct.unpack("<%dI" % (len(arr) // 4), arr))
        c.on(mgr, on_mgr)
        c.roundtrip()
        self.assertEqual(caps, [1, 2, 3, 4, 6])
        self.assertNotIn(5, caps)
        self.assertNotIn(7, caps)

    def test_maximize_activate_and_close_answer_the_way_the_live_one_did(self):
        srv = self.compositor()
        c = self.connect(srv)
        rows = self.toplevels(srv, c)
        g = c.find_global(wl_fake.COSMIC_MGR)
        mgr = c.bind(g[0], wl_fake.COSMIC_MGR, 4)
        c.on(mgr, lambda op, cur, fds: None)
        first = rows[0]
        self.assertEqual(first["states"], [])
        c.send(mgr, 3, [("u", first["cosmic"])])      # set_maximized
        c.roundtrip()
        self.assertEqual(first["states"], [0])
        c.send(mgr, 4, [("u", first["cosmic"])])      # unset_maximized
        c.roundtrip()
        self.assertEqual(first["states"], [])
        c.send(mgr, 2, [("u", first["cosmic"]), ("u", 0)])   # activate(seat)
        c.roundtrip()
        self.assertEqual(first["states"], [2])
        c.send(mgr, 1, [("u", first["cosmic"])])      # close
        c.roundtrip()
        self.assertTrue(first["closed"])
        self.assertEqual([n for n, _a in srv.calls],
                         ["set_maximized", "unset_maximized", "activate", "close"])

    def test_no_geometry_event_arrives_by_default(self):
        """The recorded case, and the honest one: no geometry event was ever delivered in the nested rig, not
        in 4 s and not after a maximize, because cosmic-comp sends it only alongside `output_enter` or on a
        change. A double that invented one would hide the whole question."""
        srv = self.compositor()
        rows = self.toplevels(srv, self.connect(srv))
        for r in rows:
            self.assertNotIn("geometry", r)

    def test_a_compositor_that_does_send_geometry_can_be_asked_for(self):
        srv = self.compositor(geometry=(22, 61, 1876, 997))
        rows = self.toplevels(srv, self.connect(srv))
        for r in rows:
            self.assertEqual(r["geometry"], (0, 22, 61, 1876, 997))

    def test_its_workspaces_are_cosmics_two(self):
        srv = self.compositor()
        c = self.connect(srv)
        ws = ext_workspace.WorkspaceClient.bind(c)
        self.assertIsNotNone(ws)
        c.roundtrip()
        self.assertEqual([w.name for w in ws.workspace_list()], ["1", "2"])


class HeadlessCompositors(unittest.TestCase):
    """The five compositors the live tests boot, checked against the finders that have to see them.

    Each one skips cleanly when its binary is absent, which is how they behave in the suite; where the binary
    is there they are booted for real, because the thing worth proving is that `w11common.session` finds the
    socket a live compositor actually wrote and not the one a fixture says it did. labwc, Wayfire, i3 and
    Openbox are packaged on this box and on the Ubuntu CI image; river needs `tinyrwm` beside it and is on no
    runner [M recon2/river.md §2].

    Both wlroots helpers use a SHORT `/tmp` name on purpose: labwc dies with `File name too long` and Wayfire
    with `socket path ... exceeds 108 bytes` under the suite's usual long temporary prefix
    [M recon2/labwc.md, recon2/wayfire.md]."""

    def boot(self, cls, **kw):
        rig = cls(**kw)
        self.addCleanup(rig.stop)
        return rig

    def finders(self, rig):
        """The finders, run against this compositor's runtime dir and nothing else."""
        ctx = support.env(XDG_RUNTIME_DIR=rig.rtdir, WAYLAND_DISPLAY=None,
                          SWAYSOCK=None, I3SOCK=None, WAYFIRE_SOCKET=None,
                          _WAYFIRE_SOCKET=None, HYPRLAND_INSTANCE_SIGNATURE=None)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_labwc_comes_up_with_three_named_desktops(self):
        rig = self.boot(support.HeadlessLabwc)
        self.assertTrue(os.path.exists(rig.wayland_socket))
        c = WlConn(rig.wayland_socket)
        self.addCleanup(c.close)
        c.get_registry()
        ws = ext_workspace.WorkspaceClient.bind(c)
        self.assertIsNotNone(ws, "this labwc publishes no ext_workspace_manager_v1")
        c.roundtrip()
        self.assertEqual([w.name for w in ws.workspace_list()], ["one", "two", "three"])
        self.assertEqual(ws.active_index(), 0)

    def test_wayfire_writes_the_socket_the_finder_looks_for(self):
        """The runtime-dir name has no pid field -- `wayfire-<display>-.socket`, recorded as
        `wayfire-wayland-1-.socket` -- while the /tmp one does, which is why `find_wayfire_socket()` matches
        on the prefix and the suffix and not on the shape between them [M recon2/wayfire.md §1.2]."""
        rig = self.boot(support.HeadlessWayfire)
        self.assertTrue(os.path.basename(rig.sock).startswith("wayfire-"))
        self.assertTrue(rig.sock.endswith(".socket"))
        self.finders(rig)
        self.assertEqual(session.find_wayfire_socket(), rig.sock)
        # and the variable Wayfire exports to its children wins over the scan
        with support.env(WAYFIRE_SOCKET=rig.sock):
            self.assertEqual(session.find_wayfire_socket(), rig.sock)

    def test_wayfire_answers_the_ipc_methods_the_backend_gates_on(self):
        """`plugins = ... ipc ipc-rules stipc` is what turns `window-rules/list-views` on; the api gate exists
        because a Wayfire without ipc-rules has a socket that answers `No such method found!` to it."""
        rig = self.boot(support.HeadlessWayfire)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(5.0)
        c.connect(rig.sock)
        body = json.dumps({"method": "list-methods", "data": {}}).encode()
        c.sendall(struct.pack("<i", len(body)) + body)
        (n,) = struct.unpack("<i", c.recv(4))
        out = b""
        while len(out) < n:
            out += c.recv(n - len(out))
        methods = json.loads(out.decode())["methods"]
        self.assertIn("window-rules/list-views", methods)
        self.assertIn("window-rules/list-outputs", methods)

    def test_i3_writes_its_socket_where_the_finder_now_looks(self):
        """The regression this whole finder change is about: i3 writes `$XDG_RUNTIME_DIR/i3/ipc-socket.<pid>`
        -- a subdirectory -- and `find_sway_socket()` answered None without `$I3SOCK`, which i3 puts into the
        processes it spawns and not into its own environ [M recon2/i3.md §1]."""
        rig = self.boot(support.HeadlessI3)
        self.assertEqual(os.path.dirname(rig.sock), os.path.join(rig.rtdir, "i3"))
        self.assertTrue(os.path.basename(rig.sock).startswith("ipc-socket."))
        self.finders(rig)
        self.assertEqual(session.find_sway_socket(), rig.sock)

    def test_i3_is_the_i3_dialect_the_double_replays(self):
        """The double claims i3 answers GET_VERSION with major 4 and GET_OUTPUTS with a pseudo-output and no
        modes. Here that claim is put to the real i3 that is installed."""
        rig = self.boot(support.HeadlessI3)
        c = SwayIPC(sockpath=rig.sock)
        self.addCleanup(c.close)
        version = c.msg(support.GET_VERSION)
        self.assertEqual(version["major"], 4)
        outs = c.get_outputs()
        self.assertTrue(any(not o["active"] for o in outs),
                        "no inactive pseudo-output in this i3's GET_OUTPUTS")
        for o in outs:
            self.assertNotIn("modes", o)
            self.assertNotIn("current_mode", o)
        with self.assertRaises(Fatal):
            c.run("output %s position 0 0" % outs[-1]["name"])

    def test_openbox_comes_up_on_its_own_x_server(self):
        rig = self.boot(support.HeadlessOpenbox)
        self.assertTrue(os.path.exists("/tmp/.X11-unix/X%d" % rig.num))
        self.assertEqual(rig.env["DISPLAY"], rig.display)
        self.assertIsNone(rig.wm.poll(), "openbox exited")

    def test_river_needs_a_window_manager_and_says_so(self):
        """river 0.4 has no layout of its own: without something on the other end of
        `river_window_manager_v1` nothing is ever mapped. Neither binary is packaged anywhere, so this skips
        everywhere except a box somebody built them on."""
        rig = self.boot(support.HeadlessRiver)
        self.assertTrue(os.path.exists(rig.wayland_socket))

    def test_the_helpers_leave_nothing_behind(self):
        rig = support.HeadlessLabwc()
        rtdir, proc = rig.rtdir, rig.proc
        rig.stop()
        self.assertIsNotNone(proc.poll(), "the compositor is still running")
        self.assertFalse(os.path.exists(rtdir))


if __name__ == "__main__":
    unittest.main()
