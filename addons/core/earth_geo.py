"""
core/earth_geo.py — the maths and parsing behind the Earth view. No Qt, no network.

Everything here is a plain function of its inputs so it can be checked against known
answers: where the sun is, how far apart two cities are, where a satellite is and when it
next passes overhead, which country a point is in, and what the data feeds' JSON means.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

R_EARTH_KM = 6371.0088
_A = 6378.137                       # WGS84 equatorial radius, km
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
#  Time and the sun
# ─────────────────────────────────────────────────────────────────────────────
def julian_day(dt: datetime) -> float:
    dt = dt.astimezone(timezone.utc)
    y, m = dt.year, dt.month
    if m <= 2:
        y, m = y - 1, m + 12
    a = y // 100
    b = 2 - a + a // 4
    day = dt.day + (dt.hour + (dt.minute + (dt.second + dt.microsecond / 1e6) / 60) / 60) / 24
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + day + b - 1524.5


def gmst_deg(jd: float) -> float:
    return (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360.0


def sun_subpoint(dt: datetime) -> tuple[float, float]:
    """(latitude, longitude) in degrees of the point on Earth with the sun straight overhead.
    Low-precision (about 0.01 degree), which is far finer than a screen can show."""
    jd = julian_day(dt)
    n = jd - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360
    g = math.radians((357.528 + 0.9856003 * n) % 360)
    lam = math.radians(L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.0000004 * n)
    dec = math.asin(math.sin(eps) * math.sin(lam))
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    lon = (math.degrees(ra) - gmst_deg(jd) + 180) % 360 - 180
    return math.degrees(dec), lon


# ─────────────────────────────────────────────────────────────────────────────
#  Distances, bearings, great circles
# ─────────────────────────────────────────────────────────────────────────────
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(b: float) -> str:
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][int((b % 360) / 22.5 + 0.5) % 16]


def great_circle(lat1, lon1, lat2, lon2, n: int = 64) -> list[tuple[float, float]]:
    """n+1 points along the shortest path, as (lat, lon)."""
    def vec(la, lo):
        la, lo = math.radians(la), math.radians(lo)
        return (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))
    a, b = vec(lat1, lon1), vec(lat2, lon2)
    d = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b)))))
    out = []
    for i in range(n + 1):
        t = i / n
        if d < 1e-9:
            p = a
        else:
            s0, s1 = math.sin((1 - t) * d) / math.sin(d), math.sin(t * d) / math.sin(d)
            p = tuple(s0 * x + s1 * y for x, y in zip(a, b))
        out.append((math.degrees(math.asin(max(-1, min(1, p[2])))), math.degrees(math.atan2(p[1], p[0]))))
    return out


def wrap_lon(lon: float) -> float:
    return (lon + 180) % 360 - 180


# ─────────────────────────────────────────────────────────────────────────────
#  Web Mercator (the tile maps)
# ─────────────────────────────────────────────────────────────────────────────
def merc_x(lon: float, z: float) -> float:
    return (lon + 180.0) / 360.0 * 256.0 * 2 ** z


def merc_y(lat: float, z: float) -> float:
    lat = max(-85.0511, min(85.0511, lat))
    s = math.sin(math.radians(lat))
    return (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * 256.0 * 2 ** z


def inv_merc(x: float, y: float, z: float) -> tuple[float, float]:
    n = 256.0 * 2 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def altitude_km(z: float) -> float:
    """The "height of the camera" shown on the card; 12 742 km (one Earth diameter) at zoom 3."""
    return 12742.0 * 2 ** (3.0 - z)


# ─────────────────────────────────────────────────────────────────────────────
#  Satellites (SGP4)
# ─────────────────────────────────────────────────────────────────────────────
def parse_tles(text: str) -> list[tuple[str, str, str]]:
    """Three-line element sets: name, line 1, line 2."""
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    out = []
    i = 0
    while i + 2 < len(lines) + 0:
        if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            out.append((lines[i].strip(), lines[i + 1], lines[i + 2]))
            i += 3
        else:
            i += 1
    return out


def make_sat(l1: str, l2: str):
    from sgp4.api import Satrec
    return Satrec.twoline2rv(l1, l2)


def sat_ecef(sat, dt: datetime) -> Optional[tuple[float, float, float, float]]:
    """Position in the Earth-fixed frame (km) and speed (km/s), or None if SGP4 gives up."""
    from sgp4.api import jday
    dt = dt.astimezone(timezone.utc)
    jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second + dt.microsecond / 1e6)
    err, r, v = sat.sgp4(jd, fr)
    if err != 0:
        return None
    th = math.radians(gmst_deg(jd + fr))
    c, s = math.cos(th), math.sin(th)
    x, y, z = r
    return (x * c + y * s, -x * s + y * c, z, math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2))


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float, float]:
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - _E2))
    alt = 0.0
    for _ in range(5):
        n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
        alt = p / max(1e-12, math.cos(lat)) - n
        lat = math.atan2(z, p * (1 - _E2 * n / (n + alt)))
    return math.degrees(lat), math.degrees(lon), alt


def sat_position(sat, dt: datetime) -> Optional[dict]:
    e = sat_ecef(sat, dt)
    if e is None:
        return None
    lat, lon, alt = ecef_to_geodetic(*e[:3])
    return {"lat": lat, "lon": lon, "alt_km": alt, "speed_kms": e[3]}


def ground_track(sat, dt: datetime, minutes: float = 95, step_s: float = 30,
                 back_minutes: float = 0) -> list[list[tuple[float, float]]]:
    """The path over the ground as segments, split wherever it crosses the date line."""
    segs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    t = -back_minutes * 60.0
    last_lon = None
    while t <= minutes * 60.0:
        pos = sat_position(sat, dt + timedelta(seconds=t))
        t += step_s
        if pos is None:
            continue
        if last_lon is not None and abs(pos["lon"] - last_lon) > 180:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
        cur.append((pos["lat"], pos["lon"]))
        last_lon = pos["lon"]
    if len(cur) > 1:
        segs.append(cur)
    return segs


def look_angles(sat, dt: datetime, lat: float, lon: float) -> Optional[tuple[float, float, float]]:
    """(elevation, azimuth, range km) of the satellite from a spot on the ground."""
    e = sat_ecef(sat, dt)
    if e is None:
        return None
    la, lo = math.radians(lat), math.radians(lon)
    n = _A / math.sqrt(1 - _E2 * math.sin(la) ** 2)
    ox, oy, oz = n * math.cos(la) * math.cos(lo), n * math.cos(la) * math.sin(lo), n * (1 - _E2) * math.sin(la)
    dx, dy, dz = e[0] - ox, e[1] - oy, e[2] - oz
    east = -math.sin(lo) * dx + math.cos(lo) * dy
    north = -math.sin(la) * math.cos(lo) * dx - math.sin(la) * math.sin(lo) * dy + math.cos(la) * dz
    up = math.cos(la) * math.cos(lo) * dx + math.cos(la) * math.sin(lo) * dy + math.sin(la) * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    return math.degrees(math.atan2(up, math.hypot(east, north))), (math.degrees(math.atan2(east, north)) + 360) % 360, rng


def next_pass(sat, lat: float, lon: float, start: datetime, hours: float = 36, min_el: float = 10.0) -> Optional[dict]:
    """The next time the satellite climbs above `min_el` degrees from this spot."""
    step = 20.0
    t = 0.0
    rise = None
    peak = (-90.0, None, 0.0)
    while t <= hours * 3600:
        when = start + timedelta(seconds=t)
        la = look_angles(sat, when, lat, lon)
        t += step
        if la is None:
            continue
        el, az, rng = la
        if el >= min_el:
            if rise is None:
                rise = (when, az)
            if el > peak[0]:
                peak = (el, when, az)
        elif rise is not None:
            return {"rise": rise[0], "rise_az": rise[1], "peak": peak[1], "max_el": peak[0],
                    "set": when, "set_az": az, "minutes": (when - rise[0]).total_seconds() / 60}
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Countries and cities (Natural Earth)
# ─────────────────────────────────────────────────────────────────────────────
def _rings(geom: dict) -> list[list[list[float]]]:
    """Outer rings only, each a list of [lon, lat]."""
    t, c = geom.get("type"), geom.get("coordinates", [])
    if t == "Polygon":
        return [c[0]] if c else []
    if t == "MultiPolygon":
        return [p[0] for p in c if p]
    return []


class CountryIndex:
    def __init__(self, geojson: dict):
        self.items = []
        for f in geojson.get("features", []):
            props = f.get("properties", {})
            name = props.get("NAME") or props.get("ADMIN") or props.get("name") or "?"
            rings = _rings(f.get("geometry") or {})
            if not rings:
                continue
            boxes = [(min(p[0] for p in r), min(p[1] for p in r), max(p[0] for p in r), max(p[1] for p in r)) for r in rings]
            big = max(range(len(rings)), key=lambda i: (boxes[i][2] - boxes[i][0]) * (boxes[i][3] - boxes[i][1]))
            b = boxes[big]
            self.items.append({"name": name, "rings": rings, "boxes": boxes,
                               "centroid": ((b[1] + b[3]) / 2, (b[0] + b[2]) / 2),
                               "area": (b[2] - b[0]) * (b[3] - b[1]), "iso": props.get("ISO_A2", "")})

    @staticmethod
    def _inside(ring, lon, lat) -> bool:
        inside = False
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
            if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi:
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _seg_dist_deg(px, py, ax, ay, bx, by) -> float:
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    def lookup(self, lat: float, lon: float, coast_km: float = 120.0) -> Optional[str]:
        """The country containing the point. The outlines are generalised (about 110 km
        detail), so a coastal city can fall just offshore; those resolve to the nearest
        country within `coast_km`. Open ocean gives None."""
        lon = wrap_lon(lon)
        for it in self.items:
            for ring, b in zip(it["rings"], it["boxes"]):
                if b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and self._inside(ring, lon, lat):
                    return it["name"]
        if coast_km <= 0:
            return None
        pad = coast_km / 111.0 + 0.5
        k = max(0.2, math.cos(math.radians(lat)))                # degrees of longitude shrink away from the equator
        best, bd = None, coast_km / 111.2
        for it in self.items:
            for ring, b in zip(it["rings"], it["boxes"]):
                if not (b[0] - pad / k <= lon <= b[2] + pad / k and b[1] - pad <= lat <= b[3] + pad):
                    continue
                for i in range(len(ring) - 1):
                    d = self._seg_dist_deg(lon * k, lat, ring[i][0] * k, ring[i][1], ring[i + 1][0] * k, ring[i + 1][1])
                    if d < bd:
                        best, bd = it["name"], d
        return best


class Places:
    def __init__(self, geojson: dict):
        self.items = []
        for f in geojson.get("features", []):
            p, g = f.get("properties", {}), f.get("geometry") or {}
            if g.get("type") != "Point":
                continue
            lon, lat = g["coordinates"][:2]
            self.items.append({"name": p.get("name") or p.get("nameascii") or "?", "lat": lat, "lon": lon,
                               "rank": p.get("scalerank", 9), "pop": p.get("pop_max") or p.get("pop_min") or 0,
                               "country": p.get("adm0name", "")})

    def nearest(self, lat: float, lon: float, max_km: float = 400.0) -> Optional[dict]:
        best, bd = None, max_km
        for it in self.items:
            if abs(it["lat"] - lat) > 6:
                continue
            d = haversine_km(lat, lon, it["lat"], it["lon"])
            if d < bd:
                best, bd = it, d
        return None if best is None else {**best, "km": bd}


# ─────────────────────────────────────────────────────────────────────────────
#  What the data feeds mean
# ─────────────────────────────────────────────────────────────────────────────
def parse_usgs(data: dict) -> list[dict]:
    out = []
    for f in data.get("features", []):
        try:
            lon, lat = f["geometry"]["coordinates"][:2]
            depth = f["geometry"]["coordinates"][2] if len(f["geometry"]["coordinates"]) > 2 else 0
            p = f["properties"]
            if p.get("mag") is None:
                continue
            out.append({"lat": float(lat), "lon": float(lon), "mag": float(p["mag"]), "depth": float(depth or 0),
                        "place": str(p.get("place") or ""), "time": (p.get("time") or 0) / 1000.0})
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out


def parse_adsb(data: dict) -> list[dict]:
    """adsb.lol / readsb style: {"ac": [{hex, flight, lat, lon, alt_baro, gs, track}, ...]}."""
    out = []
    for a in data.get("ac", []) or []:
        try:
            if a.get("lat") is None or a.get("lon") is None:
                continue
            alt = a.get("alt_baro")
            on_ground = alt == "ground"
            out.append({"id": str(a.get("hex", "")), "call": str(a.get("flight") or "").strip(),
                        "lat": float(a["lat"]), "lon": float(a["lon"]),
                        "alt_ft": 0 if on_ground else (float(alt) if isinstance(alt, (int, float)) else None),
                        "ground": on_ground, "gs": float(a["gs"]) if a.get("gs") is not None else None,
                        "track": float(a["track"]) if a.get("track") is not None else 0.0,
                        "type": str(a.get("t") or "")})
        except (TypeError, ValueError):
            continue
    return out


def parse_weather(data: dict) -> dict:
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    def first(k):
        v = daily.get(k) or []
        return v[0] if v else None
    return {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"), "wind": cur.get("wind_speed_10m"),
            "code": cur.get("weather_code"), "offset_s": data.get("utc_offset_seconds", 0),
            "tz": data.get("timezone_abbreviation") or data.get("timezone") or "",
            "sunrise": first("sunrise"), "sunset": first("sunset")}


def parse_geocode(data: dict) -> Optional[dict]:
    res = (data.get("results") or [])
    if not res:
        return None
    r = res[0]
    return {"name": r.get("name", ""), "lat": float(r["latitude"]), "lon": float(r["longitude"]),
            "country": r.get("country", ""), "admin": r.get("admin1", ""), "tz": r.get("timezone", ""),
            "population": r.get("population")}


WEATHER_CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "light drizzle",
                 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow",
                 73: "snow", 75: "heavy snow", 80: "rain showers", 81: "rain showers", 82: "violent showers",
                 95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail"}
