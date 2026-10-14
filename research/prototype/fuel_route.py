"""
Fuel stops between two US locations: cheapest fuel without stopping more often than it is worth.

Usage (from the repo root):
  python research/prototype/fuel_route.py "Chicago, IL" "Dallas, TX"
  python research/prototype/fuel_route.py "41.8781,-87.6298" "32.7767,-96.7970" --reserve 50 --stop-cost 0

Steps:
  1. Locate    start/finish text -> coordinates (Nominatim; skipped for "lat,lon")   0-2 calls
  2. Route     one OSRM call: fastest route + any alternatives                       1 call
  3. Match     stations on the highway the route uses at that point, near the line   local
  4. Plan      lowest (fuel cost + stop cost x number of stops), exact               local
  5. Report    print the plan and write an HTML map

If the trip can't be done as asked, these give way in this order:
  a. station rules, inside the uncrossable gap only (any station within 3 miles)
  b. the route: the next one OSRM returned in the same call
  c. the reserve: the largest one that still works, with a warning

Vehicle: 500-mile range, 10 mpg. Fuel is counted in miles of range throughout.
"""
import argparse
import csv
import hashlib
import html
import json
import math
import re
import sys
import time
import webbrowser
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import numpy as np
import requests

HERE = Path(__file__).resolve().parent         # research/prototype
ROOT = HERE.parents[1]                          # repo root
STATIONS_FILE = ROOT / "data" / "stations_geocoded.csv"
MAP_DIR = HERE / "maps"
CACHE_DIR = HERE / "cache"

OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "spotter-fuel-route-assessment/0.1 (+https://abdullahcheema.dev)"}

RANGE_MILES = 500.0
MILES_PER_GALLON = 10.0
MILES_PER_DEGREE = 69.09          # one degree of latitude
EARTH_RADIUS_MILES = 3958.8

# How far a station may sit from the route line, by how well we know its location.
MAX_OFF_ROUTE = {"exact": 1.0, "exit": 1.5}
TOWN_EXTRA_MILES = 1.0            # town-level station: town radius + this ...
TOWN_CAP_MILES = 3.5              # ... but never more than this
BIG_CITY_RADIUS_MILES = 5.0       # a big-city centre says nothing about where the station is
DEFAULT_TOWN_RADIUS_MILES = 2.0
RELAXED_OFF_ROUTE_MILES = 3.0     # used only inside a gap the truck can't cross
HIGHWAY_TOLERANCE_MILES = 2.0     # station's highway must be used by the route this close to it
SAME_SPOT_MILES = 0.1             # stations this close along the route count as one
GAP_WARNING_SHARE = 0.8           # warn when the longest gap is over 80% of the usable range

SAMPLE_EVERY_MILES = 1.0          # route points used to find nearby stations
SEARCH_WINDOW_MILES = 5.0         # exact distance is measured this far either side of it
GRID_DEGREES = 0.1                # grid cell size (about 7 miles)

TIE_BREAK_USD = 1e-6              # per stop: between equal-cost plans, prefer fewer stops
EPSILON = 1e-9

external_calls = []               # (what, seconds)


class TripImpossible(Exception):
    """The truck runs dry: no fuel between `last_fuel_mile` and `next_fuel_mile`."""

    def __init__(self, last_fuel_mile, dry_mile, next_fuel_mile, route=None):
        super().__init__(dry_mile)
        self.last_fuel_mile = last_fuel_mile
        self.dry_mile = dry_mile
        self.next_fuel_mile = next_fuel_mile
        self.route = route


@dataclass
class Route:
    coords: np.ndarray            # [lon, lat] of every vertex
    mile_at: np.ndarray           # mile marker of every vertex
    total_miles: float
    hours: float
    section_start: np.ndarray     # sections: stretches of the route on the same highways
    section_end: np.ndarray
    section_highways: list


@dataclass
class Stations:
    rows: list                    # the CSV rows
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray
    max_off_route: np.ndarray     # strict distance limit per station (-1 = never matches)
    highways: list                # highways named in each address


@dataclass
class Candidate:
    station: int                  # index into Stations
    mile: float                   # mile marker along the route
    off_route: float              # miles from the route line
    price: float
    relaxed: bool                 # accepted by the relaxed rule only


@dataclass
class Stop:
    row: dict
    mile: float
    off_route: float
    price: float
    relaxed: bool
    gallons: float
    cost: float


@dataclass
class TripPlan:
    route: Route
    candidates: list
    relaxed_ranges: list          # mile ranges where the relaxed rule was needed
    purchases: list               # (candidate index, miles of fuel bought)
    fuel_left: float              # miles of fuel on arrival
    reserve: float                # reserve actually used
    fastest_route_problem: object = None   # TripImpossible, when an alternative route was used


# ---------------------------------------------------------------- 1. locate
def load_cached(name, fetch, use_cache=True, worth_keeping=lambda data: True):
    """Return the JSON saved under `name`, or fetch it and save it."""
    path = CACHE_DIR / name
    if use_cache and path.exists():
        return json.loads(path.read_text())
    data = fetch()
    if worth_keeping(data):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    return data


