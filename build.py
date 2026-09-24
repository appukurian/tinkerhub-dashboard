import json, hashlib
from datetime import date, datetime

with open('/tmp/thd-publish/parsed-prev.json') as f:
    prev = json.load(f)

today = date(2026,9,24)

def sha_id(group, subject, counterpart):
    h = hashlib.sha1(f"{group}|{subject}|{counterpart}".encode('utf-8')).hexdigest()[:10]
    return f"{group}-{h}"

def d(s):
    return date.fromisoformat(s)

# 1. non-report threads carried from prior, minus report group
threads = [t for t in prev['threads'] if t['group'] != 'report']
print('non-report carried over:', len(threads))

# 2. apply fix to 1a0c9c15f6cf9e69
for t in threads:
    if t['threadId'] == '1a0c9c15f6cf9e69':
        t['status'] = 'Resolved'
        t['note'] = 'Habeeb declined further deadline extension for late project submission — final answer given, no further action needed'
        t['last'] = '2026-09-24'
        t['cc'] = ['campus@tinkerhub.org', 'femina@tinkerhub.org']
        t['daysOpen'] = (d(t['last']) - d(t['received'])).days
        t['daysSinceReceived'] = (today - d(t['received'])).days
        print('updated:', t)

# 3. fresh report threads (internal, unredacted)
report_raw = [
  dict(threadId="1a0cd7b3ab449267", subject="Request for a conversation — TinkerHub Internal Committee",
       counterpart="Sai Cheranjeeve . S", email="saicheranjeeves@gmail.com",
       received="2026-09-23", last="2026-09-23", status="Informational",
       note="IC (Arundhathi) confirmed a meeting with Sai for 4pm on 9/24 as part of the POSH investigation"),
  dict(threadId="1a0b073d8ca35649", subject="Complaint regarding unwelcome conduct",
       counterpart="Aiswarya Viswanath", email="aiswaryaviswanath2007@gmail.com",
       received="2026-09-17", last="2026-09-23", status="Awaiting reply (from us)",
       note="IC investigating complaint against Sai; meeting with him scheduled — we owe Aiswarya an outcome update after"),
  dict(threadId="1a07f972d58575c1", subject="OTP failing",
       counterpart="Shaphen Binu Kuriakose", email="shaphenbinukuriakose@gmail.com",
       received="2026-09-08", last="2026-09-23", status="Resolved",
       note="OTP issue resolved by support"),
  dict(threadId="1a07f8adc7a282ef", subject="",
       counterpart="Renil Augustine", email="augustinerenil723@gmail.com",
       received="2026-09-08", last="2026-09-23", status="Resolved",
       note="OTP rate-limit issue explained by support"),
  dict(threadId="1a07f81e530dfb34", subject="Tinkerhub not sending OTP for login",
       counterpart="Dev Anand VP", email="blindinglucario@gmail.com",
       received="2026-09-08", last="2026-09-23", status="Resolved",
       note="OTP rate-limit issue explained by support"),
  dict(threadId="1a06c6af7fac7d4f", subject="Complaint Regarding Participation Fee for Useless Project 3.0 at Sahrdaya College",
       counterpart="Arjun", email="kiki432005@proton.me",
       received="2026-09-04", last="2026-09-04", status="Awaiting reply (from us)",
       note="Arundhathi promised to follow up with the campus lead about the fee — no update sent yet, ~20 days stale"),
  dict(threadId="1a064036048dfc40", subject="",
       counterpart="Fidha", email="msx201700@gmail.com",
       received="2026-09-02", last="2026-09-02", status="No response",
       note="OTP login failure complaint — never replied to"),
  dict(threadId="1a042cdc25fcb5c6", subject="OTP Not Being Sent When Trying to Log In",
       counterpart="Athul Benedict", email="athulbenedict123@gmail.com",
       received="2026-08-27", last="2026-09-19", status="Resolved",
       note="Login issue resolved by support; asked user to confirm"),
  dict(threadId="1a037a3e72a46ef9", subject="Cannot login to page",
       counterpart="Gouri ES", email="gourishaju07@gmail.com",
       received="2026-08-25", last="2026-09-23", status="Resolved",
       note="Confirmed working by Gouri on 9/23"),
]

report_threads_full = []
for r in report_raw:
    rid = sha_id("report", r["subject"], r["counterpart"])
    recv = d(r["received"]); last = d(r["last"])
    daysSinceReceived = (today - recv).days
    daysOpen = (last - recv).days if r["status"] in ("Resolved","Informational") else (today - recv).days
    full = {
        "id": rid, "threadId": r["threadId"], "group": "report",
        "subject": r["subject"], "counterpart": r["counterpart"], "email": r["email"],
        "received": r["received"], "last": r["last"], "status": r["status"], "note": r["note"],
        "daysOpen": daysOpen, "daysSinceReceived": daysSinceReceived, "cc": []
    }
    report_threads_full.append(full)

# redacted version for publish
report_threads_redacted = []
for t in report_threads_full:
    report_threads_redacted.append({
        "id": t["id"], "threadId": None, "group": "report", "subject": None,
        "counterpart": None, "email": None, "received": None, "last": None,
        "status": t["status"], "note": None, "daysOpen": t["daysOpen"],
        "daysSinceReceived": t["daysSinceReceived"], "cc": []
    })

all_threads_internal = threads + report_threads_full  # for summary/analytics (real status)
all_threads_publish = threads + report_threads_redacted

print('total internal threads:', len(all_threads_internal))
print('total publish threads:', len(all_threads_publish))

# summary + analytics, matching the shape mailbox-dashboard.html's own
# client-side recompute() produces, for consistency with any direct readers.
STATUSES = ["No response","Awaiting reply (from us)","Awaiting reply (from them)","Resolved","Informational"]
OPEN_STATUSES = {"No response","Awaiting reply (from us)","Awaiting reply (from them)"}

summary = {s: 0 for s in STATUSES}
for t in all_threads_internal:
    summary[t['status']] = summary.get(t['status'], 0) + 1

groups = ["campus","support","finance","report","partner","tinkerspace"]
analytics = {}
for g in groups:
    gts = [t for t in all_threads_internal if t['group']==g]
    row = {"total": len(gts)}
    for s in STATUSES:
        row[s] = sum(1 for t in gts if t['status']==s)
    open_days = [t['daysOpen'] for t in gts if t['status'] in OPEN_STATUSES]
    resolved_days = [t['daysOpen'] for t in gts if t['status']=='Resolved']
    row["avgOpenDays"] = round(sum(open_days)/len(open_days),1) if open_days else None
    row["avgResolvedDays"] = round(sum(resolved_days)/len(resolved_days),1) if resolved_days else None
    analytics[g] = row

out = {
    "generatedAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    "threads": all_threads_publish,
    "summary": summary,
    "analytics": analytics
}

with open('/tmp/thd-publish/dashboard-data.js', 'w') as f:
    f.write("window.DASHBOARD_DATA = ")
    json.dump(out, f, indent=2, ensure_ascii=False)
    f.write(";\n")

print('DONE. summary=', dict(summary))
print('analytics=', json.dumps(analytics, indent=2))
