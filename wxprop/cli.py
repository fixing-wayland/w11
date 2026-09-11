"""xprop-compatible option parsing and dispatch.

xprop has a hand-rolled parser (xprop.c main + dsimple.c) and order
matters; this is a straight port of the 1.2.8 RELEASE binary (verified
against the oracle — it differs from post-1.2.8 git master, which added a
pre-scan that accepts --help/--version; the release does NOT):

1. Get_Display_Name: -display/-d consumed anywhere before a lone "-";
2. Select_Window_Args: -root/-id/-name consumed anywhere (later wins,
   -name resolved immediately, a lone "-" stops the scan);
3. the option loop, stopping at the first argument not starting with "-":
   -grammar/-help/-version act HERE, in argv order (single dash only) —
   so `xprop -badflag -version` is a usage error, and on a dead display
   even `xprop -version` fails to open it first (both oracle-verified);
4. trailing `[format [dformat]] atom` spec groups.

Program name: like xprop we print argv[0] verbatim in messages -- xprop
sets `program_name = argv[0]` and never trims it, so an absolute path in
argv[0] comes out as one (WXPROP_ARGV0 overrides it, and `python -m wxprop`
runs, where argv[0] is __main__.py, print "wxprop").
Deliberate deviations, documented in tests: -version prints "xprop 1.2.8"
for drop-in version-sniffing, which is NOT what the oracle on either
supported flavor prints (1.2.6 on 24.04, 1.2.7 on 26.04); a missing X
server degrades to the native plane instead of "unable to open display"
(unless -display was explicit), so `wxprop -version` with no server still
prints (the option loop reaches it); and click-to-select is the compositor
next-focus wait with a stderr hint.
"""

import os
import re
import struct
import sys

from w11common import passthrough, stdio
from wdotool import backend
from wdotool.cnum import atoi as _atoi
from wxprop import core
from wxprop import fmt as fmtmod
from wxprop.fmt import FatalError

MAXSTR = fmtmod.MAXSTR

# what -version prints. NOT byte parity with the oracle: the binary on both supported flavors is 1.2.6/1.2.7,
# and this string is deliberately the newest release, so a version-sniffing script sees a clone that implements
# everything it may ask for. The package identity lives in wxprop.VERSION.
XPROP_VERSION = "xprop 1.2.8"

HELP_MESSAGE = """\
where options include:
    -help                          print out a summary of command line options
    -grammar                       print out full grammar for command line
    -display host:dpy              the X server to contact
    -id id                         resource id of window to examine
    -name name                     name of window to examine
    -font name                     name of font to examine
    -remove propname               remove a property
    -set propname value            set a property to a given value
    -root                          examine the root window
    -len n                         display at most n bytes of any property
    -notype                        do not display the type field
    -fs filename                   where to look for formats for properties
    -frame                         don't ignore window manager frames
    -f propname format [dformat]   formats to use for property of given name
    -spy                           examine window properties forever
    -version                       print program version
"""

GRAMMAR_TAIL = """\


\tdisp ::= -display host:dpy
\tselect option ::= -root | -id <id> | -font <font> | -name <name>
\toption ::= -len <n> | -notype | -spy | {-formats|-fs} <format file>
\tmapping ::= {-f|-format} <atom> <format> [<dformat>]
\t            | -remove <propname>
\t            | -set <propname> <value>
\tspec ::= [<format> [<dformat>]] <atom>
\tformat ::= {0|8|16|32}{a|b|c|i|m|s|t|x}*
\tdformat ::= <unit><unit>*             (can't start with a letter or '-')
\tunit ::= ?<exp>(<unit>*) | $<n> | <display char>
\texp ::= <term> | <term>=<exp> | !<exp>
\tterm ::= <n> | $<n> | m<n>
\tdisplay char ::= <normal char> | \\<non digit char> | \\<octal number>
\tnormal char ::= <any char except a digit, $, ?, \\, or )>

"""


class UsageError(Exception):
    """usage(errmsg): help text to stderr, exit 1."""

    def __init__(self, msg=None):
        super().__init__(msg or "")
        self.msg = msg


# argv[0] as the oracle prints it; core needs it too (the merged root's
# -set/-remove note), so it lives there
_progname = core._progname


