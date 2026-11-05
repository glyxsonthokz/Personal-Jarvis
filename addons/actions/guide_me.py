"""
actions/guide_me.py — "show me": walk the user through a task by POINTING at it.

WHAT IT DOES
    When the user asks how to do something on their own computer, the assistant
    no longer recites a list. It works one step at a time:

      1. finds the thing on the screen (a screenshot and a vision model),
      2. draws a glowing highlight, an arrow and a short caption on it, and
         glides the real cursor over to it,
      3. WAITS — the highlight stays exactly where it is until the user clicks
         it (or says "next"),
      4. if the step needs text, types it for them the moment they have clicked
         into the field,
      5. tells the model the step is done, so it can point at the next one.

    The tool answers the moment it is called (`status=started`) and reports the
    ending through `player.request_say(...)`, which is the contract
    core/outcome.py describes for anything that takes longer than a breath.

WHY NOTHING IS TYPED UNTIL THE USER HAS CLICKED
    Typing into whatever happens to have focus is how text lands in the wrong
    window. The click is the proof that the field in front of the pointer is the
    one that has focus, so the typing waits for it.

WHAT IT REFUSES
    It never types into a field that looks like a password, PIN, card number or
    other secret. It still points at the field — the user types that part.

PLATFORM NOTES
    Windows: click detection and Unicode typing use the Win32 API directly, so
    nothing extra is installed and the clipboard is never touched.
    macOS / Linux: click detection needs `pynput` (in requirements.txt). Without
    it — or on Wayland, which does not let any program watch the mouse — the
    pointer still appears and the user says "next" to move on. The tool tells
    the model which of the two it got, so it never claims to be watching for a
    click it cannot see.
"""
from __future__ import annotations

import io
import math
import re
import sys
import threading
import time
from typing import Optional

from core import outcome

_WIN = sys.platform == "win32"
_MAC = sys.platform == "darwin"

_TIMEOUT_S = 15 * 60       # a step nobody acts on for this long is let go
_CLICK_PAD = 14            # px of slack around the target; nobody clicks dead centre
_MIN_BOX = 44              # a target smaller than this is drawn this big
_MAX_TYPE = 400            # characters the assistant will type on someone's behalf
_DEFAULT_ACCENT = "#00d4ff"

# A field that holds a secret is the user's to fill in. Checked against what the
# step says it is pointing at, because that is all there is to go on.
_SENSITIVE = re.compile(
    r"pass(word|code|phrase)|\bpin\b|cvv|cvc|card\s*(number|no)|\bssn\b|secret|"
    r"token|api[\s_-]*key|private\s*key|recovery\s*(code|phrase)|2fa|\botp\b|"
    r"security\s*code|şifre|parola|contraseña|mot de passe|passwort",
    re.I,
)


# ─────────────────────────────────────────────────────────────────────────────
#  State — one walkthrough step is live at a time
# ─────────────────────────────────────────────────────────────────────────────
class _Step:
    def __init__(self, n: int, label: str, target: str, bbox, type_text: str,
                 press_enter: bool, wait_for: str, plan: str):
        self.n = n
        self.label = label
        self.target = target
        self.bbox = bbox                   # (x1, y1, x2, y2) in screen pixels, or None
        self.type_text = type_text
        self.press_enter = press_enter
        self.wait_for = wait_for           # "click" | "say_done"
        self.plan = plan
        self.advance = threading.Event()   # the user said "next"
        self.cancel = threading.Event()    # replaced, finished or stopped


_lock = threading.Lock()
_current: Optional[_Step] = None
_counter = 0
_plan = ""


def _say(player, instruction: str) -> None:
    """Tell the model something happened. Never raises."""
    if player is not None and hasattr(player, "request_say"):
        try:
            player.request_say(instruction)
        except Exception:                                     # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  The highlight — a click-through, always-on-top Qt overlay
# ─────────────────────────────────────────────────────────────────────────────
_QT: dict = {}
_QT_LOCK = threading.Lock()


def _accent() -> str:
    try:
        from PyQt6.QtGui import QColor
        from memory.config_manager import load_api_keys
        value = str(load_api_keys().get("ui_color", "") or "").strip()
        if value and QColor.isValidColor(value):
            return value
    except Exception:                                         # noqa: BLE001
        pass
    return _DEFAULT_ACCENT


