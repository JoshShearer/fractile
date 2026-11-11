// Fractile compositor integration for GNOME Shell (45+).
//
// Window management on GNOME Wayland is only reachable from inside the
// compositor. This extension is a thin, stateless bridge: it connects to the
// Fractile daemon's private per-user socket ($XDG_RUNTIME_DIR/fractile/
// fractile.sock, mode 0600, same-uid checked by the daemon), announces itself
// with `bridge_attach`, then answers `bridge_call` requests and forwards
// window, output, workspace and shortcut events. It exports no D-Bus API,
// opens no network connection and stores nothing.
//
// SPDX-License-Identifier: GPL-3.0-or-later

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const PROTOCOL = 1;
const ADAPTER_API = 1;
const RETRY_SECONDS = 3;
const REJECTED_RETRY_SECONDS = 30;
const SETTLE_TIMEOUT_MS = 1500;
// A new window is offered once Mutter shows it, or after this long anyway.
const UNSHOWN_TIMEOUT_MS = 3000;
const MAX_ATTEMPTS = 3;
const TOLERANCE_PX = 2;

const TILING_EXTENSIONS = {
    'pop-shell@system76.com': 'pop-shell',
    'tiling-assistant@leleat-on-github': 'tiling-assistant',
    'forge@jmmaranan.com': 'forge',
    'gTile@vibou': 'gtile',
    'tilingshell@ferrarodomenico.com': 'tiling-shell',
};

class BridgeError extends Error {
    constructor(kind, detail) {
        super(detail);
        this.kind = kind;
    }
}

function wid(w) {
    return `gnome:${w.get_id()}`;
}

/** Track whether a window starts being unmanaged (closed) while a timer
 * still refers to it. */
function watchGone(w) {
    const st = {gone: false};
    let hid = w.connect('unmanaging', () => {
        st.gone = true;
    });
    st.release = () => {
        if (hid) {
            w.disconnect(hid);
            hid = 0;
        }
    };
    return st;
}

function rectOf(r) {
    return {x: r.x, y: r.y, w: r.width, h: r.height};
}

function maxDelta(a, b) {
    return Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y),
        Math.abs(a.x + a.w - (b.x + b.w)), Math.abs(a.y + a.h - (b.y + b.h)));
}

function isMaximized(w) {
    if (typeof w.get_maximized === 'function')
        return w.get_maximized() === Meta.MaximizeFlags.BOTH;
    return w.is_maximized();
}

function isPartlyMaximized(w) {
    if (typeof w.get_maximized === 'function') {
        const m = w.get_maximized();
        return m !== 0 && m !== Meta.MaximizeFlags.BOTH;
    }
    return false;
}

function unmaximize(w) {
    // GNOME 49 dropped the flags argument.
    try {
        w.unmaximize(Meta.MaximizeFlags.BOTH);
    } catch (_e) {
        w.unmaximize();
    }
}

function maximize(w) {
    try {
        w.maximize(Meta.MaximizeFlags.BOTH);
    } catch (_e) {
        w.maximize();
    }
}

function windowType(w) {
    const T = Meta.WindowType;
    switch (w.get_window_type()) {
    case T.NORMAL: return 'normal';
    case T.DIALOG:
    case T.MODAL_DIALOG: return 'dialog';
    case T.UTILITY: return 'utility';
    case T.TOOLBAR: return 'toolbar';
    case T.MENU:
    case T.DROPDOWN_MENU:
    case T.POPUP_MENU:
    case T.COMBO: return 'menu';
    case T.SPLASHSCREEN: return 'splash';
    case T.DOCK: return 'dock';
    case T.DESKTOP: return 'desktop';
    case T.NOTIFICATION: return 'notification';
    case T.TOOLTIP: return 'tooltip';
    default: return 'other';
    }
}

