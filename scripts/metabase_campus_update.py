#!/usr/bin/env python3
"""
Build campus-data.js for the TinkerHub Campus Chapters Dashboard.

Runs inside GitHub Actions (see .github/workflows/campus-update.yml), NOT in
any Claude sandbox -- the Metabase API key stays a repo secret.

For every active campus chapter (sub_org), computes FY-to-date stats:
  - events run this FY, broken down by type
  - unique attendees this FY and how many came back for a 2nd+ event
    ("retention"). An "attendee" here is any non-rejected attendee row
    (registration_status != 'rejected') with a resolved membership_id --
    this matches how the existing chapter dashboard counts people, which is
    NOT gated on check-in (many chapters register people who never formally
    check in, and gating on check_in undercounts badly for those chapters).
  - projects submitted at that chapter's events this FY, plus independent
    (non-event) projects created this FY by any of the chapter's members
  - community membership snapshot (current, not FY-scoped): total members,
    new joiners this FY, "still in campus" (graduation year >= current
    year) vs alumni vs unknown graduation year

The query window always starts at the current Indian financial year's
April 1 and runs up to "now" (not a full FY forecast) -- this is a
"how's this year going so far" dashboard, not a fixed annual report.

Required env vars:
  METABASE_URL       e.g. https://metabase.tinkerhub.org
  METABASE_API_KEY   Metabase API key (X-API-KEY header)

Optional env vars (rarely needed -- override the FY start if you want a
custom window instead):
  CAMPUS_SINCE_DATE   e.g. "2026-04-01" (overrides the computed FY start)
  CAMPUS_UNTIL_DATE   e.g. "2026-08-18" (overrides "today")
"""
import os
import json
import urllib.request
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

BASE = os.environ.get("METABASE_URL", "").rstrip("/")
KEY = os.environ.get("METABASE_API_KEY", "")
DB_ID = 33

OUTPUT_PATH = "campus-data.js"

MB_HEADERS = {"X-API-KEY": KEY, "Content-Type": "application/json"}

TYPE_ORDER = [
    "Learning_Program", "Meetup", "Talk_Session", "Core_Team_Meeting",
    "Workshop", "Bootcamp", "Hackathon",
]


def current_financial_year_start():
    """Indian FY starts April 1. Returns 'YYYY-MM-DD' for the start of the
    FY containing today (IST)."""
    today = datetime.now(IST).date()
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    return f"{fy_start_year}-04-01"


_today_ist = datetime.now(IST).date().isoformat()
SINCE_DATE = os.environ.get("CAMPUS_SINCE_DATE", current_financial_year_start())
UNTIL_DATE = os.environ.get("CAMPUS_UNTIL_DATE", _today_ist)
CURRENT_YEAR = int(UNTIL_DATE[:4])


