"""What the proxy does with a request, as data.

Five classes (design section 3.1), and in this stage every row is PASS: stage 1
of the plan is a proxy that changes not one byte, and the parity oracle through
it is the gate that says so. The table is here now, complete and inert, because
the batches that follow only change rows -- never the shape of the lookup, and
never `xw11/server.py`'s loop.

* **PASS** -- forwarded byte for byte; the reply, if any, streams back untouched.
* **EDIT** -- forwarded; the reply is rewritten on the way back. The sequence
  never moves; a length-changing edit rewrites the reply's length word.
* **CONSUME** -- the client gets nothing and upstream gets `NoOperation` in the
  request's place, because a request the proxy eats still costs one sequence
  number upstream, for ever (recon/wire.md 3.2a, 3.3).
* **ANSWER** -- the proxy writes the reply itself and upstream gets
  `GetInputFocus`, whose 32-byte reply comes back in stream order carrying the
  substituted request's sequence and is replaced by the stored packet.
* **BATCH** -- a RandR write inside a GrabServer, with a deferred side effect.

The opcode names are for `XW11_DEBUG=1`'s log and nothing else. Core opcodes are
fixed by the protocol and are written down here from /usr/share/xcb/xproto.xml;
extension MAJORS are assigned per server at run time (RANDR is 140 on Xvfb and
139 on Xwayland, XTEST 132 and 133 [recon/tools.md 7, recon/env.md 2.5]) and are
never written down anywhere in this package -- the log resolves them from the
QueryExtension replies it watched go past.
"""

from typing import NamedTuple

PASS = "PASS"
EDIT = "EDIT"
CONSUME = "CONSUME"
ANSWER = "ANSWER"
BATCH = "BATCH"

CLASSES = (PASS, EDIT, CONSUME, ANSWER, BATCH)


class Row(NamedTuple):
    """One request, in the three places design section 3.2 distinguishes:
    a window id the proxy minted (`shadow`), the setup's root (`root`), and
    anything else -- a real X window, a pixmap, a dead id -- which upstream
    answers for itself, `BadWindow` included."""

    shadow: str = PASS
    root: str = PASS
    other: str = PASS


#: Every row of design section 3.2, all PASS. The rows are listed rather than
#: defaulted so that a later batch changing one changes a line that already
#: names the request, and `test_xw11_wire.py` can count them.
PASS_ROW = Row()

POLICY = {
    2: PASS_ROW,     # ChangeWindowAttributes -- event masks (section 5.4)
    3: PASS_ROW,     # GetWindowAttributes
    4: PASS_ROW,     # DestroyWindow
    8: PASS_ROW,     # MapWindow
    10: PASS_ROW,    # UnmapWindow
    12: PASS_ROW,    # ConfigureWindow
    14: PASS_ROW,    # GetGeometry
    15: PASS_ROW,    # QueryTree
    16: PASS_ROW,    # InternAtom -- always PASS: atoms are server-global
    17: PASS_ROW,    # GetAtomName -- likewise (recon/wire.md 7.2)
    18: PASS_ROW,    # ChangeProperty
    19: PASS_ROW,    # DeleteProperty
    20: PASS_ROW,    # GetProperty
    21: PASS_ROW,    # ListProperties
    25: PASS_ROW,    # SendEvent -- the EWMH ClientMessages (section 3.4)
    36: PASS_ROW,    # GrabServer -- opens a RandR batch (section 7.4)
    37: PASS_ROW,    # UngrabServer -- commits it
    38: PASS_ROW,    # QueryPointer
    40: PASS_ROW,    # TranslateCoordinates
    41: PASS_ROW,    # WarpPointer
    42: PASS_ROW,    # SetInputFocus
    43: PASS_ROW,    # GetInputFocus
    98: PASS_ROW,    # QueryExtension -- watched, and DRI3's reply edited
    113: PASS_ROW,   # KillClient
    127: PASS_ROW,   # NoOperation
}

