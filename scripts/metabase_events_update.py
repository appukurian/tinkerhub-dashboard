#!/usr/bin/env python3
"""
Build events-data.js for the TinkerHub "what's happening" Kerala map dashboard.

Runs inside GitHub Actions (see .github/workflows/events-update.yml), NOT in
any Claude sandbox -- the Metabase API key stays a repo secret.

Pulls events (Learning_Program, Meetup, Talk_Session, Workshop, Bootcamp,
Hackathon, Core_Team_Meeting, Project_Building_Program, Makeathon) in a
rolling window around "now", joins in the campus (sub_org) / venue / district
and attendee counts, and resolves a lat/lng for each event so it can be
plotted on a map:
  1. If the venue/campus has a Google Maps link, follow redirects and pull
     coordinates out of the resolved URL (cheap, precise, no external geocoder).
  2. Otherwise geocode the postal address via OpenStreetMap Nominatim
     (rate-limited to ~1 req/sec, and cached across runs in geocode-cache.json
     so we only ever look up a given address once).
  3. Otherwise fall back to the Kerala district centroid, nudged by a small
     deterministic jitter (hashed from the campus name) so multiple pins in
     the same district don't stack exactly on top of each other.

Required env vars:
  METABASE_URL       e.g. https://metabase.tinkerhub.org
  METABASE_API_KEY   Metabase API key (X-API-KEY header)

Optional env vars:
  EVENTS_SINCE_DATE   default "2026-07-01" (fixed cutoff -- "after June")
  EVENTS_FUTURE_DAYS  default 45
"""
import os
import re
import json
import time
import hashlib
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

BASE = os.environ.get("METABASE_URL", "").rstrip("/")
KEY = os.environ.get("METABASE_API_KEY", "")
DB_ID = 33
SINCE_DATE = os.environ.get("EVENTS_SINCE_DATE", "2026-07-01")
FUTURE_DAYS = int(os.environ.get("EVENTS_FUTURE_DAYS", "45"))

OUTPUT_PATH = "events-data.js"
GEOCODE_CACHE_PATH = "geocode-cache.json"

MB_HEADERS = {"X-API-KEY": KEY, "Content-Type": "application/json"}
GEOCODE_UA = "TinkerHubEventsDashboard/1.0 (https://github.com/appukurian/tinkerhub-dashboard; kurian@tinkerhub.org)"

# Approximate centroid for each Kerala district (used only as a last-resort
# fallback when we can't resolve a real coordinate for an event).
DISTRICT_CENTROIDS = {
    "thiruvananthapuram": (8.5241, 76.9366),
    "kollam": (8.8932, 76.6141),
    "pathanamthitta": (9.2648, 76.7870),
    "alappuzha": (9.4981, 76.3388),
    "kottayam": (9.5916, 76.5222),
    "idukki": (9.8500, 76.9700),
    "ernakulam": (9.9816, 76.2999),
    "thrissur": (10.5276, 76.2144),
    "palakkad": (10.7867, 76.6548),
    "malappuram": (11.0510, 76.0711),
    "kozhikode": (11.2588, 75.7804),
    "wayanad": (11.6854, 76.1320),
    "kannur": (11.8745, 75.3704),
    "kasaragod": (12.4996, 74.9869),
    "all kerala": (10.27, 76.32),
}

EVENT_TYPE_COLORS = {
    "Learning_Program": "#2563eb",
    "Meetup": "#16a34a",
    "Talk_Session": "#d97706",
    "Workshop": "#7c3aed",
    "Core_Team_Meeting": "#6b7280",
    "Bootcamp": "#db2777",
    "Hackathon": "#dc2626",
    "Project_Building_Program": "#0891b2",
    "Makeathon": "#ca8a04",
}
DEFAULT_COLOR = "#64748b"


