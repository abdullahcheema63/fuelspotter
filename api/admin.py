from django.contrib import admin

from .models import FuelStation, TripRecord, TripStop


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ('opis_id', 'name', 'city', 'state', 'price', 'precision', 'is_active')
    list_filter = ('precision', 'is_active', 'relevant', 'state')
    search_fields = ('name', 'city', 'address', '=opis_id')


class TripStopInline(admin.TabularInline):
    model = TripStop
    extra = 0
    can_delete = False
    raw_id_fields = ('station',)
    readonly_fields = [field.name for field in TripStop._meta.fields if field.name != 'id']


@admin.register(TripRecord)
class TripRecordAdmin(admin.ModelAdmin):
    """Read-only history of planned trips."""

    list_display = (
        'created_at', 'start_input', 'finish_input', 'status', 'distance_miles',
        'number_of_stops', 'total_fuel_cost_usd', 'external_calls', 'elapsed_ms',
    )
    list_filter = ('status', 'error_code', 'used_alternative_route')
    search_fields = ('start_input', 'finish_input', 'start_label', 'finish_label')
    date_hierarchy = 'created_at'
    inlines = [TripStopInline]
    exclude = ('map_data',)          # large; it is what the map link shows
    readonly_fields = [field.name for field in TripRecord._meta.fields if field.name != 'map_data']

    def has_add_permission(self, request):
        return False
