// w11-overlap -- put two GNOME monitors on top of each other.
//
// THIS IS NOT THE BRIDGE.  `w11-bridge@w11` is plain
// feature-detected JavaScript against public APIs and works on every Shell
// major its metadata.json lists; it is what wdotool, wwmctl and wxprop need and
// it is safe to leave installed for ever.  This extension is a different kind
// of thing: it ships a compiled type description pinned to a *private*
// structure layout inside libmutter, and it writes into gnome-shell's own
// heap.  A wrong layout is a dead compositor, and on Wayland a dead compositor
// is the whole session.
//
// Three rules hold it together.  See docs/Technical.md section 6.
//
//   1. IT DOES NOTHING AT LOGIN.  enable() exports one D-Bus object and stops.
//      No typelib is loaded, no libmutter symbol is touched, nothing is read
//      and nothing is written until somebody calls a method.  The catastrophic
//      failure is a crash at session start with no way to reach a setting to
//      switch this off, so there is no code that can run then.
//   2. EVERY CHECK RUNS ON EVERY CALL, never once at install: a distribution
//      upgrade can replace libmutter under a running session.
//   3. NO POINTER IS EVER DEREFERENCED BY THE TYPE SYSTEM.  The shipped type
//      description declares every pointer as guint64, so reading one yields a
//      number; each step to the next struct is g_memdup2(ptr, n) with the
//      address range-checked against /proc/self/maps first.  A wrong offset
//      then reads garbage, which the comparison against Mutter's public view
//      rejects, instead of walking a wild pointer into a SIGSEGV.
//
// It can never cause ~/.config/monitors.xml to be written: the type
// description does not declare meta_monitor_config_manager_save_current (or
// any other writer), the apply method is the constant METHOD_TEMPORARY, and
// the file's digest is taken before and after and returned to the caller.

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import GObject from 'gi://GObject';
import GIRepository from 'gi://GIRepository';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// Every decision with a right answer lives next door, in a file with no gi
// imports, so that tests/test_gnome_overlap.py can run it under plain node.
import {generations, generationFor, knownMajors, describeTable} from './rules.js';
import {shellMajor, sonameToken, forceGate, selectByStructSize, TABLE_FIELDS} from './rules.js';
import {mutterFault, modalVerdict, libmutterNote, isForceable} from './rules.js';
import {parseMutterMappings, mutterSonames, key, cmpRegion, le32, compare, drift} from './rules.js';

const BUS_NAME = 'org.w11.Overlap';
const OBJECT_PATH = '/org/w11/Overlap';
const IFACE_NAME = 'org.w11.Overlap1';
const VERSION = 1;

// META_MONITORS_CONFIG_METHOD_TEMPORARY.  The one and only value this file
// ever passes to meta_monitor_manager_apply_monitors_config: 2 (PERSISTENT) is
// what makes Mutter write ~/.config/monitors.xml, and that must be
// unreachable, not merely unused.  tests/test_gnome_overlap.py asserts that no
// other constant reaches the apply.
const METHOD_TEMPORARY = 1;

const MAX_MONITORS = 16;        // list walks are bounded; nothing here loops on heap data
const MAX_CONNECTOR = 63;       // g_strndup bound for a connector name
const SENTINEL = 0x5f5a;        // written through Mutter's own setter, read back at our offset

const IFACE_XML = `
<node>
  <interface name="${IFACE_NAME}">
    <method name="Probe">
      <arg type="s" name="request" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <method name="ApplyOverlap">
      <arg type="s" name="request" direction="in"/>
      <arg type="s" name="result" direction="out"/>
    </method>
    <property name="Version" type="u" access="read"/>
  </interface>
</node>`;

// -- refusals ----------------------------------------------------------------

class Refused extends Error {
    // `found` is what had been measured when this refusal happened, if
    // anything: the versions in the room, the size the GType registry reports.
    // It is carried out to the caller so that a refusal on a build nobody has
    // measured prints what a maintainer needs to add it, instead of sending
    // them back to a debugger for numbers this already had.
    constructor(check, detail, found) {
        super(detail);
        this.check = check;
        this.found = found || null;
    }
}

const refuse = (check, detail, found) => {
    throw new Refused(check, detail, found);
};

// -- /proc/self/maps ---------------------------------------------------------
//
// The only thing standing between a wrong offset and a compositor crash.  An
// address that is not inside a readable mapping is never handed to g_memdup2.

class Maps {
    constructor() {
        this._ranges = [];
        this.reload();
    }

    reload() {
        const [ok, bytes] = GLib.file_get_contents('/proc/self/maps');
        if (!ok)
            refuse('maps', 'cannot read /proc/self/maps');
        const ranges = [];
        for (const line of new TextDecoder().decode(bytes).split('\n')) {
            const m = line.match(/^([0-9a-f]+)-([0-9a-f]+) (....)/);
            if (m && m[3][0] === 'r')
                ranges.push([parseInt(m[1], 16), parseInt(m[2], 16)]);
        }
        this._ranges = ranges;
        this.text = new TextDecoder().decode(bytes);
    }

    holds(p, n) {
        if (!Number.isSafeInteger(p) || p < 0x1000 || n <= 0)
            return false;
        const hit = () => this._ranges.some(([a, b]) => p >= a && p + n <= b);
        if (hit())
            return true;
        this.reload();          // the heap may have grown since we last looked
        return hit();
    }

