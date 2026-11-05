"""
core/gui_call.py — run a function on the Qt GUI thread and wait for its result.

Qt widgets may only be touched from the GUI thread. Worker threads (tool handlers, background
loops) call `on_gui(fn)`; `fn` runs on the GUI thread and its return value (or exception) comes
back. The @pyqtSlot decorator is what makes Qt queue the call onto the thread the Bridge lives
on; without it PyQt would run the slot on whichever thread connected it.
"""
from __future__ import annotations

import threading
from typing import Callable

_bridge = None
_lock = threading.Lock()


def _get():
    global _bridge
    with _lock:
        if _bridge is not None:
            return _bridge
        from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return None

        class Bridge(QObject):
            run_sig = pyqtSignal(object)

            @pyqtSlot(object)
            def _run(self, job):
                try:
                    job["result"] = job["fn"]()
                except Exception as e:                        # noqa: BLE001
                    job["error"] = e
                finally:
                    job["done"].set()

        b = Bridge()
        b.moveToThread(app.thread())
        b.run_sig.connect(b._run)
        _bridge = b
        return b


def on_gui(fn: Callable, timeout: float = 8.0):
    b = _get()
    if b is None:
        raise RuntimeError("no screen to draw on")
    job = {"fn": fn, "done": threading.Event()}
    b.run_sig.emit(job)
    if not job["done"].wait(timeout):
        raise RuntimeError("the screen did not answer in time")
    if "error" in job:
        raise job["error"]
    return job.get("result")
