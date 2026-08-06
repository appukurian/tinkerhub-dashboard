#!/usr/bin/env python3
"""One-shot exploratory query runner -- same pattern as metabase_discover.py.
Runs a handful of SQL queries against TheHubDB via Metabase's /api/dataset
and commits the results to metabase-sample.json for inspection."""
import os
import json
import urllib.request
import urllib.error

BASE = os.environ.get("METABASE_URL", "").rstrip("/")
KEY = os.environ.get("METABASE_API_KEY", "")
DB_ID = 33
OUTPUT_PATH = "metabase-sample.json"

HEADERS = {"X-API-KEY": KEY, "Content-Type": "application/json"}

QUERIES = {
    "event_types": "SELECT type, count(*) AS n FROM events GROUP BY type ORDER BY n DESC LIMIT 50",
    "event_status": "SELECT status, count(*) AS n FROM events GROUP BY status ORDER BY n DESC LIMIT 50",
    "sub_org_districts": "SELECT district, state, count(*) AS n FROM sub_orgs GROUP BY district, state ORDER BY n DESC LIMIT 100",
    "sub_org_types": "SELECT type, count(*) AS n FROM sub_orgs GROUP BY type ORDER BY n DESC LIMIT 50",
    "recent_events_sample": """
        SELECT e.id, e.name, e.type, e.status, e.start_date, e.end_date, e.location,
               e.map_url, e.is_virtual, e.number_of_seats, e.seats_available,
               so.id AS sub_org_id, so.name AS campus_name, so.district, so.state,
               so.address AS campus_address, so.map_url AS campus_map_url
        FROM events e
        LEFT JOIN sub_orgs so ON e.sub_org_id = so.id
        WHERE e.start_date >= now() - interval '45 days'
        ORDER BY e.start_date DESC
        LIMIT 40
    """,
    "upcoming_events_sample": """
        SELECT e.id, e.name, e.type, e.status, e.start_date, e.end_date, e.location,
               so.name AS campus_name, so.district
        FROM events e
        LEFT JOIN sub_orgs so ON e.sub_org_id = so.id
        WHERE e.start_date >= now()
        ORDER BY e.start_date ASC
        LIMIT 20
    """,
    "event_venue_sample": "SELECT * FROM event_venue ORDER BY created_at DESC LIMIT 15",
    "attendee_counts_sample": """
        SELECT event_id, count(*) AS registered,
               sum(CASE WHEN check_in THEN 1 ELSE 0 END) AS checked_in
        FROM attendees
        GROUP BY event_id
        ORDER BY registered DESC
        LIMIT 20
    """,
    "sub_org_sample": "SELECT id, name, district, state, address, map_url, type FROM sub_orgs LIMIT 30",
}


def run_query(sql):
    body = json.dumps({
        "database": DB_ID,
        "type": "native",
        "native": {"query": sql},
    }).encode()
    req = urllib.request.Request(BASE + "/api/dataset", data=body, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode(errors='replace')[:1000]}"}
    cols = [c.get("name") for c in data.get("data", {}).get("cols", [])]
    rows = data.get("data", {}).get("rows", [])
    return {"columns": cols, "rows": rows, "row_count": len(rows)}


def main():
    out = {}
    for key, sql in QUERIES.items():
        print("Running:", key)
        out[key] = run_query(sql)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print("Wrote", OUTPUT_PATH)


if __name__ == "__main__":
    main()
