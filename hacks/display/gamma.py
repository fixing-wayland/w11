"""--brightness/--gamma via zwlr_gamma_control_manager_v1.

The Wayland gamma control dies with its client connection, so a non-identity
brightness/gamma forks a tiny detached holder process per output (the wdotool
daemon double-fork, simplified) that acquires the gamma control, submits the
ramp, and then just keeps the connection alive. Changing brightness kills and
replaces the holder; identity (brightness 1, gamma 1:1:1) just kills it — the
compositor restores the neutral ramp when the control's client disconnects.

Ramp math is xrandr's set_gamma() (xrandr.c:1419):
    ramp[i] = min((i/(size-1))^(1/gamma) * brightness, 1.0) * 65535
with the linear shortcut when gamma == 1 and brightness == 1.

Holder liveness is tracked in the wxrandr state file as (pid, starttime) —
starttime from /proc/pid/stat guards against pid reuse. The detaching, the
status pipe and the /proc identity checks are w11common.procs, which is where
that protocol lives for the whole tree."""

import os
import signal
import struct
import sys
import time

from w11common import procs

MANAGER = "zwlr_gamma_control_manager_v1"

#: A holder whose starttime was never captured (a '?' record) is identified
#: by name instead: it is a fork of the running command, and dist/wxrandr is
#: a zipapp under `env python3`. Loose, but it is only ever reached for a
#: record that would otherwise strand a holder holding the gamma control.
HOLDER_COMM = ("python",)

#: How long a `--brightness` blocks waiting for the holder's verdict.
HOLDER_SECONDS = 10.0


def compute_ramp(size: int, brightness: float, gamma_rgb) -> bytes:
    """The three channel ramps, red then green then blue, native-endian u16
    (the zwlr_gamma_control fd format)."""
    out = bytearray()
    for g in gamma_rgb:
        shift = 1.0 / g if g else 1.0
        for i in range(size):
            frac = i / (size - 1) if size > 1 else 0.0
            if g == 1.0 and brightness == 1.0:
                v = frac
            else:
                # clamp BOTH ends: xrandr accepts a negative --brightness and applies the (black) ramp, exit 0 —
                # without the low clamp struct.pack("=H") would raise on a negative value.
                v = min(max(pow(frac, shift) * brightness, 0.0), 1.0)
            out += struct.pack("=H", int(v * 65535.0))
    return bytes(out)


def stop_holder(state, output: str) -> bool:
    """Kill the recorded holder for `output` (verifying ownership and starttime, so neither a recycled pid nor
    somebody else's process is ever signalled). Returns True if one was running."""
    rec = state.gamma().pop(output, None)
    if not rec or not rec.get("pid"):
        return False
    start = rec.get("start")
    # a record with no starttime field at all is not a '?': it says nothing
    # that can be checked, and has never been something we signal
    if start is None or not procs.alive(rec["pid"], start, HOLDER_COMM):
        return False
    return procs.kill_bounded(rec["pid"], start)


def set_output_gamma(state, output: str, brightness: float, gamma_rgb,
                     wayland_socket: str | None = None) -> str | None:
    """Kill any existing holder, then (unless identity) spawn a fresh one. Returns None on success, else an
    error string ("refused" when the compositor rejected the gamma control — headless outputs have no LUT)."""
    had_holder = stop_holder(state, output)
    identity = (brightness == 1.0 and tuple(gamma_rgb) == (1.0, 1.0, 1.0))
    if identity:
        return None
    err = _spawn_holder(state, output, brightness, gamma_rgb, wayland_socket)
    if err == "refused" and had_holder:
        # a replaced holder's socket-close may not have reached the compositor before the new control asked for
        # the LUT (two independent client fds, no ordering): give it a moment and try once more.
        time.sleep(0.2)
        err = _spawn_holder(state, output, brightness, gamma_rgb, wayland_socket)
    return err