def locate(text, use_cache=True):
    """'Chicago, IL' or '41.87,-87.62' -> (lat, lon, label). Exits if not found in the USA."""
    as_numbers = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text)
    if as_numbers:
        lat, lon = float(as_numbers.group(1)), float(as_numbers.group(2))
        label = f"{lat:.4f},{lon:.4f}"
    else:
        results = geocode(text, use_cache)
        if not results:
            sys.exit(f"Could not find '{text}' in the USA.")
        lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
        label = results[0]["display_name"]
    if not (18 <= lat <= 72 and -170 <= lon <= -65):
        sys.exit(f"'{text}' is outside the USA.")
    return lat, lon, label


def geocode(text, use_cache):
    """One Nominatim lookup, limited to the USA."""
    def fetch():
        started = time.time()
        params = {"q": text, "format": "jsonv2", "countrycodes": "us", "limit": 1}
        response = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=20)
        external_calls.append(("geocode " + text, time.time() - started))
        response.raise_for_status()
        return response.json()

    normalised = " ".join(text.lower().split())
    key = hashlib.sha1(normalised.encode()).hexdigest()[:16]
    return load_cached(f"geocode_{key}.json", fetch, use_cache)


# ---------------------------------------------------------------- 2. route
def fetch_routes(start, finish, use_cache=True):
    """One OSRM call -> raw routes, fastest first, then any alternatives it offers."""
    def fetch():
        started = time.time()
        url = f"{OSRM_URL}/{start[1]},{start[0]};{finish[1]},{finish[0]}"
        params = {"overview": "full", "geometries": "geojson", "steps": "true", "alternatives": "3"}
        response = requests.get(url, params=params, headers=HEADERS, timeout=60)
        external_calls.append(("route", time.time() - started))
        if response.status_code != 400:      # OSRM reports "no route" as HTTP 400 with a JSON body
            response.raise_for_status()
        return response.json()

    name = f"routes_{start[0]:.4f}_{start[1]:.4f}_{finish[0]:.4f}_{finish[1]:.4f}.json"
    found = lambda data: data.get("code") == "Ok"         # failures are not saved
    answer = load_cached(name, fetch, use_cache, worth_keeping=found)
    if answer.get("code") in ("NoRoute", "NoSegment"):
        sys.exit("No road route between these two places.")
    if answer.get("code") != "Ok":
        sys.exit(f"Routing failed: {answer.get('code')} {answer.get('message', '')}")
    return answer["routes"]


def parse_route(raw):
    """Raw OSRM route -> Route: the line, mile markers, and which highways it uses where."""
    coords = np.array(raw["geometry"]["coordinates"])
    mile_at = miles_along(coords)
    sections = highway_sections(raw, mile_at)
    return Route(
        coords=coords,
        mile_at=mile_at,
        total_miles=float(mile_at[-1]),
        hours=raw["duration"] / 3600,
        section_start=np.array([start for start, _, _ in sections]),
        section_end=np.array([end for _, end, _ in sections]),
        section_highways=[highways for _, _, highways in sections],
    )


def miles_along(coords):
    """Mile marker of every vertex (great-circle distance summed along the line)."""
    lat = np.radians(coords[:, 1])
    lon = np.radians(coords[:, 0])
    half_chord = (np.sin(np.diff(lat) / 2) ** 2
                  + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(np.diff(lon) / 2) ** 2)
    segment_miles = EARTH_RADIUS_MILES * 2 * np.arcsin(np.sqrt(half_chord))
    return np.concatenate([[0.0], np.cumsum(segment_miles)])


def highway_sections(raw, mile_at):
    """[start mile, end mile, highways] for each stretch of the route.

    An OSRM step is one stretch of road between two manoeuvres and names its highway in `ref`.
    Consecutive steps share their end vertex, so each step's first vertex is known exactly.
    """
    steps = [step for leg in raw["legs"] for step in leg["steps"]]
    vertices_in_steps = sum(len(step["geometry"]["coordinates"]) - 1 for step in steps)
    vertices_line_up = vertices_in_steps == len(mile_at) - 1
    miles_per_metre = mile_at[-1] / max(raw["distance"], EPSILON)   # fallback if they don't

    sections = []
    vertex = 0
    metres = 0.0
    for step in steps:
        start = mile_at[vertex] if vertices_line_up else metres * miles_per_metre
        vertex = min(vertex + len(step["geometry"]["coordinates"]) - 1, len(mile_at) - 1)
        metres += step["distance"]
        end = mile_at[vertex] if vertices_line_up else metres * miles_per_metre
        highways = highways_in(step.get("ref", ""), from_route=True)
        if sections and sections[-1][2] == highways:
            sections[-1][1] = end                 # same highways as the previous step: extend it
        else:
            sections.append([start, end, highways])
    return sections


def highways_in(text, from_route):
    """'I 40; US 287' or 'I-40, EXIT 76 & SR-72' -> {('I','40'), ('US','287'), ('S','72')}.

    Interstates and US highways keep their type; every state/county road becomes 'S'.
    """
    if from_route:
        pattern = r"\b(I|US|[A-Z]{2})[ -]?(\d+)"                     # OSRM: 'TX 71', 'CA 58'
    else:
        pattern = r"\b(I|US|SR|SH|CR|HWY|RT|ROUTE)[ -]?(\d+)"         # station addresses
    found = set()
    for kind, number in re.findall(pattern, (text or "").upper()):
        found.add((kind if kind in ("I", "US") else "S", number))
    return frozenset(found)


