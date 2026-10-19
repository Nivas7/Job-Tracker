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
from datetime import date, datetime, timedelta, timezone

import requests

# ----------------------------------------------------------------------------
# FILTER CONFIG - edit these to tune what you see
# ----------------------------------------------------------------------------
TITLE_INCLUDE = re.compile(
    r"engineer|\bsde\b|developer|programmer|full[- ]?stack|back[- ]?end|"
    r"front[- ]?end|member of technical staff|\bmts\b",
    re.I,
)
TITLE_EXCLUDE = re.compile(
    r"senior|\bsr\b|staff|principal|\blead(er|ership)?\b|manager|director|architect|"
    r"head of|distinguished|\bintern\b|internship|\bsdet\b|\bqa\b|quality|"
    r"\b(engineer|developer|sde)\s*(iii|iv|v|3|4|5)\b|"
    # engineer-titled roles that are not software development:
    r"sales|solutions? (engineer|architect)|support engineer|customer engineer|"
    r"field engineer|pre-?sales|technical account|consultant|analyst|hardware|"
    r"mechanical|electrical|civil|validation|verification|manufactur|recruit",
    re.I,
)
# Max "years of experience" a posting may ask for (0-1, 1-2 => 2).
MAX_YEARS_REQUIRED = 2
# Ignore postings older than this many days (set to 0 to disable).
MAX_AGE_DAYS = 90
# Location must match one of these (India only).
LOCATION_OK = re.compile(
    r"india|bengaluru|bangalore|chennai|hyderabad|pune|mumbai|gurgaon|gurugram|"
    r"noida|delhi|kolkata|coimbatore|kochi|ahmedabad|trivandrum|thiruvananthapuram",
    re.I,
)
# Workday shows '3 Locations' instead of names; resolved from the job page.
MULTI_LOC = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)
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
# Public job-board names. A wrong name just logs "[skip] ... not found".
GREENHOUSE = {
    "Anthropic": "anthropic", "Razorpay": "razorpaysoftwareprivatelimited",
    "Stripe": "stripe", "Databricks": "databricks", "Cloudflare": "cloudflare",
    "MongoDB": "mongodb", "Elastic": "elastic", "Twilio": "twilio", "Okta": "okta",
    "Datadog": "datadog", "Coinbase": "coinbase", "Pinterest": "pinterest",
    "Reddit": "reddit", "Airbnb": "airbnb", "Lyft": "lyft", "Dropbox": "dropbox",
    "GitLab": "gitlab", "Asana": "asana", "Figma": "figma", "Samsara": "samsara",
    "Rubrik": "rubrik", "ThoughtSpot": "thoughtspot", "Postman": "postman",
    "BrowserStack": "browserstack", "Chargebee": "chargebee",
    "Innovaccer": "innovaccer", "Groww": "groww", "Druva": "druva",
    "Zscaler": "zscaler", "PagerDuty": "pagerduty", "HubSpot": "hubspot",
    "Mixpanel": "mixpanel", "Roblox": "roblox", "Unity": "unity3d",
    "PhonePe": "phonepe", "Juspay": "juspay", "Sprinklr": "sprinklr",
    "Cohesity": "cohesity", "Netskope": "netskope", "Vimeo": "vimeo",
}
LEVER = {
    "CRED": "cred", "Meesho": "meesho", "Palantir": "palantir", "Spotify": "spotify",
    "Paytm": "paytm", "Gupshup": "gupshup", "Whatfix": "whatfix", "Zepto": "zepto",
}
ASHBY = {
    "OpenAI": "openai", "Notion": "notion", "Ramp": "ramp", "Deel": "deel",
    "Retool": "retool", "Vercel": "vercel", "Supabase": "supabase",
    "Confluent": "confluent", "Linear": "linear",
}
SMARTRECRUITERS = {"Visa": "Visa", "ServiceNow": "ServiceNow"}
# (label, host, tenant, site) for Workday career sites (many big companies).
WORKDAY = [
    ("Adobe", "adobe.wd5.myworkdayjobs.com", "adobe", "external_experienced"),
    ("Salesforce", "salesforce.wd12.myworkdayjobs.com", "salesforce", "External_Career_Site"),
    ("NVIDIA", "nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite"),
    ("Intel", "intel.wd1.myworkdayjobs.com", "intel", "External"),
    ("Walmart Global Tech", "walmart.wd5.myworkdayjobs.com", "walmart", "WalmartExternal"),
    ("Dell", "dell.wd1.myworkdayjobs.com", "dell", "External"),
    ("Qualcomm", "qualcomm.wd12.myworkdayjobs.com", "qualcomm", "External"),
    ("Mastercard", "mastercard.wd1.myworkdayjobs.com", "mastercard", "CorporateCareers"),
    ("PayPal", "paypal.wd1.myworkdayjobs.com", "paypal", "jobs"),
    ("Broadcom", "broadcom.wd1.myworkdayjobs.com", "broadcom", "External_Career"),
    ("Capital One", "capitalone.wd12.myworkdayjobs.com", "capitalone", "Capital_One"),
    ("Workday", "workday.wd5.myworkdayjobs.com", "workday", "Workday"),
    ("Autodesk", "autodesk.wd1.myworkdayjobs.com", "autodesk", "Ext"),
]

