#!/usr/bin/env python3
"""Performance and soak measurements on a real window manager (nested GNOME
Shell on X11), with the real fractiled, the real PyQt6 UI and real GTK4/Qt
windows. Measures the SRS section 10 quality targets and runs a timed soak
(SRS T27) with repeated simulated relogins.

What is measured, and how:
  idle RSS / threads / fds   /proc/<fractiled>/status and /proc/<pid>/fd, after
                             a warm-up, with the UI attached and windows open
  idle CPU                   utime+stime deltas of fractiled over 1 s samples
  placement latency          fractiled's own timing of place (request ->
                             geometry read back from the WM), plus the IPC
                             round trip seen by a client
  panel latency              real global hotkey (XTEST key events sent by this
                             harness, never by Fractile) -> panel window
                             viewable (X map state). Excludes compositor paint.
  soak                       for --minutes: placements, panel show/hide by
                             hotkey, snapshot save + restore, layout apply;
                             RSS/fd/thread samples of one long-lived fractiled
  relogins                   --relogins cycles of: stop service and UI as a
                             logout does, new XDG_SESSION_ID, start with
                             --autostart and policy last_clean; then check for
                             ghost processes and duplicate windows
  mid-restore logouts        --midrestore cycles of: log in, re-snap a window
                             the login restore already placed, log out while
                             the restore is still running; then one last login
                             must restore everything, the user's changes
                             included, without pausing automatic restore

This is a measurement on one machine and a nested X11 desktop, not a 24 h run
and not a real logout. Results go to tests/evidence/perf-x11.json.

Usage: tests/perf/soak_x11.py [--display 23] [--minutes 20] [--relogins 10]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "gui"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))
import e2e_x11  # noqa: E402
from gui_x11 import GuiEnv  # noqa: E402
from e2e_x11 import check, results  # noqa: E402
from Xlib import X, XK, display as xdisplay, error as xerror  # noqa: E402
from Xlib.ext import xtest  # noqa: E402

TICK = os.sysconf("SC_CLK_TCK")
PAGE_KIB = os.sysconf("SC_PAGE_SIZE") // 1024


def proc_sample(pid: int) -> dict | None:
    try:
        st = Path(f"/proc/{pid}/status").read_text()
        kv = {ln.split(":", 1)[0]: ln.split(":", 1)[1].strip() for ln in st.splitlines() if ":" in ln}
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"rss_kib": int(kv["VmRSS"].split()[0]), "threads": int(kv["Threads"]), "fds": len(os.listdir(f"/proc/{pid}/fd")),
                "cpu_ticks": int(stat[11]) + int(stat[12]), "t": time.time()}
    except (OSError, KeyError, ValueError, IndexError):
        return None


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def children_of(pid: int) -> list[int]:
    out = []
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                if int((d / "stat").read_text().rsplit(")", 1)[1].split()[1]) == pid:
                    out.append(int(d.name))
            except (OSError, ValueError, IndexError):
                pass
    return out


def zombies_of(pid: int) -> int:
    n = 0
    for c in children_of(pid):
        try:
            if Path(f"/proc/{c}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
                n += 1
        except OSError:
            pass
    return n


class Keys:
    """XTEST key events from the test harness (the product never injects)."""

    def __init__(self, disp: str):
        self.d = xdisplay.Display(disp)
        self.root = self.d.screen().root
        self._names: dict[int, str] = {}

    def combo(self, *names: str) -> None:
        codes = [self.d.keysym_to_keycode(XK.string_to_keysym(n)) for n in names]
        for c in codes:
            xtest.fake_input(self.d, X.KeyPress, c)
        for c in reversed(codes):
            xtest.fake_input(self.d, X.KeyRelease, c)
        self.d.sync()

    def visible_named(self, prefix: str) -> list[int]:
        """Managed windows (_NET_CLIENT_LIST) named prefix* that are viewable.
        Cheap enough to call every few milliseconds; names are cached."""
        atom = self.d.intern_atom("_NET_CLIENT_LIST")
        prop = self.root.get_full_property(atom, X.AnyPropertyType)
        out = []
        for xid in (prop.value if prop else []):
            name = self._names.get(xid)
            if not name:  # unknown, or not named yet when last seen
                try:
                    name = self.d.create_resource_object("window", xid).get_wm_name() or ""
                except xerror.XError:
                    name = ""
                self._names[xid] = name if isinstance(name, str) else ""
            if self._names[xid].startswith(prefix) and self.viewable(xid):
                out.append(xid)
        return out

    def viewable(self, xid: int) -> bool:
        try:
            return self.d.create_resource_object("window", xid).get_attributes().map_state == X.IsViewable
        except xerror.XError:
            return False


def snaps_by_mode(e) -> dict:
    out: dict = {}
    for x in e.cli_json("list-snapshots", "--limit", "500"):
        out[x["mode"]] = out.get(x["mode"], 0) + 1
    return out


def restore_running(e) -> bool:
    try:
        return json.loads(e.run("--json", "restore-status").stdout).get("running") is not None
    except ValueError:
        return False


def restore_summary(e) -> str:
    try:
        rj = json.loads(e.run("--json", "restore-status").stdout)
    except ValueError:
        return "?"
    last = (rj.get("recent_runs") or [{}])[0]
    return json.dumps({"running": rj.get("running"), "last_run": {k: last.get(k) for k in ("run_id", "source", "state", "summary")}})


def run_all(e: GuiEnv, a) -> dict:
    m: dict = {}
    e.start_daemon()
    dpid = e.daemon.pid
    c = e.client()
    c.call("set_config", {"patch": {"general": {"first_run_complete": True, "panel_shortcut": "Ctrl+Alt+G"},
                                    "session": {"max_recent": 20}}})
    c.close()
    e.window("gtk4", "org.fractile.SoakA", "Soak A")
    e.window("qt", "org.fractile.SoakB", "Soak B")
    e.window("gtk4", "org.fractile.SoakC", "Soak C")
    # 60 s: in a fresh VM the first GTK4 window takes over 15 s (cold caches,
    # software rendering); start-up time is not what this test measures.
    wins = [e.wait_window(lambda w, t=t: w.get("title") == t, 60.0) for t in ("Soak A", "Soak B", "Soak C")]
    if not check("P00", "test windows are up", all(wins)):
        return m
    wids = [w["id"] for w in wins]
    keys = Keys(f":{a.display}")

    # Bring the UI up once (first panel show spawns it), then let it settle.
    e.xdo("windowactivate", "--sync", str(int(wids[0].split(":")[1], 16)))
    keys.combo("Control_L", "Alt_L", "g")
    first = e.wait_for(lambda: keys.visible_named("Fractile panel"), 20.0, 0.2)
    keys.combo("Escape")
    if not check("P01", "UI starts and the panel appears on the global hotkey", bool(first)):
        # Say why, here: the UI log goes away with the test environment.
        names = subprocess.run(["sh", "-c", f"for w in $(xprop -root _NET_CLIENT_LIST | grep -o '0x[0-9a-f]*'); do xprop -id $w WM_NAME; done"],
                               env={**os.environ, "DISPLAY": f":{a.display}"}, capture_output=True, text=True).stdout
        print("P01 diagnostics: ui pids", e.ui_pids(), "| windows:", names.replace("\n", "; "), flush=True)
        print("P01 status:", e.run("status").stdout[-1500:], flush=True)
        if e.ui_log.exists():
            print("P01 ui log:\n" + e.ui_log.read_text()[-3000:], flush=True)
    time.sleep(5)

    # Idle footprint and CPU, warm service, UI attached, 3 windows open.
    samples = []
    for _ in range(a.idle_seconds + 1):
        samples.append(proc_sample(dpid))
        time.sleep(1)
    cpu = [100.0 * (b["cpu_ticks"] - x["cpu_ticks"]) / TICK / (b["t"] - x["t"]) for x, b in zip(samples, samples[1:])]
    ui = e.ui_pids()
    ui_rss = (proc_sample(ui[0]) or {}).get("rss_kib") if ui else None
    m["idle"] = {"seconds": a.idle_seconds, "rss_mib": round(samples[-1]["rss_kib"] / 1024, 1), "threads": samples[-1]["threads"], "fds": samples[-1]["fds"],
                 "cpu_pct_median": round(statistics.median(cpu), 3), "cpu_pct_mean": round(statistics.mean(cpu), 3), "cpu_pct_max": round(max(cpu), 3),
                 "ui_rss_mib": ui_rss and round(ui_rss / 1024, 1)}
    check("P02", "idle resident memory of fractiled < 75 MiB (UI excluded)", samples[-1]["rss_kib"] < 75 * 1024,
          f"{m['idle']['rss_mib']} MiB RSS, {m['idle']['threads']} threads, {m['idle']['fds']} fds; UI process {m['idle']['ui_rss_mib']} MiB (not counted)")
    check("P03", "idle CPU median < 0.5% of one core", statistics.median(cpu) < 0.5,
          f"median {m['idle']['cpu_pct_median']}%, mean {m['idle']['cpu_pct_mean']}%, max {m['idle']['cpu_pct_max']}% over {a.idle_seconds} s")

    # Placement latency: 200 placements on random grids.
    rnd = random.Random(11)
    c = e.client()
    rtt, inside = [], []
    for i in range(200):
        cols, rows = rnd.randint(1, 8), rnd.randint(1, 8)
        c0, r0 = rnd.randint(0, cols - 1), rnd.randint(0, rows - 1)
        sel = {"col0": c0, "row0": r0, "col1": rnd.randint(c0, cols - 1), "row1": rnd.randint(r0, rows - 1)}
        t0 = time.perf_counter()
        out = c.call("place", {"window": wids[i % 3], "grid": {"cols": cols, "rows": rows}, "selection": sel})
        rtt.append((time.perf_counter() - t0) * 1000)
        inside.append(bool(out.get("within_tolerance")))
    d = c.call("diagnose")
    c.close()
    pl = d.get("placement_latency_ms", {})
    m["placement"] = {"n": 200, "service_p50_ms": pl.get("p50"), "service_p95_ms": pl.get("p95"), "client_rtt_p50_ms": round(pct(rtt, 50), 1),
                      "client_rtt_p95_ms": round(pct(rtt, 95), 1), "within_tolerance": sum(inside)}
    check("P04", "placement P95 < 250 ms (request to geometry read back from the WM)", (pl.get("p95") or 1e9) < 250 and pct(rtt, 95) < 250,
          f"service p50 {pl.get('p50')} / p95 {pl.get('p95')} ms; client round trip p50 {m['placement']['client_rtt_p50_ms']} / p95 {m['placement']['client_rtt_p95_ms']} ms; "
          f"{sum(inside)}/200 within tolerance")

    # Panel latency: hotkey -> panel viewable, warm.
    lat, miss = [], 0
    for i in range(a.panel_runs + 3):
        e.xdo("windowactivate", "--sync", str(int(wids[i % 3].split(":")[1], 16)))
        time.sleep(0.3)
        t0 = time.perf_counter()
        keys.combo("Control_L", "Alt_L", "g")
        seen = None
        while time.perf_counter() - t0 < 2.0:
            if keys.visible_named("Fractile panel"):
                seen = time.perf_counter()
                break
            time.sleep(0.002)
        if i >= 3:  # warm-up excluded
            if seen:
                lat.append((seen - t0) * 1000)
            else:
                miss += 1
        keys.combo("Escape")
        e.wait_for(lambda: not keys.visible_named("Fractile panel"), 2.0, 0.01)
        time.sleep(0.2)
    m["panel"] = {"n": len(lat), "missed": miss, "p50_ms": lat and round(pct(lat, 50), 1), "p95_ms": lat and round(pct(lat, 95), 1), "max_ms": lat and round(max(lat), 1)}
    check("P05", "panel P95 < 150 ms after the hotkey (warm service; until the panel window is viewable)", lat and not miss and pct(lat, 95) < 150,
          f"p50 {m['panel']['p50_ms']} / p95 {m['panel']['p95_ms']} / max {m['panel']['max_ms']} ms over {len(lat)} presses, {miss} missed")

    # Soak: one long-lived service under steady use.
    e.cli_json("save", "--name", "soak-base")
    e.run("layout", "save", "soak-layout", "--no-launch")
    trace = []
    slots: dict[int, str] = {}  # soak-N name -> its current snapshot id
    last_trace = 0.0
    t_end = time.time() + a.minutes * 60
    cycles = errors = 0
    t_soak = time.time()
    last_progress = t_soak
    while time.time() < t_end:
        try:
            c = e.client()
            for k in range(10):
                cols, rows = rnd.randint(1, 10), rnd.randint(1, 10)
                c0, r0 = rnd.randint(0, cols - 1), rnd.randint(0, rows - 1)
                c.call("place", {"window": wids[k % 3], "grid": {"cols": cols, "rows": rows}, "selection": {"col0": c0, "row0": r0, "col1": c0, "row1": r0}})
            c.close()
            e.xdo("windowactivate", "--sync", str(int(wids[cycles % 3].split(":")[1], 16)))
            keys.combo("Control_L", "Alt_L", "g")
            time.sleep(0.4)
            keys.combo("Escape")
            # Named saves are pinned, so retention never removes them: keep
            # five named slots and delete each slot's previous snapshot, or a
            # 24 h run piles up ~20,000 pinned snapshots.
            slot = cycles % 5
            new_id = e.cli_json("save", "--name", f"soak-{slot}").get("snapshot_id")
            if slot in slots:
                e.run("snapshot", "delete", slots[slot])
            if new_id:
                slots[slot] = new_id
            rep = e.cli_json("restore", "--snapshot", "latest", "--no-launch")
            errors += rep.get("state") != "completed"
            if cycles % 3 == 0:
                e.run("layout", "apply", "soak-layout")
        except Exception as ex:  # noqa: BLE001 - count, keep soaking
            errors += 1
            print("soak error:", ex, flush=True)
        cycles += 1
        s = proc_sample(dpid)
        if s and time.time() - last_trace >= 30:  # bounded: ~2,900 samples per day
            last_trace = time.time()
            trace.append({"t": round(s["t"] - t_soak, 1), "rss_kib": s["rss_kib"], "fds": s["fds"], "threads": s["threads"]})
        # A progress line every minute, so a long run can be checked from
        # outside (a stopped run and a running one look alike otherwise). It
        # includes the memory of the whole test (Xephyr, the nested shell,
        # the UI, the test windows), not only the service.
        if time.time() - last_progress >= 60:
            last_progress = time.time()
            print(f"soak progress: {round((last_progress - t_soak) / 60)} min, {cycles} cycles, {errors} errors"
                  + (f", fractiled rss {s['rss_kib'] / 1024:.1f} MiB, fds {s['fds']}, threads {s['threads']}" if s else "")
                  + f", whole test {test_memory()}", flush=True)
        if e.daemon.poll() is not None:
            break
    alive = e.daemon.poll() is None
    n = max(1, len(trace) // 10)
    head = statistics.median(x["rss_kib"] for x in trace[:n]) if trace else 0
    tail = statistics.median(x["rss_kib"] for x in trace[-n:]) if trace else 0
    m["soak"] = {"minutes": a.minutes, "cycles": cycles, "errors": errors, "rss_mib_start": round(head / 1024, 1), "rss_mib_end": round(tail / 1024, 1),
                 "rss_mib_max": trace and round(max(x["rss_kib"] for x in trace) / 1024, 1),
                 "fds_start_end": trace and [trace[0]["fds"], trace[-1]["fds"]], "threads_start_end": trace and [trace[0]["threads"], trace[-1]["threads"]],
                 "zombie_children": zombies_of(dpid), "snapshots_by_mode": snaps_by_mode(e), "trace": trace[:: max(1, len(trace) // 60)]}
    growth = (tail - head) / 1024
    fd_ok = trace and abs(trace[-1]["fds"] - trace[0]["fds"]) <= 3
    th_ok = trace and abs(trace[-1]["threads"] - trace[0]["threads"]) <= 3
    check("P06", f"{a.minutes}-minute soak: service alive, no errors, RSS growth < 5 MiB, fds and threads flat, no zombie children",
          alive and not errors and growth < 5 and fd_ok and th_ok and m["soak"]["zombie_children"] == 0,
          f"{cycles} cycles (~{cycles * 10} placements, {cycles} saves+restores); RSS {m['soak']['rss_mib_start']} -> {m['soak']['rss_mib_end']} MiB (max {m['soak']['rss_mib_max']}); "
          f"fds {m['soak']['fds_start_end']}, threads {m['soak']['threads_start_end']}; {errors} errors")
    bm = m["soak"]["snapshots_by_mode"]
    auto = sum(v for k, v in bm.items() if k != "manual")
    # Retention keeps the max_recent newest, plus the newest of each
    # calendar day beyond those (daily history): one extra per local day the
    # run touched. (A 24 h run crosses midnight: 20 + 2.)
    days = len({time.strftime("%Y-%m-%d", time.localtime(t)) for t in (t_soak, time.time())} |
               {time.strftime("%Y-%m-%d", time.localtime(t_soak + k * 86400)) for k in range(int((time.time() - t_soak) // 86400) + 1)})
    check("P07", "automatic snapshots stay bounded by retention during the soak (max_recent=20, plus the newest of each day); manual saves are kept by design",
          auto <= 20 + days, f"{auto} automatic after {cycles} restores (each writes a pre-restore baseline), run spans {days} day(s); by mode {bm}")

    # Repeated simulated relogins.
    c = e.client()
    c.call("set_config", {"patch": {"session": {"policy": "last_clean", "confirm_before_restore": False}}})
    c.close()
    ghosts, dups, restored_ok, start_ms, fails = [], 0, 0, [], []
    for i in range(a.relogins):
        old_d = e.daemon.pid
        old_ui = e.ui_pids()
        e.cli_json("snap", "top-left-quarter", "--window", wids[i % 3])
        want = e.frame(wids[i % 3])
        e.stop_daemon(signal.SIGTERM)  # logout: the service snapshots and exits
        for p in old_ui:  # the session ends the UI too
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass
        e.wait_for(lambda: not any(Path(f"/proc/{p}").exists() for p in [old_d, *old_ui]), 10.0, 0.1)
        # the next session's desktop puts the window somewhere else
        xid = str(int(wids[i % 3].split(":")[1], 16))
        e.xdo("windowsize", xid, "500", "400")
        e.xdo("windowmove", xid, "700", "500")
        e.env["XDG_SESSION_ID"] = f"soak-{os.getpid()}-{i}"
        t0 = time.perf_counter()
        e.start_daemon("--autostart")
        start_ms.append((time.perf_counter() - t0) * 1000)
        ok = bool(e.wait_for(lambda: e2e_x11.rect_eq(e.frame(wids[i % 3]), want, 2), 15.0, 0.3))
        restored_ok += ok
        # a user who logs in and works: the login restore has finished first
        # (logging out during a restore is P09)
        e.wait_for(lambda: not restore_running(e), 15.0, 0.2)
        if not ok:
            rs = restore_summary(e)
            log = [ln for ln in (e.base / "daemon.out").read_text().splitlines() if "restore" in ln.lower() or "snapshot" in ln.lower()][-8:]
            fails.append(f"relogin {i}: {wids[i % 3]} at {e.frame(wids[i % 3])} want {want}; restore-status {rs[:300]}; log: {' | '.join(log)}")
            print("relogin miss:", fails[-1], flush=True)
        left = [p for p in [old_d, *old_ui] if Path(f"/proc/{p}").exists()]
        ghosts += left
        dups += sum(1 for w in e.cli_json("list-windows") if (w.get("title") or "").startswith("Soak ")) - 3
    e.wait_for(lambda: len(e.ui_pids()) <= 1, 5.0)
    uis = len(e.ui_pids())
    m["relogins"] = {"n": a.relogins, "restored": restored_ok, "ghost_pids": ghosts, "duplicate_windows": dups, "ui_processes_after": uis,
                     "misses": fails, "service_ready_ms_p50": round(pct(start_ms, 50)), "service_ready_ms_max": round(max(start_ms)) if start_ms else None}
    check("P08", f"{a.relogins} simulated relogins: every one restores the window, no ghost service/UI processes, no duplicate windows, at most one UI",
          restored_ok == a.relogins and not ghosts and dups == 0 and uis <= 1,
          f"restored {restored_ok}/{a.relogins}; ghosts {ghosts}; duplicate windows {dups}; UI processes {uis}; service ready p50 {m['relogins']['service_ready_ms_p50']} ms")

    # P09: log out while the login restore is still running, after re-snapping
    # a window the restore already put back. The next login must restore the
    # user's change and the rest of the layout, and repeated interrupted
    # restores must not pause automatic restore (they are not failures).
    quarters = {}
    for q in ("bottom-right-quarter", "top-left-quarter", "top-right-quarter", "bottom-left-quarter"):
        e.cli_json("snap", q, "--window", wids[0])
        quarters[q] = e.frame(wids[0])
    targets = {}
    for w, q in zip(wids, ("top-left-quarter", "top-right-quarter", "bottom-left-quarter")):
        e.cli_json("snap", q, "--window", w)
        targets[w] = e.frame(w)
    e.stop_daemon(signal.SIGTERM)
    xids = {w: str(int(w.split(":")[1], 16)) for w in wids}

    def scatter():
        for j, w in enumerate(wids):
            e.xdo("windowsize", xids[w], "500", "400")
            e.xdo("windowmove", xids[w], str(600 + 40 * j), str(500 + 30 * j))

    mid, user_snaps, misses9 = 0, 0, []
    for k in range(a.midrestore):
        scatter()
        e.env["XDG_SESSION_ID"] = f"soak-mid-{os.getpid()}-{k}"
        e.start_daemon("--autostart")
        hit = e.wait_for(lambda: next((w for w in wids if e2e_x11.rect_eq(e.frame(w), targets[w], 2)), None) if restore_running(e) else None, 15.0, 0.05)
        if hit:
            # move it to the quarter no window occupies (a change the user makes)
            free = next(q for q, r in quarters.items() if not any(e2e_x11.rect_eq(targets[w], r, 2) for w in wids))
            e.cli_json("snap", free, "--window", hit)
            targets[hit] = e.frame(hit)
            user_snaps += 1
        still = restore_running(e)
        mid += bool(hit) and still
        e.stop_daemon(signal.SIGTERM)
    scatter()
    e.env["XDG_SESSION_ID"] = f"soak-mid-{os.getpid()}-final"
    e.start_daemon("--autostart")
    back = e.wait_for(lambda: all(e2e_x11.rect_eq(e.frame(w), targets[w], 2) for w in wids), 20.0, 0.3)
    e.wait_for(lambda: not restore_running(e), 15.0, 0.2)
    if not back:
        misses9 = [f"{w} at {e.frame(w)} want {targets[w]}" for w in wids if not e2e_x11.rect_eq(e.frame(w), targets[w], 2)]
    log = (e.base / "daemon.out").read_text()
    paused = "automatic restore disabled after repeated failures" in log
    stuck = log.count("finished state=cancelled")
    m["mid_restore_logouts"] = {"n": a.midrestore, "during_restore": mid, "user_snaps_kept": user_snaps, "all_back": bool(back), "breaker_tripped": paused,
                                "runs_ended_cancelled": stuck, "misses": misses9, "last_run": restore_summary(e)}
    check("P09", f"{a.midrestore} logouts during the login restore: the next login restores the layout including the user's change, and automatic restore is never paused",
          bool(back) and not paused and mid >= max(1, a.midrestore // 2),
          f"{mid}/{a.midrestore} logouts while the restore ran; {user_snaps} user re-snaps; all back {bool(back)}; breaker tripped {paused}; runs ended cancelled {stuck}; misses {misses9}")
    return m


def cgroup_dir() -> Path | None:
    try:
        rel = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
        return Path("/sys/fs/cgroup" + rel)
    except (OSError, IndexError):
        return None


def test_memory() -> str:
    """Memory of this test's cgroup (every process it started), and the cap."""
    d = cgroup_dir()
    try:
        cur = int((d / "memory.current").read_text()) / 2**20
        mx = (d / "memory.max").read_text().strip()
        cap = "no cap" if mx == "max" else f"cap {int(mx) / 2**20:.0f}"
        # memory.current includes page cache; anon is what processes hold.
        stat = dict(ln.split() for ln in (d / "memory.stat").read_text().splitlines() if ln.count(" ") == 1)
        anon, file_ = int(stat.get("anon", 0)) / 2**20, int(stat.get("file", 0)) / 2**20
        return f"{cur:.0f} MiB ({cap}; anon {anon:.0f}, file {file_:.0f}; {top_processes(d)})"
    except (OSError, ValueError, TypeError):
        return "unknown"