    // Every libmutter mapped into this process, by soname -- the string, not a
    // number parsed out of it: since mutter 51 the number in that name is the
    // GNOME major rather than a generation of its own, and the table is keyed
    // on names for exactly that reason.
    mutters() {
        return mutterSonames(this.text);
    }

    // The same mappings with the rest of the line: the path, the inode the
    // kernel says that mapping came from, and whether the file behind it has
    // been unlinked since.  Nothing here is a guard -- the guards all run
    // against the library in memory, which is the one being written to -- it is
    // what lets the answer say which build that was.  See libmutterNote().
    mutterMappings() {
        return parseMutterMappings(this.text);
    }
}

// -- which build of libmutter this session is running ------------------------
//
// Two file reads and no compositor state: the ELF note in the mapped library,
// and the inode of the path it was mapped from.  Every failure is a null, never
// a refusal -- this identifies the build for the answer and for the agreement
// wxrandr records against it, and a library that will not say its build id is
// not a library that is unsafe to write to.

const ELF_PREFIX = 1 << 16;     // the GNU note lives in the first pages
const PT_NOTE = 4;
const NT_GNU_BUILD_ID = 3;

function elfBuildId(path) {
    let u8;
    try {
        const stream = Gio.File.new_for_path(path).read(null);
        try {
            u8 = stream.read_bytes(ELF_PREFIX, null).get_data();
        } finally {
            stream.close(null);
        }
    } catch (e) {
        return null;
    }
    if (!u8 || u8.length < 64)
        return null;
    // ELF64, little-endian, and nothing else: this is the only shape Ubuntu
    // ships and guessing at another one has no value here.
    if (u8[0] !== 0x7f || u8[1] !== 0x45 || u8[2] !== 0x4c || u8[3] !== 0x46 ||
        u8[4] !== 2 || u8[5] !== 1)
        return null;
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const u16 = o => dv.getUint16(o, true);
    const u32 = o => dv.getUint32(o, true);
    const u64 = o => Number(dv.getBigUint64(o, true));
    const inside = (o, n) => o >= 0 && n >= 0 && o + n <= u8.length;
    const phoff = u64(0x20), phentsize = u16(0x36), phnum = u16(0x38);
    if (!phentsize || !inside(phoff, phentsize * phnum))
        return null;
    for (let i = 0; i < phnum; i++) {
        const ph = phoff + i * phentsize;
        if (u32(ph) !== PT_NOTE)
            continue;
        let off = u64(ph + 8), end = off + u64(ph + 32);
        if (!inside(off, end - off))
            continue;
        while (off + 12 <= end) {
            const namesz = u32(off), descsz = u32(off + 4), type = u32(off + 8);
            const name = off + 12, desc = name + ((namesz + 3) & ~3);
            if (!inside(desc, descsz))
                break;
            if (type === NT_GNU_BUILD_ID && namesz === 4 &&
                u8[name] === 0x47 && u8[name + 1] === 0x4e && u8[name + 2] === 0x55) {
                let hex = '';
                for (let j = 0; j < descsz; j++)
                    hex += u8[desc + j].toString(16).padStart(2, '0');
                return hex;
            }
            off = desc + ((descsz + 3) & ~3);
        }
    }
    return null;
}

function libmutterIdentity(mapping) {
    if (!mapping)
        return null;
    const ident = {path: mapping.path, build_id: null, replaced: !!mapping.deleted};
    try {
        const info = Gio.File.new_for_path(mapping.path).query_info(
            'unix::inode', Gio.FileQueryInfoFlags.NONE, null);
        const onDisk = Number(info.get_attribute_uint64('unix::inode'));
        // dpkg replaces a library by writing a new file over the name, so the
        // running session keeps the old inode mapped while the path resolves to
        // a new one.  Reading the ELF at that path would then describe a
        // library this session is not running, so it is not read at all.
        if (onDisk && mapping.inode && onDisk !== mapping.inode)
            ident.replaced = true;
    } catch (e) {
        ident.replaced = true;          // the path is gone: it was replaced
    }
    if (!ident.replaced)
        ident.build_id = elfBuildId(mapping.path);
    return ident;
}

// -- the reader --------------------------------------------------------------
//
// Everything below reads through `lib`, the type description the extension
// ships, in which no pointer is a pointer.

class Reader {
    constructor(lib, maps) {
        this.lib = lib;
        this.maps = maps;
        this.refusals = [];
    }

    _at(what, p, n) {
        if (this.maps.holds(p, n))
            return true;
        this.refusals.push(`${what}: 0x${(p || 0).toString(16)}+${n} is not in a readable mapping`);
        return false;
    }

    // A bounded copy of n bytes at p, read as the named record, or null.
    copy(kind, what, p, n) {
        if (!this._at(what, p, n))
            return null;
        return this.lib[kind](p, n);
    }

    string(what, p) {
        if (!this._at(what, p, 1))
            return null;
        return this.lib.strn(p, MAX_CONNECTOR);
    }

