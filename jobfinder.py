"""
MY JOB FINDER
=============
Runs once a day (on GitHub). It:
  1. Collects jobs (any industry) from employer career sites and the Adzuna job API.
  2. Removes fake, agency, old and duplicate posts.
  3. Scores each job against my skills (using regex, no paid AI).
  4. Writes a web page: docs/index.html

Run it on your own computer:
    python jobfinder.py            -> real run (needs internet)
    python jobfinder.py --sample   -> practice run with fake jobs (no internet)
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus, urlparse

import requests
import yaml

HERE = Path(__file__).parent
CONFIG_FILE = HERE / "config.yaml"
SEEN_FILE = HERE / "data" / "seen.json"      # remembers when we first saw each job
PAGE_FILE = HERE / "docs" / "index.html"     # the web page we build
SAMPLE_FILE = HERE / "data" / "sample_jobs.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (personal job search; 1 request/sec)"}
TODAY = datetime.now(timezone.utc).date()


# ------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------
def any_match(patterns, text):
    """True if ANY regex pattern in the list is found in the text."""
    return any(re.search(p, text or "", re.IGNORECASE) for p in patterns)


def html_to_text(raw):
    """Turn HTML into plain text, keeping line breaks between paragraphs/bullets."""
    if not raw:
        return ""
    raw = html.unescape(raw)  # some sites send HTML twice-encoded
    raw = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h\d|/ul)\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)<li[^>]*>", "\n• ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"&?xa;|&nbsp;?", " ", raw)  # leftover space codes some sites send
    raw = re.sub(r"[ \t\xa0]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


def days_ago_from_text(text):
    """Workday says 'Posted Today', 'Posted Yesterday', 'Posted 5 Days Ago', 'Posted 30+ Days Ago'."""
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)\+?\s*day", t)
    return int(m.group(1)) if m else None


def days_ago_from_date(value):
    """Turn a date like '2026-11-10T14:00:00Z' (or milliseconds) into 'days ago'."""
    try:
        if isinstance(value, (int, float)):  # Lever uses milliseconds
            d = datetime.fromtimestamp(value / 1000, timezone.utc).date()
        else:
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00")[:25]).date()
        return max((TODAY - d).days, 0)
    except (ValueError, TypeError, OSError):
        return None


def make_job(source, company, title, location, url, description, days_ago, lat=None, lon=None,
             pay_min=None, pay_max=None, pay_estimated=False):
    return {
        "pay_min": pay_min,
        "pay_max": pay_max,
        "pay_estimated": pay_estimated,
        "lat": lat,
        "lon": lon,
        "source": source,
        "company": (company or "").strip(),
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": url,
        "description": description or "",
        "days_ago": days_ago,
    }



def friendly_error(err):
    """Short, safe error note for the page. Never includes web addresses (they can hold keys)."""
    code = getattr(getattr(err, "response", None), "status_code", None)
    if code == 401 or code == 403:
        return f"{code}: keys or access refused. For Adzuna, use the App ID and App Key from the Adzuna dashboard (not your login)."
    if code == 429:
        return "429: the site said 'too many requests' and blocked us today. It will try again tomorrow."
    if code:
        return f"{code}: the site returned an error."
    text = re.sub(r"https?://\S+", "[web address hidden]", str(err))
    return text[:160]


def get_or_post(method, url, tries=3, **kwargs):
    """Ask a website for data. If it says 'too many requests' (429), wait and try again."""
    for attempt in range(tries):
        r = requests.request(method, url, timeout=30, **kwargs)
        if r.status_code != 429 or attempt == tries - 1:
            r.raise_for_status()
            return r
        time.sleep(10 * (attempt + 1))  # wait 10s, then 20s


# ------------------------------------------------------------
# Step 1: COLLECT jobs from each kind of website
# ------------------------------------------------------------
def fetch_workday(emp, cfg):
    """Workday sites have a hidden JSON feed behind the careers page."""
    u = urlparse(emp["url"])
    tenant = u.netloc.split(".")[0]
    parts = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    site = parts[0]
    base = f"{u.scheme}://{u.netloc}"
    api = f"{base}/wday/cxs/{tenant}/{site}"

    wd_headers = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json",
                  "Accept-Language": "en-US", "Origin": base, "Referer": f"{base}/{site}"}
    found = {}
    for term in cfg["search_terms"]:
        for offset in (0, 20, 40):  # up to 60 results per search word
            r = get_or_post("POST", f"{api}/jobs", headers=wd_headers, json={
                "appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            postings = r.json().get("jobPostings", [])
            for p in postings:
                if p.get("externalPath") and passes_title(p.get("title", ""), cfg):
                    found[p["externalPath"]] = p
            if len(postings) < 20:
                break
            time.sleep(2)
        time.sleep(2)

    jobs = []
    for path, p in found.items():
        days = days_ago_from_text(p.get("postedOn"))
        if days is not None and days > cfg["max_days_old"]:
            continue
        # Open each matching job to read the full description.
        desc, location, url = "", p.get("locationsText", ""), f"{base}/{site}{path}"
        try:
            d = get_or_post("GET", f"{api}{path}", headers=wd_headers).json()
            info = d.get("jobPostingInfo", {})
            desc = html_to_text(info.get("jobDescription", ""))
            location = info.get("location") or location
            if info.get("remoteType"):
                location += f" ({info['remoteType']})"
            url = info.get("externalUrl") or url
        except Exception:
            pass
        jobs.append(make_job(emp["name"], emp["name"], p.get("title"), location, url, desc, days))
        time.sleep(2)
    return jobs


def fetch_greenhouse(emp, cfg):
    url = f"https://boards-api.greenhouse.io/v1/boards/{emp['board']}/jobs?content=true"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return [make_job(emp["name"], emp["name"], j.get("title"), (j.get("location") or {}).get("name"),
                     j.get("absolute_url"), html_to_text(j.get("content")), days_ago_from_date(j.get("updated_at")))
            for j in r.json().get("jobs", [])]


def fetch_lever(emp, cfg):
    url = f"https://api.lever.co/v0/postings/{emp['board']}?mode=json"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    jobs = []
    for j in r.json():
        lists = "\n".join(f"{x.get('text', '')}\n{html_to_text(x.get('content', ''))}" for x in j.get("lists", []))
        desc = (j.get("descriptionPlain") or "") + "\n" + lists
        loc = (j.get("categories") or {}).get("location", "")
        if j.get("workplaceType") == "remote":
            loc += " (Remote)"
        jobs.append(make_job(emp["name"], emp["name"], j.get("text"), loc, j.get("hostedUrl"),
                             desc, days_ago_from_date(j.get("createdAt"))))
    return jobs


def fetch_adzuna(cfg):
    """Free job API. Needs ADZUNA_APP_ID and ADZUNA_APP_KEY (stored as GitHub secrets)."""
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        raise RuntimeError("No Adzuna keys yet (optional - see README step 4)")
    az = cfg["adzuna"]
    jobs = []
    for term in cfg["search_terms"]:
        for page in (1, 2):
            r = get_or_post("GET", f"https://api.adzuna.com/v1/api/jobs/us/search/{page}", headers=HEADERS,
                             params={"app_id": app_id, "app_key": app_key, "what": term, "where": az["where"],
                                     "results_per_page": 50, "max_days_old": cfg["max_days_old"],
                                     "content-type": "application/json"})
            results = r.json().get("results", [])
            for j in results:
                company = (j.get("company") or {}).get("display_name", "")
                if az.get("only_employers") and not any_match(az["only_employers"], company):
                    continue
                jobs.append(make_job("Adzuna", company, j.get("title"),
                                     (j.get("location") or {}).get("display_name"), j.get("redirect_url"),
                                     html_to_text(j.get("description")), days_ago_from_date(j.get("created")),
                                     j.get("latitude"), j.get("longitude"),
                                     j.get("salary_min"), j.get("salary_max"),
                                     str(j.get("salary_is_predicted")) == "1"))
            if len(results) < 50:
                break
            time.sleep(1)
    return jobs


FETCHERS = {"workday": fetch_workday, "greenhouse": fetch_greenhouse, "lever": fetch_lever}


def collect_all(cfg):
    """Run every source. If one site fails, keep going and record the error."""
    all_jobs, status = [], []
    sources = [(e["name"], lambda e=e: FETCHERS[e["type"]](e, cfg)) for e in cfg.get("employers") or []]
    if cfg.get("adzuna", {}).get("enabled"):
        sources.append(("Adzuna (many CT employers)", lambda: fetch_adzuna(cfg)))
    for name, run in sources:
        try:
            jobs = run()
            all_jobs += jobs
            status.append({"source": name, "ok": True, "count": len(jobs), "note": ""})
        except Exception as err:
            status.append({"source": name, "ok": False, "count": 0, "note": friendly_error(err)})
        print(f"  {name}: {status[-1]}")
    return all_jobs, status


# ------------------------------------------------------------
# Step 2: FILTER out jobs I don't want
# ------------------------------------------------------------
def passes_title(title, cfg):
    return any_match(cfg["title_must_match"], title) and not any_match(cfg["title_must_not_match"], title)


def keep_job(job, cfg):
    text = f"{job['title']}\n{job['description']}"
    if not passes_title(job["title"], cfg):
        return False
    if any_match(cfg["blocked_companies"], job["company"]) or any_match(cfg["blocked_words"], text):
        return False
    if job["days_ago"] is not None and job["days_ago"] > cfg["max_days_old"]:
        return False
    remote = bool(re.search(r"\bremote\b|work (from|at) home|virtual",
                            f"{job['location']} {job['title']} {job['description'][:300]}", re.I))
    job["remote"] = remote
    return any_match(cfg["locations_ok"], job["location"]) or (cfg["include_remote"] and remote)


def find_pay(text):
    """Find pay in the job text, e.g. '$85,000 - $110,000', '$95K', '$38.50/hour'. Returns yearly (min, max)."""
    money = r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?([kK])?"
    m = re.search(money + r"(?:\s*(?:-|–|to)\s*" + money + r")?(.{0,25})", text)
    if not m:
        return None, None
    def num(v, k):
        if not v:
            return None
        n = float(v.replace(",", ""))
        return n * 1000 if k else n
    low, high = num(m.group(1), m.group(2)), num(m.group(3), m.group(4))
    high = high or low
    if re.search(r"hour|/\s?hr|hourly", m.group(5) or "", re.I) or 15 <= high < 150:
        low, high = low * 2080, high * 2080          # hourly -> yearly (40 hrs x 52 weeks)
    if high < 15000:                                  # too small to be a salary (e.g. a bonus)
        return None, None
    return low, high


def add_pay(job):
    if not job.get("pay_max"):
        job["pay_min"], job["pay_max"] = find_pay(job["description"])
        job["pay_estimated"] = False


def pay_ok(job, cfg):
    if not job.get("pay_max"):
        return not cfg.get("hide_jobs_without_pay", False)
    return job["pay_max"] >= cfg.get("min_salary", 0)


def dedupe(jobs):
    """The same job can show up on two sites. Keep one (prefer the employer's own site)."""
    best = {}
    for j in jobs:
        title = re.sub(r"\W+", " ", j["title"].lower().replace("&", " and ")).strip()
        company = re.sub(r"\b(the|inc|llc|corp|corporation|company|co|group)\b", "", j["company"].lower())
        company = re.sub(r"[^a-z0-9]", "", company)[:8]  # "NBC Universal" = "NBCUniversal"
        key = f"{company} {title}"
        if key not in best or (best[key]["source"] == "Adzuna" and j["source"] != "Adzuna"):
            best[key] = j
    return list(best.values())


# ------------------------------------------------------------
# Step 3: SCORE each job against my skills (regex only)
# ------------------------------------------------------------
# Skills that job posts often ask for. Used to show "gaps" (things to learn).
COMMON_SKILLS = ["SQL", "Python", "Excel", "Tableau", "Power BI", "R", "SAS", "Epic", "EHR", "Snowflake",
                 "statistics", "dashboards", "data cleaning", "data visualization", "machine learning",
                 "Agile", "project management", "HIPAA", "claims", "Cerner", "Alteryx", "Looker", "ETL",
                 "Jira", "Scrum", "Smartsheet", "MS Project", "PMP", "roadmap"]


def skill_regex(skill):
    # "R" and other short words must be whole words, so "R" doesn't match "Report".
    return r"(?<![\w-])" + re.escape(skill) + r"(?![\w-])"


def find_years_asked(text):
    """Find the smallest 'X years' the post asks for, e.g. '3+ years of experience' -> 3."""
    nums = [int(m) for m in re.findall(r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*\d{0,2}\s*\+?\s*years?", text, re.I)
            if 0 < int(m) < 20]
    return min(nums) if nums else None


def find_qualifications(text):
    """Pull a short piece of the 'Qualifications / Requirements' section."""
    m = re.search(r"(?im)^.{0,40}\b(qualifications?|requirements?|what you.ll need|what you bring|"
                  r"required (skills|experience)|education|you have)\b.{0,40}$", text)
    part = text[m.end():] if m else text
    lines = [l.strip(" •-*\t") for l in part.splitlines() if len(l.strip()) > 15]
    snippet = " • ".join(lines[:4])
    return (snippet[:420] + "…") if len(snippet) > 420 else snippet


def score_job(job, cfg):
    text = f"{job['title']}\n{job['description']}"
    mine = cfg["my_skills"]

    matched = [s for s in mine if re.search(skill_regex(s), text, re.I)]
    asked = [s for s in COMMON_SKILLS if re.search(skill_regex(s), text, re.I)]
    gaps = [s for s in asked if s.lower() not in {m.lower() for m in mine}]

    score = 30                                   # passed every filter = a start
    score += min(len(matched) * 8, 40)           # up to +40 for my skills
    score -= min(len(gaps) * 4, 16)              # small minus for skills I lack
    if cfg.get("bonus_industries") and any_match(cfg["bonus_industries"], text):
        score += cfg.get("bonus_points", 5)      # small bonus for industries I like
    if re.search(r"entry|junior|associate|\bI\b|intern|new grad|early career", job["title"], re.I):
        score += 10                              # entry level fits me
    years = find_years_asked(job["description"])
    if years is not None:
        score += 10 if years <= cfg["max_years_experience"] else -15
    if job.get("pay_max") and job["pay_max"] >= cfg.get("target_salary", 10**9):
        score += 10                              # pays my target salary or more
    if job["days_ago"] is not None and job["days_ago"] <= 3:
        score += 5                               # fresh posts: apply early

    if re.search(r"\bproduct\b", job["title"], re.I):
        category = "product"
    elif re.search(r"\bproject\b|\bprogram\b|\bPMO\b|scrum", job["title"], re.I):
        category = "project"
    else:
        category = "data"

    job.update(category=category, score=max(0, min(score, 100)), matched=matched, gaps=gaps, years=years,
               summary=find_qualifications(job["description"]))
    return job


# ------------------------------------------------------------
# Step 3b: MAP POSITIONS so the page can search "within X miles of a ZIP / town"
# ------------------------------------------------------------
def load_places(states=("CT",)):
    """ZIP codes and town names -> (latitude, longitude). Uses the free offline 'zipcodes' library."""
    zips, towns = {}, {}
    try:
        import zipcodes
    except ImportError:
        return zips, towns
    for st in states:
        for z in zipcodes.filter_by(state=st):
            pos = [round(float(z["lat"]), 4), round(float(z["long"]), 4)]
            zips[z["zip_code"]] = pos
            towns.setdefault(z["city"].lower(), pos)
    return zips, towns


def add_position(job, towns):
    """If the job has no map position, find a town name in its location text."""
    if job.get("lat") and job.get("lon"):
        return
    loc = job["location"].lower()
    for town in sorted(towns, key=len, reverse=True):  # longest names first ("East Hartford" before "Hartford")
        if re.search(r"\b" + re.escape(town) + r"\b", loc):
            job["lat"], job["lon"] = towns[town]
            return


# ------------------------------------------------------------
# Step 4: REMEMBER which jobs are new
# ------------------------------------------------------------
def mark_new(jobs):
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    for j in jobs:
        first = seen.setdefault(j["url"], TODAY.isoformat())
        j["is_new"] = first == TODAY.isoformat()
        j["first_seen"] = first
    cutoff = (TODAY - timedelta(days=60)).isoformat()  # forget very old jobs
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    SEEN_FILE.parent.mkdir(exist_ok=True)
    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True))


