# Fuel Route API

A Django REST API that plans a cost-optimal fuelling itinerary for a drive
between two US locations.

Given a start and a finish, it returns the driving route, the truck stops to
refuel at, how many gallons to buy at each, and the total fuel bill — choosing
stops so the fuel cost is as low as the price data allows.

**Vehicle model:** 500-mile maximum range, 10 miles per gallon, so a full tank
is 50 gallons.

---

## Quick start

```bash
# 1. Database
docker compose up -d

# 2. Environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # defaults match docker-compose.yml

# 3. Schema and data
python manage.py migrate
python manage.py load_fuel_prices
python manage.py geocode_stations   # ~94% offline in 2s, rest over the network

# 4. Run
python manage.py runserver
```

Then:

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start": "New York, NY", "finish": "Chicago, IL"}'
```

---

## Endpoints

### `POST /api/route/`

```json
{ "start": "New York, NY", "finish": "Chicago, IL" }
```

The same pair also works as GET query parameters, which is handy in a browser:
`GET /api/route/?start=Boston,+MA&finish=Philadelphia,+PA`

Response (abridged):

```json
{
  "cached": false,
  "start":  { "query": "New York, NY", "latitude": 40.71, "longitude": -74.01 },
  "finish": { "query": "Chicago, IL",  "latitude": 41.88, "longitude": -87.62 },
  "route": {
    "distance_miles": 790.57,
    "duration_hours": 13.2,
    "geometry_polyline": "qxwwFv|bbM...",
    "geometry_precision": 5,
    "point_count": 12647
  },
  "vehicle": {
    "max_range_miles": 500.0,
    "miles_per_gallon": 10.0,
    "tank_capacity_gallons": 50.0,
    "assumed_tank_at_start_gallons": 0.0
  },
  "fuel_stops": [
    {
      "station_id": 1852,
      "name": "7-ELEVEN #40084",
      "address": "US-46/US-1/US-9",
      "city": "Palisades Park",
      "state": "NJ",
      "latitude": 40.847017,
      "longitude": -73.997061,
      "price_per_gallon": 3.099,
      "distance_from_start_miles": 4.26,
      "detour_from_route_miles": 8.22,
      "gallons_purchased": 6.002,
      "cost_usd": 18.6
    }
  ],
  "summary": {
    "stop_count": 4,
    "expected_gallons_for_trip": 79.057,
    "total_gallons": 78.63,
    "total_fuel_cost_usd": 240.25,
    "average_price_per_gallon": 3.0555,
    "stations_considered": 117,
    "routing_api_calls": 1
  }
}
```

`geometry_polyline` is an encoded polyline at precision 5 — the format
Google Maps, Leaflet and Mapbox all decode directly.

### `GET /api/health/`

Liveness, plus how many stations are loaded and how many have coordinates.

---

## How it meets the brief

| Requirement | How |
|---|---|
| Latest stable Django | Django 5.2 |
| Route between two US locations | `POST /api/route/` |
| Optimal, cost-effective fuel stops | greedy look-ahead planner (below) |
| Multiple fuel-ups over 500-mile range | stops chained until the finish is in range |
| Total money spent on fuel | `summary.total_fuel_cost_usd` |
| Fuel prices from the supplied CSV | `load_fuel_prices`, 6,967 unique stations |
| Free map/routing API | OSRM (routing), Nominatim + US Census (geocoding) |
| Fast responses | ~1s cold, ~2ms cached |
| Minimal calls to the routing API | **exactly one per request**, asserted in the tests |

### One routing call per request

The only request-time call to the routing provider is a single OSRM
`/route/v1/driving` request. Everything else is local:

- **Station coordinates** are resolved *before* the API is used, by
  `geocode_stations`, and stored in Postgres.
- **Start and finish** are geocoded once each and memoised in `GeocodeCache`,
  so repeat place names cost nothing.
- **Finding stations near the route** is a Postgres bounding-box query plus
  local geometry — no network at all.

`PlannerIntegrationTests.test_plan_makes_exactly_one_routing_call` pins this
down with a counting stub.

### The fuel algorithm

Standing at a station, look one full tank (500 miles) ahead:

- **Somewhere cheaper in reach** → buy just enough to get there, and fill up
  where fuel is cheaper.
- **Nothing cheaper in reach** → this is the best price available, so fill the
  tank — but never more than the rest of the trip will burn.

Then drive to the cheapest station still in reach and repeat. Two practical
refinements:

- A stop must buy at least 5 gallons (`MIN_PURCHASE_GALLONS`). Without it, a
  road where prices fall steadily yields a stop every few miles for a splash of
  fuel each — cheapest on paper, useless as an itinerary. The floor costs a few
  cents and roughly halves the stop count.
- Each purchase keeps a 1-gallon reserve (`RESERVE_GALLONS`), capped by what
  the trip still needs. Buying only enough to coast into the next station means
  arriving on a dead-empty tank at every stop, which no driver would plan for.
- Stations sharing a coordinate are collapsed to the cheapest one, because the
  CSV lists the same truck stop under several names.

If no station in the price list lies within 100 miles of the start
(`ORIGIN_SEARCH_MILES`), the request fails with **422** and says so. This is a
real case: the CSV holds only ten Californian stations, all near the Mexican
border, so Los Angeles to Seattle has no usable station until mile 878.
Returning a plan whose first leg is undrivable would be worse than refusing
one.

### Geocoding, and why it is a two-pass job

The CSV gives addresses but no coordinates, and routing needs coordinates.
There are ~3,900 distinct city/state pairs, which at Nominatim's one-request-
per-second limit is over an hour.

So pass one reads the **US Census Gazetteer** — two small files committed under
`data/` — and resolves ~94% of pairs offline in about two seconds. Pass two
sends only the remainder to Nominatim.

Names are matched on a punctuation-free key, which matters more than it sounds:
the CSV writes `Oneill, NE` while the Census writes `O'Neill`, and the fallback
geocoder answered that query with a town in **Connecticut**, 1,300 miles from
the real Nebraska one — putting a phantom fuel stop on east-coast routes.
`geocode_stations --revalidate` re-checks stored coordinates against the
Gazetteer and corrects any that disagree by more than 400 miles.

