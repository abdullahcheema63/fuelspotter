"""
Match CSV stations to All The Places (chains' own store locations).

Pass 1  store #:     CSV store number == ATP ref (or "#nnn" in ATP name), same state.   (same as v1)
Pass 2  brand+city:  for stations still unmatched (incl. ones with no "#"), take the ATP store of the
                     same chain in the same state + city — ONLY if there is exactly one such store
                     and no other CSV station already claimed it. Several candidates -> 'ambiguous', skipped.

Also reports, per ATP file, how many records have no coordinates (explains QuikTrip).

Usage (from the repo root):  python research/geocoding/2_atp_match.py [--refresh]
Output: research/geocoding/data/atp_matches.csv  (method = store# | brand+city)
Skipped if atp_matches.csv already exists, unless --refresh is given. Rebuilding it needs the full
All The Places build (output.zip, ~2.7 GB) in research/geocoding/downloads/ -- see downloads/README.md.
"""
import json, os, re, sys, zipfile
from collections import defaultdict
import pandas as pd
from pathlib import Path
from paths import CSV, DATA, DL, FINAL, refresh_requested

ZIP = DL / "output.zip"
OUT = DATA / "atp_matches.csv"
if OUT.exists() and not refresh_requested():
    print(f"step 2: {OUT.name} exists -- skipped (use --refresh to rebuild from {ZIP.name})")
    sys.exit(0)
if not ZIP.exists():
    sys.exit(f"step 2: needs {ZIP} (the All The Places build, ~2.7 GB). See downloads/README.md.")

CHAINS = [  # (label, CSV name prefix regex, ATP files)
    ("Love's",           r"^LOVE'?S",              ["loves_us"]),
    ("Pilot / Flying J", r"^PILOT|^FLYING J",      ["pilot_flying_j"]),
    ("TA / Petro",       r"^TA\b|^PETRO\b|^PETRO STOPPING|^TRAVELCENTERS", ["travelcenters_of_america_us"]),
    ("Speedway",         r"^SPEEDWAY",             ["speedway_us"]),
    ("Circle K",         r"^CIRCLE K",             ["circle_k"]),
    ("Kwik Trip / Star", r"^KWIK TRIP|^KWIK STAR", ["kwik_trip"]),
    ("QuikTrip",         r"^QUIK ?TRIP",           ["quiktrip_us"]),
    ("7-Eleven",         r"^7-?ELEVEN",            ["seven_eleven_ca_us"]),
    ("Sheetz",           r"^SHEETZ",               ["sheetz"]),
    ("Allsup's",         r"^ALLSUP",               ["allsups_yesway_us"]),
    ("Road Ranger",      r"^ROAD RANGER",          ["road_ranger_us"]),
    ("Kwik Fill",        r"^KWIK FILL",            ["kwik_fill_us"]),
    ("Casey's",          r"^CASEY",                ["caseys_general_store_us"]),
    ("Maverik",          r"^MAVERIK",              ["maverik"]),
]

def num(s):
    m = re.search(r"\d+", str(s or ""))
    return str(int(m.group())) if m else None

def city_key(c):
    c = (c or "").upper()
    c = re.sub(r"\bSAINT\b", "ST", c)
    c = re.sub(r"\bFORT\b", "FT", c)
    c = re.sub(r"\bMOUNT\b", "MT", c)
    return re.sub(r"[^A-Z0-9]", "", c)

z = zipfile.ZipFile(ZIP)
members = {n.split("/")[-1].removesuffix(".geojson"): n for n in z.namelist() if n.endswith(".geojson")}

def load_atp(file_key):
    """Returns (records, n_us_total, n_us_no_geometry) or None if file missing.
    A file in atp_extra/ (pulled from an older build by atp_fetch_older.py) overrides output.zip."""
    extra = DATA / "atp_extra" / f"{file_key}.geojson"
    if os.path.exists(extra):
        raw = open(extra, "rb").read()
    else:
        n = members.get(file_key)
        if not n:
            return None
        raw = z.read(n)
    if not raw.strip():
        return [], 0, 0
    recs, us_total, no_geom = [], 0, 0
    for f in json.loads(raw).get("features", []):
        p = f.get("properties", {})
        if (p.get("addr:country") or "US") != "US":
            continue
        us_total += 1
        if not f.get("geometry"):
            no_geom += 1
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        nm = p.get("name") or ""
        m = re.search(r"#\s*(\d+)", nm)
        recs.append({
            "uid": f"{file_key}:{p.get('ref')}:{lat}:{lon}",
            "nums": {x for x in (num(p.get("ref")), num(m.group(1)) if m else None) if x},
            "state": (p.get("addr:state") or "").upper(), "city": city_key(p.get("addr:city")),
            "lat": lat, "lon": lon, "name": nm or p.get("brand"), "addr": p.get("addr:street_address") or p.get("addr:full"),
        })
    return recs, us_total, no_geom

