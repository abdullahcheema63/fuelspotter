# Routes tested with `plan_route_final.py`

Everything below was run on 2026-10-14 against the public OSRM and Nominatim servers with
`stations_geocoded.csv`. Numbers are as recorded at the time of each run; nothing was re-run to make this list.
The API should be tested on the same routes and give the same results.

Defaults unless a row says otherwise: full tank (500 mi), reserve 0, stop cost $10.

Run any of them from the repo root:

```
python research/prototype/plan_route_final.py "Los Angeles, CA" "New York, NY"
python research/prototype/plan_route_final.py "Chicago, IL" "Dallas, TX" --reserve 450
python research/prototype/plan_route_final.py "Los Angeles, CA" "New York, NY" --compare
```

## 1. Reference routes (use these first when testing the API)

| Start | Finish | Miles | Stations on route | Stops | Fuel cost |
|---|---|---|---|---|---|
| Los Angeles, CA | New York, NY | 2,789 | 213 | 6 | $709.35 |
| Chicago, IL | Dallas, TX | 967 | 97 | 1 | $140.04 |

LA → NY stops: Maverik #674 (North Las Vegas, NV) 27.0 gal, Maverik #693 (Green River, UT) 28.1 gal,
7-Eleven 42279 (Keenesburg, CO) 48.3 gal, Kum & Go #0370 (Gretna, NE) 50.0 gal, Caseys #3686 (Utica, IL) 40.3 gal,
Sheetz #639 (Youngstown, OH) 35.2 gal. 228.9 gal bought, 50.0 from the starting tank, arrives with 0 mi left.

Chicago → Dallas stop: SHELL (Osceola, AR), mile 482, 46.7 gal at $2.999.

Stop cost comparison (`--compare`):

| Stop cost | LA → NY | Chicago → Dallas |
|---|---|---|
| $0 (cheapest fuel) | 16 stops, $700.05 | 4 stops, $136.19 |
| $5 | 7 stops, $703.33 | 1 stop, $140.04 |
| $10 (default) | 6 stops, $709.35 | 1 stop, $140.04 |
| $20 | 6 stops, $709.35 | 1 stop, $140.04 |
| $40 | 5 stops, $746.27 | 1 stop, $140.04 |
| fewest stops | 5 stops, $746.27 | 1 stop, $140.04 |

## 2. Long routes

| Start | Finish | Options | Miles | Stations on route | Stops | Fuel cost |
|---|---|---|---|---|---|---|
| Key West, FL | Blaine, WA | | 3,564 | 223 | 7 | $953.72 |
| Seattle, WA | Miami, FL | | 3,299 | 221 | 7 | $860.94 |
| San Francisco, CA | Boston, MA | reserve 50 | 3,107 | 233 | 8 | $830.01 (arrives with 50 mi left) |
| Brownsville, TX | International Falls, MN | | 1,799 | 128 | 3 | $379.79 |
| El Paso, TX | Billings, MT | stop cost 0 | 1,186 | 44 | 6 | $210.04 (smallest purchase 0.3 gal) |

Seattle → Miami with `--compare`: cheapest fuel 18 stops/$850.61, stop cost $5 = 8 stops/$854.24,
$10 to $40 = 7 stops/$860.94, fewest = 6 stops/$908.39.

## 3. Route through Canada (alternative route used)

| Start | Finish | Result |
|---|---|---|
| San Diego, CA | Madawaska, ME | Fastest route (3,256 mi) runs through Ontario and Quebec: 919 mi with no station from mile 2,338. The alternative from the same OSRM call is used: 3,580 mi, 310 stations, 8 stops, $908.99. |
| Detroit, MI | Buffalo, NY | 255 mi, all through Ontario, no stations on it. No stop needed on a full tank; arrives with 245 mi left. |
| Anchorage, AK | Seattle, WA | Not possible: 2,220 mi from the start to the first station. No usable alternative. |

Checked for OSRM alternatives only (not planned): Detroit, MI → Boston, MA (fastest 706 mi with 240 mi in Ontario;
alternative 817 mi stays in the USA) and Chicago, IL → Portland, ME (fastest 1,093 mi stays in the USA).

