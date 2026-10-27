"""Job Radar collector.

Pulls senior marketing roles from Indian and remote job boards, applies the hard
rules from config.yaml in code (no AI), drops anything already seen, gives each
role a rough score and writes a short shortlist for Claude to review.

Outputs (in output/):
  latest.md            compact shortlist for Claude (the only file Claude reads)
  latest.csv           same rows with more fields
  run-log.json         per-source counts and errors
State (in state/):
  seen.json            roles already shown, so they are not repeated
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=CFG["max_age_days"])
UA = {"User-Agent": "Mozilla/5.0 (job-radar personal job search; low volume)"}
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
LOG: dict[str, dict] = {}


def rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I) if pattern else re.compile(r"(?!x)x")


T_SEN, T_FUN = rx(CFG["title"]["seniority"]), rx(CFG["title"]["function"])
T_EXC, T_SPEC, T_CORE = rx(CFG["title"]["exclude"]), rx(CFG["title"]["specialist"]), rx(CFG["title"]["core"])
L = {k: rx(v) for k, v in CFG["location"].items()}
IND_HIGH, IND_MED = rx(CFG["industry_high"]), rx(CFG["industry_medium"])
SKILLS = [rx(r"\b" + s + r"\b") for s in CFG["skills"]]


@dataclass
class Job:
    source: str
    title: str
    company: str
    location: str = ""
    url: str = ""
    posted: datetime | None = None
    date_from_source_filter: bool = False   # source only returned jobs within the age window
    remote: bool = False
    india_board: bool = False               # listing comes from an India-specific search
    salary_inr_max: float | None = None
    salary_text: str = ""
    snippet: str = ""
    notes: list[str] = field(default_factory=list)
    score: int = 0
    work_model: str = ""

    @property
    def key(self) -> str:
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
        return f"{norm(self.company)}|{norm(self.title)}"


# ---------------------------------------------------------------- helpers
def log(source: str, **kw):
    LOG.setdefault(source, {}).update(kw)
    print(f"[{source}] " + ", ".join(f"{k}={v}" for k, v in kw.items()), flush=True)


def clean(text, n=300) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    text = re.sub(r"[#*_`>|]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:n]


def to_dt(v) -> datetime | None:
    if v in (None, "", 0) or (isinstance(v, float) and v != v):
        return None
    try:
        if isinstance(v, (int, float)):
            v = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(v, timezone.utc)
        if hasattr(v, "year") and not isinstance(v, datetime):   # date
            return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        s = str(v).strip().replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            from email.utils import parsedate_to_datetime
            d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_json(url, **kw):
    r = requests.get(url, headers=UA, timeout=40, **kw)
    r.raise_for_status()
    return r.json()


def run_apify(actor: str, run_input: dict) -> list[dict]:
    """Run an Apify actor synchronously with a hard spend cap; return dataset items."""
    url = f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
    params = {"timeout": 280, "maxTotalChargeUsd": CFG["apify"]["max_usd_per_actor_run"], "clean": "true"}
    r = requests.post(url, params=params, json=run_input, timeout=320,
                      headers={"Authorization": f"Bearer {APIFY_TOKEN}"})
    r.raise_for_status()
    return r.json()


def guarded(name):
    """Run a source; a failure in one source never stops the others."""
    def deco(fn):
        def wrap(*a, **kw):
            t = time.time()
            try:
                jobs = fn(*a, **kw) or []
                log(name, fetched=len(jobs), secs=round(time.time() - t))
                return jobs
            except Exception as e:  # noqa: BLE001
                log(name, fetched=0, error=f"{type(e).__name__}: {str(e)[:200]}")
                return []
        return wrap
    return deco


# ---------------------------------------------------------------- sources
def _jobspy_rows(df, source_label, india_board, from_filter, force_remote=False):
    out = []
    if df is None or df.empty:
        return out
    for r in df.to_dict("records"):
        sal, txt = None, ""
        mx, cur, itv = r.get("max_amount"), (r.get("currency") or ""), (r.get("interval") or "")
        if mx and mx == mx:
            mult = {"yearly": 1, "monthly": 12, "hourly": 2000, "weekly": 52, "daily": 260}.get(str(itv).lower(), 1)
            rate = CFG["salary"]["usd_to_inr"] if str(cur).upper() == "USD" else 1
            sal = float(mx) * mult * rate
            txt = f"{cur} {r.get('min_amount') or ''}-{mx} {itv}".strip()
        out.append(Job(
            source=f"{source_label}:{r.get('site')}", title=str(r.get("title") or ""),
            company=str(r.get("company") or ""), location=str(r.get("location") or ""),
            url=str(r.get("job_url_direct") or r.get("job_url") or ""),
            posted=to_dt(r.get("date_posted")), date_from_source_filter=from_filter,
            remote=force_remote or (bool(r.get("is_remote")) if r.get("is_remote") == r.get("is_remote") else False),
            india_board=india_board, salary_inr_max=sal, salary_text=txt,
            snippet=clean(r.get("description"))))
    return out


def jobspy_site(site: str) -> list[Job]:
    from jobspy import scrape_jobs
    jobs: list[Job] = []
    hours = CFG["max_age_days"] * 24
    for term in CFG["search_terms"]:
        searches = [dict(location="Mumbai, Maharashtra, India", is_remote=False)]
        if site in ("linkedin", "naukri"):
            searches.append(dict(location="India", is_remote=True))
        for s in searches:
            kw = dict(site_name=[site], search_term=term, results_wanted=25,
                      country_indeed="India", description_format="markdown", verbose=0, **s)
            if site == "google":
                kw["google_search_term"] = f"{term} jobs {'remote India' if s['is_remote'] else 'Mumbai'} since last 2 weeks"
            if not (site == "indeed" and s["is_remote"]):
                kw["hours_old"] = hours
            df = scrape_jobs(**kw)
            jobs += _jobspy_rows(df, "jobspy", True, from_filter="hours_old" in kw, force_remote=s["is_remote"])
            time.sleep(3)
    return jobs


@guarded("iimjobs")
def iimjobs() -> list[Job]:
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN not set")
    items = run_apify("data_daemon/iimjobs-jobs-scraper", {
        "queries": CFG["search_terms"], "locations": ["Mumbai", "Navi Mumbai", "Remote", "Anywhere in India"],
        "minExperience": 12, "postedWithinDays": CFG["max_age_days"], "includeDescription": False,
        "maxJobsPerQuery": 20, "maxJobs": CFG["apify"]["iimjobs_max_jobs"]})
    out = []
    for i in items:
        lpa = i.get("salaryMaxLpa")
        out.append(Job("iimjobs", i.get("title") or i.get("designation") or "",
                       (i.get("company") or {}).get("name") or "", ", ".join(i.get("locations") or []),
                       i.get("url") or "", to_dt(i.get("postedAt")), remote=bool(i.get("remote")),
                       india_board=True, salary_inr_max=(lpa * 1e5 if lpa and not i.get("salaryHidden") else None),
                       salary_text=(f"{i.get('salaryMinLpa')}-{lpa} LPA" if lpa and not i.get("salaryHidden") else ""),
                       snippet=clean((i.get("company") or {}).get("industry") or "")))
    return out


@guarded("wellfound")
def wellfound() -> list[Job]:
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN not set")
    out = []
    for remote in (False, True):
        items = run_apify("blackfalcondata/wellfound-scraper", {
            "query": "marketing", "location": ["india"] if not remote else [], "remote": remote,
            "experienceLevel": "staff", "fromDate": CUTOFF.date().isoformat(),
            "maxResults": CFG["apify"]["wellfound_max_results"] // 2, "maxPages": 2,
            "descriptionMaxLength": 300, "descriptionFormat": "text", "excludeEmptyFields": True})
        for i in items:
            accepted = ", ".join(i.get("acceptedRemoteLocations") or [])
            sal = i.get("salaryMax")
            cur = (i.get("salaryCurrency") or "USD").upper()
            out.append(Job("wellfound", i.get("title") or "", i.get("companyName") or "",
                           ", ".join(i.get("locationNames") or []) + (f" | remote: {accepted}" if accepted else ""),
                           i.get("portalUrl") or i.get("detailUrl") or "", to_dt(i.get("postedAt") or i.get("datePosted")),
                           remote=bool(i.get("remote")), india_board=not remote,
                           salary_inr_max=(sal * (CFG["salary"]["usd_to_inr"] if cur == "USD" else 1) if sal else None),
                           salary_text=i.get("compensation") or "", snippet=clean(i.get("description"))))
    return out


@guarded("naukri_apify")
def naukri_apify() -> list[Job]:
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN not set")
    out = []
    for term in CFG["search_terms"][:4]:
        for loc, wfh in (("Mumbai", None), ("", ["2"])):
            inp = {"keywords": term, "location": loc, "sort": "f", "jobAge": "15", "experience": 18,
                   "limit": CFG["apify"]["naukri_max_per_term"]}
            if wfh:
                inp["wfhType"] = wfh
            for i in run_apify("valig/naukri-jobs-scraper", inp):
                sal = (i.get("salary") or {})
                locs = ", ".join(l.get("label", "") for l in (i.get("locations") or []))
                out.append(Job("naukri_apify", i.get("title") or "", (i.get("company") or {}).get("name") or "",
                               locs, i.get("url") or "", to_dt(i.get("createdDate")), remote=bool(wfh),
                               india_board=True, salary_inr_max=sal.get("maximum") or None,
                               salary_text=sal.get("label") or "",
                               snippet=clean((i.get("description") or {}).get("short") or i.get("industry"))
                               + (" [recruiter]" if i.get("consultant") else "")))
    return out


@guarded("himalayas")
def himalayas() -> list[Job]:
    out = []
    for seniority in ("Director", "Executive"):
        for page in (1, 2):
            data = get_json("https://himalayas.app/jobs/api/search",
                            params={"q": "marketing", "country": "India", "seniority": seniority, "page": page})
            jobs = data.get("jobs") or []
            for j in jobs:
                restr = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
                cur = (j.get("currency") or "USD").upper()
                mx = j.get("maxSalary")
                out.append(Job("himalayas", j.get("title") or "", j.get("companyName") or "",
                               f"Remote ({restr})", j.get("applicationLink") or j.get("guid") or "",
                               to_dt(j.get("pubDate")), remote=True,
                               salary_inr_max=(mx * CFG["salary"]["usd_to_inr"] if mx and cur == "USD" else (mx if cur == "INR" else None)),
                               salary_text=f"{cur} {j.get('minSalary') or ''}-{mx or ''}" if mx else "",
                               snippet=clean(j.get("excerpt"))))
            if not jobs:
                break
            time.sleep(1)
    return out


@guarded("remoteok")
def remoteok() -> list[Job]:
    data = get_json("https://remoteok.com/api", params={"tag": "marketing"})
    out = []
    for j in data[1:] if isinstance(data, list) else []:
        mx = j.get("salary_max") or None
        out.append(Job("remoteok", j.get("position") or "", j.get("company") or "",
                       f"Remote ({j.get('location') or 'unspecified'})", j.get("url") or "",
                       to_dt(j.get("date") or j.get("epoch")), remote=True,
                       salary_inr_max=(mx * CFG["salary"]["usd_to_inr"] if mx else None),
                       salary_text=f"USD {j.get('salary_min')}-{mx}" if mx else "",
                       snippet=clean(j.get("description"), 200)))
    return out


@guarded("weworkremotely")
def weworkremotely() -> list[Job]:
    r = requests.get("https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss", headers=UA, timeout=40)
    r.raise_for_status()
    out = []
    for item in ET.fromstring(r.content).iter("item"):
        g = lambda tag: (item.findtext(tag) or "").strip()
        company, _, title = g("title").partition(":")
        out.append(Job("weworkremotely", title.strip() or company, company.strip() if title else "",
                       f"Remote ({g('region') or 'unspecified'})", g("link"), to_dt(g("pubDate")),
                       remote=True, snippet=clean(g("description"), 200)))
    return out


@guarded("watchlist_boards")
def watchlist_boards() -> list[Job]:
    out, wl = [], CFG["watchlist"]
    for board in wl.get("greenhouse", []):
        try:
            for j in get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs").get("jobs", []):
                out.append(Job(f"greenhouse:{board}", j.get("title", ""), j.get("company_name") or board,
                               (j.get("location") or {}).get("name", ""), j.get("absolute_url", ""),
                               to_dt(j.get("first_published") or j.get("updated_at"))))
        except Exception as e:  # noqa: BLE001
            log(f"greenhouse:{board}", error=str(e)[:120])
    for co in wl.get("lever", []):
        try:
            for j in get_json(f"https://api.lever.co/v0/postings/{co}", params={"mode": "json"}):
                c = j.get("categories") or {}
                out.append(Job(f"lever:{co}", j.get("text", ""), co, c.get("location", ""), j.get("hostedUrl", ""),
                               to_dt(j.get("createdAt")), remote=(j.get("workplaceType") == "remote")))
        except Exception as e:  # noqa: BLE001
            log(f"lever:{co}", error=str(e)[:120])
    for org in wl.get("ashby", []):
        try:
            for j in get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}").get("jobs", []):
                out.append(Job(f"ashby:{org}", j.get("title", ""), org, j.get("location", ""),
                               j.get("jobUrl", ""), to_dt(j.get("publishedAt")), remote=bool(j.get("isRemote"))))
        except Exception as e:  # noqa: BLE001
            log(f"ashby:{org}", error=str(e)[:120])
    return out


# ---------------------------------------------------------------- filtering
def classify_location(j: Job) -> str | None:
    """Return work model, or None if the location is not acceptable."""
    loc = j.location.lower()
    remote = j.remote or bool(L["remote_words"].search(loc))
    if L["navi_mumbai"].search(loc):
        return "Hybrid/Onsite Navi Mumbai" if not remote else "Remote (Navi Mumbai listed)"
    if L["mumbai"].search(loc):
        return "Hybrid/Onsite Mumbai" if not remote else "Remote (Mumbai listed)"
    if remote:
        if L["india_ok"].search(loc) or (j.india_board and not L["foreign_only"].search(loc)):
            return "Remote-India"
        if L["foreign_only"].search(loc):
            return None
        return "Remote (eligibility unclear)"
    if L["extra_hybrid_cities"].search(loc):
        return "Hybrid Bengaluru/Pune"
    return None


def passes(j: Job) -> str | None:
    """Return reason for rejection, or None if kept."""
    t = j.title
    if not (T_SEN.search(t) and T_FUN.search(t)):
        return "title"
    if T_EXC.search(t):
        return "title_excluded"
    if T_SPEC.search(t) and not T_CORE.search(t):
        return "specialist"
    if j.posted is None and not j.date_from_source_filter:
        return "no_date"
    if j.posted and j.posted < CUTOFF:
        return "too_old"
    if j.salary_inr_max and j.salary_inr_max < CFG["salary"]["floor_inr"]:
        return "below_floor"
    wm = classify_location(j)
    if not wm:
        return "location"
    j.work_model = wm
    return None


def prescore(j: Job) -> int:
    wm = j.work_model
    s = {"Remote-India": 30, "Remote (Mumbai listed)": 30, "Remote (Navi Mumbai listed)": 30,
         "Remote (eligibility unclear)": 20, "Hybrid/Onsite Navi Mumbai": 22,
         "Hybrid/Onsite Mumbai": 18, "Hybrid Bengaluru/Pune": 10}.get(wm, 0)
    t = j.title.lower()
    if re.search(r"\b(vp|vice president|svp|evp|senior director|sr\.? director)\b", t):
        s += 25
    elif re.search(r"\b(cmo|chief marketing)\b", t):
        s += 18
    else:
        s += 22
    blob = f"{j.title} {j.company} {j.snippet}"
    s += 20 if IND_HIGH.search(blob) else 13 if IND_MED.search(blob) else 7
    s += min(15, 3 * sum(bool(p.search(blob)) for p in SKILLS))
    if j.salary_inr_max is None:
        s += 4
    elif j.salary_inr_max >= CFG["salary"]["preferred_inr"]:
        s += 10
    else:
        s += 7
    return min(s, 100)


# ---------------------------------------------------------------- main
def main():
    src = CFG["sources"]
    jobs: list[Job] = []
    for site in ("linkedin", "indeed", "naukri", "google"):
        if src.get(site):
            jobs += guarded(f"jobspy:{site}")(jobspy_site)(site)
    if src.get("naukri_apify") == "always" or (
            src.get("naukri_apify") == "fallback" and src.get("naukri") and not LOG.get("jobspy:naukri", {}).get("fetched")):
        jobs += naukri_apify()
    for name, fn in (("iimjobs", iimjobs), ("wellfound", wellfound), ("himalayas", himalayas),
                     ("remoteok", remoteok), ("weworkremotely", weworkremotely),
                     ("watchlist_boards", watchlist_boards)):
        if src.get(name):
            jobs += fn()

    # filter
    reasons: dict[str, int] = {}
    kept: dict[str, Job] = {}
    for j in jobs:
        why = passes(j)
        if why:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        j.score = prescore(j)
        prev = kept.get(j.key)
        if not prev or (j.posted and not prev.posted) or len(j.snippet) > len(prev.snippet):
            if prev:
                j.notes.append(f"also on {prev.source}")
            kept[j.key] = j
        else:
            prev.notes.append(f"also on {j.source}")

    # drop already-seen roles
    seen_path = ROOT / "state" / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    horizon = (NOW - timedelta(days=CFG["seen_memory_days"])).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= horizon}
    fresh = [j for k, j in kept.items() if k not in seen]
    fresh.sort(key=lambda j: (-j.score, -(j.posted.timestamp() if j.posted else 0)))
    short = fresh[: CFG["shortlist_size"]]
    today = NOW.date().isoformat()
    for j in short:
        seen[j.key] = today

    # write outputs
    out = ROOT / "output"
    out.mkdir(exist_ok=True)
    seen_path.parent.mkdir(exist_ok=True)
    seen_path.write_text(json.dumps(seen, indent=0, sort_keys=True))
    cols = ["score", "title", "company", "work_model", "location", "posted", "salary_text", "source", "url", "notes", "snippet"]
    with open(out / "latest.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for j in short:
            d = asdict(j)
            d["posted"] = j.posted.date().isoformat() if j.posted else "<=14d (source filter)"
            d["notes"] = "; ".join(sorted(set(j.notes)))
            w.writerow([d[c] for c in cols])
    lines = [f"# Job Radar shortlist {today} ({len(short)} new roles; {len(kept)} passed filters; {len(jobs)} fetched)", ""]
    for n, j in enumerate(short, 1):
        posted = j.posted.date().isoformat() if j.posted else "<=14d"
        extra = f" | {j.salary_text}" if j.salary_text else ""
        if j.notes:
            extra += " | " + "; ".join(sorted(set(j.notes)))
        lines.append(f"{n}. [{j.score}] {j.title} | {j.company} | {j.work_model} ({j.location[:60]}) | {posted}{extra} | {j.source} | {j.url}")
        if j.snippet:
            lines.append(f"   {j.snippet[:180]}")
    (out / "latest.md").write_text("\n".join(lines) + "\n")
    LOG["_summary"] = {"date": today, "fetched": len(jobs), "passed": len(kept), "new": len(fresh),
                       "shortlisted": len(short), "rejected_by": reasons}
    (out / "run-log.json").write_text(json.dumps(LOG, indent=2))
    print(json.dumps(LOG["_summary"], indent=2))


if __name__ == "__main__":
    sys.exit(main())
