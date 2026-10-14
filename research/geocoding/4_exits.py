"""
Highway-exit lookup from OpenStreetMap, fully offline.

RUN (from the repo root):
  python research/geocoding/4_exits.py [--refresh]
  (needs research/geocoding/data/towns.csv and atp_matches.csv)

What it does:
  1. the list of US exits: read from data/exits.csv if it exists. Otherwise (or with --refresh) it is
     rebuilt from OpenStreetMap -- every exit point with its number and the highways it belongs to
     (I 40, US 287 ...). That needs the one-time setup below.
  2. for each station NOT already placed exactly, whose address says e.g. "I-40, EXIT 76":
       finds exit 76 on I-40 nearest to the station's town (within 30 miles)
Writes: research/geocoding/data/exits.csv (only when rebuilt), data/stations_geocoded.csv (final,
one row per station, at the repo root -- the file load_stations reads).

ONE-TIME SETUP, only to rebuild exits.csv (macOS; run from research/geocoding):
  brew install osmium-tool
  pip install osmium
  curl -L -C - -o downloads/us-latest.osm.pbf https://download.geofabrik.de/north-america/us-latest.osm.pbf   # 11.4 GB
  # shrink it to just exits + the highways they sit on (a few minutes, output ~30 MB):
  osmium tags-filter downloads/us-latest.osm.pbf n/highway=motorway_junction w/highway=motorway,trunk -R -o downloads/us-exits.osm.pbf
"""
import math, re, sys
from collections import defaultdict
import pandas as pd
from pathlib import Path
from paths import CSV, DATA, DL, FINAL, refresh_requested

PBF = DL / "us-exits.osm.pbf"
EXITS = DATA / "exits.csv"
MAX_MI = 30
CA = {"AB", "BC", "MB", "NB", "NS", "ON", "QC", "SK", "YT"}

# ---------- 1. exits: from data/exits.csv, or rebuilt from OSM
def exits_from_osm():
    import osmium     # only needed to rebuild exits.csv

    class Junctions(osmium.SimpleHandler):
        def __init__(self):
            super().__init__(); self.j = {}
        def node(self, n):
            if n.tags.get("highway") == "motorway_junction" and n.tags.get("ref"):
                self.j[n.id] = [n.location.lat, n.location.lon, n.tags.get("ref"), set()]

    class Ways(osmium.SimpleHandler):
        def __init__(self, j):
            super().__init__(); self.j = j
        def way(self, w):
            ref = w.tags.get("ref")
            if not ref:
                return
            hw = {f"{a} {b}" for a, b in re.findall(r"\b(I|US)[ -]?(\d+)", ref.upper())}
            if not hw:
                return
            for nd in w.nodes:
                if nd.ref in self.j:
                    self.j[nd.ref][3] |= hw

    if not PBF.exists():
        sys.exit(f"step 4: needs {PBF} to rebuild exits.csv -- see downloads/README.md.")
    print("reading exits from", PBF, "...")
    jh = Junctions(); jh.apply_file(str(PBF))
    Ways(jh.j).apply_file(str(PBF))
    ex = pd.DataFrame([{"node": k, "lat": v[0], "lon": v[1], "exit_ref": v[2], "highways": "|".join(sorted(v[3]))}
                       for k, v in jh.j.items()])
    ex.to_csv(EXITS, index=False)
    return ex

if EXITS.exists() and not refresh_requested():
    print(f"step 4: exits from {EXITS.name} (use --refresh to rebuild it from OpenStreetMap)")
    ex = pd.read_csv(EXITS, dtype={"exit_ref": str, "highways": str}, keep_default_na=False)
else:
    ex = exits_from_osm()
print(f"  {len(ex)} exits with numbers, {(ex.highways != '').sum()} linked to an I-/US- highway")

def exit_keys(r):
    """'13A;13B' -> {'13A','13B'}"""
    return {re.sub(r"\s", "", x).upper() for x in str(r).split(";") if x.strip()}

by_exit = defaultdict(list)       # 'I 40' + '76'  -> [(lat, lon)]
by_exit_num = defaultdict(list)   # 'I 40' + 76    -> [(lat, lon, exact_ref)]  (number only, ignores letter)
for _, e in ex[ex.highways != ""].iterrows():
    for hw in e.highways.split("|"):
        for k in exit_keys(e.exit_ref):
            by_exit[(hw, k)].append((e.lat, e.lon))
            m = re.match(r"\d+", k)
            if m:
                by_exit_num[(hw, int(m.group()))].append((e.lat, e.lon))

# ---------- 2. match stations
def miles(la1, lo1, la2, lo2):
    la1, lo1, la2, lo2 = map(math.radians, [la1, lo1, la2, lo2])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))