def highways_near(route, mile):
    """Highways the route is on within HIGHWAY_TOLERANCE_MILES of a mile marker."""
    first = np.searchsorted(route.section_end, mile - HIGHWAY_TOLERANCE_MILES, side="left")
    last = np.searchsorted(route.section_start, mile + HIGHWAY_TOLERANCE_MILES, side="right")
    highways = set()
    for section in range(first, last):
        highways |= route.section_highways[section]
    return highways


# ---------------------------------------------------------------- 3. match stations to the route
def load_stations():
    """All placed stations as arrays (the API does this once per process)."""
    with open(STATIONS_FILE, newline="") as file:
        rows = [row for row in csv.DictReader(file) if row["precision"] != "none"]
    return Stations(
        rows=rows,
        lat=np.array([float(row["lat"]) for row in rows]),
        lon=np.array([float(row["lon"]) for row in rows]),
        price=np.array([float(row["price"]) for row in rows]),
        max_off_route=np.array([strict_distance_limit(row) for row in rows]),
        highways=[highways_in(row["address"], from_route=False) for row in rows],
    )


def strict_distance_limit(row):
    """How far from the route this station may be under the strict rule (-1 = never)."""
    if row["precision"] in MAX_OFF_ROUTE:
        return MAX_OFF_ROUTE[row["precision"]]
    radius = float(row["town_radius_mi"]) if row["town_radius_mi"] else DEFAULT_TOWN_RADIUS_MILES
    if radius > BIG_CITY_RADIUS_MILES:
        return -1.0
    return min(radius + TOWN_EXTRA_MILES, TOWN_CAP_MILES)


def stations_near_route(stations, route):
    """Stations within a few miles of the line -> (station index, miles off the line, mile marker).

    Checking every station against every route segment is ~500 ms on a cross-country route.
    Three passes, each cheaper than the next is thorough, bring it under 20 ms:
      1. grid:    keep stations in a grid cell the route passes through (or a neighbouring cell)
      2. samples: nearest of the route points sampled every mile; drop stations too far from it
      3. exact:   distance to the route segments within 5 miles of that sample
    """
    limit = max(TOWN_CAP_MILES, RELAXED_OFF_ROUTE_MILES)
    sample_mile = np.arange(0.0, route.total_miles + SAMPLE_EVERY_MILES, SAMPLE_EVERY_MILES)
    sample_lat = np.interp(sample_mile, route.mile_at, route.coords[:, 1])
    sample_lon = np.interp(sample_mile, route.mile_at, route.coords[:, 0])

    index = stations_in_route_cells(stations, sample_lat, sample_lon)
    if not len(index):
        return index, np.empty(0), np.empty(0)

    nearest, distance = nearest_sample(stations, index, sample_lat, sample_lon)
    close = distance <= limit + SAMPLE_EVERY_MILES / 2
    index, nearest = index[close], nearest[close]

    near_mile = sample_mile[nearest]
    first_vertex = np.searchsorted(route.mile_at, near_mile - SEARCH_WINDOW_MILES, side="left")
    last_vertex = np.searchsorted(route.mile_at, near_mile + SEARCH_WINDOW_MILES, side="right")
    off_route = np.empty(len(index))
    mile = np.empty(len(index))
    for n, station in enumerate(index):
        off_route[n], mile[n] = project_onto_route(
            stations.lat[station], stations.lon[station], route, first_vertex[n], last_vertex[n])

    within = off_route <= limit
    return index[within], off_route[within], mile[within]


def grid_cell(lat, lon):
    """One integer per GRID_DEGREES x GRID_DEGREES cell (rows are 10,000 apart)."""
    row = np.floor(lat / GRID_DEGREES).astype(np.int64) + 2000
    column = np.floor(lon / GRID_DEGREES).astype(np.int64) + 4000
    return row * 10000 + column


def stations_in_route_cells(stations, sample_lat, sample_lon):
    """Indices of stations in a cell the route passes through, or one of its 8 neighbours."""
    route_cells = np.unique(grid_cell(sample_lat, sample_lon))
    neighbours = [route_cells + row_step * 10000 + column_step
                  for row_step in (-1, 0, 1) for column_step in (-1, 0, 1)]
    touched = np.unique(np.concatenate(neighbours))
    return np.flatnonzero(np.isin(grid_cell(stations.lat, stations.lon), touched))


def nearest_sample(stations, index, sample_lat, sample_lon):
    """For each station: which route sample is nearest, and how far it is (miles)."""
    lat = stations.lat[index][:, None]
    lon = stations.lon[index][:, None]
    miles_per_lon_degree = MILES_PER_DEGREE * np.cos(np.radians(lat))
    east = (sample_lon[None, :] - lon) * miles_per_lon_degree
    north = (sample_lat[None, :] - lat) * MILES_PER_DEGREE
    distance = np.hypot(east, north)
    nearest = distance.argmin(axis=1)
    return nearest, distance[np.arange(len(index)), nearest]


