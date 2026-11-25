// Fractile compositor integration for KWin 6 (Plasma 6).
//
// KWin scripts can only make outgoing D-Bus calls, so this script attaches to
// the Fractile service (io.fractile.Bridge, owned by fractiled, which checks
// that every caller is the KWin process) and long-polls it for requests. It
// answers each request with Result and reports window, output and workspace
// changes with Event. It stores nothing and talks to nothing else.
//
// SPDX-License-Identifier: GPL-3.0-or-later

const BUS = "io.fractile.Bridge";
const PATH = "/io/fractile/Bridge";
const IFACE = "io.fractile.Bridge1";
const ADAPTER_API = 1;
// Short polls double as the heartbeat the daemon uses to notice an
// unloaded script; a missed poll reply means the daemon went away.
const POLL_MS = 4000;
const TOLERANCE_PX = 2;
const SETTLE_MS = 1500;

let attached = false;
let attaching = false;
let lastPollReturn = 0;
let generation = 0;

function now() {
    return Date.now();
}

function wid(w) {
    return "kwin:" + String(w.internalId).replace(/[{}]/g, "");
}

function rectOf(r) {
    return {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
}

function maxDelta(a, b) {
    return Math.max(Math.abs(a.x - b.x), Math.abs(a.y - b.y),
        Math.abs(a.x + a.w - (b.x + b.w)), Math.abs(a.y + a.h - (b.y + b.h)));
}

function sanitize(s) {
    return String(s || "").replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 48);
}

// A QTimer nothing in the script refers to can be garbage-collected before
// it fires (seen on KWin 6.6: a pending move_resize never answered), so each
// one is held here until it has fired.
const pendingTimers = {};
let nextTimer = 0;

function later(ms, fn) {
    const key = ++nextTimer;
    const t = new QTimer();
    t.singleShot = true;
    t.interval = ms;
    t.timeout.connect(function () {
        delete pendingTimers[key];
        fn();
    });
    pendingTimers[key] = t;
    t.start();
    return t;
}

function windowType(w) {
    if (w.normalWindow) return "normal";
    if (w.dialog) return "dialog";
    if (w.utility) return "utility";
    if (w.toolbar) return "toolbar";
    if (w.menu || w.dropdownMenu || w.popupMenu || w.comboBox) return "menu";
    if (w.splash) return "splash";
    if (w.dock) return "dock";
    if (w.desktopWindow) return "desktop";
    if (w.notification || w.criticalNotification || w.onScreenDisplay) return "notification";
    if (w.tooltip) return "tooltip";
    return "other";
}

function managedWindows() {
    return workspace.windowList().filter(function (w) {
        return w.managed && !w.deleted;
    });
}

function find(id) {
    const ws = managedWindows();
    for (let i = 0; i < ws.length; i++) {
        if (wid(ws[i]) === id) return ws[i];
    }
    throw {kind: "no_such_window", message: id};
}

// ---------------------------------------------------------------- outputs

function outputId(o) {
    if (o.serialNumber && o.serialNumber !== "0") {
        return "mon-" + sanitize(o.manufacturer) + "-" + sanitize(o.model) + "-" + sanitize(o.serialNumber);
    }
    return "conn-" + o.name + "-" + Math.round(o.geometry.width) + "x" + Math.round(o.geometry.height);
}

function outputs() {
    const screens = workspace.screens;
    const primary = workspace.activeScreen;
    const out = [];
    for (let i = 0; i < screens.length; i++) {
        const o = screens[i];
        const area = workspace.clientArea(KWin.MaximizeArea, o, workspace.currentDesktop);
        const fromEdid = Boolean(o.serialNumber && o.serialNumber !== "0");
        out.push({
            stable_id: outputId(o),
            stable_id_from_edid: fromEdid,
            name: o.name,
            description: [o.manufacturer, o.model].filter(Boolean).join(" ") || null,
            rect: rectOf(o.geometry),
            work_area: rectOf(area),
            scale: o.devicePixelRatio || 1.0,
            transform: "normal",
            primary: o === primary,
            physical_mm: null,
        });
    }
    return out;
}

// ---------------------------------------------------------------- windows

function isMaximized(w) {
    const area = workspace.clientArea(KWin.MaximizeArea, w.output, workspace.currentDesktop);
    return maxDelta(rectOf(w.frameGeometry), rectOf(area)) === 0 && w.maximizable;
}

