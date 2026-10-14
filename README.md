# FuelSpotter

Fuel is the biggest cost a trucking fleet can still control. FuelSpotter is a Django REST API that
takes a start and a finish in the USA and returns the driving route, where to buy fuel along it, and
the total spent on fuel. The truck has a 500-mile range and does 10 miles per
gallon. Fuel prices come from the provided price list of 6,626 US truck stops.

```
POST /api/v1/route/   {"start": "Chicago, IL", "finish": "Dallas, TX"}

-> 967 mi, 1 stop: SHELL, Osceola AR (I-55 exit 48), 46.7 gal at $2.999
   total spent on fuel: $140.04, plus the route as GeoJSON and a link to a map
```

- **Fast:** about 10–30 ms of work per request. A first request for a new trip takes 2–5 s,
  almost all of it the free routing and geocoding services; repeating it takes ~20 ms with no
  external calls.
- **Few external calls:** one routing call per trip (it also returns the alternative routes),
  plus one place lookup per place name; coordinates need none. Answers are cached.
- **Map:** every response includes `map_url`, a link to the trip on a map.
- **History:** every request, planned or failed, is stored with its stops.

## Contents

- [Run it](#run-it)
- [Use it](#use-it)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Limitations](#limitations)
- [Credits](#credits)

## Run it

### With Docker (API + PostgreSQL + Redis)

```
cp .env.example .env            # set DJANGO_SECRET_KEY and POSTGRES_PASSWORD
make setup                      # build, start, create the tables, load the stations
make api-key NAME=postman       # prints a key, shown once
```

The API is on http://localhost:8000. `make help` lists everything; day to day it is `make up` and
`make down`. Data lives in a Docker volume, so it survives restarts. After pulling new code run
`make migrate`; after changing the price list run `make load-stations`.

### Without Docker (SQLite + in-memory cache)

Python 3.12+.

```
make install                    # .venv, requirements, .env from .env.example, tables, stations
make dev-api-key NAME=postman
make dev                        # http://127.0.0.1:8000
```

## Use it

Send the key in the header `Authorization: Api-Key <key>`. Interactive docs, with an Authorize button
for the key, are at http://localhost:8000/api/docs/ (OpenAPI schema at `/api/schema/`).

### Request

```
POST /api/v1/route/
Content-Type: application/json
Authorization: Api-Key <key>

{
  "start": "Los Angeles, CA",
  "finish": {"lat": 40.7128, "lng": -74.0060},
  "options": {"start_fuel_miles": 500, "stop_cost_usd": 10}
}
```

| Field | Required | Meaning |
|---|---|---|
| `start`, `finish` | yes | A place in the USA (`"Chicago, IL"`), `"lat,lng"` text, or `{"lat": .., "lng": ..}` |
| `options.start_fuel_miles` | no | Fuel in the tank at the start, in miles of range (0–500). Default: full tank |
| `options.stop_cost_usd` | no | What one fuel stop is worth, in dollars (default 10). `0` = cheapest fuel whatever the number of stops |

### Response (shortened)

```json
{
  "map_url": "http://localhost:8000/api/v1/route/map/2732178e-.../",
  "summary": {
    "distance_miles": 2789.3, "duration_hours": 49.9,
    "fuel_burned_gallons": 278.93, "gallons_purchased": 228.93, "gallons_from_starting_tank": 50,
    "total_fuel_cost_usd": 709.35, "number_of_stops": 6, "fuel_left_on_arrival_miles": 0
  },
  "fuel_stops": [
    {"sequence": 1, "station_id": 72965, "name": "Maverik #674", "address": "I-15, Exit 45",
     "city": "North Las Vegas", "state": "NV", "lat": 36.195069, "lng": -115.142593,
     "price_per_gallon": 3.2823, "mile_marker": 270.5, "gallons": 27.05, "cost_usd": 88.78,
     "location_precision": "exact", "off_route_miles": 0.19, "matched_by": "strict"}
  ],
  "route": {"type": "FeatureCollection", "features": ["the route LineString, one Point per stop"]},
  "assumptions": {"range_miles": 500, "miles_per_gallon": 10, "start_fuel_miles": 500,
                  "stop_cost_usd": 10, "reserve_miles": 0, "notes": ["..."]},
  "start": {"lat": 34.05, "lng": -118.24, "label": "Los Angeles, ..."},
  "finish": {"lat": 40.71, "lng": -74.01, "label": "New York, ..."},
  "warnings": [],
  "meta": {"external_calls": 3, "cache": {"geocode_hits": 0, "route_hit": false},
           "candidate_stations": 213, "largest_gap_miles": 259.3, "used_alternative_route": false,
           "timings_ms": {"geocode": 1270.4, "route": 2140.9, "plan": 10.2, "total": 3425.8}}
}
```

`total_fuel_cost_usd` is the money spent on fuel during the trip. The fuel already in the tank at
the start is not counted, and the stop cost only decides how many stops are worth making; it is
never added to the total. `map_url` opens in a browser without an API key and expires after 24 hours.

### Errors

Every error is JSON with a `detail` sentence and a `code`.

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_input` | Bad request body; `errors` lists each field |
| 403 | `permission_denied` | Missing or invalid API key |
| 422 | `location_not_found`, `outside_usa` | A place can't be found in the USA |
| 422 | `no_route` | No road between the two places (e.g. Honolulu to Los Angeles) |
| 422 | `trip_impossible` | No fuel within reach somewhere on every route; says where the tank runs dry |
| 502 / 504 | `upstream_error` / `upstream_timeout` | The routing or geocoding service failed or timed out |

## How it works

1. **Locate** the start and finish. Coordinates are used as given; place names go to Nominatim
   (OpenStreetMap's geocoder), limited to the USA, preferring a city over a state of the same name.
2. **Route** with one call to OSRM, which returns the fastest route plus any alternatives. Each
   stretch of the route records which highways it is on (I 40, US 287, ...).
3. **Find usable stations.** Every station is projected onto the route line, giving its distance
   from the route and its mile marker. A station is usable if it is close to the line (1 mile for a
   station placed at its exact store location, 1.5 for one placed at its highway exit, up to 3.5 for
   one placed at its town centre) **and** its address names a highway the route uses at that point.
   Stations within 0.1 mile of each other count as one, keeping the cheaper.
4. **Choose the stops** that minimise fuel cost + `stop_cost_usd` × number of stops. The search is
   exact. In an optimal plan every stop either fills the tank or buys just enough to reach the next
   stop empty (the classic "gas station problem" result), so two values per station are enough:
   the cheapest way to arrive there empty, and the cheapest way to leave it full.
5. **Answer** with the stops, totals, route GeoJSON and map link, and store the trip.

If the trip can't be done as asked, things give way in this order:
1. Looser station rules (any station within 3 miles) inside the stretch the truck can't cross only.
2. The next route OSRM returned. For example, the fastest route from San Diego to Madawaska, Maine
   runs 919 miles through Canada, where the price list has no stations, so a longer US route is used.
3. A smaller reserve (see `RESERVE_MILES`), with a warning.

Otherwise the answer is a 422 that says where the fuel runs out.

### Why a stop cost

Cheapest fuel alone makes silly plans: Los Angeles to New York becomes 16 stops, several of them for
under a gallon. Counting each stop as $10 gives 6 stops for $9 more. The 6-stop plan wins for any
stop cost between about $6 and $37: below that the plan grows to 7 stops, above it the truck drops to
5 stops and pays $37 more for fuel. $10 is the default and each request can change it.

| Stop cost | LA → NY | Fuel cost |
|---|---|---|
| $0 (cheapest fuel) | 16 stops | $700.05 |
| $5 | 7 stops | $703.33 |
| $10 (default) | 6 stops | $709.35 |
| $40 | 5 stops | $746.27 |

### Where the stations' locations come from

The price list has no coordinates, and addresses like `I-44, EXIT 283 & US-69` defeat ordinary
geocoders, so the stations were placed offline before any API code was written:

| Placed at | Source | Stations |
|---|---|---|
| exact store location | the chains' own store locators, via All The Places | 2,633 |
| the highway exit in its address | OpenStreetMap | 1,665 |
| town centre (with the town's size) | US Census Gazetteer | 2,171 |
| not placed (never used) | | 157 |

The story, the scripts and the intermediate data are in [`research/`](research/README.md);
`make geocode` rebuilds `data/stations_geocoded.csv` from them. The algorithm's prototypes and every
route tested while designing it are in [`research/prototype/`](research/prototype/TESTED_ROUTES.md).

### Speed

Measured on Los Angeles → New York (2,789 miles, 213 usable stations):

| Stage | Time |
|---|---|
| Finding usable stations (6,469 placed stations, 33,778 route points) | ~7 ms |
| Choosing the stops | ~4 ms |
| A repeated request, end to end | ~20 ms |
| A new request | 2–5 s, almost all of it OSRM and Nominatim |

Stations are found in three passes, each cheaper than the next is thorough: a grid keeps only
stations near the route (6,469 → 469), the nearest of the route points sampled every mile drops
those too far away (→ 344), and the exact distance is measured only to the route segments around
that point (→ 337). Checking every station against every segment took about 500 ms.

### Caching

| What | Where | Kept for |
|---|---|---|
| Place lookups | Django cache (Redis in Docker, in-memory otherwise) | 30 days (1 hour if not found) |
| Routes, parsed | Django cache | 24 hours |
| Stations | each server process's memory, as arrays | until `load_stations` changes them |
| Map pages | the database, with the trip | `MAP_LINK_HOURS` |

Plans are not cached: they take ~10 ms, so a request with different options never gets a stale
answer.

## Configuration

Settings come from environment variables; `.env` (copy `.env.example`) is read if present, and real
environment variables win. Every value has a working default.

| Variable | Default | Meaning |
|---|---|---|
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` | development values | Django basics |
| `DATABASE_URL` | SQLite in the project folder | e.g. `postgres://user:pass@host:5432/db` (Docker sets this itself) |
| `CACHE_URL` | in-memory | e.g. `redis://host:6379/0` (Docker sets this itself) |
| `RESERVE_MILES` | 0 | Never plan the tank below this many miles (0–150). Fleet policy, so not a request field |
| `STOP_COST_USD` | 10 | Default for `options.stop_cost_usd` |
| `START_FUEL_MILES` | 500 | Default for `options.start_fuel_miles` |
| `MAP_LINK_HOURS` | 24 | How long `map_url` works; 0 = no expiry |
| `OSRM_URL`, `NOMINATIM_URL` | the public servers | Point these at your own servers for real traffic |
| `NOMINATIM_USER_AGENT` | this project | Nominatim refuses generic user agents |
| `WEB_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | 8000, spotter, ... | Docker only |

## Project layout

```
api/
  models.py             FuelStation, TripRecord (one per request), TripStop
  views.py              POST /api/v1/route/ and the map page
  serializers.py        request validation and the documented response shape
  services/
    geocoding.py        step 1: place names -> coordinates (Nominatim, cached)
    routing.py          step 2: the OSRM call, highway sections (cached)
    stations.py         step 3: stations in memory, matching them to the route
    planner.py          step 4: the exact stop search
    trip.py             the whole request, the fallbacks and the response
    history.py          saving each request
    map_html.py         the map page
  management/commands/  load_stations, create_api_key
data/                   the price list and the geocoded stations
research/               how the stations were geocoded and how the algorithm was designed
```

## Limitations

- **Detours are not added.** Distance, fuel and cost follow the highway route; the short drive to a
  station and back (about twice its `off_route_miles`, typically under 2 miles) is not included.
- **The public OSRM and Nominatim servers** have no uptime guarantee and ask for at most about one
  request per second. A new trip takes 2–5 s, and OSRM has occasionally taken 15–25 s. Driving times
  are OSRM's car estimates, not truck times.
- **Gaps in the price list:** it has only 8 stations in California, so a trip from Los Angeles has no
  usable station for its first 259 miles. It has no Canadian stations, which is why a route through
  Canada is replaced by a US one.
- **"Inside the USA"** is checked against a box around the country, so coordinates in southern Canada
  or northern Mexico pass the check.
- **Station locations:** 33% of stations are placed at their town centre. Matching only accepts them
  in small towns and close to the route, but the pin can still be a mile or two from the real pump.

## Credits

- Routing: [OSRM](https://project-osrm.org/) public server. Geocoding: [Nominatim](https://nominatim.org/).
  Both use OpenStreetMap data, © OpenStreetMap contributors, available under the
  [Open Database License](https://www.openstreetmap.org/copyright). Usage policies:
  [OSRM](https://github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy),
  [Nominatim](https://operations.osmfoundation.org/policies/nominatim/).
- Station locations: [All The Places](https://www.alltheplaces.xyz/) (CC0), OpenStreetMap highway
  exits, and the [US Census Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html)
  (public domain).
- Map tiles: Esri World Street Map, drawn with [Leaflet](https://leafletjs.com/).
