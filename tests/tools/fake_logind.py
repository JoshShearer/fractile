#!/usr/bin/env python3
"""Test tool only: a minimal org.freedesktop.login1 on a *private* session
bus, so E2E suites can drive the lock state and shutdown signal that a real
logind delivers. Point fractiled at it with FRACTILE_TEST_LOGIND_BUS.

Implements what fractiled uses:
  Manager: Inhibit(ssss) -> h, GetSession(s) -> o, GetSessionByPID(u) -> o,
           GetUser(u) -> o, signal PrepareForShutdown(b)
  Session: properties Id (s), LockedHint (b); SetLockedHint(b)  (as the real logind)
  User:    property Display (so), the user's graphical session
Test control (io.fractile.test.FakeLogind on /):
  EmitPrepareForShutdown(b), InhibitorCount() -> u

Usage: DBUS_SESSION_BUS_ADDRESS=<private bus> fake_logind.py
Prints "ready" once the name is owned.
"""
import os
import sys

from gi.repository import Gio, GLib

SESSION_PATH = "/org/freedesktop/login1/session/test"
SESSION_ID = "test"
USER_PATH = "/org/freedesktop/login1/user/_test"
XML = """
<node>
  <interface name="org.freedesktop.login1.Manager">
    <method name="Inhibit"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="h" direction="out"/></method>
    <method name="GetSession"><arg type="s" direction="in"/><arg type="o" direction="out"/></method>
    <method name="GetSessionByPID"><arg type="u" direction="in"/><arg type="o" direction="out"/></method>
    <method name="GetUser"><arg type="u" direction="in"/><arg type="o" direction="out"/></method>
    <signal name="PrepareForShutdown"><arg type="b"/></signal>
  </interface>
  <interface name="org.freedesktop.login1.Session">
    <property name="Id" type="s" access="read"/>
    <property name="LockedHint" type="b" access="read"/>
    <method name="SetLockedHint"><arg type="b" direction="in"/></method>
  </interface>
  <interface name="org.freedesktop.login1.User">
    <property name="Display" type="(so)" access="read"/>
  </interface>
  <interface name="io.fractile.test.FakeLogind">
    <method name="EmitPrepareForShutdown"><arg type="b" direction="in"/></method>
    <method name="InhibitorCount"><arg type="u" direction="out"/></method>
  </interface>
</node>
"""
node = Gio.DBusNodeInfo.new_for_xml(XML)
state = {"locked": False, "inhibitors": []}


def alive_inhibitors():
    keep = []
    for w in state["inhibitors"]:
        # We keep the write end; the holder closing its read end releases
        # the lock, as with logind. A write then fails with EPIPE.
        try:
            os.write(w, b"x")
            keep.append(w)
        except OSError:
            os.close(w)
    state["inhibitors"] = keep
    return len(keep)


def on_call(conn, sender, path, iface, method, params, inv):
    if method == "Inhibit":
        r, w = os.pipe()
        fds = Gio.UnixFDList.new()
        idx = fds.append(r)
        os.close(r)
        state["inhibitors"].append(w)
        inv.return_value_with_unix_fd_list(GLib.Variant("(h)", (idx,)), fds)
    elif method in ("GetSession", "GetSessionByPID"):
        inv.return_value(GLib.Variant("(o)", (SESSION_PATH,)))
    elif method == "GetUser":
        inv.return_value(GLib.Variant("(o)", (USER_PATH,)))
    elif method == "SetLockedHint":
        state["locked"] = params.unpack()[0]
        conn.emit_signal(None, SESSION_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                         GLib.Variant("(sa{sv}as)", ("org.freedesktop.login1.Session", {"LockedHint": GLib.Variant("b", state["locked"])}, [])))
        inv.return_value(None)
    elif method == "EmitPrepareForShutdown":
        conn.emit_signal(None, "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "PrepareForShutdown", GLib.Variant("(b)", params.unpack()))
        inv.return_value(None)
    elif method == "InhibitorCount":
        inv.return_value(GLib.Variant("(u)", (alive_inhibitors(),)))
    else:
        inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)


def on_get(conn, sender, path, iface, prop):
    if prop == "LockedHint":
        return GLib.Variant("b", state["locked"])
    if prop == "Id":
        return GLib.Variant("s", SESSION_ID)
    if prop == "Display":
        return GLib.Variant("(so)", (SESSION_ID, SESSION_PATH))
    return None


def on_bus(conn, name):
    for i in node.interfaces:
        p = {"org.freedesktop.login1.Session": SESSION_PATH, "org.freedesktop.login1.User": USER_PATH}.get(
            i.name, "/" if i.name.startswith("io.") else "/org/freedesktop/login1")
        conn.register_object(p, i, on_call, on_get, None)


def on_name(conn, name):
    print("ready", flush=True)


def on_lost(conn, name):
    print("could not own org.freedesktop.login1", file=sys.stderr)
    sys.exit(1)


Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.login1", Gio.BusNameOwnerFlags.NONE, on_bus, on_name, on_lost)
GLib.MainLoop().run()
