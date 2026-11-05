"""
core/screen_sight.py — keep the assistant looking at the screen, from the moment it is told to
until it is told to stop.

WHAT IT DOES
    While ON, a small picture of the screen is sent to the live Gemini session every few seconds,
    so "what is this error?" or "what am I looking at?" is answered from what is on screen NOW,
    without anyone asking for a screenshot first. Nothing is sent when nothing has changed (apart
    from an occasional refresh), so a still screen costs almost nothing.

    That is a lot to hand over, so it is built to be seen and stopped:
      * A red badge stays on the window the whole time. Click it and sight stops.
      * "Stop looking" by voice, an optional timer, or just closing the app (it starts OFF
        every launch unless the user explicitly asked for it to persist).
      * It never sends frames while the assistant is asleep (wake-word mode), while the session
        is down, or while the audio queue is busy.
      * Windows whose title looks private are NOT sent: password managers, private/incognito
        windows, banking, sign-in pages. The user can add their own words.

WHAT IT IS NOT
    It does not record or save anything. Frames go to the live session and are discarded here.
    A window title is only a hint: a page can be sensitive without saying so in its title, which
    is why the badge and the off switch matter more than the filter.

HOW FRAMES REACH GEMINI
    `player.send_media(jpeg_bytes, "image/jpeg")` (added to main.py by the installer) puts the
    frame on the same outgoing queue as the microphone, and _send_realtime forwards it as video.
"""
from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

_BASE = Path(__file__).resolve().parent.parent
_CFG = _BASE / "config" / "screen_sight.json"

DEFAULT_PRIVATE = ("password", "passwd", "1password", "bitwarden", "keepass", "lastpass", "dashlane", "authenticator",
                   "incognito", "inprivate", "private browsing", "private window", "bank", "paypal", "sign in", "log in", "login")
MAX_WIDTH = 1280
CHANGE_THRESHOLD = 2.0          # mean difference (0-255) between 64x36 thumbnails that counts as "the screen changed"


def _default_capture():
    import pyautogui
    return pyautogui.screenshot()


def _default_title() -> str:
    import sys
    if sys.platform != "win32":
        return ""
    import ctypes
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    n = u.GetWindowTextLengthW(h)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(h, buf, n + 1)
    return buf.value