    // The logical monitors of a MetaMonitorsConfig, read through the offsets
    // the shipped description believes, or null with a reason in refusals.
    logicalMonitors(cfgAddr, instanceSize) {
        const cfg = this.copy('dup_cfg', 'config', cfgAddr, instanceSize);
        if (cfg === null)
            return null;
        this.tail = {
            layout_mode: cfg.layout_mode,
            switch_config: cfg.switch_config,
            list: cfg.logical_monitor_configs,
        };
        const out = [];
        let node = cfg.logical_monitor_configs;
        for (let i = 0; node && i < MAX_MONITORS; i++) {
            const n = this.copy('dup_node', `node[${i}]`, node, 24);
            if (n === null)
                return null;
            const d = this.copy('dup_lmc', `logical[${i}]`, n.data, 40);
            if (d === null)
                return null;
            const connectors = [];
            let mnode = d.monitor_configs;
            for (let j = 0; mnode && j < MAX_MONITORS; j++) {
                const mn = this.copy('dup_node', `member[${i}][${j}]`, mnode, 24);
                if (mn === null)
                    return null;
                const mc = this.copy('dup_mc', `monitor[${i}][${j}]`, mn.data, 24);
                if (mc === null)
                    return null;
                const ms = this.copy('dup_ms', `spec[${i}][${j}]`, mc.monitor_spec, 32);
                if (ms === null)
                    return null;
                const name = this.string(`connector[${i}][${j}]`, ms.connector);
                if (name === null)
                    return null;
                connectors.push(name);
                mnode = mn.next;
            }
            out.push({
                addr: n.data,
                x: d.x, y: d.y, w: d.width, h: d.height,
                scale: Math.round(d.scale * 1000) / 1000,
                transform: d.transform,
                primary: !!d.is_primary,
                connectors: connectors.slice().sort(),
            });
            node = n.next;
        }
        return out.sort(cmpRegion);
    }
}

// -- ~/.config/monitors.xml --------------------------------------------------

function savedConfigDigest() {
    const path = GLib.build_filenamev([GLib.get_user_config_dir(), 'monitors.xml']);
    if (!GLib.file_test(path, GLib.FileTest.EXISTS))
        return {path, state: 'absent'};
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        if (!ok)
            return {path, state: 'unreadable'};
        return {
            path,
            state: 'present',
            sha256: GLib.compute_checksum_for_data(GLib.ChecksumType.SHA256, bytes),
            size: bytes.length,
        };
    } catch (e) {
        return {path, state: 'unreadable'};
    }
}

const sameDigest = (a, b) =>
    a.state === b.state && a.sha256 === b.sha256 && a.size === b.size;

// -- the table ---------------------------------------------------------------
//
// generations.json ships beside this file and is the only place a
// version-specific fact lives: the soname to match in /proc/self/maps, the Meta
// typelib version, the namespace of the description to load, the struct size to
// demand, and where each was measured.  It is read HERE, at call time, and
// never at enable: rule 1 at the top of this file is that a login touches
// nothing, and a login that has to parse a file is a login that can fail.
//
// A missing or malformed table is a refusal like any other.  It cannot be
// forced -- there is nothing to force with.

function loadGenerations(extensionPath) {
    const path = GLib.build_filenamev([extensionPath, 'generations.json']);
    let text;
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        if (!ok)
            refuse('table', `cannot read ${path}`);
        text = new TextDecoder().decode(bytes);
    } catch (e) {
        refuse('table', `cannot read ${path}: ${e}`);
    }
    let table;
    try {
        table = JSON.parse(text);
    } catch (e) {
        refuse('table', `${path} is not JSON: ${e}`);
    }
    const list = generations(table);
    if (!list.length)
        refuse('table', `${path} names no generations at all`);
    for (const g of list) {
        for (const f of TABLE_FIELDS) {
            if (g[f] === undefined || g[f] === null)
                refuse('table', `${path}: the GNOME ${g.shell_major} record has no ${f}`);
        }
    }
    return table;
}

// GObject.type_query(MetaMonitorsConfig).instance_size, or null.
//
// Public API all the way down -- a type name out of a header and the GType
// registry -- so it is safe to ask on a GNOME nobody has measured, which is
// exactly when a maintainer needs the number.  It is asked once, in version(),
// and the typelib check below compares the same value it reports.
function metaMonitorsConfigSize() {
    try {
        const gtype = GObject.type_from_name('MetaMonitorsConfig');
        if (!gtype)
            return null;
        const n = GObject.type_query(gtype).instance_size;
        return Number.isInteger(n) && n > 0 ? n : null;
    } catch (e) {
        return null;
    }
}

// -- the guarded session -----------------------------------------------------

class Guarded {
    constructor(extensionPath) {
        this.path = extensionPath;
        this.checks = [];
    }

    _pass(name, detail) {
        this.checks.push({name, ok: true, detail: `${detail}`});
    }

