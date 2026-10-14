"""Step 1: turn a start or finish into coordinates.

Coordinates are used as given (no call). Text goes to Nominatim, limited to the USA, and the
answer is cached, so the same place is looked up once.
"""
import hashlib
import re
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from api.exceptions import LocationNotFound, OutsideUSA, UpstreamError, UpstreamTimeout

COORDINATES = re.compile(r'\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*')
FOUND_TTL_SECONDS = 30 * 24 * 3600
NOT_FOUND_TTL_SECONDS = 3600
SEARCH_RESULTS = 5
# Areas too large to drive to: "New York" can mean the state, whose centre is 250 miles from the city.
TOO_BROAD = {'country', 'state', 'region', 'county'}
# A box around the USA including Alaska and Hawaii. It also covers parts of Canada and Mexico.
USA_LAT = (18.0, 72.0)
USA_LNG = (-170.0, -65.0)


@dataclass(frozen=True)
class Location:
    lat: float
    lng: float
    label: str


def resolve(value, stats):
    """{'lat': .., 'lng': ..}, 'lat,lng' or a place name -> Location inside the USA."""
    if isinstance(value, dict):
        location = from_coordinates(value['lat'], value['lng'])
        shown = location.label
    else:
        shown = value
        as_numbers = COORDINATES.fullmatch(value)
        if as_numbers:
            location = from_coordinates(float(as_numbers.group(1)), float(as_numbers.group(2)))
        else:
            location = lookup(value, stats)
    in_usa = USA_LAT[0] <= location.lat <= USA_LAT[1] and USA_LNG[0] <= location.lng <= USA_LNG[1]
    if not in_usa:
        raise OutsideUSA(f"'{shown}' is outside the USA.")
    return location


def from_coordinates(lat, lng):
    return Location(lat=lat, lng=lng, label=f'{lat:.4f},{lng:.4f}')


def lookup(text, stats):
    """One Nominatim search, cached by the normalised text."""
    normalised = ' '.join(text.lower().split())
    key = 'geo:v2:' + hashlib.sha1(normalised.encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        stats.geocode_cache_hits += 1
    else:
        cached = search_nominatim(text, stats)
        cache.set(key, cached, FOUND_TTL_SECONDS if cached['found'] else NOT_FOUND_TTL_SECONDS)
    if not cached['found']:
        raise LocationNotFound(f"Could not find '{text}' in the USA.")
    return Location(lat=cached['lat'], lng=cached['lng'], label=cached['label'])


def search_nominatim(text, stats):
    """-> {'found': True, 'lat', 'lng', 'label'} or {'found': False}."""
    params = {'q': text, 'format': 'jsonv2', 'countrycodes': 'us', 'limit': SEARCH_RESULTS}
    headers = {'User-Agent': settings.NOMINATIM_USER_AGENT}
    stats.external_calls += 1
    try:
        response = requests.get(settings.NOMINATIM_URL, params=params, headers=headers,
                                timeout=settings.NOMINATIM_TIMEOUT_SECONDS)
        response.raise_for_status()
        results = response.json()
    except requests.Timeout:
        raise UpstreamTimeout('The geocoding service (Nominatim) took too long to answer.')
    except (requests.RequestException, ValueError):
        raise UpstreamError('The geocoding service (Nominatim) could not be reached.')

    if not results:
        return {'found': False}
    try:
        best = most_specific(results)
        return {'found': True, 'lat': float(best['lat']), 'lng': float(best['lon']),
                'label': best['display_name']}
    except (KeyError, TypeError, ValueError):
        raise UpstreamError('The geocoding service (Nominatim) sent an answer we could not read.')


def most_specific(results):
    """Nominatim's best match that is a place, not a state or county; else its best match."""
    for result in results:
        if result.get('addresstype') not in TOO_BROAD:
            return result
    return results[0]
