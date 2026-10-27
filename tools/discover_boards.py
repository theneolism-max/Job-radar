"""One-off helper: find which watchlist companies use a job board with a free public API
(Greenhouse, Lever, Ashby, SmartRecruiters, Workable). Writes output/boards-found.json.
Run from the "Discover job boards" workflow; results are reviewed by hand before use."""
import json
import re
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (job-radar board discovery; one-off)"}
COMPANIES = [l.split(",")[1].strip() for l in (ROOT / "watchlist.csv").read_text().splitlines()[1:] if l.strip()]
EXTRA = {"Gong": ["gongio"], "Zip": ["ziphq"], "Haptik (Jio)": ["haptik"], "Remote.com": ["remote", "remotecom"],
         "Oyster HR": ["oyster", "oysterhr"], "Locus": ["locus", "locussh"], "Ema": ["ema", "emaunlimited", "ema-unlimited"],
         "Lyzr": ["lyzr", "lyzrai"], "RapidCanvas": ["rapidcanvas"], "Codvo.ai": ["codvo"], "Sarvam AI": ["sarvam"],
         "Fynd": ["fynd", "gofynd"], "Zeta": ["zeta", "zetatech"], "Blue Yonder": ["blueyonder"],
         "Scrut Automation": ["scrut", "scrutautomation"], "M2P Fintech": ["m2p", "m2pfintech"]}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def candidates(name):
    base = re.sub(r"\(.*?\)", "", name).strip()
    words = re.sub(r"[^A-Za-z0-9 ]", " ", base).lower().split()
    c = EXTRA.get(name, []) + ["".join(words), "-".join(words), words[0],
                               re.sub(r"(ai|io|com)$", "", "".join(words))]
    if base.lower().endswith((".ai", " ai")):
        c.append("".join(words[:-1]) if len(words) > 1 else words[0])
    seen, out = set(), []
    for x in c:
        if x and x not in seen and len(x) >= 3:
            seen.add(x)
            out.append(x)
    return out


def get(url, **kw):
    try:
        r = requests.get(url, headers=UA, timeout=20, **kw)
        return r.status_code, (r.json() if "json" in r.headers.get("content-type", "") else None)
    except Exception:
        return 0, None


def check(name, slug):
    key = norm(name.split("(")[0])[:5]
    s, d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    if s == 200 and d and key in norm(d.get("name", "")):
        n = len((get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")[1] or {}).get("jobs", []))
        return {"ats": "greenhouse", "slug": slug, "board_name": d.get("name"), "open_jobs": n}
    s, d = get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if s == 200 and isinstance(d, list) and d:
        return {"ats": "lever", "slug": slug, "open_jobs": len(d)}
    s, d = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if s == 200 and isinstance(d, dict) and "jobs" in d:
        return {"ats": "ashby", "slug": slug, "open_jobs": len(d["jobs"])}
    s, d = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100})
    if s == 200 and d and d.get("totalFound", 0) > 0:
        names = {norm((p.get("company") or {}).get("name", "")) for p in d.get("content", [])}
        if any(key in n for n in names):
            return {"ats": "smartrecruiters", "slug": slug, "open_jobs": d.get("totalFound")}
    s, d = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if s == 200 and d and key in norm(d.get("name", "")):
        return {"ats": "workable", "slug": slug, "board_name": d.get("name"), "open_jobs": len(d.get("jobs", []))}
    return None


found, missing = {}, []
for name in COMPANIES:
    hit = None
    for slug in candidates(name):
        hit = check(name, slug)
        if hit:
            break
        time.sleep(0.3)
    if hit:
        found[name] = hit
        print("FOUND", name, hit, flush=True)
    else:
        missing.append(name)
(ROOT / "output").mkdir(exist_ok=True)
(ROOT / "output" / "boards-found.json").write_text(json.dumps({"found": found, "not_found": missing}, indent=2))
print(f"{len(found)} found, {len(missing)} not found")