#: Extension requests, keyed by the extension's NAME (never its major) and minor
#: opcode. XTEST, RANDR and BIG-REQUESTS are the three the later stages own.
EXT_POLICY = {
    ("XTEST", 0): PASS_ROW,          # GetVersion
    ("XTEST", 1): PASS_ROW,          # CompareCursor
    ("XTEST", 2): PASS_ROW,          # FakeInput
    ("XTEST", 3): PASS_ROW,          # GrabControl
    ("BIG-REQUESTS", 0): PASS_ROW,   # Enable -- always PASS, and always watched
    ("RANDR", 0): PASS_ROW,          # QueryVersion
    ("RANDR", 2): PASS_ROW,          # SetScreenConfig
    ("RANDR", 4): PASS_ROW,          # SelectInput
    ("RANDR", 5): PASS_ROW,          # GetScreenInfo
    ("RANDR", 6): PASS_ROW,          # GetScreenSizeRange
    ("RANDR", 7): PASS_ROW,          # SetScreenSize
    ("RANDR", 8): PASS_ROW,          # GetScreenResources
    ("RANDR", 9): PASS_ROW,          # GetOutputInfo
    ("RANDR", 15): PASS_ROW,         # GetOutputProperty
    ("RANDR", 20): PASS_ROW,         # GetCrtcInfo
    ("RANDR", 21): PASS_ROW,         # SetCrtcConfig
    ("RANDR", 23): PASS_ROW,         # GetCrtcGamma
    ("RANDR", 25): PASS_ROW,         # GetScreenResourcesCurrent
    ("RANDR", 27): PASS_ROW,         # GetCrtcTransform
    ("RANDR", 28): PASS_ROW,         # GetPanning
    ("RANDR", 30): PASS_ROW,         # SetOutputPrimary
    ("RANDR", 31): PASS_ROW,         # GetOutputPrimary
}


def lookup(opcode: int, ext: str = None, minor: int = 0) -> Row:
    """The row for one request. A request in neither table is PASS by
    construction: the splitter needs no opcode to forward (recon/wire.md 2),
    so an opcode nobody has measured costs nothing and behaves like X."""
    if ext is not None:
        return EXT_POLICY.get((ext, minor), PASS_ROW)
    return POLICY.get(opcode, PASS_ROW)


