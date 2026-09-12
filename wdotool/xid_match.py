"""The XWayland-window matcher: which X client is which compositor toplevel.

Shared by every backend whose compositor publishes toplevels the wlr/COSMIC/Hyprland/Wayfire way -- with no X
id on them -- and which therefore has to pair its own list against `_NET_CLIENT_LIST` to print the real id, the
real WM_CLASS pair, `_NET_WM_PID` and X geometry. It was written for and measured on KWin 6 (Plasma 6.6, four
runs in ten moved the wrong window before the position key went in); the rules transfer verbatim because the
evidence does: pid and WM_CLASS are filters, title and geometry are the score, a pair must agree on something,
and a tie nobody can break keeps xid 0.

This file is a move out of `wdotool/backend_kwin.py` and nothing else -- `backend_kwin` imports the two names
back under their old spellings, so its 25 tests read the same functions they always did.

Callers hand it two lists of plain dicts, which is what keeps it free of any one backend's window class:

    raw     the compositor's own windows: "u" (its handle), optional "p" (pid),
            "c" (class), "n" (instance), "t" (title), "x"/"y"/"w"/"h", "ix"
            (position in the compositor's own stacking list)
    clients the X plane's: "xid", "pid", "cls", "inst", "name", "geo"

and get back {handle: xid} for the pairs it is sure of."""

from wdotool.backend import warn as _warn


def simplified(s: str) -> str:
    """QString::simplified(): every run of whitespace becomes one space and the ends are trimmed.

    KWin stores an X11 window's caption that way -- X11Window::readName() ends in `.simplified()` -- while the X
    server hands back the raw _NET_WM_NAME the client set. Comparing the two as they come makes any title with a
    doubled, leading or trailing space compare *unequal to its own window* and equal to nothing, which does not
    merely lose the title as a signal: it points it at the other window of the pair."""
    return " ".join((s or "").split())


def tie_warning(blocked: int) -> str:
    """The sentence a tie costs the user, in one place so that both printers of it print the same bytes.

    `match_xids` prints it for the callers that have always printed it -- KWin, Hyprland, Wayfire, whose
    join runs once per listing read -- and `backend_wlr.views()` prints it for the wlr floor, where the
    join also runs under `list()` now and `list()` runs under every window command there is."""
    return ("%d XWayland window(s) could not be told apart from each other in the X client list; "
            "their X ids are left unset" % blocked)


def match_xids(raw: "list[dict]", clients: "list[dict]",
               ratio: "float | None" = 1.0) -> "dict[str, int]":
    """`match_xids_quiet`'s pairing, with the ties it could not break said once on stderr.

    This is the spelling every caller but `backend_wlr.XPlaneViews._x_join` uses, and the warning is the
    reason it exists separately: a window whose id is silently left at 0 is a window `wwmctl -l` prints
    without an X id and nobody can tell why."""
    out, blocked = match_xids_quiet(raw, clients, ratio)
    if blocked:
        _warn(tie_warning(blocked))
    return out


