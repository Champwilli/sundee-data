import os
import requests
from datetime import datetime, timezone
from supabase import create_client

SUPABASE_URL = "https://wrixhnypdeavgjmdwsik.supabase.co"
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def fetch_power_prices():
    url = "https://api.energidataservice.dk/dataset/Elspotprices?limit=5&filter=%7B%22PriceArea%22:%5B%22NO1%22,%22NO2%22,%22NO3%22,%22NO4%22,%22NO5%22%5D%7D&sort=HourDK%20DESC"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    records = r.json().get("records", [])
    rows = []
    for rec in records:
        rows.append({
            "zone": rec["PriceArea"],
            "price_ore_kwh": round(rec["SpotPriceDKK"] / 10, 2) if rec.get("SpotPriceDKK") else None,
            "price_eur_mwh": round(rec["SpotPriceEUR"], 2) if rec.get("SpotPriceEUR") else None,
            "hour": rec["HourDK"],
            "fetched_at": datetime.now(timezone.utc).isoformat()
        })
    return rows

def fetch_reservoir_levels():
    url = "https://biapi.nve.no/magasinstatistikk/api/Magasinstatistikk/HentOffentligData"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list) and len(data) > 0:
        latest = data[0]
        return [{
            "region": "Norge",
            "fill_pct": latest.get("fyllingsgrad"),
            "median_pct": latest.get("medianFyllingsgrad"),
            "week": latest.get("uke"),
            "fetched_at": datetime.now(timezone.utc).isoformat()
        }]
    return []

def upsert(table, rows):
    if rows:
        supabase.table(table).upsert(rows).execute()
        print(f"Upserted {len(rows)} rows into {table}")

if __name__ == "__main__":
    print("Fetching power prices...")
    prices = fetch_power_prices()
    upsert("power_prices", prices)

    print("Fetching reservoir levels...")
    levels = fetch_reservoir_levels()
    upsert("reservoir_levels", levels)

    print("Done.")