    // 1. Is this a build whose private layout has been measured?
    //
    // `force` is `{shell_major: N}` and nothing else, and it is the ONE thing
    // in this file that can be skipped past: the requirement that this GNOME be
    // in the table.  When it is used, the description to read through is chosen
    // by the size the compositor's own GType registry reports, which is a
    // selection and not a relaxation -- the size still has to be exactly a
    // shipped description's, the sentinel still has to round-trip at that
    // description's offsets, and every other check below is untouched.
    version(force) {
        const shell = `${Config.PACKAGE_VERSION}`;
        const table = this.table = loadGenerations(this.path);
        const maps = new Maps();
        const loaded = maps.mutters();
        const repo = GIRepository.Repository.dup_default
            ? GIRepository.Repository.dup_default() : GIRepository.Repository.get_default();
        const meta = repo.get_version ? repo.get_version('Meta') : null;
        // Everything a maintainer needs to add this build, measured before
        // anything can refuse and carried out with the refusal if one happens.
        // All of it is public: a version string, the file names in
        // /proc/self/maps, GIRepository, and GObject.type_query() on a type
        // whose name is in a header.  Nothing private is read to fill it in.
        const found = this.found = {
            shell,
            shell_major: shellMajor(shell),
            sonames: loaded,
            meta_typelib: meta === null ? null : `${meta}`,
            instance_size: metaMonitorsConfigSize(),
            known: describeTable(table),
        };
        let generation = generationFor(table, shell);
        let forced = null;
        if (!generation) {
            const no = forceGate(force, shell);
            if (no) {
                refuse('shell-version',
                       `GNOME Shell ${shell}: this extension knows the private ` +
                       `layout of GNOME ${knownMajors(table).join(' and ')} only` +
                       (no === 'not forced' ? '' : ` (${no})`),
                       found);
            }
            const picked = selectByStructSize(table, found.instance_size);
            if (picked.refusal) {
                // Not a forceable refusal and it never becomes one: there is no
                // description of this build to try, and forcing does not write
                // one.
                refuse('struct-size', picked.refusal, found);
            }
            generation = picked.generation;
            const also = (picked.sameShape || []).length
                ? `, and the same bytes as ${picked.sameShape.join(', ')}` : '';
            forced = {shell_major: found.shell_major,
                      using: generation.namespace,
                      because: `MetaMonitorsConfig is ${found.instance_size} bytes ` +
                               `here, which is the size ${generation.namespace} ` +
                               `describes (measured on GNOME ${generation.shell_major}` +
                               `${also})`};
        }
        if (loaded.length !== 1) {
            refuse('libmutter',
                   `exactly one libmutter has to be mapped into gnome-shell; ` +
                   `this process has [${loaded.join(', ')}]`, found);
        }
        // Unforced, the mapped library has to be the one the table says this
        // shell carries.  Forced, there is no such claim to make -- that is the
        // claim being skipped -- so what is left is that the Meta typelib and
        // the mapped library agree with each other, which is mutter's own build
        // convention and not anything this project measured.
        if (!forced && loaded[0] !== generation.soname) {
            refuse('libmutter',
                   `GNOME Shell ${shell} should carry ${generation.soname}, ` +
                   `this process has [${loaded.join(', ')}]`, found);
        }
        const wantMeta = forced ? sonameToken(loaded[0]) : generation.meta_typelib;
        if (meta !== null && wantMeta !== null && `${meta}` !== `${wantMeta}`) {
            refuse('meta-typelib',
                   `the Meta typelib says ${meta}, ${loaded[0]} says ${wantMeta}`,
                   found);
        }
        this.shell = shell;
        this.generation = generation;
        this.libmutterLabel = sonameToken(loaded[0]) || generation.libmutter;
        this.forced = forced;
        this.maps = maps;
        this.repo = repo;
        // Which build, not only which generation.  `ShellVersion` cannot see a
        // libmutter replaced under it -- noble shipped mutter 46.2 under shell
        // 46.0 for most of its life -- so the answer carries the identity of the
        // library the checks are about to run against, and the caller records
        // its agreement against that rather than against a version string.
        // Nothing here can refuse: a null is a null.
        try {
            this.libmutter = libmutterIdentity(
                maps.mutterMappings().find(m => m.soname === loaded[0]));
        } catch (e) {
            this.libmutter = null;
        }
        const build = this.libmutter && this.libmutter.build_id;
        this._pass('shell-version',
                   `GNOME Shell ${shell}, ${loaded[0]}` +
                   (build ? ` (build ${build.slice(0, 12)})` : '') +
                   (forced ? ` -- FORCED: ${forced.because}` : ''));
        return this;
    }