def match_xids_quiet(raw: "list[dict]", clients: "list[dict]",
                     ratio: "float | None" = 1.0) -> "tuple[dict[str, int], int]":
    """Greedy best-first matching of compositor windows to Xwayland's clients, and the number of windows a
    tie left unpaired -- with nothing printed, so that a caller which joins under every command (the wlr
    floor's `list()`, since route-5 geometry) can pick the one place the sentence belongs.

    pid and WM_CLASS are filters (an X client never changes them behind
    KWin's back), the title and the geometry distance are the score -- two
    untitled terminals of the same class differ only in where they are, and
    the KWin rectangle is the frame while X reports the client area, so the
    distance is small but not zero.

    A pair also has to *agree* on something: an X client with neither
    _NET_WM_PID nor WM_CLASS contradicts nothing, and matching it on
    geometry alone hands its id to a native Wayland window, which then
    claims to be an X11 client. Such a client keeps xid 0 instead -- an
    unknown id beats a wrong one.

    Neither of those separates two windows of one application that sit in
    the same place under the same title -- two maximized editor windows,
    two terminals stacked on each other. Title and geometry tie, and the
    pairing was then decided by whichever uuid sorted first, which is a coin
    flip: measured on Plasma 6.6, four runs in ten moved the other window.
    So the *order* of the two lists is the third key, and it is not a
    heuristic:

        Workspace::propagateWindows() (src/layers.cpp) writes
        _NET_CLIENT_LIST from m_windows, keeping only the managed X11
        windows and their order, and workspace.windowList() *is* m_windows.

    The X11 windows of the script's list, in `ix` order, are therefore the
    client list, in its order -- so a pair whose two positions disagree is
    the wrong pair. Positions are ranked over the windows and clients that
    are actually in play, so a window with no X client (a native one, or an
    override-redirect popup, which KWin lists but never publishes) only
    shifts what it precedes, and only where the pairs already tied.

    A tie the position cannot break either -- two windows that could each
    take the same id, on a session where the script answered without `ix`
    -- is left unresolved: those windows keep xid 0 and say so, rather than
    being handed one of the two ids at random. `ix` is all-or-nothing on
    purpose: ranking over the rows that happen to carry it puts the rest at
    positions that are not their list positions, which is a wrong order
    rather than a missing one.

    `ratio` is X device pixels per compositor logical pixel (see
    KwinBackend._x_ratio): every X rect is divided by it before the distance,
    because on a 2x screen a window's own X rect is twice its KWin rect and
    the raw distance between a window and *itself* then exceeds the distance
    between the two windows of a tied pair. None means the layout has no one
    ratio, and there the distance says nothing at all and is dropped."""
    cand = []
    for ci, c in enumerate(clients):
        for d in raw:
            if c["pid"] and d.get("p") and c["pid"] != d["p"]:
                continue
            kcls, kinst = (d.get("c") or ""), (d.get("n") or "")
            if kcls and c["cls"] and kcls.lower() != c["cls"].lower():
                continue
            if kinst and c["inst"] and kinst.lower() != c["inst"].lower():
                continue
            if not (c["pid"] and c["pid"] == d.get("p")
                    or kcls and kcls.lower() == (c["cls"] or "").lower()
                    or kinst and kinst.lower() == (c["inst"] or "").lower()):
                continue
            x, y, w, h = c["geo"]
            if ratio is None:
                dist = 0
            else:
                r = ratio or 1.0
                dist = int(round(
                    abs(x / r - int(d.get("x", 0))) + abs(y / r - int(d.get("y", 0)))
                    + abs(w / r - int(d.get("w", 0))) + abs(h / r - int(d.get("h", 0)))))
            same_title = simplified(c["name"]) == simplified(d.get("t"))
            cand.append((0 if same_title else 1, dist, ci, d))
    # `ix` is the script's index into workspace.windowList(); a list without it (an older script, or 5.27,
    # which never reaches here) leaves the key inert.
    have_ix = bool(cand) and all("ix" in d for _t, _d, _c, d in cand)
    # Positions are ranked *inside* each (title, distance) tie group, over the windows and clients of that group
    # alone. Ranking them over every candidate instead let a window that is not in the tie shift the ranks of
    # the two that are: an override-redirect popup listed *ahead* of a tied pair (ix=1 against ix=2 and ix=4)
    # moved both windows one place down in the window ranking while the client ranking, which never saw it,
    # stayed put -- and the pair came out swapped. Below the popup (ix=3, ix=6) it happened to cancel out, which
    # is why the first version of this looked right.
    pairs = []
    cand.sort(key=lambda p: (p[0], p[1]))
    i = 0
    while i < len(cand):
        j = i
        while j < len(cand) and cand[j][0] == cand[i][0] and cand[j][1] == cand[i][1]:
            j += 1
        group = cand[i:j]
        krank: "dict[str, int]" = {}
        if have_ix:
            seen = {d["u"]: int(d["ix"]) for _t, _d, _c, d in group}
            for r, u in enumerate(sorted(seen, key=lambda k: (seen[k], k))):
                krank[u] = r
        crank = {ci: r for r, ci in enumerate(sorted({p[2] for p in group}))}
        for t, dist, ci, d in group:
            pairs.append((t, dist,
                          abs(krank[d["u"]] - crank[ci]) if have_ix else 0,
                          clients[ci]["xid"], d["u"]))
        i = j
    pairs.sort()
    out: "dict[str, int]" = {}
    used: "set[int]" = set()
    blocked: "set[str]" = set()
    i = 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][:3] == pairs[i][:3]:
            j += 1
        live = [p for p in pairs[i:j] if p[4] not in out and p[4] not in blocked and p[3] not in used]
        by_win: "dict[str, set[int]]" = {}
        by_id: "dict[int, set[str]]" = {}
        for _t, _d, _o, xid, u in live:
            by_win.setdefault(u, set()).add(xid)
            by_id.setdefault(xid, set()).add(u)
        for _t, _d, _o, xid, u in live:
            if u in out or u in blocked or xid in used:
                continue
            if len(by_win[u]) > 1 or len(by_id[xid]) > 1:
                blocked.update(by_id[xid])   # a coin flip: no id at all
                continue
            out[u] = xid
            used.add(xid)
        i = j
    return out, len(blocked)