function info(w, stacking, active) {
    const desktops = w.desktops || [];
    const all = workspace.desktops;
    let wsIndex = null;
    if (!w.onAllDesktops && desktops.length === 1) {
        for (let i = 0; i < all.length; i++) {
            if (all[i] === desktops[0]) wsIndex = i;
        }
    }
    return {
        id: wid(w),
        title: w.caption || "",
        app_id: w.desktopFileName || w.resourceClass || null,
        wm_class_instance: w.resourceName || null,
        wm_class: w.resourceClass || null,
        role: w.windowRole || null,
        desktop_file_id: w.desktopFileName ? (w.desktopFileName.endsWith(".desktop") ? w.desktopFileName : w.desktopFileName + ".desktop") : null,
        desktop_file_source: w.desktopFileName ? "kwin-desktop-file-name" : null,
        pid: w.pid > 0 ? w.pid : null,
        window_type: windowType(w),
        transient_for: w.transient && w.transientFor ? wid(w.transientFor) : null,
        group: null,
        frame: rectOf(w.frameGeometry),
        client: rectOf(w.clientGeometry),
        constraints: {
            min_w: w.minSize && w.minSize.width > 0 ? Math.round(w.minSize.width) : null,
            min_h: w.minSize && w.minSize.height > 0 ? Math.round(w.minSize.height) : null,
            max_w: w.maxSize && w.maxSize.width > 0 && w.maxSize.width < 32767 ? Math.round(w.maxSize.width) : null,
            max_h: w.maxSize && w.maxSize.height > 0 && w.maxSize.height < 32767 ? Math.round(w.maxSize.height) : null,
            inc_w: null,
            inc_h: null,
        },
        state: {
            maximized: isMaximized(w),
            fullscreen: Boolean(w.fullScreen),
            minimized: Boolean(w.minimized),
            above: Boolean(w.keepAbove),
            sticky: Boolean(w.onAllDesktops),
            modal: Boolean(w.modal),
            skip_taskbar: Boolean(w.skipTaskbar),
            tiled: false,
        },
        workspace: wsIndex,
        output_id: w.output ? outputId(w.output) : null,
        stacking: stacking,
        focused: w === active,
    };
}

const ops = {
    list_windows: function () {
        const ws = managedWindows();
        ws.sort(function (a, b) {
            return a.stackingOrder - b.stackingOrder;
        });
        const active = workspace.activeWindow;
        return ws.map(function (w, i) {
            return info(w, i, active);
        });
    },
    get_active_window: function () {
        const w = workspace.activeWindow;
        return w ? wid(w) : null;
    },
    get_outputs: function () {
        return outputs();
    },
    get_workspaces: function () {
        const all = workspace.desktops;
        const cur = workspace.currentDesktop;
        return all.map(function (d, i) {
            return {index: i, name: d.name || ("Desktop " + (i + 1)), active: d === cur};
        });
    },
    move_resize: function (a, done) {
        const w = find(a.id);
        const r = a.rect;
        if (!r || r.w <= 0 || r.h <= 0) throw {kind: "rejected", message: "empty target rectangle"};
        if (!w.moveable || !w.resizeable) throw {kind: "rejected", message: "the window cannot be moved or resized"};
        if (w.fullScreen) w.fullScreen = false;
        if (isMaximized(w)) w.setMaximize(false, false);
        let attempts = 1;
        const apply = function () {
            w.frameGeometry = {x: r.x, y: r.y, width: r.w, height: r.h};
        };
        apply();
        const start = now();
        let last = null;
        let stable = 0;
        const tick = function () {
            try {
                step();
            } catch (e) {
                done(false, {kind: "other", message: "while waiting for the window to settle: " + String(e)});
            }
        };
        const step = function () {
            if (w.deleted) {
                done(false, {kind: "no_such_window", message: a.id});
                return;
            }
            const cur = rectOf(w.frameGeometry);
            stable = last && maxDelta(cur, last) === 0 ? stable + 1 : 0;
            last = cur;
            const hit = maxDelta(cur, r) <= TOLERANCE_PX;
            const elapsed = now() - start;
            if (!hit && stable >= 4 && attempts < 2 && elapsed < SETTLE_MS) {
                attempts++;
                stable = 0;
                apply();
            } else if ((hit && stable >= 2) || stable >= 6 || elapsed >= SETTLE_MS) {
                done(true, {observed: cur, attempts: attempts, tolerance: TOLERANCE_PX,
                    note: hit ? null : "the window did not take the requested geometry (size limits or the application refused)"});
                return;
            }
            later(30, tick);
        };
        later(30, tick);
    },
    set_state: function (a) {
        const w = find(a.id);
        const s = a.state || {};
        if (Boolean(s.fullscreen) !== Boolean(w.fullScreen)) w.fullScreen = Boolean(s.fullscreen);
        if (s.maximized && !isMaximized(w)) w.setMaximize(true, true);
        else if (!s.maximized && isMaximized(w)) w.setMaximize(false, false);
        if (Boolean(s.above) !== Boolean(w.keepAbove)) w.keepAbove = Boolean(s.above);
        if (Boolean(s.sticky) !== Boolean(w.onAllDesktops)) w.onAllDesktops = Boolean(s.sticky);
        if (Boolean(s.minimized) !== Boolean(w.minimized)) w.minimized = Boolean(s.minimized);
        return null;
    },
    move_to_workspace: function (a) {
        const w = find(a.id);
        const all = workspace.desktops;
        if (a.workspace < 0 || a.workspace >= all.length)
            throw {kind: "rejected", message: "workspace " + (a.workspace + 1) + " does not exist (there are " + all.length + ")"};
        w.desktops = [all[a.workspace]];
        return null;
    },
    activate: function (a, done) {
        const w = find(a.id);
        if (w.minimized) w.minimized = false;
        workspace.activeWindow = w;
        later(60, function () {
            if (workspace.activeWindow === w) done(true, null);
            else done(false, {kind: "rejected", message: "KWin did not give the window focus"});
        });
    },
    session_locked: function () {
        // KWin scripts cannot observe the lock screen; the daemon relies on
        // logind's LockedHint (set by KDE's screen locker) instead.
        return null;
    },
};

