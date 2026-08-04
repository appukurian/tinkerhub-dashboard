#!/usr/bin/env python3
"""
Fetch tech-issue threads from a Discord text channel and build discord-data.js
for the TinkerHub dashboard repo.

Runs inside GitHub Actions (see .github/workflows/discord-update.yml), NOT in
any Claude sandbox — the bot token stays a GitHub Actions secret and is never
seen outside GitHub's own infrastructure.

INCREMENTAL BY DESIGN: once a thread is archived/locked (= resolved), its data
can never change again, so we never re-fetch it. Each run:
  1. Reads yesterday's discord-data.js (already checked out by the workflow)
     and keeps every thread already marked Resolved, as-is.
  2. Fully re-processes every currently ACTIVE thread (cheap, small set).
  3. Walks the archived-threads list newest-first and stops as soon as it
     hits a thread ID it already has recorded as Resolved — anything older
     is guaranteed to already be known, so only genuinely NEWLY-archived
     threads get the expensive per-thread message lookups.
On the very first run (no previous discord-data.js), this just degrades to a
full fetch of everything, same as before.

Required environment variables:
  DISCORD_BOT_TOKEN   - a Discord bot token (repo SECRET)
  DISCORD_GUILD_ID    - the Discord server (guild) ID (repo secret or variable)
  DISCORD_CHANNEL_ID  - the text channel ID to watch (repo secret or variable)

Bot needs, in that channel: View Channel, Read Message History, and the
privileged "Message Content Intent" enabled in the Discord Developer Portal
(Bot tab -> Privileged Gateway Intents) so it can read message text for the
similar-thread solution suggestions and issue categorization below.
"""

import os
import re
import json
import time
import collections
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
SNIPPET_MAX_LEN = 300
OUTPUT_PATH = "discord-data.js"

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

_missing = [name for name, val in [
    ("DISCORD_BOT_TOKEN", TOKEN),
    ("DISCORD_GUILD_ID", GUILD_ID),
    ("DISCORD_CHANNEL_ID", CHANNEL_ID),
] if not val]
if _missing:
    raise SystemExit(
        "Missing required config: " + ", ".join(_missing) +
        ". Set these under repo Settings -> Secrets and variables -> Actions "
        "(DISCORD_BOT_TOKEN as a Secret; GUILD_ID/CHANNEL_ID as either Secrets or Variables)."
    )

HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "User-Agent": "TinkerHubDiscordDashboard (https://github.com/appukurian/tinkerhub-dashboard, 1.0)",
}


def discord_get(path, retries=3):
    url = API + path
    for attempt in range(retries):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                body = json.loads(e.read().decode() or "{}")
                wait = body.get("retry_after", 1) + 0.5
                time.sleep(wait)
                continue
            raise RuntimeError(f"Discord API error {e.code} on {path}: {e.read().decode()}")
    raise RuntimeError(f"Gave up after {retries} retries on {path}")


def snowflake_to_dt(snowflake):
    ts_ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def parse_discord_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_date_str(s):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def load_previous_resolved():
    """Read the discord-data.js already checked out by the workflow (i.e.
    yesterday's output) and return {thread_id: record} for every thread
    already marked Resolved. Fails soft to {} if anything's missing/odd."""
    try:
        with open(OUTPUT_PATH, "r") as f:
            raw = f.read()
        m = re.search(r"window\.DISCORD_DATA\s*=\s*(\{.*\});?\s*$", raw, re.S)
        if not m:
            return {}
        data = json.loads(m.group(1))
        return {
            t["id"]: t
            for t in data.get("threads", [])
            if t.get("status") == "Resolved"
        }
    except Exception as e:
        print(f"Could not load previous discord-data.js (starting fresh): {e}")
        return {}


def fetch_active_threads():
    data = discord_get(f"/guilds/{GUILD_ID}/threads/active")
    return [t for t in data.get("threads", []) if str(t.get("parent_id")) == str(CHANNEL_ID)]


def fetch_new_archived_threads(known_resolved_ids, max_pages=30):
    """Walk the archived-threads list (newest-archived-first, per Discord's
    API ordering) and stop as soon as we hit a thread we already have
    recorded as Resolved — everything after that point is guaranteed old."""
    new_threads = []
    before = None
    for _ in range(max_pages):
        path = f"/channels/{CHANNEL_ID}/threads/archived/public?limit=100"
        if before:
            path += f"&before={urllib.parse.quote(before, safe='')}"
        data = discord_get(path)
        batch = data.get("threads", [])
        hit_known = False
        for t in batch:
            if t["id"] in known_resolved_ids:
                hit_known = True
                break
            new_threads.append(t)
        if hit_known or not data.get("has_more") or not batch:
            break
        before = batch[-1]["thread_metadata"]["archive_timestamp"]
    return new_threads


