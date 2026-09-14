"""GNOME Shell (Mutter) window backend over the w11 bridge extension.

GNOME has no window-management protocol and gnome-shell's own D-Bus surface
is either read-only and sender-allowlisted (org.gnome.Shell.Introspect) or
off by default (org.gnome.Shell.Eval outside "unsafe mode"). The only
supported way in is code running inside the shell: gnome/w11-bridge@
w11 (see gnome/README.md) exports Mutter's window/workspace/monitor
facts and actions on the session bus as

    name  org.w11.Bridge   path /org/w11/Bridge
    iface org.w11.Bridge1  (JSON strings for structured results)

and this module is a thin client for it over dbus_mini (pure stdlib, one
connection per process, no gdbus spawns). Window ids are Meta.Window.get_id()
(64-bit, stable for the shell's lifetime -- the same numbers
org.gnome.Shell.Introspect uses), printed in decimal like every backend.

Field mapping (bridge object -> Window):
  class_   wm_class, else gtk_app_id, else sandboxed_app_id (Mutter reports
           the Wayland app_id as wm_class for native clients)
  instance wm_class_instance -- the WM_CLASS *instance* of an XWayland client
           (`xterm -name myinst` -> "myinst"), "" for native toplevels, where
           `search --classname` then falls back to class_ = the app_id
  x,y,w,h  get_frame_rect(): logical pixels, SSD frame included, no CSD
           shadow -- the same space the input daemon's pointer lives in
  focused  has_focus()
  visible  not hidden (minimized / show-desktop) AND on the active workspace
           -- X11's IsViewable; `is_mapped` is the looser "not minimized" so
           windowmap --sync does not hang on windows parked elsewhere
  desktop  workspace index, -1 when on all workspaces (sticky)

list() order is Mutter's stacking order bottom->top, so backend.hit_test()
picks hits[-1] = topmost; it also skips the DESKTOP/DOCK layers (desktop
icons, docks) window_type names, like a click-through X11 root window would.

When the bridge name is missing but org.gnome.Shell is on the bus, the
constructor fails with a diagnosis: screen locked, extension disabled or
broken, or the one-line install hint (_HINT). Only with WDOTOOL_GNOME_AUTOLOAD
set (to anything but 0) does it first try to load the installed extension
through org.gnome.Shell.Eval, which works only while the shell is in unsafe
mode; the default path -- the common "installed, needs a re-login" case --
never probes that privileged interface."""

import json
import os
import pwd
import re
import struct
import time

from w11common import session
from w11common.dbus_mini import ERR, Bus, DBusError, no_bus_text
from w11common.errors import CmdError
from hacks.window.backend import View, Window, WindowBackend, Workspace, warn
from wdotool.ctx import NoSessionError

BUS_NAME = "org.w11.Bridge"
OBJECT_PATH = "/org/w11/Bridge"
IFACE = "org.w11.Bridge1"
SHELL_NAME = "org.gnome.Shell"
SCREENSAVER_NAME = "org.gnome.ScreenSaver"
EXT_UUID = "w11-bridge@w11"

_HINT = ("gnome backend: the w11 bridge extension is not running in "
         "GNOME Shell; run gnome/install-bridge.sh and restart the session "
         "(log out and back in)")
_GONE = ("gnome backend: the w11 bridge vanished from the session bus "
         "(extension disabled, screen locked, or shell restarting); run "
         "gnome/install-bridge.sh --check")
#: appended to _HINT when the screen is locked as well: true, and the reason
#: a re-login is needed anyway, but not the thing to say first
_ALSO_LOCKED = ("; the screen is locked as well, and GNOME Shell disables "
                "extensions behind the lock screen, so unlocking alone will "
                "not be enough")
#: install-bridge.sh's own two destinations, both
#: <data dir>/gnome-shell/extensions/<uuid>
_EXT_SUBDIR = os.path.join("gnome-shell", "extensions", EXT_UUID)
_EXT_FILE = os.path.join(_EXT_SUBDIR, "extension.js")

CALL_TIMEOUT = 10.0     # every bridge call answers in milliseconds
AUTOLOAD_WAIT = 3.0     # after a successful Eval(loadExtension)

