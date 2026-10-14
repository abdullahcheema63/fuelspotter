"""
Final terminal prototype: fuel stops between two US locations, balancing fuel price against number of stops.

Usage (from the repo root):
  python research/prototype/plan_route_final.py "Chicago, IL" "Dallas, TX"
  python research/prototype/plan_route_final.py "Los Angeles, CA" "New York, NY" --compare
  python research/prototype/plan_route_final.py "41.8781,-87.6298" "32.7767,-96.7970" --reserve 50 --stop-cost 0

Steps:
  1. start/finish -> coordinates (Nominatim, only if text was given)             0-2 external calls
  2. route from OSRM: line + which highway each part of the route is on            1 external call
     (the same call also returns alternative routes; used only if the fastest can't be fuelled)
  3. stations on the route: on the highway the route uses AT THAT POINT + close to the line   local
  4. choose stops: lowest (fuel cost + stop_cost x number of stops), exact                    local
  5. print the plan + write an HTML map

What changed from plan_route.py:
  - highway check is per part of the route (OSRM steps), not one set for the whole route
  - stations within 0.1 mi of each other along the route are merged (cheaper one kept)
  - if the strict rules leave a gap the truck can't cross, looser rules are used inside that gap only
  - if the fastest route still can't be fuelled (e.g. it cuts through Canada), the next route OSRM
    returned in the same call is used instead
  - if the trip still can't be done with the reserve asked for, the largest reserve that works is
    used, with a warning (the reserve is the last thing to give way)
  - stops are chosen with a stop cost (default $10) instead of "drop purchases under 10 gal";
    the search is exact, uses real distances (no 5-mile rounding) and runs in a few ms
  - numpy only (no pandas); Nominatim/OSRM answers are kept in prototype/cache/ so re-runs make no calls

Vehicle: 500 mi max range, 10 mpg (assignment). --reserve keeps a safety buffer: the plan never
lets the tank drop below that many miles of fuel. --start-fuel = miles of fuel in the tank at start.
"""
import argparse, csv, hashlib, html, json, math, re, sys, time, webbrowser
from pathlib import Path
import numpy as np
import requests

HERE = Path(__file__).resolve().parent         # research/prototype
ROOT = HERE.parents[1]                          # repo root
STATIONS = ROOT / "data" / "stations_geocoded.csv"
OUT_DIR = HERE / "maps"
CACHE_DIR = HERE / "cache"

RANGE_MI, MPG = 500.0, 10.0
UA = {"User-Agent": "spotter-fuel-route-assessment/0.1 (+https://abdullahcheema.dev)"}
OSRM = "https://router.project-osrm.org/route/v1/driving"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
MI_PER_DEG = 69.09            # miles per degree of latitude

# how far a station may sit from the route line, by how precisely we know its location
MAX_OFF_ROUTE = {"exact": 1.0, "exit": 1.5}
TOWN_EXTRA, TOWN_CAP, BIG_CITY_RADIUS = 1.0, 3.5, 5.0
RELAXED_OFF_ROUTE = 3.0       # used only inside a gap the truck can't cross with the strict rules
HIGHWAY_TOL_MI = 2.0          # a station matches the highways the route uses within this many miles of it
SAME_SPOT_MI = 0.1            # stations closer than this along the route count as one spot
GAP_WARNING = 0.8             # warn when the longest stretch without fuel is over 80% of the usable range

calls = []  # (what, seconds) — to show how many external calls a plan needs


class TripImpossible(Exception):
    """No fuel within reach. .last = last place fuel was available (mile), .mile = where the tank
    runs dry, .next = next station (or the finish)."""
    def __init__(self, last, mile, nxt):
        super().__init__(mile)
        self.last, self.mile, self.next = last, mile, nxt


# ---------------------------------------------------------------- 1. inputs
def cached(name, fetch, use_cache=True, keep=lambda data: True):
    path = CACHE_DIR / name
    if use_cache and path.exists():
        return json.loads(path.read_text())
    data = fetch()
    if keep(data):                                       # failures are not cached
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    return data


