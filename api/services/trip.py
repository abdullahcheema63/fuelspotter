"""The whole request: locate, route, match stations, plan stops, and shape the answer.

If the trip can't be done as asked, these give way in this order:
  a. station rules, inside the uncrossable gap only        (stations.match_with_gap_fallback)
  b. the route: the next one OSRM returned in the same call
  c. the reserve: the largest one that still works, with a warning
"""
import math
from dataclasses import dataclass, field
from functools import cached_property

from django.conf import settings

from api.exceptions import ServiceError, TripImpossible
from api.services import geocoding, routing
from api.services.planner import plan_stops
from api.services.stations import get_stations, match_with_gap_fallback, stations_near_route
from api.services.stats import RequestStats

GAP_WARNING_SHARE = 0.8           # warn when the longest gap is over 80% of the usable range


@dataclass
class TripPlan:
    route: routing.Route
    candidates: list
    relaxed_ranges: list          # mile ranges where the relaxed rule was needed
    purchases: list               # (candidate index, miles of fuel bought)
    fuel_left: float              # miles of fuel on arrival
    reserve: float                # reserve actually used
    fastest_route_problem: object = None   # TripImpossible, when an alternative route was used


@dataclass
class Trip:
    """A planned trip, with everything the response and the history record need."""
    start: geocoding.Location
    finish: geocoding.Location
    start_fuel_miles: float
    stop_cost_usd: float
    reserve_miles: float          # reserve configured (the plan may have used less)
    plan: TripPlan
    stations: object
    stats: RequestStats
    stops: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


class RouteOption:
    """One route OSRM returned; matched to stations only if it is needed."""

    def __init__(self, route, stations):
        self.route = route
        self.stations = stations

    @cached_property
    def near(self):
        return stations_near_route(self.stations, self.route)


# ---------------------------------------------------------------- planning with fallbacks
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


def plan_trip(stations, routes, start_fuel, reserve, stop_cost):
    """Plan the trip; if it can't be done as asked, the route gives way first, then the reserve."""
    options = [RouteOption(route, stations) for route in routes]

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
        raise explained(problem, len(routes), start_fuel, reserve)
    return plan


def explained(problem, routes_offered, start_fuel, reserve):
    """Give a TripImpossible its sentence and the numbers behind it."""
    gap = problem.next_fuel_mile - problem.last_fuel_mile
    can_go = problem.dry_mile - problem.last_fuel_mile
    where = 'the start'
    if problem.last_fuel_mile > 0:
        where = f'the last station at mile {problem.last_fuel_mile:.0f}'
    what = 'the next station'
    if problem.route is not None and problem.next_fuel_mile >= problem.route.total_miles:
        what = 'the finish'
    sentences = [
        f'Trip not possible: from {where} it is {gap:.0f} mi to {what} '
        f'(mile {problem.next_fuel_mile:.0f}), but the truck can only go {can_go:.0f} mi from '
        f'there ({settings.VEHICLE_RANGE_MILES:.0f} mi range, {reserve:.0f} mi reserve, '
        f'{start_fuel:.0f} mi of fuel at the start).'
    ]
    if reserve > 0:
        sentences.append('It is not possible with a reserve of 0 either.')
    if gap > settings.VEHICLE_RANGE_MILES:
        sentences.append('The price list has US stations only, so this also happens when the '
                         'fastest route leaves the USA.')
    if routes_offered > 1:
        sentences.append(f'The {routes_offered - 1} alternative route(s) offered by the routing '
                         f'service cannot be fuelled either.')
    problem.detail = ' '.join(sentences)
    problem.extra = {
        'out_of_fuel_at_mile': round(problem.dry_mile, 1),
        'last_fuel_mile': round(problem.last_fuel_mile, 1),
        'next_fuel_mile': round(problem.next_fuel_mile, 1),
    }
    return problem


# ---------------------------------------------------------------- the request
def build_trip(start_value, finish_value, start_fuel, stop_cost):
    """Locate, route, match and plan. Raises a ServiceError carrying what was known so far."""
    stats = RequestStats()
    known = {'stats': stats, 'start': None, 'finish': None}
    reserve = settings.RESERVE_MILES
    try:
        with stats.timed('geocode'):
            known['start'] = start = geocoding.resolve(start_value, stats)
            known['finish'] = finish = geocoding.resolve(finish_value, stats)
        with stats.timed('route'):
            routes = routing.get_routes(start, finish, stats)
        with stats.timed('plan'):
            stations = get_stations()
            plan = plan_trip(stations, routes, start_fuel, reserve, stop_cost)
    except ServiceError as error:
        error.known = known
        raise

    trip = Trip(start=start, finish=finish, start_fuel_miles=start_fuel, stop_cost_usd=stop_cost,
                reserve_miles=reserve, plan=plan, stations=stations, stats=stats)
    trip.stops = stops_of(trip)
    trip.warnings = warnings_for(trip)
    return trip


def stops_of(trip):
    """The plan's purchases as the stops shown to the client."""
    stops = []
    for sequence, (number, miles_bought) in enumerate(trip.plan.purchases, start=1):
        candidate = trip.plan.candidates[number]
        row = trip.stations.rows[candidate.station]
        gallons = miles_bought / settings.VEHICLE_MILES_PER_GALLON
        stops.append({
            'sequence': sequence,
            'station_id': row['opis_id'],
            'station_pk': row['id'],              # internal: for the history record
            'name': row['name'],
            'address': row['address'],
            'city': row['city'],
            'state': row['state'],
            'lat': row['lat'],
            'lng': row['lon'],
            'price_per_gallon': round(candidate.price, 4),
            'mile_marker': round(candidate.mile, 1),
            'gallons': round(gallons, 2),
            'cost_usd': round(gallons * candidate.price, 2),
            'location_precision': row['precision'],
            'off_route_miles': round(candidate.off_route, 2),
            'matched_by': 'relaxed' if candidate.relaxed else 'strict',
        })
    return stops


