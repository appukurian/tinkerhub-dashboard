#!/usr/bin/env python3
"""
Build linear-data.js for the TinkerHub "Hub App - Linear Status" dashboard
directly from Linear's GraphQL API.

Runs inside GitHub Actions (see .github/workflows/linear-update.yml), NOT in
any Claude session -- the Linear API key stays a repo secret.

Required env var:
  LINEAR_API_KEY   Linear personal API key (sent as-is in the Authorization
                    header -- no "Bearer " prefix; see
                    https://developers.linear.app/docs/graphql/working-with-the-graphql-api#authentication)

Optional env vars:
  LINEAR_TEAM_NAME   defaults to "Devs"
  LINEAR_WORKSPACE   defaults to "th-app" (cosmetic label only, shown on the
                      dashboard header -- Linear's API doesn't expose the
                      workspace's URL key directly)
"""
import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

API_URL = "https://api.linear.app/graphql"
API_KEY = os.environ.get("LINEAR_API_KEY", "")
TEAM_NAME = os.environ.get("LINEAR_TEAM_NAME", "Devs")
WORKSPACE_LABEL = os.environ.get("LINEAR_WORKSPACE", "th-app")

OUTPUT_PATH = "linear-data.js"

if not API_KEY:
    raise SystemExit("Missing LINEAR_API_KEY env var (set as a repo secret).")


def gql(query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Authorization": API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Linear API HTTP {e.code}: {e.read().decode()[:500]}")
    if "errors" in data:
        raise SystemExit(f"Linear API error: {json.dumps(data['errors'])[:800]}")
    return data["data"]


TEAM_QUERY = """
query($name: String!) {
  teams(filter: { name: { eq: $name } }) {
    nodes { id name }
  }
}
"""

ISSUES_QUERY = """
query($teamId: ID!, $after: String) {
  issues(filter: { team: { id: { eq: $teamId } } }, first: 100, after: $after) {
    nodes {
      identifier
      title
      url
      startedAt
      updatedAt
      state { name type }
      assignee { name email }
      project { name }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PROJECTS_QUERY = """
query($teamId: String!) {
  team(id: $teamId) {
    projects(first: 100) {
      nodes {
        name
        lead { name }
      }
    }
  }
}
"""

# Linear's `state.type` -> our statusType vocabulary (matches what the
# dashboard's JS expects: backlog / unstarted / started / completed /
# canceled / duplicate). Linear doesn't have a distinct "duplicate" state
# type -- duplicate issues show up as type "canceled" with state name
# "Duplicate", so special-case that by name.
def status_type_of(state):
    name = (state or {}).get("name") or ""
    raw_type = (state or {}).get("type") or ""
    if name.strip().lower() == "duplicate":
        return "duplicate"
    return raw_type  # backlog | unstarted | started | completed | canceled


def main():
    team_data = gql(TEAM_QUERY, {"name": TEAM_NAME})
    teams = team_data["teams"]["nodes"]
    if not teams:
        raise SystemExit(f"No Linear team found named {TEAM_NAME!r}")
    team_id = teams[0]["id"]

    all_issues_raw = []
    after = None
    while True:
        page = gql(ISSUES_QUERY, {"teamId": team_id, "after": after})["issues"]
        all_issues_raw.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]

    projects_raw = gql(PROJECTS_QUERY, {"teamId": team_id})["team"]["projects"]["nodes"]

    now = datetime.now(timezone.utc)

    all_issues = []
    team_totals = {"total": 0, "backlog": 0, "todo": 0, "started": 0, "completed": 0, "canceled": 0, "duplicate": 0}
    stuck = []
    STATUS_TYPE_KEY = {
        "backlog": "backlog",
        "unstarted": "todo",
        "started": "started",
        "completed": "completed",
        "canceled": "canceled",
        "duplicate": "duplicate",
    }

    for it in all_issues_raw:
        state = it.get("state") or {}
        status_type = status_type_of(state)
        assignee = it.get("assignee")
        assignee_label = None
        if assignee:
            assignee_label = assignee.get("email") or assignee.get("name")
        project = it.get("project")
        project_name = project.get("name") if project else None

        all_issues.append({
            "id": it["identifier"],
            "title": it["title"],
            "project": project_name,
            "status": state.get("name"),
            "statusType": status_type,
            "assignee": assignee_label,
            "url": it["url"],
        })

        key = STATUS_TYPE_KEY.get(status_type)
        if key:
            team_totals[key] += 1
            team_totals["total"] += 1

        if status_type == "started":
            updated = datetime.fromisoformat(it["updatedAt"].replace("Z", "+00:00"))
            days = (now - updated).days
            stuck.append({
                "id": it["identifier"],
                "title": it["title"],
                "status": state.get("name"),
                "statusType": status_type,
                "project": project_name,
                "assignee": assignee_label,
                "url": it["url"],
                "startedAt": it.get("startedAt"),
                "updatedAt": it["updatedAt"],
                "daysSinceUpdate": days,
            })

    stuck.sort(key=lambda x: -x["daysSinceUpdate"])

    proj_stats = {}
    for p in projects_raw:
        lead = p.get("lead")
        proj_stats[p["name"]] = {
            "name": p["name"],
            "lead": lead.get("name") if lead else None,
            "total": 0, "backlog": 0, "todo": 0, "started": 0,
            "completed": 0, "canceled": 0, "duplicate": 0,
        }

    for it in all_issues_raw:
        project = it.get("project")
        pname = project.get("name") if project else None
        if pname and pname in proj_stats:
            key = STATUS_TYPE_KEY.get(status_type_of(it.get("state") or {}))
            if key:
                proj_stats[pname][key] += 1
                proj_stats[pname]["total"] += 1

    out = {
        "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "workspace": WORKSPACE_LABEL,
        "team": TEAM_NAME,
        "teamTotals": team_totals,
        "stuck": stuck,
        "allIssues": all_issues,
        "projects": list(proj_stats.values()),
    }

    with open(OUTPUT_PATH, "w") as f:
        f.write("// Auto-generated by .github/workflows/linear-update.yml\n")
        f.write("// Do NOT hand-edit -- this file is overwritten on each run.\n")
        f.write("window.LINEAR_DATA = ")
        f.write(json.dumps(out, indent=2))
        f.write(";\n")

    print(f"Wrote {OUTPUT_PATH}: {team_totals['total']} issues, {len(stuck)} stuck, {len(proj_stats)} projects.")


if __name__ == "__main__":
    main()
