"""The JS programs wdotool sends to Cinnamon, one per operation, as strings.

Cinnamon exports `org.Cinnamon.Eval(s) -> (b, s)` on the session bus: `usr/share/cinnamon/js/ui/
cinnamonDBus.js` implements it as a bare `JSON.stringify(eval(code))`, and there is no unsafe-mode gate
anywhere in Cinnamon's JS tree (`grep -rn unsafe usr/share/cinnamon/js/` finds nothing) -- unlike GNOME,
where the same method exists and is off. So the window plane needs no extension installed: this is KWin's
`loadScript` story, not GNOME's bridge story. Measured on a live Cinnamon 6.4.13, ~9 ms per call including a
`gdbus` process spawn, which over dbus_mini is less [M recon2/cinnamon.md §2.2].

Shaped like wdotool/kwin_js.py -- the compositor-version knowledge lives here, in the scripts, and not in the
Python. What is here rather than in muffin's own API:

* `global.display.list_windows(0)`, not `list_all_windows`: muffin has `meta_display_list_windows(display,
  flags)` and no `meta_display_list_all_windows` at all (checked in `meta/display.h` and in `Meta-0.typelib`)
  [M cinnamon.md §2.3]. `sort_windows_by_stacking` then gives the bottom-to-top order backend.hit_test()
  wants.
* the id is `get_stable_sequence()` -- small and dense, X-id-shaped, the same shape sway's ids have.
  `get_id()` is a ~3e9 counter (two windows opened together measured 3070932382 and 2959920136), which is
  unique but reads like nothing any of these tools has ever printed [M cinnamon.md §2.3].
* the pid is `get_pid()` when it is positive and `get_client_pid()` otherwise: `get_pid()` reads
  `_NET_WM_PID` and answers -1 for a native Wayland window, where `get_client_pid()` answers (measured: the
  `foot` record carried `"pid":-1,"cpid":86275`) [M cinnamon.md §2.3].
* `get_xwindow()` is the real X id for an X client and 0 for a native one, so `views()` needs no matching
  against `_NET_CLIENT_LIST` -- much less than the KWin backend needs [M cinnamon.md §2.3].
* the window type is looked up by inverting `Meta.WindowType`, so the name that arrives is the enum's own
  (`NORMAL`, `DESKTOP`, `DOCK`, ...) and `backend.hit_test()`'s one rule about the desktop and dock layers
  works here as it does on GNOME.

THE INTERPOLATION RULE, and it is the reason every builder below is a function rather than a format string
handed out to callers: **only integers are ever interpolated, always with `%d`, always through `int()`**.
Window titles, WM_CLASS strings and workspace names come *out* of these programs and never go in; ids,
coordinates and workspace numbers go in and are numbers. `eval()` on the other end will run whatever
arrives, so a title containing `');` reaching a script would be arbitrary code execution inside the user's
shell, triggered by the user opening a window with a hostile name. tests/test_backend_cinnamon.py records
every script the backend sends and fails if one is not in its table.
"""

#: the whole list, stacking order bottom to top, as one JSON string.
#: Field names are short because this string is sent on every list(), find(),
#: views() and hit test.
LIST = """(function(){
const M=imports.gi.Meta;
const ws=global.workspace_manager;
const act=ws.get_active_workspace_index();
const TYPE={};for(const k in M.WindowType)TYPE[M.WindowType[k]]=k;
return JSON.stringify(global.display.sort_windows_by_stacking(
global.display.list_windows(0)).map(function(w){
const r=w.get_frame_rect();const wsp=w.get_workspace();
return {id:w.get_stable_sequence(),xid:w.get_xwindow(),
title:w.get_title()||"",cls:w.get_wm_class()||"",
inst:w.get_wm_class_instance()||"",
pid:(w.get_pid()>0?w.get_pid():(w.get_client_pid?w.get_client_pid():0)),
ct:w.get_client_type(),wt:TYPE[w.get_window_type()]||"NORMAL",
x:r.x,y:r.y,w:r.width,h:r.height,
ws:(w.is_on_all_workspaces()?-1:(wsp?wsp.index():-1)),act:act,
foc:w.has_focus(),min:w.minimized,max:w.get_maximized(),
full:w.is_fullscreen(),above:w.is_above(),shaded:w.is_shaded(),
skipt:w.is_skip_taskbar(),dec:!w.is_client_decorated()};}));})()"""

#: every workspace with its name and work area. `prefs_get_workspace_name` is
#: Meta's own accessor (muffin keeps it), and the work area is monitor 0's --
#: what wwmctl -d prints as WA.
WORKSPACES = """(function(){
const ws=global.workspace_manager;const n=ws.get_n_workspaces();
const a=ws.get_active_workspace_index();
return JSON.stringify(Array.from({length:n},function(_,i){
const r=ws.get_workspace_by_index(i).get_work_area_for_monitor(0);
return {i:i,name:imports.gi.Meta.prefs_get_workspace_name(i),act:i==a,
wa:[r.x,r.y,r.width,r.height]};}));})()"""

