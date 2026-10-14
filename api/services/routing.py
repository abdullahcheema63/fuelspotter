"""Step 2: the driving route, from one OSRM call.

The call returns the fastest route and any alternatives OSRM offers. Each is parsed into a
Route (line, mile markers, which highways it is on where) and the result is cached.
"""
from dataclasses import dataclass

import numpy as np
import requests
from django.conf import settings
from django.core.cache import cache

from api.exceptions import NoRouteFound, UpstreamError, UpstreamTimeout
from api.services.highways import highways_in

EARTH_RADIUS_MILES = 3958.8
ROUTE_TTL_SECONDS = 24 * 3600
MAX_LINE_POINTS = 3000            # points kept in the line sent to clients
EPSILON = 1e-9


@dataclass
class Route:
    coords: np.ndarray            # [lon, lat] of every vertex
    mile_at: np.ndarray           # mile marker of every vertex
    total_miles: float
    hours: float
    section_start: np.ndarray     # sections: stretches of the route on the same highways
    section_end: np.ndarray
    section_highways: list

    def line(self):
        """The route as [lon, lat] pairs, thinned to at most MAX_LINE_POINTS."""
        every = max(1, len(self.coords) // MAX_LINE_POINTS)
        points = [[round(float(lon), 5), round(float(lat), 5)] for lon, lat in self.coords[::every]]
        last = [round(float(self.coords[-1][0]), 5), round(float(self.coords[-1][1]), 5)]
        if points[-1] != last:
            points.append(last)
        return points


def get_routes(start, finish, stats):
    """Routes from start to finish, fastest first. One OSRM call, or none if cached."""
    key = f'route:v1:{start.lat:.4f},{start.lng:.4f}:{finish.lat:.4f},{finish.lng:.4f}'
    routes = cache.get(key)
    if routes is not None:
        stats.route_cache_hit = True
        return routes
    routes = [parse_route(raw) for raw in fetch_routes(start, finish, stats)]
    cache.set(key, routes, ROUTE_TTL_SECONDS)
    return routes


def fetch_routes(start, finish, stats):
    """The OSRM call -> raw routes."""
    url = f'{settings.OSRM_URL}/{start.lng},{start.lat};{finish.lng},{finish.lat}'
    params = {'overview': 'full', 'geometries': 'geojson', 'steps': 'true', 'alternatives': '3'}
    headers = {'User-Agent': settings.NOMINATIM_USER_AGENT}
    stats.external_calls += 1
    try:
        response = requests.get(url, params=params, headers=headers,
                                timeout=settings.OSRM_TIMEOUT_SECONDS)
        if response.status_code != 400:      # OSRM reports "no route" as HTTP 400 with a JSON body
            response.raise_for_status()
        answer = response.json()
    except requests.Timeout:
        raise UpstreamTimeout('The routing service (OSRM) took too long to answer.')
    except (requests.RequestException, ValueError):
        raise UpstreamError('The routing service (OSRM) could not be reached.')

    code = answer.get('code') if isinstance(answer, dict) else None
    if code in ('NoRoute', 'NoSegment'):
        raise NoRouteFound('There is no road route between these two places.')
    if code != 'Ok' or not answer.get('routes'):
        raise UpstreamError(f'The routing service (OSRM) failed: {code or "unreadable answer"}.')
    return answer['routes']


def parse_route(raw):
    """Raw OSRM route -> Route."""
    try:
        coords = np.array(raw['geometry']['coordinates'], dtype=float)
        mile_at = miles_along(coords)
        sections = highway_sections(raw, mile_at)
        hours = raw['duration'] / 3600
    except (KeyError, TypeError, ValueError, IndexError):
        raise UpstreamError('The routing service (OSRM) sent a route we could not read.')
    return Route(
        coords=coords,
        mile_at=mile_at,
        total_miles=float(mile_at[-1]),
        hours=hours,
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
    steps = [step for leg in raw['legs'] for step in leg['steps']]
    vertices_in_steps = sum(len(step['geometry']['coordinates']) - 1 for step in steps)
    vertices_line_up = vertices_in_steps == len(mile_at) - 1
    miles_per_metre = mile_at[-1] / max(raw['distance'], EPSILON)   # fallback if they don't

    sections = []
    vertex = 0
    metres = 0.0
    for step in steps:
        start = mile_at[vertex] if vertices_line_up else metres * miles_per_metre
        vertex = min(vertex + len(step['geometry']['coordinates']) - 1, len(mile_at) - 1)
        metres += step['distance']
        end = mile_at[vertex] if vertices_line_up else metres * miles_per_metre
        highways = highways_in(step.get('ref', ''), from_route=True)
        if sections and sections[-1][2] == highways:
            sections[-1][1] = end                 # same highways as the previous step: extend it
        else:
            sections.append([start, end, highways])
    return sections
