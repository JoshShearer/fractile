#!/usr/bin/env python3
"""T14: restore with real browsers and an IDE that restore their own windows.

Firefox, Google Chrome and VS Code reopen their previous windows by
themselves when started. Fractile must launch each of them once (not once
per window), let the app bring its own windows back, place those windows
where they were, and never open extra windows or tabs. It must also say in
the restore report what it did, without claiming to restore the pages,
tabs or unsaved editor content (that is the app's own session restore).

Every app runs with a throwaway profile (--profile / --user-data-dir) on the
nested desktop's private D-Bus, with background networking turned off where
the app allows it. Nothing of the user's own profiles is touched. Desktop
entries with the real desktop ids (firefox.desktop, google-chrome.desktop,
code.desktop) shadow the system ones inside the test's XDG_DATA_HOME only, so
the launch adds the profile arguments.

Usage: tests/integration/e2e_x11_apps.py [--display 25] [--keep] [--apps firefox,chrome,code]
Needs: the e2e_x11.py requirements, and the apps installed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "integration"))
from e2e_x11 import Env, check, rect_eq, results  # noqa: E402

KNOWN: list = []  # known limitations observed, reported in the evidence

def page(base: Path, title: str) -> str:
    """A local page with a known title (browsers block top-level data: URLs)."""
    f = base / "pages" / (title.replace(" ", "-") + ".html")
    f.parent.mkdir(exist_ok=True)
    f.write_text(f"<!doctype html><title>{title}</title><h1>{title}</h1>")
    return f.as_uri()


def firefox_spec(base: Path) -> dict:
    prof = base / "ff-profile"
    prof.mkdir()
    prefs = {
        "browser.startup.page": 3,  # restore the previous session
        "browser.sessionstore.resume_from_crash": True,
        "browser.sessionstore.interval": 1000,
        "browser.shell.checkDefaultBrowser": False,
        "browser.aboutwelcome.enabled": False,
        "trailhead.firstrun.didSeeAboutWelcome": True,
        "browser.startup.homepage_override.mstone": "ignore",
        "startup.homepage_welcome_url": "",
        "startup.homepage_welcome_url.additional": "",
        "datareporting.policy.dataSubmissionEnabled": False,
        "datareporting.healthreport.uploadEnabled": False,
        "toolkit.telemetry.reportingpolicy.firstRun": False,
        "toolkit.telemetry.enabled": False,
        "app.update.auto": False,
        "app.normandy.enabled": False,
        "browser.safebrowsing.malware.enabled": False,
        "browser.safebrowsing.phishing.enabled": False,
        "browser.safebrowsing.downloads.enabled": False,
        "extensions.update.enabled": False,
        "browser.newtabpage.enabled": False,
        "network.captive-portal-service.enabled": False,
        "network.connectivity-service.enabled": False,
        "browser.tabs.warnOnClose": False,
        "browser.warnOnQuit": False,
    }
    (prof / "user.js").write_text("".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n" for k, v in prefs.items()))
    argv = ["firefox", "--profile", str(prof)]
    return {
        "name": "Firefox", "desktop": "firefox.desktop", "wm_class": "firefox",
        "exec": " ".join(argv) + " %u",
        "first": argv + ["--new-window", page(base, "T14 Alpha")],
        "second": argv + ["--new-window", page(base, "T14 Beta")],
        "private": argv + ["--private-window", page(base, "T14 Secret")],
        "titles": ["T14 Alpha", "T14 Beta"],
        "match": lambda w: (w.get("wm_class") or "").lower() == "firefox" or (w.get("app_id") or "").lower() == "firefox",
    }


def chrome_spec(base: Path) -> dict:
    ud = base / "chrome-profile"
    (ud / "Default").mkdir(parents=True)
    (ud / "Default" / "Preferences").write_text(json.dumps({"session": {"restore_on_startup": 1},
                                                             "browser": {"check_default_browser": False, "has_seen_welcome_page": True}}))
    (ud / "First Run").write_text("")
    argv = ["google-chrome", f"--user-data-dir={ud}", "--no-first-run", "--no-default-browser-check", "--password-store=basic",
            "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-features=Translate,MediaRouter"]
    if os.environ.get("FRACTILE_CHROME_NO_SANDBOX"):
        # docker's default seccomp profile blocks the user namespaces Chrome's sandbox needs
        argv.append("--no-sandbox")
    return {
        "name": "Google Chrome", "desktop": "google-chrome.desktop", "wm_class": "google-chrome",
        "exec": " ".join(argv) + " %U",
        "first": argv + ["--new-window", page(base, "T14 Gamma")],
        "second": argv + ["--new-window", page(base, "T14 Delta")],
        "private": argv + ["--incognito", page(base, "T14 Secret")],
        "private_detectable": False,
        "titles": ["T14 Gamma", "T14 Delta"],
        "match": lambda w: (w.get("wm_class") or "").lower() == "google-chrome",
    }


def code_spec(base: Path) -> dict:
    ud, ext = base / "code-user", base / "code-ext"
    (ud / "User").mkdir(parents=True)
    ext.mkdir()
    (ud / "User" / "settings.json").write_text(json.dumps({
        "window.restoreWindows": "all", "telemetry.telemetryLevel": "off", "update.mode": "none",
        "extensions.autoCheckUpdates": False, "workbench.startupEditor": "none", "security.workspace.trust.enabled": False,
        "window.titleBarStyle": "native", "workbench.enableExperiments": False}))
    folders = []
    for n in ("t14-epsilon", "t14-zeta"):
        f = base / n
        f.mkdir()
        (f / "README.md").write_text(f"{n}\n")
        folders.append(f)
    argv = ["code", "--user-data-dir", str(ud), "--extensions-dir", str(ext), "--disable-gpu"]
    return {
        # VS Code ships com.microsoft.VSCode.desktop and sets that WM class
        # (1.10x and later; older builds used code.desktop / "Code").
        "name": "VS Code", "desktop": "com.microsoft.VSCode.desktop", "wm_class": "com.microsoft.VSCode",
        "exec": " ".join(argv) + " %F",
        "first": argv + ["--new-window", str(folders[0])],
        "second": argv + ["--new-window", str(folders[1])],
        "titles": ["t14-epsilon", "t14-zeta"],
        "match": lambda w: (w.get("wm_class") or "").lower() == "com.microsoft.vscode",
    }


SPECS = {"firefox": firefox_spec, "chrome": chrome_spec, "code": code_spec}


def app_pids(e: Env, exe_names: set[str]) -> list[int]:
    """Processes of the app in this test session: its private display in the
    environment, or this run's throwaway profile dir on the command line
    (Chrome's process-title rewriting can clobber /proc/<pid>/environ)."""
    out = []
    mark = str(e.base).encode()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if d.stat().st_uid != os.getuid():
                continue
            comm = (d / "comm").read_text().strip()
            env = (d / "environ").read_bytes()
            cmd = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if comm in exe_names and (f"DISPLAY=:{e.display}".encode() + b"\0" in env or mark in cmd):
            out.append(int(d.name))
    return out


EXE = {"firefox": {"firefox", "firefox-bin"}, "chrome": {"chrome", "google-chrome"}, "code": {"code"}}


def run_app(e: Env, key: str, n: int) -> None:
    spec = SPECS[key](e.base)
    tid = f"A{n:02d}"
    apps = e.base / "xdgdata" / "applications"
    (apps / spec["desktop"]).write_text(
        f"[Desktop Entry]\nType=Application\nName={spec['name']} (test profile)\nExec={spec['exec']}\n"
        f"StartupWMClass={spec['wm_class']}\nTerminal=false\n")
    mine = lambda: [w for w in e.cli_json("list-windows") if spec["match"](w)]  # noqa: E731
    titled = lambda t: next((w for w in mine() if t in (w.get("title") or "")), None)  # noqa: E731
    p1 = subprocess.Popen(spec["first"], env=e.env, stdout=subprocess.DEVNULL, stderr=open(e.base / f"{key}.log", "a"))
    e.procs.append(p1)
    a = e.wait_for(lambda: titled(spec["titles"][0]), 45.0, 0.5)
    p2 = subprocess.Popen(spec["second"], env=e.env, stdout=subprocess.DEVNULL, stderr=open(e.base / f"{key}.log", "a"))
    e.procs.append(p2)
    b = e.wait_for(lambda: titled(spec["titles"][1]), 30.0, 0.5)
    ws = mine()
    if not check(f"{tid}a", f"{spec['name']}: two windows open with a throwaway profile, identified as {spec['desktop']}",
                 bool(a and b) and len(ws) == 2 and all(w.get("desktop_file_id") == spec["desktop"] for w in ws),
                 str([(w.get("title"), w.get("wm_class"), w.get("desktop_file_id")) for w in ws])):
        return
    e.cli_json("place", "--grid", "2x1", "--cells", "0,0,0,0", "--window", a["id"])
    e.cli_json("place", "--grid", "2x1", "--cells", "1,0,1,0", "--window", b["id"])
    time.sleep(1)
    want = {spec["titles"][0]: e.frame(a["id"]), spec["titles"][1]: e.frame(b["id"])}
    time.sleep(3)  # the apps write their own session state periodically
    e.cli_json("save", "--name", f"t14-{key}")
    # Quit the app the way a logout does (SIGTERM to its processes).
    found = app_pids(e, EXE[key])
    for pid in found:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    # What a restore depends on is that the windows closed. Helper processes
    # that ignore SIGTERM get SIGKILL, as the session manager does at logout.
    gone = e.wait_for(lambda: not mine(), 30.0, 0.5)
    e.wait_for(lambda: not app_pids(e, EXE[key]), 15.0, 0.5)
    lingered = []
    for pid in app_pids(e, EXE[key]):
        try:
            lingered.append(b" ".join(Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[:2]).decode(errors="replace")[-60:])
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    e.wait_for(lambda: not app_pids(e, EXE[key]), 10.0, 0.5)
    rp = e.run("--json", "restore", "--snapshot", f"t14-{key}", "--approve-all")
    try:
        rep = json.loads(rp.stdout)
    except ValueError:
        check(f"{tid}b", f"{spec['name']}: restore ran", False, f"exit {rp.returncode}: {rp.stdout[-400:]} {rp.stderr[-400:]}")
        return
    rep["exit"] = rp.returncode
    # The app reopens its windows by itself; give it time, then let the
    # restore finish placing them.
    both = e.wait_for(lambda: titled(spec["titles"][0]) and titled(spec["titles"][1]), 60.0, 0.5)
    e.wait_for(lambda: (e.cli_json("restore-status").get("running") is None), 60.0, 0.5)
    time.sleep(2)
    ws = mine()
    placed = {t: (titled(t) or {}).get("frame") for t in spec["titles"]}
    ok_place = all(placed[t] and rect_eq(placed[t], want[t], 4) for t in spec["titles"])
    runs = e.cli_json("restore-status").get("recent_runs") or []
    last = runs[0] if runs else {}
    check(f"{tid}b", f"{spec['name']}: restore launches it once, it reopens its own two windows, both are placed where they were; no extra windows",
          # how the app quit is setup, reported in the detail, not judged
          bool(both) and len(ws) == 2 and ok_place and rep["report"]["launches"] == 1,
          f"launches={rep['report']['launches']} windows={len(ws)} placed={placed} want={want} state={last.get('state')} windows_closed_on_sigterm={bool(gone)} pids_signalled={len(found)} killed_after_15s={lingered}")
    stem = spec["desktop"].removesuffix(".desktop")
    items = [i for i in rep["report"].get("items", []) if i.get("label", "").split(" #")[0] == stem]
    text = json.dumps(rep).lower()
    check(f"{tid}c", f"{spec['name']}: the report covers both windows and claims nothing about tabs or unsaved content",
          len(items) == 2 and all(i.get("status") == "LAUNCHED_AND_PLACED" for i in items) and "unsaved" not in text and "tabs restored" not in text,
          json.dumps([{k: i.get(k) for k in ("label", "status", "launched", "message")} for i in items])[:400])
    rep2 = e.cli_json("restore", "--snapshot", f"t14-{key}", "--approve-all")
    time.sleep(3)
    check(f"{tid}d", f"{spec['name']}: restoring again launches nothing and opens nothing new",
          rep2["report"]["launches"] == 0 and len(mine()) == 2, f"launches={rep2['report']['launches']} windows={len(mine())}")
    if spec.get("private"):
        # RS-20: a real private / incognito window is left out of snapshots.
        p3 = subprocess.Popen(spec["private"], env=e.env, stdout=subprocess.DEVNULL, stderr=open(e.base / f"{key}.log", "a"))
        e.procs.append(p3)
        priv = e.wait_for(lambda: titled("T14 Secret"), 30.0, 0.5)
        e.cli_json("save", "--name", f"t14-{key}-private")
        snap = e.cli_json("snapshot", "export", f"t14-{key}-private")
        recs = [r for r in snap["windows"] if r.get("desktop_file_id") == spec["desktop"]]
        hints = [r.get("title_hint") for r in recs]
        left_out = bool(priv) and len(mine()) == 3 and len(recs) == 2 and not any("Secret" in (h or "") for h in hints)
        detail = f"private window title {(priv or {}).get('title')!r}; open {len(mine())}; recorded {len(recs)}"
        if spec.get("private_detectable", True):
            check(f"{tid}e", f"{spec['name']}: a private window is left out of the snapshot; the two normal windows are recorded", left_out, detail)
        else:
            # Documented limitation (docs/user-guide/README.md, "Private windows"): nothing in the
            # title or the X11 properties marks this app's private windows, so
            # Fractile records them like normal windows. Reported, not passed.
            KNOWN.append({"id": f"{tid}e", "name": f"{spec['name']}: private windows cannot be detected and are recorded", "left_out": left_out, "detail": detail})
            print(f"KNOWN {tid}e {spec['name']}: private window not detectable :: {detail}")
            # What Fractile does instead: it never stores this browser's
            # titles (session.titles_never_stored_for), so nothing from the
            # private window's content reaches the snapshot.
            check(f"{tid}f", f"{spec['name']}: no title of this browser is stored, so the private window's title is not in the snapshot",
                  bool(priv) and len(recs) >= 2 and all(h is None for h in hints) and "Secret" not in json.dumps(snap),
                  f"recorded {len(recs)}; title hints {hints}")
    for pid in app_pids(e, EXE[key]):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    e.wait_for(lambda: not app_pids(e, EXE[key]), 20.0, 0.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", type=int, default=25)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--apps", default=os.environ.get("FRACTILE_TEST_APPS", "firefox,chrome,code"))
    ap.add_argument("--log-dir", help="copy the service and app logs here before cleanup")
    ap.add_argument("--evidence", default=str(ROOT / "tests" / "evidence" / "e2e-x11-apps.json"))
    a = ap.parse_args()
    nested = ROOT / "tests" / "integration" / "nested-gnome-x11.sh"
    st = subprocess.run(["bash", str(nested), "start", str(a.display), "1600x1200"], capture_output=True, text=True)
    if st.returncode != 0:
        print(st.stdout, st.stderr)
        return 2
    info = dict(kv.split("=", 1) for kv in st.stdout.strip().splitlines()[-1].split())
    e = Env(a.display)
    # The apps use the nested session's private bus, not the user's.
    e.env["DBUS_SESSION_BUS_ADDRESS"] = info["DBUS_SESSION_BUS_ADDRESS"]
    e.env["HOME"] = str(e.base / "home")  # nothing lands in the real home either
    (e.base / "home").mkdir()
    versions = {}
    try:
        e.start_daemon()
        for n, key in enumerate(a.apps.split(","), 1):
            exe = {"firefox": "firefox", "chrome": "google-chrome", "code": "code"}[key]
            if not shutil.which(exe):
                check(f"A{n:02d}a", f"{key} installed", False, "not installed")
                continue
            versions[key] = subprocess.run([exe, "--version"], capture_output=True, text=True, env=e.env, timeout=60).stdout.strip().splitlines()[0]
            try:
                run_app(e, key, n)
            except Exception as ex:  # noqa: BLE001
                check(f"A{n:02d}X", f"{key} part crashed", False, repr(ex))
    finally:
        if a.log_dir:
            Path(a.log_dir).mkdir(parents=True, exist_ok=True)
            for f in e.base.glob("*.log"):
                shutil.copy(f, a.log_dir)
            e.stop_daemon()
            if (e.base / "daemon.out").exists():
                shutil.copy(e.base / "daemon.out", a.log_dir)
        e.cleanup()
        if not a.keep:
            subprocess.run(["bash", str(nested), "stop", str(a.display)])
    gs = subprocess.run(["gnome-shell", "--version"], capture_output=True, text=True).stdout.strip()
    ev = {"suite": "e2e-x11-apps", "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": f"Xephyr nested {gs} (X11); throwaway profiles; {versions}", "results": results, "known_limitations": KNOWN}
    Path(a.evidence).write_text(json.dumps(ev, indent=2) + "\n")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; evidence: {a.evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
