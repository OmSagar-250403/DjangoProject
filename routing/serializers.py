from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    """Validates the inbound payload for POST /api/route/."""

    start = serializers.CharField(max_length=255, trim_whitespace=True)
    finish = serializers.CharField(max_length=255, trim_whitespace=True)

    def validate_start(self, value):
        return self._non_blank(value, 'start')

    def validate_finish(self, value):
        return self._non_blank(value, 'finish')

    @staticmethod
    def _non_blank(value, field):
        if not value.strip():
            raise serializers.ValidationError(f'{field} must not be blank.')
        return value.strip()


class FuelStopSerializer(serializers.Serializer):
    """One refuelling stop in the returned itinerary."""

    station_id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField()
    city = serializers.CharField()
    state = serializers.CharField()
    latitude = serializers.FloatField()
    longitude = serializers.FloatField()
    price_per_gallon = serializers.FloatField()
    distance_from_start_miles = serializers.FloatField()
    detour_from_route_miles = serializers.FloatField()
    gallons_purchased = serializers.FloatField()
    cost_usd = serializers.FloatField()
