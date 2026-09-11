"""The Hyprland display backend: `j/monitors` to read, `keyword monitor` to write.

Hyprland advertises `zwlr_output_manager_v1` version 4 and the generic wlroots backend reads it correctly --
but it cannot APPLY through it. Measured on 0.53.3, and again on 0.56.2 [M recon2/hyprland.md §4,
recon2/arch.md]:

* the FIRST apply of a fresh session with one head works (rc 0 in 0.25 s, the mode changed, `hyprctl monitors`
  agreed);
* every apply after it times out at 10 s with nothing changed and no `[COutputConfiguration] Applying
  configuration` line in Hyprland's own log -- and `wlr-randr`, the reference client, hangs for ever on the
  same request, so this is not our protocol code;
* with a second (headless) output present, even the first apply times out;
* and after a wedged apply, Hyprland's OWN `hyprctl keyword monitor` stops taking effect too (three times in a
  row on one session), while the same command on a fresh session applies at once.

That last line is what this file is: `keyword monitor NAME,WxH@Hz,XxY,SCALE` applied instantly and correctly
on a session that had never touched the protocol. So the route is Hyprland's own IPC, and the wlr path is left
for `--backend wlr` and told what it is walking into (wxrandr/core.py's timeout carries a Hyprland clause).

And when the keyword is the one doing nothing -- which is a state, not a version: a fresh 0.56.2 session took
none of three modes or a scale [M goal2/recon/flavors.md 3b] while the arch-hypr golden took every one of them
on 2026-09-11 and then stopped later in the same session -- there is a second route under it, and it is route
2 rather than 6: the same rules written into a file `hyprland.conf` sources, plus `hyprctl reload`. That is
`HyprOutputs.apply`'s fallback, and the block above it carries what it costs.

The transform numbering here is wlroots', not the spec-as-Mutter-reads-it: `--rotate left` over the protocol
put sway transform "270" on the wire and `hyprctl` reported `transform: 3` [M recon2/hyprland.md §4], which is
`core.WL_TRANSFORM`'s numbering (3 == 270 == xrandr `left`) and not `core.from_wl_spec_transform`'s (where 3 is
`right`). `snapshot_wlr` reads the same enum the same way."""

import json
import os
import re
import socket
import time

from w11common import session as wsession
from wxrandr import core
from wxrandr.core import Fatal, Mode, OutputState

#: `1920x1080@75.00Hz`, the shape of every string in `availableModes`
#: [M recon2/hyprland.md fixtures, tests/fixtures/hypr/monitors.json]
_MODE_RE = re.compile(r"^(\d+)x(\d+)@([0-9.]+)Hz$")

#: Deadline on connect and on the reply, the ten seconds every backend here arms.
IPC_TIMEOUT = 10.0
#: What probe() waits. `hypr` is second in AUTO_ORDER and `wxrandr --backends` runs every probe, so a socket
#: file whose compositor is gone must not cost ten seconds before the wlr fallback is even tried.
PROBE_TIMEOUT = 2.0


