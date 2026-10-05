"""Tests for the fuel-route planner.

The fuel algorithm is covered by a feasibility simulator rather than by
hard-coded expectations: for every plan it replays the journey and asserts the
vehicle never runs dry, never overfills the tank, and always arrives. That
catches the class of bug where a plan looks cheap but is not drivable.
"""

from decimal import Decimal

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from routing.models import FuelStation
from routing.services.fuel import (
    MIN_PURCHASE_GALLONS,
    ORIGIN_SEARCH_MILES,
    RouteCandidate,
    UnreachableRouteError,
    _dedupe_by_location,
    plan_fuel_stops,
)
from routing.services.gazetteer import lookup, normalise
from routing.services.geo import cumulative_distances, haversine_miles
from routing.services.planner import RoutePlanner
from routing.services.providers import Route

MAX_RANGE = 500.0
MPG = 10.0
TANK = MAX_RANGE / MPG


def make_candidates(pairs):
    """Build candidates from ``[(mile, price), ...]`` without touching the DB."""
    candidates = []
    for index, (mile, price) in enumerate(pairs, start=1):
        station = FuelStation(
            id=index,
            name=f'STATION {index}',
            address='',
            city='Testville',
            state='XX',
            retail_price=Decimal(str(price)),
            latitude=40.0 + index * 0.01,
            longitude=-80.0,
        )
        candidates.append(RouteCandidate(station, float(mile), 1.0))
    return candidates


class FuelPlanFeasibilityTests(SimpleTestCase):
    """Every plan the planner returns must be physically drivable."""

    def assert_feasible(self, total_distance, stops):
        self.assertTrue(stops, 'a trip needing fuel must produce at least one stop')

        position = stops[0]['distance_from_start_miles']
        fuel = 0.0
        for index, stop in enumerate(stops):
            if index:
                fuel -= (stop['distance_from_start_miles'] - position) / MPG
                self.assertGreaterEqual(
                    fuel,
                    -1e-6,
                    f"ran dry before mile {stop['distance_from_start_miles']}",
                )
                position = stop['distance_from_start_miles']
            fuel += stop['gallons_purchased']
            self.assertLessEqual(
                fuel, TANK + 1e-4, f'tank overfilled at mile {position}'
            )

        self.assertGreaterEqual(
            fuel + 1e-6,
            (total_distance - position) / MPG,
            'not enough fuel to reach the destination',
        )

    def test_single_tank_trip_with_fuel_aboard_needs_no_stop(self):
        stops, cost, gallons = plan_fuel_stops(
            400.0, make_candidates([(5, 3.0)]), start_fuel_gallons=TANK
        )
        self.assertEqual(stops, [])
        self.assertEqual(cost, 0.0)
        self.assertEqual(gallons, 0.0)

    def test_buys_only_enough_to_reach_a_cheaper_station(self):
        stops, _, _ = plan_fuel_stops(750.0, make_candidates([(5, 3.50), (450, 3.00)]))
        self.assert_feasible(750.0, stops)
        # The dear station is used as a bridge, the cheap one for the long haul.
        self.assertLess(stops[0]['gallons_purchased'], stops[1]['gallons_purchased'] + TANK)
        self.assertEqual(stops[1]['price_per_gallon'], 3.0)

    def test_fills_up_when_nothing_cheaper_is_in_range(self):
        stops, _, _ = plan_fuel_stops(750.0, make_candidates([(5, 3.00), (450, 4.00)]))
        self.assert_feasible(750.0, stops)
        self.assertEqual(stops[0]['gallons_purchased'], TANK)

    def test_never_buys_more_than_the_trip_burns(self):
        total = 300.0
        _, _, gallons = plan_fuel_stops(total, make_candidates([(0, 3.0)]))
        self.assertAlmostEqual(gallons, total / MPG, places=6)

    def test_long_route_with_descending_prices_is_feasible(self):
        pairs = [(5, 3.9), (300, 3.1), (700, 3.0), (1100, 3.4), (1400, 2.9)]
        stops, _, gallons = plan_fuel_stops(1500.0, make_candidates(pairs))
        self.assert_feasible(1500.0, stops)
        self.assertAlmostEqual(gallons, (1500.0 - stops[0]['distance_from_start_miles']) / MPG, places=4)

    def test_cheap_station_at_the_start_is_used_to_the_full(self):
        pairs = [(5, 2.50), (400, 3.9), (800, 3.9), (1200, 3.9)]
        stops, _, _ = plan_fuel_stops(1400.0, make_candidates(pairs))
        self.assert_feasible(1400.0, stops)
        self.assertEqual(stops[0]['gallons_purchased'], TANK)

    def test_no_micro_stops_on_a_steadily_cheapening_route(self):
        # Without a floor on purchase size this produces a stop every 10 miles.
        pairs = [(mile, 4.3 - mile * 0.002) for mile in range(0, 300, 10)]
        stops, _, _ = plan_fuel_stops(300.0, make_candidates(pairs))
        self.assert_feasible(300.0, stops)
        self.assertLess(len(stops), 10)
        for stop in stops[:-1]:
            self.assertGreaterEqual(stop['gallons_purchased'], MIN_PURCHASE_GALLONS - 1e-6)

    def test_gap_wider_than_the_vehicle_range_is_rejected(self):
        with self.assertRaises(UnreachableRouteError):
            plan_fuel_stops(1200.0, make_candidates([(5, 3.0), (900, 3.0)]))

    def test_route_with_no_stations_is_rejected(self):
        with self.assertRaises(UnreachableRouteError):
            plan_fuel_stops(600.0, [])

    def test_station_too_far_from_the_start_is_rejected(self):
        # Real case: Los Angeles to Seattle. The price list holds only ten
        # Californian stations, all near the Mexican border, so the first one
        # on that route is 878 miles in. Returning a plan whose opening leg is
        # undrivable would be worse than refusing.
        far = ORIGIN_SEARCH_MILES + 50
        with self.assertRaises(UnreachableRouteError) as caught:
            plan_fuel_stops(1200.0, make_candidates([(far, 3.0), (far + 300, 3.0)]))
        self.assertIn('miles of the start', str(caught.exception))

    def test_station_just_inside_the_origin_window_is_accepted(self):
        stops, _, _ = plan_fuel_stops(
            900.0,
            make_candidates([(ORIGIN_SEARCH_MILES - 10, 3.0), (500, 3.0), (880, 3.0)]),
        )
        self.assert_feasible(900.0, stops)

    def test_duplicate_locations_collapse_to_the_cheapest(self):
        candidates = make_candidates([(100, 3.5), (100, 3.1), (100, 3.9)])
        for candidate in candidates:
            candidate.station.latitude = 40.1
            candidate.station.longitude = -80.0
        deduped = _dedupe_by_location(candidates)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].price, 3.1)


