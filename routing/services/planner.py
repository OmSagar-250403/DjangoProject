"""Orchestration: geocode -> one routing call -> corridor scan -> fuel plan."""

import hashlib
import logging

from django.conf import settings
from django.core.cache import cache

from .fuel import UnreachableRouteError, find_corridor_stations, plan_fuel_stops
from .geocoding import GeocodingService
from .providers import OsrmRouter, ProviderError

logger = logging.getLogger(__name__)

ROUTE_CACHE_TTL_SECONDS = 60 * 60


class LocationNotFound(ProviderError):
    """Raised when a user-supplied place name cannot be geocoded."""


class RoutePlanner:
    def __init__(self, router=None, geocoder=None):
        self.router = router or OsrmRouter()
        self.geocoder = geocoder or GeocodingService()

    def plan(self, start_text, finish_text, use_cache=True, start_fuel_gallons=0.0):
        cache_key = self._cache_key(start_text, finish_text, start_fuel_gallons)
        if use_cache:
            cached = cache.get(cache_key)
            if cached is not None:
                cached = dict(cached)
                cached['cached'] = True
                return cached

        start = self.geocoder.resolve(start_text)
        if start is None:
            raise LocationNotFound(f'Could not locate start address: {start_text!r}.')
        finish = self.geocoder.resolve(finish_text)
        if finish is None:
            raise LocationNotFound(f'Could not locate finish address: {finish_text!r}.')

        # The single routing-provider call for this request.
        route = self.router.route(start, finish)

        candidates = find_corridor_stations(route)
        stops, total_cost, total_gallons = plan_fuel_stops(
            route.distance_miles, candidates, start_fuel_gallons=start_fuel_gallons
        )

        mpg = settings.VEHICLE_MILES_PER_GALLON
        result = {
            'cached': False,
            'start': {
                'query': start_text,
                'latitude': start[0],
                'longitude': start[1],
            },
            'finish': {
                'query': finish_text,
                'latitude': finish[0],
                'longitude': finish[1],
            },
            'route': {
                'distance_miles': round(route.distance_miles, 2),
                'duration_hours': round(route.duration_seconds / 3600, 2),
                'geometry_polyline': route.encoded_polyline,
                'geometry_precision': 5,
                'point_count': len(route.points),
            },
            'vehicle': {
                'max_range_miles': settings.VEHICLE_MAX_RANGE_MILES,
                'miles_per_gallon': mpg,
                'tank_capacity_gallons': round(
                    settings.VEHICLE_MAX_RANGE_MILES / mpg, 2
                ),
                'assumed_tank_at_start_gallons': 0.0,
            },
            'fuel_stops': stops,
            'summary': {
                'stop_count': len(stops),
                'expected_gallons_for_trip': round(route.distance_miles / mpg, 3),
                'total_gallons': round(total_gallons, 3),
                'total_fuel_cost_usd': round(total_cost, 2),
                'average_price_per_gallon': (
                    round(total_cost / total_gallons, 4) if total_gallons else 0.0
                ),
                'stations_considered': len(candidates),
                'routing_api_calls': 1,
            },
        }

        if use_cache:
            cache.set(cache_key, result, ROUTE_CACHE_TTL_SECONDS)
        return result

    @staticmethod
    def _cache_key(start_text, finish_text, start_fuel_gallons=0.0):
        raw = (
            f'{start_text.strip().lower()}|{finish_text.strip().lower()}'
            f'|{start_fuel_gallons:g}'
        )
        return 'route:' + hashlib.sha256(raw.encode()).hexdigest()[:32]
