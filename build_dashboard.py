#!/usr/bin/env python3
"""
Ananda Farm Summer Camp 2026 — Dashboard auto-refresh
=====================================================
Pulls ticket data from the TicketSpice (Webconnex) REST API, transforms it into
the structure the dashboard expects, and writes the result into index.html.

Run by .github/workflows/refresh-dashboard.yml on a daily schedule.

Maintenance owner: Ryan (Marketing / AI Lead)
Originally scaffolded June 2026.

IMPORTANT — first-run / field-discovery mode:
  The camper name, date of birth, and extended-care info live inside each
  ticket's `fieldData` array as CUSTOM form fields. The exact `path` strings
  are specific to the Ananda form and were not visible when this script was
  written. On run, the script tries to auto-detect them by label. If it cannot
  confidently find them, it PRINTS the available field paths/labels to the log
  (with values redacted) and exits without changing the dashboard, so you can
  fill in the FIELD_PATHS map below. See README_AUTOMATION.md.
"""

import os
import sys
import re
import json
import datetime as dt
from collections import defaultdict

import requests

# ──────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────
API_BASE = "https://api.webconnex.com/v2/public/search/tickets"
PRODUCT = "ticketspice.com"
FORM_ID = "990538"            # Ananda Farm Summer Camp 2026 form
PAGE_SIZE = 250               # API max per page
HTML_PATH = "index.html"      # file to update in the repo

# The API key comes from the environment (GitHub Actions secret), never hardcoded.
API_KEY = os.environ.get("TICKETSPICE_API_KEY", "").strip()

# TicketSpice numeric status codes → human strings the dashboard uses.
# Confirmed mapping may need adjustment; 3 = completed is the common Webconnex code.
STATUS_MAP = {
    "1": "pending offline payment",
    "2": "pre-registered",
    "3": "completed",
    "4": "canceled",
    "5": "refunded",
}

# Custom-field path mapping. LEAVE AS None to trigger auto-detection on first run.
# After the first run prints the available paths, set these to the exact `path`
# strings from your form, e.g. FIELD_PATHS["camperFirst"] = "camperName.firstName"
FIELD_PATHS = {
    "camperFirst": "camperFirstLastName.first",
    "camperLast": "camperFirstLastName.last",
    "dob": "dateOfBirth",
    # extended care is detected per-week from level labels, not a single field
}

# Set True to make the script print EVERY field path/label the live API returns,
# then exit WITHOUT changing the dashboard. Use this to discover the real paths,
# fill in FIELD_PATHS above, then set this back to False.
DISCOVERY_MODE = False

# Heuristics used to auto-detect the custom fields if FIELD_PATHS not set.
# "all" = every term must appear; "any" = at least one term must appear.
DETECT_HINTS = {
    "camperFirst": (["camper", "first"], "all"),
    "camperLast": (["camper", "last"], "all"),
    "dob": (["birth", "dob", "date of birth"], "any"),
}

WEEK_DATES = {
    1: "June 1–5", 2: "June 8–12", 3: "June 15–19", 4: "June 22–26",
    5: "July 6–10", 6: "July 13–17", 7: "July 20–24", 8: "July 27–31",
    9: "August 3–7",
}

# Bundle level labels → (weeks, day type)
BUNDLE_WEEKS = {
    "Session 1 Bundle (Full Day, Weeks 1–4) — $275/Week": (range(1, 5), "Full Day"),
    "Session 2 Bundle (Full Day, Weeks 5–9) — $275/Week": (range(5, 10), "Full Day"),
    "Full Summer Bundle (Full Day, Weeks 1–9) —$275/Week": (range(1, 10), "Full Day"),
    "Half Day Full Summer Bundle (Weeks 1–9) — $175/Week": (range(1, 10), "Half Day"),
    "Half Day Bundle (Weeks 1–4) — $175/week": (range(1, 5), "Half Day"),
    "Half Day Bundle (Weeks 5–9) — $175/Week": (range(5, 10), "Half Day"),
}
EC_BUNDLE_WEEKS = {
    "ADD-ON: Extended Care Bundle (Session 1) — $50/Week": range(1, 5),
    "ADD-ON: Extended Care Bundle (Session 2) — $50/Week": range(5, 10),
    "ADD-ON: Extended Care Bundle (Full Summer) — $50/Week": range(1, 10),
}
SINGLE_WEEK_RE = re.compile(r'Week (\d) \(.*?\) — (Full Day|Half Day) Camp')
EC_SINGLE_RE = re.compile(r'Week (\d) \(.*?\) — ADD-ON: Extended Care')


