# Spotter fuel-route API — project context

Take-home for Spotter AI (Backend Django Engineer). Build a Django API: given a start and finish in the USA,
return the driving route, the cost-optimal fuel stops along it, and the total fuel cost.

## The assignment (verbatim requirements)
- Input: start and finish locations, both within the USA.
- Return a map of the route + optimal places to fuel up (optimal = mostly cost-effective, using the provided fuel prices).
- Vehicle max range 500 miles (so multiple stops may be needed); 10 miles per gallon. Return total money spent on fuel.
- Latest stable Django. Free map/routing API. Fast responses — "the quicker the better".
- Few calls to the routing API: 1 ideal, 2–3 acceptable.
- Deliverables: GitHub repo + a ≤5-min Loom demoing the API in Postman and walking through the code.

## Folder layout (parent folder `spotter/`, this repo is `spotter/spotter_api/`)
```
spotter/
  fuel-prices-for-be-assessment.csv   original data (8,151 rows, no coordinates)
  stations_geocoded.csv               FINAL geocoded stations (input for this app) — copy into this repo
  GEOCODING.md                        how stations were geocoded — copy into this repo's docs/
  geocoding/                          offline pipeline scripts (1_..5_) that produced stations_geocoded.csv
  prototype/fuel_route.py             READABLE final prototype — port the services from THIS file
  prototype/plan_route_final.py       the tested final prototype it was rewritten from (same behaviour)
  prototype/plan_route.py             first prototype (kept as the baseline to compare against)
  prototype/compare_strategies.py     first stop-cost search (5-mile units, slow) + strategy comparison
  downloads/                          big raw files (never commit)
  spotter_api/                        THIS Django project (git repo)
```
`prototype/TESTED_ROUTES.md` lists every route and edge case tested with their results — test the API on the same.
`prototype/fuel_route.py` is the **reference implementation** — port its logic into the app (typed, tested).
Do not import from it at runtime. Where it and the older prototypes disagree, `fuel_route.py` wins.

**Status:** algorithm locked; API build started 2026-10-14 (owner said go). Build order: setup, models +
`load_stations`, planner, routing, stations, geocoding, endpoint, API keys, map, schema, benchmark, README.
Docs (README, cheat sheet) are written last.

**History:** every planning request is saved as a `TripRecord` (inputs, resolved locations, outcome, totals,
calls, timing) with its `TripStop` rows (station FK, price and amounts copied). Stations that leave the price
list are marked `is_active=False`, never deleted, so history keeps pointing at them.

## Station data — `stations_geocoded.csv`
One row per US station (6,626; Canadian rows and duplicate IDs already handled; duplicate prices averaged).
Columns: `id` (OPIS truck-stop id), `name`, `address` (highway-style, e.g. `I-40, EXIT 76 & SR-72`), `city`, `state`,
`price` (USD/gal), `relevant` (address mentions an I- or US- highway), `town_radius_mi`, `lat`, `lon`,
`precision` (`exact` | `exit` | `town` | `none`), `method` (how it was placed).
- `exact` = chain's own store location (All The Places), `exit` = the highway exit named in the address
  (OpenStreetMap / Geofabrik), `town` = town center (US Census Gazetteer; `town_radius_mi` = town size), `none` = unplaced (157 rows — load but never use in planning).
- Geocoding is done **offline**; the API never geocodes stations at request time.

## Request flow (target)
1. Start/finish: either text (`"Chicago, IL"`) → Nominatim (US only), or `{"lat":..,"lng":..}` (no call). 0–2 calls.
2. Route: **one** OSRM call (`router.project-osrm.org`,
   `overview=full&geometries=geojson&steps=true&alternatives=3`). It returns the fastest route plus any
   alternatives (usually 1, sometimes none; ~0.3 s and ~2× the download vs. fastest-only). The fastest route is
   used whenever the truck can be fuelled along it; the alternatives are tried in order only if it can't.
   OSRM answers "no route" with **HTTP 400** + a JSON body (`code: NoRoute`/`NoSegment`) — read the body.
