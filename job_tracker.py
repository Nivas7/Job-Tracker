#!/usr/bin/env python3
"""
Daily job tracker: fetch jobs from career sites, filter for entry-level
software roles in India, and sync to Google Sheets (Active / Archive tabs).

Usage:
  python job_tracker.py --dry-run                 # fetch + filter, print only
  python job_tracker.py --dry-run --only amazon   # test one site
  python job_tracker.py                           # fetch + filter + sync to Sheets

Env vars (for Sheets sync):
  GOOGLE_SERVICE_ACCOUNT_JSON   full JSON of the service account key
  SHEET_ID                      the id from the Google Sheet URL
"""
import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone

import requests

# ----------------------------------------------------------------------------
# FILTER CONFIG - edit these to tune what you see
# ----------------------------------------------------------------------------
TITLE_INCLUDE = re.compile(
    r"software (development )?engineer|\bsde\b|developer|full[- ]?stack|"
    r"back[- ]?end|front[- ]?end|member of technical staff|\bmts\b|"
    r"application engineer",
    re.I,
)
TITLE_EXCLUDE = re.compile(
    r"senior|\bsr\b|staff|principal|\blead\b|manager|director|architect|"
    r"head of|distinguished|\bintern\b|internship|\bsdet\b|\bqa\b|quality|"
    r"\b(engineer|developer|sde)\s*(iii|iv|v|3|4|5)\b",
    re.I,
)
# Max "years of experience" a posting may ask for (0-1, 1-2 => 2).
MAX_YEARS_REQUIRED = 2
# Location must match one of these (India only).
LOCATION_OK = re.compile(
    r"india|bengaluru|bangalore|chennai|hyderabad|pune|mumbai|gurgaon|gurugram|"
    r"noida|delhi|kolkata|coimbatore|kochi|ahmedabad|trivandrum|thiruvananthapuram",
    re.I,
)
# Skills from your resume. Matched against the job description.
SKILL_PATTERNS = {
    "Java": r"\bjava\b(?!\s*script)",
    "JavaScript": r"javascript|\bjs\b",
    "TypeScript": r"typescript",
    "Python": r"\bpython\b",
    "Go": r"\bgolang\b",
    "React": r"\breact(\.?js)?\b",
    "React Native": r"react[- ]native",
    "Next.js": r"next\.?js",
    "Node.js": r"node\.?js|\bnode\b",
    "Flask": r"\bflask\b",
    "AWS": r"\baws\b|amazon web services",
    "DynamoDB": r"dynamodb",
    "MongoDB": r"mongo ?db",
    "Redis": r"\bredis\b",
    "REST": r"\brest(ful)?\b",
    "WebSockets": r"websockets?",
}
SKILL_RX = {k: re.compile(v, re.I) for k, v in SKILL_PATTERNS.items()}
# Need at least this many skills in the description. Only applied when we
# have a full description (listing-only teasers are too short to judge).
MIN_SKILL_MATCHES = 1
FULL_DESC_MIN_CHARS = 400

# More companies with public job-board APIs. Add tokens freely; a wrong
# token just logs an error and is skipped.
GREENHOUSE = {"MongoDB": "mongodb", "Databricks": "databricks", "Stripe": "stripe",
              "Cloudflare": "cloudflare", "Elastic": "elastic"}
LEVER = {"CRED": "cred", "Meesho": "meesho"}

HEADERS = ["ID", "Company", "Title", "Location", "Skills matched", "Score",
           "Years req", "Posted", "First seen", "Link", "Status", "Notes"]

# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
})