def project_onto_route(lat, lon, route, first_vertex, last_vertex):
    """Closest point of the route to a station -> (miles off the line, mile marker)."""
    first = max(first_vertex - 1, 0)
    last = min(last_vertex + 1, len(route.mile_at))
    # Flat map in miles with the station at (0, 0); accurate over a few miles.
    x = (route.coords[first:last, 0] - lon) * MILES_PER_DEGREE * math.cos(math.radians(lat))
    y = (route.coords[first:last, 1] - lat) * MILES_PER_DEGREE
    step_x, step_y = np.diff(x), np.diff(y)
    segment_length_squared = np.maximum(step_x ** 2 + step_y ** 2, 1e-12)
    # How far along each segment (0 = its start, 1 = its end) the closest point lies.
    along = np.clip(-(x[:-1] * step_x + y[:-1] * step_y) / segment_length_squared, 0, 1)
    distance = np.hypot(x[:-1] + along * step_x, y[:-1] + along * step_y)
    best = distance.argmin()
    segment_miles = route.mile_at[first + best + 1] - route.mile_at[first + best]
    return distance[best], route.mile_at[first + best] + along[best] * segment_miles


def match_stations(stations, route, near, relaxed_ranges=()):
    """Stations the truck may use, sorted by mile marker.

    Strict rule: close to the line, and on a highway the route uses at that point.
    Inside `relaxed_ranges`: any station within RELAXED_OFF_ROUTE_MILES.
    """
    matched = []
    for station, off_route, mile in zip(*near):
        named = stations.highways[station]
        on_route_highway = not named or bool(named & highways_near(route, mile))
        strict = off_route <= stations.max_off_route[station] and on_route_highway
        in_relaxed_range = any(start < mile < end for start, end in relaxed_ranges)
        relaxed = not strict and in_relaxed_range and off_route <= RELAXED_OFF_ROUTE_MILES
        if strict or relaxed:
            matched.append(Candidate(int(station), float(mile), float(off_route),
                                     float(stations.price[station]), relaxed))
    matched.sort(key=lambda candidate: (candidate.mile, candidate.price))
    return merge_same_spot(matched)


def merge_same_spot(candidates):
    """Of stations within SAME_SPOT_MILES of each other along the route, keep the cheaper."""
    kept = []
    for candidate in candidates:
        if kept and candidate.mile - kept[-1].mile < SAME_SPOT_MILES:
            if candidate.price < kept[-1].price:
                kept[-1] = candidate
        else:
            kept.append(candidate)
    return kept


def find_gap(station_miles, total_miles, start_fuel, reserve):
    """None if the truck can make it, else (last fuel mile, mile it runs dry, next fuel mile)."""
    usable = RANGE_MILES - reserve
    last_fuel = 0.0
    reach = min(start_fuel, RANGE_MILES) - reserve
    for mile in list(station_miles) + [total_miles]:
        if mile > reach + EPSILON:
            return last_fuel, max(reach, 0.0), mile
        last_fuel = mile
        reach = mile + usable
    return None


def match_with_gap_fallback(stations, route, near, start_fuel, reserve):
    """Strict rules; where the truck can't cross a gap, relaxed rules inside that gap only."""
    relaxed_ranges = []
    while True:
        candidates = match_stations(stations, route, near, relaxed_ranges)
        miles = [candidate.mile for candidate in candidates]
        gap = find_gap(miles, route.total_miles, start_fuel, reserve)
        if gap is None:
            return candidates, relaxed_ranges
        last_fuel, dry, next_fuel = gap
        if (last_fuel, next_fuel) in relaxed_ranges:      # already relaxed here; it didn't help
            raise TripImpossible(last_fuel, dry, next_fuel)
        relaxed_ranges.append((last_fuel, next_fuel))


