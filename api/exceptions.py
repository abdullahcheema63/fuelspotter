"""Errors the services raise, and how they become HTTP responses.

Every error response has a JSON body with `detail` (a sentence) and `code` (a stable name).
"""
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class ServiceError(Exception):
    """Base for errors with a known cause. Subclasses set the HTTP status and code."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = 'error'

    def __init__(self, detail, **extra):
        super().__init__(detail)
        self.detail = detail
        self.extra = extra          # further fields for the response body


class LocationNotFound(ServiceError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = 'location_not_found'


class OutsideUSA(ServiceError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = 'outside_usa'


class NoRouteFound(ServiceError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = 'no_route'


class TripImpossible(ServiceError):
    """The truck runs dry: no fuel between `last_fuel_mile` and `next_fuel_mile`."""

    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = 'trip_impossible'

    def __init__(self, last_fuel_mile, dry_mile, next_fuel_mile, route=None):
        super().__init__('The trip is not possible with this range.')
        self.last_fuel_mile = last_fuel_mile
        self.dry_mile = dry_mile
        self.next_fuel_mile = next_fuel_mile
        self.route = route


class UpstreamError(ServiceError):
    """Nominatim or OSRM answered with something we can't use."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = 'upstream_error'


class UpstreamTimeout(ServiceError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = 'upstream_timeout'


class MapNotFound(ServiceError):
    status_code = status.HTTP_404_NOT_FOUND
    code = 'not_found'


class MapLinkExpired(ServiceError):
    status_code = status.HTTP_410_GONE
    code = 'link_expired'


def exception_handler(exc, context):
    """DRF exception handler: service errors and validation errors in one shape."""
    if isinstance(exc, ServiceError):
        body = {'detail': exc.detail, 'code': exc.code, **exc.extra}
        return Response(body, status=exc.status_code)

    response = drf_exception_handler(exc, context)
    if response is None:
        return None
    if isinstance(exc, ValidationError):
        response.data = {'detail': 'Invalid input.', 'code': 'invalid_input', 'errors': response.data}
    elif isinstance(response.data, dict) and 'detail' in response.data:
        response.data['code'] = getattr(response.data['detail'], 'code', 'error')
    return response
