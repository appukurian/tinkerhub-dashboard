#!/usr/bin/env python3
"""
Fetch tech-issue threads from a Discord text channel and build discord-data.js
for the TinkerHub dashboard repo.

Runs inside GitHub Actions (see .github/workflows/discord-update.yml), NOT in
any Claude sandbox — the bot token stays a GitHub Actions secret and is never
seen outside GitHub's own infrastructure.

Required environment variables:
  DISCORD_BOT_TOKEN   - a Discord bot token (repo SECRET)
  DISCORD_GUILD_ID    - the Discord server (guild) ID (repo VARIABLE, not sensitive)
  DISCORD_CHANNEL_ID  - the text channel ID to watch (repo VARIABLE, not sensitive)

Bot needs, in that channel: View Channel, Read Message History.
No privileged "Message Content" intent is required — we only look at message
authorship/timestamps, never message text.
"""

import os
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000

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
                # Rate limited — back off using Retry-After if present.
                body = json.loads(e.read().decode() or "{}")
                wait = body.get("retry_after", 1) + 0.5
                time.sleep(wait)
                continue
            raise RuntimeError(f"Discord API error {e.code} on {path}: {e.read().decode()}")
    raise RuntimeError(f"Gave up after {retries} retries on {path}")


def snowflake_to_dt(snowflake):
    ts_ms = (int(snowflake) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def fetch_active_threads():
    data = discord_get(f"/guilds/{GUILD_ID}/threads/active")
    return [t for t in data.get("threads", []) if str(t.get("parent_id")) == str(CHANNEL_ID)]


def fetch_archived_threads(max_pages=20):
    threads = []
    before = None
    for _ in range(max_pages):
        path = f"/channels/{CHANNEL_ID}/threads/archived/public?limit=100"
        if before:
            path += f"&before={before}"
        data = discord_get(path)
        batch = data.get("threads", [])
        threads.extend(batch)
        if not data.get("has_more") or not batch:
            break
        before = batch[-1]["thread_metadata"]["archive_timestamp"]
    return threads


def classify(thread):
    thread_id = thread["id"]
    name = thread.get("name") or "(untitled thread)"
    owner_id = thread.get("owner_id")
    meta = thread.get("thread_metadata", {}) or {}
    archived = bool(meta.get("archived"))
    locked = bool(meta.get("locked"))
    created_dt = snowflake_to_dt(thread_id)

    msg_count = thread.get("total_message_sent", thread.get("message_count", 0)) or 0

    last_author_id = owner_id
    last_dt = created_dt
    try:
        latest = discord_get(f"/channels/{thread_id}/messages?limit=1")
        if latest:
            last_msg = latest[0]
            last_dt = snowflake_to_dt(last_msg["id"])
            last_author_id = last_msg.get("author", {}).get("id", owner_id)
    except RuntimeError:
        # If we can't read messages (e.g. permission hiccup), fall back to thread-level info.
        pass

    if archived or locked:
        status = "Resolved"
    elif msg_count <= 1:
        status = "No response"
    elif str(last_author_id) == str(owner_id):
        status = "Awaiting reply (from us)"
    else:
        status = "Awaiting reply (from them)"

    now = datetime.now(timezone.utc)
    return {
        "id": thread_id,
        "name": name,
        "url": f"https://discord.com/channels/{GUILD_ID}/{thread_id}",
        "status": status,
        "received": created_dt.strftime("%Y-%m-%d"),
        "last": last_dt.strftime("%Y-%m-%d"),
        "daysOpen": (now - last_dt).days,
        "daysSinceReceived": (now - created_dt).days,
        "messageCount": msg_count,
        "archived": archived,
        "locked": locked,
    }


def main():
    active = fetch_active_threads()
    archived = fetch_archived_threads()
    all_threads = active + archived

    results = [classify(t) for t in all_threads]
    # newest activity first
    results.sort(key=lambda r: r["last"], reverse=True)

    statuses = ["No response", "Awaiting reply (from us)", "Awaiting reply (from them)", "Resolved"]
    summary = {s: len([r for r in results if r["status"] == s]) for s in statuses}

    open_days = [r["daysOpen"] for r in results if r["status"] != "Resolved"]
    resolved_days = [r["daysOpen"] for r in results if r["status"] == "Resolved"]
    avg_open = round(sum(open_days) / len(open_days), 1) if open_days else None
    avg_resolved = round(sum(resolved_days) / len(resolved_days), 1) if resolved_days else None

    out = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "threads": results,
        "summary": summary,
        "avgOpenDays": avg_open,
        "avgResolvedDays": avg_resolved,
    }

    with open("discord-data.js", "w") as f:
        f.write("// Auto-generated daily by .github/workflows/discord-update.yml\n")
        f.write("// Do NOT hand-edit — this file is overwritten on each run.\n")
        f.write("window.DISCORD_DATA = ")
        f.write(json.dumps(out, indent=2))
        f.write(";\n")

    print(f"Wrote discord-data.js with {len(results)} threads. Summary: {summary}")


if __name__ == "__main__":
    main()