3. Stations on the route (local, no calls): project each nearby station onto the route line → `off_route_mi` and
   `mile` (mile marker). Projection is three passes (grid cells the route touches → nearest point among
   points sampled every mile → exact distance to segments within 5 mi). Keep a station if:
   - the highway in its address is one the route uses **at that point of the route**: OSRM step `ref`s with
     their mile ranges, matched within ±2 mi of the station's mile marker (stations with no parsable highway
     are judged on distance alone), and
   - it is close to the line: exact ≤ 1.0 mi, exit ≤ 1.5 mi, town ≤ min(town_radius + 1, 3.5) mi;
     town-level stations in big cities (radius > 5 mi) are skipped.
   - De-duplicate: stations within 0.1 mi of each other along the route → keep the cheaper.
   - Fallback 1: if the strict rules leave a gap the truck can't cross, relaxed rules (any station ≤ 3 mi from
     the line) apply **inside that gap only**, not along the whole route.
   - Fallback 2: if the route still can't be fuelled (e.g. the fastest road cuts through Canada, where we have
     no stations: San Diego → Madawaska ME has 919 such miles), use the next route from the same OSRM call and
     add a warning. If no route works → clear error naming the last station, the distance to the next one and
     how far the truck could go.
4. Choose stops (local): minimise **fuel cost + stop_cost × number_of_stops** (default `stop_cost_usd = 10`).
   Exact search `plan_stops` in `plan_route_final.py`, with real distances (no fuel units): every stop either
   fills up or buys just enough to reach the next stop empty, so two values per station are enough
   (E = cheapest way to arrive empty, F = cheapest way to leave full). `stop_cost_usd = 0` = pure cheapest plan
   (`plan_greedy` gives the same cost and is the test oracle; small random trips are checked against brute force).
   Measured on real data, LA→NY (2,789 mi, 213 candidates): cheapest = 16 stops/$700.05; stop cost $5 = 7/$703.33;
   **stop cost $10 = 6 stops/$709.35**; fewest stops = 5/$746.27. Chicago→Dallas (967 mi): 1 stop/$140.04.
   The 6-stop plan wins for any stop cost between roughly $6 and $34 (a 15-min stop at $10 ≈ $40/h of truck time).
   **Performance (measured):** station matching + stop choice = ~20–45 ms on 2,800–3,600 mi routes
   (first prototype: ~850 ms). Target for the API: both together under 100 ms. Measure and report.
   Cold requests are dominated by OSRM (1.2–2.2 s typical, occasionally 15–25 s) and Nominatim (~1 s each).
5. Response: JSON (summary, stops, route as GeoJSON) + an HTML map view.

## Agreed decisions / defaults
- Range 500 mi, 10 mpg (constants, not request params).
- Start with a **full tank** by default (`start_fuel_miles` param, default 500). "Total spent" = fuel **bought on the trip**;
  also report gallons used from the starting tank and fuel left on arrival.
- Reserve is a **server setting, not a request field** (decided 2026-10-14: it is fleet policy, set once):
  env `RESERVE_MILES`, default 0 = assignment's exact 500-mi range, validated at startup to 0–150.
  The plan never lets the tank drop below it; the value used is reported in `assumptions.reserve_miles`.
  (Realistic values are ~25–100 mi; the 250–450 runs in TESTED_ROUTES.md were stress tests. The prototype keeps
  its `--reserve` flag for testing.)
  It is the **last** thing to give way: if the trip can't be done with it (after relaxed stations in the gap and
  the alternative route), plan with the largest whole-mile reserve that works and add a warning naming the reserve
  used. Error only if the trip is impossible at reserve 0. Measured: a 25–100 mi reserve adds one stop on LA→NY
  and Chicago→Dallas; most of the extra cost is fuel still in the tank on arrival (sweep in `prototype/TESTED_ROUTES.md`).
- `stop_cost_usd` and `start_fuel_miles` are **optional request fields whose defaults come from the environment**
  (decided 2026-10-14): env `STOP_COST_USD` (default 10) and `START_FUEL_MILES` (default 500 = full tank).
  A request that omits them gets the env values; the values used are reported in `assumptions`.
- Detour to a station is not added to cost; each stop reports `off_route_miles` and `location_precision`.
- The fastest route is always preferred; an alternative from the same OSRM call is used only when the fastest
  can't be fuelled (see flow step 3). Routes are never compared on cost.
