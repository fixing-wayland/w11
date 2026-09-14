"""wxrandr gamma holder tests against a mock Wayland compositor.

Headless sway refuses zwlr_gamma_control (no LUT on virtual outputs — the
first client gets `failed` immediately, verified live), so the success path
machinery is proven here against a wire-level mock that implements just
enough of wl_display/wl_registry/wl_output/zwlr_gamma_control_manager_v1:
the holder must acquire the control, ship the xrandr-formula ramp through
the fd, stay alive holding it, block a second acquirer, and die on demand
(restoring gamma = the compositor seeing the disconnect)."""

import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wl_fake import wstr
from w11common import procs
from hacks.display import core
from hacks.display import gamma as gammamod
from hacks.display.core import State

# The suite never hands a tool over to the real X11 one: see
# tests/conftest.py (which covers pytest) and tests/test_passthrough.py.
# This line is what covers `python3 tests/<file>.py`, where conftest is
# not loaded, and it reaches every subprocess a test spawns.
os.environ["W11_PASSTHROUGH"] = "never"

GAMMA_SIZE = 256


class MockCompositor(threading.Thread):
    """One-thread-per-connection mock speaking the Wayland wire protocol for
    the gamma path. Registry: wl_output v4 (name HEADLESS-1) + gamma manager.
    Only ONE gamma control may exist at a time; extras get `failed`."""

    def __init__(self, path: str):
        super().__init__(daemon=True)
        self.path = path
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        self.lock = threading.Lock()
        self.holder_conn = None      # connection object owning the control
        self.ramps = []              # every ramp received, in order
        self.acquires = 0
        self.refusals = 0
        self.clients = []            # every accepted connection, for close()
        self.stop = False
        self.gamma_size = GAMMA_SIZE  # advertised LUT size (override to test)

    def run(self):
        while not self.stop:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.clients.append(conn)
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def close(self):
        """Stop listening *and* drop every client, which is what a compositor
        exiting looks like from the other end: closing only the listening socket
        leaves an established connection established, and the gamma holder --
        whose whole job is to keep one -- would never notice."""
        self.stop = True
        try:
            self.srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.srv.close()
        for conn in self.clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    # -- wire helpers --------------------------------------------------------

    @staticmethod
    def _event(conn, obj_id, opcode, payload=b""):
        try:
            conn.sendall(struct.pack(
                "<II", obj_id, ((8 + len(payload)) << 16) | opcode) + payload)
        except OSError:
            pass

    def _serve(self, conn):
        buf = b""
        fds = []
        registry = None
        outputs = {}          # obj id -> name
        manager = set()
        controls = {}         # control id -> is_holder(bool)
        my_control = [None]
        try:
            while True:
                try:
                    data, anc, _fl, _ad = conn.recvmsg(65536, 4096)
                except OSError:
                    break
                if not data:
                    break
                for lvl, typ, fddata in anc:
                    if lvl == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                        n = len(fddata) // 4
                        fds.extend(struct.unpack(
                            "%di" % n, fddata[:n * 4]))
                buf += data
                while len(buf) >= 8:
                    obj_id, sizeop = struct.unpack_from("<II", buf)
                    size, opcode = sizeop >> 16, sizeop & 0xFFFF
                    if len(buf) < size:
                        break
                    payload = buf[8:size]
                    buf = buf[size:]
                    if obj_id == 1 and opcode == 0:      # sync(cb)
                        (cb,) = struct.unpack_from("<I", payload)
                        self._event(conn, cb, 0, struct.pack("<I", 1))
                    elif obj_id == 1 and opcode == 1:    # get_registry
                        (registry,) = struct.unpack_from("<I", payload)
                        self._event(conn, registry, 0,
                                    struct.pack("<I", 1)
                                    + wstr("wl_output")
                                    + struct.pack("<I", 4))
                        self._event(conn, registry, 0,
                                    struct.pack("<I", 2) + wstr(
                                        "zwlr_gamma_control_manager_v1")
                                    + struct.pack("<I", 1))
                    elif obj_id == registry and opcode == 0:  # bind
                        (gname,) = struct.unpack_from("<I", payload)
                        (slen,) = struct.unpack_from("<I", payload, 4)
                        rest = 8 + ((slen + 3) & ~3)
                        (new_id,) = struct.unpack_from("<I", payload,
                                                       rest + 4)
                        if gname == 1:
                            outputs[new_id] = "HEADLESS-1"
                            # wl_output v4 name event
                            self._event(conn, new_id, 4,
                                        wstr("HEADLESS-1"))
                        else:
                            manager.add(new_id)
                    elif obj_id in manager and opcode == 0:
                        cid, _out = struct.unpack_from("<II", payload)
                        with self.lock:
                            if self.holder_conn is None:
                                self.holder_conn = conn
                                my_control[0] = cid
                                controls[cid] = True
                                self.acquires += 1
                                self._event(conn, cid, 0,
                                            struct.pack("<I", self.gamma_size))
                            else:
                                self.refusals += 1
                                self._event(conn, cid, 1)  # failed
                    elif controls.get(obj_id) and opcode == 0:  # set_gamma
                        if fds:
                            fd = fds.pop(0)
                            os.lseek(fd, 0, os.SEEK_SET)
                            ramp = b""
                            while True:
                                chunk = os.read(fd, 65536)
                                if not chunk:
                                    break
                                ramp += chunk
                            os.close(fd)
                            with self.lock:
                                self.ramps.append(ramp)
        finally:
            with self.lock:
                if self.holder_conn is conn:
                    self.holder_conn = None  # control dies with the client
            conn.close()