    // 2. Load our own type description, and refuse unless the size it declares
    //    for MetaMonitorsConfig is the size the GType registry reports.  Read
    //    back from our own typelib, so there is no constant to go stale.
    typelib() {
        const ns = this.generation.namespace;
        const loadedSonames = this.maps.mutters();
        const dir = GLib.build_filenamev([this.path, 'typelib']);
        if (!GLib.file_test(GLib.build_filenamev([dir, `${ns}-1.0.typelib`]),
                            GLib.FileTest.EXISTS))
            refuse('typelib', `${ns}-1.0.typelib is not installed in ${dir}`);
        try {
            if (this.repo.prepend_search_path)
                this.repo.prepend_search_path(dir);
            else
                GIRepository.Repository.prepend_search_path(dir);
        } catch (e) {
            refuse('typelib', `cannot prepend ${dir}: ${e}`);
        }
        let lib;
        try {
            lib = globalThis.imports.gi[ns];
        } catch (e) {
            refuse('typelib', `${ns} did not load: ${e}`);
        }
        // A description that names a shared library GIRepository cannot open is
        // FATAL, not a refusal: gjs builds the call with no ffi arguments and
        // then aborts the process on the first call
        // (`assertion failed (ffi_arg_pos == ffi_argc)`), and on Wayland that
        // process is the session.  Measured on GNOME 51, where a forced run
        // picked the 80-byte W11Overlap18 -- correct about the layout -- and its
        // `shared-library="libmutter-18.so.0"` was not there to open, so the
        // dryrun that writes nothing still ended the session.
        //
        // The descriptions this project ships have named no library since 0.4
        // (gen-gir.py says why): the symbols resolve out of gnome-shell's own
        // global scope, where libmutter already is.  This check is for
        // everything else -- an older typelib left behind by a previous
        // install, a hand-built one, a distribution's -- and it is a *static*
        // read of the loaded namespace, before any call is made through it.
        for (const named of sharedLibraries(this.repo, ns)) {
            if (loadedSonames.indexOf(named) < 0) {
                refuse('shared-library',
                       `${ns} names the shared library ${named}, which is not ` +
                       `mapped into gnome-shell ([${loadedSonames.join(', ')}] ` +
                       'are).  Calling through a description whose library ' +
                       'cannot be opened aborts gjs, and on Wayland that is the ' +
                       'session -- so this refuses instead.  A description ' +
                       'generated by gnome/overlap-typelib/gen-gir.py names no ' +
                       'library at all; reinstall with gnome/install-overlap.sh');
            }
        }
        // Touching a declared function whose symbol is missing throws a
        // catchable GLib.Error rather than crashing, so every symbol can be
        // probed.  A libmutter that dropped one of these is not one to write to.
        for (const fn of ['dup_cfg', 'dup_node', 'dup_lmc', 'dup_mc', 'dup_ms',
                          'strn', 'addr', 'unref_addr', 'type_name',
                          'get_config_manager', 'get_current', 'create_linear',
                          'get_switch_config', 'set_switch_config',
                          'verify', 'apply', 'wr']) {
            if (typeof lib[fn] !== 'function')
                refuse('symbols', `${ns}.${fn} is not callable`);
        }
        const declared = structSize(this.repo, ns, 'ConfigN');
        if (!declared)
            refuse('struct-size', `cannot read the size of ${ns}.ConfigN back from its own typelib`);
        // Three numbers, arrived at three ways, and all three have to agree:
        // the compiled description's own record size, the size the table
        // records as measured, and the size this build's GType registry
        // reports.  The middle one is new with the table, and it is not
        // ceremony -- a forced run picks its description BY that number, so a
        // table that disagreed with the typelib beside it would be a wrong
        // description chosen on purpose.
        if (declared !== this.generation.struct_size) {
            refuse('struct-size',
                   `${ns} describes a ${declared}-byte struct, generations.json ` +
                   `records ${this.generation.struct_size} for GNOME ` +
                   `${this.generation.shell_major}: the table and the description ` +
                   'beside it disagree, and neither can be trusted until they do not');
        }
        const actual = this.found.instance_size;
        if (!actual)
            refuse('struct-size', 'MetaMonitorsConfig is not a registered GType', this.found);
        if (declared !== actual) {
            refuse('struct-size',
                   `this build's MetaMonitorsConfig is ${actual} bytes, the ` +
                   `description shipped for ${this.generation.soname} is ${declared}`,
                   this.found);
        }
        // The size is right, so now -- and not before -- one call, to prove the
        // description can reach the process at all.  g_strndup(NULL, 0) is NULL
        // on every glib and touches nothing.  It goes here rather than above
        // because a struct-size refusal is documented as happening having
        // called nothing and read nothing, and because a description that is
        // the wrong size is exactly the one whose calls should not be made.
        try {
            lib.strn(0, 0);
        } catch (e) {
            refuse('symbols',
                   `${ns} loaded but calling through it failed: ${e}`);
        }
        this.lib = lib;
        this.instanceSize = actual;
        this._pass('typelib', `${ns}, MetaMonitorsConfig ${actual} bytes as declared`);
        return this;
    }

    // 3. A value written through Mutter's own exported setter has to appear at
    //    the offset we believe.  Done on a throwaway config built for the
    //    purpose, never on the one the session is running.
    sentinel() {
        const lib = this.lib;
        const mm = global.backend.get_monitor_manager();
        const cm = lib.get_config_manager(mm);
        if (!cm)
            refuse('sentinel', 'meta_monitor_manager_get_config_manager returned nothing');
        const throwaway = lib.create_linear(cm);
        if (!throwaway)
            refuse('sentinel', 'meta_monitor_config_manager_create_linear returned nothing');
        const name = lib.type_name(throwaway);
        if (name !== 'MetaMonitorsConfig')
            refuse('sentinel', `create_linear returned a ${name}`);
        lib.set_switch_config(throwaway, SENTINEL);
        if (lib.get_switch_config(throwaway) !== SENTINEL)
            refuse('sentinel', 'Mutter did not read back its own switch_config');
        const addr = lib.addr(throwaway);
        lib.unref_addr(addr);
        const reader = new Reader(lib, this.maps);
        const copy = reader.copy('dup_cfg', 'sentinel config', addr, this.instanceSize);
        if (copy === null)
            refuse('sentinel', reader.refusals.join('; '));
        if (copy.switch_config !== SENTINEL) {
            refuse('sentinel',
                   `switch_config reads ${copy.switch_config} at the offset this ` +
                   `description believes, not ${SENTINEL}: the tail of ` +
                   'MetaMonitorsConfig is not where it was measured');
        }
        this.mm = mm;
        this.cm = cm;
        this._pass('sentinel', `switch_config round-tripped at the declared offset`);
        return this;
    }

