"""The one KWin script wdotool loads, as a string.

It is pushed into KWin with org.kde.kwin.Scripting.loadScript(), which is
unprivileged on Plasma 5.27 and 6 alike -- nothing has to be installed for
the user, unlike the GNOME bridge extension. KWin gives the script a fresh
QJSEngine with `workspace`, `options`, the `KWin` enums, QTimer and exactly
these globals: readConfig, callDBus, registerShortcut, register*ScreenEdge,
registerUserActionsMenu (+ console.*). There is no print(), no way to return
a value from run() and no way to register a D-Bus object, so results go out
through callDBus() to a name the Python side owns.

The script is a single constant: the caller prepends one `var A = {...}`
line (see backend_kwin._source) naming the destination, a per-call token and
the operation. Every path answers exactly once -- a JS exception is caught
and reported as {ok:false}, because KWin logs script errors to the journal
only and still replies OK to run().

Version differences live here, not in Python:
  windowList()/clientList(), activeWindow/activeClient, currentDesktop as a
  VirtualDesktop object (6) or a 1-based int (5.27), w.desktops[] (6) vs
  w.desktop (5.27), captionNormal (6) vs caption, w.windowId (X11 windows on
  5.27, gone on 6), w.shade (5.27, shading removed in 6), workspace.
  raiseWindow (6, absent on 5.27), w.maximizeMode (a Q_PROPERTY on 6 only --
  5.27's window.h declares none, so there the mode is read off the geometry),
  w.output (KWin::Output* -- a datatype QJSEngine does not know on 5.27, so
  reading it at all logs a QMetaProperty::read warning per window).

A state write is answered only once the window agrees: a Wayland client takes
a new size when it acks the configure, so `fullScreen`/`maximizeMode` read
back stale for a few milliseconds right after the write and warning on that
read is a false alarm. When the immediate read-back disagrees the script
arms the window's own change signal plus a QTimer backstop (both exist in
KWin's script engine on 5.27 and 6) and answers from whichever comes first --
which also means the *next* command sees a settled window.
"""