def print_help(prog: str):
    sys.stdout.flush()
    try:
        sys.stdout.buffer.flush()
    except (AttributeError, ValueError):
        pass
    sys.stderr.write("usage:  %s [-options ...] [[format [dformat]] atom]"
                     " ...\n\n" % prog)
    sys.stderr.write("%s\n" % HELP_MESSAGE)


def print_grammar(prog: str):
    sys.stdout.write("Grammar for xprop:\n\n")
    sys.stdout.write("\t%s [<disp>] [<select option>] <option>* <mapping>*"
                     " <spec>*" % prog)
    sys.stdout.write(GRAMMAR_TAIL)


def _setup_locale():
    """setlocale(LC_CTYPE, "") + nl_langinfo(CODESET) == "UTF-8" -- asked as a question, and left the way it was
    found.

    Only the answer is ever used: every byte we print goes out of `sys.stdout.buffer`, so nothing downstream
    reads the C locale. Leaving LC_CTYPE switched is a change to global process state, and since 3.12 it is
    state `open()` reads -- so a caller that runs `main()` in-process (the suite does, and with LC_ALL=C in the
    environment) would find its own default text encoding quietly become ASCII for the rest of the run.
    """
    try:
        import locale
    except Exception:
        return False
    try:
        prev = locale.setlocale(locale.LC_CTYPE)
    except Exception:
        prev = None
    try:
        locale.setlocale(locale.LC_CTYPE, "")
        return locale.nl_langinfo(locale.CODESET) == "UTF-8"
    except Exception:
        return False
    finally:
        if prev is not None:
            try:
                locale.setlocale(locale.LC_CTYPE, prev)
            except Exception:
                pass


def _term_width() -> int:
    """xprop's Format_Icons width: TIOCGWINSZ on stdin when it is a tty,
    else 144+8."""
    try:
        if sys.stdin.isatty():
            import fcntl
            import termios
            buf = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
            rows, cols = struct.unpack("HH", buf[:4])
            if cols:
                return cols
    except Exception:
        pass
    return 144 + 8


def _c_int(v: int) -> int:
    """A Python int as C would keep it in an `int`."""
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v >= 0x80000000 else v


def _strtoul(s: str) -> int:
    """strtoul(s, NULL, 0): C number syntax, invalid -> 0. Overflow SATURATES the magnitude at ULONG_MAX
    (glibc's ERANGE return) before the sign is applied in unsigned arithmetic — so "-1" is ULONG_MAX, an
    overflowing positive is ULONG_MAX (verified: -set 32c 18446744073709551617 stores 0xffffffff, not 1), and an
    overflowing negative wraps small, exactly like glibc."""
    m = re.match(r"[ \t\n\v\f\r]*([+-]?)(0[xX][0-9a-fA-F]+|[0-9]+)", s)
    if not m:
        return 0
    sign, digits = m.groups()
    if digits[:2].lower() == "0x":
        val = int(digits, 16)
    elif digits.startswith("0") and len(digits) > 1:
        val = int(re.match(r"0[0-7]*", digits).group(0), 8)
    else:
        val = int(digits, 10)
    if val > 0xFFFFFFFFFFFFFFFF:  # strtoul saturates the magnitude first
        val = 0xFFFFFFFFFFFFFFFF
    if sign == "-":
        val = -val
    return val & 0xFFFFFFFFFFFFFFFF


def _parse_window_id(arg: str) -> int:
    """dsimple.c: sscanf("0x%lx") else sscanf("%lu"), partial parses count, 0 (or nothing) is fatal.

    The literal "0x" of the first format matches no whitespace and no uppercase X -- sscanf only skips
    whitespace for a conversion, and the format has none before the 0 -- so " 0x20" and "0X20" fall through to
    %lu, parse as 0 and are fatal (verified against the oracle). %lu is strtoul, which does skip whitespace and
    does accept a sign: "-5" is ULONG_MAX-4, wrapped into the id below."""
    w = 0
    m = re.match(r"0x([0-9a-fA-F]+)", arg)
    if m:
        w = int(m.group(1), 16)
    if not w:
        m = re.match(r"[ \t\n\v\f\r]*([-+]?[0-9]+)", arg)
        if m:
            w = int(m.group(1))
    if not w:
        raise FatalError("Invalid window id format: %s." % arg)
    return w & 0xFFFFFFFF


