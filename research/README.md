# Research

How the API's two hard parts were worked out before any Django code was written:
where the fuel stations are (the price list has no coordinates), and how to choose the stops.
Nothing in here runs as part of the API; the API only reads the result, `data/stations_geocoded.csv`.

```
research/
  geocoding/      giving 6,626 stations coordinates  -> data/stations_geocoded.csv
  prototype/      the route + fuel-stop algorithm, from first version to the one the API was ported from
  requirements.txt  pandas, requests, osmium (the API itself needs none of these)
```

## 1. Geocoding

The provided price list has a name, a highway-style address (`I-44, EXIT 283 & US-69`), a city and a
state for each station, but no coordinates, and ordinary geocoders cannot read those addresses.
[`geocoding/GEOCODING.md`](geocoding/GEOCODING.md) tells the story: what was tried, what was dropped
and why, and the result.

In short, each station is placed by the best source available for it:

| Precision | Source | Stations |
|---|---|---|
| exact | the chain's own store locator, via [All The Places](https://www.alltheplaces.xyz/) | 2,633 |
| exit | the highway exit named in the address, from [OpenStreetMap](https://www.openstreetmap.org/) | 1,665 |
| town | the town centre, from the [US Census Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html), with the town's size | 2,171 |
| none | could not be placed (never used for planning) | 157 |

### Re-running it

```
pip install -r research/requirements.txt
python research/geocoding/run_all.py
```

This rebuilds `data/stations_geocoded.csv` in seconds from the small files committed in
`geocoding/data/`, and makes no big downloads. Each step skips itself when its output already exists:

| Step | Writes (committed) | Rebuilding it from scratch needs |
|---|---|---|
| `1_atp_fetch_older.py` | `data/atp_extra/*.geojson` | a few MB, read straight out of older All The Places builds |
| `2_atp_match.py` | `data/atp_matches.csv` | the full All The Places build, `output.zip` (~2.7 GB) |
| `3_towns.py` | `data/towns.csv` | the Census Gazetteer files (~2 MB) |
| `4_exits.py` | `data/exits.csv`, then `data/stations_geocoded.csv` | the US OpenStreetMap extract (~11 GB) and `osmium` |
| `5_make_check_files.py` | `checks/*.csv` | nothing; spreadsheets for checking placements by hand |

`--refresh` rebuilds every step from its original source. The big downloads go in
`geocoding/downloads/` (git-ignored); [`geocoding/downloads/README.md`](geocoding/downloads/README.md)
has the exact download commands, including the pinned All The Places build and the OpenStreetMap
snapshot date. All The Places and OpenStreetMap change over time, which
is why the intermediate files are committed: they are the snapshot the API's data was built from.

## 2. The algorithm

`prototype/` holds the terminal prototypes, in the order they were written:

| File | What it is |
|---|---|
| `plan_route.py` | First prototype: route, station matching, cheapest-fuel plan, map |
| `compare_strategies.py` | Compares strategies (naive driver, cheapest fuel, fewest stops, a cost per stop) on the same route; showed that a small cost per stop removes most stops for a few dollars |
| `plan_route_final.py` | The algorithm as decided: per-step highway matching, exact stop-cost search, gap-only relaxed matching, alternative routes, soft reserve |
| `fuel_route.py` | The same algorithm rewritten for readability; the API's services were ported from this file |
| [`TESTED_ROUTES.md`](prototype/TESTED_ROUTES.md) | Every route and edge case tested, with results; the API gives the same answers |

Run one from the repo root, e.g. `python research/prototype/fuel_route.py "Chicago, IL" "Dallas, TX"`.
It writes an HTML map into `prototype/maps/`.

## Data and credits

- Fuel prices: `data/fuel-prices-for-be-assessment.csv`, as provided with the assignment.
- Store locations: All The Places, CC0.
- Highway exits (`geocoding/data/exits.csv`) and the routing and geocoding services used at run time:
  © OpenStreetMap contributors, available under the Open Database License.
- Town centres and sizes: US Census Bureau, public domain.
