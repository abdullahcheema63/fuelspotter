# Big downloads

Everything in this folder except this file is git-ignored. You only need these files to rebuild
the committed intermediate files in `../data/` from scratch (`run_all.py --refresh`); a normal
`run_all.py` uses the committed files and needs nothing here.

Run the commands from `research/geocoding/`.

## All The Places build (~2.7 GB) -> `data/atp_matches.csv` (step 2)

The exact build used was run `2026-10-03-13-32-35` (5,533 spiders, 2,731,861,789 bytes):

```
curl -L -C - -o downloads/output.zip \
  https://alltheplaces-data.openaddresses.io/runs/2026-10-03-13-32-35/output.zip
```

Other builds are listed at https://data.alltheplaces.xyz/runs/history.json. A newer build gives
slightly different matches, because chains open and close stores.

Step 1 also uses two older builds (`2026-09-19-13-32-18` for Casey's and Maverik, `2026-07-04-13-32-25`
for QuikTrip), but it reads just those chains' files straight out of the remote zips, so nothing
needs downloading here for it.

## OpenStreetMap, United States (~11 GB) -> `data/exits.csv` (step 4)

The extract used was Geofabrik's `us-latest`, data as of 2026-10-11T20:21:06Z. Geofabrik replaces
`us-latest` every day, so a fresh download will be a newer snapshot with slightly different exits.

```
brew install osmium-tool            # the command-line tool (macOS)
pip install osmium                  # the Python bindings step 4 uses
curl -L -C - -o downloads/us-latest.osm.pbf \
  https://download.geofabrik.de/north-america/us-latest.osm.pbf
# keep only exits and the highways they sit on (a few minutes, ~30 MB):
osmium tags-filter downloads/us-latest.osm.pbf \
  n/highway=motorway_junction w/highway=motorway,trunk -R -o downloads/us-exits.osm.pbf
```

Step 4 reads only `downloads/us-exits.osm.pbf`; the 11 GB file can be deleted once that exists.

## Census Gazetteer (~2 MB) -> `data/towns.csv` (step 3)

Nothing to do: step 3 downloads the 2024 files itself from
https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/.
