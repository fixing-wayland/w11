// gi://Gio: the D-Bus export both extensions hang themselves on, plus the
// two file/settings calls the bridge makes and the File.read() the overlap
// extension's elfBuildId() needs.
//
// `wrapJSObject(xml, obj)` keeps the object it wrapped, so a test drives the
// extension the way the bus does -- `Gio.exported().object.ListWindowsAsync(
// params, invocation)` -- and reads what came back out of the invocation
// double rather than off a real session bus.  Signals go into `signals`
// instead of onto a connection nobody started.

import {record, tag} from './harness.mjs';

const exported = [];
const owned = [];
const files = new Map();
const elves = new Map();
let schemas = new Set();

class ExportedObject {
    constructor(xml, obj) {
        tag(this, 'DBusExportedObject');
        this.xml = xml;
        this.object = obj;
        this.signals = [];
        this.path = null;
        this.exported = false;
    }

    export(conn, path) {
        record('dbus.export', [path]);
        this.path = path;
        this.exported = true;
    }

    unexport() {
        record('dbus.unexport', []);
        this.exported = false;
    }

    flush() {
        record('dbus.flush', []);
    }

    emit_signal(name, variant) {
        record('dbus.emit_signal', [name, variant && variant.signature]);
        this.signals.push([name, variant ? variant.value : null]);
    }
}

/**
 * The invocation double `<Name>Async(params, invocation)` answers to.  What
 * the method returned is in `.reply`, and a D-Bus error in `.error`.
 */
export function makeInvocation() {
    const inv = tag({}, 'Invocation');
    inv.reply = undefined;
    inv.error = null;
    inv.return_value = v => {
        record('invocation.return_value', []);
        inv.reply = v;
    };
    inv.return_dbus_error = (name, msg) => {
        record('invocation.return_dbus_error', [name]);
        inv.error = [name, msg];
    };
    inv.return_error_literal = (domain, code, msg) => {
        record('invocation.return_error_literal', [code]);
        inv.error = [`${domain}.${code}`, msg];
    };
    inv.get_sender = () => ':1.42';
    return inv;
}