class HyprIPC:
    """Hyprland's request socket: send the request as text, read the reply to EOF, close.

    That is the whole protocol -- one connection per request, proved against a live 0.53.3 with a ten-line
    AF_UNIX client [M recon2/hyprland.md §2] -- and this is the display half of it: connect, `j/<name>`,
    `keyword <text>`.

    It is a second copy of `wdotool/hypr_ipc.py`'s reader, and deliberately, for the reason
    `wdotool/layoutbox.py` carries its own copy of Mutter's logical-size rule: the zipapp install route builds
    `dist/wxrandr` out of `w11common` and `wxrandr` alone (scripts/build-pyz.sh, pinned by
    tests/test_build_scripts.py:TheZipapps -- "no display tool carries the input stack", 680 kB off each of
    the three), so a `from wdotool...` here would work from the .deb and quietly not from the zipapp -- and
    "quietly" on Hyprland means falling back to a wlr path that cannot apply. The price is this class; what
    keeps the two from drifting is tests/test_wxrandr_hypr.py:TheTwoClients, which drives both against one
    double and insists on the same bytes and the same sentences.

    `wxrandr` speaks Fatal, so every failure is one here rather than the CmdError the wdotool side raises."""

    def __init__(self, sockpath: "str | None" = None, timeout: float = IPC_TIMEOUT):
        self.sockpath = sockpath or wsession.find_hypr_socket()
        if not self.sockpath:
            raise Fatal("hypr backend: no Hyprland IPC socket found ($HYPRLAND_INSTANCE_SIGNATURE unset "
                        "and no hypr/*/.socket.sock in any runtime dir)\n")
        self.timeout = timeout

    def close(self):
        """Nothing to close -- there is no held connection. It exists so this object can be a probe's handle,
        which is closed once by the probe sweep and again by the Session that reused it."""

    @property
    def events_path(self) -> str:
        """`.socket2.sock` beside `.socket.sock`. Nothing here reads it; it is the one line that keeps the
        two clients' idea of the instance directory identical."""
        return os.path.join(os.path.dirname(self.sockpath), ".socket2.sock")

    def _connect(self) -> socket.socket:
        """A connected socket. The connect gets its own deadline because a compositor wedged inside its own
        event loop still has a LISTENING socket -- the kernel queues connections for it without waking it --
        so connecting looks fine right up to the moment the backlog fills."""
        deadline = time.monotonic() + self.timeout
        while True:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                s.connect(self.sockpath)
                break
            except (TimeoutError, BlockingIOError):
                # both are OSError subclasses: this arm has to come first
                s.close()
                if time.monotonic() >= deadline:
                    raise self._wedged() from None
                time.sleep(0.01)
            except OSError as e:
                s.close()
                raise Fatal("hypr backend: cannot connect to %s: %s\n" % (self.sockpath, e)) from None
        s.settimeout(self.timeout)
        return s

    def _wedged(self) -> Fatal:
        return Fatal("hypr backend: no answer from the compositor within %gs "
                     "(it is not responding)\n" % self.timeout)

    def request(self, text: str) -> bytes:
        s = self._connect()
        try:
            try:
                s.sendall(text.encode())
            except TimeoutError:
                raise self._wedged() from None
            except OSError as e:
                raise Fatal("hypr backend: lost the connection to the compositor (%s)\n" % e) from None
            out = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except TimeoutError:
                    # TimeoutError is an OSError: this arm has to come first, or a compositor that is merely
                    # wedged reads as one that has gone.
                    raise self._wedged() from None
                except OSError as e:
                    raise Fatal("hypr backend: lost the connection to the compositor (%s)\n" % e) from None
                if not chunk:
                    break
                out += chunk
        finally:
            s.close()
        if not out:
            raise Fatal("hypr backend: the compositor closed the IPC socket without answering `%s`\n" % text)
        return out

    def json(self, name: str):
        raw = self.request("j/" + name)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as e:
            raise Fatal("hypr backend: the compositor's answer to j/%s is not JSON (%s)\n"
                        % (name, e)) from None

    def keyword(self, text: str) -> str:
        """`keyword <text>` -- a live config assignment, which is how a layout is applied. Hyprland answers
        `ok`, or its own words, which are relayed whole and in its name."""
        return self._ok("keyword " + text)

    def reload(self) -> str:
        """`reload` -- re-read hyprland.conf and everything it sources, and apply what it says.

        This is the request `hyprctl reload` sends, and it is the half of the route-2 apply that makes a
        written `monitor =` rule take effect [M arch-hypr golden, Hyprland 0.56.2, 2026-09-11]."""
        return self._ok("reload")

    def _ok(self, text: str) -> str:
        reply = self.request(text).decode("utf-8", "replace").strip()
        if reply != "ok":
            raise Fatal("hypr: %s\n" % reply)
        return reply


#: `mirrorOf` when an output mirrors nothing
NO_MIRROR = "none"

#: The w11-owned file the route-2 apply writes its `monitor =` rules into, in Hyprland's own config
#: directory beside hyprland.conf.
W11_CONF_NAME = "w11-monitors.conf"

#: The header that file is rewritten with every time, so whoever opens it knows who wrote it and what
#: removing it does.
W11_CONF_HEADER = (
    "# Written by wxrandr (w11) -- one `monitor =` rule per output it applied.\n"
    "# Some Hyprland 0.56.2 sessions store `hyprctl keyword monitor` and never apply it (measured on\n"
    "# a fresh one, 2026-09-11: three modes out of the head's own availableModes and a scale, every\n"
    "# one answered ok and none applied -- while on another session the same day every one applied).\n"
    "# These rules apply on either: Hyprland re-reads a file it sources, and mode, position, scale,\n"
    "# transform and disable were all measured landing that way.  Delete this file, and the\n"
    "# `source =` line hyprland.conf carries for it, to be rid of both.\n"
)