def locate(text, use_cache=True):
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        label = f"{lat:.4f},{lon:.4f}"
    else:
        def fetch():
            t = time.time()
            r = requests.get(NOMINATIM, params={"q": text, "format": "jsonv2", "countrycodes": "us", "limit": 1},
                             headers=UA, timeout=20)
            calls.append(("geocode " + text, time.time() - t))
            r.raise_for_status()
            return r.json()
        norm = " ".join(text.lower().split())
        j = cached(f"geocode_{hashlib.sha1(norm.encode()).hexdigest()[:16]}.json", fetch, use_cache)
        if not j:
            sys.exit(f"Could not find '{text}' in the USA.")
        lat, lon, label = float(j[0]["lat"]), float(j[0]["lon"]), j[0]["display_name"]
    if not (18 <= lat <= 72 and -170 <= lon <= -65):
        sys.exit(f"'{text}' is outside the USA.")
    return lat, lon, label


# ---------------------------------------------------------------- 2. route
def road_keys(text, from_route):
    """'I 40;US 287' / 'I-40, EXIT 76 & SR-72' -> {('I','40'), ('US','287'), ('S','72')}"""
    keys = set()
    pat = r"\b(I|US|[A-Z]{2})[ -]?(\d+)" if from_route else r"\b(I|US|SR|SH|CR|HWY|RT|ROUTE)[ -]?(\d+)"
    for kind, num in re.findall(pat, (text or "").upper()):
        keys.add((kind if kind in ("I", "US") else "S", num))
    return frozenset(keys)


