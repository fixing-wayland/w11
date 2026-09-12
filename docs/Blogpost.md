# Six tools that should not exist

There is a four-line script on a lot of machines. Find the terminal, raise it, type
the command, press Return. It has worked since 2007, which is when `xdotool` shipped.
Then the session underneath it becomes a Wayland session, and the script prints
nothing, because `xdotool` no longer does anything at all, and it does not say so.

Go looking and the first thing you find is that this is already well known. Jordan
Sissel, who wrote xdotool, knows it better than anyone. His post
[xdotool and exploring Wayland fragmentation](https://www.semicomplete.com/blog/xdotool-and-exploring-wayland-fragmentation/)
is the one everybody links to, and it hurts precisely because it is not a rant. It is
the maintainer of the tool doing an honest survey and finding nothing:

> Wayland comes along and eliminates *everything* xdotool can do.

and then, further down, the sentence I think about most:

> I don't mind what the protocol is, but I sure would love to have *any* protocol that
> does what I need.

His conclusion is that there is no such protocol, and that the replacement for one
protocol is a dozen partial ones. A GNOME Shell extension here. A KDE D-Bus interface
there. `wlrctl` for wlroots. libei and the portal, which ask the user for permission
with a dialog. A separate implementation per compositor, and no way to write one tool
that runs everywhere, which is precisely what xdotool was for.

Read the KDE forums and you get the same answer, this time from the developers
themselves. Somebody wants to
[move the mouse to another screen](https://discuss.kde.org/t/move-mouse-to-screen/28971)
with a footswitch and is told, correctly, that Wayland's pointer API has no absolute
positioning. Somebody else asks
[what UI automation looks like on KWin](https://discuss.kde.org/t/questions-about-ui-automation-on-kwin-wayland/1778)
and gets a patient and entirely accurate reply: propose a protocol, get it reviewed,
implement it in the compositors, wire it through the portal. That is the right process
and it takes years. Meanwhile there is a footswitch on the floor that does not work.

So I checked whether the survey's conclusion was actually true, and it turns out to be
true about *protocols* and false about *reach*. The reach did not go away. Every
compositor still holds all of it. Each one just exposes some of it, differently,
through an interface it invented, and nobody had written the compatibility layer,
because writing four backends for one tool is boring and writing them for six tools is
worse.

I wrote them anyway.

Six commands came out of it. Four are drop-in clones with byte parity against the
originals, one is a clone of arandr's GUI, and one has no original at all.

```
wdotool   xdotool, all 48 commands
wwmctl    wmctrl, native Wayland and XWayland windows in one list
wxprop    xprop, both planes
wxrandr   xrandr, with multimonitor as the point rather than an afterthought
warandr   arandr, the drag your monitors window
wmirror   nothing. There is no xrandr syntax for what it does
```

No dependencies. Not "few dependencies", none: the whole thing is the Python standard
library, including the D-Bus client, the Wayland client and the X11 client. The one
exception is `warandr`, which imports the system GTK 3 bindings, because writing a
GUI toolkit as well would have been silly even by the standards of this project.

What follows is what the measurements found while I was building them, which turned
out to be more interesting than the code.

## What X11 got right, and what replaced it

The thing X11 got right was not a protocol. It was a **boundary in the wrong place**,
and the wrongness is exactly what made it useful.

On X11 there is a server, and the server holds every window, every property, every
input event and the whole screen layout, and it will talk to anybody who can open its
socket. `xdotool` types into your terminal because XTEST is a request like any other.
`wmctrl` closes a window because `_NET_CLOSE_WINDOW` is a message you send to the
root window. `xprop` reads a property because properties live in the server, not in
the application. `xrandr` moves a monitor because RandR is an extension of the same
socket. Four small C programs, none of which the window manager knows about, all of
which work on every window manager anyone ever wrote.

That is a security hole and everybody knew it. Any X client could read your
keystrokes. The Wayland answer was to move all of it inside the compositor, where a
client can only see and touch its own surfaces, and that answer is correct. It is
also why none of those four programs work any more.

Here is the real problem. Wayland closed the hole and then did not open a door.
There is no standard protocol to list windows. There is no standard protocol to move
one. There is no standard protocol to type a key. There is no standard protocol to
read a window property, because there are no window properties. There is a
`wlr-output-management` protocol for monitors that wlroots compositors implement and
GNOME and KDE do not. There is a **portal**, which asks the user for permission once
per session with a dialog, and that is fine for a screen sharing application and
useless for a line in a shell script bound to a hotkey.

That is the fragmentation Sissel is describing, and it is worth being precise about
what kind of problem it is. It is not that the capability is gone. It is that the
capability now lives at four different addresses, and nothing points from the old one
to the new ones. Every compositor has all of it, and each one exposes some of it,
differently, through an interface it invented.

So the actual project is not "reimplement xdotool". It is **find where each compositor
keeps the thing X11 used to hold, and prove that what you found is right**. The first
half is an afternoon of reading. The second half is the whole rest of this post.

## Four compositors, four answers

| | windows | input | monitors |
|---|---|---|---|
| **sway and wlroots** | its own i3 IPC, complete and documented | `zwp_virtual_keyboard_v1` and `zwlr_virtual_pointer_v1`, unprivileged | `zwlr_output_management_v1`, atomic |
| **GNOME** | nothing. I provide a Shell extension | nothing. `/dev/uinput` | `org.gnome.Mutter.DisplayConfig` on the session bus |
| **KDE Plasma** | `org.kde.kwin.Scripting.loadScript()`, which runs JavaScript inside the compositor | nothing. `/dev/uinput` | `kde_output_management_v2`, a Wayland protocol of its own |
| **X11 sessions** | the real tools. I just run them | the real tools | the real tools |

sway is the easy one and it is worth saying why: it has an IPC because i3 had an IPC,
and i3 had an IPC because somebody wanted to script their window manager. The
protocol was designed for the use case rather than around it.

GNOME is the hard one, and not for the reason you would guess. Mutter knows
everything. `global.display` has every window with its title, class, pid, frame rect
and workspace. It is simply not reachable: `org.gnome.Shell.Introspect` exists and is
sender-allowlisted, `org.gnome.Shell.Eval` is disabled outside unsafe mode, and there
is nothing else. So there is an extension in here, about fifteen hundred lines of
GJS, exporting the pieces the tools need on a bus name of its own. It never evaluates
code and it never injects input. It is also, and I say this in the threat model rather
than hiding it, a deliberate widening of GNOME's default: anything that can reach your
session bus can then list and kill your windows.

KDE is the funny one. You do not have to install anything, because
`org.kde.kwin.Scripting.loadScript()` is plain `Q_SCRIPTABLE` with no polkit action
and no bus policy, which means **any client on your session bus can already push
JavaScript into your compositor**. I use that. I am not the one who made it
possible.

### Where each of them lies to you

Every one of the three has a place where the obvious reading of its API is wrong, and
each of those cost me a day.

**Mutter counts a workspace that is not there.** GNOME's default is dynamic
workspaces, and with dynamic workspaces Mutter always keeps one trailing empty
workspace so you have somewhere to drag a window to. `get_n_workspaces()` counts it.
So `wmctrl -d` on a fresh GNOME session lists two workspaces when the user can see
one, and `wdotool set_num_desktops 4` is refused outright, because with dynamic
workspaces the count is not a thing you set. Both behaviours are correct and both
look like bugs. I report the count Mutter reports and refuse the setter with the
reason, which is the only honest pair.

**KWin has no window ids.** Not "different ids". None. The scripting API's only
handle is a UUID string, and everything downstream of it wants a number, because
wmctrl prints `0x%08lx` and `wxprop -id` parses into an XID. So I invent one:
`0x40000000` bitwise-or thirty bits of the internal id. The range is chosen so that an
invented id can never collide with an id Xwayland hands its own clients, because a
listing that mixes native and X windows must not let you confuse the two. If two
uuids collide in thirty bits, which happens in about one session in a million, the
second window gets a fresh id rather than being dropped.

**KWin saves your layout whether you asked or not.** xrandr's model is that a change
is temporary until something writes it down. KWin has no temporary mode at all: every
apply it accepts is written to `~/.config/kwinoutputconfig.json` within the same
second, the file is already there before you run anything, and `--persistent` is
accepted and means nothing. Deleting the file from inside the session achieves nothing
either, because KWin writes it again when it exits. So `wxrandr` prints, once, the
exact command that puts the previous layout back, because on that desktop it is the
only undo there is.

**Plasma 6.7 stopped publishing outputs.** Up to 6.6, each output is a
`kde_output_device_v2` global in the `wl_registry`, so you enumerate the registry and
you have your monitors. From 6.7.0 (kwin commit `7e32e00c`, never backported) the
globals are gone and the device objects come out of a
`kde_output_device_registry_v2` object instead. Not deprecated. Absent. On a real
6.7.4 session the old path finds zero outputs and reports, correctly and uselessly,
that this compositor has no monitors. `wxrandr` now tries both, and the fallback is
covered by a wire-level fake KWin that speaks both.

## Four things the measurements said that I did not expect

None of these came from reading a specification. All four came from running the thing
on a real desktop and watching a screenshot.

### 1. Both screens go entirely black

`wmirror` mirrors a region of one output onto another by running `wl-mirror`, which
opens a fullscreen window on the target and paints captured frames into it.

Now consider two outputs that share pixels. Not two outputs side by side: two that
overlap, or worse, two at the same position, which is exactly how you mirror on
wlroots. The mirror window is fullscreen on the target. The target's pixels
**are** the source's pixels. So the capture captures the window that is displaying
the capture.

I expected the classic video feedback tunnel. What I got, on a real sway session,
was both screens going entirely black. Every pixel, on both monitors, immediately. Not
a tunnel, not a flicker, just nothing at all until the mirror was killed from another
tty.

That is why `wmirror` refuses by name rather than warning. Two outputs that share a
rectangle is one of four configurations it will not start on, and the refusal names
the configuration and prints the `wxrandr` line that does what you actually wanted.

### 2. A display manager hands you a display whose cookie cannot open it

This one is the best kind of bug, because everything involved is behaving correctly.

Run something as root on a Plasma X11 session. `sudo wmctrl -l`, for example. Root has
no `DISPLAY` and no `XAUTHORITY`, so the tools find them for the user: logind knows
the session's display, and the cookie is normally in `$XAUTHORITY`, or in the runtime
directory, or in `~/.Xauthority`. I looked in all three. I found a cookie. I handed
it over. The X server said:

```
Authorization required, but no authorization protocol specified
```

SDDM 0.20 does not put its cookie in any of those places. It writes it to
`/tmp/xauth_<random>` and passes the path in the session leader's environment and
nowhere else. The cookie I found in `~/.Xauthority` was a real cookie for a real
display, left over, and it authorised nothing.

The fix is not a fourth path, it is a **first** one: read `/proc/<pid>/environ` of
the session's own leader process, uid-qualified, and believe that before anything
else. `gnome-shell`, `startplasma-x11`, `kwin_x11`, `plasmashell`, `xfce4-session`,
`sway`. It is the only route to SDDM's cookie and it is right on every desktop I
measured.

There is a second trap right next to it. On a machine with a display manager, the
lowest numbered runtime directory in `/run/user` is usually the **greeter's**. Its
cookie is perfectly valid, for the greeter's X server, which is not the one you want.
And `sudo -i` run by root leaves `SUDO_UID=0` behind, so believing `SUDO_UID` sends
the search into `/root`. On a real Xfce machine that was the difference between
`sudo -i xdotool getactivewindow` printing a window name and printing the
authorization error above.

### 3. Two maximize requests, racing a client that has not answered yet

`wmctrl -b add,maximized_vert,maximized_horz` is one command with two states in it.
On X11 it is one `_NET_WM_STATE` ClientMessage carrying two atoms, the window manager
applies both, done.

On Wayland a maximize is a **negotiation**. The compositor tells the client "you are
maximized now, here is your new size", and nothing has actually happened until the
client acks the configure and draws. Which means that if you send the vertical
maximize and then immediately read the state back, you get "not maximized", because
the client has not answered yet. Send the horizontal one immediately after and you are
now racing your own first request.

I found this on GNOME, from the opposite direction: removing the two states rather
than adding them. There it was worse than a wrong reading.
`wwmctl -b remove,maximized_vert,maximized_horz` removed only the horizontal half and
**corrupted the saved restore rectangle** doing it, so the window came back the wrong
size and only an explicit `windowsize` recovered it. The bridge was calling
`setMaximized()` twice where Mutter's own code path for the same two atoms is a
single `set_unmaximize_flags()`.

The general fix is to stop reading state immediately. On KDE the injected script now
waits for the window's own change signal, with a timer as a fallback, and answers from
whichever comes first, so a fullscreen on a native window no longer warns about a
state KWin had already applied. On GNOME the pair became one call. And the wait means
the **next** command sees a settled window, which is what makes a two-axis maximize
end with both axes on.

### 4. A window id match that got it wrong six times in eight

Plasma 6 dropped every scriptable property from `x11window.h`, so from inside a KWin
script there is no way to ask an XWayland window for its X id. `wwmctl` prints real X
ids, because that is the whole point of a listing that mixes both planes, so they had
to come from somewhere.

The somewhere is the X server's own `_NET_CLIENT_LIST`, matched against KWin's list:
filter on pid and `WM_CLASS`, then score on title and geometry distance, greedy best
first. It works, and for the ordinary desktop it is exact rather than approximate,
because `_NET_CLIENT_LIST` on that session is KWin's own window list with everything
but the managed X11 windows dropped.

Then I wrote the adversarial case, which is `repro/kde-xid-twins.py`: one X client,
two top level windows, same pid, same class, same title, same rectangle. Nothing but
the order of the two lists can tell them apart. `WM_WINDOW_ROLE` records which
window is which, because KWin exposes the role to scripts and the matcher never looks
at it, so the match can be checked without changing it.

The naive matcher swapped the two ids in six runs out of eight.

The fix is not a better score. It is a rule about when you are allowed to answer at
all: **a pair must agree on pid or on class**. An X client that publishes neither
`_NET_WM_PID` nor `WM_CLASS` contradicts nothing, so matching it on geometry alone
would hand its id to a native Wayland window, which would then claim to be an X11
client, which is worse than saying nothing. A pair that nothing separates keeps id 0.
Zero is a fine answer. A coin flip is not.

## What "no privilege on sway" costs everywhere else

Input is where this project is most obviously a compromise, and it is worth being
precise about what that compromise is.

The portable path is `/dev/uinput`. You create a virtual keyboard, a relative mouse
and an absolute tablet shaped like QEMU's usb-tablet, and the compositor cannot
distinguish them from real hardware, because at that layer they are real hardware.
This works on GNOME, on KDE, on sway, on anything. It costs root or a udev rule, and
it costs about 600 milliseconds of hotplug settling the first time, which is why there
is a daemon that owns the devices and why every later command is a client of it.

wlroots has something better. `zwp_virtual_keyboard_v1` lets a client **upload its
own keymap** and send keycodes against it, and `zwlr_virtual_pointer_v1` does motion,
buttons and scroll. Both are advertised to every client of your Wayland socket and
restricted to none. So on sway, an ordinary unprivileged process can type your
password into `swaylock`, and I measured that, because pretending otherwise would be
dishonest. wdotool uses those protocols exactly where the kernel device cannot be
opened, which turns a hard failure into working input and changes nothing where
uinput already works.

The uploaded keymap makes one problem disappear and creates another. It disappears
because the keymap reading the keycodes is the keymap just uploaded, so there is
no lookup to get wrong. It appears because that keymap is plain US, so a German
session reaches `ü` through the kernel path and not through this one.

And that lookup, on the kernel path, is the largest piece of code in the project that
nobody ever sees. wdotool injects **keycodes**, and the compositor reads them through
whatever XKB layout the session has active. A fixed US table types `z` for `y` on a
German layout. X11's trick was to rebind a spare keycode to the keysym you wanted,
and Wayland has no equivalent, so the lookup runs backwards instead: every Wayland
client is handed the full keymap on `wl_keyboard.keymap`, so it is parsed, the active
group taken, and character to keycode plus modifier mask built out of it. AltGr is
found in the keymap rather than assumed, because which key carries level three is
`<RALT>` on
German and `<CAPS>` on Neo. A character needing a dead key becomes two presses and
the application composes them.

That is a lot of machinery to get wrong, so the way it stays safe is the interesting
part. `active_group_is_plain_us()` verifies, key by key, that every keycode the fixed
table would emit carries exactly the keysyms the fixed table assumes at levels one and
two in the active group. If it holds, the old code path runs and **nothing else in the
layout module is called at all**. What it deliberately does not check matters as much
as what it does: only the printable characters, and a key's type only where the fixed
table actually presses level two. Checking more than that made a plain US session with
`caps:swapescape` fail the check and run the whole reverse map instead, which is a
fail-open, and a fail-open is the one thing the bypass exists to prevent.

There is one more thing on this path that is pure kernel trivia and that caused a real
defect. `--clearmodifiers` cannot clear a modifier held on your physical keyboard.
`input_handle_event()` **drops an `EV_KEY` release for a code the emitting device
does not hold**, so the key-up wdotool sends generates no event at all, and pressing
the modifier back afterwards would leave it stuck for the rest of the session, because
Mutter and KWin reference count key state across the seat's devices. So it clears only
what it holds itself, and it says which foreign modifier is blocking it when it can
read that, and it is silent with identical behaviour when it cannot.

## 5444 tests, and what these releases removed

The suite is the reason any of the sentences above can be written as facts. It has
byte parity oracles that run the real `xdotool`, `wmctrl`, `xprop` and `xrandr` and
diff its output against theirs. It has wire-level fakes: a fake KWin that speaks
`kde_output_device_v2` over a real unix socket in the real Wayland wire format, a
fake Mutter on an in-process D-Bus mock, a fake X server, and a **hostile** X server
that subclasses it and lies. It has live tests against a real headless sway with
XWayland. And it has thirteen VM images, eleven built from cloud images and two
installed from the release ISOs by the actual Ubuntu installer with every question
left at its default, because "it works on a default Ubuntu desktop" is a claim about an installed
system and a cloud image plus `ubuntu-desktop` measurably is not one. That distinction
is not pedantry: running the install guide verbatim on the real 24.04 install
corrected three sentences of it.

0.3 is a **subtraction** release, which is a strange thing to be proud of, so here is
what it removed.

Six copies of C's `atoi` became one module with C's semantics, including `[0-9]`
rather than `\d`, so a Unicode digit gives zero exactly as C does. Three getopt
wrappers became one. One hit-test that had been written three times, with three
tables of which window layers to look through, became one function over one table.
The detach protocol that both the gamma holder and the mirror supervisor needed,
double fork, a status line written before anything can fail, liveness as a
`(pid, starttime)` pair so a recycled pid is never mistaken for ours, and bounded
kills, became one module both of them call. Two copies of the Wayland to RandR
transform table became one. Four display backends that each had their own shape grew
the same six methods, so the session object holds one backend instead of four handles
and six name tests. Everything more than one tool needed became a package,
`w11common`, which is also why the single file builds of the display tools shrank by
more than half.

Seven fake Wayland servers became a marshaller library plus a server base: three of
them now build their compositor on that base, and the other four, whose sync replies
differ enough that a shared base would need a flag for each, take the marshallers
alone. Across the release the six packages lost a thousand and fourteen lines of
Python and the tests gained two thousand six hundred and seventy six, which is the
trade this release was for.

The other thing 0.3 did was stop lying in small ways. A README that said the test rig
had seven images when it had twelve. A design document that said the daemon costs 500
milliseconds of hotplug when the code sleeps 600. A note that the GNOME bridge is
version 1 when it is version 3. A sentence saying `warandr --save` fails without
`~/.screenlayout` when it creates the directory. A KWin undo line documented as
starting with the word `xrandr` when it starts with `wxrandr`, which matters
precisely because a KDE machine has a real `/usr/bin/xrandr` that would answer
`BadMatch` and change nothing. None of those were bugs. All of them were the
documentation drifting away from a tree that kept being measured, and that drift is
the thing a release like this exists to stop.

## What a real desktop found

Everything above was proved on virtual machines that get made, used and thrown away,
and that turns out to be a particular kind of desktop. A VM never logs out. It never
sits idle for an afternoon. Nobody configures a second keyboard layout on it and
switches to it. Nobody upgrades its GNOME underneath it. 0.4 is what happened when
those things were done on purpose.

The input daemon ran for as long as the machine was up. On a rig that is invisible.
On a real desktop a logout clears the runtime directory the socket lives in, so the
daemon is still running, still holding the lock, no client can ever dial it again,
and every later wdotool command in that boot fails to start one. Once it was looked
for it was on the rig too: a hundred and sixty one of them alive at the same time,
about three gigabytes between them, the oldest eighteen hours old, every one of them
listening on a socket path that had been deleted. It compares its own socket file by
inode every fifteen seconds now and exits when that file is gone or has been
replaced, and exits anyway after fifteen minutes with no client, which is an order of
magnitude past the gap between two commands of one piece of work.

Then a Greek layout. Everything that types had been falling back to the built-in US
table for anything the active layout could not produce, and for a chord that means
pressing whichever key is `s` on a US keyboard. On Greek that key is sigma, so
`key ctrl+s` gave Kate control and sigma, which saved nothing, said nothing and
exited 0. The layout is the authority now and the chord is refused with a line, which
is what `type` had always done for a single character. The oracle was a sway binding
on the US `s` position: with `us` the file appears, with `gr` it does not and there
is a warning, with `us,gr` it appears again. The same session found the notice that
says which of several layouts wdotool had to assume. It was printed once and then the
wrong characters were typed in silence, because the second command hit the layout
cache and returned before ever reaching the line that prints it. It reaches every
command now, and the variable it tells you to set is read from the environment of the
command and carried to the daemon with the text, because the daemon keeps the
environment it was spawned with and outlives it.

Fixing the notice was the smaller half of that, because what the notice announced was
a guess, and the guess was wrong on every session whose user had switched layouts.
`wl_keyboard.modifiers` carries the live group and every compositor sends it to the
focused window only, which an injector never is. Both of the big desktops publish it
somewhere else instead. KWin answers a method on the session bus with the index of
the active layout in the list you configured, and that list is the keymap's group
order, so the group is the index plus one. GNOME keeps it in the input-sources
setting its shell writes on every switch, and the desktop portal serves that setting
to anybody who asks, which is one call with no consent step behind it. Reading it
turned `wdotool type 'yz@'` on a session switched to German from `zy"` into `yz@`,
byte for byte, on both Plasma generations and both GNOME ones. The notice went quiet
with it, because there was nothing left to assume, and a GNOME desktop with a single
layout configured had been getting that notice on every command it ever ran: Mutter
appends a `us` group of its own, so one layout and two look identical in the keymap
and only the setting tells them apart. Where nothing answers, the guess and the
notice are exactly what they were.

And a saved display configuration. GNOME reads `~/.config/monitors.xml` back through
the same validator that refuses overlapping monitors, and one entry it does not like
throws away the whole file, at every boot, with nothing said on any screen. Nothing
here can put a bad entry in it. What can is a settings change made afterwards: with
fractional scaling switched off the positions are saved in physical pixels and the
file records no layout mode, so switching that setting on re-reads them as logical
pixels, a scaled monitor stops reaching its neighbour, and the file goes. So the
option that saves a layout reads the file first and says when GNOME has already
discarded it, says when the layout being saved is one that setting would break, and
keeps a copy of what it replaced.

The fourth thing is the one I am least comfortable with, and it has a section of its
own in the README rather than a paragraph here. GNOME will not place two monitors so
that they share screen area, by a validator every route into Mutter goes through, and
0.4 has one way through it: a second Shell extension that the package ships and that
nothing enables, an agreement you record against the exact build of GNOME the checks
ran on, and eight bytes per monitor written into a private structure of the running
`gnome-shell`. Six checks refuse any build they do not recognise. Twelve
deliberately wrong descriptions of that structure were installed on purpose and every
one was refused by name, before any write, with the session still running. It is
still a bet, it is off, and if you are not sure you want it you do not.

None of this is the protocol the survey asked for. A compatibility layer is what you
write while you wait for one, and it takes the shape of every desktop whose
differences it has to hide, which is why there is a GNOME Shell extension in here and
why I am not happy about it either. But the four-line script works again, on GNOME and
on KDE and on sway and on X11, and it does not know which. That was the entire
requirement.

The tools are at [github.com/zardus/w11](https://github.com/zardus/w11).
[Technical.md](Technical.md) is where to start if you want to change something.