def _request(method, url, **kw):
    for attempt in range(3):
        try:
            r = S.request(method, url, timeout=30, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def get(url, **kw):
    return _request("GET", url, **kw)


def post(url, **kw):
    return _request("POST", url, **kw)


def clean(text):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_date(ts):
    try:
        if isinstance(ts, (int, float)):
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        return str(ts or "")[:10]
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# Site adapters. Each returns a list of dicts:
# id, company, title, location, url, posted, desc, [req_text], [detail_fn]
# ----------------------------------------------------------------------------
def amazon():
    out, offset = [], 0
    while True:
        r = get("https://www.amazon.jobs/en/search.json", params={
            "base_query": "", "loc_query": "India", "country": "IND",
            "category[]": "software-development", "sort": "recent",
            "offset": offset, "result_limit": 100,
        }).json()
        jobs = r.get("jobs", [])
        for j in jobs:
            basic = clean(j.get("basic_qualifications"))
            pref = clean(j.get("preferred_qualifications"))
            out.append(dict(
                id="amzn-" + str(j.get("id_icims") or j.get("id")),
                company="Amazon", title=j.get("title", ""),
                location=j.get("normalized_location") or j.get("location", ""),
                url="https://www.amazon.jobs" + j.get("job_path", ""),
                posted=j.get("posted_date", ""),
                desc=" ".join([clean(j.get("description_short")), basic, pref]),
                req_text=basic,  # years check uses basic quals only
            ))
        offset += len(jobs)
        if not jobs or offset >= r.get("hits", 0):
            break
        time.sleep(1)
    return out


def atlassian():
    data = get("https://www.atlassian.com/endpoint/careers/listings").json()
    out = []
    for j in data:
        locs = j.get("locations") or []
        loc = "; ".join(locs) if isinstance(locs, list) else str(locs)
        desc = clean(" ".join(str(j.get(k) or "") for k in
                              ("overview", "responsibilities", "qualifications")))
        out.append(dict(
            id="atl-" + str(j.get("id")), company="Atlassian",
            title=j.get("title", ""), location=loc,
            url=j.get("applyUrl") or
            f"https://www.atlassian.com/company/careers/details/{j.get('id')}",
            posted="", desc=desc,
        ))
    return out


def microsoft():
    base = "https://apply.careers.microsoft.com"
    out, start = [], 0
    while start < 1000:
        r = get(base + "/api/pcsx/search", params={
            "domain": "microsoft.com", "query": "software engineer",
            "location": "India", "start": start, "sort_by": "distance",
            "filter_include_remote": 1,
        }).json()
        pos = (r.get("data") or {}).get("positions", [])
        if not pos:
            break
        for p in pos:
            pid = p.get("id")

            def detail(pid=pid):
                d = get(base + "/api/pcsx/position_details", params={
                    "position_id": pid, "domain": "microsoft.com", "hl": "en",
                }).json()
                return clean((d.get("data") or {}).get("jobDescription"))

            out.append(dict(
                id=f"msft-{pid}", company="Microsoft", title=p.get("name", ""),
                location="; ".join(p.get("locations") or []),
                url=base + (p.get("positionUrl") or f"/careers/job/{pid}"),
                posted=to_date(p.get("postedTs")), desc="", detail_fn=detail,
            ))
        start += len(pos)
        time.sleep(1)
    return out


def phenom(company, host, path, locale, country, keywords=""):
    """Phenom People career sites (Cisco, Blue Yonder)."""
    out, frm, size = [], 0, 50
    while True:
        payload = {
            "lang": locale, "deviceType": "desktop", "country": country,
            "pageName": "search-results", "ddoKey": "refineSearch",
            "sortBy": "", "subsearch": "", "from": frm, "jobs": True,
            "counts": True, "all_fields": ["category", "country", "state", "city", "type"],
            "size": size, "clearAll": False, "jdsource": "facets",
            "isSliderEnable": False, "pageId": "page11", "siteType": "external",
            "keywords": keywords, "global": True,
            "selected_fields": {"country": ["India"]},
            "sort": {"order": "desc", "field": "postedDate"}, "locationData": {},
        }
        r = post(f"https://{host}/widgets", json=payload).json()
        rs = r.get("refineSearch") or {}
        jobs = (rs.get("data") or {}).get("jobs", [])
        for j in jobs:
            jid = j.get("jobId") or j.get("reqId")
            loc = j.get("location") or ", ".join(
                x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
            out.append(dict(
                id=f"{company.lower().replace(' ', '')}-{jid}", company=company,
                title=j.get("title", ""), location=loc,
                url=f"https://{host}/{path}/job/{jid}",
                posted=to_date(j.get("postedDate")),
                desc=clean(j.get("descriptionTeaser")),
            ))
        frm += len(jobs)
        if not jobs or frm >= rs.get("totalHits", 0):
            break
        time.sleep(1)
    return out


def cisco():
    seen, out = set(), []
    # Your two Cisco links: all India jobs, and keyword "Software".
    for kw in ("", "Software"):
        for j in phenom("Cisco", "careers.cisco.com", "global/en", "en_global", "global", kw):
            if j["id"] not in seen:
                seen.add(j["id"])
                out.append(j)
    return out


def blueyonder():
    return phenom("Blue Yonder", "careers.blueyonder.com", "us/en", "en_us", "us")


def greenhouse(company, token):
    r = get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
            params={"content": "true"}).json()
    return [dict(
        id=f"gh-{token}-{j['id']}", company=company, title=j.get("title", ""),
        location=(j.get("location") or {}).get("name", ""),
        url=j.get("absolute_url", ""), posted=to_date(j.get("updated_at")),
        desc=clean(j.get("content")),
    ) for j in r.get("jobs", [])]


def lever(company, slug):
    r = get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json()
    return [dict(
        id=f"lv-{slug}-{j['id']}", company=company, title=j.get("text", ""),
        location=(j.get("categories") or {}).get("location", "") or "",
        url=j.get("hostedUrl", ""), posted=to_date(j.get("createdAt")),
        desc=clean(j.get("descriptionPlain") or j.get("description")),
    ) for j in r]


# NOT included yet: Google and Apple. Their career sites have no stable
# public JSON feed (Google embeds data in a JS-rendered page, Apple needs a
# CSRF-token handshake). Best added once the sites below are confirmed.
SITES = [
    ("Amazon", amazon),
    ("Atlassian", atlassian),
    ("Microsoft", microsoft),
    ("Blue Yonder", blueyonder),
    ("Cisco", cisco),
] + [(c, lambda c=c, t=t: greenhouse(c, t)) for c, t in GREENHOUSE.items()] \
  + [(c, lambda c=c, t=t: lever(c, t)) for c, t in LEVER.items()]

# ----------------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------------
YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*years?", re.I)


def years_required(text):
    """Largest 'N years ... experience' requirement mentioned (range -> its low end)."""
    vals = []
    for m in YEARS_RE.finditer(text or ""):
        window = text[max(0, m.start() - 40): m.end() + 80].lower()
        if "experience" in window or "exp." in window:
            vals.append(int(m.group(1)))
    return max(vals) if vals else None


def evaluate(j):
    """Return an enriched row dict if the job passes all filters, else None."""
    title = j["title"] or ""
    if not TITLE_INCLUDE.search(title) or TITLE_EXCLUDE.search(title):
        return None
    if not LOCATION_OK.search(j["location"] or ""):
        return None
    if j.get("detail_fn") and not j["desc"]:
        try:
            j["desc"] = j["detail_fn"]()
            time.sleep(0.5)
        except requests.RequestException:
            pass
    text = j["desc"] or ""
    yrs = years_required(j.get("req_text") or text)
    if yrs is not None and yrs > MAX_YEARS_REQUIRED:
        return None
    matched = [s for s, rx in SKILL_RX.items() if rx.search(text)]
    if len(text) > FULL_DESC_MIN_CHARS and len(matched) < MIN_SKILL_MATCHES:
        return None
    return {
        "ID": j["id"], "Company": j["company"], "Title": title,
        "Location": j["location"], "Skills matched": ", ".join(matched),
        "Score": len(matched), "Years req": "" if yrs is None else yrs,
        "Posted": j["posted"], "Link": j["url"],
    }


def collect(only=None):
    found, ok_companies = {}, set()
    for company, fn in SITES:
        if only and company.lower().replace(" ", "") not in only:
            continue
        try:
            raw = fn()
        except Exception as e:  # one broken site must not stop the rest
            print(f"[ERROR] {company}: {e}", file=sys.stderr)
            continue
        ok_companies.add(company)
        kept = 0
        for j in raw:
            row = evaluate(j)
            if row:
                found[row["ID"]] = row
                kept += 1
        print(f"[ok] {company}: fetched {len(raw)}, kept {kept}")
    return found, ok_companies


# ----------------------------------------------------------------------------
# Google Sheets sync
# ----------------------------------------------------------------------------
def sync_sheet(found, ok_companies):
    import gspread

    gc = gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
    sh = gc.open_by_key(os.environ["SHEET_ID"])

    def tab(name):
        try:
            return sh.worksheet(name)
        except gspread.WorksheetNotFound:
            return sh.add_worksheet(title=name, rows=1000, cols=len(HEADERS) + 1)

    active, archive = tab("Active"), tab("Archive")
    old = {str(r["ID"]): r for r in active.get_all_records() if r.get("ID")}
    today = date.today().isoformat()

    keep, gone, new = [], [], []
    for jid, r in old.items():
        if jid in found:
            keep.append(r)  # still open; preserves your Status / Notes
        elif r.get("Company") in ok_companies:
            r["Closed"] = today
            gone.append(r)
        else:
            keep.append(r)  # site failed today: don't archive on a failed fetch
    for jid, row in found.items():
        if jid not in old:
            row["First seen"] = today
            new.append(row)
    new.sort(key=lambda r: -r["Score"])

    rows = new + keep
    data = [HEADERS] + [[r.get(h, "") for h in HEADERS] for r in rows]
    active.clear()
    active.update(range_name="A1", values=data)

    if gone:
        if not archive.get_values("A1:A1"):
            archive.update(range_name="A1", values=[HEADERS + ["Closed"]])
        archive.append_rows([[r.get(h, "") for h in HEADERS + ["Closed"]] for r in gone])
    print(f"Sheet synced: {len(new)} new, {len(gone)} archived, {len(keep)} unchanged")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print results, no Sheets")
    ap.add_argument("--only", help="comma list of companies, e.g. amazon,atlassian")
    args = ap.parse_args()
    only = {x.strip().lower().replace(" ", "") for x in args.only.split(",")} if args.only else None

    found, ok = collect(only)
    if args.dry_run:
        for r in sorted(found.values(), key=lambda r: -r["Score"]):
            print(f"{r['Score']} | {r['Company']} | {r['Title']} | {r['Location']} | "
                  f"yrs={r['Years req']} | {r['Link']}")
        print(f"\n{len(found)} matching jobs")
        return
    sync_sheet(found, ok)


if __name__ == "__main__":
    main()