SCRIPT = r"""
/* ---------------------------------------------------------------- plumbing */
var _done = false;
var _keep = [];   /* a QTimer nothing references is collected by the engine */
var DEFER = {defer: true};   /* main()'s "I will answer later" */

function _ret(x) {
  /* exactly once: a deferred answer and its backstop timer race each other,
     and the loser must not send a second Result under the same token. */
  if (_done) {
    return;
  }
  _done = true;
  callDBus(A.dest, A.path, A.iface, "Result", A.token, JSON.stringify(x));
}
function _ev(u, c) {
  callDBus(A.dest, A.path, A.iface, "Event", A.token, u, c);
}
var SIX = (typeof workspace.windowList === "function");

function xnum(w) {
  /* The X11 window id, and 0 for a native Wayland toplevel: `windowId` is a
     property of X11Window only, so 5.27 answers with a number and 6 with
     undefined for a native window. Whether a window is on the X plane is
     what decides which of KWin's capability gaps apply to it (shading). */
  var x = w.windowId;
  return (typeof x === "number" && x > 0) ? x : 0;
}

function wins() {
  var l = SIX ? workspace.windowList() : workspace.clientList();
  return l ? l : [];
}
function uuid(w) {
  return String(w.internalId).replace(/[{}]/g, "").toLowerCase();
}
function num(v) {
  return (typeof v === "number" && isFinite(v)) ? Math.round(v) : 0;
}
function str(v) {
  return (typeof v === "string") ? v : (v === undefined || v === null ? "" : String(v));
}
function desktopList() {
  /* 6: list<VirtualDesktop>; 5.27: an int count. */
  var d = workspace.desktops;
  return (d && typeof d.length === "number") ? d : null;
}
function dnum(w) {                       /* 0-based index, -1 sticky/unknown */
  if (w.onAllDesktops) {
    return -1;
  }
  if (SIX) {
    var d = w.desktops;
    return (d && d.length) ? num(d[0].x11DesktopNumber) - 1 : -1;
  }
  return w.desktop > 0 ? w.desktop - 1 : -1;
}
function oncur(w) {
  if (w.onAllDesktops) {
    return true;
  }
  var c = workspace.currentDesktop;
  if (SIX) {
    var d = w.desktops;
    if (!d) {
      return false;
    }
    for (var i = 0; i < d.length; i++) {
      if (String(d[i].id) === String(c.id)) {
        return true;
      }
    }
    return false;
  }
  return w.desktop === c;
}
function title(w) {
  if (typeof w.captionNormal === "string") {
    return w.captionNormal;                   /* 6.x: no suffix, by design */
  }
  /* 5.27 has no captionNormal property. caption is captionNormal plus KWin's
     disambiguation suffix for duplicate titles: " <2>" and a U+200E left-to-
     right mark, which no client ever puts at the end of its own title. */
  return str(w.caption).replace(/ <\d+>\u200e$/, "");
}
function find(u) {
  var l = wins();
  for (var i = 0; i < l.length; i++) {
    if (uuid(l[i]) === u) {
      return l[i];
    }
  }
  return null;
}

/* ------------------------------------------------------------------- facts */
function maxArea(w) {
  /* clientArea(option, window) is the one overload both releases have. */
  var opt = (typeof KWin !== "undefined" && KWin.MaximizeArea !== undefined)
            ? KWin.MaximizeArea : 2;
  try {
    var r = workspace.clientArea(opt, w);
    return (r && typeof r.width === "number") ? r : null;
  } catch (e) {
    return null;
  }
}
function covers(gp, gs, ap, as) {
  /* Is the window maximized along this axis, judged by geometry alone?
     It has to sit inside the maximize area -- KWin never puts a maximized
     window outside it -- and fall short of it by less than one size
     increment. Not "equal to the area": KWin honours an X11 client's size
     hints when it maximizes it and centres the remainder, so a maximized
     xterm is 1918x1033 at (1,0) in a 1920x1036 area. The slack is what
     tells that from a window the user merely sized large. */
  var slack = Math.max(32, Math.round(as * 0.02));
  return gp >= ap - 2 && gp + gs <= ap + as + 2 && (as - gs) <= slack;
}
function mmode(w) {
  /* 1 = vertical, 2 = horizontal, as KWin::MaximizeMode.
     Plasma 6 has the property. Plasma 5.27's window.h declares no
     maximizeMode Q_PROPERTY at all, so `w.maximizeMode` is undefined there
     and reading it as 0 would (a) report every maximized window as restored
     and (b) clear the other axis on every setMaximize(). Off the geometry
     instead, against the area KWin would maximize this window into. */
  var m = w.maximizeMode;
  if (typeof m === "number") {
    return m;
  }
  if (w.fullScreen) {
    return 0;   /* a fullscreen frame is larger than the maximize area and
                   says nothing about what is underneath it */
  }
  var a = maxArea(w), g = w.frameGeometry;
  if (!a || !g) {
    return 0;
  }
  var out = 0;
  if (covers(num(g.y), num(g.height), num(a.y), num(a.height))) {
    out |= 1;
  }
  if (covers(num(g.x), num(g.width), num(a.x), num(a.width))) {
    out |= 2;
  }
  return out;
}
function info(w) {
  var g = w.frameGeometry || {};
  var xid = xnum(w);
  var tf = w.transientFor;
  /* SIX: on 5.27 `output` is a KWin::Output*, a datatype QJSEngine has no
     converter for -- merely reading it logs "QMetaProperty::read: Unable to
     handle unregistered datatype" into the journal, once per window. */
  var out = SIX ? w.output : null;
  return {
    u: uuid(w), t: title(w), c: str(w.resourceClass), n: str(w.resourceName),
    p: num(w.pid),
    x: num(g.x), y: num(g.y), w: num(g.width), h: num(g.height),
    f: !!w.active, m: !!w.minimized, hi: !!w.hidden,
    d: dnum(w), oc: oncur(w), st: !!w.onAllDesktops,
    fs: !!w.fullScreen, mm: mmode(w),
    ka: !!w.keepAbove, kb: !!w.keepBelow,
    sk: !!w.skipTaskbar, sp: !!w.skipPager, at: !!w.demandsAttention,
    nb: !!w.noBorder, sh: (typeof w.shade === "boolean") ? w.shade : null,
    ty: num(w.windowType), ly: num(w.layer), so: num(w.stackingOrder),
    tf: tf ? uuid(tf) : "", df: str(w.desktopFileName), ro: str(w.windowRole),
    xid: xid,
    o: (out && out.name) ? str(out.name) : ""
  };
}
function listAll() {
  var l = wins(), out = [];
  for (var i = 0; i < l.length; i++) {
    var w = info(l[i]);
    /* Position in workspace.windowList(), kept because the sort below
       destroys it: windowList() is KWin's m_windows, and _NET_CLIENT_LIST
       is m_windows with everything but the managed X11 windows dropped
       (Workspace::propagateWindows). The two orders are therefore the same
       order, which is what tells two identical windows of one client apart
       on 6, where nothing carries the X id. */
    w.ix = i;
    out.push(w);
  }
  out.sort(function (a, b) { return a.so - b.so; });   /* bottom -> top */
  return out;
}
function screenInfo() {
  var s = workspace.virtualScreenSize || {};
  var wa = (typeof KWin !== "undefined" && KWin.WorkArea !== undefined)
           ? KWin.WorkArea : 5;
  var areas = [], d = desktopList();
  var n = d ? d.length : num(workspace.desktops);
  for (var i = 0; i < n; i++) {
    var r = null;
    try {
      r = SIX ? workspace.clientArea(wa, workspace.activeScreen, d[i])
              : workspace.clientArea(wa, workspace.activeScreen, i + 1);
    } catch (e) {
      r = null;
    }
    areas.push(r ? [num(r.x), num(r.y), num(r.width), num(r.height)]
                 : [0, 0, 0, 0]);
  }
  return {w: num(s.width), h: num(s.height), areas: areas};
}

function cursorPos() {
  /* workspace.cursorPos: a QPoint in global layout coordinates, on 5.27 and
     6 alike -- the compositor's own pointer, wherever it was last moved from
     (our tablet, a REL event, a physical mouse, another daemon). */
  var c = workspace.cursorPos;
  if (!c || typeof c.x !== "number" || typeof c.y !== "number") {
    throw new Error("nocursor");
  }
  return {x: num(c.x), y: num(c.y)};
}

/* ----------------------------------------------------------------- actions */
function unclamp(w) {
  /* moveResize() is clamped or ignored for maximized, quick-tiled, custom-
     tiled and fullscreen windows: undo those first, or the write is a no-op.
     Unconditionally, not `if (w.maximizeMode)`: that property does not exist
     on 5.27, where the test was always false and a maximized window kept its
     _NET_WM_STATE_MAXIMIZED_* while being resized out from under it.
     setMaximize(false, false) on a restored window is a no-op. */
  try {
    w.setMaximize(false, false);
  } catch (e) { }
  try {
    if (w.tile) {
      w.tile = null;
    }
  } catch (e) { }
  try {
    if (w.fullScreen) {
      w.fullScreen = false;
    }
  } catch (e) { }
}
function geom(w, x, y, ww, hh) {
  unclamp(w);
  var g = w.frameGeometry;
  w.frameGeometry = {
    x: (x === null) ? g.x : x, y: (y === null) ? g.y : y,
    width: (ww === null) ? g.width : ww, height: (hh === null) ? g.height : hh
  };
  var r = w.frameGeometry;
  return {x: num(r.x), y: num(r.y), w: num(r.width), h: num(r.height)};
}
function activate(w) {
  if (SIX) {
    workspace.activeWindow = w;
  } else {
    workspace.activeClient = w;
  }
}
function toDesktop(w, n) {
  if (n < 0) {
    w.onAllDesktops = true;
    return;
  }
  var d = desktopList();
  if (SIX) {
    if (!d || n >= d.length) {
      throw new Error("nodesktop");
    }
    w.onAllDesktops = false;
    w.desktops = [d[n]];
  } else {
    if (n >= num(workspace.desktops)) {
      throw new Error("nodesktop");
    }
    w.onAllDesktops = false;
    w.desktop = n + 1;
  }
}
function readState(w, s) {
  if (s === "MAXIMIZED_VERT") { return !!(mmode(w) & 1); }
  if (s === "MAXIMIZED_HORZ") { return !!(mmode(w) & 2); }
  var name = _STATE_PROPS[s];
  if (!name) {
    throw new Error("nostate");
  }
  if (typeof w[name] !== "boolean") {
    throw new Error(s === "SHADED" ? "noshade" : "nostate");
  }
  return w[name];
}
var _STATE_PROPS = {
  FULLSCREEN: "fullScreen", HIDDEN: "minimized", ABOVE: "keepAbove",
  BELOW: "keepBelow", STICKY: "onAllDesktops", SKIP_TASKBAR: "skipTaskbar",
  SKIP_PAGER: "skipPager", DEMANDS_ATTENTION: "demandsAttention",
  SHADED: "shade"
};
function setBool(w, s, v) {
  /* Assigning a property the window does not have (shade on Plasma 6, which
     dropped shading) silently creates a plain JS property on the wrapper and
     even reads back -- so refuse before writing, never after. */
  var name = _STATE_PROPS[s];
  if (typeof w[name] !== "boolean") {
    throw new Error(s === "SHADED" ? "noshade" : "nostate");
  }
  w[name] = v;
}
function writeState(w, s, v) {
  /* setMaximize takes both axes at once, so one axis is always a read-
     modify-write of the other -- through mmode(), never the raw property. */
  if (s === "MAXIMIZED_VERT") {
    w.setMaximize(v, !!(mmode(w) & 2));
    return;
  }
  if (s === "MAXIMIZED_HORZ") {
    w.setMaximize(!!(mmode(w) & 1), v);
    return;
  }
  if (!_STATE_PROPS[s]) {
    throw new Error("nostate");
  }
  setBool(w, s, v);
}
/* The signals that say "this state has landed", best first. A Wayland client
   applies a size-changing state only when it acks the configure, so the read
   right after the write is stale and warning on it is a false alarm. */
var _WATCH = {
  FULLSCREEN: ["fullScreenChanged", "frameGeometryChanged"],
  MAXIMIZED_VERT: ["maximizedChanged", "clientMaximizedStateChanged",
                   "frameGeometryChanged"],
  MAXIMIZED_HORZ: ["maximizedChanged", "clientMaximizedStateChanged",
                   "frameGeometryChanged"],
  HIDDEN: ["minimizedChanged"],
  ABOVE: ["keepAboveChanged"],
  BELOW: ["keepBelowChanged"],
  STICKY: ["desktopsChanged", "desktopChanged"],
  SKIP_TASKBAR: ["skipTaskbarChanged"],
  SKIP_PAGER: ["skipPagerChanged"],
  DEMANDS_ATTENTION: ["demandsAttentionChanged"],
  SHADED: ["shadeChanged"]
};
function later(w, s, want) {
  /* Answer the state op once the window agrees, or once the backstop timer
     gives up -- whichever comes first. Returns false when neither could be
     armed; then the caller answers with the read it already has. */
  var sigs = _WATCH[s] || [], armed = false, t = null;
  function reply(settled) {
    try {
      _ret({ok: true, v: {applied: readState(w, s), settled: !!settled,
                          xid: xnum(w)}});
    } catch (e) {
      _ret({ok: false, err: (e && e.message) ? String(e.message) : String(e)});
    }
  }
  function check() {
    try {
      if (readState(w, s) !== want) {
        return;
      }
    } catch (e) {
      return;
    }
    if (t) {
      try { t.stop(); } catch (e2) { }
    }
    reply(true);
  }
  for (var i = 0; i < sigs.length; i++) {
    if (on(w, sigs[i], check)) {
      armed = true;
    }
  }
  try {
    t = new QTimer();                    /* KWin::ScriptTimer, both releases */
    t.singleShot = true;
    t.interval = num(A.settle) > 0 ? num(A.settle) : 1000;
    t.timeout.connect(function () { reply(true); });
    t.start();
    _keep.push(t);
  } catch (e) {
    t = null;
  }
  return armed || t !== null;
}

/* ------------------------------------------------------------------ events */
function on(obj, name, fn) {
  try {
    var s = obj[name];
    if (s && typeof s.connect === "function") {
      s.connect(fn);
      return true;
    }
  } catch (e) { }
  return false;
}
function hook(w) {
  var u = uuid(w);
  function ev(c) {
    return function () { _ev(u, c); };
  }
  on(w, "captionChanged", ev("title"));
  on(w, "minimizedChanged", ev("minimized"));
  on(w, "fullScreenChanged", ev("fullscreen_mode"));
  on(w, "desktopsChanged", ev("workspace"));            /* 6 */
  on(w, "desktopChanged", ev("workspace"));             /* 5.27 */
  on(w, "frameGeometryChanged", ev("move"));
  on(w, "demandsAttentionChanged", ev("urgent"));
}
function hookEvents() {
  var l = wins();
  for (var i = 0; i < l.length; i++) {
    hook(l[i]);
  }
  function added(w) {
    if (w) {
      _ev(uuid(w), "new");
      hook(w);
    }
  }
  function removed(w) {
    if (w) {
      _ev(uuid(w), "close");
    }
  }
  function activated(w) {
    if (w) {
      _ev(uuid(w), "focus");
    }
  }
  on(workspace, "windowAdded", added);                  /* 6 */
  on(workspace, "clientAdded", added);                  /* 5.27 */
  on(workspace, "windowRemoved", removed);
  on(workspace, "clientRemoved", removed);
  on(workspace, "windowActivated", activated);
  on(workspace, "clientActivated", activated);
  function ws() {
    _ev("", "workspace");
  }
  on(workspace, "currentDesktopChanged", ws);
  on(workspace, "desktopsChanged", ws);                 /* 6 */
  on(workspace, "numberDesktopsChanged", ws);           /* 5.27 */
  watchOwner();
  return {hooked: l.length};
}

/* This is the one script that stays loaded, and only the wdotool that loaded
   it can unload it -- by a pluginName that is random per call and dies with
   the process. A Ctrl-C, an OOM kill or a crash therefore used to leave it
   running inside KWin for the rest of the session, connected to every
   window's signals and sending a D-Bus call per frame of every drag, with no
   way for anyone to name it again. So it watches its owner: a Ping every
   WATCH_MS, and after WATCH_MISSES rounds with no answer it unloads itself.
   The Python side answers every method call on that connection, so an owner
   that is alive but busy just answers late (the counter resets on any
   reply). Wrapped: without QTimer this is exactly the old behaviour. */
var WATCH_MS = 10000;
var WATCH_MISSES = 3;
function watchOwner() {
  if (!A.plugin) {
    return;
  }
  var alive = true, missed = 0;
  try {
    var wd = new QTimer();
    wd.interval = WATCH_MS;
    wd.timeout.connect(function () {
      missed = alive ? 0 : missed + 1;
      if (missed >= WATCH_MISSES) {
        wd.stop();
        callDBus("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
                 "unloadScript", String(A.plugin));
        return;
      }
      alive = false;
      callDBus(A.dest, A.path, A.iface, "Ping", A.token,
               function () { alive = true; });
    });
    wd.start();
    _keep.push(wd);
  } catch (e) { }
}

/* -------------------------------------------------------------------- main */
function main() {
  var op = A.op;
  if (op === "list") {
    return listAll();
  }
  if (op === "screen") {
    return screenInfo();
  }
  if (op === "events") {
    return hookEvents();
  }
  if (op === "cursor") {
    return cursorPos();
  }
  var w = find(A.uuid);
  if (!w) {
    throw new Error("nowindow");
  }
  if (op === "activate") {
    w.minimized = false;
    if (!oncur(w)) {
      toCurrent(w);
    }
    activate(w);
    return {f: !!w.active};
  }
  if (op === "focus") {
    activate(w);
    return {f: !!w.active};
  }
  if (op === "close") {
    w.closeWindow();
    return {};
  }
  if (op === "minimize") {
    w.minimized = true;
    return {};
  }
  if (op === "unminimize") {
    w.minimized = false;
    return {};
  }
  if (op === "raise") {
    /* A lower of a non-active window is approximated with keepBelow, and
       that flag outlives the process: without clearing it here a raise
       could not undo a lower, and the window stayed pinned to the bottom
       for the rest of the session with nothing saying so. Mirrors lower's
       own `w.keepAbove = false`. */
    w.keepBelow = false;
    if (typeof workspace.raiseWindow === "function") {
      workspace.raiseWindow(w);                         /* 6: a real raise */
      return {how: "raise"};
    }
    if (w.active) {
      workspace.slotWindowRaise();
      return {how: "raise"};
    }
    activate(w);                                        /* 5.27: no API */
    return {how: "activate"};
  }
  if (op === "lower") {
    if (w.active) {
      workspace.slotWindowLower();                      /* a real lower */
      return {how: "lower"};
    }
    w.keepAbove = false;
    w.keepBelow = true;                                 /* approximation */
    return {how: "keepBelow"};
  }
  if (op === "geometry") {
    return geom(w, A.x, A.y, A.w, A.h);
  }
  if (op === "state") {
    var want = (A.action === 2) ? !readState(w, A.state) : (A.action === 1);
    writeState(w, A.state, want);
    var got = readState(w, A.state);
    if (got === want) {
      return {applied: got, settled: true, xid: xnum(w)};  /* sync, as X11 is */
    }
    if (later(w, A.state, want)) {
      return DEFER;
    }
    /* unverifiable: do not warn on it */
    return {applied: got, settled: false, xid: xnum(w)};
  }
  if (op === "desktop") {
    toDesktop(w, A.n);
    return {d: dnum(w)};
  }
  if (op === "info") {
    return info(w);
  }
  throw new Error("noop");
}
function toCurrent(w) {
  /* activate() on a window parked elsewhere: bring its desktop up first, so
     `windowactivate` lands the window on screen like xdotool's does. */
  var n = dnum(w);
  if (n < 0) {
    return;
  }
  var d = desktopList();
  if (SIX) {
    if (d && n < d.length) {
      workspace.currentDesktop = d[n];
    }
  } else {
    workspace.currentDesktop = n + 1;
  }
}

try {
  var _v = main();
  if (_v !== DEFER) {                 /* DEFER: a signal or the timer answers */
    _ret({ok: true, v: _v});
  }
} catch (e) {
  _ret({ok: false, err: (e && e.message) ? String(e.message) : String(e)});
}
"""
