import csv
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from api.models import FuelStation
from api.services.highways import decode, highways_in

HEADER = ['id', 'name', 'address', 'city', 'state', 'price', 'relevant',
          'town_radius_mi', 'lat', 'lon', 'precision', 'method']
ROWS = [
    ['7', 'WOODSHED OF BIG CABIN', 'I-44, EXIT 283 & US-69', 'Big Cabin', 'OK', '3.0073', 'True',
     '0.85', '36.5611723', '-95.2178811', 'exit', 'exit I 44 #283'],
    ['9', 'KWIK TRIP #796', 'I-94, EXIT 143 & US-12 & SR-21', 'Tomah', 'WI', '3.2873', 'True',
     '1.58', '44.01746', '-90.50153', 'exact', 'ATP store#'],
    ['35', 'ACI TRUCK STOP', 'US-46', 'Columbia', 'NJ', '3.079', 'True',
     '', '40.925868', '-75.094062', 'town', 'town center (no exit in address)'],
    ['99', 'NOWHERE FUEL', 'MAIN ST', 'Nowhere', 'TX', '3.5', 'False', '', '', '', 'none', ''],
]


class LoadStationsTests(TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'stations.csv'

    def write(self, rows):
        with open(self.path, 'w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(HEADER)
            writer.writerows(rows)

    def load(self):
        out = StringIO()
        call_command('load_stations', path=str(self.path), stdout=out)
        return out.getvalue()

    def test_loads_every_row(self):
        self.write(ROWS)
        output = self.load()
        self.assertEqual(FuelStation.objects.count(), 4)
        self.assertIn('4 stations loaded: 4 new, 0 updated, 0 marked inactive', output)
        station = FuelStation.objects.get(opis_id=7)
        self.assertEqual(station.price, Decimal('3.0073'))
        self.assertEqual(station.precision, 'exit')
        self.assertEqual(decode(station.highway_keys), {('I', '44'), ('US', '69')})

    def test_unplaced_station_loads_without_coordinates(self):
        self.write(ROWS)
        self.load()
        station = FuelStation.objects.get(opis_id=99)
        self.assertEqual(station.precision, 'none')
        self.assertIsNone(station.lat)
        self.assertIsNone(station.lon)

    def test_running_twice_changes_nothing(self):
        self.write(ROWS)
        self.load()
        ids = list(FuelStation.objects.values_list('id', 'opis_id'))
        output = self.load()
        self.assertEqual(list(FuelStation.objects.values_list('id', 'opis_id')), ids)
        self.assertIn('0 new, 4 updated, 0 marked inactive', output)

    def test_changed_price_is_updated(self):
        self.write(ROWS)
        self.load()
        changed = [row[:] for row in ROWS]
        changed[0][5] = '2.9999'
        self.write(changed)
        self.load()
        self.assertEqual(FuelStation.objects.get(opis_id=7).price, Decimal('2.9999'))
        self.assertEqual(FuelStation.objects.count(), 4)

    def test_station_missing_from_file_is_kept_but_inactive(self):
        self.write(ROWS)
        self.load()
        self.write(ROWS[1:])
        output = self.load()
        self.assertIn('1 marked inactive', output)
        self.assertFalse(FuelStation.objects.get(opis_id=7).is_active)
        self.assertEqual(FuelStation.objects.filter(is_active=True).count(), 3)

    def test_station_returning_to_the_file_is_active_again(self):
        self.write(ROWS)
        self.load()
        self.write(ROWS[1:])
        self.load()
        self.write(ROWS)
        self.load()
        self.assertTrue(FuelStation.objects.get(opis_id=7).is_active)

    def test_bad_row_loads_nothing(self):
        bad = [row[:] for row in ROWS]
        bad[1][8] = ''                      # an 'exact' station without a latitude
        self.write(bad)
        with self.assertRaises(CommandError) as raised:
            self.load()
        self.assertIn('line 3', str(raised.exception))
        self.assertIn('nothing was loaded', str(raised.exception))
        self.assertEqual(FuelStation.objects.count(), 0)

    def test_duplicate_id_is_rejected(self):
        self.write(ROWS + [ROWS[0]])
        with self.assertRaises(CommandError) as raised:
            self.load()
        self.assertIn('duplicate id 7', str(raised.exception))

    def test_missing_column_is_rejected(self):
        with open(self.path, 'w', newline='') as file:
            csv.writer(file).writerows([HEADER[:-1], ROWS[0][:-1]])
        with self.assertRaises(CommandError) as raised:
            self.load()
        self.assertIn('missing columns: method', str(raised.exception))


class HighwayNameTests(TestCase):
    def test_station_address(self):
        self.assertEqual(highways_in('I-40, EXIT 76 & SR-72', from_route=False),
                         {('I', '40'), ('S', '72')})

    def test_route_ref(self):
        self.assertEqual(highways_in('I 40; US 287', from_route=True), {('I', '40'), ('US', '287')})
        self.assertEqual(highways_in('TX 71', from_route=True), {('S', '71')})

    def test_no_highway(self):
        self.assertEqual(highways_in('MAIN ST', from_route=False), frozenset())
        self.assertEqual(highways_in(None, from_route=True), frozenset())
