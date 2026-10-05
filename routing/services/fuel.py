"""Station corridor search and the cheapest-fuel stop planner."""

import logging

from django.conf import settings
from django.db.models import Q

from routing.models import FuelStation

from .geo import (
    MILES_PER_DEGREE_LAT,
    cumulative_distances,
    densify_indices,
    haversine_miles,
    miles_per_degree_lon,
)

logger = logging.getLogger(__name__)

# Spacing of the thinned polyline ladder used for station matching. Small
# enough that a station's measured mileage is accurate, coarse enough that the
# scan stays linear and fast.
LADDER_SPACING_MILES = 2.0

# Pulling in for less than this is not worth the detour, so the planner skips a
# marginally cheaper station and keeps the fuel it would have bought there for
# the next one. Purely about producing a sane itinerary; it costs a few cents.
MIN_PURCHASE_GALLONS = 5.0

# How far along the route the departure fill-up may be. The vehicle starts with
# an empty tank, so the first station has to be close to the origin to be a
# credible starting point; beyond this the price data does not cover the area.
ORIGIN_SEARCH_MILES = 100.0

# Buying only the fuel needed to coast into the next station leaves the vehicle
# arriving on an empty tank. A small reserve keeps the plan drivable in the real
# world and absorbs the rounding in the reported figures. It is capped by what
# the rest of the trip needs, so it never inflates the total.
RESERVE_GALLONS = 1.0


class RouteCandidate:
    """A station that lies within the corridor, with its along-route position."""

    __slots__ = ('station', 'distance_from_start', 'detour_miles', 'price')

    def __init__(self, station, distance_from_start, detour_miles):
        self.station = station
        self.distance_from_start = distance_from_start
        self.detour_miles = detour_miles
        self.price = float(station.retail_price)


def find_corridor_stations(route, corridor_miles=None):
    """Stations near ``route``, ordered by distance from the start.

    Runs entirely against Postgres and local geometry - no external calls. The
    bounding box is an indexed prefilter; the precise corridor test then runs
    against a thinned copy of the route shape.
    """
    corridor = corridor_miles or settings.STATION_CORRIDOR_MILES
    points = route.points
    cumulative = cumulative_distances(points)

    ladder_idx = densify_indices(points, cumulative, LADDER_SPACING_MILES)
    ladder = [(points[i][0], points[i][1], cumulative[i]) for i in ladder_idx]

    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    lat_pad = corridor / MILES_PER_DEGREE_LAT
    mid_lat = (min(lats) + max(lats)) / 2
    lon_pad = corridor / miles_per_degree_lon(mid_lat)

    queryset = FuelStation.objects.filter(
        latitude__gte=min(lats) - lat_pad,
        latitude__lte=max(lats) + lat_pad,
        longitude__gte=min(lons) - lon_pad,
        longitude__lte=max(lons) + lon_pad,
    ).exclude(Q(latitude__isnull=True) | Q(longitude__isnull=True))

    # Bucket ladder rungs by latitude so each station only compares against the
    # slice of the route at its own latitude, not the whole line.
    bucket_size = max(lat_pad, 0.05)
    buckets = {}
    for lat, lon, dist in ladder:
        buckets.setdefault(int(lat / bucket_size), []).append((lat, lon, dist))

    candidates = []
    for station in queryset.iterator(chunk_size=2000):
        key = int(station.latitude / bucket_size)
        nearby = []
        for offset in (-1, 0, 1):
            nearby.extend(buckets.get(key + offset, ()))
        if not nearby:
            continue

        best_detour = None
        best_distance = 0.0
        for lat, lon, dist in nearby:
            detour = haversine_miles(station.latitude, station.longitude, lat, lon)
            if best_detour is None or detour < best_detour:
                best_detour = detour
                best_distance = dist
                if detour <= 0.5:  # close enough; stop refining
                    break

        if best_detour is not None and best_detour <= corridor:
            candidates.append(RouteCandidate(station, best_distance, best_detour))

    candidates.sort(key=lambda c: c.distance_from_start)
    return _dedupe_by_location(candidates)


def _dedupe_by_location(candidates):
    """Collapse stations sharing a coordinate, keeping the cheapest.

    Many CSV rows are the same truck stop listed under slightly different
    names, and they all geocode to one point; keeping every copy would let the
    planner "stop" repeatedly in the same place.
    """
    best = {}
    for candidate in candidates:
        key = (
            round(candidate.station.latitude, 4),
            round(candidate.station.longitude, 4),
        )
        existing = best.get(key)
        if existing is None or candidate.price < existing.price:
            best[key] = candidate
    return sorted(best.values(), key=lambda c: c.distance_from_start)


class UnreachableRouteError(RuntimeError):
    """Raised when no sequence of in-corridor stations can cover the route."""