const Gio = {
    BusNameOwnerFlags: {NONE: 0, ALLOW_REPLACEMENT: 1, REPLACE: 2, DO_NOT_QUEUE: 4},
    BusNameWatcherFlags: {NONE: 0, AUTO_START: 1},
    FileQueryInfoFlags: {NONE: 0, NOFOLLOW_SYMLINKS: 1},

    DBus: {session: tag({}, 'DBusConnection(session)'),
           system: tag({}, 'DBusConnection(system)')},

    DBusExportedObject: {
        wrapJSObject(xml, obj) {
            record('DBusExportedObject.wrapJSObject', []);
            const e = new ExportedObject(xml, obj);
            exported.push(e);
            return e;
        },
    },

    bus_own_name_on_connection(conn, name, flags, acquired, lost) {
        const id = owned.length + 1;
        record('Gio.bus_own_name_on_connection', [name, flags], id);
        owned.push({name, flags, acquired, lost, id});
        return id;
    },

    bus_unown_name(id) {
        record('Gio.bus_unown_name', [id]);
    },

    bus_watch_name_on_connection(conn, name, flags, appeared, vanished) {
        return record('Gio.bus_watch_name_on_connection', [name], owned.length + 1000);
    },

    bus_unwatch_name(id) {
        record('Gio.bus_unwatch_name', [id]);
    },

    File: {
        new_for_path(path) {
            record('Gio.File.new_for_path', [path]);
            const f = tag({}, `File(${path})`);
            f.query_info = (attrs, flags, cancellable) => {
                record('file.query_info', [attrs]);
                if (!files.has(path))
                    throw new Error(`query_info: ${path}: No such file or directory`);
                const mtime = files.get(path);
                const st = tag({}, `FileInfo(${path})`);
                st.get_attribute_uint64 = a => record('info.get_attribute_uint64', [a], mtime);
                return st;
            };
            // g_file_read(): the whole of what elfBuildId() does with a file.
            // A path nobody planted throws, which is the branch that answers
            // null rather than refusing (extension.js:172-174).
            f.read = cancellable => {
                record('file.read', [path]);
                if (!elves.has(path))
                    throw new Error(`read: ${path}: No such file or directory`);
                const bytes = elves.get(path);
                const s = tag({}, `FileInputStream(${path})`);
                s.read_bytes = (n, c) => {
                    record('stream.read_bytes', [n]);
                    const b = tag({}, 'Bytes');
                    b.get_data = () => bytes.slice(0, n);
                    return b;
                };
                s.close = c => {
                    record('stream.close', []);
                    return null;
                };
                return s;
            };
            return f;
        },
    },

    Settings: class Settings {
        constructor(props) {
            record('new Gio.Settings', [props.schema_id]);
            this.schema_id = props.schema_id;
            this.values = Gio.settingsValues[props.schema_id] || {};
        }

        get_string(key) {
            return record('settings.get_string', [key], this.values[key] ?? '');
        }

        get_boolean(key) {
            return record('settings.get_boolean', [key], !!this.values[key]);
        }

        get_strv(key) {
            return record('settings.get_strv', [key], this.values[key] ?? []);
        }
    },

    SettingsSchemaSource: {
        get_default() {
            record('SettingsSchemaSource.get_default', []);
            const src = tag({}, 'SettingsSchemaSource');
            src.lookup = (id, recursive) =>
                record('schemaSource.lookup', [id], schemas.has(id) ? tag({}, `Schema(${id})`) : null);
            return src;
        },
    },

    // -- what a case turns -------------------------------------------------

    /** The most recent wrapJSObject() result, which is the one under test. */
    exported(n = -1) {
        return exported.at(n);
    },

    /** Every bus name asked for, as {name, flags, acquired, lost, id}. */
    names() {
        return owned.slice();
    },

    /**
     * Give `path` the one uint64 query_info() is ever asked for on that path:
     * the bridge asks time::modified, the overlap extension unix::inode; the
     * double answers the stored number for any attribute name.
     */
    setFileAttribute(path, value) {
        files.set(path, value);
    },

    /** The bridge's spelling of the same thing. */
    setFileMtime(path, mtime) {
        return Gio.setFileAttribute(path, mtime);
    },

    /** What `File.new_for_path(path).read()` hands back, as raw bytes. */
    setFileBytes(path, u8) {
        elves.set(path, u8 instanceof Uint8Array ? u8 : new Uint8Array(u8));
    },

    /**
     * ...and the same thing as the smallest ELF64 that carries a build id:
     * one PT_NOTE program header pointing at one NT_GNU_BUILD_ID note whose
     * descriptor is `buildIdHex`.
     *
     * elfBuildId() (extension.js:163-213) reads e_phoff/e_phentsize/e_phnum,
     * walks the program headers for PT_NOTE and then the notes inside it for
     * type 3 with the name `GNU`, so all of that has to be real; the rest of
     * the file does not exist, which is the point -- a test that planted a
     * copy of the host's libmutter would be a test of this machine.
     */
    setElf(path, buildIdHex) {
        const desc = Buffer.from(buildIdHex, 'hex');
        const note = Buffer.alloc(16 + desc.length);
        note.writeUInt32LE(4, 0);                   // namesz: 'GNU\0'
        note.writeUInt32LE(desc.length, 4);         // descsz
        note.writeUInt32LE(3, 8);                   // NT_GNU_BUILD_ID
        note.write('GNU\0', 12);
        desc.copy(note, 16);
        const NOFF = 0x1000;
        const buf = Buffer.alloc(NOFF + note.length);
        buf[0] = 0x7f; buf[1] = 0x45; buf[2] = 0x4c; buf[3] = 0x46;   // \x7fELF
        buf[4] = 2;                                 // ELFCLASS64
        buf[5] = 1;                                 // ELFDATA2LSB
        buf[6] = 1;                                 // EV_CURRENT
        buf.writeBigUInt64LE(64n, 0x20);            // e_phoff
        buf.writeUInt16LE(56, 0x36);                // e_phentsize
        buf.writeUInt16LE(1, 0x38);                 // e_phnum
        buf.writeUInt32LE(4, 64);                   // p_type = PT_NOTE
        buf.writeBigUInt64LE(BigInt(NOFF), 64 + 8);            // p_offset
        buf.writeBigUInt64LE(BigInt(note.length), 64 + 32);    // p_filesz
        note.copy(buf, NOFF);
        Gio.setFileBytes(path, new Uint8Array(buf));
    },

    settingsValues: {},

    setSchemas(...ids) {
        schemas = new Set(ids);
    },

    reset() {
        exported.length = 0;
        owned.length = 0;
        files.clear();
        elves.clear();
        schemas = new Set();
        Gio.settingsValues = {};
    },
};

export default Gio;
