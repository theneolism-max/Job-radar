# Job Radar

Finds senior marketing roles (Director / VP / Head) in Mumbai, Navi Mumbai and remote-India,
without spending AI tokens on raw listings.

**How it works**

1. Twice a week (Tue & Fri, 6:37 PM IST) GitHub runs `collector.py`.
2. It pulls listings from LinkedIn, Indeed and Naukri (via [JobSpy](https://github.com/speedyapply/JobSpy)),
   iimjobs and Wellfound (via Apify), Himalayas, RemoteOK, We Work Remotely, and the public job boards
   of watchlist companies.
3. It applies the rules in `config.yaml` in code: seniority, function, location, posted within 14 days,
   salary floor, and drops roles already shown before.
4. It writes a short shortlist to `output/latest.md` (plus `latest.csv` and `run-log.json`).
5. Claude's "Job Picks" task reads only that file, checks the best ones, and emails the digest.

**Change the rules:** edit `config.yaml` (search terms, title words, cities, salary floor, watchlist boards).

**Run it now:** Actions tab → "Collect jobs" → Run workflow.

**Secret needed:** `APIFY_TOKEN` (Settings → Secrets and variables → Actions). Without it, iimjobs,
Wellfound and the Naukri backup are skipped; everything else still runs.

**Costs:** GitHub Actions is free well within limits. Apify spend is capped per run in `config.yaml`
(about US$1–3 a month expected, inside the free plan's monthly credit).
