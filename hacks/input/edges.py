"""`behave_screen_edge` as a pure state machine, behind a pointer-sample seam.

xdotool's `behave_screen_edge` polls the pointer and runs an action while it sits on an edge or corner of the
screen, with `--delay` (how long the pointer must dwell in the region before the action runs, reset by leaving
it) and `--quiesce` (a cooldown after each run before it can run again). None of that is a compositor question:
it is a clock and the pointer's position, so it lives here as a state machine that is handed one `(x, y, now)`
sample at a time. The CLI (`wdotool/input_cmds.py`) owns the pointer source -- the compositor's own query where
it has one (GNOME/KWin/Wayfire/Hyprland/Cinnamon, AGENTS.md route 2), else the input daemon's tracked position
-- and the loop that feeds this. Splitting it here is what makes the timing testable with a scripted clock
instead of a real pointer.

The edge test is xdotool's: the outermost row or column of the layout box. `left` is the whole left column,
`top-left` the corner pixel, and so on; a pointer clamped inside the screen means `x <= box.x` can only be the
leftmost column and `y <= box.y` only the topmost row, so `<=`/`>=` against the boundary is exact and survives a
query that reports the boundary pixel itself."""

#: xdotool's eight edge/corner names, in `cmd_behave_screen_edge.c` order.
EDGES = {"left", "top-left", "top", "top-right", "right", "bottom-right", "bottom", "bottom-left"}


def in_edge(edge: str, box: "tuple[int, int, int, int]", x: int, y: int) -> bool:
    """Is (x, y) on `edge` of the layout box (bx, by, bw, bh)? False for an empty box or an unknown edge."""
    bx, by, bw, bh = box
    if bw <= 0 or bh <= 0:
        return False
    left = x <= bx
    right = x >= bx + bw - 1
    top = y <= by
    bottom = y >= by + bh - 1
    in_x = bx <= x <= bx + bw - 1
    in_y = by <= y <= by + bh - 1
    return {
        "left": left and in_y,
        "right": right and in_y,
        "top": top and in_x,
        "bottom": bottom and in_x,
        "top-left": left and top,
        "top-right": right and top,
        "bottom-left": left and bottom,
        "bottom-right": right and bottom,
    }.get(edge, False)


class EdgeMachine:
    """One edge, with xdotool's dwell (`--delay`) and cooldown (`--quiesce`) semantics.

    The action fires ONCE per entry into the edge, on the none->in transition (`cmd_behave_screen_edge.c`
    lines 218-233): xdotool never re-runs it while the pointer keeps sitting on the edge, only when it leaves
    and comes back. `--delay` holds the fire until the pointer has dwelled that long since the entry; `--quiesce`
    is a cooldown that drops the entry whose fire moment lands within it of the last fire (the pointer must
    still leave and return for the next chance). A pointer that rests on the edge forever fires exactly once.

    `feed(x, y, now)` is handed one pointer sample and the current time in seconds; it returns True on the
    single sample of each entry where the action should run. `x`/`y` may be None for a sample where the pointer
    is unknown, which counts as "not in the edge" (and so resets the dwell and re-arms the next entry). Times
    are whatever monotonic clock the caller uses; only differences matter."""

    def __init__(self, edge: str, box: "tuple[int, int, int, int]",
                 delay_ms: int = 0, quiesce_ms: int = 2000):
        self.edge = edge
        self.box = box
        self.delay_ms = max(0, int(delay_ms))
        self.quiesce_ms = max(0, int(quiesce_ms))
        self._in = False
        self._armed = False     # this entry still owes a fire (cleared once it fires or is dropped)
        self._entered = 0.0     # `now` of the sample that first found us in the edge
        self._last_fire = None  # `now` of the last fire, None until the first

    def feed(self, x, y, now: float) -> bool:
        inside = x is not None and y is not None and in_edge(self.edge, self.box, x, y)
        if not inside:
            self._in = False        # leaving the edge re-arms the next entry (the dwell reset)
            self._armed = False
            return False
        if not self._in:            # rising edge: a fresh entry to arm and time from
            self._in = True
            self._armed = True
            self._entered = now
        if not self._armed:         # already fired (or was dropped) for this entry
            return False
        if (now - self._entered) * 1000.0 < self.delay_ms:
            return False            # still dwelling; stay armed
        cooled = (self._last_fire is None
                  or (now - self._last_fire) * 1000.0 >= self.quiesce_ms)
        self._armed = False         # this entry's one chance is spent, fire or drop
        if not cooled:
            return False            # quiesce dropped it; the next entry must leave and return
        self._last_fire = now
        return True