def _spawn_holder(state, output: str, brightness: float, gamma_rgb, wayland_socket: str | None) -> str | None:
    named = {}

    def record():
        state.gamma()[output] = dict(named, brightness=brightness, gamma=list(gamma_rgb))

    def on_line(line: str) -> bool:
        if not line.startswith("pid "):
            return False
        # the grandchild reports its identity BEFORE acquiring the control, and this runs the moment the line
        # arrives, so even a later failure/timeout leaves a record a future wxrandr can stop — no unstoppable
        # orphan.
        parts = line.split()
        if len(parts) == 3:
            named.update(pid=int(parts[1]), start=parts[2])
            record()
        return True

    def child(status_fd):
        holder_main(output, brightness, gamma_rgb, status_fd=status_fd, wayland_socket=wayland_socket)

    status = procs.spawn_detached(child, HOLDER_SECONDS, on_line)
    if status == "ok":
        return None                       # early record already stands
    if status is not None:                # explicit failure: holder is exiting
        state.gamma().pop(output, None)
        if status.startswith("failed "):
            return status[len("failed "):]
        return "gamma holder did not report status"
    # no terminal status within the deadline: the grandchild may still be
    # alive holding the control — keep the early record so it stays stoppable.
    if named:
        record()
    return "gamma holder did not report status"


def holder_main(output: str, brightness: float, gamma_rgb,
                status_fd: int | None = None,
                wayland_socket: str | None = None):
    """The detached holder: acquire the output's gamma control, submit the ramp, report over status_fd, then
    keep the connection alive forever. Exits when the compositor closes the connection or on SIGTERM."""

    def emit(msg: str, close: bool = False):
        procs.emit(status_fd, msg, close)

    def report(msg: str):  # terminal status (closes the pipe)
        emit(msg, close=True)

    # tell the parent who we are before touching the control, so a failure or timeout past this point still
    # leaves a stoppable record (finding: no unstoppable orphan holder)
    emit("pid %d %s" % (os.getpid(), procs.proc_starttime(os.getpid()) or "?"))

    try:
        from w11common import session
        from w11common.wayland_mini import WlConn
        if wayland_socket is None:
            hit = session.find_wayland_socket()
            if hit is None:
                report("failed no wayland socket")
                return 1
            wayland_socket = hit[2]
        conn = WlConn(wayland_socket)
        reg = conn.get_registry()
        mgr = conn.find_global(MANAGER)
        if mgr is None:
            report("failed compositor does not advertise %s" % MANAGER)
            return 1
        target = None
        pending = []
        for name, (iface, ver) in sorted(reg.items()):
            if iface != "wl_output":
                continue
            oid = conn.bind(name, "wl_output", min(ver, 4))
            info = {"oid": oid, "name": None}

            def h(op, cur, fds, info=info):
                if op == 4:  # name event (wl_output v4)
                    info["name"] = cur.string()

            conn.on(oid, h)
            pending.append(info)
        conn.roundtrip()
        for info in pending:
            if info["name"] == output:
                target = info["oid"]
        if target is None:
            report("failed no wl_output named %s" % output)
            return 1
        mid = conn.bind(mgr[0], MANAGER, 1)
        gid = conn.alloc()
        st = {"size": None, "failed": False}

        def gh(op, cur, fds):
            if op == 0:
                st["size"] = cur.u32()
            elif op == 1:
                st["failed"] = True

        conn.on(gid, gh)
        conn.send(mid, 0, [("u", gid), ("u", target)])  # get_gamma_control
        conn.roundtrip()
        # a real LUT is 256-4096 entries; reject a bogus/hostile size before
        # compute_ramp turns it into a multi-GB allocation.
        if st["failed"] or not st["size"] or st["size"] > 65536:
            report("failed refused")
            return 1

        def submit():
            ramp = compute_ramp(st["size"], brightness, gamma_rgb)
            fd = os.memfd_create("wxrandr-gamma")
            try:
                os.write(fd, ramp)
                os.lseek(fd, 0, os.SEEK_SET)
                conn.send_fds(gid, 0, [], [fd])  # set_gamma(fd)
            finally:
                os.close(fd)

        submit()
        conn.roundtrip()
        if st["failed"]:
            report("failed refused")
            return 1
        report("ok")  # identity (pid/start) was sent up front
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        while True:  # a compositor may resend gamma_size; resubmit then
            before = st["size"]
            conn.dispatch(timeout=60.0)
            if st["failed"]:
                return 0
            if st["size"] != before:
                submit()
    except Exception as e:
        report("failed %s" % e)
        return 1