    // 3b. Refuse while anything holds a modal grab, because "Keep changes?" is
    //     one of the things that does.
    //
    // This is the guard that stands between this extension and the only lasting
    // damage it can do: Mutter saves the *current* configuration when a pending
    // display change is confirmed, and a saved overlap poisons
    // ~/.config/monitors.xml for ever (rules.js says how, and what was measured
    // when this check could not fire).  wxrandr cannot arm such a change
    // together with this flag -- --persistent is refused -- but the Settings
    // panel in another window can, and did.
    //
    // Main.modalCount is a number on the window that matters and a refusal
    // whenever it is not readable; the decision itself is in rules.js, where
    // plain node tests it.  Reading a field on the shell's own main module
    // cannot crash anything.
    noPendingDialog() {
        let modal;
        try {
            modal = Main.modalCount;
        } catch (e) {
            modal = null;
        }
        const bad = modalVerdict(modal);
        if (bad)
            refuse('pending-dialog', bad);
        this.modalCount = modal;
        this._pass('pending-dialog',
                   'nothing holds a modal grab, so GNOME is not asking "Keep changes?"');
        return this;
    }

    // 4. Read the live configuration, bounded, and refuse unless it is exactly
    //    what Mutter says publicly.
    read(layoutMode, expect) {
        const lib = this.lib;
        const cfg = lib.get_current(this.cm);
        if (!cfg)
            refuse('current', 'this session has no current monitors configuration');
        const name = lib.type_name(cfg);
        if (name !== 'MetaMonitorsConfig')
            refuse('current', `the current configuration is a ${name}`);
        const addr = lib.addr(cfg);
        lib.unref_addr(addr);
        const reader = new Reader(lib, this.maps);
        const priv = reader.logicalMonitors(addr, this.instanceSize);
        if (priv === null)
            refuse('bounded-read', reader.refusals.join('; '));
        if (layoutMode && reader.tail.layout_mode !== layoutMode) {
            refuse('layout-mode',
                   `layout_mode reads ${reader.tail.layout_mode} at the offset this ` +
                   `description believes; DisplayConfig says ${layoutMode}`);
        }
        const pub = this.publicView(expect);
        const diffs = compare(priv, pub);
        if (diffs.length)
            refuse('public-view', diffs.join('; '));
        this.cfg = cfg;
        this.cfgAddr = addr;
        this.reader = reader;
        this.private = priv;
        this.public = pub;
        this._pass('bounded-read', `${priv.length} logical monitors, every address range-checked`);
        this._pass('public-view', `identical to Mutter's public view (${pub.source})`);
        return this;
    }

    // Mutter's public answer about the same monitors.  Geometry, scale and
    // primary come from global.display, which is public API on every version.
    // Connector names come from MetaMonitorManager where it enumerates them
    // (GNOME 50), and otherwise from resolving the names the caller read out
    // of DisplayConfig -- Mutter answering, not the caller.
    publicView(expect) {
        const mm = this.mm;
        const byIndex = {};
        let source = 'global.display';
        if (typeof mm.get_monitors === 'function' &&
            typeof mm.get_monitor_for_connector === 'function') {
            for (const m of mm.get_monitors()) {
                const c = m.get_connector();
                const i = mm.get_monitor_for_connector(c);
                (byIndex[i] = byIndex[i] || []).push(c);
            }
            source = 'global.display + MetaMonitorManager.get_monitors';
        } else if (typeof mm.get_monitor_for_connector === 'function') {
            for (const group of expect || []) {
                for (const c of group.connectors || []) {
                    const i = mm.get_monitor_for_connector(c);
                    if (!(i >= 0))
                        refuse('connectors', `Mutter does not know a connector named ${c}`);
                    (byIndex[i] = byIndex[i] || []).push(c);
                }
            }
            source = 'global.display + get_monitor_for_connector on the requested names';
        } else if ((expect || []).length) {
            // GNOME 46 exposes no way to name a monitor from JS at all (and a
            // synchronous DisplayConfig call from inside gnome-shell deadlocks,
            // because gnome-shell is what serves it).  Fall back to attaching
            // the names the caller read out of DisplayConfig to the geometry
            // global.display reports, and say in the result that that is what
            // happened: the geometry, scale and primary half of the comparison
            // is still Mutter's own answer, independent of the caller.
            const byXY = {};
            for (const group of expect)
                byXY[`${group.x},${group.y}`] = (group.connectors || []).slice().sort();
            this._byXY = byXY;
            source = 'global.display + connector names relayed from DisplayConfig by the caller';
        } else {
            refuse('connectors', 'this Shell exposes no way to name a monitor from JS');
        }
        const primary = global.display.get_primary_monitor();
        const out = [];
        for (let i = 0; i < global.display.get_n_monitors(); i++) {
            const g = global.display.get_monitor_geometry(i);
            out.push({
                x: g.x, y: g.y, w: g.width, h: g.height,
                scale: Math.round(global.display.get_monitor_scale(i) * 1000) / 1000,
                primary: i === primary,
                connectors: (byIndex[i] ||
                             (this._byXY ? this._byXY[`${g.x},${g.y}`] : null) ||
                             []).slice().sort(),
            });
        }
        out.sort(cmpRegion);
        out.source = source;
        return out;
    }

    monitors() {
        return this.private.map(m => ({
            connectors: m.connectors, x: m.x, y: m.y, w: m.w, h: m.h,
            scale: m.scale, transform: m.transform, primary: m.primary,
        }));
    }

