"""
core/orb_hud.py — the "orb" HUD: a wireframe globe over a holographic platform, in a starfield.

Drawn with plain QPainter (no GPU), like the other two HUD styles, and from the same
state HudCanvas already tracks, so nothing new has to be wired up:

    listening  cyan globe, GREEN orbit ring, platform ticks follow the microphone
    thinking   AMBER ring that tumbles, ticks hold still with a slow sweeping highlight
    speaking   the core flares, ORANGE ring spins faster, ticks follow the voice
    muted      everything dims and turns red

Everything that moves is integrated from elapsed time (not from absolute time times a
rate that changes with state), so a change of state speeds things up smoothly instead of
making the rings jump. The starfield is drawn once per size into a cached pixmap; only a
few dozen stars twinkle each frame.

HudCanvas.paintEvent calls paint(canvas, painter) when hud_style == "orb". Any failure
here falls back to the ordinary HUD, so a drawing bug can never leave the window blank.
"""
from __future__ import annotations

import math
import random
import sys
import time

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter, QPen, QPixmap,
                         QRadialGradient)

# The app's own palette when we can find it (so live re-theming still works); these are the
# same values ui.py ships with otherwise.
_DEFAULTS = dict(BG="#00060a", PRI="#00d4ff", PRI_DIM="#007a99", ACC="#ff6b00", ACC2="#ffcc00",
                 GREEN="#00ff88", RED="#ff3355", MUTED_C="#ff3366", WHITE="#d8f8ff",
                 TEXT_DIM="#3a8a9a")


class _Pal:
    def __init__(self, canvas):
        mod = sys.modules.get(type(canvas).__module__)
        src = getattr(mod, "C", None)
        for k, v in _DEFAULTS.items():
            setattr(self, k, getattr(src, k, v) if src is not None else v)