def _qt() -> dict:
    """Build the Qt classes on first use. PyQt is not imported at launch, so a
    session that never asks for a walkthrough pays nothing for this file."""
    if _QT:
        return _QT
    from PyQt6.QtCore import QObject, QPointF, QRect, QRectF, Qt, QTimer, pyqtSignal, pyqtSlot
    from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
    from PyQt6.QtWidgets import QWidget

    class Overlay(QWidget):
        def __init__(self, box, text: str, accent: str):
            super().__init__(None)
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
                | Qt.WindowType.WindowTransparentForInput
                | Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

            screen = QGuiApplication.primaryScreen()
            geo = screen.geometry()
            dpr = screen.devicePixelRatio() or 1.0
            # One pixel short of the full height: a window that exactly covers
            # the screen is treated as "fullscreen" by some systems, which can
            # hide the taskbar or knock a game out of its own fullscreen mode.
            self.setGeometry(geo.x(), geo.y(), geo.width(), geo.height() - 1)

            x1, y1, x2, y2 = box
            pad = 8
            self._rect = QRectF(
                x1 / dpr - geo.x() - pad, y1 / dpr - geo.y() - pad,
                (x2 - x1) / dpr + 2 * pad, (y2 - y1) / dpr + 2 * pad,
            )
            self._text = text
            self._color = QColor(accent if QColor.isValidColor(accent) else _DEFAULT_ACCENT)
            self._t0 = time.monotonic()
            self._timer = QTimer(self)
            self._timer.setInterval(33)
            self._timer.timeout.connect(self.update)
            self._timer.start()

        # --- drawing -------------------------------------------------------
        def _bubble_origin(self, bw: float, bh: float):
            """Where the caption goes: right of the target, else left, else below."""
            r, W, H = self._rect, self.width(), self.height()
            gap = 38
            clamp = lambda v, lo, hi: max(lo, min(hi, v))          # noqa: E731
            if r.right() + gap + bw <= W - 10:
                return r.right() + gap, clamp(r.center().y() - bh / 2, 10, H - bh - 10), "right"
            if r.left() - gap - bw >= 10:
                return r.left() - gap - bw, clamp(r.center().y() - bh / 2, 10, H - bh - 10), "left"
            if r.bottom() + gap + bh <= H - 10:
                return clamp(r.center().x() - bw / 2, 10, W - bw - 10), r.bottom() + gap, "below"
            return clamp(r.center().x() - bw / 2, 10, W - bw - 10), max(10, r.top() - gap - bh), "above"

        def paintEvent(self, _event):
            c = self._color
            t = time.monotonic() - self._t0
            beat = (math.sin(t * 5.0) + 1.0) / 2.0
            ripple = (t * 1.1) % 1.0
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            # Highlight: three strokes make a glow without a blur filter.
            box = self._rect.adjusted(-beat * 3, -beat * 3, beat * 3, beat * 3)
            p.setBrush(Qt.BrushStyle.NoBrush)
            for width, alpha in ((13, 35), (7, 85), (3, 240)):
                p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), alpha), width))
                p.drawRoundedRect(box, 10, 10)
            # A ring that spreads outwards and fades — the eye goes to motion.
            grow = ripple * 28
            p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), int(150 * (1 - ripple))), 2))
            p.drawRoundedRect(self._rect.adjusted(-grow, -grow, grow, grow), 12 + grow / 2, 12 + grow / 2)

            if not self._text:
                return
            font = QFont()
            font.setPointSize(13)
            font.setBold(True)
            p.setFont(font)
            wrap = Qt.TextFlag.TextWordWrap.value
            tr = p.fontMetrics().boundingRect(QRect(0, 0, 340, 2000), wrap, self._text)
            bw, bh = tr.width() + 30, tr.height() + 22
            bx, by, side = self._bubble_origin(bw, bh)

            p.setBrush(QColor(8, 16, 26, 232))
            p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), 230), 2))
            p.drawRoundedRect(QRectF(bx, by, bw, bh), 12, 12)
            p.setPen(QColor(235, 247, 255))
            p.drawText(QRectF(bx + 15, by + 11, bw - 30, bh - 22),
                       wrap | Qt.AlignmentFlag.AlignLeft.value, self._text)

            # Arrow from the caption to the target, nudging toward it on the beat.
            r = self._rect
            if side == "right":
                a, b = QPointF(bx, by + bh / 2), QPointF(r.right() + 6, min(max(by + bh / 2, r.top() + 6), r.bottom() - 6))
            elif side == "left":
                a, b = QPointF(bx + bw, by + bh / 2), QPointF(r.left() - 6, min(max(by + bh / 2, r.top() + 6), r.bottom() - 6))
            elif side == "below":
                a, b = QPointF(bx + bw / 2, by), QPointF(min(max(bx + bw / 2, r.left() + 6), r.right() - 6), r.bottom() + 6)
            else:
                a, b = QPointF(bx + bw / 2, by + bh), QPointF(min(max(bx + bw / 2, r.left() + 6), r.right() - 6), r.top() - 6)
            dx, dy = b.x() - a.x(), b.y() - a.y()
            length = math.hypot(dx, dy) or 1.0
            ux, uy = dx / length, dy / length
            tip = QPointF(b.x() - ux * beat * 6, b.y() - uy * beat * 6)
            p.setPen(QPen(c, 3))
            p.drawLine(a, tip)
            for ang in (math.radians(25), math.radians(-25)):
                hx = -(ux * math.cos(ang) - uy * math.sin(ang)) * 14
                hy = -(ux * math.sin(ang) + uy * math.cos(ang)) * 14
                p.drawLine(tip, QPointF(tip.x() + hx, tip.y() + hy))

    class Bridge(QObject):
        """Lives on the GUI thread. Worker threads only ever emit its signals,
        which Qt queues onto that thread — widgets are never touched from here."""
        show_sig = pyqtSignal(object, str, str)     # box, caption, accent
        hide_sig = pyqtSignal()

        def __init__(self):
            super().__init__()
            self._ov = None

        # @pyqtSlot is load-bearing. Without it PyQt wraps the method in a proxy
        # object that lives in whichever thread called connect() — a worker
        # thread with no event loop — and the "queued" slot then runs there, so
        # the overlay and its timer get built on the wrong thread. Decorated, the
        # connection targets this QObject itself, whose thread is the GUI thread.
        @pyqtSlot(object, str, str)
        def _show(self, box, text, accent):
            self._hide()
            try:
                self._ov = Overlay(box, text, accent)
                self._ov.show()
            except Exception as e:                            # noqa: BLE001
                print(f"[GuideMe] could not draw the highlight: {e}")
                self._ov = None

        @pyqtSlot()
        def _hide(self):
            if self._ov is not None:
                try:
                    self._ov.close()
                    self._ov.deleteLater()
                except Exception:                             # noqa: BLE001
                    pass
                self._ov = None

    _QT.update(Overlay=Overlay, Bridge=Bridge)
    return _QT