# ---------------------------------------------------------------- 4. plan the stops
class StopSearch:
    """Exact search for the lowest (fuel cost + stop cost x number of stops).

    Why two numbers per station are enough. Take two stops in a row, A then B. If the truck
    leaves A not full and reaches B not empty, shifting fuel from the pricier of the two to the
    cheaper never costs more. Repeat, and every stop either FILLS UP or buys JUST ENOUGH to reach
    the next stop empty ("empty" = down to the reserve). So per station we keep:

        arrive_empty[k]   cheapest way to reach k with an empty tank
        leave_full[k]     cheapest way to drive away from k with a full tank

    Pseudocode (U = usable tank in miles, each purchase also pays the stop cost):

        leave_full[start] = 0
        for each station i, in order along the route:
            leave_full[i] = cheapest of
                arrive_empty[i] + price[i] * U                    # arrived empty, fill up
                leave_full[h]   + price[i] * dist(h, i)           # h = earlier fill-up in range
            for each k that i can reach:                          # buy just enough at i
                offer arrive_empty[k] = arrive_empty[i] + price[i] * dist(i, k)
            for each k up to one top-up beyond i's reach:         # leave i full, top up at j
                j = cheapest station that i can reach and that can reach k
                offer arrive_empty[k] = leave_full[i] + price[j] * (dist(i, k) - U)
        answer = arrive_empty[finish]

    The inner loops run as numpy operations over slices: about 4 ms for 200 stations.
    """

    START = 0

    def __init__(self, station_miles, station_prices, total_miles,
                 fuel_at_start, usable, stop_cost):
        self.count = len(station_miles)
        self.finish = self.count + 1
        self.usable = usable
        self.per_stop = stop_cost + TIE_BREAK_USD
        # Nodes along the road: 0 = start, 1..count = stations, count+1 = finish.
        # The start is "left full" at an imaginary point, so that fuel_at_start remains at mile 0.
        self.position = np.concatenate([[fuel_at_start - usable], station_miles, [total_miles]])
        per_mile = np.asarray(station_prices) / MILES_PER_GALLON
        self.price_per_mile = np.concatenate([[0.0], per_mile, [0.0]])

        nodes = self.count + 2
        self.arrive_empty = np.full(nodes, math.inf)
        self.leave_full = np.full(nodes, math.inf)
        self.leave_full[self.START] = 0.0
        # How each best value was reached, to rebuild the plan afterwards.
        self.empty_came_from = np.full(nodes, -1)       # node the truck drove from
        self.empty_bought_at = np.full(nodes, -1)       # station where that leg's fuel was bought
        self.full_came_from = np.full(nodes, -1)        # earlier fill-up, or itself (arrived empty)

        # For every node: the nodes within one tank behind it, one tank ahead, two tanks ahead.
        position = self.position
        one_tank = usable + EPSILON
        self.first_behind = np.searchsorted(position, position - one_tank, side="left")
        self.last_ahead = np.searchsorted(position, position + one_tank, side="right") - 1
        two_tanks_on = position + 2 * one_tank
        self.last_two_tanks_ahead = np.searchsorted(position, two_tanks_on, side="right") - 1

    def run(self):
        """Fill in the tables; returns [(station number, miles of fuel bought)] in trip order."""
        self.top_up_after(self.START)
        for station in range(1, self.count + 1):
            self.fill_up_at(station)
            self.buy_just_enough_at(station)
            self.top_up_after(station)
        return self.purchases()

    def fill_up_at(self, i):
        """leave_full[i]: refill after an earlier fill-up, or fill up after arriving empty."""
        earlier = slice(self.first_behind[i], i)
        miles_bought = self.position[i] - self.position[earlier]
        cost = self.leave_full[earlier] + self.price_per_mile[i] * miles_bought + self.per_stop
        cost[miles_bought <= EPSILON] = math.inf          # nothing to buy is not a stop
        if cost.size and cost.min() < self.leave_full[i]:
            best = int(cost.argmin())
            self.leave_full[i] = cost[best]
            self.full_came_from[i] = earlier.start + best

        a_full_tank = self.price_per_mile[i] * self.usable
        after_arriving_empty = self.arrive_empty[i] + a_full_tank + self.per_stop
        if after_arriving_empty < self.leave_full[i]:
            self.leave_full[i] = after_arriving_empty
            self.full_came_from[i] = i

    def buy_just_enough_at(self, i):
        """From arriving empty at i: buy exactly the fuel to reach each node within range."""
        if not math.isfinite(self.arrive_empty[i]):
            return
        targets = slice(i + 1, self.last_ahead[i] + 1)
        miles_bought = self.position[targets] - self.position[i]
        cost = self.arrive_empty[i] + self.price_per_mile[i] * miles_bought + self.per_stop
        self.offer_arrive_empty(targets, cost, came_from=i, bought_at=i)

    def top_up_after(self, i):
        """From leaving i full: top up at the cheapest station on the way, to reach further."""
        if not math.isfinite(self.leave_full[i]):
            return
        # Stations i can reach, and the nodes just beyond its reach.
        on_the_way = slice(i + 1, min(self.last_ahead[i], self.count) + 1)
        targets = slice(self.last_ahead[i] + 1, self.last_two_tanks_ahead[i] + 1)
        if on_the_way.stop <= on_the_way.start or targets.stop <= targets.start:
            return

        # A top-up station must be able to reach the target, so for each target the choice is
        # "the cheapest station from the first one in range of it, onwards".
        cheapest_price, cheapest_at = cheapest_from_each_onwards(self.price_per_mile[on_the_way])
        first_in_range = np.maximum(self.first_behind[targets], on_the_way.start) - on_the_way.start
        has_top_up = first_in_range < len(cheapest_price)
        first_in_range = np.minimum(first_in_range, len(cheapest_price) - 1)

        miles_bought = self.position[targets] - self.position[i] - self.usable
        cost = self.leave_full[i] + cheapest_price[first_in_range] * miles_bought + self.per_stop
        cost[~has_top_up] = math.inf
        bought_at = on_the_way.start + cheapest_at[first_in_range]
        self.offer_arrive_empty(targets, cost, came_from=i, bought_at=bought_at)

    def offer_arrive_empty(self, targets, cost, came_from, bought_at):
        """Keep each offered way of arriving empty that beats what the target already has."""
        better = cost < self.arrive_empty[targets]
        self.arrive_empty[targets][better] = cost[better]
        self.empty_came_from[targets][better] = came_from
        self.empty_bought_at[targets][better] = np.broadcast_to(bought_at, cost.shape)[better]

    def purchases(self):
        """Walk back from 'arrive at the finish empty' to list what was bought where."""
        if not math.isfinite(self.arrive_empty[self.finish]):
            return None
        bought = []
        node = self.finish
        arrived_empty = True
        while node != self.START:
            if arrived_empty:
                came_from = self.empty_came_from[node]
                station = self.empty_bought_at[node]
                if station == came_from:                  # bought just enough there
                    miles = self.position[node] - self.position[station]
                else:                                     # left `came_from` full, topped up here
                    miles = self.position[node] - self.position[came_from] - self.usable
                    arrived_empty = False
                bought.append((station, miles))
                node = came_from
            else:
                came_from = self.full_came_from[node]
                if came_from == node:                     # arrived empty and filled up
                    bought.append((node, self.usable))
                    arrived_empty = True
                else:                                     # refilled what was burned since then
                    bought.append((node, self.position[node] - self.position[came_from]))
                    node = came_from
        return [(int(station) - 1, float(miles)) for station, miles in reversed(bought)]