# ---- CSV
CA = {"AB", "BC", "MB", "NB", "NS", "ON", "QC", "SK", "YT"}
df = pd.read_csv(CSV)
df = df[~df.State.isin(CA)].drop_duplicates("OPIS Truckstop ID")
for c in ["Truckstop Name", "City", "State"]:
    df[c] = df[c].astype(str).str.strip()
df["store_no"] = df["Truckstop Name"].map(lambda s: num(re.search(r"#\s*(\d+)", s).group(1)) if re.search(r"#\s*(\d+)", s) else None)
df["city_key"] = df["City"].map(city_key)

hdr = f"{'chain':18s} {'CSV':>5s} {'ATP US':>7s} {'no coords':>9s} | {'store#':>6s} {'+brand+city':>11s} {'ambiguous':>9s} | {'total':>5s} {'%':>5s}"
print(hdr); print("-" * len(hdr))
rows, T = [], defaultdict(int)
for label, pat, files in CHAINS:
    sub = df[df["Truckstop Name"].str.upper().str.contains(pat, regex=True)]
    if sub.empty:
        continue
    recs, us_total, no_geom, note = [], 0, 0, ""
    for fk in files:
        res = load_atp(fk)
        if res is None:
            note = "(file not in zip)"; continue
        r_, t_, g_ = res
        recs += r_; us_total += t_; no_geom += g_
        if t_ == 0:
            note = "(file empty)"
        elif os.path.exists(DATA / "atp_extra" / f"{fk}.geojson"):
            note = "(from older build)"
    by_num = defaultdict(list)
    by_city = defaultdict(list)
    for rec in recs:
        for n_ in rec["nums"]:
            by_num[n_].append(rec)
        by_city[(rec["state"], rec["city"])].append(rec)

    claimed, matched_ids = set(), set()
    p1 = p2 = amb = 0
    # pass 1: store number + same state
    for _, r in sub[sub["store_no"].notna()].iterrows():
        hits = [h for h in by_num.get(r["store_no"], []) if h["state"] == r["State"].upper()]
        if hits:
            h = hits[0]; p1 += 1
            claimed.add(h["uid"]); matched_ids.add(r["OPIS Truckstop ID"])
            rows.append({"id": r["OPIS Truckstop ID"], "csv_name": r["Truckstop Name"], "csv_city": r["City"], "csv_state": r["State"],
                         "method": "store#", "atp_name": h["name"], "atp_addr": h["addr"], "lat": h["lat"], "lon": h["lon"]})
    # pass 2: brand + city, unique and unclaimed
    for _, r in sub[~sub["OPIS Truckstop ID"].isin(matched_ids)].iterrows():
        cands = [h for h in by_city.get((r["State"].upper(), r["city_key"]), []) if h["uid"] not in claimed]
        if len(cands) == 1:
            h = cands[0]; p2 += 1
            claimed.add(h["uid"])
            rows.append({"id": r["OPIS Truckstop ID"], "csv_name": r["Truckstop Name"], "csv_city": r["City"], "csv_state": r["State"],
                         "method": "brand+city", "atp_name": h["name"], "atp_addr": h["addr"], "lat": h["lat"], "lon": h["lon"]})
        elif len(cands) > 1:
            amb += 1
    tot = p1 + p2
    print(f"{label:18s} {len(sub):5d} {us_total:7d} {no_geom:9d} | {p1:6d} {p2:11d} {amb:9d} | {tot:5d} {tot / len(sub):5.0%}  {note}")
    for k, v in [("csv", len(sub)), ("p1", p1), ("p2", p2), ("amb", amb)]:
        T[k] += v

print("-" * len(hdr))
tt = T["p1"] + T["p2"]
print(f"{'TOTAL':18s} {T['csv']:5d} {'':>7s} {'':>9s} | {T['p1']:6d} {T['p2']:11d} {T['amb']:9d} | {tt:5d} {tt / T['csv']:5.0%}")
print(f"\nAll US stations in CSV: {len(df)}  ->  placed exactly via ATP: {tt} ({tt / len(df):.0%})"
      f"   [store#: {T['p1']}, brand+city: {T['p2']}]")
pd.DataFrame(rows).to_csv(OUT, index=False)
print(f"Saved {OUT}")