---

## Assumptions

These are the places the brief left open. Each is a deliberate choice, not an
oversight.

1. **The tank starts empty.** The brief asks for "the total money spent on
   fuel" but not how full the tank is at departure. Starting empty makes the
   reported total cover every gallon the trip consumes, so
   `total_gallons × 10 mpg` equals the distance driven and the figure can be
   checked by hand. A caller can pass `start_fuel_gallons` to model a full tank
   instead.
2. **The first stop is the departure fill-up.** An empty tank cannot move, so
   the station nearest the origin is where the journey begins; the short hop to
   it is not charged.
3. **City-level station coordinates.** Stations are placed at their city
   centroid rather than their street address. At a 500-mile planning range this
   is well inside the 10-mile corridor tolerance, and it avoids ~7,000
   address-level geocoding calls.
4. **A 10-mile corridor.** A station must lie within 10 miles of the route to
   be a candidate (`STATION_CORRIDOR_MILES`). Reported as
   `detour_from_route_miles` on each stop.
5. **Prices are a flat snapshot.** The CSV has no timestamps, so the price for
   a station is taken as current.
6. **US routing only.** The CSV contains ~112 Canadian truck stops; they are
   loaded but the brief specifies US start and finish points.
7. **Price coverage is uneven, and that is visible.** Station counts range from
   790 in Texas to 10 in California, so some corridors cannot be planned at
   all. The API reports this as a 422 rather than inventing a stop.

---

## Tests

```bash
python manage.py test routing
```

24 tests. The fuel planner is checked by replaying each plan through a
feasibility simulator that asserts the vehicle never runs dry, never overfills
the tank, and always reaches the destination — which catches plans that look
cheap but are not drivable.

---

## Layout

```
fuelroute/settings.py              configuration, vehicle constants
routing/models.py                  FuelStation, GeocodeCache
routing/views.py                   /api/route/, /api/health/
routing/serializers.py             request validation
routing/services/
    providers.py                   OSRM router, Nominatim geocoder
    gazetteer.py                   offline US place lookup
    geocoding.py                   cached geocoding
    geo.py                         haversine and polyline helpers
    fuel.py                        corridor search + the planner
    planner.py                     orchestration
routing/management/commands/
    load_fuel_prices.py            CSV -> Postgres, de-duplicated
    geocode_stations.py            offline-first coordinate resolution
data/                              US Census Gazetteer files
```

## Configuration

Set in `.env`; defaults suit local development.

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_*` | see `.env.example` | database connection |
| `STATION_CORRIDOR_MILES` | `10` | how far off-route a station may be |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | routing provider |
| `NOMINATIM_BASE_URL` | `https://nominatim.openstreetmap.org` | geocoding provider |
| `HTTP_TIMEOUT_SECONDS` | `20` | upstream timeout |

The public OSRM and Nominatim instances are shared, best-effort services and do
occasionally time out; both are swappable via these variables, and upstream
failures surface as `502` with a clear message rather than a stack trace.