def cheapest_from_each_onwards(prices):
    """For each position: the lowest price from there to the end, and where it is.

    [3, 1, 2] -> prices [1, 1, 2], positions [1, 1, 2]. A running minimum taken from the right.
    """
    from_the_right = prices[::-1]
    lowest = np.minimum.accumulate(from_the_right)
    is_new_low = from_the_right <= lowest
    where = np.maximum.accumulate(np.where(is_new_low, np.arange(len(prices)), 0))
    return lowest[::-1], (len(prices) - 1 - where)[::-1]


def plan_stops(station_miles, station_prices, total_miles, start_fuel, reserve, stop_cost):
    """-> ([(station number, miles of fuel bought)], miles of fuel left). Raises TripImpossible."""
    usable = RANGE_MILES - reserve
    fuel_at_start = min(start_fuel, RANGE_MILES) - reserve
    if usable <= 0 or fuel_at_start < 0:
        raise TripImpossible(0.0, 0.0, 0.0)
    gap = find_gap(station_miles, total_miles, start_fuel, reserve)
    if gap:
        raise TripImpossible(*gap)
    if total_miles <= fuel_at_start:                      # the starting tank covers the trip
        return [], fuel_at_start - total_miles + reserve

    search = StopSearch(station_miles, station_prices, total_miles,
                        fuel_at_start, usable, stop_cost)
    return search.run(), reserve                          # the plan arrives exactly at the reserve


# ---------------------------------------------------------------- fallbacks: route, then reserve
class RouteOption:
    """One route OSRM returned; parsed and matched to stations only if it is needed."""

    def __init__(self, raw, stations):
        self.raw = raw
        self.stations = stations

    @cached_property
    def route(self):
        return parse_route(self.raw)

    @cached_property
    def near(self):
        return stations_near_route(self.stations, self.route)


def plan_on_first_workable_route(stations, options, start_fuel, reserve, stop_cost):
    """Try the routes fastest first -> (TripPlan or None, the fastest route's problem or None)."""
    fastest_problem = None
    for option in options:
        route = option.route
        try:
            candidates, relaxed_ranges = match_with_gap_fallback(
                stations, route, option.near, start_fuel, reserve)
            purchases, fuel_left = plan_stops(
                [candidate.mile for candidate in candidates],
                [candidate.price for candidate in candidates],
                route.total_miles, start_fuel, reserve, stop_cost)
        except TripImpossible as problem:
            if fastest_problem is None:
                problem.route = route
                fastest_problem = problem
            continue
        plan = TripPlan(route, candidates, relaxed_ranges, purchases, fuel_left, reserve,
                        fastest_problem)
        return plan, fastest_problem
    return None, fastest_problem


def largest_workable_reserve(stations, options, start_fuel, reserve, stop_cost):
    """Largest whole-mile reserve below `reserve` the trip can be done with, or None."""
    def works(miles):
        plan, _ = plan_on_first_workable_route(stations, options, start_fuel, miles, stop_cost)
        return plan is not None

    if not works(0):
        return None
    low, high = 0, math.ceil(reserve) - 1                 # `low` always works
    while low < high:
        middle = (low + high + 1) // 2
        if works(middle):
            low = middle
        else:
            high = middle - 1
    return float(low)


def plan_trip(stations, raw_routes, start_fuel, reserve, stop_cost):
    """Plan the trip; if it can't be done as asked, the route gives way first, then the reserve."""
    options = [RouteOption(raw, stations) for raw in raw_routes]

    # As asked: fastest route first, then the alternatives. plan is None if none can be fuelled.
    plan, problem = plan_on_first_workable_route(stations, options, start_fuel, reserve, stop_cost)

    # Last resort: give up part of the reserve. With a reserve of 0 there is nothing to give up.
    if plan is None and reserve > 0:
        smaller_reserve = largest_workable_reserve(
            stations, options, start_fuel, reserve, stop_cost)
        if smaller_reserve is not None:               # None = impossible even with no reserve
            plan, problem = plan_on_first_workable_route(
                stations, options, start_fuel, smaller_reserve, stop_cost)

    # Still nothing: report why the fastest route can't be done.
    if plan is None:
        raise problem
    return plan


