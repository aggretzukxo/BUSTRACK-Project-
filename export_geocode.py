#!/usr/bin/env python3
"""Save the stop coordinates your local server already geocoded into geocode_seed.json.

Run once on your own machine AFTER the app has started successfully (so bustrack.db has the
stops), then commit geocode_seed.json. A hosted copy then starts instantly and never has to
call the Google Geocoding API.

    python export_geocode.py
"""
import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent
DB = BASE / "bustrack.db"
OUT = BASE / "geocode_seed.json"

if not DB.exists():
    sys.exit("bustrack.db not found. Start the server once (python -m uvicorn main:app) and let it finish geocoding.")

con = sqlite3.connect(DB)
rows = con.execute("SELECT name, lat, lng FROM geocode_cache WHERE status = 'ok' AND lat IS NOT NULL").fetchall()
if not rows:
    sys.exit("No geocoded stops in the database yet - check the server log for geocoding errors first.")

OUT.write_text(json.dumps({name: [lat, lng] for name, lat, lng in rows}, indent=0), encoding="utf-8")
print(f"Saved {len(rows)} stops to {OUT.name}. Commit this file with the rest of the project.")
