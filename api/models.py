from django.db import models
from rest_framework_api_key.models import APIKey


class FuelStation(models.Model):
    """One US truck stop from the price list, with the coordinates found for it offline."""

    class Precision(models.TextChoices):
        EXACT = 'exact', 'Exact (chain store locator)'
        EXIT = 'exit', 'Highway exit'
        TOWN = 'town', 'Town centre'
        NONE = 'none', 'Not placed'

    opis_id = models.PositiveIntegerField(unique=True, help_text='OPIS truck stop id from the price list.')
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=150, help_text="Highway-style, e.g. 'I-40, EXIT 76 & SR-72'.")
    city = models.CharField(max_length=60)
    state = models.CharField(max_length=2)
    price = models.DecimalField(max_digits=6, decimal_places=4, help_text='USD per gallon.')
    relevant = models.BooleanField(help_text='Address names an interstate or US highway.')
    town_radius_mi = models.FloatField(null=True, blank=True)
    lat = models.FloatField(null=True, blank=True)
    lon = models.FloatField(null=True, blank=True)
    precision = models.CharField(max_length=5, choices=Precision.choices)
    method = models.CharField(max_length=60, blank=True, help_text='How the location was found.')
    highway_keys = models.CharField(
        max_length=100, blank=True,
        help_text="Highways named in the address, parsed at load time, e.g. 'I:44,US:69'.",
    )
    # Stations that drop out of the price list are kept, inactive, so past trips still point at them.
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['opis_id']
        indexes = [
            models.Index(fields=['is_active', 'precision'], name='station_usable_idx'),
            models.Index(fields=['state', 'city'], name='station_place_idx'),
        ]

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state})'


class TripRecord(models.Model):
    """One planning request and its outcome, kept for history."""

    class Status(models.TextChoices):
        PLANNED = 'planned', 'Planned'
        FAILED = 'failed', 'Failed'

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    api_key = models.ForeignKey(
        APIKey, null=True, blank=True, on_delete=models.SET_NULL, related_name='trips')

    # What was asked
    start_input = models.CharField(max_length=255)
    finish_input = models.CharField(max_length=255)
    start_fuel_miles = models.FloatField()
    stop_cost_usd = models.DecimalField(max_digits=7, decimal_places=2)
    reserve_miles = models.FloatField(help_text='Reserve configured when the trip was planned.')

    # Where that resolved to (empty if a location was not found)
    start_label = models.CharField(max_length=255, blank=True)
    start_lat = models.FloatField(null=True, blank=True)
    start_lng = models.FloatField(null=True, blank=True)
    finish_label = models.CharField(max_length=255, blank=True)
    finish_lat = models.FloatField(null=True, blank=True)
    finish_lng = models.FloatField(null=True, blank=True)

    # Outcome
    status = models.CharField(max_length=7, choices=Status.choices, db_index=True)
    http_status = models.PositiveSmallIntegerField()
    error_code = models.CharField(max_length=40, blank=True)
    error_detail = models.TextField(blank=True)
    warnings = models.JSONField(default=list, blank=True)

    # The plan (empty when the trip failed)
    distance_miles = models.FloatField(null=True, blank=True)
    duration_hours = models.FloatField(null=True, blank=True)
    number_of_stops = models.PositiveSmallIntegerField(null=True, blank=True)
    gallons_purchased = models.FloatField(null=True, blank=True)
    total_fuel_cost_usd = models.DecimalField(max_digits=9, decimal_places=2, null=True, blank=True)
    fuel_left_on_arrival_miles = models.FloatField(null=True, blank=True)
    reserve_used_miles = models.FloatField(
        null=True, blank=True, help_text='Lower than reserve_miles if the reserve had to give way.')
    used_alternative_route = models.BooleanField(default=False)
    candidate_stations = models.PositiveIntegerField(null=True, blank=True)

    # How it went
    external_calls = models.PositiveSmallIntegerField(default=0)
    elapsed_ms = models.FloatField(default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.start_input} -> {self.finish_input} ({self.status})'


class TripStop(models.Model):
    """One fuel purchase on a planned trip. Price and amounts are copied, so history survives price updates."""

    class MatchedBy(models.TextChoices):
        STRICT = 'strict', 'Strict rules'
        RELAXED = 'relaxed', 'Relaxed rule inside a gap'

    trip = models.ForeignKey(TripRecord, on_delete=models.CASCADE, related_name='stops')
    station = models.ForeignKey(FuelStation, on_delete=models.PROTECT, related_name='trip_stops')
    sequence = models.PositiveSmallIntegerField()
    mile_marker = models.FloatField()
    off_route_miles = models.FloatField()
    price_per_gallon = models.DecimalField(max_digits=6, decimal_places=4)
    gallons = models.FloatField()
    cost_usd = models.DecimalField(max_digits=8, decimal_places=2)
    matched_by = models.CharField(max_length=7, choices=MatchedBy.choices, default=MatchedBy.STRICT)

    class Meta:
        ordering = ['trip', 'sequence']
        constraints = [
            models.UniqueConstraint(fields=['trip', 'sequence'], name='trip_stop_sequence_unique'),
        ]

    def __str__(self):
        return f'Stop {self.sequence} of trip {self.trip_id}'