DEBUG_DROPPED = os.environ.get("DEBUG_DROPPED") == "1"  # or use --show-dropped

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


def eightfold(company, base, domain, prefix, query="software engineer"):
    """Eightfold career sites (Microsoft, Netflix)."""
    out, start = [], 0
    while start < 1000:
        r = get(base + "/api/pcsx/search", params={
            "domain": domain, "query": query, "location": "India",
            "start": start, "sort_by": "distance", "filter_include_remote": 1,
        }).json()
        pos = (r.get("data") or {}).get("positions", [])
        if not pos:
            break
        for p in pos:
            pid = p.get("id")

            def detail(pid=pid):
                d = get(base + "/api/pcsx/position_details", params={
                    "position_id": pid, "domain": domain, "hl": "en",
                }).json()
                return clean((d.get("data") or {}).get("jobDescription"))

            out.append(dict(
                id=f"{prefix}-{pid}", company=company, title=p.get("name", ""),
                location="; ".join(p.get("locations") or []),
                url=base + (p.get("positionUrl") or f"/careers/job/{pid}"),
                posted=to_date(p.get("postedTs")), desc="", detail_fn=detail,
            ))
        start += len(pos)
        time.sleep(1)
    return out


def microsoft():
    return eightfold("Microsoft", "https://apply.careers.microsoft.com", "microsoft.com", "msft")


