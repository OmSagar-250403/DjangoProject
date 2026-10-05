from django.contrib import admin

from .models import FuelStation, GeocodeCache


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ('name', 'city', 'state', 'retail_price', 'latitude', 'longitude')
    list_filter = ('state',)
    search_fields = ('name', 'city', 'address', 'opis_id')


@admin.register(GeocodeCache)
class GeocodeCacheAdmin(admin.ModelAdmin):
    list_display = ('query', 'latitude', 'longitude', 'created_at')
    search_fields = ('query', 'display_name')