# -- format files (-fs / $XPROPFORMATS): Read_Mappings ----------------------

_WS = b" \t\n\v\f\r"
_MAX_FORMAT_FILE = 1 << 24  # hardening: no sane format file is 16MB


def _read_mappings_text(formatter, data: bytes):
    pos = 0
    n = len(data)

    def skip_ws(p):
        while p < n and data[p] in _WS:
            p += 1
        return p

    def token(p, width):
        start = p
        while p < n and data[p] not in _WS and p - start < width:
            p += 1
        return data[start:p], p

    while True:
        pos = skip_ws(pos)
        if pos >= n:
            return
        name, pos = token(pos, 990)
        pos = skip_ws(pos)
        if pos >= n:
            raise FatalError("Bad format file format.")
        fmt_b, pos = token(pos, 90)
        pos = skip_ws(pos)
        dfmt_b = fmtmod.DEFAULT_DFORMAT
        if pos < n and data[pos] == 0x27:  # '
            dfmt_b, pos = _read_quoted(data, pos)
        formatter.add_mapping(name, fmt_b, dfmt_b)


def _read_quoted(data: bytes, pos: int):
    """Read_Quoted: chars up to an unescaped ', backslash keeps both chars
    except backslash-newline which drops both (line continuation)."""
    pos += 1  # opening quote
    out = bytearray()
    n = len(data)
    while True:
        if len(out) > MAXSTR:
            raise FatalError("Bad format file format: dformat too long.")
        if pos >= n:
            raise FatalError("Bad format file: Unexpected EOF.")
        c = data[pos]
        pos += 1
        if c == 0x27:  # '
            return bytes(out), pos
        out.append(c)
        if c == 0x5C:  # backslash
            if pos >= n:
                raise FatalError("Bad format file: Unexpected EOF.")
            c2 = data[pos]
            pos += 1
            if c2 == 0x0A:
                out.pop()  # \<newline>: line continuation, both dropped
            else:
                out.append(c2)


def _read_mappings_file(formatter, path: str):
    try:
        with open(path, "rb") as f:
            data = f.read(_MAX_FORMAT_FILE)
    except OSError:
        raise FatalError("unable to open file %s for reading." % path) from None
    _read_mappings_text(formatter, data)


# -- argv pre-passes ---------------------------------------------------------


def _extract_display(args):
    """Get_Display_Name: -display/-d consumed anywhere; a lone "-" stops
    the scan and keeps everything from itself on."""
    out = []
    display = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-display", "-d"):
            i += 1
            if i >= len(args):
                raise UsageError("-display requires an argument")
            display = args[i]
            i += 1
            continue
        if a == "-":
            out.extend(args[i:])
            break
        out.append(a)
        i += 1
    return display, out


def _select_window_args(sess, args):
    """Select_Window_Args: -root/-id/-name consumed anywhere, later wins, -name resolved on the spot (its
    no-match error fires before any later bad-flag diagnosis, like real xprop)."""
    spec = None
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "-":
            out.append(a)
            out.extend(args[i:])
            break
        if a == "-root":
            spec = ("root",)
            continue
        if a == "-name":
            if i >= len(args):
                raise UsageError("-name requires an argument")
            spec = ("target", core.resolve_name(sess, args[i]))
            i += 1
            continue
        if a == "-id":
            if i >= len(args):
                raise UsageError("-id requires an argument")
            spec = ("id", _parse_window_id(args[i]))
            i += 1
            continue
        out.append(a)
    return spec, out


# -- -set value conversions (Set_Property, xprop.c:1643) ---------------------


def _pack_ints(vals, size: int) -> bytes:
    mask = (1 << size) - 1
    code = {8: "<%dB", 16: "<%dH", 32: "<%dI"}[size]
    return struct.pack(code % len(vals), *[v & mask for v in vals])


def _parse_int_list(value: str):
    toks = [t for t in value.split(",") if t != ""]
    if not toks:
        return [0]  # real xprop segfaults on strtoul(NULL); we don't
    vals = []
    for t in toks:
        vals.append(_strtoul(t))
        if len(vals) == 64:
            sys.stderr.write("Maximum number of elements reached (64). "
                             "List truncated.\n")
            break
    return vals


