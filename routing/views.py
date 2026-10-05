import logging

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from routing.models import FuelStation
from routing.serializers import RouteRequestSerializer
from routing.services.fuel import UnreachableRouteError
from routing.services.planner import LocationNotFound, RoutePlanner
from routing.services.providers import ProviderError

logger = logging.getLogger(__name__)


@api_view(['GET'])
def health(request):
    """Liveness plus a quick view of how much of the station data is usable."""
    total = FuelStation.objects.count()
    geocoded = FuelStation.objects.filter(
        latitude__isnull=False, longitude__isnull=False
    ).count()
    return Response(
        {
            'status': 'ok',
            'stations_total': total,
            'stations_geocoded': geocoded,
            'vehicle': {
                'max_range_miles': settings.VEHICLE_MAX_RANGE_MILES,
                'miles_per_gallon': settings.VEHICLE_MILES_PER_GALLON,
            },
        }
    )


@api_view(['POST', 'GET'])
def route(request):
    """Plan a fuel-optimal route between two US locations.

    POST ``{"start": "New York, NY", "finish": "Chicago, IL"}``, or the same
    pair as GET query parameters for quick browser checks.
    """
    data = request.data if request.method == 'POST' else request.query_params
    serializer = RouteRequestSerializer(data=data)
    serializer.is_valid(raise_exception=True)

    planner = RoutePlanner()
    try:
        result = planner.plan(
            serializer.validated_data['start'],
            serializer.validated_data['finish'],
        )
    except LocationNotFound as exc:
        return Response(
            {'error': 'location_not_found', 'detail': str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except UnreachableRouteError as exc:
        return Response(
            {'error': 'route_not_fuelable', 'detail': str(exc)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except ProviderError as exc:
        logger.warning('Upstream provider failed: %s', exc)
        return Response(
            {'error': 'upstream_provider_error', 'detail': str(exc)},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response(result)