// A current event timestamp for focus requests. Outside input handling
// Mutter reports 0 (both get_current_time and the roundtrip variant on a
// Wayland-only session), and a 0 timestamp loses to any recorded user time
// in focus-stealing prevention. Wayland input timestamps are CLOCK_MONOTONIC
// milliseconds, so "now" on that clock is valid and always current.
function currentTime() {
    const t = global.get_current_time();
    if (t)
        return t;
    if (Meta.is_wayland_compositor())
        return Math.floor(GLib.get_monotonic_time() / 1000) >>> 0;
    return global.display.get_current_time_roundtrip();
}

function sanitize(s) {
    return String(s ?? '').replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 48);
}

export default class FractileExtension extends Extension {
    enable() {
        this._enabled = true;
        this._conn = null;
        this._input = null;
        this._cancel = null;
        this._writeQueue = [];
        this._writing = false;
        this._nextId = 1;
        this._replies = new Map();
        this._retryId = 0;
        this._grabs = new Map(); // binding_id -> action
        this._actions = new Map(); // action -> binding_id
        this._outputs = [];
        this._monitorConfig = null;
        this._signals = [];
        this._windowSignals = new Map();
        // Windows created but not yet shown: Mutter has not placed them, and
        // their frame can be 0x0 at 0,0 (GNOME 47). Placing one now is undone
        // by Mutter's initial placement, so they are not offered until shown.
        this._unshown = new Map(); // window -> {shownId, timeoutId}
        this._settleSources = new Set();
        this._overlay = null;
        this._overlayLabel = null;

        this._connectSignals();
        this._refreshOutputs();
        this._connect();
    }

    disable() {
        this._enabled = false;
        if (this._retryId) {
            GLib.source_remove(this._retryId);
            this._retryId = 0;
        }
        for (const id of this._settleSources)
            GLib.source_remove(id);
        this._settleSources.clear();
        for (const [obj, id] of this._signals)
            obj.disconnect(id);
        this._signals = [];
        for (const [w, ids] of this._windowSignals) {
            for (const id of ids)
                w.disconnect(id);
        }
        this._windowSignals.clear();
        for (const w of [...this._unshown.keys()])
            this._dropUnshown(w);
        this._ungrabAll();
        this._destroyOverlay();
        this._closeConnection();
        this._outputs = [];
    }

    // ------------------------------------------------------------ socket

    _socketPath() {
        const dir = GLib.getenv('FRACTILE_RUNTIME_DIR') ||
            GLib.build_filenamev([GLib.get_user_runtime_dir(), 'fractile']);
        return GLib.build_filenamev([dir, 'fractile.sock']);
    }