#: opcode -> name, for `XW11_DEBUG=1` only. The whole core table out of
#: /usr/share/xcb/xproto.xml: the ~60 the four tools actually send
#: (recon/tools.md 3) are a subset, and a log that prints `op 63` for the one
#: request outside the measured set is the log failing at the moment it is
#: wanted. 74-77 are the text requests, which that file spells with a
#: `<request name=... opcode=...>` the rest of the table shares.
CORE_NAMES = {
    1: "CreateWindow", 2: "ChangeWindowAttributes", 3: "GetWindowAttributes",
    4: "DestroyWindow", 5: "DestroySubwindows", 6: "ChangeSaveSet",
    7: "ReparentWindow", 8: "MapWindow", 9: "MapSubwindows", 10: "UnmapWindow",
    11: "UnmapSubwindows", 12: "ConfigureWindow", 13: "CirculateWindow",
    14: "GetGeometry", 15: "QueryTree", 16: "InternAtom", 17: "GetAtomName",
    18: "ChangeProperty", 19: "DeleteProperty", 20: "GetProperty",
    21: "ListProperties", 22: "SetSelectionOwner", 23: "GetSelectionOwner",
    24: "ConvertSelection", 25: "SendEvent", 26: "GrabPointer",
    27: "UngrabPointer", 28: "GrabButton", 29: "UngrabButton",
    30: "ChangeActivePointerGrab", 31: "GrabKeyboard", 32: "UngrabKeyboard",
    33: "GrabKey", 34: "UngrabKey", 35: "AllowEvents", 36: "GrabServer",
    37: "UngrabServer", 38: "QueryPointer", 39: "GetMotionEvents",
    40: "TranslateCoordinates", 41: "WarpPointer", 42: "SetInputFocus",
    43: "GetInputFocus", 44: "QueryKeymap", 45: "OpenFont", 46: "CloseFont",
    47: "QueryFont", 48: "QueryTextExtents", 49: "ListFonts",
    50: "ListFontsWithInfo", 51: "SetFontPath", 52: "GetFontPath",
    53: "CreatePixmap", 54: "FreePixmap", 55: "CreateGC", 56: "ChangeGC",
    57: "CopyGC", 58: "SetDashes", 59: "SetClipRectangles", 60: "FreeGC",
    61: "ClearArea", 62: "CopyArea", 63: "CopyPlane", 64: "PolyPoint",
    65: "PolyLine", 66: "PolySegment", 67: "PolyRectangle", 68: "PolyArc",
    69: "FillPoly", 70: "PolyFillRectangle", 71: "PolyFillArc", 72: "PutImage",
    73: "GetImage", 74: "PolyText8", 75: "PolyText16", 76: "ImageText8",
    77: "ImageText16", 78: "CreateColormap", 79: "FreeColormap",
    80: "CopyColormapAndFree", 81: "InstallColormap", 82: "UninstallColormap",
    83: "ListInstalledColormaps", 84: "AllocColor", 85: "AllocNamedColor",
    86: "AllocColorCells", 87: "AllocColorPlanes", 88: "FreeColors",
    89: "StoreColors", 90: "StoreNamedColor", 91: "QueryColors",
    92: "LookupColor", 93: "CreateCursor", 94: "CreateGlyphCursor",
    95: "FreeCursor", 96: "RecolorCursor", 97: "QueryBestSize",
    98: "QueryExtension", 99: "ListExtensions", 100: "ChangeKeyboardMapping",
    101: "GetKeyboardMapping", 102: "ChangeKeyboardControl",
    103: "GetKeyboardControl", 104: "Bell", 105: "ChangePointerControl",
    106: "GetPointerControl", 107: "SetScreenSaver", 108: "GetScreenSaver",
    109: "ChangeHosts", 110: "ListHosts", 111: "SetAccessControl",
    112: "SetCloseDownMode", 113: "KillClient", 114: "RotateProperties",
    115: "ForceScreenSaver", 116: "SetPointerMapping", 117: "GetPointerMapping",
    118: "SetModifierMapping", 119: "GetModifierMapping", 127: "NoOperation",
}

#: Extension minors worth a name in the log, by extension name: the extensions
#: the prologue of every connection touches (recon/tools.md 2) plus the ones the
#: four tools drive. Every minor here is copied from the xcb protocol
#: description of that extension in /usr/share/xcb (xkb.xml, xinerama.xml,
#: randr.xml, xtest.xml, bigreq.xml, ge.xml, dri3.xml on this box, xcb-proto
#: 1.17.0), and tests/test_xw11_wire.py:ExtensionNames reads those files back and
#: compares, because a table that names the wrong request is the log failing at
#: the one moment it is wanted: XKEYBOARD minor 1 is SelectEvents (11 is
#: SetCompatMap) and XINERAMA minor 4 is IsActive (5 is QueryScreens), which is
#: how this table read before 2026-09-10.
EXT_NAMES = {
    "BIG-REQUESTS": {0: "Enable"},
    "XTEST": {0: "GetVersion", 1: "CompareCursor", 2: "FakeInput",
              3: "GrabControl"},
    "XKEYBOARD": {0: "UseExtension", 1: "SelectEvents", 4: "GetState",
                  5: "LatchLockState", 8: "GetMap", 9: "SetMap",
                  17: "GetNames"},
    "Generic Event Extension": {0: "QueryVersion"},
    "XINERAMA": {0: "QueryVersion", 1: "GetState", 4: "IsActive",
                 5: "QueryScreens"},
    "RANDR": {0: "QueryVersion", 2: "SetScreenConfig", 4: "SelectInput",
              5: "GetScreenInfo", 6: "GetScreenSizeRange", 7: "SetScreenSize",
              8: "GetScreenResources", 9: "GetOutputInfo",
              15: "GetOutputProperty", 16: "CreateMode", 18: "AddOutputMode",
              20: "GetCrtcInfo", 21: "SetCrtcConfig", 23: "GetCrtcGamma",
              25: "GetScreenResourcesCurrent", 27: "GetCrtcTransform",
              28: "GetPanning", 29: "SetPanning", 30: "SetOutputPrimary",
              31: "GetOutputPrimary", 42: "GetMonitors"},
    "DRI3": {0: "QueryVersion"},
}

