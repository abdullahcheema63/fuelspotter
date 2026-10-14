"""
Terminal prototype: cheapest fuel stops between two US locations.

Usage (from the repo root):
  pip install numpy requests
  python research/prototype/plan_route.py "Chicago, IL" "Dallas, TX"
  python research/prototype/plan_route.py "41.8781,-87.6298" "32.7767,-96.7970" --reserve 50 --start-fuel 500

Steps:
  1. start/finish -> coordinates (Nominatim, only if text was given)          0-2 external calls
  2. route from OSRM: line + distance + highways used                          1 external call
  3. stations on the route: same highway as the route + close to the line      local
  4. choose stops: cheaper station within reach? buy just enough to get there;
     otherwise fill up and go to the cheapest one within reach                 local
  5. print the plan + write an HTML map (opens in your browser)

Vehicle: 500 mi max range, 10 mpg (assignment). --reserve keeps a safety buffer: the plan never
lets the tank drop below that many miles of fuel. --start-fuel = miles of fuel in the tank at start.
"""
import argparse, json, math, re, sys, time, webbrowser
from pathlib import Path
import numpy as np
import pandas as pd
import requests

HERE = Path(__file__).resolve().parent         # research/prototype
ROOT = HERE.parents[1]                          # repo root
STATIONS = ROOT / "data" / "stations_geocoded.csv"
OUT_DIR = HERE / "maps"

RANGE_MI, MPG = 500.0, 10.0
UA = {"User-Agent": "spotter-fuel-route-assessment/0.1 (+https://abdullahcheema.dev)"}
OSRM = "https://router.project-osrm.org/route/v1/driving"
NOMINATIM = "https://nominatim.openstreetmap.org/search"

# how far a station may sit from the route line, by how precisely we know its location
MAX_OFF_ROUTE = {"exact": 1.0, "exit": 1.5}
TOWN_EXTRA, TOWN_CAP, BIG_CITY_RADIUS = 1.0, 3.5, 5.0
RELAXED_OFF_ROUTE = 3.0   # used only if the strict rules leave a gap the truck can't cross

calls = []  # (what, seconds) — to show how many external calls a plan needs


# ---------------------------------------------------------------- 1. inputs
def locate(text):
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        label = f"{lat:.4f},{lon:.4f}"
    else:
        t = time.time()
        r = requests.get(NOMINATIM, params={"q": text, "format": "jsonv2", "countrycodes": "us", "limit": 1},
                         headers=UA, timeout=20)
        calls.append(("geocode " + text, time.time() - t))
        r.raise_for_status()
        j = r.json()
        if not j:
            sys.exit(f"Could not find '{text}' in the USA.")
        lat, lon, label = float(j[0]["lat"]), float(j[0]["lon"]), j[0]["display_name"]
    if not (18 <= lat <= 72 and -170 <= lon <= -65):
        sys.exit(f"'{text}' is outside the USA.")
    return lat, lon, label


# ---------------------------------------------------------------- 2. route
def get_route(a, b):
    url = f"{OSRM}/{a[1]},{a[0]};{b[1]},{b[0]}"
    t = time.time()
    r = requests.get(url, params={"overview": "full", "geometries": "geojson", "steps": "true"}, headers=UA, timeout=60)
    calls.append(("route", time.time() - t))
    r.raise_for_status()
    j = r.json()
    if j.get("code") != "Ok":
        sys.exit(f"Routing failed: {j.get('code')} {j.get('message', '')}")
    route = j["routes"][0]
    coords = np.array(route["geometry"]["coordinates"])  # [lon, lat]
    refs = " ; ".join(s.get("ref", "") for leg in route["legs"] for s in leg["steps"] if s.get("ref"))
    return coords, route["distance"] / 1609.344, route["duration"] / 3600, refs


