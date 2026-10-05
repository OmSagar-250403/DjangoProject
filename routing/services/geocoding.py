"""Cached forward geocoding, backed by the ``GeocodeCache`` table."""

import logging

from routing.models import GeocodeCache

from .providers import NominatimGeocoder

logger = logging.getLogger(__name__)


class GeocodingService:
    """Resolves place names to coordinates, hitting the network only on a miss."""

    def __init__(self, provider=None):
        self.provider = provider or NominatimGeocoder()

    def resolve(self, query, country_codes='us'):
        """Return ``(lat, lon)`` or ``None``. Results and misses are cached."""
        key = ' '.join(query.split()).lower()
        row = GeocodeCache.objects.filter(query=key).first()
        if row is not None:
            if row.latitude is None:
                return None
            return row.latitude, row.longitude

        result = self.provider.geocode(query, country_codes=country_codes)
        if result is None:
            GeocodeCache.objects.update_or_create(
                query=key,
                defaults={'latitude': None, 'longitude': None, 'display_name': ''},
            )
            return None

        lat, lon, display = result
        GeocodeCache.objects.update_or_create(
            query=key,
            defaults={'latitude': lat, 'longitude': lon, 'display_name': display[:512]},
        )
        return lat, lon
