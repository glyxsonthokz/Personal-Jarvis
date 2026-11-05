import os, sys
import os, sys, tempfile, pathlib
APP = os.environ.get("JARVIS_DIR") or os.path.join(os.path.expanduser("~"), "Jarvis")
if not os.path.exists(os.path.join(APP, "main.py")):
    sys.exit("Set JARVIS_DIR to your installed Jarvis folder (run Install_Jarvis.bat first).")
sys.path.insert(0, APP); os.chdir(APP)
TMP = tempfile.mkdtemp(prefix="jarvis-test-")

def download(url, name):
    import urllib.request
    p = os.path.join(TMP, name)
    if not os.path.exists(p):
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "jarvis-tests"}), timeout=120) as r, open(p, "wb") as f:
            f.write(r.read())
    return p
def earth_assets():
    from core import earth_data as data
    data.CACHE = pathlib.Path(TMP)
    err = data.download_assets()
    assert err is None, err
    return data
import json, math
from datetime import datetime, timezone, timedelta
from core import earth_geo as g

# sun: J2000 (2000-01-01 12:00 UTC): declination about -23.0, sub-solar longitude about +0.85
la, lo = g.sun_subpoint(datetime(2000, 1, 1, 12, tzinfo=timezone.utc)); print(f"sun J2000: lat {la:.2f} lon {lo:.2f}")
assert abs(la + 23.0) < 0.25 and abs(lo - 0.85) < 0.35
la, lo = g.sun_subpoint(datetime(2024, 12, 21, 9, 21, tzinfo=timezone.utc)); print(f"solstice : lat {la:.2f}"); assert abs(la + 23.44) < 0.1
la, lo = g.sun_subpoint(datetime(2024, 3, 20, 12, tzinfo=timezone.utc)); print(f"equinox  : lat {la:.2f}"); assert abs(la) < 1.0
# distances and bearings
d = g.haversine_km(51.5074, -0.1278, 40.7128, -74.0060); b = g.bearing_deg(51.5074, -0.1278, 40.7128, -74.0060)
print(f"London-NYC {d:.0f} km, bearing {b:.0f} ({g.compass(b)})"); assert abs(d - 5570) < 20 and abs(b - 288) < 2
gc = g.great_circle(51.5074, -0.1278, 40.7128, -74.0060, 32); assert abs(gc[0][0] - 51.5074) < 1e-6 and abs(gc[-1][1] + 74.0060) < 1e-6
mid = gc[16]; assert abs(g.haversine_km(51.5074, -0.1278, *mid) - d / 2) < 5
# mercator
assert abs(g.merc_x(0, 0) - 128) < 1e-9 and abs(g.merc_y(0, 0) - 128) < 1e-6
for lat, lon, z in ((48.85, 2.35, 5.3), (-33.9, 151.2, 9), (24.8, 93.9, 3.2)):
    x, y = g.merc_x(lon, z), g.merc_y(lat, z); la2, lo2 = g.inv_merc(x, y, z); assert abs(la2 - lat) < 1e-6 and abs(lo2 - lon) < 1e-6