def road_keys(text, from_route):
    """'I 40;US 287' / 'I-40, EXIT 76 & SR-72' -> {('I','40'), ('US','287'), ('S','72')}"""
    keys = set()
    pat = r"\b(I|US|[A-Z]{2})[ -]?(\d+)" if from_route else r"\b(I|US|SR|SH|CR|HWY|RT|ROUTE)[ -]?(\d+)"
    for kind, num in re.findall(pat, (text or "").upper()):
        keys.add((kind if kind in ("I", "US") else "S", num))
    return keys


# ---------------------------------------------------------------- 3. stations on the route
def along_route(coords):
    """cumulative miles at each route vertex"""
    lat = np.radians(coords[:, 1]); lon = np.radians(coords[:, 0])
    dlat = np.diff(lat); dlon = np.diff(lon)
    h = np.sin(dlat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    seg = 3958.8 * 2 * np.arcsin(np.sqrt(h))
    return np.concatenate([[0], np.cumsum(seg)])


def project(st_lat, st_lon, coords, cum):
    """for each station: distance to the route line (mi) and its mile marker along the route"""
    lat0 = np.radians(coords[:, 1].mean())
    kx, ky = 69.17 * np.cos(lat0), 69.17          # degrees -> miles (local approximation)
    ax, ay = coords[:-1, 0] * kx, coords[:-1, 1] * ky
    bx, by = coords[1:, 0] * kx, coords[1:, 1] * ky
    dx, dy = bx - ax, by - ay
    L2 = np.maximum(dx * dx + dy * dy, 1e-12)
    seg_len = cum[1:] - cum[:-1]
    off = np.empty(len(st_lat)); mile = np.empty(len(st_lat))
    for s in range(0, len(st_lat), 256):  # chunked to keep memory small
        px = st_lon[s:s + 256, None] * kx; py = st_lat[s:s + 256, None] * ky
        t = np.clip(((px - ax) * dx + (py - ay) * dy) / L2, 0, 1)
        d = np.hypot(ax + t * dx - px, ay + t * dy - py)
        i = d.argmin(axis=1); r = np.arange(len(i))
        off[s:s + 256] = d[r, i]
        mile[s:s + 256] = cum[i] + t[r, i] * seg_len[i]
    return off, mile


def candidates(stations, coords, cum, route_refs, relaxed=False):
    lo, la = coords[:, 0], coords[:, 1]
    pad = 0.1
    s = stations[(stations.lat.between(la.min() - pad, la.max() + pad)) &
                 (stations.lon.between(lo.min() - pad, lo.max() + pad))].copy()
    if s.empty:
        return s
    s["off_route_mi"], s["mile"] = project(s.lat.values, s.lon.values, coords, cum)
    rk = road_keys(route_refs, from_route=True)

    def ok(r):
        if relaxed:
            return r.off_route_mi <= RELAXED_OFF_ROUTE
        keys = road_keys(r.address, from_route=False)
        if keys and not (keys & rk):
            return False                       # station is on a road this route doesn't use
        if r.precision in MAX_OFF_ROUTE:
            return r.off_route_mi <= MAX_OFF_ROUTE[r.precision]
        if r.precision == "town":
            rad = r.town_radius_mi if not pd.isna(r.town_radius_mi) else 2.0
            if rad > BIG_CITY_RADIUS:
                return False                   # big-city center says nothing about where the station is
            return r.off_route_mi <= min(rad + TOWN_EXTRA, TOWN_CAP)
        return False
    s = s[s.apply(ok, axis=1)]
    # one station per spot: if two are within 0.1 mi along the route, keep the cheaper
    s = s.sort_values(["mile", "price"])
    return s.reset_index(drop=True)


# ---------------------------------------------------------------- 4. choose stops
def plan_with_min_purchase(cands, total_mi, start_fuel, reserve, min_gal):
    """Run the plan; if a stop buys less than min_gal, drop that station and plan again
    (unless the trip becomes impossible without it). Each stop costs the driver time,
    so a stop for 1 gallon to save a few cents isn't worth making."""
    keep = set()
    while True:
        stops, end_fuel = plan_stops(cands, total_mi, start_fuel, reserve)
        tiny = [st for st in stops if st["gallons"] < min_gal and st["id"] not in keep]
        if not tiny:
            return stops, end_fuel, cands
        victim = min(tiny, key=lambda st: st["gallons"])
        trial = cands[cands.id != victim["id"]].reset_index(drop=True)
        try:
            plan_stops(trial, total_mi, start_fuel, reserve)
            cands = trial
        except ValueError:
            keep.add(victim["id"])              # needed to finish the trip — keep it


def plan_stops(cands, total_mi, start_fuel, reserve):
    """Classic cheapest-refuelling rule on a line. Fuel is tracked in miles of range.
    Returns list of stops or raises ValueError(mile) if a gap can't be crossed."""
    usable = RANGE_MI - reserve
    nodes = [{"mile": 0.0, "price": math.inf, "row": None}]             # start: can't buy here
    nodes += [{"mile": r.mile, "price": r.price, "row": r} for r in cands.itertuples()]
    nodes.append({"mile": total_mi, "price": -math.inf, "row": None})   # finish: "cheapest", stop there
    fuel = min(start_fuel, RANGE_MI) - reserve                          # usable miles in the tank
    if fuel < 0:
        raise ValueError(0.0)
    i, stops = 0, []
    while i < len(nodes) - 1:
        here = nodes[i]
        reach = here["mile"] + usable
        ahead = [j for j in range(i + 1, len(nodes)) if nodes[j]["mile"] <= reach]
        if not ahead:
            raise ValueError(here["mile"])
        cheaper = next((j for j in ahead if nodes[j]["price"] < here["price"]), None)
        if cheaper is not None:
            need = nodes[cheaper]["mile"] - here["mile"]
            buy = max(0.0, need - fuel)
            nxt = cheaper
        else:
            buy = usable - fuel                                     # fill up
            nxt = min(ahead, key=lambda j: (nodes[j]["price"], -nodes[j]["mile"]))
        if here["row"] is None and buy > 1e-6:     # at the start we can't buy: next hop must be reachable
            raise ValueError(here["mile"])
        if buy > 1e-6:
            r = here["row"]
            stops.append({"id": r.id, "mile": round(here["mile"], 1), "name": r.name, "address": r.address,
                          "city": r.city, "state": r.state, "price": r.price, "lat": r.lat, "lon": r.lon,
                          "precision": r.precision, "off_route_mi": round(r.off_route_mi, 2),
                          "gallons": round(buy / MPG, 2), "cost": round(buy / MPG * r.price, 2),
                          "tank_before_mi": round(fuel + reserve, 1)})
        fuel += buy - (nodes[nxt]["mile"] - here["mile"])
        i = nxt
    return stops, fuel + reserve


# ---------------------------------------------------------------- 5. output
def write_map(path, coords, start, finish, cands, stops):
    step = max(1, len(coords) // 3000)
    line = [[round(la, 5), round(lo, 5)] for lo, la in coords[::step]] + [[coords[-1][1], coords[-1][0]]]
    cand = [[r.lat, r.lon, f"{r.name} — ${r.price:.3f} (mile {r.mile:.0f}, {r.precision})"] for r in cands.itertuples()]
    stp = [[s["lat"], s["lon"], f"<b>Stop {k + 1}: {s['name']}</b><br>{s['address']}, {s['city']} {s['state']}<br>"
            f"${s['price']:.3f}/gal · {s['gallons']} gal · ${s['cost']:.2f}<br>mile {s['mile']} · {s['precision']}"]
           for k, s in enumerate(stops)]
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Fuel route</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#m{{height:100%;margin:0}}</style></head><body><div id="m"></div><script>
const m=L.map('m');
// Esri street basemap (no key needed). OSM's own tile servers refuse pages opened from disk (file://).
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',
  {{attribution:'Tiles © Esri',maxZoom:19}}).addTo(m);
const line=L.polyline({json.dumps(line)},{{color:'#2563eb',weight:4}}).addTo(m);m.fitBounds(line.getBounds());
{json.dumps(cand)}.forEach(c=>L.circleMarker([c[0],c[1]],{{radius:3,color:'#888',weight:1}}).bindPopup(c[2]).addTo(m));
{json.dumps(stp)}.forEach(s=>L.marker([s[0],s[1]]).bindPopup(s[2]).addTo(m));
L.circleMarker([{start[0]},{start[1]}],{{radius:8,color:'#16a34a',fillOpacity:1}}).bindPopup('Start').addTo(m);
L.circleMarker([{finish[0]},{finish[1]}],{{radius:8,color:'#dc2626',fillOpacity:1}}).bindPopup('Finish').addTo(m);
</script></body></html>"""
    path.write_text(html)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start"); ap.add_argument("finish")
    ap.add_argument("--reserve", type=float, default=0, help="safety buffer in miles (default 0)")
    ap.add_argument("--start-fuel", type=float, default=RANGE_MI, help="miles of fuel at start (default full)")
    ap.add_argument("--min-gallons", type=float, default=10, help="skip stops that would buy less than this (default 10)")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    t0 = time.time()
    stations = pd.read_csv(STATIONS)
    stations = stations[stations.precision != "none"]
    start, finish = locate(a.start), locate(a.finish)
    coords, total_mi, hours, refs = get_route(start, finish)
    cum = along_route(coords)
    total_mi = cum[-1]  # use our own measure so mile markers and total agree

    t1 = time.time()
    cands = candidates(stations, coords, cum, refs)
    mode = "strict"
    try:
        stops, end_fuel, used = plan_with_min_purchase(cands, total_mi, a.start_fuel, a.reserve, a.min_gallons)
    except ValueError:
        cands = candidates(stations, coords, cum, refs, relaxed=True)
        mode = "relaxed (strict rules left a gap)"
        try:
            stops, end_fuel, used = plan_with_min_purchase(cands, total_mi, a.start_fuel, a.reserve, a.min_gallons)
        except ValueError as e:
            sys.exit(f"No fuel within reach after mile {e.args[0]:.0f} — trip not possible with {RANGE_MI:.0f} mi range.")
    t2 = time.time()

    spent = sum(s["cost"] for s in stops); gal = sum(s["gallons"] for s in stops)
    print(f"\nFrom:   {start[2]}\nTo:     {finish[2]}")
    print(f"Route:  {total_mi:.0f} mi, ~{hours:.1f} h driving   | fuel burned: {total_mi / MPG:.1f} gal")
    print(f"Stations considered on route: {len(cands)}  ({mode})   reserve: {a.reserve:.0f} mi   start fuel: {a.start_fuel:.0f} mi   min purchase: {a.min_gallons:.0f} gal\n")
    if not stops:
        print("No stops needed — the starting tank covers the trip.")
    for k, s in enumerate(stops, 1):
        print(f"  {k}. mile {s['mile']:>6.0f}  {s['name'][:30]:30s} {s['city'][:16]:16s} {s['state']}  "
              f"${s['price']:.3f}  {s['gallons']:6.1f} gal  ${s['cost']:8.2f}   [{s['precision']}, {s['off_route_mi']} mi off]")
    print(f"\nTotal spent on fuel: ${spent:.2f}  ({gal:.1f} gal bought; "
          f"{max(total_mi / MPG - gal, 0):.1f} gal came from the starting tank; arrives with {end_fuel:.0f} mi left)")
    print(f"External calls: {len(calls)}  ({', '.join(f'{w} {s:.1f}s' for w, s in calls)})   "
          f"local planning: {(t2 - t1) * 1000:.0f} ms   total: {time.time() - t0:.1f}s")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{a.start}-to-{a.finish}".lower()).strip("-")[:80]
    path = OUT_DIR / f"{slug}.html"
    write_map(path, coords, start, finish, cands, stops)
    print(f"Map: {path}")
    if not a.no_open:
        webbrowser.open(path.as_uri())


if __name__ == "__main__":
    main()