## 4. Medium routes (from the search for off-route stops; fuel cost not recorded for most)

| Start | Finish | Miles | Stops | Furthest stop from the route |
|---|---|---|---|---|
| Spokane, WA | Minneapolis, MN | 1,378 | 3 | 0.14 mi |
| Albuquerque, NM | Sioux Falls, SD | 1,001 | 2 | 0.57 mi |
| Laredo, TX | Kansas City, MO | 970 | 1 | 0.22 mi |
| Boise, ID | Rapid City, SD | 969 | 1 | 0.00 mi |
| Tucson, AZ | Amarillo, TX | 670 | 1 | 0.01 mi |
| Savannah, GA | Louisville, KY | 639 | 1 | 0.00 mi |
| Memphis, TN | Omaha, NE | 635 | 1 | 0.01 mi |
| Fargo, ND | Billings, MT | 605 | 1 | 0.12 mi |
| Lubbock, TX | Denver, CO | 545 | 1 | 1.39 mi (see section 5) |
| Amarillo, TX | Wichita, KS | 384 | 0 | no stop needed |

## 5. Stops that sit off the route

| Start | Finish | Miles | Stop | Off route | Purchase |
|---|---|---|---|---|---|
| Lubbock, TX | Denver, CO | 545 | CEFCO #2070, Plainview, TX (town-level location) | 1.39 mi | 4.5 gal, $12.80 |
| 37.0261,-88.6123 (near Paducah, KY) | 43.7382,-98.9419 (near Kimball, SD) | 900 | QUIKTRIP #179, Liberty, MO (exact location) | 0.89 mi | 40.0 gal, $119.96 |
| 42.1989,-113.0249 (near Snowville, UT) | 44.5430,-104.0857 (near Piedmont, SD) | 700 | MAVERIK COUNTRY STORE #547, Casper, WY (exact location) | 0.83 mi | 20.0 gal, $63.71 |

Plainview is off the line because the station could only be placed at the town centre. Liberty and Casper are
real positions.

**Detours are not added (decision, 2026-10-14).** Distance, fuel and cost follow the highway route; the short
drive to a station and back is not included, and the map line passes by the station rather than through it.
Measured with an extra OSRM call routed through the station: Liberty +2.0 mi and +7 min (902.0 vs 900.0 mi),
Casper +2.3 mi and +4 min (702.3 vs 700.0 mi). Twice the off-route distance (1.8 and 1.7 mi) is a close estimate.
That is about 0.2% of the trip, so it is left out; every stop reports its off-route distance instead.
Two ways to include it were looked at and not adopted: estimating it locally inside the planner, and a second
routing call through the chosen stops (maps: `research/prototype/maps/option-b-liberty-mo.html`, `research/prototype/maps/option-b-casper-wy.html`).
This needs a one-line mention in the API docs.

## 6. Short trips and no-stop cases

| Start | Finish | Result |
|---|---|---|
| Reno, NV | Ely, NV | 319 mi, 7 stations, no stop needed, arrives with 181 mi left |
| 41.8781,-87.6298 | 41.5,-88.1 | 45 mi, 9 stations, no stop needed |
| Denver, CO | Denver, CO | 0 mi, no stop, $0.00 |

## 7. Starting fuel and reserve

The reserve is the last thing to give way: station rules in the gap first, then the alternative route, then the
largest reserve that works (with a warning). Rows below are from the final code, after that was added.

| Start | Finish | Options | Result |
|---|---|---|---|
| Chicago, IL | Dallas, TX | start fuel 30 | 3 stops, $288.39 (first stop at mile 19, 93.7 gal bought) |
| Chicago, IL | Dallas, TX | start fuel 0 | Not possible: first station is 3 mi from the start |
| Chicago, IL | Dallas, TX | reserve 450 | Fastest route has a 54-mile gap from mile 325 and the truck can only go 50, so the alternative route is used (925 mi): 22 stops, $276.85, reserve kept |
| Chicago, IL | Dallas, TX | reserve 100, start fuel 50 | Reserve can't be kept: planned with 47 mi, the largest that works. 4 stops, $294.09, first stop a relaxed station at mile 3 |
| Los Angeles, CA | New York, NY | start fuel 100 | Not possible: first station is at mile 259 (the alternative route fails too, and the reserve is already 0) |
| Los Angeles, CA | New York, NY | reserve 250 | Reserve can't be kept: planned with 240 mi, the largest that works. 12 stops, $809.42 |
| Los Angeles, CA | New York, NY | reserve 300 | Same as reserve 250: planned with 240 mi, 12 stops, $809.42 |