def along_route(coords):
    """cumulative miles at each route vertex"""
    lat = np.radians(coords[:, 1]); lon = np.radians(coords[:, 0])
    dlat = np.diff(lat); dlon = np.diff(lon)
    h = np.sin(dlat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    seg = 3958.8 * 2 * np.arcsin(np.sqrt(h))
    return np.concatenate([[0], np.cumsum(seg)])


def get_routes(a, b, use_cache=True):
    """One OSRM call. Returns the raw routes, fastest first: the fastest route plus any alternatives
    OSRM offers (usually one, sometimes none). The alternatives are only used if the truck can't be
    fuelled along the fastest route — e.g. when it cuts through Canada, where we have no stations."""
    def fetch():
        t = time.time()
        r = requests.get(f"{OSRM}/{a[1]},{a[0]};{b[1]},{b[0]}",
                         params={"overview": "full", "geometries": "geojson", "steps": "true", "alternatives": "3"},
                         headers=UA, timeout=60)
        calls.append(("route", time.time() - t))
        if r.status_code != 400:                         # OSRM says "no route" with HTTP 400 + a JSON body
            r.raise_for_status()
        return r.json()
    j = cached(f"routes_{a[0]:.4f}_{a[1]:.4f}_{b[0]:.4f}_{b[1]:.4f}.json", fetch, use_cache,
               keep=lambda data: data.get("code") == "Ok")
    if j.get("code") in ("NoRoute", "NoSegment"):
        sys.exit("No road route between these two places (one of them can't be reached by road from the other).")
    if j.get("code") != "Ok":
        sys.exit(f"Routing failed: {j.get('code')} {j.get('message', '')}")
    return j["routes"]


def read_route(route):
    """One raw OSRM route -> the line, mile marker of every vertex, driving hours, and
    'sections': which highways the route is on between which mile markers."""
    coords = np.array(route["geometry"]["coordinates"])  # [lon, lat]
    cum = along_route(coords)

    # Each OSRM step (one stretch of road between two manoeuvres) names its highway in 'ref'.
    # Step lines share their end points, so the vertex where each step starts is known exactly.
    steps = [s for leg in route["legs"] for s in leg["steps"]]
    exact = sum(len(s["geometry"]["coordinates"]) - 1 for s in steps) == len(coords) - 1
    scale = cum[-1] / max(route["distance"], 1e-9)       # only used if the vertex counts don't line up
    sections, vertex, metres = [], 0, 0.0
    for s in steps:
        start = cum[vertex] if exact else metres * scale
        vertex += len(s["geometry"]["coordinates"]) - 1
        metres += s["distance"]
        end = cum[min(vertex, len(cum) - 1)] if exact else metres * scale
        keys = road_keys(s.get("ref", ""), from_route=True)
        if sections and sections[-1][2] == keys:
            sections[-1][1] = end                        # same highway as the previous step: extend it
        else:
            sections.append([start, end, keys])
    return {"coords": coords, "cum": cum, "total_mi": float(cum[-1]), "hours": route["duration"] / 3600,
            "sec_start": np.array([s[0] for s in sections]), "sec_end": np.array([s[1] for s in sections]),
            "sec_keys": [s[2] for s in sections]}


def highways_near(route, mile):
    """highways the route is on within HIGHWAY_TOL_MI of this mile marker"""
    lo = np.searchsorted(route["sec_end"], mile - HIGHWAY_TOL_MI, side="left")
    hi = np.searchsorted(route["sec_start"], mile + HIGHWAY_TOL_MI, side="right")
    keys = set()
    for k in range(lo, hi):
        keys |= route["sec_keys"][k]
    return keys


# ---------------------------------------------------------------- 3. stations on the route
def load_stations():
    """All placed stations as arrays (the API will do this once per process)."""
    rows = [r for r in csv.DictReader(open(STATIONS, newline="")) if r["precision"] != "none"]
    max_off = []
    for r in rows:
        if r["precision"] in MAX_OFF_ROUTE:
            max_off.append(MAX_OFF_ROUTE[r["precision"]])
        else:                                            # town centre
            rad = float(r["town_radius_mi"]) if r["town_radius_mi"] else 2.0
            # big-city centre says nothing about where the station is -> never matches
            max_off.append(-1.0 if rad > BIG_CITY_RADIUS else min(rad + TOWN_EXTRA, TOWN_CAP))
    return {"rows": rows,
            "lat": np.array([float(r["lat"]) for r in rows]), "lon": np.array([float(r["lon"]) for r in rows]),
            "price": np.array([float(r["price"]) for r in rows]), "max_off": np.array(max_off),
            "keys": [road_keys(r["address"], from_route=False) for r in rows]}


def near_route(st, route):
    """Every station within a few miles of the route line: its index, distance to the line (mi)
    and mile marker along the route. Three passes, each cheaper than checking everything:
      a. grid: keep stations in a ~7-mile grid cell the route passes through (or a neighbour)
      b. nearest point among route points sampled every mile
      c. exact distance to the route segments within 5 miles of that point"""
    coords, cum = route["coords"], route["cum"]
    reach = max(TOWN_CAP, RELAXED_OFF_ROUTE)

    s_mile = np.arange(0.0, cum[-1] + 1.0, 1.0)
    s_lat = np.interp(s_mile, cum, coords[:, 1]); s_lon = np.interp(s_mile, cum, coords[:, 0])
    cell = lambda lat, lon: (np.floor(lat / 0.1).astype(np.int64) + 2000) * 10000 + np.floor(lon / 0.1).astype(np.int64) + 4000
    base = np.unique(cell(s_lat, s_lon))
    touched = np.unique(np.concatenate([base + dy * 10000 + dx for dy in (-1, 0, 1) for dx in (-1, 0, 1)]))
    idx = np.flatnonzero(np.isin(cell(st["lat"], st["lon"]), touched))
    if not len(idx):
        return idx, np.empty(0), np.empty(0)

    lat, lon = st["lat"][idx], st["lon"][idx]
    kx = MI_PER_DEG * np.cos(np.radians(lat))            # miles per degree of longitude at each station
    d = np.hypot((s_lon[None, :] - lon[:, None]) * kx[:, None], (s_lat[None, :] - lat[:, None]) * MI_PER_DEG)
    nearest = d.argmin(axis=1)
    keep = d[np.arange(len(idx)), nearest] <= reach + 0.5        # samples are 1 mi apart
    idx, lat, lon, kx, nearest = idx[keep], lat[keep], lon[keep], kx[keep], nearest[keep]

    first = np.searchsorted(cum, s_mile[nearest] - 5.0, side="left")
    last = np.searchsorted(cum, s_mile[nearest] + 5.0, side="right")
    off = np.empty(len(idx)); mile = np.empty(len(idx))
    for n in range(len(idx)):
        i0, i1 = max(first[n] - 1, 0), min(last[n] + 1, len(cum))
        x = (coords[i0:i1, 0] - lon[n]) * kx[n]; y = (coords[i0:i1, 1] - lat[n]) * MI_PER_DEG   # station at (0, 0)
        dx, dy = np.diff(x), np.diff(y)
        t = np.clip(-(x[:-1] * dx + y[:-1] * dy) / np.maximum(dx * dx + dy * dy, 1e-12), 0, 1)
        dist = np.hypot(x[:-1] + t * dx, y[:-1] + t * dy)
        k = dist.argmin()
        off[n] = dist[k]
        mile[n] = cum[i0 + k] + t[k] * (cum[i0 + k + 1] - cum[i0 + k])
    keep = off <= reach
    return idx[keep], off[keep], mile[keep]


def candidates(st, route, near, relaxed_ranges=()):
    """Stations the truck may use, sorted by mile marker.
    Strict rule: station's highway is one the route is on at that point, and it is close to the line.
    Inside relaxed_ranges (mile ranges the truck couldn't cross otherwise): anything within 3 miles."""
    idx, off, mile = near
    picked = []
    for n in range(len(idx)):
        s = idx[n]
        keys = st["keys"][s]
        strict = off[n] <= st["max_off"][s] and (not keys or bool(keys & highways_near(route, mile[n])))
        relaxed = (not strict and off[n] <= RELAXED_OFF_ROUTE
                   and any(lo < mile[n] < hi for lo, hi in relaxed_ranges))
        if strict or relaxed:
            picked.append({"station": int(s), "mile": float(mile[n]), "off": float(off[n]),
                           "price": float(st["price"][s]), "relaxed": relaxed})
    picked.sort(key=lambda c: (c["mile"], c["price"]))
    out = []                                             # one station per spot: keep the cheaper
    for c in picked:
        if out and c["mile"] - out[-1]["mile"] < SAME_SPOT_MI:
            if c["price"] < out[-1]["price"]:
                out[-1] = c
        else:
            out.append(c)
    return out


def first_gap(miles, total_mi, start_fuel, reserve):
    """None if the truck can make it, else (last station before the gap, where the tank runs dry, next station)."""
    usable = RANGE_MI - reserve
    last, reach = 0.0, min(start_fuel, RANGE_MI) - reserve
    for m in list(miles) + [total_mi]:
        if m > reach + 1e-9:
            return last, max(reach, 0.0), m
        last, reach = m, m + usable
    return None


def candidates_with_fallback(st, route, near, start_fuel, reserve):
    """Strict rules first; if the truck can't cross some gap, loosen the rules inside that gap only."""
    relaxed_ranges = []
    while True:
        cands = candidates(st, route, near, relaxed_ranges)
        gap = first_gap([c["mile"] for c in cands], route["total_mi"], start_fuel, reserve)
        if gap is None:
            return cands, relaxed_ranges
        last, dry, nxt = gap
        if (last, nxt) in relaxed_ranges:                # already loosened here and it didn't help
            raise TripImpossible(last, dry, nxt)
        relaxed_ranges.append((last, nxt))


# ---------------------------------------------------------------- 4. choose stops
def plan_stops(miles, prices, total_mi, start_fuel, reserve, stop_cost):
    """Exact: lowest (fuel cost + stop_cost * number of stops). Returns ([(candidate index, miles of fuel bought)], fuel left).

    Fuel is counted in miles of range above the reserve ("empty" = at the reserve).
    Why this is exact: take any plan and two stops in a row, A then B. If the truck leaves A not full
    and reaches B not empty, moving fuel from the pricier of the two to the cheaper one never costs more.
    Keep doing that and every stop ends up either FILLING UP or buying JUST ENOUGH to reach the next stop
    empty. So only two things need remembering per station:
        E[j] = cheapest way to arrive at station j empty
        F[j] = cheapest way to drive away from station j full
    and the moves between them are:
        E[j] -> F[j]            fill up at j                                   buys U
        E[j] -> E[k]            buy just enough at j to reach k                buys dist(j,k)
        F[i] -> F[j]            fill up again at j                             buys dist(i,j)
        F[i] -> E[k] via j      top up at j just enough to reach k             buys dist(i,k) - U
    (U = usable tank in miles). For the last move the best j is simply the cheapest station that
    i can reach and that can reach k. The start is treated as "left full" at an imaginary point
    U - start_fuel miles before mile 0, the finish as a station to arrive at empty.
    """
    usable = RANGE_MI - reserve
    f0 = min(start_fuel, RANGE_MI) - reserve
    if usable <= 0 or f0 < 0:
        raise TripImpossible(0.0, 0.0, 0.0)
    gap = first_gap(miles, total_mi, start_fuel, reserve)
    if gap:
        raise TripImpossible(*gap)
    if total_mi <= f0:
        return [], f0 - total_mi + reserve               # the starting tank covers the trip

    n = len(miles)
    m = np.concatenate([[f0 - usable], miles, [total_mi]])           # node 0 = start, 1..n = stations, n+1 = finish
    c = np.concatenate([[0.0], np.asarray(prices) / MPG, [0.0]])     # $ per mile of fuel
    E = np.full(n + 2, math.inf); F = np.full(n + 2, math.inf)
    En = np.zeros(n + 2, int); Fn = np.zeros(n + 2, int)             # number of stops behind each E / F
    E_src = np.full(n + 2, -1); E_at = np.full(n + 2, -1)            # E[k] came from node E_src, fuel bought at E_at
    F_src = np.full(n + 2, -1)                                       # F[j] came from F[F_src] (or E[j] if F_src == j)
    F[0] = 0.0
    reach = np.searchsorted(m, m + usable + 1e-9, side="right") - 1  # furthest node reachable on a full tank
    reach2 = np.searchsorted(m, m + 2 * usable + 1e-9, side="right") - 1
    back = np.searchsorted(m, m - usable - 1e-9, side="left")        # earliest node that can reach this one
    eps = 1e-9

    def offer(k0, k1, cost, stops, src, at):
        """offer new ways of arriving empty at nodes k0..k1-1; keep each one that is cheaper (or equal with fewer stops)"""
        cur, curn = E[k0:k1], En[k0:k1]
        better = np.isfinite(cost) & ((cost < cur - eps) | ((cost <= cur + eps) & (stops < curn)))
        cur[better] = cost[better]; curn[better] = stops
        E_src[k0:k1][better] = src; E_at[k0:k1][better] = at[better] if isinstance(at, np.ndarray) else at

    for i in range(n + 1):
        if i:                                            # a station: work out F[i], then leave it "just enough"
            lo = back[i]
            bought = m[i] - m[lo:i]
            cost = np.where(bought > eps, F[lo:i] + c[i] * bought + stop_cost, math.inf)
            if len(cost) and np.isfinite(cost.min()):
                best = np.lexsort((Fn[lo:i], cost))[0]
                F[i], Fn[i], F_src[i] = cost[best], Fn[lo + best] + 1, lo + best
            fill = E[i] + c[i] * usable + stop_cost
            if fill < F[i] - eps or (fill <= F[i] + eps and En[i] + 1 < Fn[i]):
                F[i], Fn[i], F_src[i] = fill, En[i] + 1, i
            if np.isfinite(E[i]) and reach[i] > i:       # E[i] -> E[k]
                k0, k1 = i + 1, reach[i] + 1
                offer(k0, k1, E[i] + c[i] * (m[k0:k1] - m[i]) + stop_cost, En[i] + 1, i, i)
        # F[i] -> E[k] via the cheapest station j that i can reach and that can reach k
        j0, j1 = i + 1, min(reach[i], n) + 1             # stations i can reach
        k0, k1 = reach[i] + 1, reach2[i] + 1             # nodes one top-up away
        if not np.isfinite(F[i]) or j1 <= j0 or k1 <= k0:
            continue
        rev = c[j0:j1][::-1]                             # cheapest station from each j onwards (suffix minimum)
        low = np.minimum.accumulate(rev)
        arg = np.maximum.accumulate(np.where(rev <= low, np.arange(len(rev)), 0))
        suf_price, suf_at = low[::-1], (j1 - 1 - arg)[::-1]
        first_j = np.maximum(back[k0:k1], j0) - j0       # first station that can reach k
        ok = first_j < (j1 - j0)
        first_j = np.minimum(first_j, j1 - j0 - 1)
        cost = np.where(ok, F[i] + suf_price[first_j] * (m[k0:k1] - m[i] - usable) + stop_cost, math.inf)
        offer(k0, k1, cost, Fn[i] + 1, i, suf_at[first_j])

    if not np.isfinite(E[n + 1]):
        raise TripImpossible(0.0, 0.0, 0.0)             # can't happen once first_gap() has passed

    buys, node, empty = [], n + 1, True                  # walk back from "arrive at the finish empty"
    while node:
        if empty:
            src, at = E_src[node], E_at[node]
            if src == at:
                buys.append((at - 1, m[node] - m[at]))
            else:
                buys.append((at - 1, m[node] - m[src] - usable)); empty = False
            node = src
        else:
            src = F_src[node]
            if src == node:
                buys.append((node - 1, usable)); empty = True
            else:
                buys.append((node - 1, m[node] - m[src])); node = src
    return [(int(k), float(b)) for k, b in buys[::-1]], reserve


def plan_greedy(miles, prices, total_mi, start_fuel, reserve):
    """Classic cheapest-refuelling rule (same as plan_route.py): cheaper station within reach? buy just
    enough to get there; otherwise fill up and go to the cheapest one within reach. Gives the lowest
    possible fuel cost with no regard for the number of stops — used to check plan_stops(stop_cost=0)."""
    usable = RANGE_MI - reserve
    m = [0.0] + list(miles) + [total_mi]
    p = [math.inf] + list(prices) + [-math.inf]
    fuel = min(start_fuel, RANGE_MI) - reserve
    if fuel < 0:
        raise TripImpossible(0.0, 0.0, 0.0)
    i, buys = 0, []
    while i < len(m) - 1:
        ahead = [j for j in range(i + 1, len(m)) if m[j] <= m[i] + usable]
        if not ahead:
            raise TripImpossible(m[i], m[i] + fuel, m[i + 1])
        cheaper = next((j for j in ahead if p[j] < p[i]), None)
        if cheaper is not None:
            buy, nxt = max(0.0, m[cheaper] - m[i] - fuel), cheaper
        else:
            buy, nxt = usable - fuel, min(ahead, key=lambda j: (p[j], -m[j]))
        if i == 0 and buy > 1e-6:                        # can't buy at the start
            raise TripImpossible(m[i], m[i] + fuel, m[i + 1])
        if buy > 1e-6:
            buys.append((i - 1, buy))
        fuel += buy - (m[nxt] - m[i])
        i = nxt
    return buys, fuel + reserve


def choose_route(st, routes, start_fuel, reserve, stop_cost):
    """Fastest route first; the next one only if the truck can't be fuelled along it.
    routes: list of [raw OSRM route, read route, stations near it] (the last two filled in when first needed).
    Returns (plan, failed): plan is None if no route works; failed = (problem, route) on the fastest route, if it failed."""
    failed = None
    for n, entry in enumerate(routes):
        if entry[1] is None:
            entry[1] = read_route(entry[0]); entry[2] = near_route(st, entry[1])
        route, near = entry[1], entry[2]
        try:
            cands, relaxed_ranges = candidates_with_fallback(st, route, near, start_fuel, reserve)
            t_found = time.perf_counter()
            buys, end_fuel = plan_stops([c["mile"] for c in cands], [c["price"] for c in cands],
                                        route["total_mi"], start_fuel, reserve, stop_cost)
            return {"route": route, "cands": cands, "relaxed_ranges": relaxed_ranges, "buys": buys,
                    "end_fuel": end_fuel, "t_found": t_found}, failed
        except TripImpossible as e:
            failed = failed or (e, route)                # report the problem on the fastest route
    return None, failed


def describe(st, cands, buys, start_fuel, reserve):
    """turn (candidate index, miles bought) into printable stops"""
    stops, fuel, pos = [], min(start_fuel, RANGE_MI), 0.0
    for k, bought in buys:
        c = cands[k]; r = st["rows"][c["station"]]
        fuel -= c["mile"] - pos; pos = c["mile"]
        stops.append({"id": r["id"], "mile": c["mile"], "name": r["name"], "address": r["address"], "city": r["city"],
                      "state": r["state"], "price": c["price"], "lat": float(r["lat"]), "lon": float(r["lon"]),
                      "precision": r["precision"], "off_route_mi": c["off"], "relaxed": c["relaxed"],
                      "gallons": bought / MPG, "cost": bought / MPG * c["price"], "tank_before_mi": fuel})
        fuel += bought
    return stops


# ---------------------------------------------------------------- 5. output
def write_map(path, route, start, finish, st, cands, stops):
    coords = route["coords"]
    step = max(1, len(coords) // 3000)
    line = [[round(la, 5), round(lo, 5)] for lo, la in coords[::step]] + [[coords[-1][1], coords[-1][0]]]
    e = html.escape
    cand = []
    for c in cands:
        r = st["rows"][c["station"]]
        cand.append([float(r["lat"]), float(r["lon"]),
                     f"{e(r['name'])} — ${c['price']:.3f} (mile {c['mile']:.0f}, {r['precision']})"])
    stp = [[s["lat"], s["lon"], f"<b>Stop {k + 1}: {e(s['name'])}</b><br>{e(s['address'])}, {e(s['city'])} {s['state']}<br>"
            f"${s['price']:.3f}/gal · {s['gallons']:.1f} gal · ${s['cost']:.2f}<br>mile {s['mile']:.0f} · {s['precision']}"]
           for k, s in enumerate(stops)]
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Fuel route</title>
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
    path.write_text(page)


def compare(st, cands, total_mi, a):
    """same route, same stations, different stop costs — to see what the stop cost buys"""
    miles = [c["mile"] for c in cands]; prices = [c["price"] for c in cands]
    rows = []
    def run(label, fn):
        t = time.perf_counter()
        buys, _ = fn()
        ms = (time.perf_counter() - t) * 1000
        rows.append((label, len(buys), sum(b for _, b in buys) / MPG, sum(b / MPG * prices[k] for k, b in buys), ms))
    run("greedy cheapest (check)", lambda: plan_greedy(miles, prices, total_mi, a.start_fuel, a.reserve))
    for sc in (0, 5, 10, 20, 40, 10_000):
        label = {0: "stop cost $0 (cheapest fuel)", 10_000: "fewest stops"}.get(sc, f"stop cost ${sc}")
        run(label, lambda sc=sc: plan_stops(miles, prices, total_mi, a.start_fuel, a.reserve, sc))
    base = rows[0][3]
    print(f"\n{'strategy':30s} {'stops':>5s} {'gallons':>8s} {'fuel cost':>10s} {'vs cheapest':>12s} {'time':>8s}")
    for label, n, g, cost, ms in rows:
        print(f"{label:30s} {n:5d} {g:8.1f} {cost:10.2f} {cost - base:+12.2f} {ms:6.1f}ms")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start"); ap.add_argument("finish")
    ap.add_argument("--reserve", type=float, default=0, help="safety buffer in miles (default 0)")
    ap.add_argument("--start-fuel", type=float, default=RANGE_MI, help="miles of fuel at start (default full)")
    ap.add_argument("--stop-cost", type=float, default=10, help="what one stop is worth in $ (default 10; 0 = cheapest fuel)")
    ap.add_argument("--compare", action="store_true", help="also print the plan for other stop costs")
    ap.add_argument("--no-cache", action="store_true", help="call Nominatim/OSRM even if the answer is cached")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    if not 0 <= a.reserve < RANGE_MI:
        sys.exit("--reserve must be between 0 and 500.")

    t0 = time.perf_counter()
    st = load_stations()
    t_load = time.perf_counter()
    start, finish = locate(a.start, not a.no_cache), locate(a.finish, not a.no_cache)
    raw_routes = get_routes(start, finish, not a.no_cache)

    # What gives way when the trip can't be done as asked, in this order:
    #   1. station rules, inside the gap only          (candidates_with_fallback)
    #   2. the route: next one from the same OSRM call  (choose_route)
    #   3. the reserve: the largest one that still works, with a warning
    t1 = time.perf_counter()
    routes = [[raw, None, None] for raw in raw_routes]
    asked_reserve = a.reserve
    plan, failed = choose_route(st, routes, a.start_fuel, a.reserve, a.stop_cost)
    if plan is None and a.reserve > 0 and choose_route(st, routes, a.start_fuel, 0, a.stop_cost)[0]:
        lo, hi = 0, int(a.reserve)                       # lo works; find the largest whole-mile reserve that does
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if mid < a.reserve and choose_route(st, routes, a.start_fuel, mid, a.stop_cost)[0]:
                lo = mid
            else:
                hi = mid - 1
        a.reserve = float(lo)
        plan, failed = choose_route(st, routes, a.start_fuel, a.reserve, a.stop_cost)
    if plan is None:
        e, route = failed
        where = "the start" if e.last == 0 else f"the last station at mile {e.last:.0f}"
        what = "the finish" if e.next >= route["total_mi"] else "the next station"
        msg = (f"Trip not possible: from {where} it is {e.next - e.last:.0f} mi to {what} (mile {e.next:.0f}), "
               f"but the truck can only go {e.mile - e.last:.0f} mi from there "
               f"({RANGE_MI:.0f} mi range, {a.reserve:.0f} mi reserve, {a.start_fuel:.0f} mi of fuel at the start).")
        if a.reserve > 0:
            msg += "\nIt is not possible with a reserve of 0 either."
        if e.next - e.last > RANGE_MI:
            msg += "\nThe price list has US stations only, so this also happens when the fastest route leaves the USA."
        if len(raw_routes) > 1:
            msg += f"\nThe {len(raw_routes) - 1} alternative route(s) OSRM offered can't be fuelled either."
        sys.exit(msg)
    t3 = time.perf_counter()
    route, cands, relaxed_ranges, buys, end_fuel, t2 = (plan[k] for k in ("route", "cands", "relaxed_ranges", "buys", "end_fuel", "t_found"))
    total_mi = route["total_mi"]  # our own measure so mile markers and total agree
    miles = [c["mile"] for c in cands]; prices = [c["price"] for c in cands]
    stops = describe(st, cands, buys, a.start_fuel, a.reserve)

    spent = sum(s["cost"] for s in stops); gal = sum(s["gallons"] for s in stops)
    edges = [0.0] + miles + [total_mi]
    gap, gap_at = max((b - x, x) for x, b in zip(edges, edges[1:]))
    mode = "strict" if not relaxed_ranges else "strict + relaxed in " + ", ".join(f"mile {lo:.0f}-{hi:.0f}" for lo, hi in relaxed_ranges)
    print(f"\nFrom:   {start[2]}\nTo:     {finish[2]}")
    print(f"Route:  {total_mi:.0f} mi, ~{route['hours']:.1f} h driving   | fuel burned: {total_mi / MPG:.1f} gal")
    if failed:
        fast = failed[1]
        print(f"** Not the fastest route: the fastest ({fast['total_mi']:.0f} mi, ~{fast['hours']:.1f} h) has {failed[0].next - failed[0].last:.0f} mi "
              f"without a station in our data (from mile {failed[0].last:.0f}), so a longer route is used. **")
    if a.reserve < asked_reserve:
        print(f"** Reserve of {asked_reserve:.0f} mi can't be kept on this trip: planned with {a.reserve:.0f} mi, "
              f"the largest that works. The tank will drop to {a.reserve:.0f} mi at some point. **")
    print(f"Stations considered on route: {len(cands)}  ({mode})   reserve: {a.reserve:.0f} mi   "
          f"start fuel: {a.start_fuel:.0f} mi   stop cost: ${a.stop_cost:g}")
    print(f"Longest stretch without a station: {gap:.0f} mi (from mile {gap_at:.0f})"
          + ("   ** close to the truck's range **" if gap > GAP_WARNING * (RANGE_MI - a.reserve) else "") + "\n")
    if not stops:
        print("No stops needed — the starting tank covers the trip.")
    for k, s in enumerate(stops, 1):
        print(f"  {k}. mile {s['mile']:>6.0f}  {s['name'][:30]:30s} {s['city'][:16]:16s} {s['state']}  "
              f"${s['price']:.3f}  {s['gallons']:6.1f} gal  ${s['cost']:8.2f}   "
              f"[{s['precision']}, {s['off_route_mi']:.2f} mi off{', relaxed' if s['relaxed'] else ''}]")
    print(f"\nTotal spent on fuel: ${spent:.2f}  ({gal:.1f} gal bought; "
          f"{max(total_mi / MPG - gal, 0):.1f} gal came from the starting tank; arrives with {end_fuel:.0f} mi left)")
    print(f"External calls: {len(calls)}  ({', '.join(f'{w} {s:.1f}s' for w, s in calls) or 'all cached'})")
    print(f"Local work: find stations {(t2 - t1) * 1000:.0f} ms + choose stops {(t3 - t2) * 1000:.1f} ms "
          f"= {(t3 - t1) * 1000:.0f} ms   (loading the station file once: {(t_load - t0) * 1000:.0f} ms)")
    if a.compare:
        compare(st, cands, total_mi, a)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{a.start}-to-{a.finish}".lower()).strip("-")[:80]
    path = OUT_DIR / f"{slug}-final.html"
    write_map(path, route, start, finish, st, cands, stops)
    print(f"Map: {path}")
    if not a.no_open:
        webbrowser.open(path.as_uri())


if __name__ == "__main__":
    main()