def longest_gap(plan):
    """(miles, starting mile) of the longest stretch of the route without a usable station."""
    marks = [0.0] + [candidate.mile for candidate in plan.candidates] + [plan.route.total_miles]
    return max((after - before, before) for before, after in zip(marks, marks[1:]))


def warnings_for(trip):
    plan = trip.plan
    warnings = []
    if plan.fastest_route_problem is not None:
        problem = plan.fastest_route_problem
        gap = problem.next_fuel_mile - problem.last_fuel_mile
        warnings.append(
            f'Not the fastest route: the fastest ({problem.route.total_miles:.0f} mi, '
            f'{problem.route.hours:.1f} h) has {gap:.0f} mi without a fuel station in our data '
            f'(from mile {problem.last_fuel_mile:.0f}), so a longer route is used.')
    if plan.reserve < trip.reserve_miles:
        warnings.append(
            f'The reserve of {trip.reserve_miles:.0f} mi cannot be kept on this trip: planned with '
            f'{plan.reserve:.0f} mi, the largest that works.')
    if plan.relaxed_ranges:
        ranges = ', '.join(f'mile {first:.0f}-{last:.0f}' for first, last in plan.relaxed_ranges)
        warnings.append(f'Station matching was relaxed to any station within 3 miles of the route '
                        f'in: {ranges}.')
    gap_miles, gap_from = longest_gap(plan)
    usable = settings.VEHICLE_RANGE_MILES - plan.reserve
    if gap_miles > GAP_WARNING_SHARE * usable:
        warnings.append(f'Longest stretch without a fuel station is {gap_miles:.0f} mi '
                        f'(from mile {gap_from:.0f}), close to the usable range of {usable:.0f} mi.')
    return warnings


# ---------------------------------------------------------------- the response
def summary_of(trip):
    route = trip.plan.route
    mpg = settings.VEHICLE_MILES_PER_GALLON
    gallons = round(sum(stop['gallons'] for stop in trip.stops), 2)
    burned = route.total_miles / mpg
    return {
        'distance_miles': round(route.total_miles, 1),
        'duration_hours': round(route.hours, 1),
        'fuel_burned_gallons': round(burned, 2),
        'gallons_purchased': gallons,
        'total_fuel_cost_usd': round(sum(stop['cost_usd'] for stop in trip.stops), 2),
        'number_of_stops': len(trip.stops),
        'gallons_from_starting_tank': round(max(burned - gallons, 0.0), 2),
        'fuel_left_on_arrival_miles': round(trip.plan.fuel_left, 1),
    }


def route_geojson(trip):
    """The route line and the stops as a GeoJSON FeatureCollection."""
    features = [{
        'type': 'Feature',
        'properties': {'kind': 'route'},
        'geometry': {'type': 'LineString', 'coordinates': trip.plan.route.line()},
    }]
    for stop in trip.stops:
        features.append({
            'type': 'Feature',
            'properties': {'kind': 'fuel_stop', 'sequence': stop['sequence'],
                           'station_id': stop['station_id'], 'name': stop['name']},
            'geometry': {'type': 'Point', 'coordinates': [stop['lng'], stop['lat']]},
        })
    return {'type': 'FeatureCollection', 'features': features}


def response_of(trip):
    """The JSON body for a planned trip."""
    plan = trip.plan
    stats = trip.stats
    gap_miles, _ = longest_gap(plan)
    public_stops = [{key: value for key, value in stop.items() if key != 'station_pk'}
                    for stop in trip.stops]
    return {
        'summary': summary_of(trip),
        'assumptions': {
            'range_miles': settings.VEHICLE_RANGE_MILES,
            'miles_per_gallon': settings.VEHICLE_MILES_PER_GALLON,
            'start_fuel_miles': trip.start_fuel_miles,
            'stop_cost_usd': trip.stop_cost_usd,
            'reserve_miles': plan.reserve,
            'notes': [
                'total_fuel_cost_usd is the fuel bought on the trip; stop_cost_usd only steers '
                'how many stops are worth making and is not charged.',
                'Distance, fuel and cost follow the highway route; the short drive to a station '
                'and back is not included (see off_route_miles on each stop).',
            ],
        },
        'start': {'lat': trip.start.lat, 'lng': trip.start.lng, 'label': trip.start.label},
        'finish': {'lat': trip.finish.lat, 'lng': trip.finish.lng, 'label': trip.finish.label},
        'fuel_stops': public_stops,
        'route': route_geojson(trip),
        'warnings': trip.warnings,
        'meta': {
            'external_calls': stats.external_calls,
            'cache': {'geocode_hits': stats.geocode_cache_hits, 'route_hit': stats.route_cache_hit},
            'candidate_stations': len(plan.candidates),
            'largest_gap_miles': round(gap_miles, 1),
            'used_alternative_route': plan.fastest_route_problem is not None,
            'timings_ms': {
                **{stage: round(ms, 1) for stage, ms in stats.timings_ms.items()},
                'total': round(stats.total_ms(), 1),
            },
        },
    }
