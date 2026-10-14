"""
Town locations + sizes from the US Census Gazetteer (official list of every US town/CDP with
its land area and center point). Small download (~2 MB), runs in seconds.

Usage (from the repo root):  python research/geocoding/3_towns.py [--refresh]
Writes: research/geocoding/data/towns.csv  (state, city, lat, lon, land_sqmi, radius_mi, source)
Skipped if towns.csv already exists, unless --refresh is given.
Prints: how many unique station towns were found, and how big they are
        (radius = radius of a circle with the town's land area).
"""
import io, math, re, sys, zipfile
import pandas as pd
import requests
from pathlib import Path
from paths import CSV, DATA, DL, FINAL, refresh_requested

ATP = DATA / "atp_matches.csv"
OUT = DATA / "towns.csv"
if OUT.exists() and not refresh_requested():
    print(f"step 3: {OUT.name} exists -- skipped (use --refresh to download the Census files again)")
    sys.exit(0)
GAZ = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/"
FILES = {"place": "2024_Gaz_place_national.zip",      # cities, towns, villages, CDPs
         "cousub": "2024_Gaz_cousubs_national.zip"}   # townships etc. (fallback, common in NJ/PA/MI)
CA = {"AB", "BC", "MB", "NB", "NS", "ON", "QC", "SK", "YT"}

def key(name):
    s = (name or "").upper()
    s = re.sub(r"\bSAINT\b", "ST", s); s = re.sub(r"\bFORT\b", "FT", s); s = re.sub(r"\bMOUNT\b", "MT", s)
    return re.sub(r"[^A-Z0-9]", "", s)

def strip_type(name):
    """'Round Lake village' -> 'Round Lake', 'Midland CDP' -> 'Midland', 'Nashville-Davidson metropolitan government (balance)' -> 'Nashville-Davidson'"""
    toks = name.split()
    while len(toks) > 1 and (toks[-1] == "CDP" or toks[-1][:1].islower() or toks[-1].startswith("(")):
        toks.pop()
    return " ".join(toks)

def load(kind):
    r = requests.get(GAZ + FILES[kind], timeout=120, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    g = pd.read_csv(z.open(z.namelist()[0]), sep="\t", dtype=str, encoding="latin-1")
    g.columns = [c.strip() for c in g.columns]
    g["k"] = g["NAME"].map(strip_type).map(key)
    g["lat"] = g["INTPTLAT"].astype(float); g["lon"] = g["INTPTLONG"].astype(float)
    g["land_sqmi"] = g["ALAND_SQMI"].astype(float)
    g["incorporated"] = ~g["NAME"].str.endswith(" CDP")
    # if two places share a name in a state, prefer incorporated, then larger
    g = g.sort_values(["incorporated", "land_sqmi"], ascending=False).drop_duplicates(["USPS", "k"])
    return g.set_index(["USPS", "k"])

print("downloading Census Gazetteer files...")
place, cousub = load("place"), load("cousub")

df = pd.read_csv(CSV)
df = df[~df.State.isin(CA)].drop_duplicates("OPIS Truckstop ID").copy()
for c in ["City", "State", "Address"]:
    df[c] = df[c].astype(str).str.strip()
df["k"] = df["City"].map(key)

towns = df[["State", "City", "k"]].drop_duplicates(["State", "k"])
rows = []
for _, t in towns.iterrows():
    hit, src = None, "not found"
    for tbl, name in ((place, "place"), (cousub, "township")):
        if (t.State, t.k) in tbl.index:
            hit, src = tbl.loc[(t.State, t.k)], name; break
    rows.append({"state": t.State, "city": t.City, "k": t.k, "source": src,
                 "lat": hit["lat"] if hit is not None else None, "lon": hit["lon"] if hit is not None else None,
                 "land_sqmi": hit["land_sqmi"] if hit is not None else None})
T = pd.DataFrame(rows)
T["radius_mi"] = (T["land_sqmi"] / math.pi) ** 0.5
T.drop(columns="k").to_csv(OUT, index=False)

found = T[T.source != "not found"]
print(f"\nUnique station towns: {len(T)}")
print(f"  found: {len(found)} ({len(found) / len(T):.0%})   [places: {(T.source == 'place').sum()}, townships: {(T.source == 'township').sum()}]")
print(f"  not found: {(T.source == 'not found').sum()}  e.g. {', '.join((T[T.source == 'not found'].city + ' ' + T[T.source == 'not found'].state).head(12))}")

def describe(r, label):
    q = r.quantile([.25, .5, .75, .9]).round(1)
    print(f"{label:45s} median {q[.5]} mi | 25%: {q[.25]} | 75%: {q[.75]} | 90%: {q[.9]}")

print("\nTown size as a radius (how far the edge is from the center):")
describe(found["radius_mi"], "  all station towns")

# per-station view, focusing on the relevant stations we could NOT place exactly
df = df.merge(T[["state", "k", "radius_mi", "source"]], left_on=["State", "k"], right_on=["state", "k"], how="left")
a = df["Address"].str.upper()
inter = a.str.contains(r"\bI-\d+"); us = a.str.contains(r"\bUS-\d+"); has_exit = (inter | us) & a.str.contains(r"EXIT\s*\d+")
df["relevant"] = inter | us
try:
    exact_ids = set(pd.read_csv(ATP)["id"])
except FileNotFoundError:
    exact_ids = set()
df["exact"] = df["OPIS Truckstop ID"].isin(exact_ids)
left = df[df.relevant & ~df.exact & ~has_exit]
describe(left["radius_mi"].dropna(), f"  relevant stations left at town level ({len(left)})")
print(f"    of those, town not found in Census: {left['radius_mi'].isna().sum()}")
print(f"\nSaved {OUT}")