def _set_property(formatter, target, prog: str, utf8_locale: bool, name_s: str, value_s: str):
    name_b = os.fsencode(name_s)
    target.intern(name_b, create=True)  # Parse_Atom(propname, False)
    f, _d = formatter.lookup_formats(name_b, None, None)
    if f is None:
        raise FatalError("unsupported conversion for %s" % name_s)
    size = fmtmod.get_format_size(f)
    char = fmtmod.get_format_char(f, 0)
    if char in (0x73, 0x75, 0x74):  # s u t
        letter = chr(char)
        if size != 8:
            raise FatalError("can't use format character '%s' with any "
                             "size except 8." % letter)
        if char == 0x73:
            target.set_prop(name_b, "STRING", 8, os.fsencode(value_s))
        elif char == 0x75:
            target.set_prop(name_b, "UTF8_STRING", 8, os.fsencode(value_s))
        else:  # t: XStdICCTextStyle — STRING when latin-1 representable
            raw = os.fsencode(value_s)
            if utf8_locale:
                try:
                    raw = raw.decode("utf-8").encode("latin-1")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    sys.stderr.write("cannot convert %s argument to STRING "
                                     "or COMPOUND_TEXT.\n" % name_s)
                    return
            target.set_prop(name_b, "STRING", 8, raw)
    elif char in (0x63, 0x78):  # c x -> CARDINAL
        vals = _parse_int_list(value_s)
        target.set_prop(name_b, "CARDINAL", size, _pack_ints(vals, size) if size in (8, 16, 32) else b"")
    elif char == 0x69:  # i -> INTEGER
        vals = _parse_int_list(value_s)
        target.set_prop(name_b, "INTEGER", size, _pack_ints(vals, size) if size in (8, 16, 32) else b"")
    elif char == 0x62:  # b -> INTEGER True/False
        if value_s == "True":
            v = 1
        elif value_s == "False":
            v = 0
        else:
            sys.stderr.write("cannot convert %s argument to Bool\n" % name_s)
            return
        eff = size if size in (8, 16, 32) else 32  # C: case 32: default:
        target.set_prop(name_b, "INTEGER", eff, _pack_ints([v], eff))
    elif char == 0x61:  # a -> ATOM
        # only reached on the X plane (step 5 fataled for any other), so
        # target always has a live .conn — no None fallback needed
        atom = target.conn.atom(value_s)
        data = struct.pack("<Q", atom)[:size // 8] if size in (8, 16, 32) else b""
        target.set_prop(name_b, "ATOM", size, data)
    else:  # 'm' is NYI in xprop too
        raise FatalError("bad format character: %s" % chr(char))


# -- main --------------------------------------------------------------------


def main(argv=None) -> int:
    stdio.repair_std()      # fd 1 or 2 closed before Python started
    prog = _progname()
    backend.set_program(prog)
    # X11 session: hand over to the real xprop. Unlike the other three we do have a native X11 path
    # (core.Session talks to $DISPLAY directly), so a box with no x11-utils installed keeps working instead of
    # exiting 127.
    rc = passthrough.maybe_exec_real(
        "xprop", sys.argv[1:] if argv is None else argv, entry=argv is None,
        fallback_native=True)
    if rc is not None:
        return rc
    # Wayland with the original installed: the original ITSELF, run against
    # the xw11 display, which answers for the whole desktop (design section
    # 8.1).  Imported here and not at the top of the file: on an X11 session
    # main() has already left above, so `xw11.wrap` is never imported there --
    # and the wxprop zipapp carries no xw11/ (scripts/build-pyz.sh).
    try:
        from xw11.wrap import maybe_exec_through_proxy
    except ImportError:                 # pragma: no cover - a bundle without xw11/
        maybe_exec_through_proxy = None
    if maybe_exec_through_proxy is not None:
        rc = maybe_exec_through_proxy(
            "xprop", sys.argv[1:] if argv is None else argv, entry=argv is None)
        if rc is not None:
            return rc
    quiet = False
    try:
        code = _main(prog, list(sys.argv[1:] if argv is None else argv))
    except SystemExit as e:
        stdio.exit_after_flush(prog, e)
        raise                       # unreachable; the line above raises
    except UsageError as e:
        if e.msg:
            stdio.warn("%s: %s\n\n" % (prog, e.msg))
        print_help(prog)
        code = 1
    except FatalError as e:
        stdio.warn("%s: error: %s\n" % (prog, e))
        code = 1
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        code = 1
    except Exception as e:
        # X protocol errors print Xlib's classic block; anything else is a
        # one-line fatal (never a traceback).  DEBUG set to anything (including "") is the escape hatch for a
        # bug report: the exception propagates and Python prints the traceback it would have.
        if os.environ.get("DEBUG") is not None:
            raise
        if hasattr(e, "code") and hasattr(e, "major"):
            stdio.warn(core.x_error_report(e))
        else:
            stdio.warn("%s: error: %s\n" % (prog, e))
        # An OSError here is a write to stdout that failed (a full disk, a quota, `>/dev/full`): the flush below
        # is about to fail with the same errno, and the originals print one line, not two.
        quiet = isinstance(e, OSError)
        code = 1
    return code if stdio.flush_stdout(prog, quiet) else (code or 1)


def _out_write(data: bytes):
    sys.stdout.buffer.write(data)


def _main(prog: str, args) -> int:
    utf8_locale = _setup_locale()

    # 1. display selection (Get_Display_Name). The 1.2.8 release has no
    # pre-scan for -grammar/-help/-version — they live in the option loop
    # below, after the display is opened, so a dead -display fails first.
    display, args = _extract_display(args)
    sess = core.Session(display)
    if display is not None and sess.x11() is None:
        sys.stderr.write("%s:  unable to open display '%s'\n" % (prog, display))
        return 1

    # 2. window selection args
    spec, args = _select_window_args(sess, args)

    formatter = fmtmod.Formatter(
        notype=False, max_len=MAXSTR, utf8_locale=utf8_locale,
        truecolor=(os.environ.get("COLORTERM") == "truecolor"),
        term_width=_term_width())
    # xprop picks its property-format table before the option loop runs, by scanning argv for -font
    # (xprop.c:1961): the FONT table replaces the window one for the whole run.
    if "-font" in args:
        formatter.setup_font_table()
    else:
        formatter.setup_window_table()
    xpropformats = os.environ.get("XPROPFORMATS")
    if xpropformats:
        _read_mappings_file(formatter, xpropformats)

    # 3. the option loop (xprop.c:2009) — stops at the first non-'-' arg;
    # -grammar/-help/-version act HERE, in argv order (release behavior,
    # single dash only — --grammar/--help/--version are unrecognized)
    spy = False
    removes = []
    sets = []
    font_name = None
    pending_interns = []
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        i += 1
        if a == "-":
            continue
        if a == "-grammar":
            print_grammar(prog)
            return 0
        if a == "-help":
            print_help(prog)
            return 0
        if a == "-version":
            print(XPROP_VERSION)
            return 0
        if a == "-notype":
            formatter.notype = True
            continue
        if a == "-spy":
            spy = True
            continue
        if a == "-len":
            if i >= len(args):
                raise UsageError("-len requires an argument")
            # xprop keeps max_len in a long but parses it with atoi(), which returns an int: "-len 4294967296"
            # is 0 there (nothing is printed), and "-len 2147483648" is INT_MIN, whose negative word count
            # reaches the server as a huge unsigned one and fetches everything.
            formatter.max_len = _c_int(_atoi(args[i]))
            i += 1
            continue
        if a in ("-formats", "-fs"):
            if i >= len(args):
                raise UsageError("-fs requires an argument")
            _read_mappings_file(formatter, args[i])
            i += 1
            continue
        if a == "-font":
            if i >= len(args):
                raise UsageError("-font requires an argument")
            font_name = args[i]
            i += 1
            continue
        if a == "-remove":
            if i >= len(args):
                raise UsageError("-remove requires an argument")
            removes.append(args[i])
            i += 1
            continue
        if a == "-set":
            if i + 2 > len(args):
                raise UsageError("insufficient arguments for -set")
            sets.append((args[i], args[i + 1]))
            # xprop.c:2052 advances argv by 3 AND the loop increment eats one more: `-set name value` swallows
            # the next argument unexamined. Bug-for-bug — the oracle really does this.
            i += 3
            continue
        if a == "-frame":
            # accepted for parity and nothing else: xprop's -frame picks the frame under a click, and on
            # Wayland the compositor owns the frame, so there is no other window to pick
            continue
        if a in ("-f", "-format"):
            if i >= len(args):
                raise UsageError("insufficient arguments for -format")
            name = args[i]
            i += 1
            if i >= len(args):
                raise UsageError("insufficient arguments for -format")
            f_arg = args[i]
            i += 1
            if not fmtmod.is_a_format(f_arg):
                raise FatalError("Bad format: %s." % f_arg)
            dformat = None
            if i < len(args) and fmtmod.is_a_dformat(args[i]):
                dformat = os.fsencode(args[i])
                i += 1
            pending_interns.append(name)
            formatter.add_mapping(os.fsencode(name), os.fsencode(f_arg), dformat)
            continue
        sys.stderr.write("%s: unrecognized argument %s\n\n" % (prog, a))
        raise UsageError(None)
    specs_args = args[i:]

    if (removes or sets) and specs_args:
        sys.stderr.write("%s: unrecognized argument %s\n\n" % (prog, specs_args[0]))
        raise UsageError(None)

    # 4. resolve the target window (or the font, which replaces it)
    if font_name is not None:
        target = core.resolve_font(sess, font_name)
    elif spec is None:
        target = core.select_target(sess, prog)
    elif spec[0] == "root":
        target = core.resolve_root(sess)
    elif spec[0] == "id":
        target = core.resolve_id(sess, spec[1])
    else:
        target = spec[1]
    formatter.atom_name = target.atom_name
    for name in pending_interns:  # -f's Parse_Atom(name, False)
        target.intern(os.fsencode(name), create=True)

    # 5. -remove / -set: apply and exit 0, printing nothing on success
    if removes or sets:
        if target.plane == "font":
            what = "-remove" if removes else "-set"
            raise FatalError("%s works only on windows, not fonts" % what)
        if target.plane == "missing":
            target.intern(b"", create=False)  # raises does-not-exists
        if target.plane != "x":
            what = "-remove" if removes else "-set"
            raise FatalError("%s cannot work on a native Wayland window "
                             "(it has no X property store)" % what)
        for name in removes:
            if not target.remove_prop(os.fsencode(name)):
                sys.stderr.write('%s:  no such property "%s"\n' % (prog, name))
        for name, value in sets:
            _set_property(formatter, target, prog, utf8_locale, name, value)
        return 0

    # 6. property requests (Handle_Prop_Requests) — written per property,
    # like printf, so a Fatal mid-list keeps the output printed so far
    specs = None
    if specs_args:
        specs = []
        j = 0
        while j < len(specs_args):
            f_b = d_b = None
            if fmtmod.is_a_format(specs_args[j]):
                f_b = os.fsencode(specs_args[j])
                j += 1
                if j >= len(specs_args):
                    raise UsageError("format specified without atom")
            if fmtmod.is_a_dformat(specs_args[j]):
                d_b = os.fsencode(specs_args[j])
                j += 1
                if j >= len(specs_args):
                    raise UsageError("dformat specified without atom")
            prop_b = os.fsencode(specs_args[j])
            j += 1
            if target.intern(prop_b, create=False):
                specs.append((prop_b, f_b, d_b))
            seg = bytearray()
            # xprop writes each property as it renders it, so a fatal halfway through a value still leaves the
            # name line (and every property before it) on stdout. Ours built the whole segment first and dropped
            # it on the way out.
            try:
                core.show_prop(formatter, target, seg, f_b, d_b, prop_b)
            finally:
                _out_write(bytes(seg))
    else:
        for name_b in target.list_names():
            seg = bytearray()
            try:
                core.show_prop(formatter, target, seg, None, None, name_b)
            finally:
                _out_write(bytes(seg))

    # 7. -spy
    if spy:
        if target.plane == "font":
            return 0   # a font has no property events; the oracle exits too
        sys.stdout.buffer.flush()
        if isinstance(target, core.MergedRootTarget):
            return core.spy_merged_root(formatter, target, specs)
        if target.plane == "x":
            return core.spy_x(formatter, target, specs)
        if isinstance(target, core.NativeRootTarget):
            return core.spy_native_root(formatter, target, specs)
        return core.spy_native_view(formatter, target, specs)
    return 0
