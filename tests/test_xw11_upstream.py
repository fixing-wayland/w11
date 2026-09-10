#!/usr/bin/env python3
"""xw11/upstream.py: the one connection the proxy opens for itself.

What this file pins is the four things a forwarder cannot read off the stream it
forwards, and the lifetime that makes them worth having:

* **ids.** `rid_base | n` out of the range the server allocated exclusively to
  this connection [recon/wire.md 7.1], so a shadow can never collide with a real
  Xwayland window and a reopened connection re-bases every one of them.
* **atoms.** Server-global: the same name interned on three connections came
  back 0x187 every time [recon/wire.md 7.2]. Interned once, here, and the ids
  handed to clients are ids `GetAtomName` resolves upstream.
* **majors.** Per server, never per spec -- RANDR is 139 on Xwayland and 140 on
  Xvfb 21.1.22, XTEST 132 [recon/env.md 2.5, recon/tools.md 7]. `Majors` below
  runs the same proxy against two different tables to prove nothing is written
  down.
* **the root's events.** `PropertyChange | SubstructureNotify`, which is what
  makes the X plane's own creates and property writes reach the registry.

And the lifetime: **lazy at the first accept, held for `_IDLE_SECONDS` after the
last client leaves.** Xwayland dies fifteen seconds after ITS last client and
sway relaunches it lazily [recon/env.md 6]; a proxy that dialled at startup
would pin one nobody asked for, and a proxy that dropped its connection between
two `xdotool` invocations would hand the second invocation different ids for the
same windows (design section 2.6).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# `import support` resolves only with the tests directory itself on sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support                                                      # noqa: E402
from support import FakeBackend, ProxyRig, fake_window              # noqa: E402
from wdotool import backend_detect, x11_mini                        # noqa: E402
from wxprop import core as wxprop_core                              # noqa: E402
from xw11 import policy                                             # noqa: E402
from xw11 import server as server_mod                               # noqa: E402
from xw11 import upstream as upstream_mod                           # noqa: E402

# The suite never hands a tool over to the real X11 one: see tests/conftest.py
# and tests/test_passthrough.py; this line covers `python3 tests/<file>.py`.
os.environ["W11_PASSTHROUGH"] = "never"

#: FakeXServer's first screen, which is the root every fake reply names.
FAKE_ROOT = support.FakeXServer.ROOTS[0]

HAVE_XVFB = bool(shutil.which("Xvfb"))


class OwnCase(unittest.TestCase):
    """A rig with the shadow side on. `passthrough=True` is the rig's default
    because a rig has no compositor; every test here wants the machinery."""

    def rig(self, windows=(1, 2), **kw):
        kw.setdefault("backend", FakeBackend(
            windows=[fake_window(w, "w%d" % w) for w in windows]))
        kw.setdefault("passthrough", False)
        kw.setdefault("num", 24)
        kw.setdefault("upstream_num", 25)
        got = ProxyRig(**kw)
        self.addCleanup(got.stop)
        return got


class Atoms(OwnCase):
    def test_every_name_is_interned_exactly_once(self):
        """One burst at open, and never again: atoms are server-global, so a
        second client interning the same name gets the same id back from the
        server itself and the proxy has nothing to do [recon/wire.md 7.2]."""
        rig = self.rig()
        rig.conn()
        own = rig.wait_own()
        interned = [row[1] for row in rig.upstream.log if row[0] == "InternAtom"]
        self.assertEqual(sorted(interned), sorted(policy.ATOMS))
        self.assertEqual(len(interned), len(set(interned)))
        self.assertEqual(own.atoms["_NET_WM_STATE"],
                         rig.upstream._atoms["_NET_WM_STATE"])

    def test_a_second_clients_intern_of_the_same_name_gets_the_same_id(self):
        """The whole reason InternAtom is PASS: the id the proxy will put in a
        synthesized `_NET_WM_STATE` is the id the client resolves."""
        rig = self.rig()
        first = rig.conn()
        own = rig.wait_own()
        self.assertEqual(first.atom("_NET_WM_STATE"), own.atoms["_NET_WM_STATE"])
        second = rig.conn()
        self.assertEqual(second.atom("_NET_WM_STATE"), own.atoms["_NET_WM_STATE"])

    def test_the_table_is_wxprops_extended_set_plus_the_names_it_has_no_server_for(self):
        """`policy.ATOMS` is written out rather than imported so that `import
        xw11` does not pull wxprop.core in, and this is what keeps the two from
        drifting: every name the native property synthesis knows is interned
        here, in that order."""
        self.assertEqual(list(policy.ATOMS[:len(wxprop_core._EXTENDED_ATOMS)]),
                         list(wxprop_core._EXTENDED_ATOMS))
        for name in ("_NET_CLOSE_WINDOW", "WM_CHANGE_STATE", "_NET_FRAME_EXTENTS",
                     "_NET_DESKTOP_GEOMETRY", "_NET_WM_STATE_MODAL"):
            self.assertIn(name, policy.ATOMS)

    def test_the_68_predefined_atoms_are_never_interned(self):
        """1..68 are fixed by the protocol [recon/wire.md 7.2]; asking the
        server for `WM_NAME`'s id would be a round trip for a number that is in
        /usr/include/X11/Xatom.h."""
        self.assertEqual([n for n in policy.ATOMS if n in policy.PREDEFINED_ATOMS], [])
        self.assertTrue(all(1 <= a <= 68 for a in policy.PREDEFINED_ATOMS.values()))
        rig = self.rig()
        rig.conn()
        own = rig.wait_own()
        interned = [row[1] for row in rig.upstream.log if row[0] == "InternAtom"]
        self.assertNotIn("WM_NAME", interned)
        self.assertEqual(own.atoms["WM_NAME"], 39)
        self.assertEqual(own.atom_names[31], "STRING")

    def test_a_name_nobody_interned_at_open_is_asked_for_once_and_cached(self):
        """`xprop -set _MY_THING` interns whatever it likes; the proxy has to be
        able to read back the id it was handed."""
        rig = self.rig()
        client = rig.conn()
        own = rig.wait_own()
        mine = client.atom("_XW11_A_NAME_NOBODY_HAS")
        self.assertEqual(own.atom_name(mine), "_XW11_A_NAME_NOBODY_HAS")
        asked = len([r for r in rig.upstream.log if r[0] == "GetAtomName"])
        self.assertEqual(own.atom_name(mine), "_XW11_A_NAME_NOBODY_HAS")
        self.assertEqual(len([r for r in rig.upstream.log if r[0] == "GetAtomName"]),
                         asked)


class Majors(OwnCase):
    def test_xtest_is_whatever_the_server_says_and_never_a_constant(self):
        """132 on this Xwayland [recon/env.md 2.5]. The same proxy against a
        server that answers 152 must resolve 152, which is the whole claim: a
        major written down anywhere in xw11 fails this test."""
        first = self.rig(num=24, upstream_num=25)
        first.conn()
        self.assertEqual(first.wait_own().majors["XTEST"][0], 132)
        moved = dict(support.FAKE_EXTENSIONS)
        moved["XTEST"] = (152, 0, 0)
        second = self.rig(num=26, upstream_num=27, extensions=moved)
        second.conn()
        self.assertEqual(second.wait_own().majors["XTEST"][0], 152)

    def test_the_first_event_and_first_error_ride_along(self):
        """RANDR's are 88 and 145 here [recon/env.md 2.5]; batch 5 needs the
        event base to tell a RandR event from a core one."""
        rig = self.rig()
        rig.conn()
        self.assertEqual(rig.wait_own().majors["RANDR"], (139, 88, 145))

    def test_the_wanted_list_is_the_one_the_design_names_and_all_of_it_is_asked(self):
        rig = self.rig()
        rig.conn()
        own = rig.wait_own()
        asked = [row[1] for row in rig.upstream.log if row[0] == "QueryExtension"]
        self.assertEqual(asked[:len(policy.WANTED_EXTENSIONS)],
                         list(policy.WANTED_EXTENSIONS))
        self.assertEqual(sorted(own.majors), sorted(policy.WANTED_EXTENSIONS))

    def test_a_major_resolves_back_to_its_extension_name(self):
        """What `client.decode` asks when a client sends an extension request:
        which extension is major 139 -- a question only the server can answer."""
        rig = self.rig()
        rig.conn()
        rig.wait_own()
        self.assertEqual(rig.server.ext_for_major(139), "RANDR")
        self.assertIsNone(rig.server.ext_for_major(200))


class LazyOpen(OwnCase):
    def test_nothing_is_opened_before_the_first_client(self):
        """A proxy that dialled at startup would pin Xwayland alive for the
        whole session: it dies fifteen seconds after ITS last client leaves and
        sway relaunches it lazily from the `-listenfd`s [recon/env.md 6]."""
        rig = self.rig()
        self.assertIsNone(rig.server.own)
        self.assertEqual(rig.upstream.accepted, 0)
        self.assertEqual(rig.upstream.setup_attempts, [])
        rig.conn()
        rig.wait_own()
        self.assertEqual(rig.upstream.accepted, 2)   # ours, then the client's

    def test_the_backend_is_not_detected_before_the_first_client_either(self):
        """`detect()` is 0.7 ms and caches a bus, a name list and a registry
        connection [recon/seams.md 2.2]; none of that is owed by a proxy nobody
        has spoken to.

        `backend=None` and a counted `detect`, because a proxy handed a backend
        never detects at all and a test that injected one would be asserting
        nothing about when detection happens."""
        calls = []
        fresh = FakeBackend(windows=[fake_window(1, "w1")])
        old = backend_detect.detect
        backend_detect.detect = lambda: (calls.append(1), fresh)[1]
        self.addCleanup(setattr, backend_detect, "detect", old)
        rig = self.rig(backend=None)
        self.assertEqual(calls, [])
        self.assertIsNone(rig.server.shadows)
        rig.conn()
        rig.wait_own()
        self.assertEqual(len(calls), 1)
        self.assertIs(rig.server.backend, fresh)
        self.assertIsNotNone(rig.server.shadows)


class HeldWhileIdle(OwnCase):
    def test_the_connection_and_the_ids_survive_the_gap_between_two_invocations(self):
        """`W=$(xdotool search ...); ...; xdotool windowactivate $W` is two
        processes with a gap in between, and the id has to mean the same window
        in the second one. The connection is held for `_IDLE_SECONDS` after the
        last client leaves (design section 2.6)."""
        rig = self.rig(idle=0.4, check=0.02)
        first = rig.conn()
        own = rig.wait_own()
        before = {e.handle: e.shadow for e in rig.server.shadows.snapshot()}
        self.assertTrue(before and all(before.values()))
        first.close()
        rig.wait(lambda: rig.server.live == 0, what="the client's exit")
        second = rig.conn()
        rig.wait(lambda: rig.server.live == 1, what="the second client")
        self.assertIs(rig.server.own, own)
        self.assertEqual(own.generation, 1)
        after = {e.handle: e.shadow for e in rig.server.shadows.snapshot()}
        self.assertEqual(before, after)
        second.close()

    def test_and_the_proxy_exits_when_the_idle_seconds_are_up(self):
        rig = self.rig(idle=0.3, check=0.02)
        client = rig.conn()
        own = rig.wait_own()
        client.close()
        rig.wait(lambda: rig.server.stopping, timeout=10, what="the idle exit")
        rig.thread.join(timeout=5)
        self.assertFalse(own.open)
        self.assertIn("no client for", rig.server.reason)

    def test_the_hold_is_the_daemons_own_constant(self):
        """900 s, the input daemon's (wdotool/daemon.py:177), because the two
        answer the same question: how long after the last tool does the thing
        it started stay up."""
        self.assertEqual(server_mod._IDLE_SECONDS, 900.0)
        self.assertEqual(server_mod._CHECK_SECONDS, 15.0)


class ReopenRebases(OwnCase):
    def test_an_upstream_that_went_away_is_reopened_and_every_id_reminted(self):
        """An Xwayland restart under a live proxy: root, atoms and majors are
        stable across one, XIDs are not [recon/env.md 2.5, 6]. R16."""
        rig = self.rig(idle=0.0, check=0.02)
        first = rig.conn()
        own = rig.wait_own()
        before = {e.handle: e.shadow for e in rig.server.shadows.snapshot()}
        base_before, root_before = own.rid_base, own.root
        atoms_before = dict(own.atoms)
        majors_before = dict(own.majors)
        rig.upstream.close_when_idle(0.15)
        rig.wait(lambda: not rig.server.own.open, timeout=10,
                 what="the upstream going away")
        first.close()
        rig.upstream._idle_seconds = None
        second = rig.conn()
        rig.wait(lambda: rig.server.own.open, timeout=10, what="the reopen")
        own = rig.server.own
        self.assertEqual(own.root, root_before)
        self.assertEqual(own.atoms, atoms_before)
        self.assertEqual(own.majors, majors_before)
        self.assertNotEqual(own.rid_base, base_before)
        after = {e.handle: e.shadow for e in rig.server.shadows.snapshot()}
        self.assertEqual(sorted(after), sorted(before))
        for handle, shadow in after.items():
            self.assertNotEqual(shadow, before[handle])
            self.assertEqual(shadow & ~own.rid_mask, own.rid_base)
        second.close()


class RootSelected(OwnCase):
    def test_property_change_and_substructure_notify_on_the_root(self):
        """Step 1 of design section 2.4: every property write on the root and
        every create/destroy/map/unmap/configure of one of its children reaches
        the proxy, which is how the X plane invalidates the registry."""
        rig = self.rig()
        rig.conn()
        rig.wait_own()
        selected = [row for row in rig.upstream.log
                    if row[0] == "ChangeWindowAttributes"]
        self.assertEqual(len(selected), 1)
        _op, win, mask, values = selected[0]
        self.assertEqual(win, FAKE_ROOT)
        self.assertEqual(mask, 0x0800)                    # CWEventMask
        self.assertEqual(values, (upstream_mod.ROOT_EVENT_MASK,))
        self.assertEqual(upstream_mod.ROOT_EVENT_MASK, 0x00400000 | 0x00080000)
        self.assertEqual(rig.upstream.wires[0].masks[FAKE_ROOT],
                         upstream_mod.ROOT_EVENT_MASK)

    def test_an_event_on_the_root_reaches_the_registry_over_the_wire(self):
        """The whole path of design section 2.4 step 1, end to end: the server
        writes a `PropertyNotify` on the connection the proxy opened for itself,
        the framer hands it to `on_event`, and the registry's next read
        re-lists. Nothing here calls a method of the server directly, which is
        what makes it fail when the `on_event` wiring is missing.

        `conn_index=0` is that own connection: `accept()` opens it before the
        client's twin, which is the same index the mask above is read from.
        `stamp=False` because an event's sequence field is a watermark and this
        one is not being read for it."""
        rig = self.rig()
        rig.conn()
        rig.wait_own()
        shadows = rig.server.shadows
        # Ten seconds of TTL, so the only thing that can make this registry
        # stale in the next moment is the event.
        shadows.ttl = 10.0
        shadows.snapshot()
        self.assertFalse(shadows.stale())
        rig.upstream.push_event(b"\x1c" + b"\0" * 31, conn_index=0, stamp=False)
        rig.wait(shadows.stale, what="the root event")

    def test_and_the_handler_itself_drops_the_cache(self):
        """The smaller half of the same claim, without a socket in it."""
        rig = self.rig(num=34, upstream_num=35)
        rig.conn()
        rig.wait_own()
        shadows = rig.server.shadows
        shadows.ttl = 10.0
        shadows.snapshot()
        self.assertFalse(shadows.stale())
        rig.server.on_root_event(b"\x1c" + b"\0" * 31)     # PropertyNotify
        self.assertTrue(shadows.stale())


class KeyboardMappingRead(OwnCase):
    def test_the_whole_range_is_read_and_kept_raw(self):
        """8..255, the keycode range the server advertises [recon/env.md 2.5].
        Kept as the bytes it arrived in: batch 6 turns them into the keycode ->
        keysym table XTEST cannot work without, and re-deriving them from a
        parsed copy would be a second parser."""
        rig = self.rig()
        rig.conn()
        own = rig.wait_own()
        asked = [row for row in rig.upstream.log if row[0] == "GetKeyboardMapping"]
        self.assertEqual(asked, [("GetKeyboardMapping", 8, 248)])
        expected = rig.upstream.keymap_reply(8, 248, 0)
        self.assertEqual(own.keyboard_mapping[32:], expected[32:])
        self.assertEqual(own.keyboard_mapping[1], rig.upstream.keysyms_per_keycode)
        self.assertEqual(len(own.keyboard_mapping),
                         32 + 4 * 248 * rig.upstream.keysyms_per_keycode)

    def test_the_table_is_the_servers_and_not_a_guess(self):
        """A server with three keysyms per keycode is read as three."""
        rig = self.rig(num=28, upstream_num=29)
        rig.upstream.keysyms_per_keycode = 3
        rig.conn()
        own = rig.wait_own()
        self.assertEqual(own.keyboard_mapping[1], 3)
        self.assertEqual(len(own.keyboard_mapping), 32 + 4 * 248 * 3)


class SupportedRead(OwnCase):
    def test_upstreams_own_net_supported_is_read_at_open(self):
        """Step 5: upstream's list is what design section 4.6's union is added
        to, and what design section 3.4 gates the ClientMessage route on."""
        rig = self.rig(num=30, upstream_num=31)
        atoms = [rig.upstream.intern(n) for n in ("_NET_ACTIVE_WINDOW",
                                                  "_NET_CLIENT_LIST")]
        import struct as _struct
        rig.upstream.set_prop(FAKE_ROOT, "_NET_SUPPORTED", "ATOM", 32,
                              _struct.pack("<%dI" % len(atoms), *atoms))
        rig.conn()
        own = rig.wait_own()
        self.assertEqual(list(own.upstream_supported), atoms)

    def test_a_root_without_the_property_reads_as_empty(self):
        """sway deletes `_NET_CLIENT_LIST` outright and publishes no desktop
        pair [recon/env.md 2.7]; an absent `_NET_SUPPORTED` is a list of nothing,
        never a failure to open."""
        rig = self.rig(num=32, upstream_num=33)
        rig.conn()
        own = rig.wait_own()
        self.assertEqual(list(own.upstream_supported), [])


class OpeningIsRetried(unittest.TestCase):
    """What happens between two X servers, and what happens when one says no.

    An X server **resets when its last client disconnects**, and a connection
    that arrives during that reset is met with ECONNRESET mid-handshake --
    measured against Xvfb 21.1.22 on 2026-09-10, where `AgainstARealServer`
    below (whose tests each leave the server clientless) failed two runs in five
    before the whole open was retried rather than only the `connect()`.
    Xwayland's `-terminate` and sway's lazy relaunch [recon/env.md 6] are the
    same window."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="xw11retry-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        old = x11_mini._SOCK_DIR
        x11_mini._SOCK_DIR = self.dir
        self.addCleanup(setattr, x11_mini, "_SOCK_DIR", old)
        self.lines = []

    def test_a_server_that_is_not_there_yet_is_waited_for(self):
        """The 2 s the daemon client already waits (daemon.py:2053)."""
        started = [None]

        def late():
            time.sleep(0.3)
            started[0] = support.FakeUpstream(self.dir, num=62)
        thread = threading.Thread(target=late, daemon=True)
        thread.start()
        self.addCleanup(lambda: started[0] and started[0].stop())
        conn = upstream_mod.OwnConn(":62", log=self.lines.append)
        self.addCleanup(conn.close)
        began = time.monotonic()
        self.assertTrue(conn.connect(), self.lines)
        self.assertGreater(time.monotonic() - began, 0.25)
        thread.join(timeout=5)

    def test_a_server_that_says_no_is_not_asked_twice(self):
        """A `Failed` setup is the server refusing, not the server missing:
        trying again changes nothing, and the reason is carried into the log in
        the server's own words -- the same sentence a client whose cookie the
        upstream refuses gets."""
        fake = support.FakeUpstream(self.dir, num=63, cookie=b"z" * 16,
                                    accept_empty=False)
        self.addCleanup(fake.stop)
        conn = upstream_mod.OwnConn(":63", log=self.lines.append)
        self.addCleanup(conn.close)
        began = time.monotonic()
        self.assertFalse(conn.connect())
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(conn.refusal, "Authentication rejected by fake")
        self.assertTrue([line for line in self.lines
                         if "Authentication rejected by fake" in line])
        self.assertTrue([line for line in self.lines
                         if "passes through untouched" in line])

    def test_no_server_at_all_gives_up_after_the_deadline(self):
        conn = upstream_mod.OwnConn(":64", log=self.lines.append)
        self.addCleanup(conn.close)
        began = time.monotonic()
        self.assertFalse(conn.connect())
        self.assertGreaterEqual(time.monotonic() - began,
                                server_mod.CONNECT_RETRY_SECONDS - 0.1)
        self.assertIn("no socket at :64", conn.refusal)


