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
from urllib.parse import urlparse

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


def make_job(source, company, title, location, url, description, days_ago):
    return {
        "source": source,
        "company": (company or "").strip(),
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": url,
        "description": description or "",
        "days_ago": days_ago,
    }


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

    found = {}
    for term in cfg["search_terms"]:
        for offset in (0, 20, 40):  # up to 60 results per search word
            r = requests.post(f"{api}/jobs", headers=HEADERS, timeout=30, json={
                "appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            r.raise_for_status()
            postings = r.json().get("jobPostings", [])
            for p in postings:
                if p.get("externalPath") and passes_title(p.get("title", ""), cfg):
                    found[p["externalPath"]] = p
            if len(postings) < 20:
                break
            time.sleep(1)

    jobs = []
    for path, p in found.items():
        days = days_ago_from_text(p.get("postedOn"))
        if days is not None and days > cfg["max_days_old"]:
            continue
        # Open each matching job to read the full description.
        desc, location, url = "", p.get("locationsText", ""), f"{base}/{site}{path}"
        try:
            d = requests.get(f"{api}{path}", headers=HEADERS, timeout=30).json()
            info = d.get("jobPostingInfo", {})
            desc = html_to_text(info.get("jobDescription", ""))
            location = info.get("location") or location
            if info.get("remoteType"):
                location += f" ({info['remoteType']})"
            url = info.get("externalUrl") or url
        except Exception:
            pass
        jobs.append(make_job(emp["name"], emp["name"], p.get("title"), location, url, desc, days))
        time.sleep(1)
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
            r = requests.get(f"https://api.adzuna.com/v1/api/jobs/us/search/{page}", headers=HEADERS, timeout=30,
                             params={"app_id": app_id, "app_key": app_key, "what": term, "where": az["where"],
                                     "results_per_page": 50, "max_days_old": cfg["max_days_old"],
                                     "content-type": "application/json"})
            r.raise_for_status()
            results = r.json().get("results", [])
            for j in results:
                company = (j.get("company") or {}).get("display_name", "")
                if az.get("only_employers") and not any_match(az["only_employers"], company):
                    continue
                jobs.append(make_job("Adzuna", company, j.get("title"),
                                     (j.get("location") or {}).get("display_name"), j.get("redirect_url"),
                                     html_to_text(j.get("description")), days_ago_from_date(j.get("created"))))
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
            status.append({"source": name, "ok": False, "count": 0, "note": str(err)[:160]})
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
    remote = bool(re.search(r"\bremote\b|work (from|at) home|virtual", f"{job['location']} {job['title']}", re.I))
    job["remote"] = remote
    return any_match(cfg["locations_ok"], job["location"]) or (cfg["include_remote"] and remote)


def dedupe(jobs):
    """The same job can show up on two sites. Keep one (prefer the employer's own site)."""
    best = {}
    for j in jobs:
        key = re.sub(r"\W+", " ", f"{j['company'][:12]} {j['title']}".lower()).strip()
        if key not in best or (best[key]["source"] == "Adzuna" and j["source"] != "Adzuna"):
            best[key] = j
    return list(best.values())


# ------------------------------------------------------------
# Step 3: SCORE each job against my skills (regex only)
# ------------------------------------------------------------
# Skills that job posts often ask for. Used to show "gaps" (things to learn).
COMMON_SKILLS = ["SQL", "Python", "Excel", "Tableau", "Power BI", "R", "SAS", "Epic", "EHR", "Snowflake",
                 "statistics", "dashboards", "data cleaning", "data visualization", "machine learning",
                 "Agile", "project management", "HIPAA", "claims", "Cerner", "Alteryx", "Looker", "ETL"]


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
    if job["days_ago"] is not None and job["days_ago"] <= 3:
        score += 5                               # fresh posts: apply early

    job.update(score=max(0, min(score, 100)), matched=matched, gaps=gaps, years=years,
               summary=find_qualifications(job["description"]))
    return job


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
    search_text = e(f"{j['title']} {j['company']} {j['location']}".lower())
    return f"""
<article class="card" data-score="{j['score']}" data-new="{int(j['is_new'])}" data-text="{search_text}">
  <div class="top">
    <div class="score {level}">{j['score']}<small>%</small></div>
    <div class="head">
      <h2>{new}{e(j['title'])}</h2>
      <p class="meta">{e(j['company'])} · {e(j['location']) or 'Location not listed'}</p>
      <p class="meta">{posted_text(j['days_ago'])}{years}{via}</p>
    </div>
  </div>
  {f'<p class="label">You have</p><div class="chips">{chips}</div>' if chips else ''}
  {f'<p class="label">They also ask for</p><div class="chips">{gaps}</div>' if gaps else ''}
  {f'<p class="label">Qualifications</p><p class="summary">{e(j["summary"])}</p>' if j['summary'] else ''}
  <a class="apply" href="{e(j['url'])}" target="_blank" rel="noopener">Open job &amp; apply →</a>
</article>"""


def build_page(jobs, status):
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
details{margin:20px 0;color:var(--muted);font-size:13.5px}table{width:100%;border-collapse:collapse}
td{border-top:1px solid var(--line);padding:6px 4px;vertical-align:top}.empty{text-align:center;color:var(--muted)}
"""
    js = """
let mode='all';const q=document.getElementById('q');
function show(){const t=q.value.toLowerCase().trim();let n=0;
document.querySelectorAll('.card').forEach(c=>{let ok=c.dataset.text.includes(t);
if(mode==='new')ok=ok&&c.dataset.new==='1';if(mode==='top')ok=ok&&+c.dataset.score>=70;
c.style.display=ok?'':'none';if(ok)n++});document.getElementById('n').textContent=n;}
document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{mode=b.dataset.mode;
document.querySelectorAll('[data-mode]').forEach(x=>x.classList.toggle('on',x===b));show();});
q.oninput=show;show();
"""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>My Job Finder</title><style>{css}</style></head>
<body>
<header>
  <h1>My Job Finder</h1>
  <p class="sub">Updated {updated} · <b id="n">{len(jobs)}</b> jobs shown · {new_count} new today</p>
  <div class="controls">
    <input id="q" type="search" placeholder="Search title, company, town…">
    <button data-mode="all" class="on">All</button>
    <button data-mode="new">New today</button>
    <button data-mode="top">Best matches (70%+)</button>
  </div>
</header>
<main>
{cards}
<details><summary>Which websites worked today?</summary>
<table><tr><td><b>Source</b></td><td><b>Jobs</b></td><td><b>Note</b></td></tr>{rows}</table>
<p>The % score counts your skills, healthcare focus, entry level, and years of experience asked.
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

    jobs = dedupe([j for j in raw if keep_job(j, cfg)])
    jobs = [score_job(j, cfg) for j in jobs]
    mark_new(jobs)
    jobs.sort(key=lambda j: (-j["is_new"], -j["score"]))

    PAGE_FILE.parent.mkdir(exist_ok=True)
    PAGE_FILE.write_text(build_page(jobs, status), encoding="utf-8")
    print(f"Done: {len(raw)} collected -> {len(jobs)} kept. Page saved to {PAGE_FILE}")


if __name__ == "__main__":
    main()