# xdotool action -> bridge SetState action word
_ACTIONS = {0: "remove", 1: "add", 2: "toggle"}
# _NET_WM_STATE atoms Mutter has no setter for and that change nothing a script can observe through these tools:
# warn + succeed (docs/WDOTOOL.md cosmetic rule). SHADED is not one of them: shading is a visible operation a script
# may rely on, and Mutter does not implement it at all -- a real capability gap, like BELOW. Everything else the
# bridge cannot apply is a gap too.
_COSMETIC_STATES = {"SKIP_TASKBAR", "SKIP_PAGER", "MODAL"}
_GAP_REASONS = {"SHADED": "Mutter does not implement window shading", "BELOW": "Mutter has no API for it"}
# Opt-in for the Eval-based auto-load of an installed extension (__init__).
AUTOLOAD_ENV = "WDOTOOL_GNOME_AUTOLOAD"
# org.gnome.Shell Mode values in which no user session runs extensions. The unlocked session's mode is NOT
# always "user": Ubuntu's is "ubuntu" (parent mode user), GNOME Classic's "classic" -- so the test is for the
# known no-extension modes, never `!= "user"` (that misreported a disabled extension as a locked screen,
# observed live on 24.04).
_LOCKED_MODES = {"unlock-dialog", "initial-setup"}
_GREETER_MODES = {"gdm"}

# Best-effort: load an installed-but-not-yet-loaded copy of the extension from inside the shell. Runs in
# shellDBus.js's module scope (Main, Gio, GLib imported there on 46 and 50); only reachable in unsafe mode.
_AUTOLOAD_JS = """\
(() => {
  const uuid = '%s';
  const em = Main.extensionManager;
  const G = (typeof GLib !== 'undefined') ? GLib : globalThis.imports.gi.GLib;
  let ext = em.lookup(uuid);
  let p = Promise.resolve();
  if (!ext) {
    const user = G.build_filenamev([G.get_user_data_dir(), 'gnome-shell', 'extensions', uuid]);
    const dirs = [[user, 2]].concat(G.get_system_data_dirs().map(
      d => [G.build_filenamev([d, 'gnome-shell', 'extensions', uuid]), 1]));
    for (const [d, type] of dirs) {
      const f = Gio.File.new_for_path(d);
      if (!f.query_exists(null))
        continue;
      ext = em.createExtensionObject(uuid, f, type);
      p = Promise.resolve(em.loadExtension(ext));
      break;
    }
  }
  if (!ext)
    return 'missing';
  return p.then(() => { em.enableExtension(uuid); return 'ok'; });
})()
""" % EXT_UUID


def _extension_dirs() -> list:
    """XDG data directories a copy of the bridge could live in, per-user first. The per-user one is the
    *session* user's, which under `sudo` is not ours -- the same rule `install-bridge.sh` applies with
    $SUDO_USER."""
    dirs = []
    uid = session.session_uid()
    try:
        home = pwd.getpwuid(os.getuid() if uid is None else uid).pw_dir
    except (KeyError, OverflowError, TypeError):
        home = os.environ.get("HOME") or ""
    if home:
        dirs.append(os.path.join(home, ".local", "share"))
    xdh = os.environ.get("XDG_DATA_HOME")
    if xdh and xdh not in dirs:
        dirs.append(xdh)
    system = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs.extend(d for d in system.split(":") if d and d not in dirs)
    return dirs


def extension_installed() -> str:
    """*Where* a copy of the bridge extension is on disk -- its directory -- or "" when there is none. The
    question `gnome/install-bridge.sh --check` answers with its `files:` line, asked of the same places and
    without a subprocess.

    Worth asking only on the error path, and there it decides which of two true sentences to print: behind the
    lock screen GNOME Shell disables every extension, so from the *bus* a bridge that was never installed and
    one that is merely asleep look exactly alike (live, 24.04). From disk they do not.

    The directory rather than a yes/no because the one message that needs the answer needs the path too: a copy
    the running shell has never heard of is waiting for a re-login, and a reader who is told which of the four
    possible directories holds it can check that the one they installed is the one being found."""
    for d in _extension_dirs():
        if os.path.isfile(os.path.join(d, _EXT_FILE)):
            return os.path.join(d, _EXT_SUBDIR)
    return ""