def boundary_message(thread_id, which):
    """which: 'first' or 'last'. Returns the message dict, or None."""
    if which == "first":
        path = f"/channels/{thread_id}/messages?after=0&limit=1"
    else:
        path = f"/channels/{thread_id}/messages?limit=1"
    try:
        msgs = discord_get(path)
    except RuntimeError:
        return None
    return msgs[0] if msgs else None


def author_display_name(msg, fallback_id):
    if not msg:
        return f"User {fallback_id}"
    author = msg.get("author", {}) or {}
    return author.get("global_name") or author.get("username") or f"User {fallback_id}"


def clean_snippet(text):
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > SNIPPET_MAX_LEN:
        text = text[:SNIPPET_MAX_LEN].rsplit(" ", 1)[0] + "…"
    return text


# --- Issue categorization -----------------------------------------------
# Ordered most-specific-first; first matching category wins. Tuned against
# the actual thread-title vocabulary seen in this channel.
CATEGORY_RULES = [
    ("OTP / Login issues", ["otp", "login", "log in", "password", "sign in", "signin"]),
    ("Koottam access issues", ["koottam", "kootam", "kootaam"]),
    ("Study Jam issues", ["study jam", "study jams", "jam"]),
    ("Registration issues", ["register", "registration", "kickstarter", "tinkherhack", "tink her hack", "participant"]),
    ("Role/tag display bugs", ["role", " tag ", "tag is", "tags", "core team", "learning coordinator", "duplicat", "multiple"]),
    ("Facilitator management", ["facilitator"]),
    ("Useless Projects (event)", ["useless project", "useless"]),
    ("Discord account/access", ["discord server", "discord account", "vouch", "invite", "discord"]),
    ("Event/Activity check-in & reporting", ["check in", "checkin", "attendance", "event", "activity"]),
    ("Project add/delete/submission", ["project", "submission"]),
    ("Name/Profile changes", ["name change", "profile", "rename", " name "]),
    ("Campus/College visibility", ["campus", "college"]),
    ("Deletion requests", ["delete", "deletion", "removal", "remove"]),
]


def classify_issue(name):
    lname = f" {name.lower()} "
    for label, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in lname:
                return label
    return "Other"


def collect_thread(thread):
    """Full processing (2 extra API calls) — only used for active threads and
    newly-discovered archived threads, never for already-known resolved ones."""
    thread_id = thread["id"]
    name = thread.get("name") or "(untitled thread)"
    owner_id = thread.get("owner_id")
    meta = thread.get("thread_metadata", {}) or {}
    archived = bool(meta.get("archived"))
    locked = bool(meta.get("locked"))
    archive_dt = parse_discord_dt(meta.get("archive_timestamp"))
    created_dt = snowflake_to_dt(thread_id)

    msg_count = thread.get("total_message_sent", thread.get("message_count", 0)) or 0

    first_msg = boundary_message(thread_id, "first")
    last_msg = boundary_message(thread_id, "last") or first_msg

    last_dt = snowflake_to_dt(last_msg["id"]) if last_msg else created_dt
    last_author_id = (last_msg.get("author", {}) or {}).get("id", owner_id) if last_msg else owner_id
    requester = author_display_name(first_msg, owner_id)

    if archived or locked:
        status = "Resolved"
    elif msg_count <= 1:
        status = "No response"
    elif str(last_author_id) == str(owner_id):
        status = "Awaiting reply (from us)"
    else:
        status = "Awaiting reply (from them)"

    resolved_at = archive_dt or (last_dt if status == "Resolved" else None)
    days_to_close = (resolved_at - created_dt).days if resolved_at else None

    now = datetime.now(timezone.utc)
    return {
        "id": thread_id,
        "name": name,
        "url": f"https://discord.com/channels/{GUILD_ID}/{thread_id}",
        "status": status,
        "category": classify_issue(name),
        "requester": requester,
        "received": created_dt.strftime("%Y-%m-%d"),
        "last": last_dt.strftime("%Y-%m-%d"),
        "resolvedAt": resolved_at.strftime("%Y-%m-%d") if resolved_at else None,
        "daysOpen": (now - last_dt).days,
        "daysSinceReceived": (now - created_dt).days,
        "daysToClose": days_to_close,
        "messageCount": msg_count,
        "archived": archived,
        "locked": locked,
        "resolutionSnippet": clean_snippet(last_msg.get("content")) if (status == "Resolved" and last_msg) else "",
    }