def top_processes(d: Path, n: int = 4) -> str:
    """The test's largest processes by RSS, to say what grows."""
    rows = []
    try:
        pids = [int(p) for p in (d / "cgroup.procs").read_text().split()]
    except (OSError, ValueError):
        return "processes unknown"
    for pid in pids:
        try:
            st = Path(f"/proc/{pid}/status").read_text()
            name = next(ln.split()[1] for ln in st.splitlines() if ln.startswith("Name:"))
            rss = next((int(ln.split()[1]) for ln in st.splitlines() if ln.startswith("VmRSS:")), 0)
            rows.append((rss, name))
        except (OSError, StopIteration, ValueError):
            continue
    rows.sort(reverse=True)
    return ", ".join(f"{name} {rss / 1024:.0f}" for rss, name in rows[:n])


def capped_or_exec(limit: str) -> None:
    """Re-run this script in a systemd user scope with a hard memory cap and
    no swap, so a leak in anything the test starts (the nested shell, Xephyr,
    the UI, the service) is killed at the cap instead of pushing the user's
    desktop into swap. --memory-max none skips this."""
    if limit == "none" or os.environ.get("FRACTILE_SOAK_CAPPED") == limit:
        return
    os.environ["FRACTILE_SOAK_CAPPED"] = limit
    cmd = ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={limit}", "-p", "MemorySwapMax=0",
           sys.executable, *sys.argv]
    try:
        os.execvp(cmd[0], cmd)
    except OSError as ex:
        sys.exit(f"cannot start the memory-capped scope ({ex}); pass --memory-max none to run without a cap")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", type=int, default=23)
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--relogins", type=int, default=10)
    ap.add_argument("--midrestore", type=int, default=6, help="logouts during a running login restore (P09)")
    ap.add_argument("--idle-seconds", type=int, default=60)
    ap.add_argument("--panel-runs", type=int, default=40)
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "perf-x11.json"))
    ap.add_argument("--keep-log", default=None, help="copy the service log here before cleanup")
    ap.add_argument("--memory-max", default="3G",
                    help="hard memory cap (no swap) for the whole test, via a systemd user scope; 'none' for no cap")
    a = ap.parse_args()
    capped_or_exec(a.memory_max)
    print(f"memory: whole test {test_memory()}", flush=True)
    nested = ROOT / "tests" / "integration" / "nested-gnome-x11.sh"
    st = subprocess.run(["bash", str(nested), "start", str(a.display), "1600x1200"], capture_output=True, text=True)
    if st.returncode != 0:
        print(st.stdout, st.stderr)
        return 2
    e = GuiEnv(a.display)
    m: dict = {}
    try:
        m = run_all(e, a)
    except Exception as ex:  # report, don't hide
        check("PXX", "run crashed", False, repr(ex))
    finally:
        if a.keep_log and (e.base / "daemon.out").exists():
            Path(a.keep_log).write_text((e.base / "daemon.out").read_text())
        e.cleanup()
        subprocess.run(["bash", str(nested), "stop", str(a.display)])
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    cpu = next((ln.split(":", 1)[1].strip() for ln in Path("/proc/cpuinfo").read_text().splitlines() if ln.startswith("model name")), "?")
    build = "debug" if "debug" in str(e2e_x11.BIN) else "release"
    ev = {"suite": "perf-x11", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"{os.environ.get('FRACTILE_TEST_XSERVER', 'xephyr').capitalize()} with {gs} (X11) as the window manager, PyQt6 UI; fractiled {build} build; "
                         f"CPU {cpu}, {os.cpu_count()} threads; memory cap {a.memory_max}",
          "metrics": m, "results": results}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