def _bridge():
    """The one Bridge, parented to the GUI thread. None when there is no GUI."""
    with _QT_LOCK:
        if "bridge" in _QT:
            return _QT["bridge"]
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return None
        b = _qt()["Bridge"]()
        b.moveToThread(app.thread())
        b.show_sig.connect(b._show)       # after the move, so the receiver is on the GUI thread
        b.hide_sig.connect(b._hide)
        _QT["bridge"] = b
        return b


def _show(box, text: str) -> bool:
    b = _bridge()
    if b is None:
        return False
    b.show_sig.emit(box, text, _accent())
    return True


def _hide() -> None:
    b = _QT.get("bridge")
    if b is not None:
        b.hide_sig.emit()


# ─────────────────────────────────────────────────────────────────────────────
#  Finding the thing on the screen
# ─────────────────────────────────────────────────────────────────────────────
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _parse_box(text: str, width: int, height: int):
    """Gemini's native box format is [ymin, xmin, ymax, xmax] on a 0-1000 grid.
    Returns pixel (x1, y1, x2, y2), or None when the reply is not a box."""
    if not text or "NOT_FOUND" in text.upper():
        return None
    nums = [float(n) for n in _NUM.findall(text)]
    if len(nums) < 4:
        return None
    ymin, xmin, ymax, xmax = [max(0.0, min(1000.0, n)) for n in nums[:4]]
    if xmax <= xmin or ymax <= ymin:
        return None
    return (round(xmin * width / 1000), round(ymin * height / 1000),
            round(xmax * width / 1000), round(ymax * height / 1000))