#: The extensions the proxy's own connection asks about (design section 2.4).
#: DRI3 is on the list so that its reply can be answered honestly on the way
#: down: no box in this project has a /dev/dri (recon/env.md 2.6), and until the
#: proxy forwards descriptors with sendmsg at the right offset a client that
#: asks is told "not here" rather than handed a path that breaks silently.
WANTED_EXTENSIONS = ("BIG-REQUESTS", "XTEST", "RANDR", "XKEYBOARD", "XINERAMA",
                     "Generic Event Extension", "XInputExtension", "DRI3")


#: The names the proxy interns on its own connection at open (design section
#: 2.4 step 3), in one burst, in this order -- which is also the order design
#: section 4.6's `_NET_SUPPORTED` union appends in, so the union is stable
#: between two runs of the same tool.
#:
#: The first 45 are `wxprop.core._EXTENDED_ATOMS`, exactly and in its order --
#: the set the native property synthesis already builds and the read side is a
#: port of (`NativeViewTarget._props()`, wxprop/core.py:529-597); the list is
#: repeated here rather than imported so that `import xw11` does not pull
#: wxprop.core's 10 ms in (measured on this box, 2026-09-10) for a proxy that
#: has not opened a connection yet, and
#: `tests/test_xw11_upstream.py::AtomsOnce` compares the two tables so they
#: cannot drift. The rest are the names the proxy needs that the native plane
#: never had a server to intern in: the two _NET_WM_STATE_* names wmctrl -b
#: takes and wxprop does not synthesize, the EWMH messages design section 3.4
#: routes, and the root names of design section 4.6.
#:
#: Atoms 1..68 are predefined and are never interned [recon/wire.md 7.2]; they
#: are in PREDEFINED_ATOMS below with the ids the protocol gives them.
ATOMS = (
    # wxprop.core._EXTENDED_ATOMS, in its own order
    "UTF8_STRING", "WM_STATE", "WM_PROTOCOLS", "WM_DELETE_WINDOW",
    "WM_TAKE_FOCUS", "_NET_WM_NAME", "_NET_WM_ICON_NAME", "_NET_WM_PID",
    "_NET_WM_DESKTOP", "_NET_WM_STATE", "_NET_WM_STATE_FULLSCREEN",
    "_NET_WM_STATE_HIDDEN", "_NET_WM_STATE_STICKY", "_NET_WM_STATE_FOCUSED",
    "_NET_WM_WINDOW_TYPE", "_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_ICON",
    "_NET_SUPPORTED", "_NET_CLIENT_LIST", "_NET_CLIENT_LIST_STACKING",
    "_NET_ACTIVE_WINDOW", "_NET_SUPPORTING_WM_CHECK", "_NET_CURRENT_DESKTOP",
    "_NET_NUMBER_OF_DESKTOPS", "_NET_DESKTOP_NAMES",
    "_NET_WM_STATE_MAXIMIZED_HORZ", "_NET_WM_STATE_MAXIMIZED_VERT",
    "_NET_WM_STATE_ABOVE", "_NET_WM_STATE_BELOW",
    "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER",
    "_NET_WM_STATE_DEMANDS_ATTENTION", "_NET_WM_WINDOW_TYPE_DESKTOP",
    "_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DIALOG",
    "_NET_WM_WINDOW_TYPE_TOOLBAR", "_NET_WM_WINDOW_TYPE_MENU",
    "_NET_WM_WINDOW_TYPE_UTILITY", "_NET_WM_WINDOW_TYPE_SPLASH",
    "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU", "_NET_WM_WINDOW_TYPE_POPUP_MENU",
    "_NET_WM_WINDOW_TYPE_TOOLTIP", "_NET_WM_WINDOW_TYPE_NOTIFICATION",
    "_NET_WM_WINDOW_TYPE_COMBO", "_NET_WM_WINDOW_TYPE_DND",
    # the two states wmctrl -b names that the native synthesis has no source
    # for and the proxy still has to be able to read back from an overlay
    "_NET_WM_STATE_MODAL", "_NET_WM_STATE_SHADED",
    # the EWMH client messages design section 3.4 routes, and WM_CHANGE_STATE,
    # which is how wmctrl and xdotool ask for iconify
    "_NET_CLOSE_WINDOW", "_NET_MOVERESIZE_WINDOW", "_NET_WM_MOVERESIZE",
    "_NET_SHOWING_DESKTOP", "WM_CHANGE_STATE",
    # the rest of design section 4.6's root set, plus the one name wxprop does
    # NOT synthesize (docs/WXPROP.md:103) and the proxy answers zero for
    "_NET_DESKTOP_GEOMETRY", "_NET_FRAME_EXTENTS",
)

