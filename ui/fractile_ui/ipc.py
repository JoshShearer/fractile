"""Blocking JSON-lines client for the Fractile service socket.

Used by the UI (on a worker thread) and by tests. No third-party deps.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

PROTOCOL = 1
MAX_LINE = 1 << 20


def runtime_dir() -> Path:
    if os.environ.get("FRACTILE_RUNTIME_DIR"):
        return Path(os.environ["FRACTILE_RUNTIME_DIR"])
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "fractile"


def socket_path() -> Path:
    return runtime_dir() / "fractile.sock"


class RpcError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Client:
    """One connection. `call` is thread-safe; events are delivered to
    `on_event` from the reader thread once `start_events()` is called."""

    def __init__(self, name: str = "py", path: Path | None = None, timeout: float = 120.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(path or socket_path()))
        except OSError as e:
            raise RpcError("not_running", f"cannot connect to Fractile service: {e}") from e
        self._buf = b""
        self._next = 1
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._cv = threading.Condition()
        self._reader: threading.Thread | None = None
        self.on_event = None
        self.closed = False
        self.server_info = self.call("hello", {"protocol": PROTOCOL, "client": name})

    # -- framing --
    def _read_line(self) -> bytes | None:
        while b"\n" not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self._buf += chunk
            if len(self._buf) > MAX_LINE + 1 and b"\n" not in self._buf:
                raise RpcError("protocol", "message too large")
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def _send(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        with self._lock:
            self.sock.sendall(data)

    def call(self, method: str, params: dict | None = None):
        with self._lock:
            rid = self._next
            self._next += 1
        self._send({"id": rid, "method": method, "params": params or {}})
        if self._reader is None:
            while True:
                line = self._read_line()
                if line is None:
                    raise RpcError("io", "connection closed")
                msg = json.loads(line)
                if "event" in msg:
                    if self.on_event:
                        self.on_event(msg["event"], msg.get("data"))
                    continue
                if msg.get("id") == rid:
                    return self._result(msg)
        with self._cv:
            while rid not in self._pending:
                if self.closed:
                    raise RpcError("io", "connection closed")
                if not self._cv.wait(timeout=self.sock.gettimeout()):
                    raise RpcError("timeout", f"{method} timed out")
            return self._result(self._pending.pop(rid))

    @staticmethod
    def _result(msg: dict):
        if msg.get("error"):
            e = msg["error"]
            raise RpcError(e.get("code", "error"), e.get("message", "error"))
        return msg.get("result")

    def start_events(self, kind: str = "client") -> None:
        """Subscribe and switch to a background reader thread."""
        self.call("subscribe", {"kind": kind})
        self.sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop, name="fractile-ipc", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._read_line()
                if line is None:
                    break
                msg = json.loads(line)
                if "event" in msg:
                    if self.on_event:
                        try:
                            self.on_event(msg["event"], msg.get("data"))
                        except Exception:  # never kill the reader
                            pass
                else:
                    with self._cv:
                        self._pending[msg.get("id", 0)] = msg
                        self._cv.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            self.closed = True
            with self._cv:
                self._cv.notify_all()
            if self.on_event:
                try:
                    self.on_event("disconnected", {})
                except Exception:
                    pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass
