"""Resolve station coordinates offline, so the API never geocodes at request time.

Stations are geocoded at city+state granularity: the CSV has ~7,000 rows but
only ~3,900 distinct city/state pairs, and at a 500-mile planning range city
precision sits well inside the corridor tolerance.

Pass one resolves ~94% of pairs instantly from the bundled Census Gazetteer.
Pass two sends the remainder to Nominatim, which is rate-limited to one request
per second, and caches every answer so a re-run costs nothing.
"""

import time

from django.core.management.base import BaseCommand
from django.db import transaction

from routing.models import FuelStation
from routing.services.gazetteer import lookup as gazetteer_lookup
from routing.services.geocoding import GeocodingService
from routing.services.providers import ProviderError

# States/territories the Census Gazetteer covers; anything else (the CSV
# includes a handful of Canadian truck stops) goes straight to the web
# geocoder, and is excluded from US-only routing anyway.
US_STATES = frozenset(
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS '
    'MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV '
    'WI WY DC PR'.split()
)


class Command(BaseCommand):
    help = 'Geocode fuel stations (city + state) and store their coordinates.'

    # US state bounding boxes are not worth shipping, but a geocoded point that
    # lands more than this far from every other station in the same state is
    # almost certainly the wrong "Springfield". Such answers are discarded.
    MAX_PLAUSIBLE_STATE_OFFSET_MILES = 400

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=0, help='Only process N city/state pairs.'
        )
        parser.add_argument(
            '--offline',
            action='store_true',
            help='Use only the bundled Gazetteer; skip the network fallback.',
        )
        parser.add_argument(
            '--revalidate',
            action='store_true',
            help='Re-check stored coordinates against the Gazetteer and fix outliers.',
        )
        parser.add_argument(
            '--skip-non-us',
            action='store_true',
            help='Ignore non-US rows instead of geocoding them over the network.',
        )

    def handle(self, *args, **options):
        if options['revalidate']:
            self._revalidate()

        pairs = list(
            FuelStation.objects.filter(latitude__isnull=True)
            .values_list('city', 'state')
            .distinct()
            .order_by('state', 'city')
        )
        if options['limit']:
            pairs = pairs[: options['limit']]

        if not pairs:
            self.stdout.write(self.style.SUCCESS('Every station already has coordinates.'))
            return

        started = time.monotonic()
        self.stdout.write(f'Resolving {len(pairs)} distinct city/state pairs.')

        # --- Pass 1: bundled Census Gazetteer (offline, instant) -------------
        offline_hits = 0
        remaining = []
        for city, state in pairs:
            point = gazetteer_lookup(city, state)
            if point is None:
                remaining.append((city, state))
                continue
            self._apply(city, state, point)
            offline_hits += 1

        self.stdout.write(
            f'  Gazetteer resolved {offline_hits}/{len(pairs)} pairs '
            f'in {time.monotonic() - started:.1f}s.'
        )

        if options['skip_non_us']:
            skipped = [p for p in remaining if p[1] not in US_STATES]
            remaining = [p for p in remaining if p[1] in US_STATES]
            if skipped:
                self.stdout.write(f'  Skipping {len(skipped)} non-US pairs.')

        if not remaining:
            self._report(started)
            return

        if options['offline']:
            self.stdout.write(
                self.style.WARNING(
                    f'  {len(remaining)} pairs unresolved; re-run without --offline.'
                )
            )
            self._report(started)
            return

        # --- Pass 2: Nominatim for the leftovers (1 req/sec) ----------------
        self.stdout.write(
            f'  Geocoding {len(remaining)} remaining pairs over the network '
            f'(~{len(remaining) * 1.1 / 60:.0f} min)...'
        )
        service = GeocodingService()
        resolved = failed = 0

        for index, (city, state) in enumerate(remaining, start=1):
            country = 'us' if state in US_STATES else None
            query = f'{city}, {state}, USA' if country else f'{city}, {state}, Canada'
            try:
                point = service.resolve(query, country_codes=country or 'ca')
            except ProviderError as exc:
                self.stderr.write(f'    {query}: {exc}')
                failed += 1
                continue

            if point is None:
                failed += 1
                continue

            self._apply(city, state, point)
            resolved += 1

            if index % 25 == 0 or index == len(remaining):
                self.stdout.write(
                    f'    {index}/{len(remaining)} '
                    f'({resolved} resolved, {failed} unresolved)'
                )

        self._report(started)

    def _revalidate(self):
        """Clear coordinates that disagree with the Gazetteer, then re-resolve.

        Nominatim will happily answer "Oneill, NE" with a town in Connecticut;
        the Gazetteer is authoritative for US places, so where the two differ
        materially we trust the Gazetteer.
        """
        from routing.services.geo import haversine_miles

        fixed = 0
        queryset = FuelStation.objects.filter(
            latitude__isnull=False, state__in=US_STATES
        ).only('city', 'state', 'latitude', 'longitude')

        for station in queryset.iterator(chunk_size=2000):
            reference = gazetteer_lookup(station.city, station.state)
            if reference is None:
                continue
            offset = haversine_miles(
                station.latitude, station.longitude, reference[0], reference[1]
            )
            if offset <= self.MAX_PLAUSIBLE_STATE_OFFSET_MILES:
                continue
            FuelStation.objects.filter(city=station.city, state=station.state).update(
                latitude=reference[0], longitude=reference[1]
            )
            fixed += 1
            self.stdout.write(
                f'  corrected {station.city}, {station.state} '
                f'(was {offset:.0f} miles away)'
            )

        self.stdout.write(
            self.style.SUCCESS(f'Revalidation corrected {fixed} city/state pairs.')
            if fixed
            else 'Revalidation found no outliers.'
        )

    @staticmethod
    def _apply(city, state, point):
        lat, lon = point
        with transaction.atomic():
            FuelStation.objects.filter(
                city=city, state=state, latitude__isnull=True
            ).update(latitude=lat, longitude=lon)

    def _report(self, started):
        total = FuelStation.objects.count()
        geocoded = FuelStation.objects.filter(latitude__isnull=False).count()
        self.stdout.write(
            self.style.SUCCESS(
                f'Done in {time.monotonic() - started:.1f}s. '
                f'{geocoded}/{total} stations have coordinates '
                f'({100 * geocoded / total:.1f}%).'
            )
        )