print("mercator round-trips; altitude at zoom 3 =", round(g.altitude_km(3)), "km")
# satellites: the classic ISS test TLE (epoch 2008-09-20 12:25 UTC)
tle = """ISS (ZARYA)
1 25544U 98067A   08264.51782528 -.00002182  00000-0 -11606-4 0  2927
2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.72125391563537
"""
sats = g.parse_tles(tle); assert len(sats) == 1 and sats[0][0] == "ISS (ZARYA)"
s = g.make_sat(sats[0][1], sats[0][2]); t0 = datetime(2008, 9, 20, 12, 25, 40, tzinfo=timezone.utc)
p = g.sat_position(s, t0); print("ISS:", {k: round(v, 2) for k, v in p.items()})
assert 330 < p["alt_km"] < 450 and abs(p["lat"]) <= 51.7 and 7.5 < p["speed_kms"] < 7.8
tr = g.ground_track(s, t0, minutes=95, step_s=30); print("ground track:", len(tr), "segments,", sum(len(x) for x in tr), "points")
assert sum(len(x) for x in tr) > 150 and all(abs(a[1] - b[1]) < 180 for seg in tr for a, b in zip(seg, seg[1:]))
orbit_pts = [g.sat_position(s, t0 + timedelta(seconds=60 * k)) for k in range(0, 93)]
assert max(abs(q["lat"]) for q in orbit_pts) > 50.5 and min(q["alt_km"] for q in orbit_pts) > 330   # reaches the 51.6 degree inclination
pa = g.next_pass(s, 40.7128, -74.0060, t0, hours=36); print("NYC pass:", {k: (v.strftime("%H:%M:%S") if hasattr(v, "strftime") else round(v, 1)) for k, v in pa.items()})
assert pa and pa["max_el"] >= 10 and 1 < pa["minutes"] < 14 and pa["rise"] < pa["peak"] < pa["set"]
# at the peak, the satellite really is that high, and straight-down below it the elevation is ~90
pk = g.sat_position(s, pa["peak"]); el, az, rng = g.look_angles(s, pa["peak"], 40.7128, -74.0060); print(f"peak elevation {el:.1f}, range {rng:.0f} km")
assert abs(el - pa["max_el"]) < 0.5
assert g.look_angles(s, t0, p["lat"], p["lon"])[0] > 85, "directly under the satellite it should be overhead"
# countries / places with the real Natural Earth files
cj = json.load(open(earth_assets().asset_path("countries"), encoding="utf-8")); ci = g.CountryIndex(cj)
for (la, lo), want in (((48.85, 2.35), "France"), ((40.7, -74.0), "United States"), ((28.6, 77.2), "India"), ((-33.9, 151.2), "Australia"), ((0, -30), None)):
    got = ci.lookup(la, lo); print(f"({la},{lo}) ->", got); assert (got is None) if want is None else (got is not None and want in got)
pl = g.Places(json.load(open(earth_assets().asset_path("places"), encoding="utf-8"))); n = pl.nearest(28.6, 77.2); print("nearest to 28.6,77.2:", n["name"], round(n["km"]), "km"); assert n["km"] < 60
# parsers on realistic payloads
q = g.parse_usgs({"features": [{"properties": {"mag": 5.1, "place": "10 km S of Somewhere", "time": 1700000000000}, "geometry": {"coordinates": [-120.5, 36.2, 8.0]}}, {"properties": {"mag": None}, "geometry": {"coordinates": [0, 0, 0]}}]}); assert len(q) == 1 and q[0]["mag"] == 5.1 and q[0]["lat"] == 36.2
ac = g.parse_adsb({"ac": [{"hex": "a1b2c3", "flight": "UAL123 ", "lat": 40.1, "lon": -74.2, "alt_baro": 35000, "gs": 450.2, "track": 271.5}, {"hex": "x", "lat": 1, "lon": 2, "alt_baro": "ground"}, {"hex": "nopos"}]}); assert len(ac) == 2 and ac[0]["call"] == "UAL123" and ac[1]["ground"]
w = g.parse_weather({"utc_offset_seconds": 10800, "timezone_abbreviation": "GMT+3", "current": {"temperature_2m": 14.3, "apparent_temperature": 14.3, "relative_humidity_2m": 94, "wind_speed_10m": 7.0}, "daily": {"sunrise": ["2026-09-21T07:02"], "sunset": ["2026-09-21T19:14"]}}); assert w["temp"] == 14.3 and w["sunset"].endswith("19:14")
assert g.parse_geocode({"results": [{"name": "Imphal", "latitude": 24.8, "longitude": 93.9, "country": "India", "admin1": "Manipur"}]})["country"] == "India" and g.parse_geocode({}) is None
print("\nGEO TESTS PASSED")