def parse(addr):
    """'I-20/59, EXIT 100' -> (['I 20','I 59'], '100');  'I-70 & SR-79, EXIT 304' -> (['I 70'], '304')"""
    a = addr.upper()
    x = re.search(r"EXIT\s*(\d+\s*[A-Z]?)\b", a)
    if not x:
        return [], None
    head = a[:x.start()]
    hws = []
    for kind, nums in re.findall(r"\b(I|US)-(\d+(?:/\d+)*)", head):
        hws += [f"{kind} {n}" for n in nums.split("/")]
    return hws, x.group(1).replace(" ", "")

def key(name):
    s = (name or "").upper()
    s = re.sub(r"\bSAINT\b", "ST", s); s = re.sub(r"\bFORT\b", "FT", s); s = re.sub(r"\bMOUNT\b", "MT", s)
    return re.sub(r"[^A-Z0-9]", "", s)

df = pd.read_csv(CSV)
df = df[~df.State.isin(CA)].drop_duplicates("OPIS Truckstop ID").copy()
for c in ["Truckstop Name", "Address", "City", "State"]:
    df[c] = df[c].astype(str).str.strip()
atp = pd.read_csv(DATA / "atp_matches.csv").set_index("id")
T = pd.read_csv(DATA / "towns.csv"); T["k"] = T["city"].map(key)
town = {(r.state, r.k): (r.lat, r.lon, r.radius_mi) for r in T.itertuples() if not pd.isna(r.lat)}

raw = pd.read_csv(CSV)
price = raw.groupby("OPIS Truckstop ID")["Retail Price"].mean()   # duplicate rows -> average price

def find_exit(s, tp):
    hws, ex_ref = parse(s.Address)
    if not hws or not ex_ref:
        return None, "no exit in address"
    cands = []
    for hw in hws:
        pts = by_exit.get((hw, ex_ref)) or by_exit_num.get((hw, int(re.match(r"\d+", ex_ref).group())), [])
        cands += [(miles(tp[0], tp[1], la, lo), la, lo, hw, ex_ref) for la, lo in pts]
    cands = [c for c in cands if c[0] <= MAX_MI]
    return (min(cands), "ok") if cands else (None, "exit not found near town")

CONFIRM_MI = 2.0
rows, stat = [], defaultdict(int)
for _, s in df.iterrows():
    sid = s["OPIS Truckstop ID"]
    tp = town.get((s.State, key(s.City)))
    a = s.Address.upper()
    relevant = bool(re.search(r"\b(I|US)-\d+", a))
    ex, why = find_exit(s, tp) if tp else (None, "town not found")
    rec = {"id": sid, "name": s["Truckstop Name"], "address": s.Address, "city": s.City, "state": s.State,
           "price": round(price[sid], 4), "relevant": relevant, "town_radius_mi": tp[2] if tp else None}

    if sid in atp.index and atp.loc[sid, "method"] == "store#":
        rec.update(lat=atp.loc[sid, "lat"], lon=atp.loc[sid, "lon"], precision="exact", method="ATP store#")
    elif sid in atp.index:  # brand+city: verify against the exit named in the address, if any
        alat, alon = atp.loc[sid, "lat"], atp.loc[sid, "lon"]
        if ex is None:
            rec.update(lat=alat, lon=alon, precision="exact", method="ATP brand+city (no exit to verify)")
            stat["brand+city: nothing to verify against"] += 1
        elif miles(alat, alon, ex[1], ex[2]) <= CONFIRM_MI:
            rec.update(lat=alat, lon=alon, precision="exact", method="ATP brand+city (confirmed by exit)")
            stat["brand+city: confirmed by exit"] += 1
        else:
            rec.update(lat=ex[1], lon=ex[2], precision="exit", method=f"exit {ex[3]} #{ex[4]} (replaced brand+city)")
            stat["brand+city: far from exit -> replaced by exit"] += 1
    elif ex is not None:
        rec.update(lat=ex[1], lon=ex[2], precision="exit", method=f"exit {ex[3]} #{ex[4]}")
    elif tp:
        rec.update(lat=tp[0], lon=tp[1], precision="town", method=f"town center ({why})")
    else:
        rec.update(lat=None, lon=None, precision="none", method="town not found")
    rows.append(rec)

out = pd.DataFrame(rows)
out.to_csv(FINAL, index=False)
print(f"Saved {FINAL}")

print("\nBrand+city check (ATP match vs exit named in the address):")
for k in ["brand+city: confirmed by exit", "brand+city: far from exit -> replaced by exit", "brand+city: nothing to verify against"]:
    print(f"  {k:48s} {stat[k]}")

def table(d, label):
    vc = d["precision"].value_counts()
    print(f"\n{label}: {len(d)}")
    for p_ in ["exact", "exit", "town", "none"]:
        n = vc.get(p_, 0)
        print(f"  {p_:6s} {n:5d}  ({n / len(d):.0%})")
table(out, "ALL US stations")
table(out[out.relevant], "RELEVANT stations (interstate / US highway)")
ex_rows = out[out.precision == "exit"]
print("\nSaved stations_geocoded.csv  (one row per station: price, lat, lon, precision, method)")
