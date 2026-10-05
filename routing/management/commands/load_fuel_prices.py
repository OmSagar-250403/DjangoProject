"""Import the OPIS fuel-price CSV into Postgres, de-duplicated."""

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import FuelStation

DEFAULT_CSV = 'fuel-prices-for-be-assessment.csv'


class Command(BaseCommand):
    help = 'Load fuel station prices from the assessment CSV.'

    def add_arguments(self, parser):
        parser.add_argument('--csv', default=DEFAULT_CSV, help='Path to the CSV file.')
        parser.add_argument(
            '--truncate',
            action='store_true',
            help='Delete existing stations first (drops cached coordinates).',
        )

    def handle(self, *args, **options):
        path = Path(options['csv'])
        if not path.exists():
            raise CommandError(f'CSV not found: {path}')

        if options['truncate']:
            deleted, _ = FuelStation.objects.all().delete()
            self.stdout.write(f'Deleted {deleted} existing rows.')

        # Keep any coordinates we already resolved, so a re-import of refreshed
        # prices does not force a full re-geocode.
        known_coords = {
            (s.city.strip().upper(), s.state.strip().upper()): (s.latitude, s.longitude)
            for s in FuelStation.objects.filter(latitude__isnull=False).only(
                'city', 'state', 'latitude', 'longitude'
            )
        }

        seen = set()
        rows = []
        skipped = 0

        with path.open(newline='', encoding='utf-8-sig') as handle:
            for line_no, raw in enumerate(csv.DictReader(handle), start=2):
                record = {k.strip(): (v or '').strip() for k, v in raw.items() if k}
                try:
                    price = Decimal(record['Retail Price'])
                except (KeyError, InvalidOperation):
                    skipped += 1
                    continue

                city = record.get('City', '')
                state = record.get('State', '')[:2].upper()
                if not city or not state:
                    skipped += 1
                    continue

                key = (
                    record.get('OPIS Truckstop ID', ''),
                    record.get('Truckstop Name', ''),
                    record.get('Address', ''),
                    city.upper(),
                    state,
                )
                if key in seen:
                    continue
                seen.add(key)

                lat, lon = known_coords.get((city.upper(), state), (None, None))
                rows.append(
                    FuelStation(
                        opis_id=record.get('OPIS Truckstop ID', '')[:32],
                        name=record.get('Truckstop Name', '')[:255],
                        address=record.get('Address', '')[:255],
                        city=city[:128],
                        state=state,
                        rack_id=record.get('Rack ID', '')[:32],
                        retail_price=price,
                        latitude=lat,
                        longitude=lon,
                    )
                )

        with transaction.atomic():
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(rows, batch_size=1000)

        self.stdout.write(
            self.style.SUCCESS(
                f'Loaded {len(rows)} unique stations '
                f'({skipped} rows skipped, {len(rows)} after de-duplication).'
            )
        )
        pre_geocoded = sum(1 for r in rows if r.latitude is not None)
        if pre_geocoded:
            self.stdout.write(f'Reused coordinates for {pre_geocoded} stations.')
        self.stdout.write('Next: python manage.py geocode_stations')
