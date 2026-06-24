import os
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


# ── SPOT PRICES (Energi Data Service) ─────────────────────────────────────────
# Henter siste 5 timer per sone (NO1-NO5) fra dansk energidataservice
# Upsert paa timestamp_utc + zone (unik per time per sone)
def fetch_power_prices():
    # Hent siste 2 dager for aa sikre alle 5 soner
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    url = (
        "https://api.energidataservice.dk/dataset/Elspotprices"
        "?limit=250"
        "&filter=%7B%22PriceArea%22:%5B%22NO1%22,%22NO2%22,%22NO3%22,%22NO4%22,%22NO5%22%5D%7D"
        f"&start={yesterday}&end={today}"
        "&sort=HourDK%20DESC"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    records = r.json().get("records", [])
    rows = []
    seen = set()
    for rec in records:
        zone = rec.get("PriceArea")
        hour_dk = rec.get("HourDK")  # e.g. "2026-06-24T19:00:00"
        if zone not in PRICE_AREAS or not hour_dk:
            continue
        key = (zone, hour_dk)
        if key in seen:
            continue
        seen.add(key)
        # Konverter HourDK (dansk lokaltid = CEST = UTC+2) til UTC
        try:
            dt_local = datetime.fromisoformat(hour_dk)
            dt_utc = dt_local.replace(tzinfo=timezone(timedelta(hours=2)))
            timestamp_utc = dt_utc.astimezone(timezone.utc).isoformat()
        except Exception:
            timestamp_utc = now_utc()
        price_dkk = rec.get("SpotPriceDKK")
        price_eur = rec.get("SpotPriceEUR")
        price_ore_kwh = round(price_dkk / 10, 2) if price_dkk is not None else None
        price_eur_mwh = round(price_eur, 2) if price_eur is not None else None
        rows.append({
            "timestamp_utc": timestamp_utc,
            "zone": zone,
            "price_ore_kwh": price_ore_kwh,
            "price_eur_mwh": price_eur_mwh,
            "hour": hour_dk,
            "fetched_at": now_utc()
        })
    return rows


def upsert_power_prices(rows):
    if not rows:
        print("No price rows to upsert.")
        return
    # Upsert paa timestamp_utc + zone (behold historikk, dedupliser per time)
    supabase.table("spot_prices").upsert(
        rows, on_conflict="timestamp_utc,zone"
    ).execute()
    print(f"Upserted {len(rows)} price rows.")


# ── RESERVOIR LEVELS (NVE Magasinstatistikk) ──────────────────────────────────
# Henter ukentlig magasinfylling per region NO1-NO5 fra NVE
# API returnerer siste uke automatisk
def fetch_reservoir_levels():
    url = "https://biapi.nve.no/magasinstatistikk/api/Magasinstatistikk/HentOffentligDataSisteUke"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    # omrnr 1-5 = NO1-NO5 (elkraftomraader)
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
        # NVE returnerer fyllingsgrad som desimal (0-1), gang med 100 for prosent
        if fyllingsgrad is not None:
            if fyllingsgrad <= 1.0:
                fill_pct = round(fyllingsgrad * 100, 1)
            else:
                fill_pct = round(fyllingsgrad, 1)  # allerede i prosent
        else:
            fill_pct = None
        year = rec.get("iso_aar")
        week = rec.get("iso_uke")
        rows.append({
            "region": region,
            "year": year,
            "week": week,
            "fill_pct": fill_pct,
            "fetched_at": now_utc()
        })

    # Behold kun siste rad per region (hoeyest aar+uke)
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
    # Upsert paa region (vi lagrer kun siste uke per region)
    supabase.table("reservoir_levels").upsert(
        rows, on_conflict="region"
    ).execute()
    print(f"Upserted {len(rows)} reservoir rows.")


# ── NEWS ITEMS (RSS feeds) ─────────────────────────────────────────────────────
# Energifakta.no fungerer. NVE/Statnett/NordPool RSS er nede - bruker alternativ
NEWS_FEEDS = [
    {
        "url": "https://energifakta.no/feed/",
        "source": "Energifakta",
        "category": "market"
    },
    {
        "url": "https://www.ssb.no/rss/energi",
        "source": "SSB Energi",
        "category": "market"
    },
    {
        "url": "https://www.regjeringen.no/en/rss/rss_olje_energi/",
        "source": "Olje- og energidepartementet",
        "category": "grid"
    },
    {
        "url": "https://www.nve.no/rss/nyheter/",
        "source": "NVE",
        "category": "grid"
    },
    {
        "url": "https://e24.no/rss2/",
        "source": "E24",
        "category": "market"
    },
]


def parse_rss_feed(feed_url, source, category, max_items=5):
    items = []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; SundeeBot/2.0; +https://sundee.no)"
        }
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
    supabase.table("news_items").upsert(
        rows, on_conflict="source_url"
    ).execute()
    print(f"Upserted {len(rows)} news items.")


# ── GENERATION MIX (ENTSO-E stub) ─────────────────────────────────────────────
def fetch_generation_mix():
    api_key = os.environ.get("ENTSOE_KEY", "")
    if not api_key:
        print("  SKIP: ENTSOE_KEY not set. Skipping generation mix fetch.")
        return []
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).strftime("%Y%m%d%H00")
    end = now.strftime("%Y%m%d%H00")
    url = (
        "https://web-api.tp.entsoe.eu/api"
        f"?securityToken={api_key}"
        "&documentType=A75&processType=A16"
        "&in_Domain=10YNO-0--------C"
        f"&periodStart={start}&periodEnd={end}"
    )
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        print(f"  Fetched ENTSO-E generation mix ({len(r.content)} bytes)")
        return []
    except Exception as e:
        print(f"  ERROR fetching generation mix: {e}")
        return []


# ── CROSS-BORDER FLOWS (ENTSO-E stub) ─────────────────────────────────────────
def fetch_cross_border_flows():
    api_key = os.environ.get("ENTSOE_KEY", "")
    if not api_key:
        print("  SKIP: ENTSOE_KEY not set. Skipping cross-border flows fetch.")
        return []
    return []


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("--- Fetching power prices ---")
    try:
        price_rows = fetch_power_prices()
        print(f"Fetched {len(price_rows)} price rows (alle soner, siste timer).")
        upsert_power_prices(price_rows)
    except Exception as e:
        print(f"ERROR in fetch_power_prices: {e}")

    print("--- Fetching reservoir levels ---")
    try:
        reservoir_rows = fetch_reservoir_levels()
        print(f"Fetched {len(reservoir_rows)} reservoir rows.")
        for r in reservoir_rows:
            print(f"  {r['region']}: {r['fill_pct']}% (uke {r['week']}, {r['year']})")
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
