"""Save every planning request, planned or failed, as a TripRecord."""
import uuid

from django.conf import settings
from django.db import transaction

from api.models import TripRecord, TripStop
from api.services.map_html import map_data_of
from api.services.trip import summary_of


def describe_input(value):
    """A start or finish as the client gave it, as one string."""
    if isinstance(value, dict):
        return f"{value['lat']},{value['lng']}"
    return str(value)[:255]


def location_fields(prefix, location):
    if location is None:
        return {}
    return {f'{prefix}_label': location.label[:255],
            f'{prefix}_lat': location.lat, f'{prefix}_lng': location.lng}


def record_planned(trip, start_value, finish_value, api_key=None):
    """Save a planned trip and its stops."""
    summary = summary_of(trip)
    with transaction.atomic():
        record = TripRecord.objects.create(
            public_id=uuid.uuid4(),
            map_data=map_data_of(trip),
            api_key=api_key,
            start_input=describe_input(start_value),
            finish_input=describe_input(finish_value),
            start_fuel_miles=trip.start_fuel_miles,
            stop_cost_usd=trip.stop_cost_usd,
            reserve_miles=trip.reserve_miles,
            **location_fields('start', trip.start),
            **location_fields('finish', trip.finish),
            status=TripRecord.Status.PLANNED,
            http_status=200,
            warnings=trip.warnings,
            distance_miles=summary['distance_miles'],
            duration_hours=summary['duration_hours'],
            number_of_stops=summary['number_of_stops'],
            gallons_purchased=summary['gallons_purchased'],
            total_fuel_cost_usd=summary['total_fuel_cost_usd'],
            fuel_left_on_arrival_miles=summary['fuel_left_on_arrival_miles'],
            reserve_used_miles=trip.plan.reserve,
            used_alternative_route=trip.plan.fastest_route_problem is not None,
            candidate_stations=len(trip.plan.candidates),
            external_calls=trip.stats.external_calls,
            elapsed_ms=round(trip.stats.total_ms(), 1),
        )
        TripStop.objects.bulk_create([
            TripStop(
                trip=record,
                station_id=stop['station_pk'],
                sequence=stop['sequence'],
                mile_marker=stop['mile_marker'],
                off_route_miles=stop['off_route_miles'],
                price_per_gallon=stop['price_per_gallon'],
                gallons=stop['gallons'],
                cost_usd=stop['cost_usd'],
                matched_by=stop['matched_by'],
            )
            for stop in trip.stops
        ])
    return record


def record_failed(error, start_value, finish_value, start_fuel, stop_cost, api_key=None):
    """Save a request that ended in a service error (location not found, no route, ...)."""
    known = getattr(error, 'known', {})
    stats = known.get('stats')
    return TripRecord.objects.create(
        api_key=api_key,
        start_input=describe_input(start_value),
        finish_input=describe_input(finish_value),
        start_fuel_miles=start_fuel,
        stop_cost_usd=stop_cost,
        reserve_miles=settings.RESERVE_MILES,
        **location_fields('start', known.get('start')),
        **location_fields('finish', known.get('finish')),
        status=TripRecord.Status.FAILED,
        http_status=error.status_code,
        error_code=error.code,
        error_detail=error.detail,
        external_calls=stats.external_calls if stats else 0,
        elapsed_ms=round(stats.total_ms(), 1) if stats else 0,
    )
