"""
Rebuild data/stations_geocoded.csv from the committed intermediate files, in seconds.

Usage (from the repo root):
  python research/geocoding/run_all.py              # each step skips if its output already exists
  python research/geocoding/run_all.py --refresh    # rebuild everything; needs the big downloads

Steps 1-3 skip when their output is in research/geocoding/data/. Step 4 always places the stations
(using data/exits.csv unless --refresh), and step 5 writes the spreadsheets for checking by hand.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = ["1_atp_fetch_older.py", "2_atp_match.py", "3_towns.py", "4_exits.py", "5_make_check_files.py"]

for step in STEPS:
    print(f"\n=== {step}")
    subprocess.run([sys.executable, str(HERE / step), *sys.argv[1:]], check=True, cwd=HERE)
