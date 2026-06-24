import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from supabase import create_client

SUPABASE_URL = "https://wrixhnypdeavgjmdwsik.supabase.co"
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PRICE_AREAS = ["NO1", "NO2", "NO3", "NO4", "NO5"]


def now_utc():
    return datetime.now(timezone.utc).isoformat()


# ── SPOT PRICES ──────────────────────────────────────────────────────────────

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


def upsert_power_prices(rows):
    if not rows:
        print("No price rows to upsert.")
        return
    supabase.table("spot_prices").upsert(rows, on_conflict="zone").execute()
    print(f"Upserted {len(rows)} price rows.")


# ── RESERVOIR LEVELS ─────────────────────────────────────────────────────────

def fetch_reservoir_levels():
    url = "https://biapi.nve.no/magasinstatistikk/api/Magasinstatistikk/HentOffentligDataSisteUke"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    zone_map = {1: "NO1", 2: "NO2", 3: "NO3", 4: "NO4", 5: "NO5"}
    rows = []
    for rec in data:
        if rec.get("omrType") != "EL":
            continue
        omrnr = rec.get("omrnr")
        region = zone_map.get(omrnr)
        if not region:
            continue
        fyllingsgrad = rec.get("fyllingsgrad")
        fill_pct = round(fyllingsgrad * 100, 1) if fyllingsgrad is not None else None
        rows.append({
            "region": region,
            "year": rec.get("iso_aar"),
            "week": rec.get("iso_uke"),
            "fill_pct": fill_pct,
            "fetched_at": now_utc()
        })

    latest_rows = {}
    for row in sorted(rows, key=lambda x: (x["year"] or 0, x["week"] or 0), reverse=True):
        region = row["region"]
        if region not in latest_rows:
            latest_rows[region] = row
    return list(latest_rows.values())


def upsert_reservoir_levels(rows):
    if not rows:
        print("No reservoir rows to upsert.")
        return
    supabase.table("reservoir_levels").upsert(rows, on_conflict="region").execute()
    print(f"Upserted {len(rows)} reservoir rows.")


# ── NEWS ITEMS ───────────────────────────────────────────────────────────────

NEWS_FEEDS = [
    {
        "url": "https://www.nve.no/rss/nyheter/",
        "source": "NVE",
        "category": "grid"
    },
    {
        "url": "https://www.nordpoolgroup.com/en/rss/",
        "source": "Nord Pool",
        "category": "market"
    },
    {
        "url": "https://www.statnett.no/rss/nyheter/",
        "source": "Statnett",
        "category": "grid"
    },
    {
        "url": "https://energifakta.no/feed/",
        "source": "Energifakta",
        "category": "market"
    },
]