GET_DESKTOP = "global.workspace_manager.get_active_workspace_index()"
NUM_DESKTOPS = "global.workspace_manager.get_n_workspaces()"
#: `global.screen_width/height` -- the layout bounding box, measured 800 600 on
#: the nested session [M cinnamon.md §4]
DISPLAY_SIZE = "JSON.stringify([global.screen_width,global.screen_height])"
#: [x, y, modifier mask]; the pointer wherever it is and whoever moved it
POINTER = "JSON.stringify(global.get_pointer())"

#: what a per-window program answers when the id names no window. A string and
#: not an exception because Eval turns an exception into `(false, <the whole
#: stack trace>)`, and "no such window" is an answer, not a failure.
NOWIN = "NOWIN"


def _window(wid: int, body: str) -> str:
    """One program against one window: find it by stable sequence, run `body`, answer "ok".

    The lookup is inside the program because there is no window handle to hold on to between Eval calls --
    every call starts from `global.display`."""
    return ("(function(){const w=global.display.list_windows(0)"
            ".find(function(w){return w.get_stable_sequence()==%d;});"
            "if(!w)return '%s';%s;return 'ok';})()" % (int(wid), NOWIN, body))


#: the actions that are one call on the window and nothing else
_ACTIONS = {
    "activate": "w.activate(global.get_current_time())",
    "focus": "w.focus(global.get_current_time())",
    "close": "w.delete(global.get_current_time())",
    "kill": "w.kill()",
    "minimize": "w.minimize()",
    "unminimize": "w.unminimize()",
    "raise": "w.raise()",
    "lower": "w.lower()",
}


def action(wid: int, name: str) -> str:
    return _window(wid, _ACTIONS[name])


def move(wid: int, x: int, y: int) -> str:
    """`move_frame(user_op, x, y)`: the frame rectangle, which is the space `get_frame_rect()` reports and
    the one the input daemon's pointer lives in."""
    return _window(wid, "w.move_frame(true,%d,%d)" % (int(x), int(y)))


def resize(wid: int, w: int, h: int) -> str:
    """Resize about the frame's current origin: muffin has no size-only call, so the position is read inside
    the same program rather than in a round trip of its own."""
    return _window(wid, "const r=w.get_frame_rect();"
                        "w.move_resize_frame(true,r.x,r.y,%d,%d)" % (int(w), int(h)))


def move_resize(wid: int, x: int, y: int, w: int, h: int) -> str:
    return _window(wid, "w.move_resize_frame(true,%d,%d,%d,%d)"
                   % (int(x), int(y), int(w), int(h)))


def set_workspace(wid: int, n: int) -> str:
    return _window(wid, "w.change_workspace_by_index(%d,false)" % int(n))


def set_desktop(n: int) -> str:
    """Activating a workspace answers nothing, so the program ends in a literal 1: Eval returns `(true, "")`
    for an `undefined` result and the caller cannot tell that from a program that never ran."""
    return ("global.workspace_manager.get_workspace_by_index(%d)"
            ".activate(global.get_current_time());1" % int(n))


#: _NET_WM_STATE name -> (the JS that sets it, the JS that clears it).
#: MAXIMIZED is the pair in one call -- `MaximizeFlags.BOTH`, which measured
#: `get_maximized() == 3` afterwards [M cinnamon.md §4] -- because two
#: single-axis calls are not the same operation on a Mutter-derived compositor
#: (see WindowBackend.maximize_pair_state).
#: SHADED is here and is a real operation: muffin kept `shade`/`unshade`/
#: `is_shaded` where mutter dropped shading and KWin 6 removed it
#: [M cinnamon.md §2.3].
STATES = {
    "MAXIMIZED": ("w.maximize(imports.gi.Meta.MaximizeFlags.BOTH)",
                  "w.unmaximize(imports.gi.Meta.MaximizeFlags.BOTH)"),
    "MAXIMIZED_VERT": ("w.maximize(imports.gi.Meta.MaximizeFlags.VERTICAL)",
                       "w.unmaximize(imports.gi.Meta.MaximizeFlags.VERTICAL)"),
    "MAXIMIZED_HORZ": ("w.maximize(imports.gi.Meta.MaximizeFlags.HORIZONTAL)",
                       "w.unmaximize(imports.gi.Meta.MaximizeFlags.HORIZONTAL)"),
    "FULLSCREEN": ("w.make_fullscreen()", "w.unmake_fullscreen()"),
    "ABOVE": ("w.make_above()", "w.unmake_above()"),
    "STICKY": ("w.stick()", "w.unstick()"),
    "SHADED": ("w.shade(global.get_current_time())",
               "w.unshade(global.get_current_time())"),
    "HIDDEN": ("w.minimize()", "w.unminimize()"),
}


def state(wid: int, name: str, add: bool) -> str:
    return _window(wid, STATES[name][0 if add else 1])