def _c(hexs: str, a: int = 255) -> QColor:
    col = QColor(hexs)
    col.setAlpha(max(0, min(255, int(a))))
    return col


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = QColor(a), QColor(b)
    t = max(0.0, min(1.0, t))
    return QColor(int(ca.red() + (cb.red() - ca.red()) * t), int(ca.green() + (cb.green() - ca.green()) * t),
                  int(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


# ─────────────────────────────────────────────────────────────────────────────
def _starfield(W: int, H: int):
    """Static stars into a pixmap, plus a small list of stars that twinkle."""
    rnd = random.Random(7)
    pm = QPixmap(max(1, W), max(1, H))
    pm.fill(Qt.GlobalColor.transparent)
    q = QPainter(pm)
    q.setRenderHint(QPainter.RenderHint.Antialiasing)
    q.setPen(Qt.PenStyle.NoPen)
    n = int(W * H / 3000)
    tints = ["#9fc8ff", "#ffffff", "#7fb0ff", "#cfe4ff", "#ffd9a0"]
    for _ in range(n):
        x, y = rnd.uniform(0, W), rnd.uniform(0, H)
        r = rnd.choice((0.6, 0.8, 1.0, 1.0, 1.3, 1.7))
        q.setBrush(_c(rnd.choice(tints), rnd.randint(70, 210)))
        q.drawEllipse(QPointF(x, y), r, r)
    q.end()
    twinkle = [(rnd.uniform(0, W), rnd.uniform(0, H), rnd.uniform(0, 6.28), rnd.uniform(1.0, 2.2),
                rnd.choice(tints)) for _ in range(max(12, n // 7))]
    return pm, twinkle


def _state(canvas):
    """(accent colour, label, speed multiplier, tick mode) for what the HUD is doing."""
    pal = _Pal(canvas)
    if canvas.muted:
        return pal.MUTED_C, "MUTED", 0.15, "dim"
    if canvas.speaking:
        return pal.ACC, "SPEAKING", 2.1, "voice"
    st = canvas.state
    if st in ("THINKING", "PROCESSING"):
        return pal.ACC2, st, 1.5, "sweep"
    if st == "LISTENING":
        return pal.GREEN, "LISTENING", 1.0, "voice"
    return pal.PRI, str(st), 0.7, "idle"


def paint(canvas, p: QPainter) -> None:
    pal = _Pal(canvas)
    W, H = canvas.width(), canvas.height()
    now = time.monotonic()
    accent, label, speed, tick_mode = _state(canvas)
    amp = float(getattr(canvas, "_amp_disp", 0.0) or 0.0)
    amp = max(0.0, min(1.0, amp))

    # Integrated phases: change of state changes the SPEED, never the position.
    st = getattr(canvas, "_orb", None)
    if st is None:
        st = canvas._orb = {"t": now, "a": 0.0, "b": 0.0, "g": 0.0, "stars": None, "key": None, "speed": speed}
    dt = min(0.1, max(0.0, now - st["t"]))
    st["t"] = now
    st["speed"] += (speed - st["speed"]) * min(1.0, dt * 3.0)
    s = st["speed"]
    st["a"] += dt * 0.9 * s          # ring A / orbiting dot
    st["b"] += dt * 0.45 * s         # ring B and platform sweep
    st["g"] += dt * 0.28 * (0.5 + 0.5 * s)   # globe spin
    pa, pb, pg = st["a"], st["b"], st["g"]
    tsec = now

    # ---- layout -----------------------------------------------------------
    # The globe floats just above a platform about 2.5 globe-radii wide; the whole scene is
    # centred in the band above the name/state/waveform stack.
    bottom = 118.0
    Hs = max(150.0, H - bottom)
    R = max(20.0, min(W * 0.11, (Hs - 50.0) * 0.205))
    rx = R * 2.5
    ry = rx * 0.46
    cx = W / 2.0
    scene_h = 3.3 * R + ry + 0.45 * R              # ring top .. platform bottom incl. ticks
    top = 44.0 + max(0.0, (Hs - 44.0 - scene_h) / 2.0)
    py = top + 3.3 * R
    sy = py - ry * 0.55 - R

    # ---- background ---------------------------------------------------------
    bg = QLinearGradient(0, 0, 0, H)
    bg.setColorAt(0.0, QColor("#020817"))
    bg.setColorAt(0.5, QColor("#08225f"))
    bg.setColorAt(1.0, QColor("#020a1e"))
    p.fillRect(0, 0, W, H, QBrush(bg))
    halo = QRadialGradient(QPointF(cx, sy), max(W, H) * 0.55)
    halo.setColorAt(0.0, _c(pal.PRI, 46))
    halo.setColorAt(1.0, _c(pal.PRI, 0))
    p.fillRect(0, 0, W, H, QBrush(halo))

    if st["key"] != (W, H):
        st["stars"], st["tw"] = _starfield(W, H)
        st["key"] = (W, H)
    p.drawPixmap(0, 0, st["stars"])
    p.setPen(Qt.PenStyle.NoPen)
    for (x, y, ph, sp, tint) in st["tw"]:
        a = 0.5 + 0.5 * math.sin(tsec * sp + ph)
        p.setBrush(_c(tint, 40 + 190 * a * a))
        r = 0.9 + 1.1 * a
        p.drawEllipse(QPointF(x, y), r, r)

    # ---- state pill at the top ---------------------------------------------------
    f = QFont("Courier New", 8, QFont.Weight.Bold)
    p.setFont(f)
    pw = max(104.0, p.fontMetrics().horizontalAdvance(label) + 44.0)
    pill = QRectF(cx - pw / 2, 10, pw, 22)
    p.setBrush(_c("#020a1e", 200))
    p.setPen(QPen(_c(accent, 210), 1.2))
    p.drawRoundedRect(pill, 8, 8)
    blink = 0.55 + 0.45 * math.sin(tsec * 4.0)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_c(accent, 120 + 135 * blink))
    p.drawEllipse(QPointF(pill.left() + 14, pill.center().y()), 3.2, 3.2)
    p.setPen(_c(accent))
    p.drawText(QRectF(pill.left() + 22, pill.top(), pill.width() - 24, pill.height()),
               Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)

    # ---- platform --------------------------------------------------------------
    disc = QRadialGradient(QPointF(cx, py), rx * 1.05)
    disc.setColorAt(0.0, _c("#1a8cff", 120))
    disc.setColorAt(0.55, _c("#0b4fd0", 60))
    disc.setColorAt(1.0, _c("#0b4fd0", 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(disc))
    p.drawEllipse(QPointF(cx, py), rx * 1.08, ry * 1.12)
    for k, (scale, alpha, w) in enumerate(((1.0, 235, 1.6), (0.84, 120, 1.0), (0.62, 95, 1.0), (0.4, 80, 1.0))):
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_c("#3fa0ff" if k else pal.PRI, alpha), w))
        p.drawEllipse(QPointF(cx, py), rx * scale, ry * scale)
    # soft light pool where the globe's glow lands
    pool = QRadialGradient(QPointF(cx, py), rx * 0.45)
    pool.setColorAt(0.0, _c(pal.PRI, 90))
    pool.setColorAt(1.0, _c(pal.PRI, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(pool))
    p.drawEllipse(QPointF(cx, py), rx * 0.5, ry * 0.5)

    # ticks round the rim: the real audio level when listening/speaking
    N = 96
    for i in range(N):
        th = i / N * 2 * math.pi
        ex, ey = math.cos(th), math.sin(th)
        x0, y0 = cx + rx * ex, py + ry * ey
        shim = (0.5 + 0.5 * math.sin(tsec * 3.1 + i * 0.93)) * (0.5 + 0.5 * math.sin(tsec * 1.7 + i * 0.37))
        if tick_mode == "voice":
            ln = 4.0 + 2.0 * shim + amp * 34.0 * (0.25 + 0.75 * shim)
            al = 150 + 105 * min(1.0, amp * 2.0 + 0.3 * shim)
        elif tick_mode == "sweep":
            d = (th - pb * 1.3) % (2 * math.pi)
            hit = max(0.0, 1.0 - min(d, 2 * math.pi - d) / 0.9)
            ln, al = 4.0 + 12.0 * hit, 110 + 145 * hit
        elif tick_mode == "dim":
            ln, al = 3.0, 70
        else:
            ln = 4.0 + 3.0 * shim
            al = 120 + 80 * shim
        dx, dy = ex, ey * 0.27
        nrm = math.hypot(dx, dy) or 1.0
        p.setPen(QPen(_c(accent, al), 1.7))
        p.drawLine(QPointF(x0, y0), QPointF(x0 + dx / nrm * ln, y0 + dy / nrm * ln * 1.4))

    # ---- globe ---------------------------------------------------------------------
    flare = 1.0 + (0.35 * amp if canvas.speaking else 0.1 * amp)
    core_col = _mix(pal.ACC2, pal.ACC, 0.4) if canvas.speaking and not canvas.muted else pal.PRI
    if canvas.muted:
        core_col = pal.MUTED_C
    g = QRadialGradient(QPointF(cx, sy), R * 2.6 * flare)
    g.setColorAt(0.0, _c(core_col, 120))
    g.setColorAt(0.45, _c(core_col, 36))
    g.setColorAt(1.0, _c(core_col, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(QPointF(cx, sy), R * 2.6 * flare, R * 2.6 * flare)

    body = QRadialGradient(QPointF(cx - R * 0.25, sy - R * 0.3), R * 1.15)
    body.setColorAt(0.0, _c("#7fe8ff", 150))
    body.setColorAt(0.6, _c("#0a74d8", 110))
    body.setColorAt(1.0, _c("#06307a", 150))
    p.setBrush(QBrush(body))
    p.setPen(QPen(_c(pal.PRI, 230), 1.4))
    p.drawEllipse(QPointF(cx, sy), R, R)
    # wireframe: meridians turn with the globe, parallels are fixed
    p.setBrush(Qt.BrushStyle.NoBrush)
    for k in range(6):
        a = pg + k * math.pi / 6
        mw = R * abs(math.cos(a))
        p.setPen(QPen(_c("#a8f0ff", 70 + 80 * abs(math.cos(a))), 0.9))
        p.drawEllipse(QPointF(cx, sy), max(0.5, mw), R)
    for lat in (-60, -30, 0, 30, 60):
        la = math.radians(lat)
        lw = R * math.cos(la)
        p.setPen(QPen(_c("#a8f0ff", 105), 0.9))
        p.drawEllipse(QPointF(cx, sy + R * math.sin(la) * 0.92), lw, lw * 0.2)
    # the core
    cr = R * 0.62 * flare
    cg = QRadialGradient(QPointF(cx, sy), cr)
    cg.setColorAt(0.0, _c("#ffffff", 255))
    cg.setColorAt(0.28, _c(core_col, 235))
    cg.setColorAt(1.0, _c(core_col, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(cg))
    p.drawEllipse(QPointF(cx, sy), cr, cr)
    p.setBrush(_c("#ffffff", 255))
    p.drawEllipse(QPointF(cx, sy), 2.6, 2.6)

    # ---- orbit rings ----------------------------------------------------------------
    def ring(radius, rot, squash, col, alpha, width, dot_phase=None, dot_col=None):
        p.save()
        p.translate(cx, sy)
        p.rotate(math.degrees(rot))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_c(col, alpha), width))
        ryy = max(radius * 0.06, radius * abs(squash))
        p.drawEllipse(QPointF(0, 0), radius, ryy)
        if dot_phase is not None:
            dx, dy = radius * math.cos(dot_phase), ryy * math.sin(dot_phase)
            glow = QRadialGradient(QPointF(dx, dy), 9)
            glow.setColorAt(0.0, _c(dot_col or col, 230))
            glow.setColorAt(1.0, _c(dot_col or col, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawEllipse(QPointF(dx, dy), 9, 9)
            p.setBrush(_c("#ffffff", 240))
            p.drawEllipse(QPointF(dx, dy), 2.2, 2.2)
        p.restore()

    ring(R * 1.95, 0.0, 0.26, "#3fa0ff", 120, 1.0, dot_phase=pb * 1.3 + 0.6, dot_col=pal.ACC2)
    ring(R * 1.36, -pa * 0.28 + 1.0, 0.55 + 0.35 * math.sin(pa * 0.45), "#4ab8ff", 135, 1.1)
    ring(R * 1.62, pa * 0.33, math.cos(pa * 0.62), accent, 235, 2.0, dot_phase=pa * 1.7, dot_col=accent)

    # ---- name, state and waveform ---------------------------------------------------------
    name = str(getattr(canvas, "_assistant_name", "J.A.R.V.I.S") or "J.A.R.V.I.S").upper()
    nf = QFont("Segoe UI", 12, QFont.Weight.DemiBold)
    nf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5.0)
    p.setFont(nf)
    p.setPen(_c(pal.WHITE, 235))
    ny = H - bottom + 14
    p.drawText(QRectF(0, ny, W, 26), Qt.AlignmentFlag.AlignCenter, name)

    sf = QFont("Courier New", 8, QFont.Weight.Bold)
    sf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
    p.setFont(sf)
    sy2 = ny + 38
    tw = p.fontMetrics().horizontalAdvance(label) + 14
    p.setPen(_c(accent))
    p.drawText(QRectF(0, sy2 - 9, W, 18), Qt.AlignmentFlag.AlignCenter, label)
    p.setPen(QPen(_c(accent, 160), 1))
    p.drawLine(QPointF(cx - tw / 2 - 74, sy2), QPointF(cx - tw / 2 - 8, sy2))
    p.drawLine(QPointF(cx + tw / 2 + 8, sy2), QPointF(cx + tw / 2 + 74, sy2))

    wy, ww = sy2 + 22, min(300.0, W * 0.62)
    pts = []
    steps = 90
    for i in range(steps + 1):
        u = i / steps
        env = math.sin(math.pi * u) ** 0.8
        if canvas.muted:
            v = 0.0
        elif tick_mode == "sweep":
            v = 0.12 * math.sin(tsec * 5 + u * 14) * env
        else:
            v = (0.1 + amp * 1.5) * env * (0.6 * math.sin(tsec * 9.0 + u * 26) + 0.4 * math.sin(tsec * 5.3 + u * 53))
        pts.append(QPointF(cx - ww / 2 + u * ww, wy + v * 13))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(_c(accent, 80), 4))
    p.drawPolyline(pts)
    p.setPen(QPen(_c(accent, 235), 1.4))
    p.drawPolyline(pts)