### Reserve sweep (full tank, stop cost $10, both routes from cache)

Los Angeles, CA → New York, NY (2,789 mi):

| Reserve | Usable range | Stops | Gallons bought | Fuel cost | vs reserve 0 |
|---|---|---|---|---|---|
| 0 | 500 | 6 | 228.9 | $709.35 | |
| 25 | 475 | 7 | 231.4 | $711.94 | +$2.59 |
| 50 | 450 | 7 | 233.9 | $723.71 | +$14.36 |
| 75 | 425 | 7 | 236.4 | $732.59 | +$23.24 |
| 100 | 400 | 7 | 238.9 | $743.47 | +$34.12 |
| 150 | 350 | 10 | 243.9 | $760.53 | +$51.18 |
| 200 | 300 | 10 | 248.9 | $789.78 | +$80.43 |
| 240 | 260 | 12 | 252.9 | $809.42 | +$100.07 |
| 250 | 250 | 12 | 252.9 | $809.42 | reserve can't be kept (first station is at mile 259): planned with 240 |

Chicago, IL → Dallas, TX (967 mi):

| Reserve | Usable range | Stops | Gallons bought | Fuel cost | vs reserve 0 |
|---|---|---|---|---|---|
| 0 | 500 | 1 | 46.7 | $140.04 | |
| 25 | 475 | 2 | 49.2 | $143.81 | +$3.77 |
| 50 | 450 | 2 | 51.7 | $153.66 | +$13.62 |
| 75 | 425 | 2 | 54.2 | $161.27 | +$21.23 |
| 100 | 400 | 2 | 56.7 | $170.84 | +$30.80 |
| 150 | 350 | 2 | 61.7 | $191.53 | +$51.49 |
| 200 | 300 | 3 | 66.7 | $202.13 | +$62.09 |
| 240 | 260 | 4 | 70.7 | $210.35 | +$70.31 |
| 250 | 250 | 4 | 71.7 | $213.47 | +$73.43 |

In every row the tank never went below the reserve and the truck arrived with exactly the reserve left.
Part of each cost increase is fuel still in the tank on arrival (reserve ÷ 10 gallons).

## 8. Bad input

| Start | Finish | Result |
|---|---|---|
| Honolulu, HI | Los Angeles, CA | No road route (OSRM answers HTTP 400, code NoRoute) |
| 30.0,-75.0 (in the Atlantic) | Miami, FL | No road route |
| Qwertyville Nowhere, ZZ | Dallas, TX | Location not found in the USA |
| 51.5,-0.12 (London) | Chicago, IL | Outside the USA |
| 43.6532,-79.3832 (Toronto) | Chicago, IL | Accepted (known limit: the USA check is a bounding box). 521 mi, 1 stop, 2.1 gal, $6.90 |

## 9. Planner checks without a route

- Known answer: stations at mile 100/300/450/700/900 priced 3.50/3.00/3.60/3.20/3.40, 1,000-mile trip, full tank,
  stop cost 0 → 30 gal at mile 300 + 20 gal at mile 700 = $154.00.
- 1,260 random small trips (mixed stop cost, reserve, start fuel) match a try-every-combination search;
  1,729 impossible ones agree.
- 300 random large trips (50 to 400 stations): stop cost 0 gives the same cost as the greedy. Slowest plan about 10 ms.
- Forced gap: with every strict station between mile 300 and 900 of Chicago → Dallas removed, relaxed stations
  were added only inside that gap and the trip planned; with none available it reported the mile where the tank runs dry.

## Timings seen

- Local work (find stations + choose stops): 5 to 45 ms; 21 to 44 ms on LA → NY.
- OSRM: 1.2 to 2.2 s typical, 14.9 s and 23.5 s seen once each. Nominatim: 0.6 to 2.1 s per lookup.