# ------------------------------------------------------------
# Step 5: BUILD the web page
# ------------------------------------------------------------
def e(x):
    return html.escape(str(x or ""), quote=True)


def posted_text(d):
    if d is None:
        return "Date unknown"
    return "Posted today" if d == 0 else "Posted yesterday" if d == 1 else f"Posted {d} days ago"


def job_card(j):
    level = "high" if j["score"] >= 70 else "mid" if j["score"] >= 50 else "low"
    chips = "".join(f'<span class="chip ok">✓ {e(s)}</span>' for s in j["matched"])
    gaps = "".join(f'<span class="chip gap">{e(s)}</span>' for s in j["gaps"])
    years = f" · Asks {j['years']}+ yrs" if j["years"] else ""
    via = ' · <span class="via">via Adzuna</span>' if j["source"] == "Adzuna" else ""
    new = '<span class="new">NEW</span>' if j["is_new"] else ""
    search_text = e(f"{j['title']} {j['company']}".lower())
    lat = j.get("lat") or ""
    lon = j.get("lon") or ""
    remote_tag = ' · <span class="remote">Remote</span>' if j.get("remote") else ""
    if j.get("pay_max"):
        lo, hi = j.get("pay_min") or j["pay_max"], j["pay_max"]
        rng = f"${lo/1000:.0f}K" if round(lo, -3) == round(hi, -3) else f"${lo/1000:.0f}K – ${hi/1000:.0f}K"
        cls = "pay top" if hi >= 100000 else "pay"
        est = " (estimate)" if j.get("pay_estimated") else ""
        pay_html = f'<p class="{cls}">💵 {rng} a year{est}</p>'
    else:
        pay_html = '<p class="pay none">Pay not listed</p>'
    pay_val = int(j.get("pay_max") or 0)
    if j["source"] == "Adzuna":
        # Adzuna only gives its own page. Add a quick search for the company's own careers site.
        q = quote_plus(f'{j["company"]} careers "{j["title"]}"')
        apply_html = (f'<div class="btns"><a class="apply" href="https://www.google.com/search?q={q}" target="_blank" '
                      f'rel="noopener">Find on company site →</a>'
                      f'<a class="apply alt" href="{e(j["url"])}" target="_blank" rel="noopener">View on Adzuna</a></div>')
    else:
        apply_html = f'<a class="apply" href="{e(j["url"])}" target="_blank" rel="noopener">Open job &amp; apply →</a>'

    return f"""
<article class="card" data-score="{j['score']}" data-new="{int(j['is_new'])}" data-text="{search_text}"
  data-loc="{e(j['location'].lower())}" data-lat="{lat}" data-lon="{lon}" data-remote="{int(bool(j.get('remote')))}"
  data-cat="{j['category']}" data-pay="{pay_val}">
  <div class="top">
    <div class="score {level}">{j['score']}<small>%</small></div>
    <div class="head">
      <h2>{new}{e(j['title'])}</h2>
      <p class="meta">{e(j['company'])} · {e(j['location']) or 'Location not listed'}{remote_tag}<span class="dist"></span></p>
      <p class="meta">{posted_text(j['days_ago'])}{years}{via}</p>
      {pay_html}
    </div>
  </div>
  {f'<p class="label">You have</p><div class="chips">{chips}</div>' if chips else ''}
  {f'<p class="label">They also ask for</p><div class="chips">{gaps}</div>' if gaps else ''}
  {f'<p class="label">Qualifications</p><p class="summary">{e(j["summary"])}</p>' if j['summary'] else ''}
  {apply_html}
</article>"""


