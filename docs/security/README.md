# Fractile security model

This describes what Fractile protects, from whom, how, and the limits that remain.
It covers Fractile 0.9.0 and is part of the release security review.
The verifying checks are named after each claim (X/W/K/SEC ids, see
[TEST_EVIDENCE.md](../../TEST_EVIDENCE.md)).

## What Fractile can do, and therefore must guard

Fractile can list windows, including their titles and application ids. It can move,
resize, activate and change the state of any window. It can launch applications
from installed `.desktop` entries. On GNOME and KWin it does this through code
that runs inside the compositor. If someone else could drive it, they could read
window titles, rearrange the desktop, or start software.

## Trust boundary

The boundary is **the user's own login session**. Fractile runs only as that
user. It is never root and needs no capabilities, setuid or group membership:
`fractiled` refuses to start as root, as the package tests show. It does not open
`/dev/input`, does not inject input events, and does not screen-scrape.

Processes running as the same user are *not* inside a stronger boundary than
Fractile itself. A same-uid process can already ptrace the user's programs,
read their files and change their autostart entries. Fractile does not claim to
defend against such a process. What it does guarantee:

- other users (including other users' sessions on the same machine) cannot reach it;
- an ordinary same-uid process cannot pose as the compositor integration;
- nothing reaching Fractile can make it run a command that is not an approved
  `.desktop` launch;
- window titles are not written to logs unless the user turns that on, and they
  can be redacted in exports.

## IPC: the local socket

- The socket is `$XDG_RUNTIME_DIR/fractile/fractile.sock`. The directory is
  mode 0700 and the socket 0600 (SEC1).
- Every connection is checked with `SO_PEERCRED`, and a peer with a different
  uid is dropped before reading a byte. The first message must be `hello` with
  the protocol version; anything else is refused (SEC2).
- The protocol is JSON lines with a 1 MiB line limit and typed parameters.
  Nothing is passed to a shell.
- There is no TCP listener and no public D-Bus service with window control. The
  only D-Bus name Fractile owns is `io.fractile.Bridge`, and only on KWin
  sessions (see below).

## Compositor integrations

Session-bus names and socket paths are not access control, since any
same-session process can call a name or connect to a path. Fractile therefore
authenticates the **process** behind each integration connection:

| Integration | Transport | Who may attach | Check |
|---|---|---|---|
| GNOME Shell extension | the local socket (`bridge_attach`) | `gnome-shell` | the peer pid from SO_PEERCRED; `/proc/<pid>/exe` must be a root-owned, not group/world-writable binary named `gnome-shell` (SEC5) |
| KWin script | D-Bus `io.fractile.Bridge1` on the session bus | `kwin_wayland` / `kwin_x11` | the caller's pid from the bus daemon (`GetConnectionUnixProcessID`); same exe rule, or the cap_sys_nice rule below (SEC6) |

Why the executable and not the process name: `/proc/<pid>/comm` can be set by
any process (`PR_SET_NAME`). The executable link cannot be faked without
running the real binary.

**KWin and cap_sys_nice.** Distributions install `kwin_wayland` with the
`cap_sys_nice` file capability. That makes the process non-dumpable, so even its
own user cannot read `/proc/<pid>/exe`. For such a peer, Fractile requires both
the compositor process name *and* `cap_sys_nice` in its permitted capability set.
An ordinary same-uid process can rename itself, but it cannot gain that
capability without executing a binary that grants it. This is weaker than the
exe check: a user who installs another binary with `cap_sys_nice`, named
`kwin_wayland`, could pass. Installing such a binary needs root, which is outside
the threat model.

**Hyprland** is the other way round: there is no integration to attach.
The service is a client of Hyprland's own IPC sockets in
`$XDG_RUNTIME_DIR/hypr/<instance>/` (never the shared `/tmp/hypr` of old
Hyprland versions). It uses an instance only when the directories are real
directories owned by the user and writable by no one else, and checks on
every connection, through `SO_PEERCRED`, that the listening process runs as
the user. Values sent to Hyprland are
embedded in Lua string literals that keep only letters, digits and a few
punctuation characters and escape every other byte, so a shortcut id cannot
end the literal and run code in the compositor (SEC7, unit test
`lua_strings_cannot_be_escaped`). Hotkey events arrive as Hyprland custom
events whose names carry a random per-service part, so binds left by an
earlier service do not trigger actions. Any same-uid process can use these
sockets too; Fractile adds no access beyond what the user already has.

**Sway** works the same way. The service uses `$SWAYSOCK` or this user's
`sway-ipc.<uid>.*.sock` only when it is a socket owned by the user in a
directory private to the user, and checks the listening process's uid with
`SO_PEERCRED` on every connection. Commands contain only numbers, fixed text
and key names limited to letters, digits and `_`, so a shortcut id or a window
title cannot add a Sway command (SEC7, unit tests `ids_are_numbers_only`,
`combos_refuse_sway_syntax_in_key_names`). Shortcut bindings run `nop` with a
token holding a random per-service part, so bindings left by a killed service
do not trigger actions. Fractile reads the Sway configuration files (to find
keys they already bind) and never writes them.

After `Attach`, KWin's `Poll`, `Result` and `Event` calls are accepted only from
the D-Bus connection that attached. A second live integration is refused while
the first one's process exists.

**Version negotiation.** Each integration declares the adapter API it speaks.
A mismatch, for example after a shell or Fractile upgrade, is refused. The reason
and the fix are reported by `fractile status`, `fractile diagnose` and the
Diagnostics page (W23, bridge unit test). Fractile never drives an integration
whose protocol it does not know.

**What the integrations expose.** The GNOME extension and the KWin script accept
calls only from the authenticated Fractile service. They expose no D-Bus methods
and no other entry points of their own. They do not read files and do not
execute anything.

## Launching applications

- Applications are launched only from installed `.desktop` entries, identified by
  desktop id, with argv arrays: `gio launch <entry>`, wrapped in a transient
  `systemd-run --user --scope` when a user manager is available. There is no
  shell interpolation. `Exec` field codes are handled by the desktop-entry rules,
  not by string concatenation.
- Desktop ids are validated: no `/`, no `..`, and only the desktop-id character
  set (SEC4, SEC7).
- The launch policy per app is `always`, `if_missing`, `never` or `ask`, and
  defaults to `ask`. A restore never launches an `ask` app without approval
  (X16, X17).
- **Imports never auto-launch.** Imported snapshots and layouts have every launch
  policy forced to `ask`. Any command or argv field in the file is dropped
  (SEC3, SEC7). A hostile import cannot run anything.
- A snapshot stores the process executable for information only. It is never
  executed.

## Data at rest

- Config is in `~/.config/fractile/config.toml`. Snapshots and layouts are in
  `~/.local/share/fractile/fractile.db` (SQLite, WAL, checksummed payloads, daily
  backup). Logs are in `~/.local/state/fractile/`. All are created with user-only
  permissions.
- Snapshots contain window geometry, app ids, desktop ids, and (by default)
  window titles as matching hints. They never contain document contents,
  clipboard data, passwords, environment variables or command lines. Titles can
  be turned off (`session.store_titles`) and are redacted by `snapshot export
  --redacted`.
- Windows whose titles match `session.private_title_patterns` (private and
  incognito browser windows by default) are left out of snapshots entirely.
- The service log is local only. It mentions window titles in a tagged form, so
  that `fractile logs --redacted` and the diagnostics export reliably remove them
  (X20).
- While the session is locked, Fractile takes no automatic snapshots, does not
  apply on-create rules, refuses restores and ignores its shortcuts (W21). The
  lock state comes from logind's `LockedHint` or from the compositor
  integration.
- No telemetry. Fractile makes no network connections. `diagnose` says so.

## Things Fractile never does silently

- It never changes desktop or tiling settings. When an automatic tiler is active,
  Fractile reports it and leaves the tiler's settings alone. On Hyprland, a
  window the user places becomes floating (the capability report and the
  placement result say so); Fractile's shortcuts are runtime binds and its own
  panel floats through a runtime window rule matching only Fractile's app id.
  None of this is written to the user's configuration, and it is listed in
  docs/compatibility/hyprland.md. Sway is handled the same way (runtime
  `bindsym` and `for_window`, never written to a file; a shortcut on keys the
  user's configuration binds is refused, because a runtime binding would
  replace the user's); see docs/compatibility/sway.md.
- It never enables its GNOME extension or KWin script. The packages ship them
  disabled, and the user enables them.
- It never closes or replaces user windows.
- It never claims to restore unsaved document contents. Restore covers window
  placement, plus relaunching approved apps. Content is restored only if the
  application restores it itself.

## Test seams (not product behaviour)

These environment variables exist for the test harnesses. Each one only
*narrows* or *redirects* what the service talks to, and none grants access:

| Variable | Effect |
|---|---|
| `FRACTILE_RUNTIME_DIR`, `FRACTILE_DATA_DIR`, `FRACTILE_CONFIG`, `FRACTILE_STATE_DIR` | private directories for an isolated test instance |
| `FRACTILE_NO_LOGIND` | do not connect to logind |
| `FRACTILE_TEST_LOGIND_BUS` | read lock state and shutdown signals from a fake logind on a given private bus. Anyone able to set this in the service's environment already controls the user's session |
| `FRACTILE_AUTOSTART_MODE` | force `systemd`, `xdg` or `none` |
| `FRACTILE_UI` | path of the UI program to start |
| `FRACTILE_NO_SCOPE` | launch apps without a systemd scope |

Input events in the test suites (xdotool, XTEST, `tests/tools/wlkeys.py`) are sent
by the harness, never by Fractile.

## Remaining limitations

1. **Same-uid processes.** A same-uid process can connect to the socket and drive
   Fractile, which means it can move windows and launch apps whose policy is
   `always`/`if_missing`. It could already do worse directly. Fractile adds no new
   capability beyond window control that GNOME/KWin otherwise withhold from
   ordinary Wayland clients. Diagnose states this.
2. **Socket squatting.** A same-uid process that starts before `fractiled` could
   bind the socket path first. The second-instance check (X02) detects a live
   socket, but it cannot tell an impostor from a real service. A client that
   connects to an impostor could leak its requests. The UI and CLI send only
   window-control requests. The integrations send window lists to it, which an
   attacker with same-uid access could read from the compositor anyway.
3. **D-Bus name squatting (KWin).** A same-uid process could own
   `io.fractile.Bridge` before `fractiled`. The KWin script would then attach to
   it, and the impostor could drive window placement through the script. The
   script does not verify the name owner. A same-uid attacker can already load
   KWin scripts through KWin's own D-Bus API, so this adds no capability.
4. **The cap_sys_nice rule** is weaker than the executable check (see above).
5. **No sandbox.** The service has no seccomp or landlock profile in 0.9.0. The
   systemd user unit sets `NoNewPrivileges`, `LockPersonality`,
   `RestrictRealtime`, `RestrictSUIDSGID`, `SystemCallArchitectures=native` and
   `UMask=0077`. Under XDG autostart, none of these apply.

## Reporting

Report security issues privately to the maintainers (placeholder:
`maintainers@fractile.invalid` until a project address exists). Do not open
public issues for them.