- Report the largest gap between usable stations on the route (warning if close to the range).
- The API response sticks to what the assignment asks for: **no strategy-comparison block** (cheapest vs chosen)
  and no stop-time fields — those stay in the prototype's `--compare` output.
- Known data limits: only 8 California stations (LA→NY has no station for the first 259 mi); 620 Canadian rows
  are not in the data; coordinates are checked against a USA bounding box only (Toronto would pass).

## API
- `POST /api/v1/route/`
  ```json
  {"start": "Chicago, IL", "finish": {"lat": 32.78, "lng": -96.80},
   "options": {"start_fuel_miles": 500, "stop_cost_usd": 10}}
  ```
  Response (shape, names can be refined in the plan): `summary` (distance_miles, duration_hours, fuel_burned_gallons,
  gallons_purchased, total_fuel_cost_usd, number_of_stops, gallons_from_starting_tank, fuel_left_on_arrival_miles),
  `assumptions`, `fuel_stops[]` (sequence, station_id, name, address, city, state, lat, lng, price_per_gallon,
  mile_marker, gallons, cost_usd, location_precision, off_route_miles), `route` (GeoJSON FeatureCollection:
  simplified LineString + stop Points), `warnings[]`, `meta` (external_calls, timings_ms, cache hits).
- `GET /api/v1/route/map/?start=...&finish=...` → HTML Leaflet map (route, all candidate stations as small dots,
  stops as pins with popups). Basemap: Esri World_Street_Map tiles (no key; OSM tiles block file:// and need a referer).
- OpenAPI schema + Swagger UI via drf-spectacular at `/api/schema/` and `/api/docs/`, with the API-key scheme documented.
- Errors: 400 bad input; 422 location not found / outside USA / no road route / trip impossible with 500-mi range;
  502/504 when Nominatim/OSRM fail or time out. Always a JSON body with a clear `detail`.

## Auth — API keys (`djangorestframework-api-key`, already installed)
- `rest_framework_api_key` in INSTALLED_APPS; default permission `HasAPIKey` for the API.
- Header: `Authorization: Api-Key <key>` (library default).
- Map view (opened in a browser, can't send headers): also accept `?api_key=<key>` via a small custom permission.
- Management command `create_api_key <name>` that prints a new key once. Keys manageable in Django admin.
- `djangorestframework_simplejwt` is installed but **not used** — leave it out of settings and requirements.

## Engineering rules
- Structure: `api/` app with `models.py` (FuelStation), `serializers.py`, `views.py`, `urls.py`,
  `services/` (`geocoding.py`, `routing.py`, `stations.py` (route matching), `planner.py`, `map_html.py`),
  `management/commands/` (`load_stations`, `create_api_key`), `tests/`.
- Serializers validate format only; external calls live in services. Services raise typed exceptions; views map them to HTTP codes.
- SQLite is fine. Stations are small (6.6k rows): load them once per process into numpy arrays (cache, refresh on `load_stations`).
- Cache Nominatim results (normalised text) and OSRM results (rounded coords) with Django's cache → repeat requests make 0 external calls.
- External calls: timeouts, a proper `User-Agent` (from settings/env; Nominatim returns 403 for generic agents), no retries storms.
- Settings from environment variables (`DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `NOMINATIM_USER_AGENT`, `OSRM_URL`, `NOMINATIM_URL`, `RESERVE_MILES`, `STOP_COST_USD`, `START_FUEL_MILES`) with dev defaults.
- `requirements.txt` with only what's used (Django, djangorestframework, drf-spectacular, djangorestframework-api-key, numpy, requests).
- `.gitignore`: `.venv/`, `db.sqlite3`, `.idea/`, `__pycache__/`, `.DS_Store`.
- Tests never hit the network (mock Nominatim/OSRM). Must cover: planner known answer (stations at mi 100/300/450/700/900
  priced 3.50/3.00/3.60/3.20/3.40, 1000-mi trip, full tank, stop_cost 0 → buys 30 gal at mi 300 + 20 gal at mi 700 = $154),
  infeasible gap, stop-cost reduces stops, station filtering (wrong highway / too far / big-city town ignored),
  API auth (no key → 401/403), validation errors, response shape.
- Small, focused commits with clear messages. Ask before adding new dependencies.
- The owner wants the plan agreed before code is written — propose, wait for approval, then build step by step.
