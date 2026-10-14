# How the fuel stations were geocoded

The provided price file (`fuel-prices-for-be-assessment.csv`) has station name, address, city, state and price,
but **no coordinates**. Addresses are highway descriptions, not street addresses
(e.g. `I-40, EXIT 76`), so normal geocoders can't read them.

Geocoding is done **once, offline**; the result is `data/stations_geocoded.csv`, which the
`load_stations` management command loads into the database.
The API never geocodes stations at request time.

## Starting point

| | Count |
|---|---|
| Rows in CSV | 8,151 |
| US rows (620 Canadian rows dropped) | 7,531 |
| **Unique US stations** (by OPIS ID; duplicate rows with different prices averaged) | **6,626** |
| Relevant to trucks: address on an interstate or US highway | 5,603 |
| Local / state road only (trucks unlikely to use) | 1,023 |

**Canadian rows are left out.** The assignment is US-only and the sources below (US OpenStreetMap extract,
US Census) don't cover Canada; Canadian prices are also higher (median 4.45 vs 3.40), so they would rarely be chosen.
A route between two US places can still cut through Canada (e.g. Detroit → Buffalo via Ontario) — it then has
no stations on that stretch. That only matters if the stretch is longer than the truck's range, in which case
a longer alternative route is used.

## Step 1 — Nominatim (online geocoder): tried, dropped

- Address as written: **0%** real matches (it can't read `I-40, EXIT 76`).
- Station name + town: ~45% returned something, but only **~15% reliable** — it can't tell apart
  several same-brand stations in one town (e.g. picked the wrong Pilot in Knoxville).
- Also rate-limited to 1 request/second.

## Step 2 — All The Places (chains' own store locators): main source

[All The Places](https://www.alltheplaces.xyz/) publishes, weekly and free (CC0), every location scraped from
brands' official store finders, with coordinates and store numbers.

- Matched CSV store numbers (e.g. `LOVES TRAVEL STOP #865`) to ATP `ref`, same state required.
- Second pass: brand + same city, only when exactly one such store exists (ambiguous cases skipped).
- QuikTrip, Casey's and Maverik were broken in the latest build (missing coordinates / scraper blocked /
  proxy account suspended) — taken from older builds (2026-07-04, 2026-09-19) after checking build logs.
- Result: **2,639 stations placed exactly** (2,254 by store number, 385 by brand + city).
  Love's, Kwik Trip, Sheetz, QuikTrip ~100%; Pilot/Flying J 90%; TA/Petro 80%.
- Of the 5,603 relevant stations: **2,305 placed exactly (41%)**.

## Step 3 — OpenStreetMap highway exits (Geofabrik download)

Many remaining addresses name an exit (`I-40, EXIT 76`). A truck stop sits at its exit, so the exit's location
is a good stand-in (~0.5 mi). Exit locations come from the [Geofabrik](https://download.geofabrik.de/) US
OpenStreetMap extract, filtered locally to highway exits (`osmium tags-filter`), matched by
highway + exit number nearest the station's town.

- 11.4 GB US extract filtered to 48,107 numbered exits (39,157 linked to an I-/US- highway).
- Result: **1,665 stations placed at their exit** (all relevant).
- Also used to verify the ATP brand+city matches: **224 confirmed** (within 2 mi of the named exit),
  **6 replaced** by the exit (wrong store picked), 155 had no exit to check against.
- (Overpass, OSM's live query service, was tried first for this and dropped — too slow/unreliable.)

## Step 4 — Census town centers: fallback

Remaining stations are placed at their town's center from the
[US Census Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.2024.html),
which also gives each town's land area, so the possible error is known (town radius).

- Expected: ~1,375 relevant stations (25%) — mostly stations on US highways through towns.
- Measured: 3,653 of 3,808 station towns (96%) found in the Census list. Town radius (center to edge):
  median **1.4 mi**, 90% within **3.6 mi** for the stations left at town level — so the worst-case error is small.

## How precision is used

Each station stores `precision` = `exact` | `exit` | `town` (or `none` if it could not be placed). When picking fuel stops, a station counts as
"on the route" only if it's on a highway the route actually uses and close to the route line — trucks stay on
highways, so local-road stations are ignored.

## Not used, and why

- **Google Geocoding API**: needs a billing account; its terms forbid storing coordinates beyond 30 days and
  using results on a non-Google map.

## Final result

| | All US (6,626) | Relevant (5,603) |
|---|---|---|
| exact (store location) | 2,633 (40%) | 2,299 (41%) |
| exit (~0.5 mi) | 1,665 (25%) | 1,665 (30%) |
| town center (median radius 1.4 mi) | 2,171 (33%) | 1,509 (27%) |
| not placed | 157 (2%) | 130 (2%) |

**71% of relevant stations placed exactly or at their exit.** Output: `stations_geocoded.csv`.

### The town-level stations, by town size

The 1,509 relevant town-level stations sit in 1,217 unique towns. Town radius (center to edge):

| Town radius | Towns | Stations |
|---|---|---|
| < 1 mi | 403 | 439 |
| 1–2 mi | 450 | 543 |
| 2–3 mi | 189 | 258 |
| 3–5 mi | 125 | 181 |
| 5–10 mi | 43 | 74 |
| 10+ mi | 7 | 14 |

82% of town-level stations are in towns under 3 mi in radius, so the pin is close. Only 88 (about 1.5% of relevant
stations) are in big cities (radius over 5 mi, e.g. Oklahoma City, Phoenix, San Antonio), where a city-center pin
says little about where the station actually is.