    result(extra) {
        return Object.assign({
            ok: true,
            version: VERSION,
            shell: this.shell,
            libmutter: this.libmutterLabel,
            // null on an ordinary run.  Present, and named, when the version
            // gate was forced past: what was skipped, which description was
            // picked and why.  wxrandr refuses to record an agreement for a
            // reply that carries this.
            forced: this.forced || null,
            found: this.found || null,
            // The size the struct-size check just read out of this build's GType
            // registry.  It is in the answer because the caller records an
            // agreement against it (hacks/display/gnome_overlap.py): what was agreed to
            // has to be what was measured, not a number typed anywhere else.
            instance_size: this.instanceSize,
            // The build of libmutter every check above ran against, so that an
            // agreement can name it: `ShellVersion` cannot see this change and
            // Ubuntu makes it inside a release (measured: 46.0 -> 46.2 under
            // one unchanged shell version).  Null when it could not be read,
            // which is not an error and never a refusal.
            libmutter_build: (this.libmutter && this.libmutter.build_id) || null,
            libmutter_path: (this.libmutter && this.libmutter.path) || null,
            modal_count: this.modalCount,
            // Advisory, not a verdict.  Everything that decides anything is a
            // check above; this is news the caller should pass on.
            notes: [libmutterNote(this.libmutter)].filter(Boolean),
            checks: this.checks,
            monitors: this.monitors(),
        }, extra || {});
    }
}

function sharedLibraries(repo, ns) {
    // Every shared library the loaded namespace names, as file names.  A
    // description generated here names none; one that names something is one
    // GIRepository will dlopen, and a dlopen that fails is not an error anybody
    // can catch -- gjs aborts on the first call through it.  So this is read
    // *before* any call, and its answer decides whether there is one.
    //
    // The API moved with GIRepository: `get_shared_library` (one comma-joined
    // string) through glib 2.84, `get_shared_libraries` (a string array) after
    // it.  Ask both shapes and both spellings; an answer nothing gives is an
    // empty list, which is the safe reading -- it can only make this check
    // pass, and the checks that follow are the ones that write nothing on their
    // own account.
    const tries = [
        () => repo.get_shared_libraries(ns),
        () => repo.get_shared_library(ns),
        () => GIRepository.Repository.get_shared_libraries(repo, ns),
        () => GIRepository.Repository.get_shared_library(repo, ns),
    ];
    for (const get of tries) {
        let v;
        try {
            v = get();
        } catch (e) {
            continue;
        }
        if (v === null || v === undefined)
            continue;
        const list = Array.isArray(v) ? v : `${v}`.split(',');
        return list.map(x => `${x}`.trim()).filter(Boolean);
    }
    return [];
}


function structSize(repo, ns, record) {
    // GIRepository's own API moved between glib 2.80 and 2.88; ask every shape
    // rather than pick one, and refuse if none of them answers.  The size has
    // to come out of the shipped typelib itself: a constant written here could
    // go stale against the description beside it, which is the whole failure
    // this check exists to catch.
    let info = null;
    for (const find of [() => repo.find_by_name(ns, record),
                        () => GIRepository.Repository.find_by_name(repo, ns, record)]) {
        try {
            info = find();
        } catch (e) {
            info = null;
        }
        if (info)
            break;
    }
    if (!info)
        return 0;
    for (const get of [() => info.get_size(),
                       () => GIRepository.struct_info_get_size(info),
                       () => GIRepository.StructInfo.get_size(info)]) {
        try {
            const n = get();
            if (Number.isInteger(n) && n > 0)
                return n;
        } catch (e) {
            // try the next shape
        }
    }
    return 0;
}

// -- the extension -----------------------------------------------------------