#: One rule line: `monitor = NAME,...`, with the spacing Hyprland's own parser accepts either way.
_RULE_RE = re.compile(r"^\s*monitor\s*=\s*([^,#]+,[^#]*?)\s*$")

#: A `source =` line, for finding the one that already points at our file.
_SOURCE_RE = re.compile(r"^\s*source\s*=\s*(.+?)\s*$")

#: What the file costs, on either path, said out loud rather than hidden: a re-read is of the WHOLE
#: config, so every runtime `hyprctl keyword` since login goes back to what the files say --
#: `general:gaps_out 40` read back `40 40 40 40, set: true` and was `20 20 20 20, set: false` after one
#: [M arch-hypr golden, 0.56.2, 2026-09-11].  And the re-read is not the `reload` request's alone: on the
#: same golden a bare rewrite of the sourced file, with no `hyprctl reload` sent at all, applied the new
#: mode AND reset that same keyword the same way, as did appending one comment line to hyprland.conf.
#: Hyprland watches the files it sources; writing one IS a reload.
RELOAD_COST = "the whole config is re-read: runtime `hyprctl keyword`s set since login are reset"

WROTE_CONF_NOTE = ("Hyprland accepted the live `keyword monitor` and did not apply it, so the layout went "
                   "into %s and the config was reloaded (" + RELOAD_COST + ")\n")

PERSIST_CONF_NOTE = ("--persistent: the layout is in %s for the next session too, and writing that file is "
                     "itself a reload, no `hyprctl reload` needed (" + RELOAD_COST + ")\n")

KEPT_CONF_NOTE = ("those rules stay in %s, with or without --persistent: Hyprland re-reads a file it "
                  "sources as soon as it changes, so taking them back out puts the head straight back at "
                  "its preferred mode (measured on Hyprland 0.56.2, 2026-09-11). Delete that file, or the "
                  "`source =` line hyprland.conf carries for it, to undo this layout\n")

SOURCED_CONF_NOTE = "added a `source = %s` line to %s\n"

NO_CONF_NOTE = ("there is no %s to source the rules from, and a config file written by this tool would be "
                "the whole of the next session's config, so none was: add `source = %s` to the config this "
                "session started with\n")