class GnomeBackend(WindowBackend):
    name = "gnome"

    def __init__(self, bus: Bus | None = None, names: list[str] | None = None, settle: float = 0.5):
        """`bus`/`names`: reuse backend_detect's connection and its ListNames result (one round trip per
        process). `settle`: how long activate() waits for the focus change to land before returning (Mutter
        animates and may defer focus; `key --window` injects 50 ms after activate)."""
        self.settle = settle
        if bus is None:
            try:
                bus = Bus()
            except DBusError as e:
                raise NoSessionError("gnome backend: %s" % no_bus_text(e)) from None
        self.bus = bus
        if names is None:
            try:
                names = self.bus.list_names()
            except DBusError as e:
                raise CmdError("gnome backend: ListNames failed: %s" % e) from None
        if BUS_NAME not in names:
            if SHELL_NAME not in names:
                raise NoSessionError("gnome backend: %s is not on the session "
                                     "bus (no GNOME session?)" % SHELL_NAME)
            if not (_autoload_wanted() and self._try_autoload()):
                # Every reason the bridge can be missing (not installed, not enabled, screen locked, greeter,
                # shell restarting) means the same thing to a script: there is no window backend to talk to yet.
                # That is rc 2, distinct from "no matching window" (rc 1) -- see B5 and "Session readiness
                # and exit codes" in docs/WDOTOOL.md.
                raise NoSessionError(self._missing_bridge_text())

    # -- plumbing -----------------------------------------------------------

    def _call(self, member: str, sig: str = "", args=(), timeout: float | None = CALL_TIMEOUT) -> tuple:
        try:
            return self.bus.call(BUS_NAME, OBJECT_PATH, IFACE, member, sig, args, timeout=timeout)
        except DBusError as e:
            raise self._map_error(member, e) from None
        except (ValueError, OverflowError, struct.error) as e:
            # An argument the wire format cannot carry (a negative or >64-bit window id reached us from
            # somewhere): one line, rc 1, never a marshalling traceback (B8).
            raise CmdError("gnome backend: %s: invalid argument: %s" % (member, e)) from None

    @staticmethod
    def _map_error(member: str, e: DBusError) -> CmdError:
        n = e.name
        if n.startswith(IFACE + "."):
            kind = n[len(IFACE) + 1:]
            if kind in ("NotFound", "Unsupported", "Cancelled"):
                err = CmdError(e.message or "%s: %s" % (member, kind))
                if kind == "Unsupported":
                    err.unsupported = True  # a capability gap, not a failure
                if kind == "Cancelled":
                    # SelectWindow only: Escape, the timeout, a shutdown --
                    # the user's answer was "no window", not a failure of ours
                    err.cancelled = True
                return err
            return CmdError("gnome backend: %s: %s" % (member, e.message or kind))
        if n in (ERR + "ServiceUnknown", ERR + "NameHasNoOwner"):
            return CmdError(_GONE)
        if n == ERR + "NoReply":
            return CmdError("gnome backend: %s: no reply from the bridge within "
                            "the timeout (is gnome-shell hung?)" % member)
        if n == ERR + "Disconnected":
            return CmdError("gnome backend: session bus connection lost (%s)" % e.message)
        return CmdError("gnome backend: %s failed: %s" % (member, e))

    def _missing_bridge_text(self) -> str:
        """Why is the bridge name missing? Extensions only run in the shell's `user` session mode (the name goes
        away behind the lock screen), and an installed extension may simply be disabled; both are readable by
        anyone on the bus. Falls back to the generic install hint.

        A locked screen is not the answer when the extension was never installed. Both are true then, and
        blaming the lock is the one a reader can act on least -- unlocking will not make the command work, and a
        first-time reader on a default desktop hits exactly that pair (the install guide's own `apt install`
        outlasts `idle-delay 300`; live on 24.04). The bus cannot tell the two apart, so the disk is asked, and
        the lock stays in the message as a clause."""
        try:
            mode = str(self.bus.get_property(SHELL_NAME, "/org/gnome/Shell",
                                             SHELL_NAME, "Mode",
                                             timeout=CALL_TIMEOUT) or "")
        except DBusError:
            mode = ""
        if mode in _GREETER_MODES:
            return ("gnome backend: the GNOME Shell on this session bus is the "
                    "GDM greeter ('%s' mode): nobody is logged in there, and "
                    "extensions do not run in the greeter" % mode)
        locked = mode in _LOCKED_MODES or self._screen_locked()
        where = extension_installed()
        if not where:
            return _HINT + (_ALSO_LOCKED if locked else "")
        if mode in _LOCKED_MODES:
            return ("gnome backend: the w11 bridge is unavailable while "
                    "GNOME Shell is in '%s' mode (screen locked?); extensions run "
                    "only in the unlocked session" % mode)
        if locked:
            return ("gnome backend: the w11 bridge is unavailable while "
                    "the screen is locked (GNOME Shell disables extensions "
                    "behind the lock screen); unlock the session")
        try:
            (info,) = self.bus.call(SHELL_NAME, "/org/gnome/Shell",
                                    SHELL_NAME + ".Extensions", "GetExtensionInfo",
                                    "s", (EXT_UUID,), timeout=CALL_TIMEOUT)
        except DBusError:
            info = {}
        if info:
            state = int(info.get("state", 0) or 0)
            if state == 1:
                return ("gnome backend: the w11 bridge extension reports "
                        "active but %s is not owned; gnome/install-bridge.sh --check"
                        % BUS_NAME)
            if state == 3:
                return ("gnome backend: the w11 bridge extension failed "
                        "to load: %s (gnome/install-bridge.sh --check)"
                        % (info.get("error") or "see journalctl --user _COMM=gnome-shell"))
            if state == 4:
                return self._out_of_date_text(info.get("shell-version"))
            return ("gnome backend: the w11 bridge extension is installed "
                    "but not enabled (state %d); run gnome/install-bridge.sh "
                    "(or: gnome-extensions enable %s)" % (state, EXT_UUID))
        # On disk, and the shell has never heard of the uuid: it has not rescanned the extension directories
        # since the copy appeared, which only a new session does. Live on 24.04 and 26.04 this is what an
        # `apt install w11` inside a running session looks like, and the old text sent the reader to
        # gnome/install-bridge.sh -- a script the .deb does not ship (F0.2) and which would change nothing here.
        return ("gnome backend: the w11 bridge extension is installed in %s but the running GNOME "
                "Shell has not loaded it (it knows nothing about %s): log out and back in" % (where, EXT_UUID))

    def _out_of_date_text(self, listed) -> str:
        """State 4 (OUT OF DATE): the shell refuses to load the extension because its own major is not in the
        extension's `shell-version`. Both halves of that have to be in the message -- which shell, and which
        majors the extension names -- because the fix is to add the one to the other.

        Measured on stonking-gnome (GNOME Shell 51.beta, 26.10, package route): the old text was `...marked out
        of date for this GNOME Shell (['45', ..., '50']); reinstall a matching gnome/ from the repo`, and the
        repo's gnome/ carries that same list, so the advice could not help anybody. Naming 51 and the list is
        what turns it into an instruction."""
        if isinstance(listed, (list, tuple)):
            names = ", ".join(str(x) for x in listed)
        else:
            names = str(listed)
        ver = self.compositor_version()
        major = str(ver[0]) if ver else ""
        add = ('add "%s" to' % major) if major else "add this shell's major to"
        return ("gnome backend: the w11 bridge extension is marked out of date for this GNOME Shell "
                "%s (it names %s); %s shell-version in the extension's metadata.json and log out and back in"
                % (major or "(the shell will not say which)", names or "nothing", add))

    def _screen_locked(self) -> bool:
        """org.gnome.ScreenSaver.GetActive(): public, unprivileged, true while the shell's lock screen is up.
        Needed besides `Mode`: GNOME 46 keeps the Mode property at the session mode ('ubuntu') while locked
        (observed live), only 50 reports 'unlock-dialog' there."""
        try:
            (active,) = self.bus.call(SCREENSAVER_NAME, "/org/gnome/ScreenSaver",
                                      SCREENSAVER_NAME, "GetActive",
                                      timeout=CALL_TIMEOUT)
        except (DBusError, ValueError):
            return False
        return bool(active)

    def _try_autoload(self) -> bool:
        """Eval-based load of an installed extension; False unless the shell is in unsafe mode and the load
        produced the bridge name. Opt-in only (WDOTOOL_GNOME_AUTOLOAD): Eval is a privileged interface and the
        common bridge-less case is "installed, needs a re-login", which must not send a round trip to it just to
        be refused."""
        try:
            ok, result = self.bus.call(SHELL_NAME, "/org/gnome/Shell", SHELL_NAME,
                                       "Eval", "s", (_AUTOLOAD_JS,), timeout=CALL_TIMEOUT)
        except DBusError:
            return False
        if not ok or "ok" not in str(result):
            return False
        deadline = time.monotonic() + AUTOLOAD_WAIT
        while time.monotonic() < deadline:
            try:
                if self.bus.name_has_owner(BUS_NAME):
                    return True
            except DBusError:
                return False
            time.sleep(0.1)
        return False

    def _json(self, member: str, sig: str = "", args=()):
        (raw,) = self._call(member, sig, args)
        try:
            return json.loads(raw)
        except ValueError:
            raise CmdError("gnome backend: %s returned malformed JSON" % member) from None

    def _raw_list(self) -> "list[dict]":
        data = self._json("ListWindows")
        return data if isinstance(data, list) else []

    def _raw_get(self, wid: int) -> dict:
        return self._json("GetWindow", "t", (wid,))

    @staticmethod
    def _win(d: dict) -> Window:
        hidden = bool(d.get("hidden", False))
        on_active = bool(d.get("on_active_workspace", True))
        return Window(
            id=int(d.get("id", 0)),
            title=d.get("title") or "",
            class_=d.get("wm_class") or d.get("gtk_app_id")
            or d.get("sandboxed_app_id") or "",
            instance=d.get("wm_class_instance") or "",
            pid=int(d.get("pid") or 0),
            x=int(d.get("x", 0)), y=int(d.get("y", 0)),
            w=int(d.get("width", 0)), h=int(d.get("height", 0)),
            focused=bool(d.get("focused", False)),
            visible=(not hidden) and on_active,
            desktop=int(d.get("workspace", -1)),
            window_type=d.get("window_type") or "NORMAL",
        )

    @classmethod
    def _view(cls, d: dict) -> View:
        win = cls._win(d)
        client = d.get("client_type") or "wayland"
        wm_class = d.get("wm_class") or ""
        app_id = d.get("gtk_app_id") or (wm_class if client == "wayland" else "")
        return View(
            window=win,
            xid=int(d.get("xid") or 0),
            instance=d.get("wm_class_instance") or wm_class or win.class_,
            cls=wm_class or win.class_,
            app_id=app_id,
            fullscreen=bool(d.get("fullscreen")),
            maximized_h=bool(d.get("maximized_h")),
            maximized_v=bool(d.get("maximized_v")),
            above=bool(d.get("above")),
            sticky=bool(d.get("on_all_workspaces")),
            urgent=bool(d.get("urgent")),
            minimized=bool(d.get("minimized")),
            hidden=bool(d.get("hidden")),
            skip_taskbar=bool(d.get("skip_taskbar")),
            floating=True,
            ws_name="",
            window_type=win.window_type,
            client_type=client,
            role=d.get("role") or "",
            desktop_id=d.get("desktop_id") or "",
            monitor=int(d.get("monitor", -1)),
            transient_for=int(d.get("transient_for") or 0),
            decorated=bool(d.get("decorated", True)),
        )

    def _wait_focused(self, wid: int):
        """activate()/focus() return before Mutter has moved the focus; give
        it `settle` seconds so `key --window` lands in the right window."""
        deadline = time.monotonic() + self.settle
        while time.monotonic() < deadline:
            try:
                if self._raw_get(wid).get("focused"):
                    return
            except CmdError:
                return
            time.sleep(0.03)

    # -- WindowBackend ------------------------------------------------------

    def list(self) -> list[Window]:
        return [self._win(d) for d in self._raw_list()]

    def find(self, wid: int) -> Window:
        return self._win(self._raw_get(wid))

    def activate(self, wid: int):
        self._call("Activate", "t", (wid,))
        self._wait_focused(wid)

    def focus(self, wid: int):
        self._call("Focus", "t", (wid,))
        self._wait_focused(wid)

    def close(self, wid: int):
        self._call("Close", "t", (wid,))

    def kill(self, wid: int):
        # Mutter kills the client itself (XKillClient for X11, SIGKILL on the
        # pid for Wayland) -- works for any client whatever uid we run as.
        self._call("Kill", "t", (wid,))

    def minimize(self, wid: int):
        self._call("Minimize", "t", (wid,))

    def map(self, wid: int):
        self._call("Unminimize", "t", (wid,))

    def unmap(self, wid: int):
        self._call("Minimize", "t", (wid,))

    def is_mapped(self, wid: int) -> bool:
        return not self._raw_get(wid).get("minimized", False)

    def raise_(self, wid: int):
        self._call("Raise", "t", (wid,))

    def lower(self, wid: int):
        self._call("Lower", "t", (wid,))

    def move_window(self, wid: int, x: int, y: int):
        self._call("Move", "tii", (wid, x, y))

    def resize(self, wid: int, w: int, h: int):
        self._call("Resize", "tii", (wid, w, h))

    def set_state(self, wid: int, state: str, action: int):
        word = _ACTIONS.get(action)
        if word is None:
            raise CmdError("windowstate: bad action %r" % (action,))
        (applied,) = self._call("SetState", "tss", (wid, state, word))
        if applied:
            return
        if state in _COSMETIC_STATES:
            warn("windowstate %s: Mutter cannot set it on Wayland; ignoring" % state)
            return
        raise CmdError("windowstate %s is not supported by the gnome backend (%s)"
                       % (state, _GAP_REASONS.get(state, "Mutter has no API for it")))

    def maximize_pair_state(self) -> str:
        """"MAXIMIZED": the bridge's name for both axes at once, which reaches Mutter as one
        set_maximize_flags()/set_unmaximize_flags() call carrying both direction bits. Two single-axis calls do
        not add up to it (they corrupt the saved rectangle); every bridge version has accepted the name."""
        return "MAXIMIZED"

    def unsupported_states(self) -> "set[str]":
        """_NET_WM_STATE names the bridge answers "not applied" to: Mutter exports no Wayland setter for them,
        so they are cosmetic no-ops here. wwmctl asks so it knows when to reach an XWayland window through the X
        server instead, where Mutter honours them."""
        return set(_COSMETIC_STATES) | set(_GAP_REASONS)

    def window_desktop(self, wid: int) -> int:
        return self.find(wid).desktop

    def set_window_desktop(self, wid: int, n: int):
        self._call("MoveToWorkspace", "ti", (wid, n))

    def get_desktop(self) -> int:
        return int(self._call("GetActiveWorkspace")[0])

    def set_desktop(self, n: int):
        self._call("SetActiveWorkspace", "i", (n,))

    def num_desktops(self) -> int:
        return int(self._call("GetNWorkspaces")[0])

    # The bridge version that made SelectWindow a click-to-pick; v1 waited
    # for a focus change and would hang on the already-focused window.
    _SELECT_MIN_VERSION = 2

    def select_window(self) -> int:
        """xdotool selectwindow: the window under the pointer at the next button press. The bridge takes a stage
        grab and answers when the user clicks; Escape (and the bridge's own timeout) come back as `.Cancelled`,
        which is rc 1 with a reason, like KWin's picker.

        The D-Bus call has no timeout of its own on purpose -- the wait is as long as the user takes -- but the
        bridge always answers, so it cannot hang here either."""
        version = 0
        try:
            version = self.bridge_version()
        except CmdError:
            pass  # too old to ask, or gone: the call below reports it
        if 0 < version < self._SELECT_MIN_VERSION:
            raise CmdError(
                "selectwindow: the installed w11 bridge is version %d, "
                "which can only wait for a focus change (clicking the window "
                "that already has focus would never return). Reinstall it with "
                "gnome/install-bridge.sh and log back in." % version)
        try:
            (wid,) = self._call("SelectWindow", "u", (0,), timeout=None)
        except CmdError as e:
            if getattr(e, "cancelled", False):
                raise CmdError("selectwindow: %s" % e) from None
            raise
        if not wid:
            raise CmdError("selectwindow: no window under the pointer")
        return int(wid)

    def display_size(self) -> tuple[int, int]:
        w, h = self._call("DisplaySize")
        if w <= 0 or h <= 0:
            raise CmdError("gnome backend: display size unknown")
        return int(w), int(h)

    # -- optional hooks -----------------------------------------------------

    def views(self) -> "list[View]":
        names = {}
        try:
            names = {w.index: w.name for w in self.workspaces()}
        except CmdError:
            pass
        out = []
        for d in self._raw_list():
            v = self._view(d)
            v.ws_name = names.get(v.window.desktop, "")
            out.append(v)
        return out

    def workspaces(self) -> "list[Workspace]":
        out = []
        for d in self._json("ListWorkspaces") or []:
            wa = d.get("work_area") or {}
            out.append(Workspace(
                index=int(d.get("index", len(out))),
                name=d.get("name") or "",
                active=bool(d.get("active")),
                work_area=(int(wa.get("x", 0)), int(wa.get("y", 0)),
                           int(wa.get("width", 0)), int(wa.get("height", 0))),
            ))
        return out

    def x_info(self) -> tuple[str, str] | None:
        """(DISPLAY, XAUTHORITY) of Xwayland: what gnome-shell itself has in its environment (the bridge reads
        it), each blank filled from the session scan (session.find_x_display / find_xauthority)."""
        display, xauth = "", ""
        try:
            display, xauth = self._call("XInfo")
        except CmdError:
            pass
        uid = None
        try:
            uid = self.bus._owner_uid()
        except Exception:  # diagnostics only
            uid = None
        display = display or session.find_x_display(uid) or ""
        xauth = xauth or session.find_xauthority(uid) or ""
        if not display and not xauth:
            return None
        return display, xauth

    def events(self, timeout: float | None = None, workspaces: bool = False):
        """(id, change) for every bridge WindowEvent, on a connection of its own so queued signals never pile up
        behind the command connection. With `workspaces` the bridge's WorkspaceEvent (switch/add/remove) is
        folded in as (0, "workspace") -- no window has id 0 -- so a root-level watcher (wxprop -root -spy) needs
        one stream only."""
        bus = Bus(self.bus.address)
        try:
            bus.add_match("type='signal',interface='%s',path='%s'" % (IFACE, OBJECT_PATH))
            for m in bus.messages(timeout):
                if m.interface != IFACE:
                    continue
                if m.member == "WindowEvent":
                    wid, change = m.args()
                    yield int(wid), str(change)
                elif workspaces and m.member == "WorkspaceEvent":
                    yield 0, "workspace"
        finally:
            bus.close()

    # -- extras for the other tools ---------------------------------------

    # wmctrl -m: what Mutter writes into _NET_SUPPORTING_WM_CHECK's _NET_WM_NAME on the X root, so the answer is
    # the same with or without Xwayland running (it is spawned on demand and must not be started just to be
    # asked its name).
    wm_name = "GNOME Shell"

    def show_desktop(self, show: bool):
        """wmctrl -k on|off. Mutter's own show-desktop mode has no public API; the bridge minimizes every normal
        window on the active workspace and remembers them for `off` (gnome/README.md)."""
        self._call("ShowDesktop", "b", (bool(show),))

    def set_num_desktops(self, n: int):
        """wmctrl -n: only with static workspaces; with dynamic workspaces (GNOME's default) the bridge answers
        Unsupported (a CmdError the caller turns into wmctrl's "the WM may ignore the request"). _map_error
        marks that answer .unsupported, so wdotool's set_num_desktops warns instead of failing a chain (B9)."""
        self._call("SetNWorkspaces", "i", (int(n),))

    def monitors(self) -> "list[dict]":
        """[{index, x, y, width, height, scale, primary, connector}] --
        `connector` is "" on GNOME 46 (no JS route), filled on 49+."""
        data = self._json("ListMonitors")
        return data if isinstance(data, list) else []

    def pointer(self) -> tuple[int, int] | None:
        """The compositor's real pointer (B6). Mutter knows where the pointer is whoever moved it -- our tablet,
        a REL event, a physical mouse or another wdotool daemon -- so getmouselocation reports this and the
        input daemon's model is corrected from it before a relative move."""
        x, y, _mods = self._call("GetPointer")
        return int(x), int(y)

    # Historical name kept for the diagnostics scripts in vm/ and gnome/.
    real_pointer = pointer

    def bridge_version(self) -> int:
        return int(self._call("GetVersion")[0])

    def compositor_version(self) -> tuple:
        """GNOME Shell's version as a tuple of ints -- (46, 0), (50, 1) -- or () when the shell will not say.
        Mutter's behaviour differs between releases in ways a wmctrl clone has to follow (see wwmctl's -e
        gravity), and this is the only version anyone can ask for: the read-only `ShellVersion` property of
        org.gnome.Shell."""
        cached = getattr(self, "_comp_version", None)
        if cached is not None:
            return cached
        out = ()
        try:
            raw = self.bus.get_property(SHELL_NAME, "/org/gnome/Shell",
                                        SHELL_NAME, "ShellVersion",
                                        timeout=CALL_TIMEOUT)
            parts = []
            for chunk in str(raw or "").split("."):
                m = re.match(r"\d+", chunk)
                if not m:
                    break
                parts.append(int(m.group(0)))
            out = tuple(parts)
        except Exception:
            out = ()
        self._comp_version = out
        return out


def _autoload_wanted() -> bool:
    v = os.environ.get(AUTOLOAD_ENV, "").strip().lower()
    return bool(v) and v not in ("0", "no", "false", "off")

