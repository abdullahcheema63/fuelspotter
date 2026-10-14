"""
Pull just a few spider files out of older ATP builds — without downloading the 2.6 GB zips.
A zip's table of contents sits at the end of the file, so Python's zipfile can read single
members if we give it a "file" that fetches byte ranges over HTTP on demand.

Usage (from the repo root):  python research/geocoding/1_atp_fetch_older.py [--refresh]
Writes: research/geocoding/data/atp_extra/<spider>.geojson, and prints how many US records have
coordinates. 2_atp_match.py picks these files up automatically (they override the ones in output.zip).
Skipped if the files already exist, unless --refresh is given.
"""
import io, json, os, sys, time, zipfile
import requests
from pathlib import Path
from paths import CSV, DATA, DL, FINAL, refresh_requested

BASE = "https://alltheplaces-data.openaddresses.io/runs"
WANT = {
    "caseys_general_store_us": "2026-09-19-13-32-18",
    "maverik":                 "2026-09-19-13-32-18",
    "quiktrip_us":             "2026-07-04-13-32-25",   # last build before coords went missing — unverified
}
args = [a for a in sys.argv[1:] if not a.startswith("--")]
if len(args) == 2:
    WANT = {args[0]: args[1]}

EXTRA = DATA / "atp_extra"
if not refresh_requested() and all((EXTRA / f"{sp}.geojson").exists() for sp in WANT):
    print(f"step 1: {EXTRA} already has {', '.join(WANT)} -- skipped (use --refresh to fetch again)")
    sys.exit(0)

S = requests.Session()
S.headers["User-Agent"] = "spotter-fuel-route-assessment/0.1 (+https://abdullahcheema.dev)"


class HttpRangeFile(io.RawIOBase):
    """Read-only, seekable file over HTTP Range requests (absolute ranges only), with retries."""

    def __init__(self, url):
        self.url, self.pos, self.fetched = url, 0, 0
        h = S.head(url, timeout=30, allow_redirects=True)
        h.raise_for_status()
        self.size = int(h.headers["Content-Length"])

    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos

    def seek(self, offset, whence=0):
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        if n == 0 or self.pos >= self.size:
            return b""
        end = min(self.pos + n, self.size) - 1
        for attempt in range(5):
            # stream=True: look at the status before reading the body. The server sometimes ignores
            # the Range header and answers 200 with the whole 2.7 GB file; that body must not be read.
            r = S.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"}, timeout=60, stream=True)
            if r.status_code == 206:
                data = r.content
                self.pos += len(data); self.fetched += len(data)
                return data
            r.close()
            print(f"    range {self.pos}-{end}: HTTP {r.status_code}, retry {attempt + 1}/5")
            time.sleep(2 * (attempt + 1))
        raise IOError(f"server would not return a partial response for {self.url}")

    def readinto(self, b):
        data = self.read(len(b)); b[:len(data)] = data
        return len(data)


os.makedirs(EXTRA, exist_ok=True)
by_run = {}
for sp, run in WANT.items():
    by_run.setdefault(run, []).append(sp)

for run, spiders in by_run.items():
    url = f"{BASE}/{run}/output.zip"
    print(f"\n{run}: reading zip index remotely...")
    f = HttpRangeFile(url)
    z = zipfile.ZipFile(io.BufferedReader(f, buffer_size=1 << 20))
    names = {n.split("/")[-1].removesuffix(".geojson"): n for n in z.namelist() if n.endswith(".geojson")}
    for sp in spiders:
        if sp not in names:
            print(f"  {sp}: NOT in this build"); continue
        data = z.read(names[sp])
        out = EXTRA / f"{sp}.geojson"
        open(out, "wb").write(data)
        feats = json.loads(data).get("features", []) if data.strip() else []
        us = [x for x in feats if (x.get("properties", {}).get("addr:country") or "US") == "US"]
        with_geo = sum(1 for x in us if x.get("geometry"))
        print(f"  {sp}: {len(data) / 1e6:.1f} MB, {len(us)} US records, {with_geo} with coordinates -> {out}")
        if us:
            p = us[0]["properties"]
            print(f"     sample: ref={p.get('ref')!r} name={p.get('name')!r} city={p.get('addr:city')!r} "
                  f"state={p.get('addr:state')!r} geom={(us[0].get('geometry') or {}).get('coordinates')}")
    print(f"  (downloaded {f.fetched / 1e6:.1f} MB of {f.size / 1e9:.1f} GB)")
