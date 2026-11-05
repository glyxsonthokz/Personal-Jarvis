"""
core/earth_data.py — everything the Earth view fetches. Standard library only.

Free, key-less sources:
    imagery / outlines   NASA Blue Marble + Natural Earth, via GitHub raw (one-off, ~3 MB)
    earthquakes          USGS summary feeds
    aircraft             adsb.lol community ADS-B (up to 250 nautical miles around a point)
    weather / local time Open-Meteo
    place search         Open-Meteo geocoding
    satellite elements   CelesTrak (cached 4 h; the last copy is reused if the network is down)
    map tiles            Esri World Imagery / CARTO dark (see earth_view.TILE_STYLES)

Every function raises on failure; callers decide how to degrade. Nothing here ever blocks the
GUI: they are called from worker threads.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from core import earth_geo as geo

_BASE = Path(__file__).resolve().parent.parent
CACHE = _BASE / "cache" / "earth"
UA = "Jarvis-Earth/1.0 (personal desktop assistant)"

_RAW = "https://raw.githubusercontent.com"
ASSETS = {
    "sat": ("earth-blue-marble.jpg", f"{_RAW}/vasturiano/three-globe/master/example/img/earth-blue-marble.jpg"),
    "night": ("earth-night.jpg", f"{_RAW}/vasturiano/three-globe/master/example/img/earth-night.jpg"),
    "countries": ("countries.geojson", f"{_RAW}/vasturiano/globe.gl/master/example/datasets/ne_110m_admin_0_countries.geojson"),
    "places": ("places.geojson", f"{_RAW}/nvkelso/natural-earth-vector/master/geojson/ne_110m_populated_places_simple.geojson"),
}


def asset_path(key: str) -> Path:
    return CACHE / ASSETS[key][0]


def assets_ready() -> bool:
    return all(asset_path(k).exists() and asset_path(k).stat().st_size > 10_000 for k in ASSETS)


def http_get(url: str, timeout: float = 15.0, headers: Optional[dict] = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url: str, timeout: float = 15.0) -> dict:
    return json.loads(http_get(url, timeout).decode("utf-8"))


def download_assets(progress: Optional[Callable[[str], None]] = None, fetch=http_get) -> Optional[str]:
    """Fetch whatever is missing. Returns an error string, or None when everything is there."""
    CACHE.mkdir(parents=True, exist_ok=True)
    for key, (name, url) in ASSETS.items():
        path = CACHE / name
        if path.exists() and path.stat().st_size > 10_000:
            continue
        try:
            if progress:
                progress(f"downloading {name}")
            data = fetch(url, 120)
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        except Exception as e:                                # noqa: BLE001
            return f"{name}: {e}"
    return None


# ─────────────────────────────────────────────────────────────────────────────
def fetch_quakes(window: str = "week", min_mag: str = "4.5", fetch=http_json) -> list[dict]:
    window = window if window in ("hour", "day", "week", "month") else "week"
    min_mag = min_mag if min_mag in ("significant", "4.5", "2.5", "1.0", "all") else "4.5"
    url = f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{min_mag}_{window}.geojson"
    return geo.parse_usgs(fetch(url))


def fetch_aircraft(lat: float, lon: float, radius_nm: int = 250, fetch=http_json) -> list[dict]:
    radius_nm = max(5, min(250, int(radius_nm)))
    return geo.parse_adsb(fetch(f"https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius_nm}"))


def fetch_weather(lat: float, lon: float, fetch=http_json) -> dict:
    q = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": "auto", "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
        "daily": "sunrise,sunset"})
    return geo.parse_weather(fetch(f"https://api.open-meteo.com/v1/forecast?{q}"))


def geocode(name: str, fetch=http_json) -> Optional[dict]:
    q = urllib.parse.urlencode({"name": name, "count": 1, "language": "en", "format": "json"})
    return geo.parse_geocode(fetch(f"https://geocoding-api.open-meteo.com/v1/search?{q}"))


def fetch_tles(group: str = "stations", max_age_s: float = 4 * 3600, fetch=http_get) -> list[tuple[str, str, str]]:
    group = "".join(c for c in group if c.isalnum() or c in "-_") or "stations"
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"tle-{group}.txt"
    fresh = path.exists() and time.time() - path.stat().st_mtime < max_age_s
    if not fresh:
        try:
            text = fetch(f"https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle", 20).decode("utf-8", "replace")
            if geo.parse_tles(text):
                path.write_text(text, encoding="utf-8")
        except Exception:                                     # noqa: BLE001
            if not path.exists():
                raise                                         # nothing cached either: let the caller say so
    return geo.parse_tles(path.read_text(encoding="utf-8"))