def refresh_relative_fields(record):
    """Cheaply recompute the 'now'-relative day counters on a carried-over
    (reused) record, without hitting the API again."""
    now = datetime.now(timezone.utc)
    last_dt = parse_date_str(record.get("last"))
    received_dt = parse_date_str(record.get("received"))
    if last_dt:
        record["daysOpen"] = (now - last_dt).days
    if received_dt:
        record["daysSinceReceived"] = (now - received_dt).days
    return record


def attach_suggestions(results):
    """For each non-resolved thread, point at the most recently resolved
    thread in the same category and surface its closing message as a
    suggested next step."""
    resolved_by_category = collections.defaultdict(list)
    for r in results:
        if r["status"] == "Resolved" and r.get("resolutionSnippet"):
            resolved_by_category[r["category"]].append(r)
    for cat in resolved_by_category:
        resolved_by_category[cat].sort(key=lambda r: r.get("resolvedAt") or r["last"], reverse=True)

    for r in results:
        r["suggestion"] = None
        if r["status"] == "Resolved":
            continue
        candidates = [c for c in resolved_by_category.get(r["category"], []) if c["id"] != r["id"]]
        if candidates:
            best = candidates[0]
            r["suggestion"] = {
                "fromThreadName": best["name"],
                "fromThreadUrl": best["url"],
                "snippet": best["resolutionSnippet"],
            }


def main():
    previous_resolved = load_previous_resolved()

    active = fetch_active_threads()
    active_ids = {t["id"] for t in active}

    # Anything previously resolved that's active again got reopened —
    # drop it from "known" so it gets freshly processed via the active list.
    known_resolved_ids = set(previous_resolved.keys()) - active_ids

    new_archived = fetch_new_archived_threads(known_resolved_ids)

    to_process = active + new_archived
    freshly_processed = [collect_thread(t) for t in to_process]

    processed_ids = {t["id"] for t in to_process}
    carried_over = [
        refresh_relative_fields(dict(rec))
        for tid, rec in previous_resolved.items()
        if tid not in processed_ids
    ]

    results = freshly_processed + carried_over
    attach_suggestions(results)
    results.sort(key=lambda r: r["last"], reverse=True)

    statuses = ["No response", "Awaiting reply (from us)", "Awaiting reply (from them)", "Resolved"]
    summary = {s: len([r for r in results if r["status"] == s]) for s in statuses}

    open_days = [r["daysOpen"] for r in results if r["status"] != "Resolved"]
    close_days = [r["daysToClose"] for r in results if r.get("daysToClose") is not None]
    avg_open = round(sum(open_days) / len(open_days), 1) if open_days else None
    avg_days_to_close = round(sum(close_days) / len(close_days), 1) if close_days else None

    category_counts = collections.Counter(r["category"] for r in results)
    top_categories = [{"category": c, "count": n} for c, n in category_counts.most_common(15)]

    requester_counts = collections.Counter(r["requester"] for r in results)
    top_requesters = [{"name": n, "count": c} for n, c in requester_counts.most_common(15)]

    out = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "threads": results,
        "summary": summary,
        "avgOpenDays": avg_open,
        "avgDaysToClose": avg_days_to_close,
        "topCategories": top_categories,
        "topRequesters": top_requesters,
    }

    with open(OUTPUT_PATH, "w") as f:
        f.write("// Auto-generated daily by .github/workflows/discord-update.yml\n")
        f.write("// Do NOT hand-edit — this file is overwritten on each run.\n")
        f.write("window.DISCORD_DATA = ")
        f.write(json.dumps(out, indent=2))
        f.write(";\n")

    print(
        f"Wrote discord-data.js with {len(results)} threads "
        f"({len(freshly_processed)} freshly processed, {len(carried_over)} carried over unchanged)."
    )
    print(f"Summary: {summary}")
    print(f"Avg days to close: {avg_days_to_close}. Top category: {top_categories[:3]}")


if __name__ == "__main__":
    main()
