import csv
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from api.models import FuelStation
from api.services.highways import encode, highways_in

COLUMNS = {'id', 'name', 'address', 'city', 'state', 'price', 'relevant',
           'town_radius_mi', 'lat', 'lon', 'precision', 'method'}
UPDATED_FIELDS = ['name', 'address', 'city', 'state', 'price', 'relevant', 'town_radius_mi',
                  'lat', 'lon', 'precision', 'method', 'highway_keys', 'is_active', 'updated_at']
MAX_ERRORS_SHOWN = 10


class Command(BaseCommand):
    help = ('Load the geocoded fuel stations from CSV. Safe to run again: existing stations are '
            'updated, new ones added, and stations no longer in the file are marked inactive.')

    def add_arguments(self, parser):
        parser.add_argument('--path', default=str(settings.STATIONS_CSV),
                            help='CSV file to load (default: data/stations_geocoded.csv).')

    def handle(self, *args, **options):
        stations = self.read(options['path'])
        file_ids = {station.opis_id for station in stations}
        known_ids = set(FuelStation.objects.values_list('opis_id', flat=True))
        active_ids = set(FuelStation.objects.filter(is_active=True).values_list('opis_id', flat=True))
        gone_ids = sorted(active_ids - file_ids)

        with transaction.atomic():
            FuelStation.objects.bulk_create(
                stations, batch_size=500,
                update_conflicts=True, unique_fields=['opis_id'], update_fields=UPDATED_FIELDS)
            for first in range(0, len(gone_ids), 500):
                FuelStation.objects.filter(opis_id__in=gone_ids[first:first + 500]).update(
                    is_active=False, updated_at=timezone.now())

        by_precision = Counter(station.precision for station in stations)
        self.stdout.write(self.style.SUCCESS(
            f'{len(stations)} stations loaded: {len(file_ids - known_ids)} new, '
            f'{len(file_ids & known_ids)} updated, {len(gone_ids)} marked inactive.'))
        self.stdout.write('By location precision: ' + ', '.join(
            f'{precision} {by_precision[precision]}' for precision in FuelStation.Precision.values))

    def read(self, path):
        """Parse and check every row before anything is written."""
        try:
            with open(path, newline='') as file:
                reader = csv.DictReader(file)
                missing = COLUMNS - set(reader.fieldnames or [])
                if missing:
                    raise CommandError(f'{path} is missing columns: {", ".join(sorted(missing))}.')
                rows = list(reader)
        except OSError as error:
            raise CommandError(f'Cannot read {path}: {error}')

        stations, errors, seen = [], [], set()
        for line, row in enumerate(rows, start=2):
            try:
                station = self.station_from(row)
                if station.opis_id in seen:
                    raise ValueError(f'duplicate id {station.opis_id}')
                seen.add(station.opis_id)
                stations.append(station)
            except (ValueError, InvalidOperation) as error:
                errors.append(f'line {line}: {error}')
        if errors:
            shown = '\n'.join(errors[:MAX_ERRORS_SHOWN])
            more = f'\n... and {len(errors) - MAX_ERRORS_SHOWN} more' if len(errors) > MAX_ERRORS_SHOWN else ''
            raise CommandError(f'{len(errors)} bad row(s) in {path}; nothing was loaded.\n{shown}{more}')
        if not stations:
            raise CommandError(f'{path} has no stations; nothing was loaded.')
        return stations

    def station_from(self, row):
        precision = row['precision']
        if precision not in FuelStation.Precision.values:
            raise ValueError(f'unknown precision {precision!r}')
        price = Decimal(row['price'])
        if price <= 0:
            raise ValueError(f'price must be positive, got {price}')
        if row['relevant'] not in ('True', 'False'):
            raise ValueError(f"relevant must be True or False, got {row['relevant']!r}")

        placed = precision != FuelStation.Precision.NONE
        lat = float(row['lat']) if row['lat'] else None
        lon = float(row['lon']) if row['lon'] else None
        if placed and (lat is None or lon is None):
            raise ValueError(f'precision {precision} needs lat and lon')
        if placed and not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f'coordinates out of range: {lat}, {lon}')

        return FuelStation(
            opis_id=int(row['id']),
            name=row['name'].strip(),
            address=row['address'].strip(),
            city=row['city'].strip(),
            state=row['state'].strip().upper(),
            price=price,
            relevant=row['relevant'] == 'True',
            town_radius_mi=float(row['town_radius_mi']) if row['town_radius_mi'] else None,
            lat=lat,
            lon=lon,
            precision=precision,
            method=row['method'].strip(),
            highway_keys=encode(highways_in(row['address'], from_route=False)),
            is_active=True,
            updated_at=timezone.now(),
        )