def mb_run_sql(sql):
    body = json.dumps({"database": DB_ID, "type": "native", "native": {"query": sql}}).encode()
    req = urllib.request.Request(BASE + "/api/dataset", data=body, headers=MB_HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    cols = [c.get("name") for c in data.get("data", {}).get("cols", [])]
    rows = data.get("data", {}).get("rows", [])
    return [dict(zip(cols, r)) for r in rows]


SUB_ORGS_SQL = """
SELECT id, name, district
FROM sub_orgs
WHERE state = 'active'
ORDER BY name
"""

EVENTS_SQL = f"""
SELECT sub_org_id, type, COUNT(*) AS c
FROM events
WHERE start_date >= timestamp '{SINCE_DATE}'
  AND start_date <= timestamp '{UNTIL_DATE}' + interval '1 day'
  AND status != 'cancelled'
  AND sub_org_id IS NOT NULL
GROUP BY sub_org_id, type
"""

ATTENDEES_SQL = f"""
WITH ev AS (
    SELECT id, sub_org_id
    FROM events
    WHERE start_date >= timestamp '{SINCE_DATE}'
      AND start_date <= timestamp '{UNTIL_DATE}' + interval '1 day'
      AND status != 'cancelled'
),
att AS (
    SELECT a.event_id, a.membership_id, ev.sub_org_id
    FROM attendees a
    JOIN ev ON ev.id = a.event_id
    WHERE a.membership_id IS NOT NULL AND a.registration_status != 'rejected'
),
per_member AS (
    SELECT sub_org_id, membership_id, COUNT(DISTINCT event_id) AS n_events
    FROM att
    GROUP BY sub_org_id, membership_id
)
SELECT sub_org_id,
       COUNT(*) AS unique_attendees,
       COUNT(*) FILTER (WHERE n_events >= 2) AS returning,
       SUM(n_events) AS total_attendance
FROM per_member
GROUP BY sub_org_id
"""

MEMBERS_SQL = f"""
SELECT sub_org_id,
       COUNT(*) AS members,
       COUNT(*) FILTER (WHERE created_at >= timestamp '{SINCE_DATE}') AS new_fy,
       COUNT(*) FILTER (WHERE year_of_graduation >= {CURRENT_YEAR}) AS still_in,
       COUNT(*) FILTER (WHERE year_of_graduation < {CURRENT_YEAR}) AS alumni,
       COUNT(*) FILTER (WHERE year_of_graduation IS NULL) AS grad_unknown
FROM memberships
WHERE sub_org_id IS NOT NULL
GROUP BY sub_org_id
"""

PROJECTS_SQL = f"""
WITH ev AS (
    SELECT id, sub_org_id
    FROM events
    WHERE start_date >= timestamp '{SINCE_DATE}'
      AND start_date <= timestamp '{UNTIL_DATE}' + interval '1 day'
      AND status != 'cancelled'
),
proj_fy AS (
    SELECT id, event_id
    FROM projects
    WHERE created_at >= timestamp '{SINCE_DATE}'
      AND created_at <= timestamp '{UNTIL_DATE}' + interval '1 day'
),
event_projects AS (
    SELECT p.id AS project_id, ev.sub_org_id
    FROM proj_fy p
    JOIN ev ON ev.id = p.event_id
),
member_projects AS (
    SELECT DISTINCT pc.project_id, m.sub_org_id
    FROM proj_fy p
    JOIN project_collaborators pc ON pc.project_id = p.id
    JOIN memberships m ON m.id = pc.membership_id
    WHERE p.event_id IS NULL
),
all_proj AS (
    SELECT project_id, sub_org_id FROM event_projects
    UNION
    SELECT project_id, sub_org_id FROM member_projects
)
SELECT sub_org_id, COUNT(DISTINCT project_id) AS projects
FROM all_proj
GROUP BY sub_org_id
"""


def main():
    sub_orgs = mb_run_sql(SUB_ORGS_SQL)
    events_rows = mb_run_sql(EVENTS_SQL)
    attendee_rows = mb_run_sql(ATTENDEES_SQL)
    member_rows = mb_run_sql(MEMBERS_SQL)
    project_rows = mb_run_sql(PROJECTS_SQL)

    types_by_org = {}
    events_by_org = {}
    for r in events_rows:
        sid = r["sub_org_id"]
        types_by_org.setdefault(sid, {})[r["type"]] = r["c"]
        events_by_org[sid] = events_by_org.get(sid, 0) + r["c"]

    att_by_org = {r["sub_org_id"]: r for r in attendee_rows}
    mem_by_org = {r["sub_org_id"]: r for r in member_rows}
    proj_by_org = {r["sub_org_id"]: r["projects"] for r in project_rows}

    campuses = []
    for so in sub_orgs:
        sid = so["id"]
        att = att_by_org.get(sid, {})
        mem = mem_by_org.get(sid, {})
        campuses.append({
            "id": sid,
            "name": so["name"],
            "district": (so.get("district") or "").strip().lower() or None,
            "events": events_by_org.get(sid, 0),
            "types": types_by_org.get(sid, {}),
            "uniqueAttendees": att.get("unique_attendees", 0) or 0,
            "returning": att.get("returning", 0) or 0,
            "totalAttendance": int(att.get("total_attendance", 0) or 0),
            "members": mem.get("members", 0) or 0,
            "newFY": mem.get("new_fy", 0) or 0,
            "stillIn": mem.get("still_in", 0) or 0,
            "alumni": mem.get("alumni", 0) or 0,
            "gradUnknown": mem.get("grad_unknown", 0) or 0,
            "projects": proj_by_org.get(sid, 0),
        })

    now = datetime.now(timezone.utc)
    out = {
        "generatedAt": now.strftime("%Y-%m-%d"),
        "generatedAtIso": now.isoformat(),
        "windowSinceDate": SINCE_DATE,
        "windowUntilDate": UNTIL_DATE,
        "campuses": campuses,
    }

    with open(OUTPUT_PATH, "w") as f:
        f.write("// Auto-generated by .github/workflows/campus-update.yml\n")
        f.write("// Do NOT hand-edit -- this file is overwritten on each run.\n")
        f.write("window.CAMPUS_DATA = ")
        f.write(json.dumps(out, indent=2))
        f.write(";\n")

    print(f"Wrote {OUTPUT_PATH}: {len(campuses)} active chapters, window {SINCE_DATE}..{UNTIL_DATE}.")


if __name__ == "__main__":
    main()