    _scheduleRetry(seconds) {
        if (!this._enabled || this._retryId)
            return;
        this._retryId = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, seconds, () => {
            this._retryId = 0;
            this._connect();
            return GLib.SOURCE_REMOVE;
        });
    }

    _connect() {
        if (!this._enabled || this._conn)
            return;
        const path = this._socketPath();
        if (!GLib.file_test(path, GLib.FileTest.EXISTS)) {
            this._scheduleRetry(RETRY_SECONDS);
            return;
        }
        const client = new Gio.SocketClient();
        const cancel = new Gio.Cancellable();
        this._cancel = cancel;
        client.connect_async(Gio.UnixSocketAddress.new(path), cancel, (c, res) => {
            let conn;
            try {
                conn = c.connect_finish(res);
            } catch (_e) {
                this._cancel = null;
                this._scheduleRetry(RETRY_SECONDS);
                return;
            }
            if (!this._enabled) {
                conn.close(null);
                return;
            }
            this._conn = conn;
            this._input = Gio.DataInputStream.new(conn.get_input_stream());
            this._readLoop();
            this._handshake();
        });
    }

    _closeConnection() {
        if (this._cancel) {
            this._cancel.cancel();
            this._cancel = null;
        }
        if (this._conn) {
            try {
                this._conn.close(null);
            } catch (_e) {}
        }
        this._conn = null;
        this._input = null;
        this._writeQueue = [];
        this._writing = false;
        for (const [, cb] of this._replies)
            cb({error: {kind: 'disconnected', message: 'connection closed'}});
        this._replies.clear();
    }

    _lost(seconds = RETRY_SECONDS) {
        this._closeConnection();
        this._ungrabAll();
        this._overlay?.hide();
        this._scheduleRetry(seconds);
    }

    _readLoop() {
        const input = this._input;
        if (!input)
            return;
        input.read_line_async(GLib.PRIORITY_DEFAULT, this._cancel, (s, res) => {
            let line;
            try {
                [line] = s.read_line_finish_utf8(res);
            } catch (_e) {
                if (this._input === input)
                    this._lost();
                return;
            }
            if (this._input !== input)
                return;
            if (line === null) {
                this._lost();
                return;
            }
            if (line.trim() !== '') {
                try {
                    this._onMessage(JSON.parse(line));
                } catch (e) {
                    logError(e, 'Fractile: bad message from daemon');
                }
            }
            this._readLoop();
        });
    }

    _send(obj) {
        if (!this._conn)
            return;
        this._writeQueue.push(new GLib.Bytes(new TextEncoder().encode(`${JSON.stringify(obj)}\n`)));
        this._flush();
    }

    _flush() {
        if (this._writing || !this._conn || this._writeQueue.length === 0)
            return;
        this._writing = true;
        const out = this._conn.get_output_stream();
        const bytes = this._writeQueue.shift();
        out.write_bytes_async(bytes, GLib.PRIORITY_DEFAULT, this._cancel, (o, res) => {
            try {
                const n = o.write_bytes_finish(res);
                if (n < bytes.get_size())
                    this._writeQueue.unshift(bytes.slice(n, bytes.get_size()));
            } catch (_e) {
                this._writing = false;
                this._lost();
                return;
            }
            this._writing = false;
            this._flush();
        });
    }

    _request(method, params, cb = null) {
        const id = this._nextId++;
        if (cb)
            this._replies.set(id, cb);
        this._send({id, method, params});
    }

    _handshake() {
        this._request('hello', {protocol: PROTOCOL, client: 'gnome-shell'}, r => {
            if (r.error) {
                log(`Fractile: daemon refused hello: ${r.error.message ?? r.error.kind}`);
                this._lost(REJECTED_RETRY_SECONDS);
                return;
            }
            this._request('bridge_attach', this._attachInfo(), a => {
                if (a.error) {
                    // An X11 session or another integration: back off quietly.
                    log(`Fractile: integration not attached: ${a.error.message ?? a.error.kind}`);
                    this._lost(REJECTED_RETRY_SECONDS);
                }
            });
        });
    }

    _attachInfo() {
        const full = {level: 'full'};
        const tiling = this._tilingManager();
        const notes = [];
        if (tiling)
            notes.push(`The '${tiling}' tiling extension is active; it may re-place windows Fractile moves.`);
        return {
            kind: 'gnome',
            api: ADAPTER_API,
            version: this.metadata['version-name'] ?? String(this.metadata.version ?? ''),
            session_type: Meta.is_wayland_compositor() ? 'wayland' : 'x11',
            compositor_version: `GNOME Shell ${Config.PACKAGE_VERSION}`,
            tiling_manager: tiling,
            notes,
            operations: {
                list_windows: full,
                get_active_window: full,
                get_outputs: {level: 'partial', note: 'Monitor identity comes from EDID vendor/product/serial as reported by Mutter, not a raw EDID hash; monitors without a serial fall back to connector and size.'},
                get_workspaces: full,
                get_geometry: full,
                move_resize: full,
                set_state: full,
                move_to_output: full,
                move_to_workspace: full,
                activate: full,
                subscribe_events: full,
                register_hotkey: {level: 'partial', note: 'Shortcuts are grabbed by GNOME Shell; a combination GNOME already uses cannot be grabbed.'},
                show_overlay: {level: 'full', note: 'The placement preview is drawn by GNOME Shell; Fractile windows are positioned through this extension.'},
                get_permissions: full,
            },
        };
    }

    _tilingManager() {
        const mgr = Main.extensionManager;
        for (const [uuid, name] of Object.entries(TILING_EXTENSIONS)) {
            const e = mgr.lookup(uuid);
            if (e && e.state === 1 /* ExtensionState.ACTIVE (ENABLED in 45) */)
                return name;
        }
        return null;
    }

    _onMessage(msg) {
        if (msg.event !== undefined) {
            if (msg.event === 'bridge_call')
                this._onCall(msg.data ?? {});
            return;
        }
        if (msg.id !== undefined) {
            const cb = this._replies.get(msg.id);
            if (cb) {
                this._replies.delete(msg.id);
                cb(msg);
            }
        }
    }

    _reply(call, ok, payload) {
        const p = ok ? {call, ok: true, result: payload ?? null}
            : {call, ok: false, error: {kind: payload.kind ?? 'other', detail: payload.message ?? String(payload)}};
        this._request('bridge_result', p);
    }

    _emit(ev) {
        if (this._conn)
            this._request('bridge_event', ev);
    }

    _onCall({call, op, args}) {
        const handler = this[`_op_${op}`];
        if (typeof handler !== 'function') {
            this._reply(call, false, new BridgeError('unsupported', `unknown operation '${op}'`));
            return;
        }
        try {
            // Handlers either return a value or a function taking a
            // completion callback (for operations that wait on the compositor).
            const r = handler.call(this, args ?? {});
            if (typeof r === 'function')
                r((ok, v) => this._reply(call, ok, v));
            else
                this._reply(call, true, r);
        } catch (e) {
            this._reply(call, false, e instanceof BridgeError ? e : new BridgeError('other', String(e?.message ?? e)));
        }
    }

    // ------------------------------------------------------------ windows

    _windows() {
        return global.get_window_actors()
            .map(a => a.meta_window)
            .filter(w => w && !w.is_override_redirect() && !this._unshown.has(w));
    }

    _find(id) {
        const w = this._windows().find(x => wid(x) === id);
        if (!w)
            throw new BridgeError('no_such_window', id);
        return w;
    }

    _outputIdFor(monitorIndex) {
        const g = global.display.get_monitor_geometry(monitorIndex);
        const o = this._outputs.find(x => x.rect.x === g.x && x.rect.y === g.y);
        return o ? o.stable_id : `monitor-${monitorIndex}`;
    }

    _info(w, stackIndex, focus) {
        const tracker = Shell.WindowTracker.get_default();
        const app = tracker.get_window_app(w);
        const appId = w.get_gtk_application_id?.() || w.get_sandboxed_app_id?.() || w.get_wm_class() || null;
        const transient = w.get_transient_for();
        const pid = w.get_pid();
        const ws = w.get_workspace();
        const T = Meta.WindowType;
        return {
            id: wid(w),
            title: w.get_title() ?? '',
            app_id: appId,
            wm_class_instance: w.get_wm_class_instance() || null,
            wm_class: w.get_wm_class() || null,
            role: w.get_role() || null,
            desktop_file_id: app && !app.is_window_backed() ? app.get_id() : null,
            desktop_file_source: app && !app.is_window_backed() ? 'gnome-shell-window-tracker' : null,
            pid: pid > 0 ? pid : null,
            window_type: windowType(w),
            transient_for: transient ? wid(transient) : null,
            group: null,
            frame: rectOf(w.get_frame_rect()),
            client: rectOf(w.get_buffer_rect()),
            state: {
                maximized: isMaximized(w),
                fullscreen: w.is_fullscreen(),
                minimized: w.minimized,
                above: w.is_above(),
                sticky: w.is_on_all_workspaces(),
                modal: w.get_window_type() === T.MODAL_DIALOG,
                skip_taskbar: w.is_skip_taskbar(),
                tiled: isPartlyMaximized(w),
            },
            workspace: ws && !w.is_on_all_workspaces() ? ws.index() : null,
            output_id: w.get_monitor() >= 0 ? this._outputIdFor(w.get_monitor()) : null,
            stacking: stackIndex,
            focused: w === focus,
        };
    }

    _op_list_windows() {
        const ws = global.display.sort_windows_by_stacking(this._windows());
        const focus = global.display.focus_window;
        return ws.map((w, i) => this._info(w, i, focus));
    }

    _op_get_active_window() {
        const w = global.display.focus_window;
        return w ? wid(w) : null;
    }

    _op_move_resize({id, rect}) {
        const w = this._find(id);
        if (!rect || rect.w <= 0 || rect.h <= 0)
            throw new BridgeError('rejected', 'empty target rectangle');
        if (w.is_fullscreen())
            w.unmake_fullscreen();
        if (isMaximized(w) || isPartlyMaximized(w))
            unmaximize(w);
        let attempts = 1;
        w.move_resize_frame(true, rect.x, rect.y, rect.w, rect.h);
        return done => {
            // Wait until the client has acknowledged the configure and the
            // frame stops changing, then report what the compositor shows.
            const start = GLib.get_monotonic_time();
            let last = null;
            let stable = 0;
            const gone = watchGone(w);
            const src = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 30, () => {
                const elapsed = (GLib.get_monotonic_time() - start) / 1000;
                // A window closed while settling must not be touched again:
                // move_resize_frame on a window Mutter is unmanaging crashes
                // GNOME Shell (stack_position assertion, then SIGSEGV).
                if (gone.gone) {
                    gone.release();
                    this._settleSources.delete(src);
                    done(false, new BridgeError('no_such_window', `${id} closed while being placed`));
                    return GLib.SOURCE_REMOVE;
                }
                let cur;
                try {
                    cur = rectOf(w.get_frame_rect());
                } catch (_e) {
                    gone.release();
                    this._settleSources.delete(src);
                    done(false, new BridgeError('no_such_window', id));
                    return GLib.SOURCE_REMOVE;
                }
                const same = last && maxDelta(cur, last) === 0;
                stable = same ? stable + 1 : 0;
                last = cur;
                const hit = maxDelta(cur, rect) <= TOLERANCE_PX;
                if (!hit && stable >= 4 && attempts < MAX_ATTEMPTS && elapsed < SETTLE_TIMEOUT_MS) {
                    // Some clients apply the size before the position (or
                    // the reverse); a re-request settles them. Moving to a
                    // monitor with another scale can take a second one: in
                    // Mutter's physical layout mode (GNOME 45) the frame is
                    // rescaled once more when the client switches its buffer
                    // scale after the move.
                    attempts++;
                    stable = 0;
                    w.move_resize_frame(true, rect.x, rect.y, rect.w, rect.h);
                    return GLib.SOURCE_CONTINUE;
                }
                if ((hit && stable >= 2) || (stable >= 6) || elapsed >= SETTLE_TIMEOUT_MS) {
                    gone.release();
                    this._settleSources.delete(src);
                    let note = null;
                    if (!hit)
                        note = 'the window did not take the requested geometry (size limits or the application refused)';
                    done(true, {observed: cur, attempts, tolerance: TOLERANCE_PX, note});
                    return GLib.SOURCE_REMOVE;
                }
                return GLib.SOURCE_CONTINUE;
            });
            this._settleSources.add(src);
        };
    }

    _op_set_state({id, state}) {
        const w = this._find(id);
        state = state ?? {};
        if (state.fullscreen && !w.is_fullscreen())
            w.make_fullscreen();
        else if (!state.fullscreen && w.is_fullscreen())
            w.unmake_fullscreen();
        if (state.maximized && !isMaximized(w))
            maximize(w);
        else if (!state.maximized && (isMaximized(w) || isPartlyMaximized(w)))
            unmaximize(w);
        if (state.above && !w.is_above())
            w.make_above();
        else if (!state.above && w.is_above())
            w.unmake_above();
        if (state.sticky && !w.is_on_all_workspaces())
            w.stick();
        else if (!state.sticky && w.is_on_all_workspaces())
            w.unstick();
        if (state.minimized && !w.minimized)
            w.minimize();
        else if (!state.minimized && w.minimized)
            w.unminimize();
        return null;
    }

    _op_move_to_workspace({id, workspace}) {
        const w = this._find(id);
        const n = global.workspace_manager.get_n_workspaces();
        if (workspace < 0 || workspace >= n)
            throw new BridgeError('rejected', `workspace ${workspace} does not exist (there are ${n})`);
        w.change_workspace_by_index(workspace, false);
        return null;
    }

    _op_activate({id}) {
        const w = this._find(id);
        let ts = currentTime();
        const overview = Main.overview;
        if (overview.visible || overview.animationInProgress) {
            // Leaving the overview pops its modal grab and gives focus back
            // to the window focused before it (GNOME starts in the overview
            // at login), which would undo an activation made now. 'hidden'
            // is emitted just before that grab is released, so activate on
            // the next idle.
            const gone = watchGone(w);
            const id2 = overview.connect('hidden', () => {
                overview.disconnect(id2);
                const idle = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                    this._settleSources.delete(idle);
                    gone.release();
                    if (gone.gone)
                        return GLib.SOURCE_REMOVE;
                    ts = currentTime();
                    Main.activateWindow(w, ts);
                    return GLib.SOURCE_REMOVE;
                });
                this._settleSources.add(idle);
            });
            overview.hide();
        } else {
            Main.activateWindow(w, ts);
        }
        return done => {
            // Report a refusal instead of claiming success.
            let n = 0;
            const src = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 20, () => {
                if (global.display.focus_window === w || ++n >= 50) {
                    this._settleSources.delete(src);
                    if (global.display.focus_window === w)
                        done(true, null);
                    else {
                        const f = global.display.focus_window;
                        log(`Fractile: focus request for ${id} refused (ts ${ts}, window user time ${w.get_user_time()}, focused ${f ? wid(f) : 'none'} user time ${f ? f.get_user_time() : '-'})`);
                        done(false, new BridgeError('rejected', 'GNOME Shell did not give the window focus'));
                    }
                    return GLib.SOURCE_REMOVE;
                }
                return GLib.SOURCE_CONTINUE;
            });
            this._settleSources.add(src);
        };
    }

    _op_session_locked() {
        return Main.sessionMode.isLocked;
    }

    // ------------------------------------------------------------ preview

    // The placement preview is drawn here, in the window layer, instead of
    // by a client window: on Wayland a client overlay would take keyboard
    // focus from the panel and could not position itself.
    _op_show_overlay({rect, label, below_pids}) {
        if (!rect || rect.w <= 0 || rect.h <= 0)
            throw new BridgeError('rejected', 'empty preview rectangle');
        if (!this._overlay) {
            this._overlay = new St.Widget({
                style_class: 'fractile-preview',
                reactive: false,
                layout_manager: new Clutter.BinLayout(),
            });
            this._overlayLabel = new St.Label({
                style_class: 'fractile-preview-label',
                x_align: Clutter.ActorAlign.CENTER,
                y_align: Clutter.ActorAlign.CENTER,
            });
            this._overlay.add_child(this._overlayLabel);
            global.window_group.add_child(this._overlay);
        }
        const group = global.window_group;
        group.set_child_above_sibling(this._overlay, null);
        // Keep it under Fractile's own windows (the panel).
        const pids = new Set(below_pids ?? []);
        const own = global.get_window_actors().filter(a => pids.has(a.meta_window?.get_pid()));
        let lowest = null;
        for (const a of own) {
            if (a.get_parent() !== group)
                continue;
            if (lowest === null || group.get_children().indexOf(a) < group.get_children().indexOf(lowest))
                lowest = a;
        }
        if (lowest)
            group.set_child_below_sibling(this._overlay, lowest);
        this._overlay.set_position(rect.x, rect.y);
        this._overlay.set_size(rect.w, rect.h);
        this._overlayLabel.text = label ?? '';
        this._overlayLabel.visible = Boolean(label);
        this._overlay.show();
        return null;
    }

    _op_hide_overlay() {
        this._overlay?.hide();
        return null;
    }

    _destroyOverlay() {
        this._overlay?.destroy();
        this._overlay = null;
        this._overlayLabel = null;
    }

    // ------------------------------------------------------------ workspaces

    _op_get_workspaces() {
        const wm = global.workspace_manager;
        const active = wm.get_active_workspace_index();
        let names = [];
        try {
            names = new Gio.Settings({schema_id: 'org.gnome.desktop.wm.preferences'}).get_strv('workspace-names');
        } catch (_e) {}
        const out = [];
        for (let i = 0; i < wm.get_n_workspaces(); i++)
            out.push({index: i, name: names[i] || `Workspace ${i + 1}`, active: i === active});
        return out;
    }

    // ------------------------------------------------------------ outputs

    _refreshOutputs() {
        // DisplayConfig is served by this same process: an async call is
        // required (a sync one would deadlock the shell).
        Gio.DBus.session.call(
            'org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
            'org.gnome.Mutter.DisplayConfig', 'GetCurrentState', null, null,
            Gio.DBusCallFlags.NONE, 2000, null, (c, res) => {
                try {
                    this._monitorConfig = c.call_finish(res).deepUnpack();
                } catch (e) {
                    this._monitorConfig = null;
                    log(`Fractile: GetCurrentState failed: ${e.message}`);
                }
                this._buildOutputs();
                this._emit({type: 'outputs_changed'});
            });
        this._buildOutputs();
    }

    _buildOutputs() {
        const display = global.display;
        const n = display.get_n_monitors();
        const primary = display.get_primary_monitor();
        const ws = global.workspace_manager.get_active_workspace();
        // Logical monitors from DisplayConfig: (x, y, scale, transform,
        // primary, [(connector, vendor, product, serial)], props).
        const logical = this._monitorConfig ? this._monitorConfig[2] : [];
        const physical = this._monitorConfig ? this._monitorConfig[1] : [];
        const transforms = ['normal', 'rotate90', 'rotate180', 'rotate270', 'flipped', 'flipped90', 'flipped180', 'flipped270'];
        const outs = [];
        for (let i = 0; i < n; i++) {
            const g = display.get_monitor_geometry(i);
            const wa = ws.get_work_area_for_monitor(i);
            const lm = logical.find(l => l[0] === g.x && l[1] === g.y);
            let name = `monitor-${i}`;
            let description = null;
            let stable = null;
            let fromEdid = false;
            let physicalMm = null;
            let transform = 'normal';
            if (lm) {
                transform = transforms[lm[3]] ?? 'normal';
                const [connector, vendor, product, serial] = lm[5][0] ?? [];
                if (connector) {
                    name = connector;
                    description = [vendor, product].filter(Boolean).join(' ') || null;
                    const pm = physical.find(p => p[0][0] === connector);
                    if (pm) {
                        const props = pm[2] ?? {};
                        const dn = props['display-name'];
                        if (dn)
                            description = typeof dn.unpack === 'function' ? dn.unpack() : dn;
                        const wmm = props['width-mm'];
                        const hmm = props['height-mm'];
                        if (wmm && hmm) {
                            const w = typeof wmm.unpack === 'function' ? wmm.unpack() : wmm;
                            const h = typeof hmm.unpack === 'function' ? hmm.unpack() : hmm;
                            if (w > 0 && h > 0)
                                physicalMm = [w, h];
                        }
                    }
                    if (serial && serial !== '0x00000000' && serial !== 'unknown') {
                        stable = `mon-${sanitize(vendor)}-${sanitize(product)}-${sanitize(serial)}`;
                        fromEdid = true;
                    }
                }
            }
            if (!stable)
                stable = `conn-${name}-${g.width}x${g.height}`;
            outs.push({
                stable_id: stable,
                stable_id_from_edid: fromEdid,
                name,
                description,
                rect: rectOf(g),
                work_area: rectOf(wa),
                scale: display.get_monitor_scale(i),
                transform,
                primary: i === primary,
                physical_mm: physicalMm,
            });
        }
        this._outputs = outs;
    }

    _op_get_outputs() {
        if (this._outputs.length === 0)
            this._buildOutputs();
        return this._outputs;
    }

    // ------------------------------------------------------------ shortcuts

    _op_grab_accelerator({binding_id, accelerator}) {
        this._ungrab(binding_id);
        const action = global.display.grab_accelerator(accelerator, Meta.KeyBindingFlags.NONE);
        if (action === Meta.KeyBindingAction.NONE || !action)
            throw new BridgeError('conflict', `GNOME Shell could not grab ${accelerator} (it is already in use or invalid)`);
        const name = Meta.external_binding_name_for_action(action);
        Main.wm.allowKeybinding(name, Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW);
        this._grabs.set(binding_id, action);
        this._actions.set(action, binding_id);
        return null;
    }

    _op_ungrab_accelerator({binding_id}) {
        this._ungrab(binding_id);
        return null;
    }

    _ungrab(bindingId) {
        const action = this._grabs.get(bindingId);
        if (action === undefined)
            return;
        global.display.ungrab_accelerator(action);
        Main.wm.allowKeybinding(Meta.external_binding_name_for_action(action), Shell.ActionMode.NONE);
        this._grabs.delete(bindingId);
        this._actions.delete(action);
    }

    _ungrabAll() {
        for (const id of [...this._grabs.keys()])
            this._ungrab(id);
    }

    // ------------------------------------------------------------ events

    _on(obj, sig, fn) {
        this._signals.push([obj, obj.connect(sig, fn)]);
    }

    /** Announce a new window once Mutter has shown (and so placed) it. A
     * window that is never shown (created minimized, or a client that never
     * maps it) is announced after a bound anyway. */
    _whenShown(w) {
        const shownId = w.connect('shown', () => {
            if (this._dropUnshown(w))
                this._emit({type: 'window_created', id: wid(w)});
        });
        const timeoutId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, UNSHOWN_TIMEOUT_MS, () => {
            const p = this._unshown.get(w);
            if (p)
                p.timeoutId = 0; // this source ends now; do not remove it again
            if (this._dropUnshown(w))
                this._emit({type: 'window_created', id: wid(w)});
            return GLib.SOURCE_REMOVE;
        });
        this._unshown.set(w, {shownId, timeoutId});
    }

    /** Stop waiting for `w` to be shown. True if it was waiting. */
    _dropUnshown(w) {
        const p = this._unshown.get(w);
        if (!p)
            return false;
        this._unshown.delete(w);
        w.disconnect(p.shownId);
        if (p.timeoutId)
            GLib.source_remove(p.timeoutId);
        return true;
    }

    _trackWindow(w) {
        if (this._windowSignals.has(w))
            return;
        const ids = [
            w.connect('unmanaged', () => {
                // closed before it was ever shown: it was never announced
                const announced = !this._dropUnshown(w);
                const sigs = this._windowSignals.get(w) ?? [];
                this._windowSignals.delete(w);
                for (const id of sigs)
                    w.disconnect(id);
                if (announced)
                    this._emit({type: 'window_closed', id: wid(w)});
            }),
            w.connect('notify::title', () => this._emit({type: 'window_changed', id: wid(w)})),
        ];
        this._windowSignals.set(w, ids);
    }

    _connectSignals() {
        const display = global.display;
        for (const w of this._windows())
            this._trackWindow(w);
        this._on(display, 'window-created', (_d, w) => {
            if (w.is_override_redirect())
                return;
            this._trackWindow(w);
            this._whenShown(w);
        });
        this._on(display, 'notify::focus-window', () => {
            const w = display.focus_window;
            this._emit({type: 'active_changed', id: w ? wid(w) : null});
        });
        this._on(display, 'accelerator-activated', (_d, action, _device, timestamp) => {
            const binding = this._actions.get(action);
            if (binding !== undefined)
                this._emit({type: 'hotkey', binding_id: binding, timestamp: timestamp >>> 0});
        });
        this._on(display, 'workareas-changed', () => {
            this._buildOutputs();
            this._emit({type: 'outputs_changed'});
        });
        this._on(Main.layoutManager, 'monitors-changed', () => this._refreshOutputs());
        const wm = global.workspace_manager;
        this._on(wm, 'notify::n-workspaces', () => this._emit({type: 'workspaces_changed'}));
        this._on(wm, 'active-workspace-changed', () => {
            this._buildOutputs();
            this._emit({type: 'workspaces_changed'});
        });
    }
}