# ---------------------------------------------------------------- 5. report
def stops_of(plan, stations):
    """The plan's purchases as printable stops."""
    stops = []
    for number, miles_bought in plan.purchases:
        candidate = plan.candidates[number]
        gallons = miles_bought / MILES_PER_GALLON
        stops.append(Stop(
            row=stations.rows[candidate.station],
            mile=candidate.mile,
            off_route=candidate.off_route,
            price=candidate.price,
            relaxed=candidate.relaxed,
            gallons=gallons,
            cost=gallons * candidate.price,
        ))
    return stops


def longest_gap(plan):
    """(miles, starting mile) of the longest stretch of the route without a usable station."""
    marks = [0.0] + [candidate.mile for candidate in plan.candidates] + [plan.route.total_miles]
    return max((after - before, before) for before, after in zip(marks, marks[1:]))


def impossible_message(problem, routes_offered, start_fuel, reserve):
    """Why the trip can't be done, in words."""
    gap = problem.next_fuel_mile - problem.last_fuel_mile
    can_go = problem.dry_mile - problem.last_fuel_mile
    where = "the start"
    if problem.last_fuel_mile > 0:
        where = f"the last station at mile {problem.last_fuel_mile:.0f}"
    what = "the next station"
    if problem.next_fuel_mile >= problem.route.total_miles:
        what = "the finish"
    lines = [
        f"Trip not possible: from {where} it is {gap:.0f} mi to {what} "
        f"(mile {problem.next_fuel_mile:.0f}), "
        f"but the truck can only go {can_go:.0f} mi from there "
        f"({RANGE_MILES:.0f} mi range, {reserve:.0f} mi reserve, "
        f"{start_fuel:.0f} mi of fuel at the start)."
    ]
    if reserve > 0:
        lines.append("It is not possible with a reserve of 0 either.")
    if gap > RANGE_MILES:
        lines.append("The price list has US stations only, so this also happens when the fastest "
                     "route leaves the USA.")
    if routes_offered > 1:
        lines.append(f"The {routes_offered - 1} alternative route(s) OSRM offered "
                     f"can't be fuelled either.")
    return "\n".join(lines)


def print_report(plan, stops, start, finish, args, local_ms, load_ms):
    route = plan.route
    spent = sum(stop.cost for stop in stops)
    gallons = sum(stop.gallons for stop in stops)
    from_tank = max(route.total_miles / MILES_PER_GALLON - gallons, 0)
    gap_miles, gap_from = longest_gap(plan)

    print(f"\nFrom:   {start[2]}\nTo:     {finish[2]}")
    print(f"Route:  {route.total_miles:.0f} mi, ~{route.hours:.1f} h driving   "
          f"| fuel burned: {route.total_miles / MILES_PER_GALLON:.1f} gal")
    if plan.fastest_route_problem:
        problem = plan.fastest_route_problem
        gap = problem.next_fuel_mile - problem.last_fuel_mile
        print(f"** Not the fastest route: the fastest ({problem.route.total_miles:.0f} mi, "
              f"~{problem.route.hours:.1f} h) has {gap:.0f} mi "
              f"without a station in our data (from mile {problem.last_fuel_mile:.0f}), "
              f"so a longer route is used. **")
    if plan.reserve < args.reserve:
        print(f"** Reserve of {args.reserve:.0f} mi can't be kept on this trip: planned with "
              f"{plan.reserve:.0f} mi, the largest that works. The tank will drop to "
              f"{plan.reserve:.0f} mi at some point. **")

    rules = "strict"
    if plan.relaxed_ranges:
        ranges = ", ".join(f"mile {first:.0f}-{last:.0f}" for first, last in plan.relaxed_ranges)
        rules = f"strict + relaxed in {ranges}"
    print(f"Stations considered on route: {len(plan.candidates)}  ({rules})   "
          f"reserve: {plan.reserve:.0f} mi   start fuel: {args.start_fuel:.0f} mi   "
          f"stop cost: ${args.stop_cost:g}")
    close_to_range = gap_miles > GAP_WARNING_SHARE * (RANGE_MILES - plan.reserve)
    print(f"Longest stretch without a station: {gap_miles:.0f} mi (from mile {gap_from:.0f})"
          + ("   ** close to the truck's range **" if close_to_range else "") + "\n")

    if not stops:
        print("No stops needed — the starting tank covers the trip.")
    for number, stop in enumerate(stops, 1):
        row = stop.row
        note = f"{row['precision']}, {stop.off_route:.2f} mi off"
        if stop.relaxed:
            note += ", relaxed"
        print(f"  {number}. mile {stop.mile:>6.0f}  {row['name'][:30]:30s} {row['city'][:16]:16s} "
              f"{row['state']}  ${stop.price:.3f}  {stop.gallons:6.1f} gal  ${stop.cost:8.2f}   "
              f"[{note}]")

    print(f"\nTotal spent on fuel: ${spent:.2f}  ({gallons:.1f} gal bought; "
          f"{from_tank:.1f} gal came from the starting tank; "
          f"arrives with {plan.fuel_left:.0f} mi left)")
    calls = ", ".join(f"{what} {seconds:.1f}s" for what, seconds in external_calls) or "all cached"
    print(f"External calls: {len(external_calls)}  ({calls})")
    print(f"Local work (match stations + plan stops): {local_ms:.0f} ms   "
          f"(loading the station file once: {load_ms:.0f} ms)")


