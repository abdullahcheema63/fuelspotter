"""Request validation (format only) and the response shapes for the OpenAPI schema."""
from django.conf import settings
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

MAX_STOP_COST_USD = 10

LOCATION_SCHEMA = {
    'oneOf': [
        {'type': 'string', 'minLength': 1, 'maxLength': 200,
         'description': "A place in the USA, or 'lat,lng'.", 'example': 'Chicago, IL'},
        {'type': 'object', 'required': ['lat', 'lng'],
         'properties': {'lat': {'type': 'number'}, 'lng': {'type': 'number'}},
         'example': {'lat': 32.7767, 'lng': -96.797}},
    ],
}


@extend_schema_field(LOCATION_SCHEMA)
class LocationField(serializers.Field):
    """A place name, or coordinates as {"lat": .., "lng": ..}."""

    default_error_messages = {
        'blank': 'This field may not be blank.',
        'too_long': 'Ensure this field has no more than 200 characters.',
        'bad_type': 'Give a place name, or an object with "lat" and "lng".',
        'bad_coordinates': '"lat" and "lng" must be numbers.',
        'out_of_range': 'lat must be between -90 and 90, lng between -180 and 180.',
    }

    def to_internal_value(self, data):
        if isinstance(data, str):
            text = data.strip()
            if not text:
                self.fail('blank')
            if len(text) > 200:
                self.fail('too_long')
            return text
        if isinstance(data, dict) and set(data) >= {'lat', 'lng'}:
            numbers = (data['lat'], data['lng'])
            if any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in numbers):
                self.fail('bad_coordinates')
            lat, lng = float(data['lat']), float(data['lng'])
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                self.fail('out_of_range')
            return {'lat': lat, 'lng': lng}
        self.fail('bad_type')

    def to_representation(self, value):
        return value


class OptionsSerializer(serializers.Serializer):
    start_fuel_miles = serializers.FloatField(
        required=False, min_value=0, max_value=settings.VEHICLE_RANGE_MILES,
        help_text='Miles of fuel in the tank at the start. Default: START_FUEL_MILES (full tank).')
    stop_cost_usd = serializers.FloatField(
        required=False, min_value=0, max_value=MAX_STOP_COST_USD,
        help_text='What one fuel stop is worth, in USD. 0 = cheapest fuel whatever the number '
                  'of stops. Default: STOP_COST_USD.')


class RouteRequestSerializer(serializers.Serializer):
    start = LocationField()
    finish = LocationField()
    options = OptionsSerializer(required=False)

    def planning_inputs(self):
        """(start, finish, start_fuel_miles, stop_cost_usd) with the configured defaults filled in."""
        data = self.validated_data
        options = data.get('options') or {}
        return (data['start'], data['finish'],
                options.get('start_fuel_miles', settings.START_FUEL_MILES),
                options.get('stop_cost_usd', settings.STOP_COST_USD))


# ---------------------------------------------------------------- response shapes (schema only)
class SummarySerializer(serializers.Serializer):
    distance_miles = serializers.FloatField()
    duration_hours = serializers.FloatField()
    fuel_burned_gallons = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    total_fuel_cost_usd = serializers.FloatField(help_text='Money spent on fuel during the trip.')
    number_of_stops = serializers.IntegerField()
    gallons_from_starting_tank = serializers.FloatField()
    fuel_left_on_arrival_miles = serializers.FloatField()


class AssumptionsSerializer(serializers.Serializer):
    range_miles = serializers.FloatField()
    miles_per_gallon = serializers.FloatField()
    start_fuel_miles = serializers.FloatField()
    stop_cost_usd = serializers.FloatField()
    reserve_miles = serializers.FloatField()
    notes = serializers.ListField(child=serializers.CharField())


class PlaceSerializer(serializers.Serializer):
    lat = serializers.FloatField()
    lng = serializers.FloatField()
    label = serializers.CharField()


class FuelStopSerializer(serializers.Serializer):
    sequence = serializers.IntegerField()
    station_id = serializers.IntegerField(help_text='OPIS truck stop id.')
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    lat = serializers.FloatField()
    lng = serializers.FloatField()
    price_per_gallon = serializers.FloatField()
    mile_marker = serializers.FloatField(help_text='Miles from the start, along the route.')
    gallons = serializers.FloatField()
    cost_usd = serializers.FloatField()
    location_precision = serializers.ChoiceField(choices=['exact', 'exit', 'town'])
    off_route_miles = serializers.FloatField(help_text='Straight-line distance from the route.')
    matched_by = serializers.ChoiceField(choices=['strict', 'relaxed'])


class CacheMetaSerializer(serializers.Serializer):
    geocode_hits = serializers.IntegerField()
    route_hit = serializers.BooleanField()


class MetaSerializer(serializers.Serializer):
    external_calls = serializers.IntegerField(help_text='Calls made to Nominatim and OSRM.')
    cache = CacheMetaSerializer()
    candidate_stations = serializers.IntegerField()
    largest_gap_miles = serializers.FloatField()
    used_alternative_route = serializers.BooleanField()
    timings_ms = serializers.DictField(child=serializers.FloatField())


class RouteResponseSerializer(serializers.Serializer):
    map_url = serializers.URLField(
        help_text='Open in a browser to see this trip on a map. Needs no API key; the link is '
                  'temporary (MAP_LINK_HOURS).')
    summary = SummarySerializer()
    assumptions = AssumptionsSerializer()
    start = PlaceSerializer()
    finish = PlaceSerializer()
    fuel_stops = FuelStopSerializer(many=True)
    route = serializers.DictField(
        help_text='GeoJSON FeatureCollection: the route LineString and one Point per stop.')
    warnings = serializers.ListField(child=serializers.CharField())
    meta = MetaSerializer()


class ErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()
    code = serializers.CharField()
