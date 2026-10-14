"""Step 3: which stations can the truck use on this route?

Stations are read from the database once per process into arrays. A station is usable if it is
close to the route line and on a highway the route uses at that point; where that leaves a gap
the truck can't cross, any station within 3 miles is accepted inside that gap.
"""
import math
import threading
import time
from dataclasses import dataclass

import numpy as np
from django.conf import settings
from django.db.models import Count, Max

from api.exceptions import TripImpossible
from api.models import FuelStation
from api.services.highways import decode

MILES_PER_DEGREE = 69.09          # one degree of latitude

# How far a station may sit from the route line, by how well we know its location.
MAX_OFF_ROUTE = {'exact': 1.0, 'exit': 1.5}
TOWN_EXTRA_MILES = 1.0            # town-level station: town radius + this ...
TOWN_CAP_MILES = 3.5              # ... but never more than this
BIG_CITY_RADIUS_MILES = 5.0       # a big-city centre says nothing about where the station is
DEFAULT_TOWN_RADIUS_MILES = 2.0
RELAXED_OFF_ROUTE_MILES = 3.0     # used only inside a gap the truck can't cross
HIGHWAY_TOLERANCE_MILES = 2.0     # station's highway must be used by the route this close to it
SAME_SPOT_MILES = 0.1             # stations this close along the route count as one

SAMPLE_EVERY_MILES = 1.0          # route points used to find nearby stations
SEARCH_WINDOW_MILES = 5.0         # exact distance is measured this far either side of it
GRID_DEGREES = 0.1                # grid cell size (about 7 miles)

SNAPSHOT_CHECK_SECONDS = 30       # how often to look for a newer station load
EPSILON = 1e-9


@dataclass
class Stations:
    """Every usable station, as arrays, for fast matching."""
    rows: list                    # dicts: id, opis_id, name, address, city, state, precision
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray
    max_off_route: np.ndarray     # strict distance limit per station (-1 = never matches)
    highways: list                # highways named in each address


@dataclass
class Candidate:
    """A station the truck may use on one particular route."""
    station: int                  # index into Stations
    mile: float                   # mile marker along the route
    off_route: float              # miles from the route line
    price: float
    relaxed: bool                 # accepted by the relaxed rule only


# ---------------------------------------------------------------- stations in memory
_snapshot = None
_snapshot_version = None
_snapshot_checked = 0.0
_snapshot_lock = threading.Lock()


def get_stations():
    """The station arrays; reloaded when `load_stations` has changed the table."""
    global _snapshot, _snapshot_version, _snapshot_checked
    now = time.monotonic()
    if _snapshot is not None and now - _snapshot_checked < SNAPSHOT_CHECK_SECONDS:
        return _snapshot
    with _snapshot_lock:
        version = table_version()
        if _snapshot is None or version != _snapshot_version:
            _snapshot = load_stations()
            _snapshot_version = version
        _snapshot_checked = now
        return _snapshot


def table_version():
    """Changes whenever stations are loaded again."""
    summary = FuelStation.objects.aggregate(count=Count('id'), newest=Max('updated_at'))
    return summary['count'], summary['newest']


def load_stations():
    """Active, placed stations from the database -> Stations."""
    rows = list(
        FuelStation.objects
        .filter(is_active=True)
        .exclude(precision=FuelStation.Precision.NONE)
        .values('id', 'opis_id', 'name', 'address', 'city', 'state', 'price', 'town_radius_mi',
                'lat', 'lon', 'precision', 'highway_keys')
    )
    return Stations(
        rows=rows,
        lat=np.array([row['lat'] for row in rows], dtype=float),
        lon=np.array([row['lon'] for row in rows], dtype=float),
        price=np.array([float(row['price']) for row in rows], dtype=float),
        max_off_route=np.array([strict_distance_limit(row) for row in rows], dtype=float),
        highways=[decode(row['highway_keys']) for row in rows],
    )


def strict_distance_limit(row):
    """How far from the route this station may be under the strict rule (-1 = never)."""
    if row['precision'] in MAX_OFF_ROUTE:
        return MAX_OFF_ROUTE[row['precision']]
    radius = row['town_radius_mi'] or DEFAULT_TOWN_RADIUS_MILES
    if radius > BIG_CITY_RADIUS_MILES:
        return -1.0
    return min(radius + TOWN_EXTRA_MILES, TOWN_CAP_MILES)


# ---------------------------------------------------------------- stations near the route
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
    first_vertex = np.searchsorted(route.mile_at, near_mile - SEARCH_WINDOW_MILES, side='left')
    last_vertex = np.searchsorted(route.mile_at, near_mile + SEARCH_WINDOW_MILES, side='right')
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


# ---------------------------------------------------------------- which of them are usable
def highways_near(route, mile):
    """Highways the route is on within HIGHWAY_TOLERANCE_MILES of a mile marker."""
    first = np.searchsorted(route.section_end, mile - HIGHWAY_TOLERANCE_MILES, side='left')
    last = np.searchsorted(route.section_start, mile + HIGHWAY_TOLERANCE_MILES, side='right')
    highways = set()
    for section in range(first, last):
        highways |= route.section_highways[section]
    return highways


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
    usable = settings.VEHICLE_RANGE_MILES - reserve
    last_fuel = 0.0
    reach = min(start_fuel, settings.VEHICLE_RANGE_MILES) - reserve
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