def build_page(jobs, status, places):
    updated = datetime.now(ZoneInfo("America/New_York")).strftime("%a %b %d, %I:%M %p ET")
    new_count = sum(j["is_new"] for j in jobs)
    rows = "".join(
        f"<tr><td>{'✅' if s['ok'] else '⚠️'} {e(s['source'])}</td><td>{s['count']}</td><td>{e(s['note'])}</td></tr>"
        for s in status)
    cards = "".join(job_card(j) for j in jobs) or '<p class="empty">No matching jobs today. Check back tomorrow.</p>'
    css = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--muted:#5f6b7a;--line:#e3e7ec;--accent:#0b6e4f;
--hi:#0b6e4f;--mid:#b26b00;--lo:#8a94a3;--okbg:#e6f4ee;--gapbg:#fff3e0}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f25;--ink:#e8ecf0;--muted:#9aa5b1;
--line:#2a313a;--accent:#3ccf91;--hi:#3ccf91;--mid:#f0a940;--okbg:#173327;--gapbg:#3a2a12}}
*{box-sizing:border-box}body{margin:0;font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--ink)}
header{position:sticky;top:0;background:var(--bg);padding:14px 16px 10px;border-bottom:1px solid var(--line);z-index:2}
h1{font-size:20px;margin:0}.sub{color:var(--muted);font-size:13px;margin:2px 0 10px}
.controls{display:flex;gap:8px;flex-wrap:wrap}
input{flex:1 1 180px;padding:9px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);
color:var(--ink);font-size:15px}
button{padding:8px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink);
font-size:14px;cursor:pointer}button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
main{max-width:760px;margin:0 auto;padding:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:0 0 12px}
.top{display:flex;gap:12px;align-items:flex-start}
.score{min-width:56px;height:56px;border-radius:12px;display:flex;align-items:center;justify-content:center;
font-weight:700;font-size:20px;color:#fff}.score small{font-size:11px}
.score.high{background:var(--hi)}.score.mid{background:var(--mid)}.score.low{background:var(--lo)}
h2{font-size:17px;margin:0 0 2px}.meta{margin:0;color:var(--muted);font-size:13.5px}
.new{background:#d93a3a;color:#fff;font-size:11px;padding:2px 6px;border-radius:6px;margin-right:6px;
vertical-align:2px}.via{font-style:italic}
.label{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:12px 0 4px}
.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{font-size:13px;padding:3px 9px;border-radius:999px}
.chip.ok{background:var(--okbg)}.chip.gap{background:var(--gapbg)}
.summary{margin:0;font-size:14.5px}
.apply{display:block;text-align:center;margin-top:12px;padding:11px;border-radius:10px;background:var(--accent);
color:#fff;text-decoration:none;font-weight:600}
select,.chk{padding:8px 10px;border:1px solid var(--line);border-radius:10px;background:var(--card);
color:var(--ink);font-size:14px}.chk{display:flex;align-items:center;gap:6px}
.go{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600;padding:9px 18px}
.chipsrow{margin-top:8px}.sep{width:8px}.msg{margin:6px 0 0;font-size:13px;color:var(--mid)}
.remote{color:var(--accent);font-weight:600}
.btns{display:flex;gap:8px;flex-wrap:wrap}.btns .apply{flex:1 1 180px}
.apply.alt{background:transparent;color:var(--accent);border:1.5px solid var(--accent)}
.pay{margin:4px 0 0;font-size:14px;font-weight:600}.pay.top{color:var(--hi)}.pay.none{color:var(--muted);font-weight:400}
.chk input{flex:none;width:18px;height:18px;margin:0}#qr{flex:0 0 auto}
@media (max-width:700px){header{position:static}}.dist{color:var(--accent)}
details{margin:20px 0;color:var(--muted);font-size:13.5px}table{width:100%;border-collapse:collapse}
td{border-top:1px solid var(--line);padding:6px 4px;vertical-align:top}.empty{text-align:center;color:var(--muted)}
"""
    js = r"""
const PLACES = __PLACES__;
let mode='all', cat='all', minPay=0;
const $=id=>document.getElementById(id);
function miles(a,b,c,d){const R=3959,r=x=>x*Math.PI/180,dl=r(c-a),dn=r(d-b);
 const h=Math.sin(dl/2)**2+Math.cos(r(a))*Math.cos(r(c))*Math.sin(dn/2)**2;return 2*R*Math.asin(Math.sqrt(h));}
function findPlace(t){t=t.trim().toLowerCase().replace(/,?\s*(ct|connecticut)$/,'').trim();
 if(!t)return null;if(PLACES.zips[t])return PLACES.zips[t];return PLACES.towns[t]||null;}
function show(){
 const words=$('qt').value.toLowerCase().split(/\s+/).filter(Boolean);
 const lt=$('ql').value.trim(), center=findPlace(lt), radius=+$('qr').value, rem=$('qremote').checked;
 $('msg').textContent = lt && !center ? 'Could not find "'+lt+'" on the map. Showing jobs whose location contains that text.' : '';
 let n=0;
 document.querySelectorAll('.card').forEach(c=>{
  const d=c.dataset; let ok=words.every(w=>d.text.includes(w)); let dist='';
  if(mode==='new')ok=ok&&d.new==='1'; if(mode==='top')ok=ok&&+d.score>=70;
  if(cat!=='all')ok=ok&&d.cat===cat;
  if(minPay)ok=ok&&+d.pay>=minPay;
  if(!rem && d.remote==='1') ok=false;
  if(ok && lt){
   if(center){
    if(d.lat&&d.lon){const m=miles(center[0],center[1],+d.lat,+d.lon); dist=' · '+Math.round(m)+' mi away';
      ok = m<=radius || (rem&&d.remote==='1');}
    else ok = rem&&d.remote==='1';
   } else ok = d.loc.includes(lt.toLowerCase()) || (rem&&d.remote==='1');
  }
  c.querySelector('.dist').textContent=dist;
  c.style.display=ok?'':'none'; if(ok)n++;});
 $('n').textContent=n;}
$('search').onsubmit=e=>{e.preventDefault();show();return false;};
$('clear').onclick=()=>{$('qt').value='';$('ql').value='';$('qr').value='25';$('qremote').checked=true;show();};
['qr','qremote'].forEach(id=>$(id).onchange=show);
document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{mode=b.dataset.mode;
 document.querySelectorAll('[data-mode]').forEach(x=>x.classList.toggle('on',x===b));show();});
document.querySelectorAll('button[data-pay]').forEach(b=>b.onclick=()=>{minPay=+b.dataset.pay;
 document.querySelectorAll('button[data-pay]').forEach(x=>x.classList.toggle('on',x===b));show();});
document.querySelectorAll('[data-cat]').forEach(b=>b.onclick=()=>{cat=b.dataset.cat;
 document.querySelectorAll('[data-cat]').forEach(x=>x.classList.toggle('on',x===b));show();});
show();
""".replace("__PLACES__", json.dumps(places, separators=(",", ":")))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>My Job Finder</title><style>{css}</style></head>
<body>
<header>
  <h1>My Job Finder</h1>
  <p class="sub">Updated {updated} · <b id="n">{len(jobs)}</b> jobs shown · {new_count} new today</p>
  <form id="search" class="controls" onsubmit="return false">
    <input id="qt" type="search" placeholder="Job title or company (e.g. project coordinator)">
    <input id="ql" type="search" placeholder="City or ZIP (e.g. 06040)">
    <select id="qr" aria-label="Distance">
      <option value="10">within 10 mi</option><option value="25" selected>within 25 mi</option>
      <option value="50">within 50 mi</option><option value="9999">any distance</option>
    </select>
    <label class="chk"><input id="qremote" type="checkbox" checked> Include remote</label>
    <button id="go" type="submit" class="go">Search</button>
    <button id="clear" type="button">Clear</button>
  </form>
  <div class="controls chipsrow">
    <button data-cat="all" class="on">All types</button>
    <button data-cat="data">Data / BI</button>
    <button data-cat="project">Project / Program</button>
    <button data-cat="product">Product</button>
  </div>
  <div class="controls chipsrow">
    <button data-pay="0" class="on">Any pay</button>
    <button data-pay="70000">$70K+</button>
    <button data-pay="100000">$100K+</button>
    <span class="sep"></span>
    <button data-mode="all" class="on">All</button>
    <button data-mode="new">New today</button>
    <button data-mode="top">Best matches (70%+)</button>
  </div>
  <p id="msg" class="msg"></p>
</header>
<main>
{cards}
<details><summary>Which websites worked today?</summary>
<table><tr><td><b>Source</b></td><td><b>Jobs</b></td><td><b>Note</b></td></tr>{rows}</table>
<p>The % score counts your skills, pay, industries you like, entry level, and years of experience asked.
Green chips = skills you have. Orange chips = skills the job asks for that you could learn.</p>
</details>
</main>
<script>{js}</script>
</body></html>"""


# ------------------------------------------------------------
# Run everything
# ------------------------------------------------------------
def main():
    cfg = yaml.safe_load(CONFIG_FILE.read_text())
    if "--sample" in sys.argv:
        print("Practice run with sample jobs (no internet)")
        raw = json.loads(SAMPLE_FILE.read_text())
        status = [{"source": "Sample data", "ok": True, "count": len(raw), "note": "practice run"}]
    else:
        print("Collecting jobs…")
        raw, status = collect_all(cfg)

    jobs = [j for j in raw if keep_job(j, cfg)]
    for j in jobs:
        add_pay(j)
    jobs = dedupe([j for j in jobs if pay_ok(j, cfg)])
    jobs = [score_job(j, cfg) for j in jobs]
    zips, towns = load_places()
    for j in jobs:
        add_position(j, towns)
    mark_new(jobs)
    jobs.sort(key=lambda j: (-j["is_new"], -j["score"]))

    PAGE_FILE.parent.mkdir(exist_ok=True)
    PAGE_FILE.write_text(build_page(jobs, status, {"zips": zips, "towns": towns}), encoding="utf-8")
    print(f"Done: {len(raw)} collected -> {len(jobs)} kept. Page saved to {PAGE_FILE}")


if __name__ == "__main__":
    main()
