from django.db import models


class FuelStation(models.Model):
    """A truck stop from the OPIS price list, with coordinates resolved offline.

    ``latitude``/``longitude`` are nullable because the source CSV ships only
    postal addresses; they are filled in by the ``geocode_stations`` command
    before the API is used, so no request-time geocoding is ever needed.
    """

    opis_id = models.CharField(max_length=32, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.CharField(max_length=32, blank=True)
    retail_price = models.DecimalField(max_digits=7, decimal_places=4)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['opis_id', 'name', 'address', 'city', 'state'],
                name='uniq_station_identity',
            ),
        ]
        indexes = [
            # Serves the bounding-box prefilter that narrows ~8k stations down
            # to the few hundred near a route before any distance maths runs.
            models.Index(fields=['latitude', 'longitude'], name='idx_station_lat_lon'),
            models.Index(fields=['retail_price'], name='idx_station_price'),
        ]
        ordering = ['state', 'city', 'name']

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state}) ${self.retail_price}'

    @property
    def is_geocoded(self):
        return self.latitude is not None and self.longitude is not None


class GeocodeCache(models.Model):
    """Address -> coordinate memo, so re-running the geocoder costs nothing.

    ``latitude``/``longitude`` stay null for queries the provider could not
    resolve; caching the failure stops us retrying it on every run.
    """

    query = models.CharField(max_length=512, unique=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    display_name = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.query