def netflix():
    return eightfold("Netflix", "https://explore.jobs.netflix.net", "netflix.com", "nflx")


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
            job_url = f"https://{host}/{path}/job/{jid}"

            def detail(job_url=job_url):
                page = get(job_url, headers={"Accept": "text/html"}).text
                blobs = re.findall(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
                if not blobs:
                    return ""
                return clean(json.loads('"' + max(blobs, key=len) + '"'))

            out.append(dict(
                id=f"{company.lower().replace(' ', '')}-{jid}", company=company,
                title=j.get("title", ""), location=loc, url=job_url,
                posted=to_date(j.get("postedDate")),
                desc="", teaser=clean(j.get("descriptionTeaser")), detail_fn=detail,
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


def ashby(company, name):
    r = get(f"https://api.ashbyhq.com/posting-api/job-board/{name}").json()
    out = []
    for j in r.get("jobs", []):
        locs = [j.get("location") or ""] + [
            (x.get("location") if isinstance(x, dict) else str(x))
            for x in (j.get("secondaryLocations") or [])]
        out.append(dict(
            id=f"ashby-{name}-{j.get('id')}", company=company, title=j.get("title", ""),
            location="; ".join(x for x in locs if x),
            url=j.get("jobUrl") or j.get("applyUrl", ""),
            posted=to_date(j.get("publishedAt") or j.get("publishedDate")),
            desc=clean(j.get("descriptionPlain") or j.get("descriptionHtml")),
        ))
    return out


def smartrecruiters(company, cid):
    out, offset = [], 0
    while offset < 1000:
        r = get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings",
                params={"limit": 100, "offset": offset, "country": "in", "q": "engineer"}).json()
        items = r.get("content", [])
        for p in items:
            loc = p.get("location") or {}
            parts = [loc.get("city"), loc.get("region")]
            if (loc.get("country") or "").lower() == "in":
                parts.append("India")
            pid = p.get("id")

            def detail(pid=pid):
                d = get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings/{pid}").json()
                secs = ((d.get("jobAd") or {}).get("sections") or {}).values()
                return clean(" ".join((x or {}).get("text", "") for x in secs))

            out.append(dict(
                id=f"sr-{cid}-{pid}", company=company, title=p.get("name", ""),
                location=", ".join(x for x in parts if x),
                url=f"https://jobs.smartrecruiters.com/{cid}/{pid}",
                posted=to_date(p.get("releasedDate")), desc="", detail_fn=detail,
            ))
        offset += len(items)
        if not items or offset >= r.get("totalFound", 0):
            break
        time.sleep(1)
    return out


def relative_date(text):
    t = (text or "").lower()
    if "today" in t:
        return date.today().isoformat()
    if "yesterday" in t:
        return (date.today() - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*days?", t)
    return (date.today() - timedelta(days=int(m.group(1)))).isoformat() if m else ""


def workday(company, host, tenant, site):
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    out = []
    for text in ("software engineer india", "software engineer"):
        offset = 0
        while offset < 400:
            r = post(base + "/jobs", json={"appliedFacets": {}, "limit": 20,
                                           "offset": offset, "searchText": text}).json()
            posts = r.get("jobPostings", [])
            if not posts:
                break
            for p in posts:
                path = p.get("externalPath", "")
                job = dict(
                    id=f"wd-{tenant}-{path.rsplit('/', 1)[-1]}", company=company,
                    title=p.get("title", ""), location=p.get("locationsText", ""),
                    url=f"https://{host}/en-US/{site}{path}",
                    posted=relative_date(p.get("postedOn")), desc="",
                )

                def detail(job=job, path=path):
                    d = (get(base + path).json().get("jobPostingInfo") or {})
                    if MULTI_LOC.search(job["location"] or ""):  # "3 Locations"
                        locs = [d.get("location") or ""] + list(d.get("additionalLocations") or [])
                        country = (d.get("country") or {}).get("descriptor", "")
                        job["location"] = "; ".join(x for x in locs + [country] if x)
                    return clean(d.get("jobDescription"))

                job["detail_fn"] = detail
                out.append(job)
            offset += len(posts)
            time.sleep(0.5)
        if out:  # the India-specific search worked; skip the fallback query
            break
    return out


# NOT included: Meta, Google, Apple, Netflix, Bloomberg. Their career sites have no stable
# public JSON feed (Google embeds data in a JS-rendered page, Apple needs a
# CSRF-token handshake). Best added once the sites below are confirmed.
SITES = [
    ("Amazon", amazon),
    ("Atlassian", atlassian),
    ("Microsoft", microsoft),
    ("Blue Yonder", blueyonder),
    ("Cisco", cisco),
] + [(c, lambda c=c, t=t: greenhouse(c, t)) for c, t in GREENHOUSE.items()] \
  + [(c, lambda c=c, t=t: lever(c, t)) for c, t in LEVER.items()] \
  + [(c, lambda c=c, t=t: ashby(c, t)) for c, t in ASHBY.items()] \
  + [(c, lambda c=c, t=t: smartrecruiters(c, t)) for c, t in SMARTRECRUITERS.items()] \
  + [(c, lambda c=c, a=a: workday(c, *a)) for c, *a in WORKDAY]

# ----------------------------------------------------------------------------
# Filtering
# ----------------------------------------------------------------------------
YEARS_RE = re.compile(r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*\d{1,2}\s*)?\+?\s*(?:years?|yrs?)\b", re.I)


def years_detail(text):
    """Largest 'N years ... experience' requirement (range -> low end) + the phrase."""
    best, snippet = None, ""
    for m in YEARS_RE.finditer(text or ""):
        window = text[max(0, m.start() - 40): m.end() + 80]
        if "experience" in window.lower() or "exp." in window.lower():
            n = int(m.group(1))
            if best is None or n > best:
                best, snippet = n, re.sub(r"\s+", " ", window).strip()
    return best, snippet


def years_required(text):
    return years_detail(text)[0]


def years_in_title(title):
    """Titles like 'Software Engineer | 4-8 years' or '9+ yrs' state the level."""
    m = YEARS_RE.search(title or "")
    return int(m.group(1)) if m else None


def posted_date(text):
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y"):
        try:
            return datetime.strptime(t[:10] if fmt == "%Y-%m-%d" else t, fmt).date()
        except ValueError:
            pass
    return None


def evaluate(j):
    """Return (row, None) if the job passes all filters, else (None, reason)."""
    title = j["title"] or ""
    j["why"] = ""
    if not TITLE_INCLUDE.search(title) or TITLE_EXCLUDE.search(title):
        j["why"] = "excluded by title filter"
        return None, "title"
    multi = bool(MULTI_LOC.search(j["location"] or ""))
    if not multi and not LOCATION_OK.search(j["location"] or ""):
        return None, "location"
    pd = posted_date(j.get("posted"))
    if MAX_AGE_DAYS and pd and (date.today() - pd).days > MAX_AGE_DAYS:
        j["why"] = f"posted {pd}"
        return None, "old"
    ty = years_in_title(title)
    if ty is not None and ty > MAX_YEARS_REQUIRED:
        j["why"] = "title says " + title
        return None, "years"
    if j.get("detail_fn") and not j["desc"]:
        try:
            j["desc"] = j["detail_fn"]()
            time.sleep(0.5)
        except Exception:
            pass
    if multi and not LOCATION_OK.search(j["location"] or ""):
        return None, "location"
    text = j["desc"] or j.get("teaser", "")
    yrs, snip = years_detail(j.get("req_text") or text)
    if yrs is not None and yrs > MAX_YEARS_REQUIRED:
        j["why"] = snip
        return None, "years"
    matched = [s for s, rx in SKILL_RX.items() if rx.search(text)]
    if len(text) > FULL_DESC_MIN_CHARS and len(matched) < MIN_SKILL_MATCHES:
        j["why"] = "no resume skills in description"
        return None, "skills"
    return {
        "ID": j["id"], "Company": j["company"], "Title": title,
        "Location": j["location"], "Skills matched": ", ".join(matched),
        "Score": len(matched), "Years req": "" if yrs is None else yrs,
        "Posted": j["posted"], "Link": j["url"],
    }, None


def worth_showing(j, reason):
    """Debug view: skip the noise of obviously non-engineering titles."""
    if reason in ("years", "skills", "old"):
        return True
    return bool(reason == "title"
                and re.search(r"engineer|developer|\bsde\b", j["title"], re.I)
                and LOCATION_OK.search(j["location"] or ""))


def collect(only=None):
    found, ok_companies = {}, set()
    for company, fn in SITES:
        if only and company.lower().replace(" ", "") not in only:
            continue
        try:
            raw = fn()
        except Exception as e:  # one broken site must not stop the rest
            resp = getattr(e, "response", None)
            if resp is not None and resp.status_code == 404:
                print(f"[skip] {company}: board not found (wrong name?)")
            else:
                print(f"[ERROR] {company}: {str(e)[:150]}")
            continue
        ok_companies.add(company)
        kept, drops = 0, {}
        for j in raw:
            row, reason = evaluate(j)
            if row:
                found[row["ID"]] = row
                kept += 1
            else:
                drops[reason] = drops.get(reason, 0) + 1
                if DEBUG_DROPPED and worth_showing(j, reason):
                    print(f"     - [{reason}] {j['title'][:80]} | {j['why'][:120]}")
        print(f"[ok] {company}: fetched {len(raw)}, kept {kept}, dropped {drops}")
        if DEBUG_DROPPED and raw and kept == 0:  # help debugging: show what a typical job looks like
            r0 = raw[0]
            print(f"     sample: {r0['title']!r} | {r0['location']!r}")
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
    ap.add_argument("--show-dropped", action="store_true",
                    help="list jobs dropped by years/skills/age and engineering titles dropped")
    ap.add_argument("--only", help="comma list of companies, e.g. amazon,atlassian")
    args = ap.parse_args()
    global DEBUG_DROPPED
    DEBUG_DROPPED = DEBUG_DROPPED or args.show_dropped
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
