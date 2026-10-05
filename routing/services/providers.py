"""External map/routing providers.

The assignment caps us at one routing call per request, so ``OsrmRouter`` makes
exactly one HTTP call and returns everything downstream code needs: the full
shape, the total mileage, and the snapped endpoints.

Geocoding is deliberately separate. Station coordinates are resolved offline by
the ``geocode_stations`` command and stored in Postgres, so a request only ever
geocodes the two user-supplied place names (and those are cached too).
"""

import logging
import threading
import time

import polyline
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Raised when an upstream map provider cannot satisfy a request."""


class Route:
    """Normalised routing result, independent of which provider produced it."""

    __slots__ = ('points', 'distance_miles', 'duration_seconds', 'start', 'finish')

    def __init__(self, points, distance_miles, duration_seconds, start, finish):
        self.points = points
        self.distance_miles = distance_miles
        self.duration_seconds = duration_seconds
        self.start = start
        self.finish = finish

    @property
    def encoded_polyline(self):
        """Google-algorithm polyline (precision 5) for drawing on a map."""
        return polyline.encode(self.points, 5)


METERS_PER_MILE = 1609.344


class OsrmRouter:
    """Driving routes from an OSRM server. Free, keyless, full USA coverage."""

    def __init__(self, base_url=None, timeout=None):
        self.base_url = (base_url or settings.OSRM_BASE_URL).rstrip('/')
        self.timeout = timeout or settings.HTTP_TIMEOUT_SECONDS

    def route(self, start, finish):
        """One call to OSRM. ``start``/``finish`` are ``(lat, lon)`` tuples."""
        coords = f'{start[1]},{start[0]};{finish[1]},{finish[0]}'
        url = f'{self.base_url}/route/v1/driving/{coords}'
        params = {
            'overview': 'full',
            'geometries': 'polyline6',
            'steps': 'false',
            'alternatives': 'false',
        }
        try:
            response = requests.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderError(f'Routing provider unreachable: {exc}') from exc

        if response.status_code != 200:
            raise ProviderError(
                f'Routing provider returned HTTP {response.status_code}.'
            )

        payload = response.json()
        if payload.get('code') != 'Ok' or not payload.get('routes'):
            raise ProviderError(
                f"No drivable route found ({payload.get('code', 'unknown error')})."
            )

        leg = payload['routes'][0]
        # polyline6 => 6 decimal places of precision, not the default 5.
        points = polyline.decode(leg['geometry'], 6)
        if len(points) < 2:
            raise ProviderError('Routing provider returned a degenerate route.')

        return Route(
            points=points,
            distance_miles=leg['distance'] / METERS_PER_MILE,
            duration_seconds=leg['duration'],
            start=points[0],
            finish=points[-1],
        )


class NominatimGeocoder:
    """Forward geocoding via OpenStreetMap's Nominatim.

    Nominatim's usage policy allows at most one request per second, so calls are
    serialised behind a lock with a minimum spacing. That is tolerable here
    because station geocoding is a one-off offline batch and request-time
    geocoding is both cached and limited to two lookups.
    """

    _lock = threading.Lock()
    _last_call_at = 0.0
    _min_interval = 1.1

    def __init__(self, base_url=None, timeout=None, user_agent=None):
        self.base_url = (base_url or settings.NOMINATIM_BASE_URL).rstrip('/')
        self.timeout = timeout or settings.HTTP_TIMEOUT_SECONDS
        self.user_agent = user_agent or settings.GEOCODER_USER_AGENT

    def geocode(self, query, country_codes='us'):
        """Return ``(lat, lon, display_name)`` or ``None`` if unresolvable."""
        params = {
            'q': query,
            'format': 'jsonv2',
            'limit': 1,
            'addressdetails': 0,
        }
        if country_codes:
            params['countrycodes'] = country_codes

        self._throttle()
        try:
            response = requests.get(
                f'{self.base_url}/search',
                params=params,
                headers={'User-Agent': self.user_agent},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(f'Geocoding provider unreachable: {exc}') from exc

        if response.status_code != 200:
            raise ProviderError(
                f'Geocoding provider returned HTTP {response.status_code}.'
            )

        results = response.json()
        if not results:
            return None

        hit = results[0]
        return float(hit['lat']), float(hit['lon']), hit.get('display_name', '')

    @classmethod
    def _throttle(cls):
        with cls._lock:
            wait = cls._min_interval - (time.monotonic() - cls._last_call_at)
            if wait > 0:
                time.sleep(wait)
            cls._last_call_at = time.monotonic()
