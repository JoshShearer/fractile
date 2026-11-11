"""Thread-safe bridge from the IPC client to Qt signals."""
from __future__ import annotations

import threading
import time

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal

from .ipc import Client, RpcError


class _Job(QRunnable):
    def __init__(self, fn, ok, fail):
        super().__init__()
        self.fn, self.ok, self.fail = fn, ok, fail

    def run(self):
        try:
            r = self.fn()
        except RpcError as e:
            self.fail.emit(e.code, e.message)
            return
        except Exception as e:  # noqa: BLE001
            self.fail.emit("error", str(e))
            return
        self.ok.emit(r)


class _Result(QObject):
    ok = pyqtSignal(object)
    fail = pyqtSignal(str, str)


class Bridge(QObject):
    event = pyqtSignal(str, object)
    connected = pyqtSignal(dict)
    disconnected = pyqtSignal()

    def __init__(self, name: str = "ui"):
        super().__init__()
        self.name = name
        self.client: Client | None = None
        self.calls: Client | None = None
        self._stop = False
        self._lock = threading.Lock()
        self._results: list[_Result] = []

    # The event connection is separate from the call connection so a long
    # blocking call never delays event delivery.
    def connect_service(self, kind: str = "ui") -> bool:
        try:
            ev = Client(self.name + "-events")
            ev.on_event = lambda e, d: self.event.emit(e, d)
            calls = Client(self.name)
        except RpcError:
            return False
        ev.start_events(kind)
        self.client, self.calls = ev, calls
        self.connected.emit(calls.server_info or {})
        return True

    def keep_connected(self, kind: str = "ui") -> None:
        """Reconnect in the background whenever the service restarts."""
        def loop():
            while not self._stop:
                if self.client is None or self.client.closed:
                    was = self.client is not None
                    self.client = None
                    if was:
                        self.disconnected.emit()
                    if not self.connect_service(kind):
                        time.sleep(1.0)
                        continue
                time.sleep(0.5)
        threading.Thread(target=loop, name="fractile-reconnect", daemon=True).start()

    def call(self, method: str, params: dict | None = None):
        """Blocking call (use for quick requests only)."""
        with self._lock:
            c = self.calls
        if c is None or c.closed:
            raise RpcError("not_running", "Fractile service is not running")
        return c.call(method, params or {})

    def try_call(self, method: str, params: dict | None = None, default=None):
        try:
            return self.call(method, params)
        except RpcError:
            return default

    def call_async(self, method: str, params: dict | None = None, ok=None, fail=None) -> None:
        """Run a call on the thread pool; callbacks run on the GUI thread."""
        res = _Result()
        self._results.append(res)

        def done(_=None):
            if res in self._results:
                self._results.remove(res)

        if ok:
            res.ok.connect(ok)
        if fail:
            res.fail.connect(fail)
        res.ok.connect(done)
        res.fail.connect(done)
        # A dedicated short-lived connection: long calls (restore) must not
        # block quick UI calls on the shared one.
        def fn():
            c = Client(self.name + "-job", timeout=900)
            try:
                return c.call(method, params or {})
            finally:
                c.close()
        QThreadPool.globalInstance().start(_Job(fn, res.ok, res.fail))

    def stop(self) -> None:
        self._stop = True
        for c in (self.client, self.calls):
            if c:
                c.close()