def mb_run_sql(sql):
    body = json.dumps({"database": DB_ID, "type": "native", "native": {"query": sql}}).encode()
    req = urllib.request.Request(BASE + "/api/dataset", data=body, headers=MB_HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    cols = [c.get("name") for c in data.get("data", {}).get("cols", [])]
    rows = data.get("data", {}).get("rows", [])
    return [dict(zip(cols, r)) for r in rows]


SQL = f"""
WITH att AS (
    SELECT event_id, COUNT(*) AS registered,
           SUM(CASE WHEN check_in THEN 1 ELSE 0 END) AS checked_in
    FROM attendees
    GROUP BY event_id
),
venue AS (
    SELECT DISTINCT ON (event_id) event_id, address AS venue_address,
           map_url AS venue_map_url, name AS venue_name
    FROM event_venue
    ORDER BY event_id, created_at DESC
)
SELECT e.id, e.name, e.type, e.status, e.start_date, e.end_date, e.is_virtual,
       e.location, e.map_url AS event_map_url, e.number_of_seats, e.seats_available,
       so.id AS sub_org_id, so.name AS campus_name, so.district,
       so.address AS campus_address, so.map_url AS campus_map_url,
       v.venue_address, v.venue_map_url, v.venue_name,
       COALESCE(a.registered, 0) AS registered, COALESCE(a.checked_in, 0) AS checked_in
FROM events e
LEFT JOIN sub_orgs so ON e.sub_org_id = so.id
LEFT JOIN venue v ON v.event_id = e.id
LEFT JOIN att a ON a.event_id = e.id
WHERE e.start_date >= timestamp '{SINCE_DATE}'
  AND e.start_date <= now() + interval '{FUTURE_DAYS} days'
  AND e.status != 'cancelled'
  AND (so.id IS NULL OR so.state = 'active')
ORDER BY e.start_date ASC
"""


def load_geocode_cache():
    try:
        with open(GEOCODE_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_geocode_cache(cache):
    with open(GEOCODE_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


COORD_RE = re.compile(r"[!@](-?\d+\.\d+),(-?\d+\.\d+)")
COORD_3D4D_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")


def resolve_maps_link(url, timeout=10):
    """Follow a Google Maps short/place link and try to extract lat,lng from
    the final resolved URL. Returns (lat, lng) or None."""
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": GEOCODE_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            # some responses also embed the place page HTML with coords
            try:
                body = resp.read(200000).decode(errors="replace")
            except Exception:
                body = ""
    except Exception:
        return None

    for text in (final_url, body):
        m = COORD_3D4D_RE.search(text)
        if m:
            return float(m.group(1)), float(m.group(2))
        m = COORD_RE.search(text)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None


def geocode_address(address, timeout=10):
    if not address:
        return None
    q = address if "kerala" in address.lower() else address + ", Kerala, India"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": q, "format": "json", "limit": 1, "countrycodes": "in",
    })
    try:
        req = urllib.request.Request(url, headers={"User-Agent": GEOCODE_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            results = json.loads(resp.read().decode())
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception:
        pass
    return None


def jitter(seed_text, base_lat, base_lng):
    h = hashlib.sha1(seed_text.encode()).hexdigest()
    dx = (int(h[:8], 16) / 0xFFFFFFFF - 0.5) * 0.12
    dy = (int(h[8:16], 16) / 0xFFFFFFFF - 0.5) * 0.12
    return round(base_lat + dx, 5), round(base_lng + dy, 5)


def resolve_location(row, cache):
    """Returns (lat, lng, source) for a row, using and updating `cache`."""
    district_key = (row.get("district") or "").strip().lower()
    campus_name = row.get("campus_name") or row.get("venue_name") or row.get("location") or "unknown"

    # Best available map link, in priority order.
    map_url = row.get("venue_map_url") or row.get("campus_map_url") or row.get("event_map_url")
    address = row.get("venue_address") or row.get("campus_address")

    cache_key = map_url or address
    if cache_key and cache_key in cache:
        cached = cache[cache_key]
        if cached.get("lat") is not None:
            return cached["lat"], cached["lng"], cached["source"]
        # cached as "unresolved" -- fall through to district fallback below,
        # but don't retry the network call every run.
    else:
        result = None
        source = None
        if map_url and ("google.com/maps" in map_url or "goo.gl" in map_url):
            result = resolve_maps_link(map_url)
            source = "maps_link"
            if not result:
                time.sleep(0.2)
        if not result and address:
            result = geocode_address(address)
            source = "nominatim"
            time.sleep(1.1)  # be polite to Nominatim's free tier

        if cache_key:
            cache[cache_key] = {
                "lat": result[0] if result else None,
                "lng": result[1] if result else None,
                "source": source if result else "unresolved",
            }
        if result:
            return result[0], result[1], source

    # Fallback: district centroid + deterministic jitter.
    base = DISTRICT_CENTROIDS.get(district_key)
    if base:
        lat, lng = jitter(campus_name, base[0], base[1])
        return lat, lng, "district_fallback"
    return None, None, "unresolved"


def main():
    rows = mb_run_sql(SQL)
    cache = load_geocode_cache()

    events = []
    lookups_done = 0
    for row in rows:
        lat, lng, source = resolve_location(row, cache)
        if source in ("maps_link", "nominatim") and lat is not None:
            lookups_done += 1

        district = (row.get("district") or "").strip().lower() or None
        events.append({
            "id": row["id"],
            "name": row["name"],
            "type": row["type"],
            "typeColor": EVENT_TYPE_COLORS.get(row["type"], DEFAULT_COLOR),
            "status": row["status"],
            "start": row["start_date"],
            "end": row["end_date"],
            "isVirtual": bool(row["is_virtual"]),
            "campusName": row.get("campus_name") or row.get("venue_name"),
            "district": district,
            "address": row.get("venue_address") or row.get("campus_address"),
            "lat": lat,
            "lng": lng,
            "locationSource": source,
            "registered": row.get("registered") or 0,
            "checkedIn": row.get("checked_in") or 0,
            "seats": row.get("number_of_seats"),
            "mapUrl": row.get("venue_map_url") or row.get("campus_map_url") or row.get("event_map_url"),
        })

    save_geocode_cache(cache)

    now = datetime.now(timezone.utc)

    def is_today(ev):
        if not ev["start"]:
            return False
        d = datetime.fromisoformat(ev["start"]).astimezone(timezone.utc).date()
        return d == now.date()

    by_type = {}
    by_district = {}
    for ev in events:
        by_type[ev["type"]] = by_type.get(ev["type"], 0) + 1
        if ev["district"]:
            by_district[ev["district"]] = by_district.get(ev["district"], 0) + 1

    summary = {
        "totalEvents": len(events),
        "todayCount": sum(1 for e in events if is_today(e)),
        "upcomingCount": sum(1 for e in events if e["start"] and datetime.fromisoformat(e["start"]).astimezone(timezone.utc) > now),
        "pastCount": sum(1 for e in events if e["start"] and datetime.fromisoformat(e["start"]).astimezone(timezone.utc) <= now),
        "totalRegistered": sum(e["registered"] for e in events),
        "totalCheckedIn": sum(e["checkedIn"] for e in events),
        "virtualCount": sum(1 for e in events if e["isVirtual"]),
        "geocoded": sum(1 for e in events if e["locationSource"] in ("maps_link", "nominatim")),
        "districtFallback": sum(1 for e in events if e["locationSource"] == "district_fallback"),
        "unresolved": sum(1 for e in events if e["locationSource"] == "unresolved"),
    }

    out = {
        "generatedAt": now.strftime("%Y-%m-%d"),
        "generatedAtIso": now.isoformat(),
        "windowSinceDate": SINCE_DATE,
        "windowFutureDays": FUTURE_DAYS,
        "events": events,
        "summary": summary,
        "byType": [{"type": t, "count": c, "color": EVENT_TYPE_COLORS.get(t, DEFAULT_COLOR)} for t, c in sorted(by_type.items(), key=lambda x: -x[1])],
        "byDistrict": [{"district": d, "count": c} for d, c in sorted(by_district.items(), key=lambda x: -x[1])],
        "typeColors": EVENT_TYPE_COLORS,
        "districtCentroids": {k: list(v) for k, v in DISTRICT_CENTROIDS.items()},
    }

    with open(OUTPUT_PATH, "w") as f:
        f.write("// Auto-generated by .github/workflows/events-update.yml\n")
        f.write("// Do NOT hand-edit -- this file is overwritten on each run.\n")
        f.write("window.EVENTS_DATA = ")
        f.write(json.dumps(out, indent=2))
        f.write(";\n")

    print(f"Wrote {OUTPUT_PATH}: {len(events)} events, {lookups_done} freshly resolved this run.")
    print(f"Summary: {summary}")


if __name__ == "__main__":
    main()