class GeometryTests(SimpleTestCase):
    def test_haversine_matches_known_distance(self):
        # New York to Chicago, great-circle, is about 711 miles.
        miles = haversine_miles(40.7128, -74.0060, 41.8781, -87.6298)
        self.assertAlmostEqual(miles, 711.0, delta=2.0)

    def test_cumulative_distances_start_at_zero_and_increase(self):
        points = [(40.0, -80.0), (40.5, -80.0), (41.0, -80.0)]
        cumulative = cumulative_distances(points)
        self.assertEqual(cumulative[0], 0.0)
        self.assertLess(cumulative[0], cumulative[1])
        self.assertLess(cumulative[1], cumulative[2])


class GazetteerTests(SimpleTestCase):
    def test_punctuation_is_ignored_when_matching(self):
        # The price CSV writes "Oneill"; the Census writes "O'Neill". Before
        # this folding, the fallback geocoder answered with a town in
        # Connecticut, 1,300 miles from the real Nebraska one.
        self.assertEqual(normalise('Oneill'), normalise("O'Neill"))
        self.assertEqual(lookup('Oneill', 'NE'), lookup("O'Neill", 'NE'))

    def test_known_city_resolves_into_the_right_state(self):
        point = lookup('Oneill', 'NE')
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point[0], 42.46, delta=0.5)
        self.assertAlmostEqual(point[1], -98.65, delta=0.5)

    def test_unknown_city_returns_none(self):
        self.assertIsNone(lookup('Nowhere At All', 'ZZ'))


class StubRouter:
    """Returns a fixed straight-line route and counts how often it is called."""

    def __init__(self):
        self.calls = 0

    def route(self, start, finish):
        self.calls += 1
        points = [
            (40.0 + step * 0.1, -80.0) for step in range(101)
        ]  # ~690 miles due north
        return Route(
            points=points,
            distance_miles=cumulative_distances(points)[-1],
            duration_seconds=36000.0,
            start=points[0],
            finish=points[-1],
        )


class StubGeocoder:
    def __init__(self):
        self.calls = 0

    def resolve(self, query, country_codes='us'):
        self.calls += 1
        return (40.0, -80.0) if 'start' in query.lower() else (50.0, -80.0)


class PlannerIntegrationTests(TestCase):
    """Exercises the planner against the database, with providers stubbed out."""

    def setUp(self):
        self.router = StubRouter()
        self.planner = RoutePlanner(router=self.router, geocoder=StubGeocoder())
        FuelStation.objects.bulk_create(
            FuelStation(
                opis_id=str(index),
                name=f'STOP {index}',
                address='I-00',
                city=f'City {index}',
                state='PA',
                retail_price=Decimal('3.50') - Decimal('0.05') * index,
                latitude=40.0 + index * 0.6,
                longitude=-80.0,
            )
            for index in range(12)
        )

    def test_plan_makes_exactly_one_routing_call(self):
        self.planner.plan('start city', 'finish city', use_cache=False)
        self.assertEqual(self.router.calls, 1)

    def test_plan_reports_gallons_consistent_with_distance(self):
        result = self.planner.plan('start city', 'finish city', use_cache=False)
        stops = result['fuel_stops']
        self.assertTrue(stops)
        driven = result['route']['distance_miles'] - stops[0]['distance_from_start_miles']
        self.assertAlmostEqual(result['summary']['total_gallons'], driven / MPG, delta=0.05)

    def test_total_cost_is_the_sum_of_the_stops(self):
        result = self.planner.plan('start city', 'finish city', use_cache=False)
        expected = sum(stop['cost_usd'] for stop in result['fuel_stops'])
        self.assertAlmostEqual(result['summary']['total_fuel_cost_usd'], expected, delta=0.02)

    def test_second_identical_request_is_served_from_cache(self):
        self.planner.plan('start city', 'finish city')
        self.planner.plan('start city', 'finish city')
        self.assertEqual(self.router.calls, 1)


class ApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_reports_station_counts(self):
        response = self.client.get('/api/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ok')
        self.assertIn('stations_geocoded', response.data)

    def test_missing_finish_is_rejected(self):
        response = self.client.post('/api/route/', {'start': 'Chicago, IL'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('finish', response.data)

    def test_blank_start_is_rejected(self):
        response = self.client.post(
            '/api/route/', {'start': '   ', 'finish': 'Chicago, IL'}, format='json'
        )
        self.assertEqual(response.status_code, 400)