MAP_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Fuel route</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0}</style></head><body><div id="map"></div><script>
const data = __DATA__;
const map = L.map('map');
// Esri street basemap (no key needed). OSM's own tile servers refuse pages opened from disk.
const tiles = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer';
L.tileLayer(tiles + '/tile/{z}/{y}/{x}', {attribution: 'Tiles © Esri', maxZoom: 19}).addTo(map);
const line = L.polyline(data.line, {color: '#2563eb', weight: 4}).addTo(map);
map.fitBounds(line.getBounds());
data.candidates.forEach(c =>
  L.circleMarker([c[0], c[1]], {radius: 3, color: '#888', weight: 1}).bindPopup(c[2]).addTo(map));
data.stops.forEach(s => L.marker([s[0], s[1]]).bindPopup(s[2]).addTo(map));
const end = (at, colour, label) =>
  L.circleMarker(at, {radius: 8, color: colour, fillOpacity: 1}).bindPopup(label).addTo(map);
end(data.start, '#16a34a', 'Start');
end(data.finish, '#dc2626', 'Finish');
</script></body></html>"""


def write_map(path, plan, stops, stations, start, finish):
    """Leaflet page: route line, every candidate station as a dot, stops as pins."""
    coords = plan.route.coords
    every = max(1, len(coords) // 3000)                   # at most ~3,000 points on the line
    line = [[round(lat, 5), round(lon, 5)] for lon, lat in coords[::every]]
    line.append([coords[-1][1], coords[-1][0]])

    dots = []
    for candidate in plan.candidates:
        row = stations.rows[candidate.station]
        popup = (f"{html.escape(row['name'])} — ${candidate.price:.3f} "
                 f"(mile {candidate.mile:.0f}, {row['precision']})")
        dots.append([float(row["lat"]), float(row["lon"]), popup])

    pins = []
    for number, stop in enumerate(stops, 1):
        row = stop.row
        popup = (f"<b>Stop {number}: {html.escape(row['name'])}</b><br>"
                 f"{html.escape(row['address'])}, {html.escape(row['city'])} {row['state']}<br>"
                 f"${stop.price:.3f}/gal · {stop.gallons:.1f} gal · ${stop.cost:.2f}<br>"
                 f"mile {stop.mile:.0f} · {row['precision']}")
        pins.append([float(row["lat"]), float(row["lon"]), popup])

    data = {"line": line, "candidates": dots, "stops": pins,
            "start": [start[0], start[1]], "finish": [finish[0], finish[1]]}
    path.write_text(MAP_PAGE.replace("__DATA__", json.dumps(data)))


def main():
    parser = argparse.ArgumentParser(description="Plan fuel stops between two US locations.")
    parser.add_argument("start")
    parser.add_argument("finish")
    parser.add_argument("--reserve", type=float, default=0,
                        help="never plan the tank below this many miles (default 0)")
    parser.add_argument("--start-fuel", type=float, default=RANGE_MILES,
                        help="miles of fuel at the start (default full)")
    parser.add_argument("--stop-cost", type=float, default=10,
                        help="what one stop is worth in $ (default 10; 0 = cheapest fuel)")
    parser.add_argument("--no-cache", action="store_true",
                        help="call Nominatim/OSRM even if the answer is saved")
    parser.add_argument("--no-open", action="store_true",
                        help="don't open the map in the browser")
    args = parser.parse_args()
    if not 0 <= args.reserve < RANGE_MILES:
        sys.exit("--reserve must be between 0 and 500.")
    use_cache = not args.no_cache

    started = time.perf_counter()
    stations = load_stations()
    load_ms = (time.perf_counter() - started) * 1000

    start = locate(args.start, use_cache)
    finish = locate(args.finish, use_cache)
    raw_routes = fetch_routes(start, finish, use_cache)

    started = time.perf_counter()
    try:
        plan = plan_trip(stations, raw_routes, args.start_fuel, args.reserve, args.stop_cost)
    except TripImpossible as problem:
        sys.exit(impossible_message(problem, len(raw_routes), args.start_fuel, args.reserve))
    local_ms = (time.perf_counter() - started) * 1000

    stops = stops_of(plan, stations)
    print_report(plan, stops, start, finish, args, local_ms, load_ms)

    MAP_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{args.start}-to-{args.finish}".lower()).strip("-")[:80]
    path = MAP_DIR / f"{slug}-final.html"
    write_map(path, plan, stops, stations, start, finish)
    print(f"Map: {path}")
    if not args.no_open:
        webbrowser.open(path.as_uri())


if __name__ == "__main__":
    main()
