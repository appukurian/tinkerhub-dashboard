#!/usr/bin/env python3
"""
One-shot schema discovery for the TinkerHub Metabase instance.

Runs inside GitHub Actions (see .github/workflows/metabase-discover.yml), NOT
in any Claude sandbox -- the API key stays a repo secret. This script exists
purely so a human/AI collaborator without direct Metabase access can inspect
the schema by reading the committed metabase-schema.json, instead of needing
live API access.

Required env vars:
  METABASE_URL       e.g. https://metabase.tinkerhub.org
  METABASE_API_KEY   Metabase personal/API key (X-API-KEY header)
"""
import os
import json
import urllib.request
import urllib.error

BASE = os.environ.get("METABASE_URL", "").rstrip("/")
KEY = os.environ.get("METABASE_API_KEY", "")
OUTPUT_PATH = "metabase-schema.json"

if not BASE or not KEY:
    raise SystemExit("Missing METABASE_URL or METABASE_API_KEY env vars (set as repo secrets).")

HEADERS = {"X-API-KEY": KEY, "Content-Type": "application/json"}


def mb_get(path):
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"GET {path} -> {e.code}: {body[:500]}")


def main():
    dbs = mb_get("/api/database?include=tables")
    dbs_list = dbs.get("data", dbs) if isinstance(dbs, dict) else dbs

    out_databases = []
    for db in dbs_list:
        db_id = db.get("id")
        db_entry = {
            "id": db_id,
            "name": db.get("name"),
            "engine": db.get("engine"),
            "tables": [],
        }
        tables = db.get("tables") or []
        for t in tables:
            table_id = t.get("id")
            table_entry = {
                "id": table_id,
                "name": t.get("name"),
                "display_name": t.get("display_name"),
                "schema": t.get("schema"),
                "fields": [],
            }
            try:
                meta = mb_get(f"/api/table/{table_id}/query_metadata")
                for f in meta.get("fields", []):
                    table_entry["fields"].append({
                        "name": f.get("name"),
                        "display_name": f.get("display_name"),
                        "base_type": f.get("base_type"),
                        "semantic_type": f.get("semantic_type"),
                    })
            except Exception as e:
                table_entry["error"] = str(e)
            db_entry["tables"].append(table_entry)
        out_databases.append(db_entry)

    with open(OUTPUT_PATH, "w") as f:
        json.dump({"databases": out_databases}, f, indent=2)

    total_tables = sum(len(d["tables"]) for d in out_databases)
    print(f"Wrote {OUTPUT_PATH}: {len(out_databases)} database(s), {total_tables} table(s).")


if __name__ == "__main__":
    main()