# ──────────────────────────────────────────────────────────────────────────
# API FETCH
# ──────────────────────────────────────────────────────────────────────────
def fetch_all_tickets():
    """Page through the API and return all ticket records for the form."""
    if not API_KEY:
        sys.exit("ERROR: TICKETSPICE_API_KEY env var is empty. "
                 "Set it as a GitHub Actions secret.")
    headers = {"apiKey": API_KEY}
    all_records = []
    start = 0
    while True:
        params = {
            "product": PRODUCT,
            "formId": FORM_ID,
            "limit": PAGE_SIZE,
            "start": start,
        }
        resp = requests.get(API_BASE, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            sys.exit(f"ERROR: API returned {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        batch = payload.get("data", [])
        all_records.extend(batch)
        total = payload.get("totalResults", len(all_records))
        start += len(batch)
        if len(batch) == 0 or start >= total:
            break
    print(f"Fetched {len(all_records)} ticket records from API.")
    return all_records


# ──────────────────────────────────────────────────────────────────────────
# FIELD DISCOVERY
# ──────────────────────────────────────────────────────────────────────────
def index_field_data(record):
    """Return {path: value, label_lower: value} for a record's fieldData array."""
    by_path = {}
    by_label = {}
    for item in record.get("fieldData", []):
        path = item.get("path", "")
        label = item.get("label", "")
        # value may live under different keys depending on field type
        value = item.get("value") or item.get("amount") or ""
        if path:
            by_path[path] = value
        if label:
            by_label[label.lower()] = value
    return by_path, by_label


def resolve_field_paths(records):
    """If FIELD_PATHS not set, try to auto-detect them by scanning labels.
    Returns a resolved dict or None if detection failed."""
    if DISCOVERY_MODE:
        seen = {}
        for rec in records:
            for item in rec.get("fieldData", []):
                p = item.get("path", "")
                l = (item.get("label", "") or "").lower()
                if p:
                    seen[p] = l
        print("=" * 60)
        print("DISCOVERY MODE — all field paths returned by the live API:")
        print("=" * 60)
        for path, label in sorted(seen.items()):
            print(f"  path={path!r}  label={label!r}")
        print("=" * 60)
        sys.exit("DISCOVERY_MODE is on — dashboard left unchanged. "
                 "Fill in FIELD_PATHS, set DISCOVERY_MODE=False, re-run.")
    if all(FIELD_PATHS.get(k) for k in ("camperFirst", "camperLast", "dob")):
        return dict(FIELD_PATHS)

    # collect all (path, label) pairs seen across records
    seen = {}
    for rec in records:
        for item in rec.get("fieldData", []):
            p = item.get("path", "")
            l = (item.get("label", "") or "").lower()
            if p:
                seen[p] = l

    resolved = dict(FIELD_PATHS)
    for key, (hints, mode) in DETECT_HINTS.items():
        if resolved.get(key):
            continue
        for path, label in seen.items():
            hay = (path + " " + label).lower()
            match = all(h in hay for h in hints) if mode == "all" else any(h in hay for h in hints)
            if match:
                resolved[key] = path
                break

    if all(resolved.get(k) for k in ("camperFirst", "camperLast", "dob")):
        print("Auto-detected custom field paths:")
        for k in ("camperFirst", "camperLast", "dob"):
            print(f"  {k}: {resolved[k]}")
        return resolved

    # Detection failed — print available fields (paths + labels only, NO values)
    print("\n" + "=" * 60)
    print("FIELD DISCOVERY MODE — could not auto-detect camper fields.")
    print("Below are the custom field paths/labels found in your form.")
    print("Copy the correct ones into FIELD_PATHS at the top of the script.")
    print("(Values are intentionally NOT printed, to protect camper data.)")
    print("=" * 60)
    for path, label in sorted(seen.items()):
        print(f"  path = {path!r:45}  label = {label!r}")
    print("=" * 60)
    return None


# ──────────────────────────────────────────────────────────────────────────
# TRANSFORM
# ──────────────────────────────────────────────────────────────────────────
def parse_age(dob_str):
    if not dob_str:
        return None, "unknown"
    try:
        # accept YYYY-MM-DD or ISO timestamp
        d = dob_str[:10]
        dob = dt.date.fromisoformat(d)
        ref = dt.date(2026, 6, 1)
        age = (ref - dob).days // 365
        if age < 5:
            return age, "under5"
        if age <= 7:
            return age, "5-7"
        if age <= 11:
            return age, "8-11"
        if age == 12:
            return age, "12"
        return age, "over12"
    except Exception:
        return None, "unknown"


def to_float(x):
    try:
        return float(str(x).replace(",", "").replace("$", ""))
    except Exception:
        return 0.0


def build_snapshot(records, paths):
    # Pass 1 — extended-care flags + per-week EC revenue by order
    ec_weeks = defaultdict(set)
    ec_rev = defaultdict(float)
    for rec in records:
        status = STATUS_MAP.get(str(rec.get("status", "")), str(rec.get("status", "")))
        if status == "canceled":
            continue
        lvl = (rec.get("levelLabel") or "").strip()
        oid = str(rec.get("orderId", ""))
        price = to_float(rec.get("total") or rec.get("amount"))
        m = EC_SINGLE_RE.match(lvl)
        if m:
            wk = int(m.group(1))
            ec_weeks[oid].add(wk)
            ec_rev[(oid, wk)] += price
        elif lvl in EC_BUNDLE_WEEKS:
            wks = list(EC_BUNDLE_WEEKS[lvl])
            per = round(price / len(wks), 2)
            for w in wks:
                ec_weeks[oid].add(w)
                ec_rev[(oid, w)] += per

    # Pass 2 — camper rows
    # seen_camper_week deduplicates when the API returns both a bundle-level
    # record AND individual per-week records for the same registration.
    # Key: (orderId, normalizedName_lower, week)
    rows = []
    seen_camper_week = set()
    for rec in records:
        status = STATUS_MAP.get(str(rec.get("status", "")), str(rec.get("status", "")))
        if status == "canceled":
            continue
        lvl = (rec.get("levelLabel") or "").strip()
        if "ADD-ON" in lvl:
            continue

        by_path, _ = index_field_data(rec)
        cf = str(by_path.get(paths["camperFirst"], "")).strip()
        cl = str(by_path.get(paths["camperLast"], "")).strip()
        dob = str(by_path.get(paths["dob"], "")).strip()
        age, age_grp = parse_age(dob)

        billing = rec.get("billing", {}) or {}
        pf = (billing.get("firstName") or "").strip()
        pl = (billing.get("lastName") or "").strip()
        pe = (rec.get("orderEmail") or "").strip()
        pp = (billing.get("phone") or "").strip()
        oid = str(rec.get("orderId", ""))
        onum = (rec.get("orderNumber") or "").strip()
        tid = (rec.get("displayId") or "").strip()
        sold = (rec.get("dateCreated") or "")[:19].replace("T", " ")
        total = to_float(rec.get("total") or rec.get("amount"))

        base = dict(
            camperFirst=cf, camperLast=cl,
            normalizedName=(cf + " " + cl).strip(),
            dob=dob or None, age=age, ageGroup=age_grp,
            parentFirst=pf, parentLast=pl, parentEmail=pe, parentPhone=pp,
            orderId=oid, orderNumber=onum, ticketId=tid,
            status=status, soldDate=sold,
        )

        m = SINGLE_WEEK_RE.match(lvl)
        if m:
            wk, dtp = int(m.group(1)), m.group(2)
            dk = (oid, (cf + cl).lower(), wk)
            if dk in seen_camper_week:
                continue
            seen_camper_week.add(dk)
            ecr = ec_rev.get((oid, wk), 0.0)
            rows.append({**base, "week": wk, "weekDates": WEEK_DATES[wk],
                         "dayType": dtp, "extendedCare": wk in ec_weeks.get(oid, set()),
                         "ticketPrice": round(total + ecr, 2)})
            continue
        if lvl in BUNDLE_WEEKS:
            wk_range, dtp = BUNDLE_WEEKS[lvl]
            wks = list(wk_range)
            per = round(total / len(wks), 2)
            for wk in wks:
                dk = (oid, (cf + cl).lower(), wk)
                if dk in seen_camper_week:
                    continue
                seen_camper_week.add(dk)
                ecr = ec_rev.get((oid, wk), 0.0)
                rows.append({**base, "week": wk, "weekDates": WEEK_DATES[wk],
                             "dayType": dtp, "extendedCare": wk in ec_weeks.get(oid, set()),
                             "ticketPrice": round(per + ecr, 2)})
            continue
        # Unknown level — skip but log
        print(f"  WARNING: unrecognized level label, skipped: {lvl!r}")

    # ── Aggregates ──
    print(f"  Raw API records fetched: {len(records)}")
    print(f"  Camper-week rows after dedup: {len(rows)}")
    # Include pre-registered in unique-camper count (they have a real spot)
    active = [r for r in rows if r["status"] in ("completed", "pending offline payment", "pre-registered")]
    for_stats = [r for r in rows if r["status"] in ("completed", "pending offline payment")]
    unique = len(set(r["normalizedName"] for r in active if r["normalizedName"]))
    total_weeks = len(for_stats)
    # Sanity guard — known-good ceiling: 9 weeks × 40 campers max = 360
    if total_weeks > 360 or unique > 220:
        import sys
        print(f"SANITY GUARD TRIGGERED: {unique} unique campers, {total_weeks} camp-weeks — "
              f"numbers implausibly high. Dashboard left unchanged.")
        sys.exit(1)
    total_rev = round(sum(r["ticketPrice"] for r in for_stats), 2)
    paid_rev = round(sum(r["ticketPrice"] for r in for_stats if r["status"] == "completed"), 2)
    pend_rev = round(sum(r["ticketPrice"] for r in for_stats if r["status"] == "pending offline payment"), 2)

    wk_map = {}
    for wk in range(1, 10):
        wk_map[wk] = dict(week=wk, weekDates=WEEK_DATES[wk], total=0, fullDay=0,
                          halfDay=0, extendedCare=0, revenue=0.0, age5_7=0,
                          age8_11=0, age12=0, ageUnder5=0, ageOver12=0, ageUnknown=0)
    for r in for_stats:
        w = wk_map[r["week"]]
        w["total"] += 1
        if r["dayType"] == "Full Day":
            w["fullDay"] += 1
        else:
            w["halfDay"] += 1
        if r["extendedCare"]:
            w["extendedCare"] += 1
        w["revenue"] = round(w["revenue"] + r["ticketPrice"], 2)
        ag = r["ageGroup"]
        key = {"5-7": "age5_7", "8-11": "age8_11", "12": "age12",
               "under5": "ageUnder5", "over12": "ageOver12"}.get(ag, "ageUnknown")
        w[key] += 1

    weeks_at_risk = [wk for wk in range(1, 10) if wk_map[wk]["total"] < 20]

    # ── Data quality ──
    oor, seen_oor = [], set()
    for r in active:
        if r["ageGroup"] in ("under5", "over12") and r["age"] is not None:
            k = (r["normalizedName"], r["week"])
            if k not in seen_oor:
                oor.append({"name": r["normalizedName"], "age": r["age"], "week": r["week"]})
                seen_oor.add(k)
    pre_reg = [{"name": r["normalizedName"], "week": r["week"], "parentEmail": r["parentEmail"]}
               for r in rows if r["status"] == "pre-registered"]
    pend_off = list(set(r["parentEmail"] for r in active
                        if r["status"] == "pending offline payment"))
    missing_dob = list(set(r["normalizedName"] for r in active if not r["dob"]))

    today = dt.date.today().isoformat()
    return {
        "generatedAt": dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "dataNote": f"Auto-refreshed from TicketSpice API — {today}",
        "summary": {
            "uniqueCampers": unique,
            "totalCampWeeks": total_weeks,
            "totalRevenue": total_rev,
            "paidRevenue": paid_rev,
            "pendingRevenue": pend_rev,
            "weeksAtRisk": weeks_at_risk,
        },
        "weekStats": list(wk_map.values()),
        "camperRows": rows,
        "dataQuality": {
            "outOfRangeAge": oor,
            "preRegistered": pre_reg,
            "pendingOfflinePayment": pend_off,
            "missingDob": missing_dob,
            "possibleDuplicates": [],
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# INJECT INTO HTML
# ──────────────────────────────────────────────────────────────────────────
def inject(snapshot):
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()

    start_marker = "const SNAPSHOT_DATA = "
    if start_marker not in html:
        sys.exit("ERROR: could not find SNAPSHOT_DATA marker in index.html.")
    start = html.index(start_marker)
    # find the terminating ';' that precedes the State section
    end_anchor = html.index("// ─── State", start)
    # back up to the ';' just before that comment
    semi = html.rindex(";", start, end_anchor)
    new_block = start_marker + json.dumps(snapshot, separators=(",", ":")) + ";"
    new_html = html[:start] + new_block + html[semi + 1:]

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(new_html)
    s = snapshot["summary"]
    print(f"Dashboard updated: {s['uniqueCampers']} campers, "
          f"{s['totalCampWeeks']} camp-weeks, ${s['totalRevenue']:,.0f}.")


# ──────────────────────────────────────────────────────────────────────────
def main():
    records = fetch_all_tickets()
    if not records:
        sys.exit("ERROR: API returned no records. Aborting (dashboard unchanged).")
    paths = resolve_field_paths(records)
    if paths is None:
        # discovery mode already printed the fields; exit WITHOUT touching html
        sys.exit("Field discovery needed — see paths above, fill in FIELD_PATHS, "
                 "and re-run. Dashboard left unchanged.")
    snapshot = build_snapshot(records, paths)
    if snapshot["summary"]["uniqueCampers"] == 0:
        sys.exit("ERROR: 0 campers after transform — likely a field-mapping issue. "
                 "Dashboard left unchanged.")
    inject(snapshot)


if __name__ == "__main__":
    main()
