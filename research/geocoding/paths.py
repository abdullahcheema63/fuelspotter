"""Where the geocoding steps read and write. Shared by every step."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # research/geocoding
REPO = HERE.parents[1]                          # repo root
DATA = HERE / "data"                            # small, committed files each step produces
DL = HERE / "downloads"                         # big downloads, only needed to rebuild; git-ignored
CSV = REPO / "data" / "fuel-prices-for-be-assessment.csv"   # the price list as provided
FINAL = REPO / "data" / "stations_geocoded.csv"              # what load_stations reads


def refresh_requested():
    """--refresh: rebuild this step's output even if it exists (may need the big downloads)."""
    return "--refresh" in sys.argv
