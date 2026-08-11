#!/usr/bin/env python3
"""
apply_manual_resolutions.py
───────────────────────────
Reads manual-resolutions.json and patches dashboard-data.js in-place,
stamping every listed mailbox thread with:
    "manuallyClosed": true,
    "originalStatus": "<previous status>",   (only if not already Resolved)
    "status": "Resolved"

Threads that are no longer in the manual list get manuallyClosed: false and
their original status restored (from "originalStatus" if present).

Run automatically by .github/workflows/apply-manual-resolutions.yml whenever
manual-resolutions.json changes, or can be triggered manually via
workflow_dispatch.

No external dependencies — pure Python 3 stdlib.
"""

import json
import re
import sys
from pathlib import Path

MANUAL_FILE  = Path("manual-resolutions.json")
MAILBOX_FILE = Path("dashboard-data.js")


def load_manual_ids() -> set:
    if not MANUAL_FILE.exists():
        print(f"[apply-resolutions] {MANUAL_FILE} not found — nothing to do.")
        return set()
    data = json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
    ids = data.get("mailbox", [])
    return set(str(i) for i in ids)


def load_mailbox_data() -> dict:
    text = MAILBOX_FILE.read_text(encoding="utf-8")
    # Strip the JS wrapper: window.DASHBOARD_DATA = {...};
    m = re.search(r"window\.DASHBOARD_DATA\s*=\s*(\{.*\})\s*;", text, re.DOTALL)
    if not m:
        sys.exit(f"[apply-resolutions] Could not parse {MAILBOX_FILE}.")
    return json.loads(m.group(1))


def write_mailbox_data(data: dict) -> None:
    blob = json.dumps(data, indent=2, ensure_ascii=False)
    MAILBOX_FILE.write_text(
        "// Auto-patched by scripts/apply_manual_resolutions.py\n"
        "// Do NOT hand-edit the manuallyClosed / originalStatus fields;\n"
        "// edit manual-resolutions.json instead.\n"
        f"window.DASHBOARD_DATA = {blob};\n",
        encoding="utf-8",
    )


def main():
    manual_ids = load_manual_ids()
    mailbox_data = load_mailbox_data()
    threads = mailbox_data.get("threads", [])

    changed = 0
    for t in threads:
        tid = str(t.get("id", ""))
        if tid in manual_ids:
            # Mark as manually resolved
            if not t.get("manuallyClosed"):
                if t.get("status") != "Resolved":
                    t["originalStatus"] = t["status"]
                    t["status"] = "Resolved"
                t["manuallyClosed"] = True
                changed += 1
        else:
            # Remove any previous manual override
            if t.get("manuallyClosed"):
                if t.get("originalStatus"):
                    t["status"] = t.pop("originalStatus")
                t["manuallyClosed"] = False
                changed += 1
            else:
                t.setdefault("manuallyClosed", False)

    write_mailbox_data(mailbox_data)
    print(
        f"[apply-resolutions] Done. {changed} thread(s) changed. "
        f"Manual IDs active: {len(manual_ids)}."
    )


if __name__ == "__main__":
    main()