def _grow(box, width: int, height: int):
    """A target smaller than _MIN_BOX is drawn (and accepts clicks) that big."""
    x1, y1, x2, y2 = box
    if x2 - x1 < _MIN_BOX:
        cx = (x1 + x2) / 2
        x1, x2 = cx - _MIN_BOX / 2, cx + _MIN_BOX / 2
    if y2 - y1 < _MIN_BOX:
        cy = (y1 + y2) / 2
        y1, y2 = cy - _MIN_BOX / 2, cy + _MIN_BOX / 2
    return (max(0, round(x1)), max(0, round(y1)), min(width, round(x2)), min(height, round(y2)))


def _locate(target: str):
    """Screenshot -> vision model -> bounding box in screen pixels, or None."""
    try:
        import pyautogui
        from google.genai import types as gtypes
        from core import gemini

        img = pyautogui.screenshot()
        width, height = img.size
        send = img
        if width > 1600:                         # normalised boxes survive a resize
            send = img.resize((1600, round(height * 1600 / width)))
        buf = io.BytesIO()
        send.save(buf, format="PNG")
        prompt = (
            "This is a screenshot of a computer screen. Find the single on-screen "
            f"element a person should click or look at for this: '{target}'. "
            "Reply with ONLY its bounding box as [ymin, xmin, ymax, xmax], every value "
            "normalised to 0-1000. If it is not visible, reply NOT_FOUND. Do not guess."
        )
        response = gemini.call(
            [gtypes.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"), prompt],
            tier=gemini.FAST, timeout_ms=25_000,
        )
        if response is None:
            return None
        box = _parse_box((response.text or "").strip(), width, height)
        return _grow(box, width, height) if box else None
    except Exception as e:                                    # noqa: BLE001
        print(f"[GuideMe] could not look for '{target}': {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Seeing the user's click
# ─────────────────────────────────────────────────────────────────────────────
class _WinClicks:
    """Polls the left button's state. Needs nothing installed and sees clicks in
    every application, which is the point: the user clicks someone else's window."""
    available = True

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ct = ctypes
        self._user32 = ctypes.windll.user32
        self._pt = wintypes.POINT()
        swapped = bool(self._user32.GetSystemMetrics(23))      # SM_SWAPBUTTON
        self._vk = 0x02 if swapped else 0x01                   # the "primary" button
        self._down = self._is_down()

    def _is_down(self) -> bool:
        return bool(self._user32.GetAsyncKeyState(self._vk) & 0x8000)

    def poll(self):
        down = self._is_down()
        out = []
        if down and not self._down:
            self._user32.GetCursorPos(self._ct.byref(self._pt))
            out.append((self._pt.x, self._pt.y))
        self._down = down
        return out

    def close(self):
        pass


class _PynputClicks:
    available = True

    def __init__(self):
        from pynput import mouse
        self._q: list = []
        self._left = mouse.Button.left

        def on_click(x, y, button, pressed):
            if pressed and button == self._left:
                self._q.append((int(x), int(y)))

        self._listener = mouse.Listener(on_click=on_click)
        self._listener.start()

    def poll(self):
        out, self._q = self._q, []
        return out

    def close(self):
        try:
            self._listener.stop()
        except Exception:                                     # noqa: BLE001
            pass


class _NoClicks:
    available = False

    def poll(self):
        return []

    def close(self):
        pass


def _click_source():
    """Whatever can see clicks on this machine, else something that sees none."""
    try:
        if _WIN:
            return _WinClicks()
        return _PynputClicks()
    except Exception:                                         # noqa: BLE001
        return _NoClicks()


# ─────────────────────────────────────────────────────────────────────────────
#  Typing for the user
# ─────────────────────────────────────────────────────────────────────────────
def _win_type(text: str) -> None:
    """SendInput with KEYEVENTF_UNICODE: any language, no clipboard involved.
    (The clipboard is avoided on purpose — a copy would trigger the HUD's
    clipboard panel and overwrite whatever the user had copied.)"""
    import ctypes
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        # MOUSEINPUT is the largest member; leaving it out makes INPUT too small
        # and SendInput fails with ERROR_INVALID_PARAMETER.
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    user32 = ctypes.windll.user32

    def send(vk: int, scan: int, flags: int) -> None:
        inp = INPUT(type=1, ki=KEYBDINPUT(vk, scan, flags, 0, 0))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    UNICODE, KEYUP = 0x0004, 0x0002
    for ch in text:
        if ch == "\n":
            send(0x0D, 0, 0)
            send(0x0D, 0, KEYUP)
            time.sleep(0.02)
            continue
        if ch == "\t":
            ch = " "
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):                       # surrogate pairs go out as two units
            unit = int.from_bytes(data[i:i + 2], "little")
            send(0, unit, UNICODE)
            send(0, unit, UNICODE | KEYUP)
        time.sleep(0.012)


def _type_for_user(text: str, press_enter: bool) -> bool:
    try:
        time.sleep(0.3)                                       # let the click's focus settle
        if _WIN:
            _win_type(text)
        else:
            import pyautogui
            if text.isascii():
                pyautogui.write(text, interval=0.02)
            else:
                import pyperclip
                old = None
                try:
                    old = pyperclip.paste()
                except Exception:                             # noqa: BLE001
                    pass
                pyperclip.copy(text)
                time.sleep(0.05)
                pyautogui.hotkey("command" if _MAC else "ctrl", "v")
                time.sleep(0.3)
                if old is not None:
                    pyperclip.copy(old)
        if press_enter:
            import pyautogui
            time.sleep(0.1)
            pyautogui.press("enter")
        return True
    except Exception as e:                                    # noqa: BLE001
        print(f"[GuideMe] typing failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  One step, on its own thread
# ─────────────────────────────────────────────────────────────────────────────
def _glide_cursor(box) -> None:
    """Once, at the start. Doing it repeatedly would be fighting the user's hand."""
    try:
        import pyautogui
        x1, y1, x2, y2 = box
        pyautogui.moveTo((x1 + x2) // 2, (y1 + y2) // 2, duration=0.4,
                         tween=pyautogui.easeInOutQuad)
    except Exception:                                         # noqa: BLE001
        pass


def _wait(step: _Step, clicks) -> tuple[str, Optional[tuple]]:
    """Block until the step is over. Returns (how, where-the-click-was)."""
    deadline = time.monotonic() + _TIMEOUT_S
    x1, y1, x2, y2 = step.bbox
    while time.monotonic() < deadline:
        if step.cancel.is_set():
            return "cancelled", None
        if step.advance.is_set():
            return "manual", None
        if step.wait_for == "click":
            for (cx, cy) in clicks.poll():
                if (x1 - _CLICK_PAD <= cx <= x2 + _CLICK_PAD
                        and y1 - _CLICK_PAD <= cy <= y2 + _CLICK_PAD):
                    return "clicked", (cx, cy)
        time.sleep(0.01 if step.wait_for == "click" else 0.1)
    return "timeout", None


def _remaining(step: _Step) -> str:
    parts = [s.strip() for s in step.plan.split("|") if s.strip()]
    rest = parts[step.n:] if step.n < len(parts) else []
    return f"Remaining plan: {' | '.join(rest)}. " if rest else ""


def _release(step: _Step) -> None:
    """Take the highlight down — but only if it is still this step's."""
    with _lock:
        if _current is step:
            _hide()


def _run_step(step: _Step, player, clicks) -> None:
    try:
        if step.bbox is None:
            box = _locate(step.target)
            if step.cancel.is_set():
                return
            if box is None:
                with _lock:
                    stale = _current is not step
                if not stale:
                    _say(player,
                         f"Tell the user, in one short sentence in their own language, that you "
                         f"cannot see '{step.target}' on the screen right now, and ask them to bring "
                         f"the right window to the front or describe where it is. Then call guide_me "
                         f"again for this same step. {_remaining(step)}")
                return
            step.bbox = box

        if not _show(step.bbox, step.label):
            _say(player, "Tell the user, in one short sentence, that you cannot draw on their "
                         "screen right now, so you will describe the steps out loud instead.")
            return
        _glide_cursor(step.bbox)

        how, _where = _wait(step, clicks)
        if how == "cancelled":
            return                                  # replaced, or stopped by the user

        typed = None
        if how == "clicked" and step.type_text:
            typed = _type_for_user(step.type_text, step.press_enter)
        _release(step)

        if how == "manual":
            return                                  # the tool call that said "next" already answers
        if how == "timeout":
            _say(player, f"Tell the user, in one short sentence in their own language, that you took "
                         f"the pointer off the screen because step {step.n} ({step.label}) has been "
                         f"waiting a long time, and ask if they want to carry on or need help.")
            return

        note = ""
        if typed is True:
            note = "You already typed the text for them, so do not tell them to type it. "
        elif typed is False:
            note = ("Typing for them FAILED — ask them to type it themselves: "
                    f"'{step.type_text[:80]}'. Then wait for them before the next step. ")
        _say(player,
             f"[WALKTHROUGH] The user just completed step {step.n}: {step.label}. {note}"
             f"{_remaining(step)}Now point at the NEXT step with guide_me (one short spoken "
             f"sentence, then wait). If that was the last step, call guide_me with "
             f"action='finish' and tell them in one short sentence they are done. "
             f"Do not repeat what they just did.")
    except Exception as e:                                    # noqa: BLE001
        print(f"[GuideMe] step failed: {e}")
        _release(step)
    finally:
        try:
            clicks.close()
        except Exception:                                     # noqa: BLE001
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  The tool
# ─────────────────────────────────────────────────────────────────────────────
def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stop_current() -> Optional[_Step]:
    """Retire whatever step is live. Caller holds _lock."""
    global _current
    old = _current
    if old is not None:
        old.cancel.set()
        _hide()
    _current = None
    return old


def guide_me(parameters: dict = None, player=None, session_memory=None) -> str:
    global _current, _counter, _plan
    p = parameters or {}
    action = str(p.get("action") or "point").strip().lower()

    # ---- finish / cancel ------------------------------------------------
    if action in ("finish", "stop", "cancel", "end", "done_all"):
        with _lock:
            old = _stop_current()
            _counter, _plan = 0, ""
        return outcome.done("guide_me", ended=True, had_active_step=old is not None)

    # ---- the user says they did it / skip it ----------------------------
    if action in ("next", "skip", "done"):
        with _lock:
            step = _current
            if step is None:
                return outcome.failed("guide_me", "there is no walkthrough step waiting right now")
            step.advance.set()
            _hide()
            _current = None
        return (outcome._line(outcome.DONE, "guide_me", {"advanced_step": step.n, "label": step.label})
                + " -- Move on: point at the NEXT step with guide_me, or action='finish' if that "
                  "was the last one. Do not repeat the step they just finished.")

    # ---- put the highlight back where it was ----------------------------
    if action in ("again", "repeat", "show_again"):
        with _lock:
            step = _current
        if step is None or step.bbox is None:
            return outcome.failed("guide_me", "there is no step on screen to show again")
        _show(step.bbox, step.label)
        _glide_cursor(step.bbox)
        return outcome._line(outcome.DONE, "guide_me", {"step": step.n, "re_shown": True}) + \
            " -- Remind the user in one short sentence what to do there. Do not call again."

    # ---- point at something --------------------------------------------
    label = str(p.get("instruction") or "").strip()[:140]
    target = str(p.get("target") or "").strip()
    x, y = _num(p.get("x")), _num(p.get("y"))
    if not target and (x is None or y is None):
        return outcome.ask("guide_me", "What on the screen should I point at?")
    if not label:
        label = target[:90]

    bbox = None
    if x is not None and y is not None:
        bbox = _grow((x - 1, y - 1, x + 1, y + 1), 10 ** 6, 10 ** 6)

    type_text = str(p.get("type_text") or "")
    typing_note = {}
    if type_text:
        if _SENSITIVE.search(f"{target} {label}"):
            typing_note = {"typing": "skipped", "why": "this looks like a secret field - the user must type it themselves"}
            type_text = ""
        elif len(type_text) > _MAX_TYPE:
            typing_note = {"typing": "skipped", "why": "text too long to type for them"}
            type_text = ""

    wait_for = "say_done" if str(p.get("wait_for") or "").strip().lower() in ("say_done", "say", "manual") else "click"

    clicks = _click_source() if wait_for == "click" else _NoClicks()
    if wait_for == "click" and not clicks.available:
        wait_for = "say_done"
        typing_note = {**typing_note, "click_detection": "unavailable",
                       "so": "ask the user to say 'next' when they have done it - you cannot see their click"}
        if type_text:
            typing_note.update(typing="skipped", why="typing needs click detection on this system")
            type_text = ""

    with _lock:
        _stop_current()
        _counter += 1
        n = int(_num(p.get("step_number")) or _counter)
        _counter = n
        plan_text = str(p.get("plan") or "").strip()
        if plan_text:
            _plan = plan_text
        step = _Step(n, label, target, bbox, type_text,
                     bool(p.get("press_enter")), wait_for, _plan)
        _current = step

    threading.Thread(target=_run_step, args=(step, player, clicks),
                     daemon=True, name=f"guide-step-{n}").start()

    facts = {"step": n, "target": target or "given position",
             "waiting_for": "the user's click" if wait_for == "click" else "the user saying 'next'",
             **typing_note}
    if type_text:
        facts["will_type_after_click"] = True
    return outcome._line(outcome.STARTED, "guide_me", facts) + (
        " -- The highlight is being placed on their screen now. Say in ONE short sentence, in the "
        "user's own language, what to do there, then STOP and wait. You will get a [WALKTHROUGH] "
        "message when they finish. Do not call guide_me again before that, and do not describe "
        "later steps yet.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "guide_me",
    "description": (
        "GUIDED WALKTHROUGH. Shows the user, on their own screen, exactly where to click — one step "
        "at a time. Use it whenever the user wants to be TAUGHT or SHOWN how to do something on their "
        "computer, in an app or on a website: 'how do I change my wallpaper', 'walk me through setting "
        "up email', 'show me where to turn on dark mode', 'guide me', 'where is the print button'. Use "
        "it INSTEAD of reading out a list of instructions. If the user wants YOU to just do the thing, "
        "use the other tools; this is for when they will do it themselves. "
        "Each call draws a glowing highlight, an arrow and a short caption on the target and moves the "
        "cursor to it. The highlight STAYS there until the user clicks it. If something must be typed "
        "there, pass type_text and it is typed for them right after they click into the field. "
        "HOW TO RUN ONE: (1) on the first call pass plan = the whole list of steps separated by ' | '. "
        "(2) call action='point' for step 1 with target (what the element is and where it is, e.g. 'the "
        "Start button at the bottom-left') and instruction (a caption of at most ten words, in the user's "
        "language). (3) The tool answers immediately — say ONE short sentence of what to do, then stop and "
        "wait. (4) When the user finishes, you receive a [WALKTHROUGH] message: point at the next step. "
        "(5) After the last step call action='finish'. "
        "Other actions: 'next' when the user says done / next / skip / I did it; 'again' when they say "
        "where? / I lost it / show me again; 'finish' also cancels. For a step with no click (press a key, "
        "wait for an install) use wait_for='say_done'. "
        "The target must be visible on the screen right now: if it is behind a menu or another window, "
        "make the first step pointing at whatever opens it. "
        "NEVER put a password, PIN, card number or other secret in type_text — point at that field and let "
        "the user type it."
    ),
    "brief": "Walks the user through a task by pointing at where to click, one step at a time, and typing for them.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "'point' (default) for the next step, 'next' when the user says done/skip, "
                               "'again' to re-show the current step, 'finish' to end or cancel.",
                "enum": ["point", "next", "again", "finish"],
            },
            "target": {
                "type": "STRING",
                "description": "What to point at, described so it can be found on a screenshot: what it is, "
                               "what it says, and roughly where. Example: 'the blue Save button at the top right'.",
            },
            "instruction": {
                "type": "STRING",
                "description": "The short caption drawn next to the highlight, at most ten words, in the "
                               "user's own language. Example: 'Click here to open Settings'.",
            },
            "type_text": {
                "type": "STRING",
                "description": "Text to type for the user AFTER they click into this field. Never a secret.",
            },
            "press_enter": {
                "type": "BOOLEAN",
                "description": "Press Enter after typing type_text.",
            },
            "wait_for": {
                "type": "STRING",
                "description": "'click' (default): wait for the user to click the target. 'say_done': wait "
                               "until the user says they are done — for steps with no click.",
                "enum": ["click", "say_done"],
            },
            "step_number": {
                "type": "INTEGER",
                "description": "Which step of the plan this is, starting at 1.",
            },
            "plan": {
                "type": "STRING",
                "description": "On the first call: the whole list of steps in order, separated by ' | '.",
            },
            "x": {"type": "NUMBER", "description": "Optional exact screen x in pixels, instead of target."},
            "y": {"type": "NUMBER", "description": "Optional exact screen y in pixels, instead of target."},
        },
        "required": ["action"],
    },
    "handler": guide_me,
}
