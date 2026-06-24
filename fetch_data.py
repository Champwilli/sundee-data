import os
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from supabase import create_client

SUPABASE_URL = "https://wrixhnypdeavgjmdwsik.supabase.co"
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

PRICE_AREAS = ["NO1", "NO2", "NO3", "NO4", "NO5"]


def now_utc():
    return datetime.now(timezone.utc).isoformat()


# -- SPOT PRICES (Energi Data Service) --
# Henter siste 100 timer PER sone (5 separate API-kall) for garantert dekning
def fetch_power_prices():
    all_rows = []
    for zone in PRICE_AREAS:
        try:
            params = {
                "limit": 100,
                "filter": '{"PriceArea":"' + zone + '"}',
                "sort": "HourDK DESC"
            }
            r = requests.get(
                "https://api.energidataservice.dk/dataset/Elspotprices",
                params=params,
                timeout=30
            )
            r.raise_for_status()
            records = r.json().get("records", [])
            zone_rows = 0
            for rec in records:
                hour_dk = rec.get("HourDK")
                if not hour_dk:
                    continue
                try:
                    dt_local = datetime.fromisoformat(hour_dk)
                    # HourDK er norsk lokaltid (CET/CEST). Vi bruker UTC+1 som konservativ offset.
                    dt_utc = dt_local.replace(tzinfo=timezone(timedelta(hours=1)))
                    timestamp_utc = dt_utc.astimezone(timezone.utc).isoformat()
                except Exception:
                    timestamp_utc = now_utc()
                price_dkk = rec.get("SpotPriceDKK")
                price_eur = rec.get("SpotPriceEUR")
                price_ore_kwh = round(price_dkk / 10, 2) if price_dkk is not None else None
                price_eur_mwh = round(price_eur, 2) if price_eur is not None else None
                all_rows.append({
                    "timestamp_utc": timestamp_utc,
                    "zone": zone,
                    "price_ore_kwh": price_ore_kwh,
                    "price_eur_mwh": price_eur_mwh,
                    "hour": hour_dk,
                    "fetched_at": now_utc()
                })
                zone_rows += 1
            print(f"  {zone}: {zone_rows} rader hentet")
            time.sleep(2)  # respekter rate limit
        except Exception as e:
            print(f"  ERROR henting av {zone}: {e}")
            time.sleep(2)  # vent ogsaa ved feil
    return all_rows


def upsert_power_prices(rows):
    if not rows:
        print("No price rows to upsert.")
        return
    # Upsert i batches av 200 for aa unngaa timeouts
    batch_size = 200
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        supabase.table("spot_prices").upsert(
            batch, on_conflict="timestamp_utc,zone"
        ).execute()
        total += len(batch)
    print(f"Upserted {total} price rows.")


# -- RESERVOIR LEVELS (NVE Magasinstatistikk) --
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
        if fyllingsgrad is not None:
            if fyllingsgrad <= 1.0:
                fill_pct = round(fyllingsgrad * 100, 1)
            else:
                fill_pct = round(fyllingsgrad, 1)
        else:
            fill_pct = None
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
    supabase.table("reservoir_levels").upsert(
        rows, on_conflict="region"
    ).execute()
    print(f"Upserted {len(rows)} reservoir rows.")


# -- NEWS ITEMS (RSS feeds) --
NEWS_FEEDS = [
    {"url": "https://energifakta.no/feed/", "source": "Energifakta", "category": "market"},
    {"url": "https://www.ssb.no/rss/energi", "source": "SSB Energi", "category": "market"},
    {"url": "https://www.nve.no/rss/nyheter", "source": "NVE", "category": "grid"},
    {"url": "https://e24.no/rss2/", "source": "E24", "category": "market"},
    {"url": "https://www.tu.no/rss", "source": "Teknisk Ukeblad", "category": "grid"},
]


def parse_rss_feed(feed_url, source, category, max_items=5):
    items = []
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; SundeeBot/2.0)"}
        r = requests.get(feed_url, timeout=20, headers=headers)
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
        print(f"  Got {len(items)} items")
        all_items.extend(items)
    return all_items


def upsert_news_items(rows):
    if not rows:
        print("No news items to upsert.")
        return
    supabase.table("news_items").upsert(rows, on_conflict="source_url").execute()
    print(f"Upserted {len(rows)} news items.")


# -- GENERATION MIX (ENTSO-E stub) --
def fetch_generation_mix():
    api_key = os.environ.get("ENTSOE_KEY", "")
    if not api_key:
        print("  SKIP: ENTSOE_KEY not set.")
        return []
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).strftime("%Y%m%d%H00")
    end = now.strftime("%Y%m%d%H00")
    url = (
        f"https://web-api.tp.entsoe.eu/api"
        f"?securityToken={api_key}"
        "&documentType=A75&processType=A16"
        "&in_Domain=10YNO-0--------C"
        f"&periodStart={start}&periodEnd={end}"
    )
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        print(f"  Fetched ENTSO-E generation mix ({len(r.content)} bytes)")
    except Exception as e:
        print(f"  ERROR fetching generation mix: {e}")
    return []


# -- CROSS-BORDER FLOWS (ENTSO-E stub) --
def fetch_cross_border_flows():
    api_key = os.environ.get("ENTSOE_KEY", "")
    if not api_key:
        print("  SKIP: ENTSOE_KEY not set.")
        return []
    return []


# -- MAIN --
if __name__ == "__main__":
    print("--- Fetching power prices ---")
    try:
        price_rows = fetch_power_prices()
        print(f"Fetched {len(price_rows)} price rows (alle soner, siste 100 timer).")
        upsert_power_prices(price_rows)
    except Exception as e:
        print(f"ERROR in fetch_power_prices: {e}")

    print("--- Fetching reservoir levels ---")
    try:
        reservoir_rows = fetch_reservoir_levels()
        print(f"Fetched {len(reservoir_rows)} reservoir rows.")
        for rr in reservoir_rows:
            print(f"  {rr['region']}: {rr['fill_pct']}% (uke {rr['week']}, {rr['year']})")
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