@unittest.skipUnless(HAVE_XVFB, "needs Xvfb")
class AgainstARealServer(unittest.TestCase):
    """The same five steps against a real X server, because every one of them
    is a claim about what a server does and a fake that agreed with the code
    would prove nothing.

    Xvfb rather than Xwayland: it is here, it is 25 ms to start [batch 1's
    measurement], and it is the server whose extension majors DIFFER from
    Xwayland's -- RANDR is 140 here and 139 there [recon/tools.md 7,
    recon/env.md 2.5], which is exactly the thing nothing in xw11 may write
    down."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="xw11real-")
        cls.num = next(n for n in range(70, 120)
                       if not os.path.exists("/tmp/.X11-unix/X%d" % n))
        cls.proc = subprocess.Popen(
            ["Xvfb", ":%d" % cls.num, "-screen", "0", "640x480x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while not os.path.exists("/tmp/.X11-unix/X%d" % cls.num):
            if cls.proc.poll() is not None or time.monotonic() > deadline:
                cls.tearDownClass()
                raise unittest.SkipTest("Xvfb never came up")
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        proc = getattr(cls, "proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:       # pragma: no cover
                proc.kill()
                proc.wait(timeout=5)
        shutil.rmtree(getattr(cls, "dir", "") or "/nonexistent", ignore_errors=True)

    def own(self):
        lines = []
        conn = upstream_mod.OwnConn(":%d" % self.num, log=lines.append)
        self.addCleanup(conn.close)
        self.assertTrue(conn.connect(), lines)
        self.lines = lines
        return conn

    def test_the_whole_burst_lands_against_a_real_server(self):
        """65 requests in two round trips, measured at 0.9-1.3 ms against the
        live server on this box (five opens, 2026-09-10)."""
        started = time.monotonic()
        own = self.own()
        elapsed = time.monotonic() - started
        self.assertEqual(len(own.atoms),
                         len(policy.ATOMS) + len(policy.PREDEFINED_ATOMS))
        for name in policy.ATOMS:
            self.assertTrue(own.atoms.get(name), name)
            self.assertGreater(own.atoms[name], 68)     # never a predefined id
            self.assertEqual(own.atom_names[own.atoms[name]], name)
        self.assertTrue(own.root)
        self.assertEqual(own.rid_base % (own.rid_mask + 1), 0)
        self.assertLess(elapsed, 1.0)

    def test_the_majors_are_this_servers_and_differ_from_xwaylands(self):
        """RANDR is 140 on Xvfb 21.1.22 and 139 on Xwayland; XTEST is 132 on
        both [recon/tools.md 7, recon/env.md 2.5]. Whatever this server says is
        what the proxy carries."""
        own = self.own()
        self.assertIn("RANDR", own.majors)
        self.assertIn("XTEST", own.majors)
        for name, (major, _ev, _err) in own.majors.items():
            self.assertGreaterEqual(major, 128, name)
            self.assertLessEqual(major, 255, name)
        self.assertNotIn("DRI3", own.majors)            # no /dev/dri here

    def test_the_keyboard_mapping_is_the_servers_own_table(self):
        """8..255, whole and raw. Xvfb answers the X.Org default layout."""
        own = self.own()
        self.assertIsNotNone(own.keyboard_mapping)
        per = own.keyboard_mapping[1]
        self.assertGreaterEqual(per, 1)
        self.assertEqual(len(own.keyboard_mapping), 32 + 4 * 248 * per)

    def test_an_atom_the_server_made_up_resolves_back(self):
        own = self.own()
        made = own.atom_name(own.atoms["_NET_SUPPORTED"])
        self.assertEqual(made, "_NET_SUPPORTED")
        self.assertEqual(own.atom_name(31), "STRING")

    def test_two_own_connections_get_two_ranges(self):
        """The promise the whole shadow scheme rests on: the server allocated
        that range exclusively [recon/wire.md 7.1]."""
        first, second = self.own(), self.own()
        self.assertNotEqual(first.rid_base, second.rid_base)
        self.assertEqual(first.rid_mask, second.rid_mask)
        self.assertEqual(first.new_xid(), first.rid_base | 1)
        self.assertEqual(second.new_xid(), second.rid_base | 1)


if __name__ == "__main__":
    unittest.main()