def wait_for(pred, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class GammaHolderTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wxrandr-gamma-")
        self.sock = os.path.join(self.dir, "wayland-mock")
        self.mock = MockCompositor(self.sock)
        self.mock.start()
        self.state_path = os.path.join(self.dir, "state.json")
        self.state = State("mock", path=self.state_path)

    def tearDown(self):
        # no holder may outlive the test
        gammamod.stop_holder(self.state, "HEADLESS-1")
        self.mock.close()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def set_gamma(self, brightness, gam=(1.0, 1.0, 1.0), state=None):
        return gammamod.set_output_gamma(state or self.state, "HEADLESS-1",
                                         brightness, gam,
                                         wayland_socket=self.sock)

    def test_01_acquire_ramp_and_hold(self):
        err = self.set_gamma(0.5)
        self.assertIsNone(err)
        rec = self.state.gamma()["HEADLESS-1"]
        self.assertTrue(wait_for(lambda: self.mock.ramps))
        # the exact xrandr-formula bytes went over the fd
        self.assertEqual(self.mock.ramps[0],
                         gammamod.compute_ramp(GAMMA_SIZE, 0.5,
                                               (1.0, 1.0, 1.0)))
        # holder is a live detached process still holding the control
        self.assertTrue(os.path.exists("/proc/%d" % rec["pid"]))
        self.assertIsNotNone(self.mock.holder_conn)
        self.assertEqual(rec["brightness"], 0.5)

    def test_02_second_acquirer_is_refused(self):
        self.assertIsNone(self.set_gamma(0.5))
        self.assertTrue(wait_for(lambda: self.mock.acquires == 1))
        # a second client (fresh state that knows nothing of the holder)
        other = State("mock2", path=os.path.join(self.dir, "s2.json"))
        err = self.set_gamma(0.7, state=other)
        self.assertEqual(err, "refused")
        self.assertGreaterEqual(self.mock.refusals, 1)
        # the original holder still owns the control
        self.assertIsNotNone(self.mock.holder_conn)
        self.addCleanup(gammamod.stop_holder, other, "HEADLESS-1")

    def test_03_change_kills_and_replaces_holder(self):
        self.assertIsNone(self.set_gamma(0.5))
        pid1 = self.state.gamma()["HEADLESS-1"]["pid"]
        self.assertTrue(wait_for(lambda: len(self.mock.ramps) == 1))
        self.assertIsNone(self.set_gamma(0.25, (1.1, 1.0, 0.9)))
        pid2 = self.state.gamma()["HEADLESS-1"]["pid"]
        self.assertNotEqual(pid1, pid2)
        self.assertTrue(wait_for(lambda: len(self.mock.ramps) == 2))
        self.assertEqual(self.mock.ramps[1],
                         gammamod.compute_ramp(GAMMA_SIZE, 0.25,
                                               (1.1, 1.0, 0.9)))
        self.assertFalse(os.path.exists("/proc/%d/cwd" % pid1)
                         and _alive(pid1))

    def test_04_identity_kills_holder_restoring_gamma(self):
        self.assertIsNone(self.set_gamma(0.5))
        pid = self.state.gamma()["HEADLESS-1"]["pid"]
        self.assertIsNone(self.set_gamma(1.0, (1.0, 1.0, 1.0)))
        # record removed, process gone, compositor saw the disconnect
        self.assertNotIn("HEADLESS-1", self.state.gamma())
        self.assertTrue(wait_for(lambda: not _alive(pid)))
        self.assertTrue(wait_for(lambda: self.mock.holder_conn is None))
        # and the control is acquirable again
        self.assertIsNone(self.set_gamma(0.8))
        self.assertTrue(wait_for(lambda: self.mock.acquires >= 2))

    def test_05_identity_ramp_is_linear_shortcut(self):
        ramp = gammamod.compute_ramp(3, 1.0, (1.0, 1.0, 1.0))
        self.assertEqual(struct.unpack("=9H", ramp)[:3], (0, 32767, 65535))

    def test_06_stop_holder_ignores_recycled_pid(self):
        self.state.gamma()["HEADLESS-1"] = {
            "pid": os.getpid(), "start": "not-the-real-starttime",
            "brightness": 0.5, "gamma": [1, 1, 1]}
        # must NOT kill us (starttime mismatch), just drop the record
        self.assertFalse(gammamod.stop_holder(self.state, "HEADLESS-1"))
        self.assertNotIn("HEADLESS-1", self.state.gamma())

    def test_07_question_start_kills_by_name(self):
        # a record whose starttime was unavailable ('?') must still be
        # stoppable (never an unstoppable orphan) — kill-by-pid + name check
        import subprocess
        p = subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(60)"])
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.state.gamma()["HEADLESS-1"] = {
            "pid": p.pid, "start": "?", "brightness": 0.5, "gamma": [1, 1, 1]}
        self.assertTrue(gammamod.stop_holder(self.state, "HEADLESS-1"))
        p.wait(timeout=5)
        self.assertIsNotNone(p.returncode)  # actually killed

    def test_08_question_start_spares_non_holder(self):
        # a '?' record pointing at an unrelated (non-python) pid is left alone
        import subprocess
        p = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.state.gamma()["HEADLESS-1"] = {
            "pid": p.pid, "start": "?", "brightness": 0.5, "gamma": [1, 1, 1]}
        self.assertFalse(gammamod.stop_holder(self.state, "HEADLESS-1"))
        self.assertIsNone(p.poll())  # still alive
        p.kill()

    def test_09_implausible_lut_size_refused(self):
        # a bogus/hostile gamma_size must be rejected, not turned into a
        # multi-GB compute_ramp allocation
        self.mock.gamma_size = 10_000_000
        self.assertEqual(self.set_gamma(0.5), "refused")

    def test_10_failed_holder_leaves_no_record(self):
        # an explicit failure exits the holder; no orphan, so no record
        self.mock.gamma_size = 10_000_000
        self.set_gamma(0.5)
        self.assertNotIn("HEADLESS-1", self.state.gamma())


    def test_11_a_holder_whose_compositor_died_exits_by_itself(self):
        """The gamma control dies with the client connection, so a holder whose
        compositor has gone is holding nothing -- and a process holding nothing
        for ever is the orphan w11common.procs exists to prevent.  It ends itself:
        `holder_main`'s dispatch loop sees the connection close and returns,
        and `spawn_detached`'s grandchild `os._exit(0)`s after it.

        Measured on a live holder here: its open fds are 0/1/2 on /dev/null and
        two sockets, nothing else -- so nothing but the connection keeps it
        alive, and nothing but the connection has to end for it to go."""
        self.assertIsNone(self.set_gamma(0.5))
        pid = self.state.gamma()["HEADLESS-1"]["pid"]
        self.assertTrue(wait_for(lambda: self.mock.ramps))
        self.assertTrue(_alive(pid))
        self.mock.close()                    # the compositor exits
        self.assertTrue(wait_for(lambda: not _alive(pid), 8.0),
                        "holder %d outlived its compositor" % pid)

    def test_12_a_dead_holders_record_is_signalled_by_nothing(self):
        """What is left after test_11: a record naming a process that is gone.

        Two things follow from it and neither may guess.  `stop_holder` must
        signal nothing at all -- the pid is free to be reused, and killing
        whatever has it next is the one thing worse than leaving a holder --
        and `--verbose` must stop reporting the gamma that holder set, because
        the compositor restored the neutral ramp when the connection closed and
        0.50 would be a lie about the screen in front of the user."""
        self.assertIsNone(self.set_gamma(0.5))
        rec = dict(self.state.gamma()["HEADLESS-1"])
        pid = rec["pid"]
        self.mock.close()
        self.assertTrue(wait_for(lambda: not _alive(pid), 8.0))

        # the record is still there, and the render is judged on it
        out = core.OutputState(name="HEADLESS-1", active=True, ident=0x42)
        lines = core.render_verbose_block(out, self.state, 0)
        self.assertIn("\tGamma:      1.0:1.0:1.0", lines)
        self.assertIn("\tBrightness: 1.0", lines)
        # ...and with the holder alive it is the holder's numbers, so the line
        # above is a fallback and not a constant
        self.state.gamma()["HEADLESS-1"] = dict(rec, pid=os.getpid(),
                                                start=procs.proc_starttime(os.getpid()))
        live = core.render_verbose_block(out, self.state, 0)
        self.assertIn("\tBrightness: 0.50", live)

        self.state.gamma()["HEADLESS-1"] = rec
        killed = []
        with mock.patch.object(gammamod.procs.os, "kill",
                               lambda p, s: killed.append((p, s))):
            self.assertFalse(gammamod.stop_holder(self.state, "HEADLESS-1"))
        self.assertEqual(killed, [])
        self.assertNotIn("HEADLESS-1", self.state.gamma())


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False



class HolderOwnership(unittest.TestCase):
    def test_a_pid_owned_by_another_user_is_never_signalled(self):
        """Belt and braces behind the state file's ownership check: a gamma
        holder we started runs as us, so a record naming somebody else's
        process is not ours to SIGTERM -- least of all as root."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        st = State("k", path=os.path.join(d, "s.json"))
        st.gamma()["HDMI-1"] = {"pid": 1, "start": "?",   # pid 1 is root's
                                "brightness": 1.0, "gamma": [1, 1, 1]}
        killed = []
        with mock.patch.object(gammamod.os, "kill", lambda p, s: killed.append(p)):
            if os.geteuid() == 0:
                self.skipTest("as root every pid is 'ours'")
            self.assertFalse(gammamod.stop_holder(st, "HDMI-1"))
        self.assertEqual(killed, [])

if __name__ == "__main__":
    unittest.main(verbosity=2)