def parse_rss_feed(feed_url, source, category, max_items=5):
    items = []
    try:
        r = requests.get(feed_url, timeout=20, headers={"User-Agent": "SundeeBot/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        channel = root.find("channel")
        if channel is None:
            return items
        for item in channel.findall("item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            description = (item.findtext("description") or "").strip()[:500]
            pub_date_str = item.findtext("pubDate") or ""
            try:
                from email.utils import parsedate_to_datetime
                pub_dt = parsedate_to_datetime(pub_date_str).isoformat()
            except Exception:
                pub_dt = now_utc()
            if title and link:
                items.append({
                    "title": title[:500],
                    "summary": description,
                    "source": source,
                    "source_url": link,
                    "category": category,
                    "published_at": pub_dt,
                    "is_active": True,
                    "fetched_at": now_utc()
                })
    except Exception as e:
        print(f"  WARNING: Could not fetch {feed_url}: {e}")
    return items


def fetch_news_items():
    all_items = []
    for feed in NEWS_FEEDS:
        print(f"  Fetching RSS: {feed['source']} ({feed['url']})")
        items = parse_rss_feed(feed["url"], feed["source"], feed["category"])
        print(f"    Got {len(items)} items")
        all_items.extend(items)
    return all_items


def upsert_news_items(rows):
    if not rows:
        print("No news items to upsert.")
        return
    # Upsert on source_url to avoid duplicates
    supabase.table("news_items").upsert(rows, on_conflict="source_url").execute()
    print(f"Upserted {len(rows)} news items.")


# ── GENERATION MIX (ENTSO-E stub) ────────────────────────────────────────────
# NOTE: Requires ENTSO-E API key (set ENTSOE_KEY env var).
# Until key is configured, this function logs a warning and skips.

def fetch_generation_mix():
    api_key = os.environ.get("ENTSOE_KEY", "")
    if not api_key:
        print("  SKIP: ENTSOE_KEY not set. Skipping generation mix fetch.")
        return []
    # ENTSO-E Transparency Platform REST API
    # DocumentType=A75 = Actual generation per type, ProcessType=A16 = Realised
    # Area: 10YNO-0--------C = Norway
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).strftime("%Y%m%d%H00")
    end = now.strftime("%Y%m%d%H00")
    url = (
        "https://web-api.tp.entsoe.eu/api"
        f"?securityToken={api_key}"
        "&documentType=A75"
        "&processType=A16"
        "&in_Domain=10YNO-0--------C"
        f"&periodStart={start}"
        f"&periodEnd={end}"
    )
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        # Parse XML response - simplified stub
        print(f"  Fetched ENTSO-E generation mix ({len(r.content)} bytes)")
        # TODO: parse XML and extract generation by fuel type
        return []
    except Exception as e:
        print(f"  ERROR fetching generation mix: {e}")
        return []


# ── CROSS-BORDER FLOWS (ENTSO-E stub) ────────────────────────────────────────

def fetch_cross_border_flows():
    api_key = os.environ.get("ENTSOE_KEY", "")
    if not api_key:
        print("  SKIP: ENTSOE_KEY not set. Skipping cross-border flows fetch.")
        return []
    # Borders: NO->SE, NO->DK, NO->DE, NO->NL, NO->GB
    # DocumentType=A11 = Aggregated net position
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).strftime("%Y%m%d%H00")
    end = now.strftime("%Y%m%d%H00")
    borders = [
        ("10YNO-0--------C", "10YSE-1--------K", "NO-SE"),
        ("10YNO-0--------C", "10Y1001A1001A65H", "NO-DK"),
        ("10YNO-0--------C", "10Y1001A1001A63L", "NO-NL"),
        ("10YNO-0--------C", "10YGB----------A", "NO-GB"),
    ]
    rows = []
    for out_domain, in_domain, border_name in borders:
        url = (
            "https://web-api.tp.entsoe.eu/api"
            f"?securityToken={api_key}"
            "&documentType=A11"
            f"&out_Domain={out_domain}"
            f"&in_Domain={in_domain}"
            f"&periodStart={start}"
            f"&periodEnd={end}"
        )
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            print(f"  Fetched ENTSO-E flow {border_name} ({len(r.content)} bytes)")
            # TODO: parse XML and store net flow MW
        except Exception as e:
            print(f"  ERROR fetching {border_name}: {e}")
    return rows


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("--- Fetching power prices ---")
    try:
        price_rows = fetch_power_prices()
        print(f"Fetched {len(price_rows)} price rows.")
        upsert_power_prices(price_rows)
    except Exception as e:
        print(f"ERROR in fetch_power_prices: {e}")

    print("--- Fetching reservoir levels ---")
    try:
        reservoir_rows = fetch_reservoir_levels()
        print(f"Fetched {len(reservoir_rows)} reservoir rows.")
        upsert_reservoir_levels(reservoir_rows)
    except Exception as e:
        print(f"ERROR in fetch_reservoir_levels: {e}")

    print("--- Fetching news items ---")
    try:
        news_rows = fetch_news_items()
        print(f"Fetched {len(news_rows)} news items total.")
        upsert_news_items(news_rows)
    except Exception as e:
        print(f"ERROR in fetch_news_items: {e}")

    print("--- Fetching generation mix (ENTSO-E) ---")
    try:
        fetch_generation_mix()
    except Exception as e:
        print(f"ERROR in fetch_generation_mix: {e}")

    print("--- Fetching cross-border flows (ENTSO-E) ---")
    try:
        fetch_cross_border_flows()
    except Exception as e:
        print(f"ERROR in fetch_cross_border_flows: {e}")

    print("Done.")