export default class W11Overlap extends Extension {
    // Nothing here reads a monitor, loads a typelib or touches libmutter.  If
    // this method ever grows any of that, an enabled extension can kill the
    // session at login, which is the one failure this design exists to prevent.
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._nameId = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, null, null);
        console.log('w11-overlap: enabled (idle; it acts only when called)');
    }

    disable() {
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        if (this._dbus) {
            this._dbus.unexport();
            this._dbus = null;
        }
    }

    get Version() {
        return VERSION;
    }

    // Run every check, read the live configuration, write nothing.
    Probe(request) {
        return this._answer(request, false);
    }

    // Run every check, then move the logical monitors the caller asked for.
    ApplyOverlap(request) {
        return this._answer(request, true);
    }

    _answer(request, write) {
        const checks = [];
        let g = null;
        try {
            const req = JSON.parse(request || '{}');
            // The only thing a caller can ask to skip, and it has to be asked
            // for by name on every single call: nothing is remembered here, and
            // an absent `force` is a refusal on an unmeasured build.
            const force = req.force || null;
            g = new Guarded(this.path);
            g.version(force).typelib().sentinel().noPendingDialog()
                .read(req.layout_mode, req.expect);
            checks.push(...g.checks);
            if (!write)
                return JSON.stringify(g.result({wrote: false}));
            return JSON.stringify(this._apply(g, req));
        } catch (e) {
            const refused = e instanceof Refused;
            if (!refused)
                console.error(`w11-overlap: ${e}\n${e.stack || ''}`);
            return JSON.stringify({
                ok: false,
                version: VERSION,
                check: refused ? e.check : 'internal',
                reason: `${e.message || e}`,
                // Whether this refusal is one forcing could get past, decided
                // here rather than by the caller reading the wording: today
                // that is `shell-version` and nothing else.  Everything else is
                // a refusal because something is wrong, and forcing does not
                // make a wrong thing right.
                forceable: refused ? isForceable(e.check) : false,
                // The measurements a maintainer needs to add this build, when
                // the refusal happened somewhere that had them.
                found: (refused && e.found) || (g && g.found) || null,
                checks,
            });
        }
    }

    _apply(g, req) {
        const want = req.want || [];
        const expect = req.expect || [];
        const live = g.private;

        // The caller's picture of the layout has to be this one, exactly.
        const liveKeys = live.map(key).sort().join(' ');
        if (expect.map(key).sort().join(' ') !== liveKeys)
            refuse('request',
                   `this session has monitors [${liveKeys}], the request `
                   + `names [${expect.map(key).sort().join(' ')}]`);
        if (want.map(key).sort().join(' ') !== liveKeys)
            refuse('request', 'the requested layout does not name the same monitors');
        for (const e of expect) {
            const m = live.find(l => key(l) === key(e));
            if (m.x !== e.x || m.y !== e.y) {
                refuse('request',
                       `${key(e)} is at +${m.x}+${m.y}, the request was built when it ` +
                       `was at +${e.x}+${e.y}: re-read the layout and try again`);
            }
        }

        // What is being asked for must be a layout Mutter refuses.  Anything
        // else belongs on the ordinary DisplayConfig path, which validates,
        // reverts on a timer and can be undone by a dialog; there is no reason
        // to write into the compositor's heap for a layout it would accept.
        const targets = want.map(w => {
            const m = live.find(l => key(l) === key(w));
            return {x: w.x | 0, y: w.y | 0, w: m.w, h: m.h};
        });
        const fault = mutterFault(targets);
        if (!fault) {
            refuse('not-an-overlap',
                   'Mutter accepts this layout: apply it the ordinary way, ' +
                   'without this extension');
        }

        const before = savedConfigDigest();
        const undo = [];
        let out;
        try {
            for (const w of want) {
                const m = live.find(l => key(l) === key(w));
                if (m.x === (w.x | 0) && m.y === (w.y | 0))
                    continue;
                // The address came out of a read the range check already
                // cleared; check it again anyway, immediately before writing.
                if (!g.maps.holds(m.addr, 8))
                    refuse('write', `0x${m.addr.toString(16)} is no longer readable`);
                undo.push({addr: m.addr, x: m.x, y: m.y});
                // le32() is four bytes and the typelib takes the length from
                // the array itself, so there is no count to pass (and gjs
                // warns about one that is passed).
                g.lib.wr(m.addr, le32(w.x | 0));             // MetaLogicalMonitorConfig.x, +0
                g.lib.wr(m.addr + 4, le32(w.y | 0));         // .y, +4
            }
            if (!undo.length)
                refuse('request', 'that is the layout this session already has');

            // Read it all back the same bounded way: the write must have
            // changed the two words it was aimed at and nothing else.
            const after = new Reader(g.lib, g.maps).logicalMonitors(g.cfgAddr, g.instanceSize);
            if (after === null)
                refuse('read-back', 'the configuration no longer reads back');
            const wantRects = want.map(w => {
                const m = live.find(l => key(l) === key(w));
                return {x: w.x | 0, y: w.y | 0, w: m.w, h: m.h, scale: m.scale,
                        transform: m.transform, primary: m.primary,
                        connectors: m.connectors};
            }).sort(cmpRegion);
            const moved = drift(after, wantRects);
            if (moved.length)
                refuse('read-back', moved.join('; '));

            // Positive control: Mutter's own validator has to refuse what we
            // just built.  If it accepts it, the write did not land on the
            // field the validator reads and nothing here means what it says.
            let verdict;
            try {
                const okay = g.lib.verify(g.cfg, g.mm);
                verdict = okay ? 'accepted' : 'refused without a message';
            } catch (e) {
                verdict = `refused: ${e.message}`;
            }
            if (verdict === 'accepted') {
                refuse('positive-control',
                       'Mutter validated the mutated configuration, so the write did ' +
                       'not land where its validator reads: nothing was applied');
            }

            let applied;
            try {
                applied = !!g.lib.apply(g.mm, g.cfg, METHOD_TEMPORARY);
            } catch (e) {
                refuse('apply', `${e.message}`);
            }
            if (!applied)
                refuse('apply', 'meta_monitor_manager_apply_monitors_config returned false');

            out = g.result({
                wrote: true,
                applied: true,
                fault,
                verify: verdict,
                wrote_words: undo.length * 2,
                monitors: after.map(m => ({
                    connectors: m.connectors, x: m.x, y: m.y, w: m.w, h: m.h,
                    scale: m.scale, transform: m.transform, primary: m.primary,
                })),
                public: g.publicView(want).map(m => ({
                    connectors: m.connectors, x: m.x, y: m.y, w: m.w, h: m.h,
                })),
            });
        } catch (e) {
            // Put the words back exactly as they were.  Nothing has been
            // applied on this path, so the session is untouched either way.
            for (const u of undo.reverse()) {
                if (g.maps.holds(u.addr, 8)) {
                    g.lib.wr(u.addr, le32(u.x));
                    g.lib.wr(u.addr + 4, le32(u.y));
                }
            }
            throw e;
        }
        const after = savedConfigDigest();
        out.saved_config = {
            path: before.path,
            before: before.state === 'present' ? before.sha256 : before.state,
            after: after.state === 'present' ? after.sha256 : after.state,
            unchanged: sameDigest(before, after),
        };
        if (!out.saved_config.unchanged)
            out.ok = false;
        return out;
    }
}
