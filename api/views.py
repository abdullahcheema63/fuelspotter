from datetime import timedelta

from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api.exceptions import MapLinkExpired, MapNotFound, ServiceError
from api.models import TripRecord
from api.permissions import api_key_of
from api.serializers import ErrorSerializer, RouteRequestSerializer, RouteResponseSerializer
from api.services import history, map_html
from api.services.trip import build_trip, response_of

ERROR_RESPONSES = {
    400: OpenApiResponse(ErrorSerializer, description='Invalid input.'),
    403: OpenApiResponse(ErrorSerializer, description='Missing or invalid API key.'),
    422: OpenApiResponse(ErrorSerializer, description=(
        'Location not found, outside the USA, no road route, or the trip is not possible '
        'with the vehicle range.')),
    502: OpenApiResponse(ErrorSerializer, description='The geocoding or routing service failed.'),
    504: OpenApiResponse(ErrorSerializer, description='The geocoding or routing service timed out.'),
}


def plan_and_record(request, start, finish, start_fuel, stop_cost):
    """Plan the trip and save it to the history, whether it was planned or failed."""
    api_key = api_key_of(request)
    try:
        trip = build_trip(start, finish, start_fuel, stop_cost)
    except ServiceError as error:
        history.record_failed(error, start, finish, start_fuel, stop_cost, api_key)
        raise
    record = history.record_planned(trip, start, finish, api_key)
    return trip, record


class RouteView(APIView):
    @extend_schema(
        summary='Plan fuel stops for a trip',
        description=(
            'Returns the driving route between two places in the USA, the fuel stops that '
            'minimise fuel cost (without stopping more often than it is worth), the total '
            'spent on fuel, and a link to the trip on a map. '
            'Vehicle: 500-mile range, 10 miles per gallon.'),
        request=RouteRequestSerializer,
        responses={200: RouteResponseSerializer, **ERROR_RESPONSES},
    )
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        trip, record = plan_and_record(request, *serializer.planning_inputs())
        map_path = reverse('route-map', args=[record.public_id])
        body = {'map_url': request.build_absolute_uri(map_path), **response_of(trip)}
        return Response(body)


class RouteMapView(APIView):
    """The map page for one planned trip. The link's random id is the permission to see it."""

    permission_classes = [AllowAny]

    @extend_schema(exclude=True)    # a page for browsers, reached through `map_url`; not part of the API
    def get(self, request, public_id):
        record = TripRecord.objects.filter(public_id=public_id, map_data__isnull=False).first()
        if record is None:
            raise MapNotFound('There is no trip map at this address.')
        if settings.MAP_LINK_HOURS > 0:
            expires = record.created_at + timedelta(hours=settings.MAP_LINK_HOURS)
            if timezone.now() > expires:
                raise MapLinkExpired(
                    f'This map link has expired (links work for {settings.MAP_LINK_HOURS:g} '
                    f'hours). Plan the trip again to get a new one.')
        page = HttpResponse(map_html.render(record.map_data), content_type='text/html; charset=utf-8')
        page['Referrer-Policy'] = 'no-referrer'      # keep the link out of requests for map tiles
        return page
