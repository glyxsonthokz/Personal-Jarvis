"""
core/earth_view.py — the Earth: a 3D globe you can spin, that opens up into a satellite map.

It is a child widget laid over the HUD's centre. While it is up the HUD's own drawing is
switched off (hud._earth_cover), and the side panels are untouched, as in the reel.

GLOBE   Drawn in software with numpy: every pixel of the sphere is turned back into a
        longitude and latitude and looked up in an equirectangular texture (NASA Blue Marble,
        or a blue "holographic" map baked from the Natural Earth outlines). The sun's real
        position darkens the night side, and city lights show through. While the globe is
        moving it renders at reduced resolution and snaps to full quality when it stops.
MAP     Past the zoom where a sphere is no use, the same view becomes a Web-Mercator tile map
        (Esri World Imagery with place names, or CARTO dark for the holographic style). Tiles
        are fetched on worker threads and cached on disk; until one arrives the dark ground
        and the vector outlines are drawn, so it never shows a hole.
LAYERS  Earthquakes (USGS), satellites with their orbit tracks (CelesTrak + SGP4, computed
        here), aircraft (adsb.lol), day/night, place names. Each is a button on the right.
CARDS   Camera time/position/height top-left; click anywhere and the place card (local time,
        weather) appears top-right.

Threads: every network call runs on a worker thread and hands its result back through
`data_sig`, a signal whose receiver lives on the GUI thread (the slot is decorated with
@pyqtSlot, which is what makes Qt queue the call onto that thread).
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath,
                         QPen, QPixmap, QPolygonF, QRadialGradient)
from PyQt6.QtWidgets import QWidget

from core import earth_data as data
from core import earth_geo as geo

TWO_PI = 2 * math.pi
GLOBE_MAX_Z = 3.9               # past this the sphere becomes a flat tile map
Z_MIN, Z_MAX = 1.9, 13.0

TILE_STYLES = {
    "sat": {"base": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            "over": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
            "attrib": "Imagery © Esri, Maxar, Earthstar Geographics, GIS User Community"},
    "holo": {"base": "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png", "over": None,
             "attrib": "© OpenStreetMap contributors © CARTO"},
}


# ─────────────────────────────────────────────────────────────────────────────
#  Textures
# ─────────────────────────────────────────────────────────────────────────────
def qimage_to_rgb(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    ptr = img.constBits()
    ptr.setsize(h * bpl)
    return np.frombuffer(ptr, np.uint8).reshape(h, bpl)[:, : w * 3].reshape(h, w, 3).copy()


def rgb_to_qimage(arr: np.ndarray) -> QImage:
    h, w = arr.shape[:2]
    arr = np.ascontiguousarray(arr)
    return QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


def _ring_poly(ring, W, H) -> QPolygonF:
    return QPolygonF([QPointF((lo + 180.0) / 360.0 * W, (90.0 - la) / 180.0 * H) for lo, la in ring])


def bake_holo(countries: dict, W: int = 4096, H: int = 2048) -> np.ndarray:
    """A blue holographic world: deep ocean, a faint graticule, luminous land with a bright coast."""
    img = QImage(W, H, QImage.Format.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, 0, H)
    g.setColorAt(0, QColor("#04204a")); g.setColorAt(0.5, QColor("#05306a")); g.setColorAt(1, QColor("#04204a"))
    p.fillRect(0, 0, W, H, QBrush(g))
    p.setPen(QPen(QColor(90, 170, 255, 50), 1.2))
    for lon in range(-180, 181, 15):
        x = (lon + 180) / 360 * W
        p.drawLine(QPointF(x, 0), QPointF(x, H))
    for lat in range(-75, 90, 15):
        y = (90 - lat) / 180 * H
        p.drawLine(QPointF(0, y), QPointF(W, y))
    land = QLinearGradient(0, 0, 0, H)
    land.setColorAt(0, QColor("#2fb0ec")); land.setColorAt(0.5, QColor("#1386cc")); land.setColorAt(1, QColor("#26a2e2"))
    polys = [_ring_poly(r, W, H) for f in countries.get("features", []) for r in _rings_of(f)]
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(land))
    for poly in polys:
        p.drawPolygon(poly)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for width, alpha in ((7.0, 40), (3.0, 90), (1.4, 230)):
        p.setPen(QPen(QColor(150, 236, 255, alpha), width))
        for poly in polys:
            p.drawPolyline(poly)
    p.end()
    return qimage_to_rgb(img)


def bake_borders(rgb: np.ndarray, countries: dict) -> np.ndarray:
    """Thin white national borders drawn onto the satellite texture."""
    img = rgb_to_qimage(rgb)
    H, W = rgb.shape[:2]
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(255, 255, 255, 120), 1.3))
    for f in countries.get("features", []):
        for r in _rings_of(f):
            p.drawPolyline(_ring_poly(r, W, H))
    p.end()
    return qimage_to_rgb(img)


def _rings_of(feature: dict):
    g = feature.get("geometry") or {}
    c = g.get("coordinates", [])
    if g.get("type") == "Polygon":
        return [c[0]] if c else []
    if g.get("type") == "MultiPolygon":
        return [p[0] for p in c if p]
    return []


# ─────────────────────────────────────────────────────────────────────────────
#  The sphere
# ─────────────────────────────────────────────────────────────────────────────
class GlobeRenderer:
    def __init__(self):
        self.tex: dict[str, np.ndarray] = {}
        self.tex_fast: dict[str, np.ndarray] = {}     # 2x2-averaged copies: thin lines survive nearest sampling
        self._key = None
        self._g = None
        self.version = 0

    def set_texture(self, name: str, arr: np.ndarray) -> None:
        self.tex[name] = arr
        h, w = (arr.shape[0] // 2) * 2, (arr.shape[1] // 2) * 2
        self.tex_fast[name] = arr[:h, :w].reshape(h // 2, 2, w // 2, 2, arr.shape[2]).mean(axis=(1, 3)).astype(np.uint8)
        self.version += 1

    def _grid(self, w, h, R, lat0):
        key = (w, h, round(R, 2), round(lat0, 4))
        if key == self._key:
            return self._g
        cx, cy = w / 2.0, h / 2.0
        x0, x1 = max(0, int(cx - R - 3)), min(w, int(cx + R + 4))
        y0, y1 = max(0, int(cy - R - 3)), min(h, int(cy + R + 4))
        if x1 <= x0 or y1 <= y0:
            return None
        ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        X = (xs + 0.5 - cx) / R
        Y = (cy - ys - 0.5) / R
        rho2 = X * X + Y * Y
        m = rho2 <= (1 + 2.0 / R) ** 2
        idx = np.flatnonzero(m.ravel())
        X, Y, rho2 = X.ravel()[idx], Y.ravel()[idx], rho2.ravel()[idx]
        rho = np.sqrt(rho2)
        Z = np.sqrt(np.clip(1 - rho2, 0, 1))
        sl, cl = math.sin(lat0), math.cos(lat0)
        sphi = np.clip(Z * sl + Y * cl, -1, 1)
        phi = np.arcsin(sphi)
        dlam = np.arctan2(X, Z * cl - Y * sl)
        self._g = {"box": (y0, y1, x0, x1), "idx": idx, "v": ((math.pi / 2 - phi) / math.pi).astype(np.float32),
                   "dlam": dlam.astype(np.float32), "cdl": np.cos(dlam), "sdl": np.sin(dlam), "sphi": sphi,
                   "cphi": np.cos(phi), "Z": Z, "alpha": np.clip((1 - rho) * R + 0.5, 0, 1).astype(np.float32)}
        self._key = key
        return self._g

    @staticmethod
    def _sample(tex, u, v, fast):
        Ht, Wt = tex.shape[:2]
        if fast:
            return tex[np.clip((v * (Ht - 1)).astype(np.int32), 0, Ht - 1), u.astype(np.int32) % Wt].astype(np.float32)
        fy = v * (Ht - 1)
        y0 = np.clip(np.floor(fy).astype(np.int32), 0, Ht - 1)
        y1 = np.minimum(y0 + 1, Ht - 1)
        wy = (fy - y0)[:, None]
        x0f = np.floor(u)
        wx = (u - x0f)[:, None]
        x0 = x0f.astype(np.int32) % Wt
        x1 = (x0 + 1) % Wt
        top = tex[y0, x0] * (1 - wx) + tex[y0, x1] * wx
        bot = tex[y1, x0] * (1 - wx) + tex[y1, x1] * wx
        return (top * (1 - wy) + bot * wy).astype(np.float32)

    def render(self, w, h, R, lon0, lat0, style, sun, fast=False):
        """-> (QImage, (x, y)) for the sphere, or None if there is no texture yet."""
        name = "holo" if style == "holo" else "sat"
        tex = (self.tex_fast if fast else self.tex).get(name)
        if tex is None:
            return None
        g = self._grid(w, h, R, lat0)
        if g is None:
            return None
        y0, y1, x0, x1 = g["box"]
        u = ((g["dlam"] + lon0) / TWO_PI + 0.5) * tex.shape[1]
        col = self._sample(tex, u, g["v"], fast)
        if sun is not None:
            slat, slon = math.radians(sun[0]), math.radians(sun[1])
            d = lon0 - slon
            nd = g["sphi"] * math.sin(slat) + g["cphi"] * math.cos(slat) * (g["cdl"] * math.cos(d) - g["sdl"] * math.sin(d))
            wgt = np.clip((nd + 0.12) / 0.24, 0, 1)
            wgt = (wgt * wgt * (3 - 2 * wgt))[:, None]
            night = self.tex.get("night")
            if style != "holo" and night is not None:
                col = self._sample(self.tex_fast.get("night", night) if fast else night, u, g["v"], fast) * (1 - wgt) * 0.95 + col * (0.16 + 0.84 * wgt) * wgt + col * 0.10 * (1 - wgt)
            else:
                col = col * (0.20 + 0.80 * wgt)
        rim = ((1 - g["Z"]) ** 2.2)[:, None]
        glow = np.array([40, 170, 255] if style == "holo" else [70, 140, 255], np.float32)
        col = np.clip(col + rim * glow * (1.0 if style == "holo" else 0.8), 0, 255)
        a = g["alpha"][:, None]
        out = np.zeros(((y1 - y0) * (x1 - x0), 4), np.uint8)
        out[g["idx"], :3] = (col * a).astype(np.uint8)
        out[g["idx"], 3] = (a[:, 0] * 255).astype(np.uint8)
        out = out.reshape(y1 - y0, x1 - x0, 4)
        img = QImage(out.data, x1 - x0, y1 - y0, (x1 - x0) * 4, QImage.Format.Format_RGBA8888_Premultiplied).copy()
        return img, (x0, y0)


# ─────────────────────────────────────────────────────────────────────────────
#  Map tiles
# ─────────────────────────────────────────────────────────────────────────────
class TileStore(QObject):
    tile_sig = pyqtSignal()

    def __init__(self, fetch: Callable = data.http_get, disk: bool = True):
        super().__init__()
        self._fetch = fetch
        self._disk = disk
        self._mem: "OrderedDict[tuple, QImage]" = OrderedDict()
        self._pending: set = set()
        self._failed: dict = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="tiles")

    def get(self, template: str, z: int, x: int, y: int) -> Optional[QImage]:
        key = (template, z, x, y)
        with self._lock:
            img = self._mem.get(key)
            if img is not None:
                self._mem.move_to_end(key)
                return img
            if key in self._pending or time.monotonic() < self._failed.get(key, 0):
                return None
            self._pending.add(key)
        self._pool.submit(self._load, key)
        return None

    def _path(self, key):
        t, z, x, y = key
        return data.CACHE / "tiles" / hashlib.md5(t.encode()).hexdigest()[:8] / str(z) / str(x) / f"{y}.bin"

    def _load(self, key):
        template, z, x, y = key
        img = None
        try:
            path = self._path(key)
            raw = path.read_bytes() if self._disk and path.exists() else None
            if raw is None:
                raw = self._fetch(template.format(z=z, x=x, y=y), 15)
                if self._disk:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
            img = QImage()
            if not img.loadFromData(raw):
                img = None
        except Exception:                                     # noqa: BLE001
            img = None
        with self._lock:
            self._pending.discard(key)
            if img is None:
                self._failed[key] = time.monotonic() + 45
            else:
                self._mem[key] = img
                while len(self._mem) > 420:
                    self._mem.popitem(last=False)
        if img is not None:
            self.tile_sig.emit()


# ─────────────────────────────────────────────────────────────────────────────
#  The widget
# ─────────────────────────────────────────────────────────────────────────────
def _col(h, a=255):
    c = QColor(h)
    c.setAlpha(a)
    return c


class EarthView(QWidget):
    data_sig = pyqtSignal(str, object)

    def __init__(self, hud, tile_fetch: Callable = data.http_get, now: Callable = geo.utcnow, auto_load: bool = True):
        super().__init__(hud)
        self.hud = hud
        self._now = now
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.lon0, self.lat0, self.z = 10.0, 22.0, 2.75
        self.style = "sat"
        self.layers = {"quakes": True, "aircraft": False, "satellites": True, "daynight": True, "labels": True}
        self.spin = True
        self.renderer = GlobeRenderer()
        self.tiles = TileStore(tile_fetch)
        self.tiles.tile_sig.connect(self._on_tile)
        self.data_sig.connect(self._on_data)
        self.countries: Optional[geo.CountryIndex] = None
        self.places: Optional[geo.Places] = None
        self._countries_raw: Optional[dict] = None
        self.quakes: list[dict] = []
        self.aircraft: list[dict] = []
        self.sats: list[tuple[str, object]] = []
        self.sat_now: dict[str, dict] = {}
        self.sat_tracks: dict[str, list] = {}
        self._track_at = 0.0
        self.selected: Optional[dict] = None
        self.arc: Optional[dict] = None
        self.status = "Preparing the globe…"
        self.errors: dict[str, str] = {}
        self._drag = None
        self._moved = False
        self._buttons: list = []
        self._flight: Optional[dict] = None
        self._globe_cache = None
        self._last_t = time.monotonic()
        self._fade = None
        self._stars = None
        self._last_air = 0.0
        self._last_quake = 0.0
        self._assets_started = False
        self.hud.installEventFilter(self)
        self.setGeometry(hud.rect())
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self.hide()
        if auto_load:
            self.start_loading()

    # ---- lifecycle -----------------------------------------------------------
    def eventFilter(self, obj, ev):
        if obj is self.hud and ev.type() == QEvent.Type.Resize:
            self.setGeometry(self.hud.rect())
        return False

    def open(self):
        self.setGeometry(self.hud.rect())
        self.hud._earth_cover = True
        self.show()
        self.raise_()
        self._timer.start()
        self.refresh_layers(force=True)
        self.update()

    def close_view(self):
        self.hud._earth_cover = False
        self._timer.stop()
        self.hide()
        self.hud.update()

    # ---- assets -----------------------------------------------------------------
    def start_loading(self):
        if self._assets_started:
            return
        self._assets_started = True
        threading.Thread(target=self._load_assets, daemon=True, name="earth-assets").start()

    def _load_assets(self):
        try:
            err = None if data.assets_ready() else data.download_assets(lambda m: self.data_sig.emit("status", m))
            if err:
                self.data_sig.emit("error", f"imagery: {err}")
                if not data.asset_path("countries").exists():
                    return
            out = {}
            countries = json.loads(data.asset_path("countries").read_text(encoding="utf-8"))
            out["countries_raw"] = countries
            out["countries"] = geo.CountryIndex(countries)
            if data.asset_path("places").exists():
                out["places"] = geo.Places(json.loads(data.asset_path("places").read_text(encoding="utf-8")))
            self.data_sig.emit("status", "Building the map…")
            out["holo"] = bake_holo(countries)
            if data.asset_path("sat").exists():
                sat = qimage_to_rgb(QImage(str(data.asset_path("sat"))))
                out["sat"] = bake_borders(sat, countries)
            if data.asset_path("night").exists():
                out["night"] = qimage_to_rgb(QImage(str(data.asset_path("night"))))
            self.data_sig.emit("assets", out)
        except Exception as e:                                # noqa: BLE001
            self.data_sig.emit("error", f"imagery: {e}")

    # ---- worker results (GUI thread) --------------------------------------------------
    @pyqtSlot()
    def _on_tile(self):
        self.update()

    @pyqtSlot(str, object)
    def _on_data(self, kind: str, payload):
        if kind == "assets":
            for k in ("sat", "night", "holo"):
                if k in payload:
                    self.renderer.set_texture(k, payload[k])
            self.countries, self._countries_raw = payload.get("countries"), payload.get("countries_raw")
            self.places = payload.get("places")
            self.status = ""
            self._globe_cache = None
        elif kind == "status":
            self.status = str(payload)
        elif kind == "error":
            self.errors[str(payload).split(":")[0]] = str(payload)
        elif kind == "quakes":
            self.quakes, self._last_quake = payload, time.monotonic()
            self.errors.pop("quakes", None)
        elif kind == "aircraft":
            self.aircraft = payload
            self.errors.pop("aircraft", None)
        elif kind == "sats":
            self.sats = payload
            self.errors.pop("satellites", None)
            self._update_sats(force_track=True)
        elif kind == "weather":
            key, w = payload
            if self.selected and self.selected.get("key") == key:
                self.selected["weather"] = w
        self.update()

    def _bg(self, name, fn, *a):
        def run():
            try:
                self.data_sig.emit(name, fn(*a))
            except Exception as e:                            # noqa: BLE001
                self.data_sig.emit("error", f"{name}: {str(e)[:70]}")
        threading.Thread(target=run, daemon=True, name=f"earth-{name}").start()

    def refresh_layers(self, force=False):
        now = time.monotonic()
        if self.layers["quakes"] and (force or now - self._last_quake > 600):
            self._last_quake = now
            self._bg("quakes", data.fetch_quakes, "week", "4.5")
        if self.layers["satellites"] and not self.sats and (force or "satellites" not in self.errors):
            self._bg("sats", self._load_sats)
        if self.layers["aircraft"] and self.z >= 3.1 and (force or now - self._last_air > 12):
            self._last_air = now
            la, lo = self.center_latlon()
            self._bg("aircraft", data.fetch_aircraft, la, lo, 250)

    @staticmethod
    def _load_sats():
        return [(n, geo.make_sat(a, b)) for n, a, b in data.fetch_tles("stations")[:12]]

    def _update_sats(self, force_track=False):
        now = self._now()
        for name, sat in self.sats:
            pos = geo.sat_position(sat, now)
            if pos:
                self.sat_now[name] = pos
        t = time.monotonic()
        if self.sats and (force_track or t - self._track_at > 30):
            self._track_at = t
            for name, sat in self.sats[:4]:
                self.sat_tracks[name] = geo.ground_track(sat, now, minutes=100, step_s=40)

    # ---- camera ------------------------------------------------------------------------
    def center_latlon(self):
        if self.z < GLOBE_MAX_Z:
            return self.lat0, geo.wrap_lon(self.lon0)
        return self.lat0, geo.wrap_lon(self.lon0)

    def _R(self):
        return 256.0 * 2 ** self.z / TWO_PI

    def target(self) -> tuple[float, float, float]:
        """Where the camera is heading: the end of a flight in progress, else where it is. Relative
        commands ("zoom in") must build on THIS, or a quick second command cancels the first trip."""
        f = self._flight
        return (f["la1"], f["lo1"], f["z1"]) if f else (self.lat0, self.lon0, self.z)

    def zoom_by(self, dz):
        if self._flight:
            self._flight["z1"] = max(Z_MIN, min(Z_MAX, self._flight["z1"] + dz))
        else:
            self.z = max(Z_MIN, min(Z_MAX, self.z + dz))
        self.spin = self.spin and self.z < 3.0
        self.update()

    def home(self):
        self.goto(22.0, 10.0, 2.75)

    def goto(self, lat, lon, z=None, animate=True):
        z = self.z if z is None else max(Z_MIN, min(Z_MAX, z))
        self.spin = False
        if not animate:
            self.lat0, self.lon0, self.z = lat, geo.wrap_lon(lon), z
            return
        dkm = geo.haversine_km(self.lat0, geo.wrap_lon(self.lon0), lat, lon)
        dl = ((lon - self.lon0 + 180) % 360) - 180
        self._flight = {"t0": time.monotonic(), "dur": 0.8 + min(1.2, dkm / 9000), "la0": self.lat0, "lo0": self.lon0,
                        "la1": lat, "lo1": self.lon0 + dl, "z0": self.z, "z1": z, "dip": 1.4 * min(1.0, dkm / 5000)}

    def select(self, lat, lon, name=None):
        lon = geo.wrap_lon(lon)
        country = self.countries.lookup(lat, lon) if self.countries else None
        city = self.places.nearest(lat, lon, 120) if self.places else None
        title = name or (city["name"] if city and city["km"] < 60 else None) or country or "Open ocean"
        sub = country if (title != country and country) else ""
        self.selected = {"lat": lat, "lon": lon, "title": title, "sub": sub, "key": (round(lat, 3), round(lon, 3)),
                         "weather": None, "t0": time.monotonic()}
        key = self.selected["key"]
        self._bg("weather", lambda: (key, data.fetch_weather(lat, lon)))
        self.update()

    def show_arc(self, a: dict, b: dict):
        self.arc = {"a": a, "b": b, "pts": geo.great_circle(a["lat"], a["lon"], b["lat"], b["lon"], 96),
                    "km": geo.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])}
        mid = self.arc["pts"][48]
        self.goto(mid[0], mid[1], max(Z_MIN, min(2.6, 3.4 - self.arc["km"] / 5000)))

    def snapshot(self) -> dict:
        la, lo = self.center_latlon()
        return {"lat": round(la, 2), "lon": round(lo, 2), "zoom": round(self.z, 2), "mode": "globe" if self.z < GLOBE_MAX_Z else "map",
                "style": self.style, "layers": dict(self.layers), "quakes": len(self.quakes), "aircraft": len(self.aircraft),
                "satellites": len(self.sat_now), "ready": bool(self.renderer.tex), "problems": dict(self.errors)}

    # ---- animation tick ------------------------------------------------------------------------
    def _tick(self):
        now = time.monotonic()
        dt = min(0.1, now - self._last_t)
        self._last_t = now
        f = self._flight
        if f:
            t = min(1.0, (now - f["t0"]) / f["dur"])
            e = t * t * (3 - 2 * t)
            self.lat0 = f["la0"] + (f["la1"] - f["la0"]) * e
            self.lon0 = f["lo0"] + (f["lo1"] - f["lo0"]) * e
            self.z = f["z0"] + (f["z1"] - f["z0"]) * e - f["dip"] * math.sin(math.pi * t)
            self.z = max(Z_MIN, min(Z_MAX, self.z))
            if t >= 1.0:
                self._flight = None
        elif self.spin and not self._drag and self.z < GLOBE_MAX_Z:
            self.lon0 += 5.0 * dt
        if self.sats:
            self._update_sats()
        if int(now * 2) != getattr(self, "_half", 0):
            self._half = int(now * 2)
            self.refresh_layers()
        self.update()

    # ---- projection ---------------------------------------------------------------------------------
    def _setup_proj(self):
        W, H = self.width(), self.height()
        self._cx, self._cy = W / 2.0, H / 2.0 + 6
        self._mode = "globe" if self.z < GLOBE_MAX_Z else "map"
        if self._mode == "globe":
            self._Rpx = self._R()
            self._l0, self._sl0, self._cl0 = math.radians(self.lon0), math.sin(math.radians(self.lat0)), math.cos(math.radians(self.lat0))
        else:
            self._mx0, self._my0 = geo.merc_x(geo.wrap_lon(self.lon0), self.z), geo.merc_y(self.lat0, self.z)

    def proj(self, lat, lon, min_depth=0.03):
        lat, lon = np.asarray(lat, np.float64), np.asarray(lon, np.float64)
        if self._mode == "globe":
            la, dl = np.radians(lat), np.radians(lon) - self._l0
            x = np.cos(la) * np.sin(dl)
            y = self._cl0 * np.sin(la) - self._sl0 * np.cos(la) * np.cos(dl)
            zz = self._sl0 * np.sin(la) + self._cl0 * np.cos(la) * np.cos(dl)
            return self._cx + self._Rpx * x, self._cy - self._Rpx * y, zz > min_depth
        n = 256.0 * 2 ** self.z
        dlon = ((lon - geo.wrap_lon(self.lon0) + 180) % 360) - 180
        mx = (geo.wrap_lon(self.lon0) + dlon + 180.0) / 360.0 * n
        s = np.sin(np.radians(np.clip(lat, -85.0511, 85.0511)))
        my = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
        sx, sy = self._cx + (mx - self._mx0), self._cy + (my - self._my0)
        return sx, sy, (sx > -80) & (sx < self.width() + 80) & (sy > -80) & (sy < self.height() + 80)

    def unproject(self, sx, sy):
        if self._mode == "globe":
            X, Y = (sx - self._cx) / self._Rpx, (self._cy - sy) / self._Rpx
            r2 = X * X + Y * Y
            if r2 > 1:
                return None
            Z = math.sqrt(1 - r2)
            lat = math.asin(max(-1, min(1, Z * self._sl0 + Y * self._cl0)))
            lon = self.lon0 + math.degrees(math.atan2(X, Z * self._cl0 - Y * self._sl0))
            return math.degrees(lat), geo.wrap_lon(lon)
        lat, lon = geo.inv_merc(self._mx0 + (sx - self._cx), self._my0 + (sy - self._cy), self.z)
        return lat, geo.wrap_lon(lon)

    # ---- painting ------------------------------------------------------------------------------------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        self._setup_proj()
        self._buttons = []
        try:
            self._paint_scene(p, W, H)
            self._paint_chrome(p, W, H)
        finally:
            p.end()

    def _paint_scene(self, p, W, H):
        from core import orb_hud
        p.fillRect(0, 0, W, H, QColor("#020817"))
        if self._stars is None or self._stars[0] != (W, H):
            self._stars = ((W, H), orb_hud._starfield(W, H)[0])
        if self._mode == "globe":
            p.drawPixmap(0, 0, self._stars[1])
            self._paint_globe(p, W, H)
        else:
            self._paint_map(p, W, H)
        self._paint_overlays(p, W, H)
        if self._fade and time.monotonic() - self._fade[1] < 0.35:
            p.setOpacity(1 - (time.monotonic() - self._fade[1]) / 0.35)
            p.drawPixmap(0, 0, self._fade[0])
            p.setOpacity(1)

    def _paint_globe(self, p, W, H):
        R = self._Rpx
        halo = QRadialGradient(QPointF(self._cx, self._cy), R * 1.22)
        halo.setColorAt(0.80, _col("#2f8cff", 0)); halo.setColorAt(0.86, _col("#3fa0ff", 120)); halo.setColorAt(1.0, _col("#2f8cff", 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(self._cx, self._cy), R * 1.22, R * 1.22)
        if not self.renderer.tex:
            p.setBrush(_col("#06285a"))
            p.setPen(QPen(_col("#3fa0ff", 200), 1.5))
            p.drawEllipse(QPointF(self._cx, self._cy), R, R)
            return
        moving = bool(self._drag or self._flight or self.spin)
        sun = geo.sun_subpoint(self._now()) if self.layers["daynight"] else None
        q = 0.55 if moving else 1.0
        key = (W, H, round(R, 1), round(self.lon0, 2), round(self.lat0, 2), self.style, int(time.time() // 30) if sun else 0, self.renderer.version, moving)
        if self._globe_cache and self._globe_cache[0] == key:
            img, org, qq = self._globe_cache[1:]
        else:
            res = self.renderer.render(int(W * q), int(H * q), R * q, math.radians(self.lon0), math.radians(self.lat0), self.style, sun, fast=moving)
            if res is None:
                return
            img, org, qq = res[0], res[1], q
            self._globe_cache = (key, img, org, qq)
        p.save()
        p.translate(0, 6 * 0)
        p.scale(1 / qq, 1 / qq)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawImage(QPointF(org[0], org[1] + (6 * qq)), img)
        p.restore()

    def _paint_map(self, p, W, H):
        base = TILE_STYLES[self.style]
        p.fillRect(0, 0, W, H, QColor("#04122c" if self.style == "holo" else "#0a1a30"))
        zi = int(max(2, min(17, round(self.z))))
        scale = 2 ** (self.z - zi)
        ts = 256.0 * scale
        cxm, cym = geo.merc_x(geo.wrap_lon(self.lon0), zi), geo.merc_y(self.lat0, zi)
        nt = 2 ** zi
        half_w, half_h = self._cx / scale, self._cy / scale
        tx0, tx1 = int(math.floor((cxm - half_w) / 256.0)), int(math.floor((cxm + half_w) / 256.0))
        ty0, ty1 = max(0, int(math.floor((cym - half_h) / 256.0))), min(nt - 1, int(math.floor((cym + half_h) / 256.0)))
        for layer in (base["base"], base["over"]):
            if not layer:
                continue
            for ty in range(ty0, ty1 + 1):
                for tx in range(tx0, tx1 + 1):
                    img = self.tiles.get(layer, zi, tx % nt, ty)
                    if img is None:
                        continue
                    sx = self._cx + (tx * 256.0 - cxm) * scale
                    sy = self._cy + (ty * 256.0 - cym) * scale
                    p.drawImage(QRectF(sx, sy, ts + 0.6, ts + 0.6), img)
        if self.layers["daynight"]:
            self._paint_night_map(p, W, H)
        self._paint_outlines(p)

    def _paint_night_map(self, p, W, H):
        sun = geo.sun_subpoint(self._now())
        gw, gh = 64, 40
        xs, ys = np.meshgrid(np.linspace(0, W, gw), np.linspace(0, H, gh))
        n = 256.0 * 2 ** self.z
        lon = (self._mx0 + (xs - self._cx)) / n * 360 - 180
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * (self._my0 + (ys - self._cy)) / n))))
        nd = (np.sin(np.radians(lat)) * math.sin(math.radians(sun[0])) +
              np.cos(np.radians(lat)) * math.cos(math.radians(sun[0])) * np.cos(np.radians(lon - sun[1])))
        w = np.clip((nd + 0.12) / 0.24, 0, 1)
        alpha = ((1 - w * w * (3 - 2 * w)) * 150).astype(np.uint8)
        arr = np.zeros((gh, gw, 4), np.uint8)
        arr[..., 2] = 18
        arr[..., 3] = alpha
        arr[..., :3] = (arr[..., :3].astype(np.uint16) * alpha[..., None] // 255).astype(np.uint8)
        img = QImage(arr.data, gw, gh, gw * 4, QImage.Format.Format_RGBA8888_Premultiplied).copy()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawImage(QRectF(0, 0, W, H), img)

    def _paint_outlines(self, p):
        """Vector borders, so the map has structure before (or without) tiles."""
        if not self._countries_raw:
            return
        p.setPen(QPen(_col("#bfe9ff", 110), 1.1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for f in self._countries_raw.get("features", []):
            for r in _rings_of(f):
                a = np.asarray(r, np.float64)
                sx, sy, vis = self.proj(a[:, 1], a[:, 0])
                if not vis.any():
                    continue
                path = QPainterPath()
                pen_down = False
                for x, y, v, prev_x in zip(sx, sy, vis, np.concatenate(([sx[0]], sx[:-1]))):
                    if v and not (pen_down and abs(x - prev_x) > self.width()):
                        path.lineTo(x, y) if pen_down else path.moveTo(x, y)
                        pen_down = True
                    else:
                        pen_down = False
                p.drawPath(path)

    # ---- layers ---------------------------------------------------------------------------------------------------
    def _runs(self, sx, sy, vis):
        path = QPainterPath()
        down = False
        for i in range(len(sx)):
            if vis[i] and not (down and abs(sx[i] - sx[i - 1]) > self.width() * 0.6):
                path.lineTo(sx[i], sy[i]) if down else path.moveTo(sx[i], sy[i])
                down = True
            else:
                down = False
        return path

    def _paint_overlays(self, p, W, H):
        if self.layers["labels"]:
            self._paint_labels(p, W, H)
        if self.arc:
            a = np.asarray(self.arc["pts"])
            sx, sy, vis = self.proj(a[:, 0], a[:, 1])
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_col("#7fe6ff", 90), 5))
            p.drawPath(self._runs(sx, sy, vis))
            p.setPen(QPen(_col("#bff4ff", 240), 1.8, Qt.PenStyle.DashLine))
            p.drawPath(self._runs(sx, sy, vis))
            f = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
            p.setFont(f)
            for end in ("a", "b"):
                x, y, v = self.proj([self.arc[end]["lat"]], [self.arc[end]["lon"]])
                if v[0]:
                    p.setBrush(_col("#ffffff")); p.setPen(Qt.PenStyle.NoPen)
                    p.drawEllipse(QPointF(x[0], y[0]), 4, 4)
                    p.setPen(_col("#e8fbff")); p.drawText(QPointF(x[0] + 8, y[0] - 6), self.arc[end]["name"])
            mx, my, mv = self.proj([self.arc["pts"][48][0]], [self.arc["pts"][48][1]])
            if mv[0]:
                p.setPen(_col("#ffe28a")); p.drawText(QPointF(mx[0] + 8, my[0] + 16), f"{self.arc['km']:,.0f} km".replace(",", " "))
        if self.layers["satellites"]:
            for name, segs in self.sat_tracks.items():
                p.setBrush(Qt.BrushStyle.NoBrush)
                for seg in segs:
                    a = np.asarray(seg)
                    sx, sy, vis = self.proj(a[:, 0], a[:, 1])
                    p.setPen(QPen(_col("#d6f6ff", 235), 1.6))
                    p.drawPath(self._runs(sx, sy, vis))
            f = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
            p.setFont(f)
            for name, pos in self.sat_now.items():
                x, y, v = self.proj([pos["lat"]], [pos["lon"]])
                if not v[0]:
                    continue
                p.setPen(QPen(_col("#ffffff"), 1.4)); p.setBrush(_col("#38b6ff", 230))
                p.save(); p.translate(x[0], y[0]); p.rotate(45); p.drawRect(QRectF(-4, -4, 8, 8)); p.restore()
                p.setPen(_col("#d8f6ff")); p.drawText(QPointF(x[0] + 9, y[0] + 4), name.split("(")[0].strip()[:14])
        if self.layers["quakes"] and self.quakes:
            la = np.array([q["lat"] for q in self.quakes]); lo = np.array([q["lon"] for q in self.quakes])
            sx, sy, vis = self.proj(la, lo)
            f = QFont("Segoe UI", 7, QFont.Weight.Bold)
            p.setFont(f)
            for q, x, y, v in zip(self.quakes, sx, sy, vis):
                if not v:
                    continue
                r = max(3.5, min(15.0, 3.5 + (q["mag"] - 4.2) * 3.6))
                g = QRadialGradient(QPointF(x, y), r * 2.4)
                g.setColorAt(0, _col("#ffb020", 150)); g.setColorAt(1, _col("#ffb020", 0))
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(g)); p.drawEllipse(QPointF(x, y), r * 2.4, r * 2.4)
                p.setBrush(_col("#ffb21c", 235)); p.setPen(QPen(_col("#fff1c8", 230), 1.0)); p.drawEllipse(QPointF(x, y), r, r)
                if self.z > 2.9 or q["mag"] >= 6.0:
                    p.setPen(_col("#ffe9b8")); p.drawText(QPointF(x + r + 3, y + 3), f"{q['mag']:.1f}")
        if self.layers["aircraft"] and self.aircraft and self.z >= 3.1:
            ac = [a for a in self.aircraft if not a["ground"]][:1400]
            if ac:
                sx, sy, vis = self.proj([a["lat"] for a in ac], [a["lon"] for a in ac])
                tri = QPolygonF([QPointF(0, -7), QPointF(5, 6), QPointF(0, 3), QPointF(-5, 6)])
                p.setPen(QPen(_col("#3b6ea8", 200), 0.8)); p.setBrush(_col("#ffffff", 235))
                f = QFont("Segoe UI", 7)
                p.setFont(f)
                for a, x, y, v in zip(ac, sx, sy, vis):
                    if not v:
                        continue
                    p.save(); p.translate(x, y); p.rotate(a["track"]); p.drawPolygon(tri); p.restore()
                    if self.z >= 5.4 and a["call"]:
                        p.setPen(_col("#e4f4ff", 220)); p.drawText(QPointF(x + 7, y + 3), a["call"])
                        p.setPen(QPen(_col("#3b6ea8", 200), 0.8))
        if self.selected:
            s = self.selected
            x, y, v = self.proj([s["lat"]], [s["lon"]])
            if v[0]:
                ph = ((time.monotonic() - s["t0"]) * 1.4) % 1.0
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(_col("#ffffff", int(220 * (1 - ph))), 1.6)); p.drawEllipse(QPointF(x[0], y[0]), 6 + ph * 22, 6 + ph * 22)
                p.setPen(QPen(_col("#ffffff"), 2)); p.setBrush(_col("#35c2ff")); p.drawEllipse(QPointF(x[0], y[0]), 5, 5)
                p.setPen(_col("#ffffff")); f = QFont("Segoe UI", 9, QFont.Weight.Bold); p.setFont(f)
                p.drawText(QPointF(x[0] + 12, y[0] + 4), s["title"])

    def _paint_labels(self, p, W, H):
        """Cities take priority over country names; a label never lands where another already is,
        and nothing is labelled near the edge of the globe where it would be squashed."""
        occupied: set = set()

        def free(x, y):
            k = (int(x // 96), int(y // 20))
            if k in occupied:
                return False
            occupied.add(k)
            return True
        depth = 0.30 if self._mode == "globe" else -1.0
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        if self.places and self.z >= 3.1:
            maxrank = 1 if self.z < 3.5 else 3 if self.z < 4.4 else 5 if self.z < 6 else 10
            for it in self.places.items:
                if it["rank"] > maxrank:
                    continue
                x, y, v = self.proj([it["lat"]], [it["lon"]], depth)
                if v[0] and 10 < x[0] < W - 10 and 56 < y[0] < H - 50 and free(x[0], y[0]):
                    p.setPen(Qt.PenStyle.NoPen); p.setBrush(_col("#ffffff", 235)); p.drawEllipse(QPointF(x[0], y[0]), 2.2, 2.2)
                    p.setPen(_col("#ffffff", 240)); p.drawText(QPointF(x[0] + 6, y[0] + 3), it["name"])
        if self.countries and 2.5 <= self.z < 5.2:
            p.setPen(_col("#e9f6ff", 175))
            min_area = 150 if self.z < 3.1 else 40 if self.z < 3.8 else 8
            for it in sorted(self.countries.items, key=lambda c: -c["area"]):
                if it["area"] < min_area:
                    continue
                lat, lon = it["centroid"]
                x, y, v = self.proj([lat], [lon], max(depth, 0.42) if self._mode == "globe" else -1.0)
                name = it["name"].upper() if self.z < 3.4 else it["name"]
                if v[0] and 20 < x[0] < W - 20 and 56 < y[0] < H - 50 and free(x[0], y[0]):
                    p.drawText(QPointF(x[0] - len(name) * 2.7, y[0]), name)

    # ---- chrome -----------------------------------------------------------------------------------------------------------
    def _card(self, p, rect, accent="#3fa0ff"):
        p.setPen(QPen(_col(accent, 200), 1.2)); p.setBrush(_col("#030d22", 214)); p.drawRoundedRect(rect, 7, 7)

    def _paint_chrome(self, p, W, H):
        from core import orb_hud
        accent, label, _s, _m = orb_hud._state(self.hud)
        f = QFont("Courier New", 8, QFont.Weight.Bold); p.setFont(f)
        pw = max(104.0, p.fontMetrics().horizontalAdvance(label) + 44.0)
        pill = QRectF(W / 2 - pw / 2, 10, pw, 22)
        p.setBrush(_col("#020a1e", 210)); p.setPen(QPen(_col(accent, 210), 1.2)); p.drawRoundedRect(pill, 8, 8)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(_col(accent, 120 + int(135 * (0.55 + 0.45 * math.sin(time.monotonic() * 4)))))
        p.drawEllipse(QPointF(pill.left() + 14, pill.center().y()), 3.2, 3.2)
        p.setPen(_col(accent)); p.drawText(QRectF(pill.left() + 22, pill.top(), pill.width() - 24, pill.height()), Qt.AlignmentFlag.AlignVCenter, label)

        # camera card
        la, lo = self.center_latlon()
        now = self._now()
        card = QRectF(14, 44, 196, 76)
        self._card(p, card)
        p.setPen(_col("#ffffff")); p.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        loc = now + timedelta(hours=lo / 15.0)
        p.drawText(QPointF(card.left() + 10, card.top() + 26), f"{loc:%H:%M} UTC{self._utc_label(now)}")
        p.setFont(QFont("Consolas", 8)); p.setPen(_col("#bcd8f0"))
        p.drawText(QPointF(card.left() + 10, card.top() + 43), f"{loc:%Y-%m-%d}")
        p.drawText(QPointF(card.left() + 10, card.top() + 57), f"● {la:.4f}, {lo:.4f}")
        p.drawText(QPointF(card.left() + 10, card.top() + 70), f"↕ {geo.altitude_km(self.z):,.0f} km".replace(",", " "))

        # place card
        if self.selected:
            s = self.selected
            w = s.get("weather")
            rect = QRectF(W - 262, 44, 236, 150 if w else 62)
            self._card(p, rect)
            p.setPen(_col("#ffffff")); p.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
            p.drawText(QPointF(rect.left() + 12, rect.top() + 25), s["title"][:22])
            p.setFont(QFont("Segoe UI", 8)); p.setPen(_col("#9ec4e6"))
            p.drawText(QPointF(rect.left() + 12, rect.top() + 42), (s["sub"] or f"{s['lat']:.2f}, {s['lon']:.2f}")[:34])
            if w:
                off = timedelta(seconds=w.get("offset_s") or 0)
                p.setPen(_col("#ffffff")); p.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
                p.drawText(QPointF(rect.left() + 12, rect.top() + 64), f"{(now + off):%H:%M}  {w.get('tz', '')}")
                p.setFont(QFont("Segoe UI", 9)); p.setPen(_col("#dff1ff"))
                desc = geo.WEATHER_CODES.get(w.get("code"), "")
                rows = [f"🌡 {w['temp']:.1f}°C   feels {w['feels']:.1f}°C" if w.get("temp") is not None else "weather unavailable",
                        f"💧 {w['humidity']}%   🌬 {w['wind']} km/h" if w.get("humidity") is not None else "", desc.capitalize(),
                        f"☀ {str(w['sunrise'])[-5:]}   ☾ {str(w['sunset'])[-5:]}" if w.get("sunrise") else ""]
                for i, r in enumerate(rows):
                    p.drawText(QPointF(rect.left() + 12, rect.top() + 86 + i * 16), r)
            else:
                p.setFont(QFont("Segoe UI", 8)); p.setPen(_col("#7fa6c8")); p.drawText(QPointF(rect.left() + 12, rect.top() + 56), "loading local time and weather…")
            self._buttons.append((QRectF(rect.right() - 22, rect.top() + 4, 18, 18), "deselect"))
            p.setPen(_col("#9ec4e6")); p.setFont(QFont("Segoe UI", 10)); p.drawText(QRectF(rect.right() - 22, rect.top() + 4, 18, 18), Qt.AlignmentFlag.AlignCenter, "×")

        # controls
        keys = [("zoom_in", "+"), ("zoom_out", "−"), ("home", "⌂"), None, ("layer:quakes", "◉"), ("layer:aircraft", "✈"),
                ("layer:satellites", "◈"), ("layer:daynight", "☀"), ("layer:labels", "Aa"), ("style", "◐"), None, ("close", "×")]
        y = H * 0.30
        for k in keys:
            if k is None:
                y += 12
                continue
            r = QRectF(W - 40, y, 28, 28)
            on = k[0].startswith("layer:") and self.layers.get(k[0][6:], False)
            p.setPen(QPen(_col("#7fd4ff" if on or not k[0].startswith("layer:") else "#3a5f86", 230), 1.4))
            p.setBrush(_col("#1a8cff" if on else "#06142e", 235 if on else 210)); p.drawEllipse(r)
            p.setPen(_col("#ffffff" if on or not k[0].startswith("layer:") else "#7f9fc0")); p.setFont(QFont("Segoe UI Symbol", 10, QFont.Weight.Bold))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, k[1])
            self._buttons.append((r, k[0]))
            y += 34

        # legend + attribution
        chips = []
        if self.layers["quakes"] and self.quakes:
            m = [q["mag"] for q in self.quakes]; chips.append(f"●  {len(m)} quakes  M{min(m):.1f}–{max(m):.1f}")
        if self.layers["aircraft"]:
            chips.append(f"✈  {len([a for a in self.aircraft if not a['ground']])} aircraft" if self.z >= 3.1 else "✈  zoom in for aircraft")
        if self.layers["satellites"] and self.sat_now:
            chips.append(f"◈  {len(self.sat_now)} satellites")
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        x = 16
        for c in chips:
            wc = p.fontMetrics().horizontalAdvance(c) + 20
            r = QRectF(x, H - 38, wc, 22); self._card(p, r, "#2a6fb8"); p.setPen(_col("#e6f4ff")); p.drawText(r, Qt.AlignmentFlag.AlignCenter, c); x += wc + 8
        if self.status or self.errors:
            p.setPen(_col("#ffd27a")); p.setFont(QFont("Segoe UI", 8))
            msg = self.status or ("Live data unavailable: " + ", ".join(sorted(self.errors)) + "  (is the internet on?)")
            p.drawText(QPointF(16, H - 48 if chips else H - 16), msg[:120])
        p.setFont(QFont("Segoe UI", 7)); p.setPen(_col("#7f9fc0", 190))
        attr = TILE_STYLES[self.style]["attrib"] if self._mode == "map" else "NASA Blue Marble · Natural Earth · USGS · CelesTrak · adsb.lol · Open-Meteo"
        p.drawText(QRectF(0, H - 18, W - 12, 14), Qt.AlignmentFlag.AlignRight, attr)

    def _utc_label(self, now):
        la, lo = self.center_latlon()
        h = round(lo / 15.0)
        return f"{h:+03d}:00"

    # ---- input --------------------------------------------------------------------------------------------------------------------
    def _press_button(self, key: str):
        if key == "zoom_in":
            self.zoom_by(0.6)
        elif key == "zoom_out":
            self.zoom_by(-0.6)
        elif key == "home":
            self.home()
        elif key == "close":
            self.close_view()
        elif key == "deselect":
            self.selected = None
        elif key == "style":
            self.style = "holo" if self.style == "sat" else "sat"
            self._globe_cache = None
        elif key.startswith("layer:"):
            name = key[6:]
            self.layers[name] = not self.layers[name]
            self.refresh_layers(force=True)
        self.update()

    def mousePressEvent(self, ev):
        pt = ev.position()
        for r, key in self._buttons:
            if r.contains(pt):
                self._press_button(key)
                return
        self._drag = (pt, self.lon0, self.lat0, self._mx0 if self._mode == "map" else 0.0, self._my0 if self._mode == "map" else 0.0, self._mode)
        self._moved = False
        self._flight = None

    def mouseMoveEvent(self, ev):
        if not self._drag:
            return
        pt0, lon0, lat0, mx0, my0, mode = self._drag
        dx, dy = ev.position().x() - pt0.x(), ev.position().y() - pt0.y()
        if abs(dx) + abs(dy) > 4:
            self._moved = True
        if not self._moved:
            return
        self.spin = False
        if mode == "globe":
            k = math.degrees(1.0 / self._R())
            self.lon0 = geo.wrap_lon(lon0 - dx * k / max(0.25, math.cos(math.radians(lat0))))
            self.lat0 = max(-85.0, min(85.0, lat0 + dy * k))
        else:
            la, lo = geo.inv_merc(mx0 - dx, my0 - dy, self.z)
            self.lat0, self.lon0 = max(-84.0, min(84.0, la)), lo
        self.update()

    def mouseReleaseEvent(self, ev):
        if self._drag and not self._moved:
            hit = self.unproject(ev.position().x(), ev.position().y())
            if hit:
                self.select(*hit)
        self._drag = None

    def mouseDoubleClickEvent(self, ev):
        hit = self.unproject(ev.position().x(), ev.position().y())
        if hit:
            self.select(*hit)
            self.goto(hit[0], hit[1], min(Z_MAX, self.z + 1.4))

    def wheelEvent(self, ev):
        self.zoom_by(ev.angleDelta().y() / 120.0 * 0.3)
        ev.accept()
