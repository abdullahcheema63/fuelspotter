"""
Make spreadsheets for checking the geocoding by hand.
Each row has two clickable links:
  pin_link    -> Google Maps at the coordinates WE assigned
  search_link -> Google Maps search for the station name + town (where Google thinks it is)
Open both: if they land in the same spot (or the pin sits on the right exit / in the right town), the row is good.

Usage (from the repo root):  python research/geocoding/5_make_check_files.py
Writes into research/geocoding/checks/:
  exit_stations.csv          all stations placed at a highway exit
  town_stations.csv          relevant stations placed at a town center (with town radius)
  unplaced_stations.csv      stations we could not place
  sample_exit_25.csv / sample_town_25.csv   random 25 of each — the ones to actually click through
"""
from pathlib import Path
from urllib.parse import quote_plus
import pandas as pd

from paths import FINAL, HERE
OUT = HERE / "checks"
OUT.mkdir(exist_ok=True)

s = pd.read_csv(FINAL)
s["pin_link"] = s.apply(lambda r: f"https://www.google.com/maps/search/?api=1&query={r.lat},{r.lon}"
                        if pd.notna(r.lat) else "", axis=1)
s["search_link"] = s.apply(lambda r: "https://www.google.com/maps/search/?api=1&query="
                           + quote_plus(f"{r['name']} {r.city} {r.state}"), axis=1)

cols = ["id", "name", "address", "city", "state", "method", "lat", "lon", "pin_link", "search_link"]

ex = s[s.precision == "exit"][cols].sort_values(["state", "city"])
tw = s[(s.precision == "town") & s.relevant][cols[:6] + ["town_radius_mi"] + cols[6:]].sort_values("town_radius_mi", ascending=False)
no = s[s.precision == "none"][["id", "name", "address", "city", "state", "relevant", "search_link"]]

ex.to_csv(OUT / "exit_stations.csv", index=False)
tw.to_csv(OUT / "town_stations.csv", index=False)
no.to_csv(OUT / "unplaced_stations.csv", index=False)
ex.sample(min(25, len(ex)), random_state=1).to_csv(OUT / "sample_exit_25.csv", index=False)
tw.sample(min(25, len(tw)), random_state=1).to_csv(OUT / "sample_town_25.csv", index=False)

print(f"exit-placed stations:      {len(ex):5d} -> checks/exit_stations.csv")
print(f"town-placed (relevant):    {len(tw):5d} -> checks/town_stations.csv")
print(f"not placed:                {len(no):5d} -> checks/unplaced_stations.csv")
print("random samples to click through: checks/sample_exit_25.csv, checks/sample_town_25.csv")
