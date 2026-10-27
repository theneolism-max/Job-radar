"""Print a few job titles/locations per board, to confirm a discovered board belongs to the right company."""
import json, sys, requests
UA = {"User-Agent": "Mozilla/5.0 (job-radar check)"}
CHECK = [("ashby", "ema"), ("ashby", "zip"), ("ashby", "snowflake"), ("lever", "zeta"), ("lever", "outreach"),
         ("lever", "coupa"), ("workable", "flosum"), ("workable", "zenara-health"), ("ashby", "oyster"),
         ("ashby", "sievo"), ("ashby", "zapier"), ("lever", "tonkean"), ("smartrecruiters", "gepworldwide"),
         ("smartrecruiters", "whatfix"), ("ashby", "deel"), ("ashby", "lyzr")]
out = {}
for ats, slug in CHECK:
    try:
        if ats == "ashby":
            d = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=UA, timeout=20).json()
            jobs = [(j.get("title"), j.get("location"), j.get("jobUrl")) for j in d.get("jobs", [])]
        elif ats == "lever":
            d = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, headers=UA, timeout=20).json()
            jobs = [(j.get("text"), (j.get("categories") or {}).get("location"), j.get("hostedUrl")) for j in d]
        elif ats == "workable":
            d = requests.post(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", json={}, headers=UA, timeout=20).json()
            jobs = [(j.get("title"), (j.get("location") or {}).get("city"), j.get("shortcode")) for j in d.get("results", [])]
            out[f"{ats}:{slug}:total"] = d.get("total")
        else:
            d = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 5}, headers=UA, timeout=20).json()
            jobs = [(j.get("name"), (j.get("location") or {}).get("city"), (j.get("company") or {}).get("name")) for j in d.get("content", [])]
        out[f"{ats}:{slug}"] = {"count": len(jobs), "sample": jobs[:4]}
    except Exception as e:
        out[f"{ats}:{slug}"] = f"error {e}"
print(json.dumps(out, indent=1))
