import os
import requests
from datetime import datetime, timezone
from supabase import create_client

SUPABASE_URL = "https://wrixhnypdeavgjmdwsik.supabase.co"
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PRICE_AREAS = ["NO1", "NO2", "NO3", "NO4", "NO5"]

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def fetch_power_prices():
    url = (
        "https://api.energidataservice.dk/dataset/Elspotprices"
        "?limit=200"
        "&filter=%7B%22PriceArea%22:%5B%22NO1%22,%22NO2%22,%22NO3%22,%22NO4%22,%22NO5%22%5D%7D"
        "&sort=HourDK%20DESC"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    records = r.json().get("records", [])

    latest_by_zone = {}
    for rec in records:
        zone = rec.get("PriceArea")
        hour = rec.get("HourDK")
        if zone not in PRICE_AREAS or not hour:
            continue
        if zone not in latest_by_zone:
            latest_by_zone[zone] = {
                "zone": zone,
                "price_ore_kwh": round(rec["SpotPriceDKK"] / 10, 2) if rec.get("SpotPriceDKK") is not None else None,
                "price_eur_mwh": round(rec["SpotPriceEUR"], 2) if rec.get("SpotPriceEUR") is not None else None,
                "hour": hour,
                "fetched_at": now_utc()
            }

    return list(latest_by_zone.values())

def fetch_reservoir_levels():
    url = "https://nvebiapi.nve.no/api/Magasinstatistikk/HentOffentligData"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    zone_map = {
        1: "NO1",
        2: "NO2",
        3: "NO3",
        4: "NO4",
        5: "NO5",
    }

    rows = []
    for rec in data:
        if rec.get("omrType") != "EL":
            continue

        omrnr = rec.get("omrnr")
        region = zone_map.get(omrnr)
        if not region:
            continue

        rows.append({
            "region": region,
            "year": rec.get("aar"),
            "week": rec.get("uke"),
            "fill_pct": rec.get("fyllingsgrad"),
            "fetched_at": now_utc()
        })

    latest_rows = {}
    for row in sorted(rows, key=lambda x: (x["year"] or 0, x["week"] or 0), reverse=True):
        if row["region"] not in latest_rows:
            latest_rows[row["region"]] = row

    return list(latest_rows.values())

def upsert_rows(table, rows, conflict_cols):
    if not rows:
        print(f"No rows for {table}")
        return

    response = (
        supabase
        .table(table)
        .upsert(rows, on_conflict=conflict_cols)
        .execute()
    )
    print(f"Upserted {len(rows)} rows into {table}")
    return response

if __name__ == "__main__":
    print("Fetching power prices...")
    prices = fetch_power_prices()
    upsert_rows("power_prices", prices, "zone,hour")

    print("Fetching reservoir levels...")
    levels = fetch_reservoir_levels()
    upsert_rows("reservoir_levels", levels, "region,year,week")

    print("Done.")