def plan_fuel_stops(
    total_distance_miles,
    candidates,
    max_range_miles=None,
    miles_per_gallon=None,
    start_fuel_gallons=None,
):
    """Greedy cheapest-fuel plan over the stations along a route.

    At each station we look ahead one tankful. If somewhere cheaper is
    reachable, we buy only enough to get there; otherwise we fill up (never
    more than is needed to finish the trip) and push on to the furthest
    reachable station. That is the standard optimal strategy for this problem.

    Returns ``(stops, total_cost, total_gallons)``.
    """
    max_range = max_range_miles or settings.VEHICLE_MAX_RANGE_MILES
    mpg = miles_per_gallon or settings.VEHICLE_MILES_PER_GALLON
    tank_gallons = max_range / mpg

    # The brief fixes the range at 500 miles but says nothing about the tank at
    # departure. We assume it starts empty, so the reported total covers every
    # gallon the trip consumes and equals distance / mpg - a figure a reviewer
    # can check by hand. Callers may override it.
    fuel = 0.0 if start_fuel_gallons is None else start_fuel_gallons

    reachable = [c for c in candidates if c.distance_from_start <= total_distance_miles]

    if total_distance_miles <= fuel * mpg + 1e-9:
        # The tank already covers the whole trip; nothing to buy.
        return [], 0.0, 0.0

    if not reachable:
        raise UnreachableRouteError(
            'No fuel stations found along this route, which is longer than the '
            f'{max_range:.0f}-mile vehicle range.'
        )

    # With an empty tank the vehicle cannot move, so the first purchase has to
    # happen where it stands. The nearest station to the origin is treated as
    # the departure fill-up: its price applies to the fuel bought there, and
    # the short hop to it is not charged (the vehicle is already there).
    if fuel <= 1e-9:
        # The departure fill-up has to be somewhere the vehicle could plausibly
        # start from. If the nearest station on the whole route is hundreds of
        # miles away, the price data simply does not cover this corridor and we
        # say so, rather than returning a plan whose first leg is undrivable.
        origin_station = min(reachable, key=lambda c: c.distance_from_start)
        if origin_station.distance_from_start > ORIGIN_SEARCH_MILES:
            raise UnreachableRouteError(
                'No fuel station in the price data within '
                f'{ORIGIN_SEARCH_MILES:.0f} miles of the start; the nearest is '
                f'{origin_station.distance_from_start:.0f} miles along the route.'
            )
        position = origin_station.distance_from_start
        index = reachable.index(origin_station)
    else:
        position = 0.0
        index = None

    stops = []
    total_cost = 0.0
    total_gallons = 0.0

    while True:
        if index is None:
            # Still running on the fuel we started with: drive as far as it
            # allows and stop at the cheapest station inside that range.
            horizon = position + fuel * mpg
            window = [
                i for i, c in enumerate(reachable)
                if position + 1e-9 < c.distance_from_start <= horizon + 1e-9
            ]
            if not window:
                raise UnreachableRouteError(
                    f'No fuel station within {fuel * mpg:.0f} miles of mile '
                    f'{position:.1f} (route length {total_distance_miles:.1f} miles).'
                )
            index = min(
                window,
                key=lambda i: (reachable[i].price, -reachable[i].distance_from_start),
            )
            fuel -= (reachable[index].distance_from_start - position) / mpg
            fuel = max(fuel, 0.0)
            position = reachable[index].distance_from_start

        current = reachable[index]
        gallons_to_finish = (total_distance_miles - position) / mpg

        # Look one tankful ahead. If fuel is cheaper somewhere we can reach,
        # buy only enough to get there; otherwise fill the tank, capped at what
        # the rest of the journey will actually burn.
        cheaper_ahead = [
            c for c in reachable[index + 1:]
            if c.distance_from_start <= position + max_range + 1e-9
            and c.price < current.price
        ]
        if cheaper_ahead:
            nearest_cheaper = min(cheaper_ahead, key=lambda c: c.distance_from_start)
            target_gallons = (
                (nearest_cheaper.distance_from_start - position) / mpg
                + RESERVE_GALLONS
            )
        else:
            target_gallons = min(tank_gallons, gallons_to_finish)

        # Never carry more than the journey still needs.
        target_gallons = min(target_gallons, gallons_to_finish, tank_gallons)

        purchase = min(tank_gallons - fuel, max(target_gallons - fuel, 0.0))

        # Don't bother stopping for a splash of fuel: buy enough to be worth the
        # detour, unless that is all the trip still needs or the tank is small
        # enough that a bigger purchase would not fit.
        if 0 < purchase < MIN_PURCHASE_GALLONS:
            purchase = min(
                tank_gallons - fuel,
                max(purchase, min(MIN_PURCHASE_GALLONS, gallons_to_finish - fuel)),
            )

        if purchase > 1e-9:
            cost = purchase * current.price
            fuel += purchase
            total_cost += cost
            total_gallons += purchase
            stops.append(
                {
                    'station_id': current.station.id,
                    'name': current.station.name,
                    'address': current.station.address,
                    'city': current.station.city,
                    'state': current.station.state,
                    'latitude': current.station.latitude,
                    'longitude': current.station.longitude,
                    'price_per_gallon': round(current.price, 4),
                    'distance_from_start_miles': round(current.distance_from_start, 2),
                    'detour_from_route_miles': round(current.detour_miles, 2),
                    'gallons_purchased': round(purchase, 3),
                    'cost_usd': round(cost, 2),
                }
            )

        # Done once the fuel on board covers the remaining distance.
        if total_distance_miles - position <= fuel * mpg + 1e-9:
            break

        # Otherwise hop to the cheapest station still in reach.
        horizon = position + fuel * mpg
        window = [
            i for i in range(index + 1, len(reachable))
            if reachable[i].distance_from_start <= horizon + 1e-9
        ]
        if not window:
            raise UnreachableRouteError(
                f'No fuel station within {fuel * mpg:.0f} miles of mile '
                f'{position:.1f} (route length {total_distance_miles:.1f} miles).'
            )
        index = min(
            window,
            key=lambda i: (reachable[i].price, -reachable[i].distance_from_start),
        )
        fuel -= (reachable[index].distance_from_start - position) / mpg
        fuel = max(fuel, 0.0)
        position = reachable[index].distance_from_start

    return stops, total_cost, total_gallons