#: The predefined atoms the proxy names by id. 1..68 are fixed by the protocol
#: and interning them would be a round trip for a number that is written in
#: /usr/include/X11/Xatom.h [recon/wire.md 7.2]. Every one here is a type or a
#: property the read and write sides handle: STRING and UTF8_STRING are the two
#: text types, WM_NAME is the one xterm sets first [recon/seams.md 4], WM_CLASS
#: is the pair `search --class` matches.
PREDEFINED_ATOMS = {
    "ATOM": 4, "CARDINAL": 6, "INTEGER": 19, "PIXMAP": 20, "STRING": 31,
    "WINDOW": 33, "WM_COMMAND": 34, "WM_HINTS": 35, "WM_CLIENT_MACHINE": 36,
    "WM_ICON_NAME": 37, "WM_NAME": 39, "WM_NORMAL_HINTS": 40,
    "WM_SIZE_HINTS": 41, "WM_CLASS": 67, "WM_TRANSIENT_FOR": 68,
}

#: Where the window/drawable id sits in a core request, as an offset into the
#: frame. Every one of them is 4 -- the id is the first field after the 4-byte
#: header -- and the table is a table anyway, because "which requests name a
#: window at all" is the question `dispatch` asks: a request that names none is
#: `Row.other` and passes [recon/wire.md 4].
#:
#: TranslateCoordinates and WarpPointer carry a SECOND window at offset 8
#: (dst_window, dst_window); the row is decided by the first here and the
#: handlers of batches 3 and 6 read the second themselves.
WINDOW_FIELD = {
    2: 4,    # ChangeWindowAttributes
    3: 4,    # GetWindowAttributes
    4: 4,    # DestroyWindow
    8: 4,    # MapWindow
    10: 4,   # UnmapWindow
    12: 4,   # ConfigureWindow
    14: 4,   # GetGeometry (a DRAWABLE: a pixmap here is never a shadow)
    15: 4,   # QueryTree
    18: 4,   # ChangeProperty
    19: 4,   # DeleteProperty
    20: 4,   # GetProperty
    21: 4,   # ListProperties
    25: 4,   # SendEvent -- the destination, which is the root for the EWMH
    38: 4,   # QueryPointer
    40: 4,   # TranslateCoordinates -- src_window; dst_window is at 8
    41: 4,   # WarpPointer -- src_window; dst_window is at 8
    42: 4,   # SetInputFocus
    113: 4,  # KillClient (a RESOURCE)
}


def request_name(opcode: int, minor: int = 0, ext: str = None) -> str:
    """`GetProperty`, `RANDR.SetCrtcConfig`, or `op 200.3` for a major nobody
    has resolved yet -- the shape recon/tools/xwlog.py logged in."""
    if opcode < 128:
        return CORE_NAMES.get(opcode, "op %d" % opcode)
    if ext is None:
        return "op %d.%d" % (opcode, minor)
    return "%s.%s" % (ext, EXT_NAMES.get(ext, {}).get(minor, "minor %d" % minor))