def hypr_config_dir() -> str:
    """Where Hyprland looks for hyprland.conf: `$XDG_CONFIG_HOME/hypr`, else `~/.config/hypr`."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "hypr")


def _mode_of(text: str) -> "Mode | None":
    """One `availableModes` string as a Mode, or None for a shape this has never seen."""
    m = _MODE_RE.match(text.strip())
    if not m:
        return None
    return Mode(w=int(m.group(1)), h=int(m.group(2)),
                refresh_mhz=int(round(float(m.group(3)) * 1000)))


def _rate_text(mhz: int) -> str:
    """The refresh a `keyword monitor` line carries: `%g` of the Hz, so 75000 is `75` and 60020 is `60.02`.

    Hyprland parses the field with its own float reader and matches the nearest mode, so an exact
    three-decimal rate is neither needed nor harmful; what matters is that a whole number does not arrive as
    `75.000000`, which is what `%f` would send."""
    return "%g" % (mhz / 1000.0)


class HyprOutputs:
    """Snapshot + per-output `keyword monitor` apply over Hyprland's IPC, in the five-method backend shape.

    Holds no connection (`HyprIPC` opens one per request, which is the protocol), so `close()` is a no-op and
    the same object can be a probe's handle and then the Session's backend."""

    name = "hypr"

    def __init__(self, sock: "str | None" = None, ipc: "HyprIPC | None" = None,
                 conf_dir: "str | None" = None):
        self.ipc = ipc if ipc is not None else HyprIPC(sock)
        #: {name: the output it mirrors}, from the last snapshot, so an apply that does not mention a mirror
        #: does not silently break one -- a `keyword monitor` line with no `mirror` field clears it.
        self.mirrors: "dict[str, str]" = {}
        #: {name: the row j/monitors gave}, for the verify step's error line
        self.rows: "dict[str, dict]" = {}
        #: Hyprland's config directory, resolved once per run (a test points it at its own).
        self.conf_dir = conf_dir or hypr_config_dir()

    @property
    def sockpath(self) -> str:
        return self.ipc.sockpath

    def close(self):
        """Nothing to close: every request is its own connection. Idempotent by construction, which is what
        the probe's handle contract asks for."""

    # -- query ---------------------------------------------------------------

    #: `all`, and not the plain `j/monitors`, because a head Hyprland has disabled is not in the plain
    #: answer at all -- the row VANISHES rather than gaining `disabled: true`.  Measured live on
    #: resolute-hypr (Hyprland 0.53.3) 2026-09-09, after `wxrandr --output Virtual-3 --off`:
    #:
    #:     j/monitors      -> [('Virtual-1', False), ('Virtual-2', False)]
    #:     j/monitors all  -> [('Virtual-1', False), ('Virtual-2', False), ('Virtual-3', True)]
    #:
    #: and the disabled row is complete: the same keys as an enabled one and all 26 `availableModes`.  X is
    #: the oracle and `xrandr --output X --off` keeps X in the listing with no mode, so the plain form was
    #: the wrong question: it made `--off` refuse ("Hyprland accepted the configuration for Virtual-3 and
    #: then stopped listing it") and left `--auto`, `--right-of` and `--below` warning `output Virtual-3 not
    #: found; ignoring` on an output that was there the whole time.
    MONITORS = "monitors all"

    def monitors(self) -> list:
        rows = self.ipc.json(self.MONITORS)
        if not isinstance(rows, list):
            raise Fatal("the compositor's j/monitors answer is not a list of monitors\n")
        return rows

    def snapshot(self, state: "core.State | None" = None) -> list:
        """OutputState per `j/monitors` row.

        `width`/`height` are the mode's pixels and `x`/`y` are already logical, so the logical rectangle is
        core.logical_size() of the two -- the recorded second head is a 1920x1080 HEADLESS at scale 2.0 sitting
        at +1920+0, which is 960x540 of layout."""
        outs = []
        self.mirrors = {}
        self.rows = {}
        rows = self.monitors()
        # `mirrorOf` comes back as the mirrored monitor's numeric `id` AS A STRING, never its name --
        # measured live on resolute-hypr 2026-09-09: `keyword monitor Virtual-3,...,mirror,Virtual-1`
        # answers ok and `j/monitors all` then reports `mirrorOf: "0"`, Virtual-1's id.  Both spellings are
        # accepted on the way in (`,mirror,0` and `,mirror,Virtual-2` both applied), so this is only about
        # what we remember: an id is a position in Hyprland's own list and moves when a head is plugged,
        # where a name does not, so the id is translated back here and the name is what a later
        # `keyword monitor` re-emits.
        by_id = {str(m.get("id")): str(m.get("name") or "?") for m in rows}
        for i, m in enumerate(rows):
            name = str(m.get("name") or "?")
            self.rows[name] = m
            mirror = str(m.get("mirrorOf") or NO_MIRROR)
            if mirror != NO_MIRROR:
                self.mirrors[name] = by_id.get(mirror, mirror)
            st = OutputState(
                name=name, active=not m.get("disabled"), ident=i + 1,
                mm_w=int(m.get("physicalWidth") or 0), mm_h=int(m.get("physicalHeight") or 0),
                make=str(m.get("make") or "") or "Unknown",
                model=str(m.get("model") or "") or "Unknown",
                serial=str(m.get("serial") or "") or "Unknown",
            )
            for text in m.get("availableModes") or []:
                mode = _mode_of(str(text))
                if mode is not None:
                    st.modes.append(mode)
            st.virtual_modes = not st.modes
            if st.active:
                st.x, st.y = int(m.get("x") or 0), int(m.get("y") or 0)
                st.scale = float(m.get("scale") or 1.0) or 1.0
                st.transform = core.WL_TRANSFORM_NAME.get(int(m.get("transform") or 0), "normal")
                st.current = self._current_mode(st, m)
                if st.current is not None:
                    st.w, st.h = core.logical_size(st.current.w, st.current.h, st.transform, st.scale)
            core.finish_modes(st, [] if state is None else state.modes_for_output(name))
            outs.append(st)
        return outs

    @staticmethod
    def _current_mode(st: OutputState, m: dict) -> "Mode | None":
        """Which of the advertised modes is running.

        `width`/`height`/`refreshRate` are the live mode and `availableModes` is the list, and the two do not
        agree to the digit: the recorded head runs 74.998 Hz and advertises `1920x1080@75.00Hz`. So the match
        is on the size and then on the nearest rate -- which is also what keeps the query one row (a mode
        synthesized beside the list would print `1920x1080` twice, both reading `75.00`). A size that is in no
        advertised mode at all (a headless output driven off-list) gets a Mode of its own, prepended, because
        the alternative is a `--query` that does not say what is running."""
        px_w, px_h = int(m.get("width") or 0), int(m.get("height") or 0)
        hz = float(m.get("refreshRate") or 0.0)
        cands = [x for x in st.modes if (x.w, x.h) == (px_w, px_h)]
        if cands:
            return min(cands, key=lambda x: abs(x.refresh_hz - hz))
        if not px_w or not px_h:
            return st.modes[0] if st.modes else None
        live = Mode(w=px_w, h=px_h, refresh_mhz=int(round(hz * 1000)))
        st.modes.insert(0, live)
        return live

    # -- planning ------------------------------------------------------------

    def predicted_dims(self, t: "core.Target", state: "core.State") -> tuple:
        """`keyword monitor` carries the scale as text in a config line, exactly as the sway IPC does, so it
        lands on the 120th the decimal was printed to rather than on the wl_fixed one the protocol would
        have."""
        return core.predicted_dims(t, state, wire="text")

    def verify(self, state: "core.State", targets: list):
        """--dryrun: Hyprland's IPC has no validating call yet. `keyword` IS the apply, and running it would
        be the thing a dryrun must not do, so this hook sends nothing and the plan is printed unchecked.

        The route is a validating call in that same IPC (AGENTS.md route 6, Hyprland's own code): its cost
        is a patched compositor for a flag that reports rather than acts, which is why nothing here waits
        for it."""

    # -- apply ---------------------------------------------------------------

    def monitor_line(self, t: "core.Target", pos: dict) -> str:
        """The `keyword monitor` argument for one target, byte for byte.

        `NAME,disable` for an output going off; otherwise `NAME,WxH@Hz,XxY,SCALE` with `,transform,N` and
        `,mirror,OTHER` appended in that order. Hyprland's parser reads the first four fields positionally and
        the rest as keyword pairs [R the config's `monitor=` grammar; the four-field form is the one measured
        applying instantly, `keyword monitor "Virtual-1,1280x1024@60,0x0,1"`, M recon2/hyprland.md §2]."""
        name = t.name
        if not t.enabled:
            return "%s,disable" % name
        mode = t.mode if t.mode is not None else t.output.current
        if mode is None:
            size = "preferred"
        elif mode.refresh_mhz:
            size = "%dx%d@%s" % (mode.w, mode.h, _rate_text(mode.refresh_mhz))
        else:
            size = "%dx%d" % (mode.w, mode.h)
        x, y = pos.get(name, (t.output.x, t.output.y))
        line = "%s,%s,%dx%d,%g" % (name, size, x, y, t.scale)
        if t.sway_tf != "normal":
            line += ",transform,%d" % core.WL_TRANSFORM[t.sway_tf]
        mirror = self._mirror_for(t)
        if mirror:
            line += ",mirror,%s" % mirror
        return line

    def _mirror_for(self, t: "core.Target") -> str:
        """The output `t` should mirror after this run: the `--same-as` it was given, else whatever it is
        mirroring now (a line with no `mirror` field clears one, so an unrelated `--pos` on a mirrored output
        would silently un-mirror it)."""
        s = t.stanza
        if s is not None and s.relation is not None:
            kind, other = s.relation
            return other if kind == "same-as" else ""
        return self.mirrors.get(t.name, "")

    # -- route 2: the rules file hyprland.conf sources -----------------------
    #
    # `hyprctl keyword monitor` is stored and not applied on SOME Hyprland 0.56.2 sessions: on a fresh one,
    # three modes from the head's own `availableModes` and a scale all answered `ok` in 4 ms and changed
    # nothing, while `keyword general:gaps_out 40` on that session took effect and read back
    # [M goal2/recon/flavors.md 3b, arch-hypr golden, 2026-09-11].  It is not every session and not a
    # version rule: on the arch-hypr golden later the same day the same keyword applied 1280x1024,
    # 1024x768, 1680x1050, 5120x2160 and a `--pos 3840x200` that CI had seen land at 3840,0, and went on
    # applying them after a wlr apply had timed out at 10 s [M, this file's batch, 2026-09-11].  Which is
    # why neither path is a promise here: the keyword is the fast one, the re-read is what says whether it
    # did anything, and the file below is what lands the layout when it did not.  The same golden applied
    # mode, scale, transform AND disable from a `source =`d file plus `hyprctl reload`, all four measured,
    # on the session where `gaps_out` proved a reload's cost.  AGENTS.md route 2, the compositor's own
    # config surface, and not route 6.
    #
    # The costs are measured and they are paid here rather than hidden.  A re-read is of the whole config,
    # so runtime `keyword`s set since login go back to what the files say -- `keyword general:gaps_out 40`
    # read back `40 40 40 40, set: true` and was `20 20 20 20, set: false` after one [M arch-hypr golden,
    # 0.56.2, 2026-09-11].  That cost belongs to the FILE and not to the `reload` request: Hyprland watches
    # the files it sources (`misc:disable_autoreload` is off by default), so on the same golden the rule
    # applied on the WRITE, before `hyprctl reload` was sent; a bare rewrite of the sourced file with no
    # reload request anywhere reset that same keyword and moved the head; and appending one comment line to
    # hyprland.conf did it too.  Writing the file IS a reload, which is why `--persistent`'s fast path says
    # the same sentence this one does.  It is also why a non-persistent run cannot take its rules out again
    # without undoing its own apply (writing the file back empty put the head at 1920x1080 before the next
    # read), and is told so instead.  And a hyprland.conf with no `source =` for our file gains one appended
    # line, in a user-owned file, so that is said out loud too.
    #
    # X's `xrandr` is one-shot -- a mode dies with the session unless something replays it -- and this file
    # does not, which is the one place the route is louder than the original.  NOT YET, and the route is the
    # same rung 2 and measured on the same golden: `hyprctl keyword misc:disable_autoreload true` takes
    # (`int: 1, set: true`), and with it set the write is not read at all (the rules file rewritten to
    # 1024x768 left the head at 1680x1050 and `gaps_out` at 40) while an explicit `hyprctl reload` still
    # applies it.  So disable / write / reload / verify / disable again / write the rules back out would
    # land the layout and leave the file clean.  The cost is what stopped it here: `reload` resets
    # `disable_autoreload` itself (`int: 0, set: false` after it), so the window between the reload and the
    # second disable is one where any other config write lands, and a run that dies in the middle leaves a
    # session whose config edits are silently ignored until something reloads.  Two writes and two keywords
    # per apply, for a file the run already names in a line the user can delete.

    def rules_path(self) -> str:
        return os.path.join(self.conf_dir, W11_CONF_NAME)

    def read_rules(self) -> dict:
        """{output: the whole `monitor =` argument} our file already holds, in file order."""
        out = {}
        try:
            with open(self.rules_path(), encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return out
        for line in text.splitlines():
            m = _RULE_RE.match(line)
            if m:
                out[m.group(1).split(",")[0].strip()] = m.group(1)
        return out

    def write_rules(self, lines: dict) -> str:
        """Merge `lines` ({output: monitor line}) into our file and return its path.

        Merged rather than replaced: two `wxrandr --output A ...` runs in a row must not take B's rule out
        with them, which is the whole difference between this file and the one-shot keyword."""
        rules = self.read_rules()
        rules.update(lines)
        return self._write_file(rules)

    def restore_rules(self, rules: dict) -> str:
        """Put the file back exactly as `rules` -- what a route-2 apply that did not land owes.

        The whole map, not a pop of the names this run touched: an earlier `--persistent` run may hold a
        rule for the SAME output, and popping by name would take that one out with ours, changing a layout
        this run never applied.  What was there before the write is what is there after it.

        The file itself stays, header and all (empty when it held nothing before), because the `source =`
        line in hyprland.conf has to keep naming a file that exists: Hyprland refuses to parse a `source =`
        whose target is missing, and the next apply would then be arguing with a config error of ours."""
        return self._write_file(rules)

    def _write_file(self, rules: dict) -> str:
        """`rules` ({output: monitor line}) as the whole file, written and renamed into place so a
        `hyprctl reload` racing the write cannot read half a rule."""
        path = self.rules_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        body = W11_CONF_HEADER + "".join("monitor = %s\n" % rules[k] for k in rules)
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
        return path

    def conf_path(self) -> str:
        return os.path.join(self.conf_dir, "hyprland.conf")

    def ensure_source(self, path: str) -> str:
        """Make hyprland.conf source our rules file: `"present"`, `"added"` or `"missing"`.

        Appended, so our rules are the LAST matching ones Hyprland reads: the rig's own config carries
        `monitor = Virtual-1,preferred,0x0,1` lines (vm/build-image.sh desktop_hypr) and a rule sourced
        before them would be the one that lost.

        A hyprland.conf that is not there is left not there.  Creating it would not be adding a line to a
        user's config, it would be REPLACING the config the running session has -- Hyprland writes a default
        one with the keybinds in it on first run, and a file of ours holding a single `source =` would be
        all the next session read.  That case is said out loud instead (`NO_CONF_NOTE`), with the line to
        add, and the apply then fails the way any other unapplied layout does."""
        conf = self.conf_path()
        try:
            with open(conf, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return "missing"
        for line in text.splitlines():
            m = _SOURCE_RE.match(line)
            if m and os.path.basename(m.group(1).strip().strip('"')) == W11_CONF_NAME:
                return "present"
        with open(conf, "a", encoding="utf-8") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write("\n# w11: wxrandr writes the layout it applies here (AGENTS.md route 2).\n"
                     "source = %s\n" % path)
        return "added"

    def _say_source(self, path: str, how: str):
        if how == "added":
            core.warn(SOURCED_CONF_NOTE % (path, self.conf_path()))
        elif how == "missing":
            core.warn(NO_CONF_NOTE % (self.conf_path(), path))

    def apply(self, state: "core.State", targets: list, persistent: bool = False) -> list:
        """One `keyword monitor` per touched output, then a `j/monitors` re-read that has to agree -- and,
        when it does not, the same rules through the file Hyprland sources plus a `reload`.

        The re-read is not decoration. Hyprland answers `ok` to a `keyword monitor` it then ignores --
        measured three times in a row on a session whose protocol apply had wedged [M recon2/hyprland.md
        §4], and on every apply of a FRESH 0.56.2 session [M goal2/recon/flavors.md 3b] -- so a run that
        only trusted the `ok` would report a layout that is not on the screen.  What follows a caught
        mismatch is the route-2 apply above.

        `--persistent` is the flag the original never had, and here it decides one thing: whether the file
        is written on the path where it was not NEEDED.  Where it was -- where the live keyword did
        nothing -- the rules stay either way, and the plan's "a non-persistent apply takes its own line out
        again" did not survive the measurement: Hyprland watches the files it sources, so removing the rule
        applied the removal within a second and put the head back at its preferred mode, with no `reload`
        sent at all (arch-hypr golden, 0.56.2, 2026-09-11: write the rule and the mode changes before the
        `hyprctl reload` is sent; write the file back empty and the mode is back before the next read).
        A non-persistent run is told that, and what to delete."""
        core.record_lastmodes(state, targets)
        dims = {}
        for t in targets:
            if t.enabled:
                dims[t.name] = self.predicted_dims(t, state)
        pos = core.resolve_positions(targets, dims)
        touched = [t for t in targets if t.changed]
        lines = {}
        for t in touched:
            lines[t.name] = self.monitor_line(t, pos)
            self.ipc.keyword("monitor " + lines[t.name])
        fresh = self.snapshot(state)
        bad = self._first_mismatch(touched, pos, {o.name: o for o in fresh})
        if bad is None:
            if persistent and lines:
                self._persist(lines)
            return fresh
        return self._apply_by_reload(state, touched, pos, lines, persistent)

    def _persist(self, lines: dict):
        """`--persistent` on a session where the live keyword DID land: the layout is already on the screen
        and the rule goes into the file for the next session.

        No `hyprctl reload` is SENT here, and that is not the same as no reload happening: Hyprland watches
        the files it sources, so the write itself is one (measured on the arch-hypr golden, 0.56.2,
        2026-09-11: a bare rewrite of the sourced file, no reload request anywhere, applied the new mode and
        put `general:gaps_out` from `40, set: true` back to `20, set: false`; appending a line to
        hyprland.conf, which is what the FIRST persistent run does, did the same).  So this path pays
        `RELOAD_COST` like the other one and says so -- the flag buys the next session's layout, not a
        cheaper apply.  `monitor` is a runtime keyword like `gaps_out`, so an untouched output that an
        earlier run had moved with the live keyword alone goes back to what the config says along with the
        rest [R: that a reload resets `gaps_out` is measured, that it resets a live `keyword monitor` is
        the same sentence read twice and was not measured on its own -- the session it would have been
        measured on was one where the keyword applied nothing]."""
        path = self.write_rules(lines)
        self._say_source(path, self.ensure_source(path))
        core.warn(PERSIST_CONF_NOTE % path)

    def _apply_by_reload(self, state, touched: list, pos: dict, lines: dict, persistent: bool) -> list:
        """The route-2 apply: write the rules, make sure hyprland.conf sources them, reload, re-read.

        The file is restored to what it held BEFORE this run when the layout did not land: a layout the
        compositor would not take is not one to leave in a file that the next login reads, and restoring
        the whole map rather than popping our names keeps an earlier `--persistent` run's rule for the same
        output.  Rules that did land stay, because removing them is itself an apply here (see `apply`), and
        a run that was not asked for `--persistent` says so and names the file to delete.

        The note claiming the reload is printed after the reload has returned and the re-read has agreed,
        because until then there is no reload and no layout to claim -- and it is not printed at all on the
        path where nothing sources our file, which is the path that sends no reload."""
        before = self.read_rules()
        path = self.write_rules(lines)
        sourced = self.ensure_source(path)
        self._say_source(path, sourced)
        landed = False
        try:
            if sourced != "missing":
                # Nothing sources our file, so a reload would re-read a config that does not mention it:
                # the whole cost of a reload (every runtime `keyword` since login) for none of its effect.
                # The apply then fails on the re-read below, which is what NO_CONF_NOTE says it will.
                self.ipc.reload()
            fresh = self.snapshot(state)
            self._verify_applied(touched, pos, {o.name: o for o in fresh})
            landed = True
            core.warn(WROTE_CONF_NOTE % path)
            if not persistent:
                core.warn(KEPT_CONF_NOTE % path)
            return fresh
        finally:
            if not landed:
                self.restore_rules(before)

    def _verify_applied(self, touched: list, pos: dict, fresh: dict):
        """`_first_mismatch` as a refusal: one line naming the first output Hyprland accepted and did not
        change."""
        bad = self._first_mismatch(touched, pos, fresh)
        if bad is not None:
            raise Fatal(bad)

    def _first_mismatch(self, touched: list, pos: dict, fresh: dict) -> "str | None":
        """The sentence for the first output Hyprland accepted and did not change, or None.

        Position, mode size and transform are checked and the scale is not: those three are what §4 measured
        standing still, and Hyprland adjusts a scale it cannot divide the head by (so a mismatch there would
        be its arithmetic rather than a refusal)."""
        for t in touched:
            o = fresh.get(t.name)
            if o is None:
                return "Hyprland accepted the configuration for %s and then stopped listing it\n" % t.name
            if o.active != t.enabled:
                return ("Hyprland accepted %s %s and did not apply it (it is still %s)\n"
                        % (t.name, "on" if t.enabled else "off", "on" if o.active else "off"))
            if not t.enabled:
                continue
            want_pos = pos.get(t.name)
            if want_pos is not None and (o.x, o.y) != tuple(want_pos):
                return ("Hyprland accepted the position %dx%d for %s and did not apply it "
                        "(it reports %dx%d)\n" % (want_pos[0], want_pos[1], t.name, o.x, o.y))
            cur = o.current
            if t.mode is not None and cur is not None and (cur.w, cur.h) != (t.mode.w, t.mode.h):
                return ("Hyprland accepted the mode %dx%d for %s and did not apply it "
                        "(it reports %dx%d)\n" % (t.mode.w, t.mode.h, t.name, cur.w, cur.h))
            if o.transform != t.sway_tf:
                return ("Hyprland accepted the transform %s for %s and did not apply it "
                        "(it reports %s)\n" % (t.sway_tf, t.name, o.transform))
        return None


def probe(sock: str, verbose: bool = False):
    """`wxrandr --backends` / `--print-backend`'s row for `hypr`.

    The caller (wxrandr/cli.py:_probe_hypr) has already found the socket, so what is left to establish is that
    something on the other end really is Hyprland: `j/version` parsing is the whole test, and it is also where
    the version in `compositor: Hyprland 0.53.3` comes from. `verbose` is taken and ignored -- _probe_sway
    pays for its version line only when asked, and here the same request is the availability test, so there is
    nothing left to defer. A probe never raises, and it never waits the backend's ten seconds either: a socket
    file whose compositor is gone must not cost that much before the wlr fallback is even tried."""
    from wxrandr.cli import Probe
    try:
        ipc = HyprIPC(sock, timeout=PROBE_TIMEOUT)
        ver = ipc.json("version")
    except Fatal as e:
        return Probe("hypr", False, str(e).rstrip("\n"))
    if not isinstance(ver, dict) or not ver.get("version"):
        return Probe("hypr", False, "the socket at %s did not answer j/version like Hyprland" % sock)
    # the backend that comes out of this probe gets the ordinary deadline: a probe may not wait ten seconds,
    # an apply may
    return Probe("hypr", True, detail="IPC socket %s" % sock,
                 compositor="Hyprland %s" % ver["version"],
                 protocol="Hyprland IPC (hyprctl)",
                 handle=HyprIPC(sock))
