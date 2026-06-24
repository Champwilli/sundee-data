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
        url = "https://biapi.nve.no/magasinstatistikk/api/MagasinStatistikk?IncludeProperties=all"
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



# ============================================================
# NYE FUNKSJONER FOR SUNDEE V2
# ============================================================

def fetch_reservoir_zones():
    """Henter magasinfylling per NO-sone fra NVE og skriver til reservoir_zones."""
        url = "https://biapi.nve.no/magasinstatistikk/api/MagasinStatistikk?IncludeProperties=all"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    zone_map = {1: "NO1", 2: "NO2", 3: "NO3", 4: "NO4", 5: "NO5"}
    rows = []
    for rec in data:
        if rec.get("omrType") != "EL":
            continue
        omrnr = rec.get("omrnr")
        zone = zone_map.get(omrnr)
        if not zone:
            continue
        rows.append({
            "zone": zone,
            "week_number": rec.get("uke"),
            "year": rec.get("aar"),
            "fill_pct": rec.get("fyllingsgrad"),
            "median_pct": rec.get("medianFyllingsgrad"),
            "min_pct": rec.get("minFyllingsgrad"),
            "max_pct": rec.get("maxFyllingsgrad"),
            "source": "NVE",
            "fetched_at": now_utc()
        })
    return rows


def fetch_spot_prices_new():
    """Henter spotpriser og skriver til spot_prices-tabellen med timestamp."""
    url = (
        "https://api.energidataservice.dk/dataset/Elspotprices"
        "?limit=200"
        "&filter=%7B%22PriceArea%22:%5B%22NO1%22,%22NO2%22,%22NO3%22,%22NO4%22,%22NO5%22%5D%7D"
        "&sort=HourDK%20DESC"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    records = r.json().get("records", [])
    rows = []
    for rec in records:
        hour_str = rec.get("HourUTC")
        if not hour_str:
            continue
        rows.append({
            "timestamp_utc": hour_str,
            "zone": rec.get("PriceArea"),
            "price_eur": round(rec.get("SpotPriceEUR", 0), 4) if rec.get("SpotPriceEUR") is not None else None,
            "price_nok": round(rec.get("SpotPriceDKK", 0) / 10, 4) if rec.get("SpotPriceDKK") is not None else None,
            "currency": "EUR",
            "source": "Nord Pool",
            "fetched_at": now_utc()
        })
    return rows


def fetch_news_items():
    """Henter nyhetsoverskrifter fra Nord Pool og Statnett RSS."""
    sources = [
        {
            "name": "Statnett",
            "url": "https://www.statnett.no/rss/",
            "category": "grid"
        },
        {
            "name": "NVE",
            "url": "https://www.nve.no/rss/",
            "category": "hydro"
        },
    ]
    import xml.etree.ElementTree as ET
    rows = []
    for src in sources:
        try:
            r = requests.get(src["url"], timeout=15)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            ns = ""
            for item in root.iter("item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                pub_date = item.findtext("pubDate", "").strip()
                description = item.findtext("description", "").strip()[:500]
                if not title or not link:
                    continue
                try:
                    from email.utils import parsedate_to_datetime
                    published = parsedate_to_datetime(pub_date).isoformat() if pub_date else now_utc()
                except Exception:
                    published = now_utc()
                rows.append({
                    "title": title,
                    "summary": description,
                    "source": src["name"],
                    "source_url": link,
                    "category": src["category"],
                    "published_at": published,
                    "fetched_at": now_utc(),
                    "is_active": True
                })
        except Exception as e:
            print(f"Feil ved henting fra {src['name']}: {e}")
    return rows



if __name__ == "__main__":
    try:
        print("Fetching power prices...")
        prices = fetch_power_prices()
        upsert_rows("power_prices", prices, "zone,hour")
    except Exception as e:
        print(f"Error fetching power prices: {e}")

    try:
        print("Fetching reservoir levels...")
        levels = fetch_reservoir_levels()
        upsert_rows("reservoir_levels", levels, "region,year,week")
    except Exception as e:
        print(f"Error fetching reservoir levels: {e}")

    try:
        print("Fetching reservoir zones per NO1-NO5...")
        zones = fetch_reservoir_zones()
        upsert_rows("reservoir_zones", zones, "zone,week_number,year")
    except Exception as e:
        print(f"Error fetching reservoir zones: {e}")

    try:
        print("Fetching spot prices to new table...")
        spot = fetch_spot_prices_new()
        upsert_rows("spot_prices", spot, "timestamp_utc,zone")
    except Exception as e:
        print(f"Error fetching spot prices: {e}")

    try:
        print("Fetching news items...")
        news = fetch_news_items()
                # Dedup: fetch existing source_urls before inserting
            existing = supabase.table("news_items").select("source_url").execute()
            existing_urls = {r["source_url"] for r in (existing.data or [])}
            new_items = [n for n in news if n.get("source_url") not in existing_urls]
            if new_items:
                supabase.table("news_items").insert(new_items).execute()
                print(f"Inserted {len(new_items)} new news items (skipped {len(news)-len(new_items)} duplicates)")
            else:
                print("No new news items to insert")
    except Exception as e:
        print(f"Error fetching news items: {e}")

    print("Done.")