// ---------------------------------------------------------------- transport

function send(method, payload) {
    callDBus(BUS, PATH, IFACE, method, JSON.stringify(payload));
}

function reply(call, ok, v) {
    if (ok) send("Result", {call: call, ok: true, result: v === undefined ? null : v});
    else send("Result", {call: call, ok: false, error: {kind: (v && v.kind) || "other", detail: (v && v.message) || String(v)}});
}

function handle(c) {
    const fn = ops[c.op];
    if (!fn) {
        reply(c.call, false, {kind: "unsupported", message: "unknown operation '" + c.op + "'"});
        return;
    }
    try {
        if (fn.length >= 2) {
            fn(c.args || {}, function (ok, v) {
                reply(c.call, ok, v);
            });
        } else {
            reply(c.call, true, fn(c.args || {}));
        }
    } catch (e) {
        reply(c.call, false, e && e.kind ? e : {kind: "other", message: String(e)});
    }
}

function poll(gen) {
    callDBus(BUS, PATH, IFACE, "Poll", POLL_MS, function (res) {
        if (gen !== generation) return;
        lastPollReturn = now();
        let calls = [];
        try {
            calls = JSON.parse(res);
        } catch (e) {
            calls = [];
        }
        for (let i = 0; i < calls.length; i++) handle(calls[i]);
        poll(gen);
    });
}

function attachInfo() {
    const full = {level: "full"};
    return {
        kind: "kwin",
        api: ADAPTER_API,
        version: "0.9.1",
        session_type: "wayland",
        compositor_version: "KWin " + (typeof KWin.version !== "undefined" ? KWin.version : "6"),
        tiling_manager: null,
        notes: [],
        operations: {
            list_windows: full,
            get_active_window: full,
            get_outputs: {level: "partial", note: "Monitor identity comes from the EDID manufacturer/model/serial KWin reports; monitors without a serial fall back to connector and size. Rotation is not reported."},
            get_workspaces: full,
            get_geometry: full,
            move_resize: full,
            set_state: {level: "partial", note: "Maximized state is inferred from the window filling the work area."},
            move_to_output: full,
            move_to_workspace: full,
            activate: full,
            subscribe_events: full,
            show_overlay: {level: "unsupported", note: "KWin scripts cannot draw; the Fractile UI draws the preview."},
            get_permissions: full,
        },
    };
}

function attach() {
    if (attaching) return;
    attaching = true;
    const gen = ++generation;
    callDBus(BUS, PATH, IFACE, "Attach", JSON.stringify(attachInfo()), function () {
        if (gen !== generation) return;
        attaching = false;
        attached = true;
        lastPollReturn = now();
        poll(gen);
    });
    // callDBus reports no errors to scripts: give up on this attempt after
    // a while and let the watchdog retry.
    later(5000, function () {
        if (gen === generation && !attached) attaching = false;
    });
}

function emit(ev) {
    if (attached) send("Event", ev);
}

// Watchdog: (re)attach while Fractile is not running or restarted.
const watchdog = new QTimer();
watchdog.interval = 1000;
watchdog.timeout.connect(function () {
    if (attached && now() - lastPollReturn > POLL_MS + 3000) {
        attached = false;
        attaching = false;
        generation++;
    }
    if (!attached) attach();
});
watchdog.start();

function track(w) {
    w.captionChanged.connect(function () {
        emit({type: "window_changed", id: wid(w)});
    });
}

workspace.windowList().forEach(track);
workspace.windowAdded.connect(function (w) {
    track(w);
    emit({type: "window_created", id: wid(w)});
});
workspace.windowRemoved.connect(function (w) {
    emit({type: "window_closed", id: wid(w)});
});
workspace.windowActivated.connect(function (w) {
    emit({type: "active_changed", id: w ? wid(w) : null});
});
workspace.screensChanged.connect(function () {
    emit({type: "outputs_changed"});
});
workspace.desktopsChanged.connect(function () {
    emit({type: "workspaces_changed"});
});
workspace.currentDesktopChanged.connect(function () {
    emit({type: "workspaces_changed"});
});

attach();