class ScreenSight:
    def __init__(self, capture: Callable = _default_capture, fg_title: Callable = _default_title, clock: Callable = time.monotonic):
        self._capture, self._title, self._clock = capture, fg_title, clock
        self.player = None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.running = False
        self.interval = 3.0
        self.keepalive = 30.0
        self.extra_words: list[str] = []
        self.until: Optional[float] = None
        self.persist = False
        self.started_at = 0.0
        self.sent = self.skipped_private = self.skipped_static = self.dropped = 0
        self.last_blocked = ""
        self._last_thumb = None
        self._last_sent = 0.0
        self._load()

    # ---- settings --------------------------------------------------------------
    def _load(self) -> None:
        try:
            c = json.loads(_CFG.read_text(encoding="utf-8"))
            self.persist = bool(c.get("persist"))
            self.interval = float(c.get("interval", self.interval))
            self.extra_words = [str(w) for w in c.get("exclude", [])]
        except Exception:                                     # noqa: BLE001
            pass

    def _save(self) -> None:
        try:
            _CFG.parent.mkdir(parents=True, exist_ok=True)
            _CFG.write_text(json.dumps({"persist": self.persist, "interval": self.interval, "exclude": self.extra_words}), encoding="utf-8")
        except Exception:                                     # noqa: BLE001
            pass

    def words(self) -> list[str]:
        return list(DEFAULT_PRIVATE) + [w.lower() for w in self.extra_words]

    def is_private(self, title: str) -> bool:
        t = (title or "").lower()
        return any(w and w in t for w in self.words())

    # ---- control ------------------------------------------------------------------------
    def start(self, minutes: float = 0, interval: Optional[float] = None, exclude: Optional[list] = None, persist: bool = False) -> None:
        with self._lock:
            if self.running:
                self.stop("restarting", tell=False, _quiet=True)
            if interval:
                self.interval = max(1.0, min(30.0, float(interval)))
            if exclude:
                self.extra_words = sorted({*self.extra_words, *[str(w).strip().lower() for w in exclude if str(w).strip()]})
            self.persist = bool(persist)
            self._save()
            self.until = self._clock() + minutes * 60 if minutes else None
            self.started_at = self._clock()
            self.sent = self.skipped_private = self.skipped_static = self.dropped = 0
            self.last_blocked, self._last_thumb, self._last_sent = "", None, 0.0
            self._stop.clear()
            self.running = True
            self._thread = threading.Thread(target=self._run, daemon=True, name="screen-sight")
            self._thread.start()
        _badge(self.player, True, self)

    def stop(self, reason: str = "", tell: bool = False, _quiet: bool = False) -> bool:
        with self._lock:
            was = self.running
            self.running = False
            self._stop.set()
            if was and not _quiet:
                self.persist = False
                self._save()
        if was and not _quiet:
            _badge(self.player, False, self)
            if tell and self.player is not None and hasattr(self.player, "request_say"):
                try:
                    self.player.request_say(f"[SCREEN SIGHT] Screen sight has been switched off ({reason or 'stopped'}). "
                                            "Tell the user in one short sentence that you can no longer see their screen.")
                except Exception:                             # noqa: BLE001
                    pass
        return was

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            return {"on": self.running, "seconds_on": int(now - self.started_at) if self.running else 0, "frames_sent": self.sent,
                    "skipped_private": self.skipped_private, "skipped_unchanged": self.skipped_static, "dropped": self.dropped,
                    "minutes_left": round((self.until - now) / 60, 1) if (self.running and self.until) else None,
                    "interval_s": self.interval, "last_private_window": self.last_blocked, "persist": self.persist,
                    "extra_private_words": list(self.extra_words)}

    # ---- the loop ----------------------------------------------------------------------------
    @staticmethod
    def _thumb(img):
        import numpy as np
        return np.asarray(img.convert("L").resize((64, 36)), dtype=np.int16)

    @staticmethod
    def _jpeg(img) -> bytes:
        if img.width > MAX_WIDTH:
            img = img.resize((MAX_WIDTH, round(img.height * MAX_WIDTH / img.width)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=55)
        return buf.getvalue()

    def tick(self) -> str:
        """One look. Returns what happened: 'sent', 'private', 'unchanged', 'dropped', 'nocapture', 'expired'."""
        now = self._clock()
        if self.until and now >= self.until:
            self.stop("the time you set ran out", tell=True)
            return "expired"
        title = ""
        try:
            title = self._title()
        except Exception:                                     # noqa: BLE001
            pass
        if self.is_private(title):
            self.skipped_private += 1
            self.last_blocked = title[:60]
            self._last_thumb = None                           # show the screen afresh once it is safe again
            return "private"
        try:
            img = self._capture()
        except Exception:                                     # noqa: BLE001
            img = None
        if img is None:
            return "nocapture"
        import numpy as np
        th = self._thumb(img)
        changed = self._last_thumb is None or float(np.abs(th - self._last_thumb).mean()) > CHANGE_THRESHOLD
        if not changed and now - self._last_sent < self.keepalive:
            self.skipped_static += 1
            return "unchanged"
        send = getattr(self.player, "send_media", None)
        ok = bool(send and send(self._jpeg(img), "image/jpeg"))
        if ok:
            self.sent += 1
            self._last_thumb, self._last_sent = th, now
            return "sent"
        self.dropped += 1
        return "dropped"

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:                            # noqa: BLE001
                print(f"[ScreenSight] a look failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  The badge: always visible while sight is on, and the kill switch
# ─────────────────────────────────────────────────────────────────────────────
def _badge(player, on: bool, engine) -> None:
    if player is None:
        return
    try:
        from core import gui_call
        gui_call.on_gui(lambda: _badge_set(player, on), timeout=3)
    except Exception:                                         # noqa: BLE001
        pass


def _badge_set(player, on: bool) -> None:
    from PyQt6.QtCore import QEvent, QObject, Qt
    from PyQt6.QtWidgets import QLabel

    def place(b):
        b.adjustSize()
        b.move(max(8, player.width() - b.width() - 16), 8)

    b = getattr(player, "_sight_badge", None)
    if on:
        if b is None:
            b = QLabel("●  SCREEN SIGHT ON  ·  click to stop", player)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet("background:#3a0a14;color:#ffd6dc;border:1px solid #ff3355;border-radius:11px;"
                            "padding:3px 12px;font:bold 9pt 'Segoe UI';")
            b.mousePressEvent = lambda ev: engine().stop("you clicked the badge", tell=True)

            class Follow(QObject):
                def eventFilter(self, obj, ev):
                    if ev.type() == QEvent.Type.Resize:
                        place(b)
                    return False
            f = Follow(player)
            player.installEventFilter(f)
            player._sight_badge, player._sight_follow = b, f
        place(b)
        b.show()
        b.raise_()
    elif b is not None:
        b.hide()


# ─────────────────────────────────────────────────────────────────────────────
_engine: Optional[ScreenSight] = None
_elock = threading.Lock()


def engine() -> ScreenSight:
    global _engine
    with _elock:
        if _engine is None:
            _engine = ScreenSight()
        return _engine


def bind(player) -> None:
    """Called once by main.py when the window exists. Resumes only if the user asked for persistence."""
    e = engine()
    e.player = player
    if e.persist and not e.running:
        e.start(interval=e.interval, persist=True)


def stop(reason: str = "", tell: bool = False) -> bool:
    return engine().stop(reason, tell=tell)
