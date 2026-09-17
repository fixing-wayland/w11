#!/usr/bin/env python3
"""What a real X server answers to a BIG-REQUESTS length it cannot honour.

The oracle for four of xw11's fixtures, and the reason this is a file in the
tree rather than a page of scratchpad: `tests/fixtures/xw11/badlength-nobigreq.hex`
names `scratchpad/b1/r3.py` in its comment, and a fixture nobody can re-cut is
a fixture nobody can re-measure -- not on a newer Xvfb, and not on the Xwayland
none of these numbers has been taken off yet.

  Xvfb :95 -ac -screen 0 640x480x24 &
  python3 scripts/xw11-probe-bigreq.py 95

It starts no server: the display number names one that is ALREADY RUNNING, and
the probes are destructive to a connection (one of them makes the server hang
up), so every probe opens its own.

Each probe is the same shape, and the sequence numbers below come out of it:
after the cookie-less setup, request 1 is `QueryExtension("BIG-REQUESTS")`,
request 2 is that extension's `Enable` -- whose reply carries the server's real
maximum-request-length in words at offset 8 -- and request 3 is the malformed
big form under test. Four NoOperations and a `GetInputFocus` follow it, so the
answer says how many bytes the server ate: a `GetInputFocus` reply naming seq 8
means the four NoOperations were framed, and no reply at all means the server
re-framed the stream from somewhere inside the bad request and is still waiting
for bytes that are not coming.

The output is one block per probe, headed by that probe's own letter (a to f):
the request as hex, then the server's whole answer as hex, or `closed` when the
server hung up with a zero-byte read, or `silent` when nothing came back before
the timeout. The fixtures under tests/fixtures/xw11/ are cut from exactly those
two lines, and each one's comment says which lettered block it came from.
"""
import socket
import struct
import sys
import time

#: The 12-byte cookie-less setup request: 'l', protocol 11.0, no auth.
SETUP = struct.pack("<BBHHHHH", 0x6C, 0, 11, 0, 0, 0, 0)
#: `GetInputFocus`, the one-word request every measurement in this tree syncs
#: with: it is what libX11's own `XSync` sends, and its reply names a sequence.
GET_INPUT_FOCUS = b"\x2b\x00\x01\x00"
#: `NoOperation`, one word. Four of them behind the bad request are how the
#: byte count is read off the sequence of the reply that follows.
NOOP = b"\x7f\x00\x01\x00"
#: The tail every probe sends behind its malformed request.
TAIL = NOOP * 4 + GET_INPUT_FOCUS
TIMEOUT = 2.0


def recvn(sock, n):
    """Exactly `n` bytes, or whatever arrived before the connection ended."""
    out = b""
    while len(out) < n:
        got = sock.recv(n - len(out))
        if not got:
            return out
        out += got
    return out


def connect(display, tries=6):
    """A fresh connection past its setup reply.

    The retry is measured, not defensive habit: the connection a probe leaves
    behind is one the server is still waiting for bytes of, and on Xvfb
    21.1.22 here (2026-09-16) the very next connect() on that display is
    answered with a reset about a third of the time -- the server reaps the
    dead client and the accept that raced it dies with it. A second connect a
    fifth of a second later has always succeeded. Nothing about the probes
    themselves depends on it; each one still runs on its own connection.
    """
    for attempt in range(tries):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        try:
            sock.connect("\0/tmp/.X11-unix/X%d" % display)
            sock.sendall(SETUP)
            head = recvn(sock, 8)
        except (ConnectionResetError, ConnectionRefusedError, BrokenPipeError):
            sock.close()
            if attempt == tries - 1:
                raise
            time.sleep(0.2)
            continue
        if len(head) < 8 or head[0] != 1:
            raise SystemExit("setup refused on :%d: %r" % (display, head))
        recvn(sock, struct.unpack_from("<H", head, 6)[0] * 4)
        return sock
    raise SystemExit("no connection to :%d" % display)


def enable_bigreq(sock):
    """Requests 1 and 2 of every probe. Returns the ceiling in WORDS.

    The major is read off the reply rather than written down, for the reason
    xw11/client.py gives: an extension's major is per server, and BIG-REQUESTS
    is 133 on the Xvfb here and need not be anywhere else.
    """
    name = b"BIG-REQUESTS"
    sock.sendall(struct.pack("<BBHH2x", 98, 0, 2 + len(name) // 4, len(name)) + name)
    reply = recvn(sock, 32)
    if len(reply) < 32 or reply[8] != 1:
        raise SystemExit("the server has no BIG-REQUESTS: %s" % reply.hex())
    major = reply[9]
    sock.sendall(struct.pack("<BBH", major, 0, 1))
    reply = recvn(sock, 32)
    if len(reply) < 32:
        raise SystemExit("no BigReqEnable reply: %s" % reply.hex())
    (ceiling,) = struct.unpack_from("<I", reply, 8)
    return ceiling


def probe(display, request):
    """One malformed request on a fresh connection, and everything that comes
    back before the timeout."""
    sock = connect(display)
    ceiling = enable_bigreq(sock)
    sock.sendall(request + TAIL)
    out = b""
    closed = False
    try:
        while True:
            got = sock.recv(65536)
            if not got:
                closed = True
                break
            out += got
    except socket.timeout:
        pass
    # `shutdown` before `close`, and not for tidiness: a bare `close()` on a
    # connection whose last request the server is still waiting for bytes of
    # made Xvfb 21.1.22 answer the NEXT connect() on the same display with a
    # reset -- measured here on 2026-09-16, one probe in three died that way
    # until the half-close went in. The probes after it are the same probes.
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()
    return ceiling, out, closed


def big(words):
    """A big-form `GetProperty` header claiming `words` 4-byte words."""
    return struct.pack("<BBHI", 20, 0, 0, words)


def main(argv):
    if len(argv) != 2 or not argv[1].lstrip(":").isdigit():
        raise SystemExit(__doc__.strip().splitlines()[0]
                         + "\nusage: xw11-probe-bigreq.py <display number of a RUNNING server>")
    display = int(argv[1].lstrip(":"))
    ceiling = enable_bigreq(connect(display))
    cases = [
        ("a", "claims 1 word -- less than its own 8-byte header", big(1)),
        ("b", "claims 0 words", big(0)),
        ("c", "claims 2 words -- the header and nothing else", big(2)),
        ("d", "claims the ceiling plus one (%d words)" % (ceiling + 1), big(ceiling + 1)),
        # One letter per case, and they are cited: the fixture comments under
        # tests/fixtures/xw11/ name the block each was cut from, so a re-cut on
        # a newer Xvfb -- or on the Xwayland none of this has been taken off yet
        # -- lines up block for block. (d) and (e) carried the same letter until
        # 2026-09-17, which made two blocks of one run's output head (d).
        ("e", "claims 0x0FFFFFFF words -- a gigabyte", big(0x0FFFFFFF)),
        ("f", "claims exactly the ceiling (%d words)" % ceiling, big(ceiling)),
    ]
    print("# display :%d, BigReqEnable ceiling %d words (%d bytes)"
          % (display, ceiling, ceiling * 4))
    for letter, what, request in cases:
        got_ceiling, answer, closed = probe(display, request)
        if got_ceiling != ceiling:
            print("# ceiling moved between connections: %d" % got_ceiling)
        print("(%s) %s" % (letter, what))
        print("    request %s + 4 NoOperations + GetInputFocus" % request.hex())
        if answer:
            print("    answer  %s" % answer.hex())
            if closed:
                print("    then    closed")
        else:
            print("    answer  %s" % ("closed" if closed else "silent"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
