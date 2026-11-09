#!/usr/bin/env python3
"""Poll career portals, filter intern roles, verify with Claude, push to ntfy.

  python watcher.py --check   # test every source once, print counts, no alerts
  python watcher.py --seed    # mark everything currently open as seen (no alerts)
  python watcher.py           # loop forever
"""
import json, re, sqlite3, sys, time, html, traceback
from pathlib import Path
import requests, yaml

ROOT = Path(__file__).parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
import os
CFG["ntfy_topic"] = os.environ.get("NTFY_TOPIC") or CFG["ntfy_topic"]
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) internship-watch/1.0"
T = 20

# ---------- adapters: each returns list of {id,title,location,url,text} ----------

def greenhouse(s):
    r = S.get(f"https://boards-api.greenhouse.io/v1/boards/{s['slug']}/jobs?content=true", timeout=T).json()
    return [dict(id=j["id"], title=j["title"], location=(j.get("location") or {}).get("name", ""),
                 url=j["absolute_url"], posted=(j.get("first_published") or "")[:10], text=html.unescape(j.get("content", ""))) for j in r["jobs"]]

def lever(s):
    r = S.get(f"https://api.lever.co/v0/postings/{s['slug']}?mode=json", timeout=T).json()
    return [dict(id=j["id"], title=j["text"], location=j.get("categories", {}).get("location", ""),
                 url=j["hostedUrl"], posted=time.strftime("%Y-%m-%d", time.gmtime(j.get("createdAt", 0)/1000)) if j.get("createdAt") else "", text=j.get("descriptionPlain", "")) for j in r]

def ashby(s):
    r = S.get(f"https://api.ashbyhq.com/posting-api/job-board/{s['slug']}", timeout=T).json()
    return [dict(id=j["id"], title=j["title"], location=j.get("location", ""),
                 url=j["jobUrl"], posted=(j.get("publishedAt") or "")[:10], text=j.get("descriptionPlain", "")) for j in r["jobs"]]

def workday(s):
    """Paginate through ALL results (Workday caps a page at 20)."""
    base = f"https://{s['host']}/wday/cxs/{s['tenant']}/{s['site']}"
    out, seen = [], set()
    for q in ("intern India", "internship India"):
        off, total = 0, 1
        while off < total and off < 2000:
            r = S.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": off, "searchText": q}, timeout=T).json()
            total = r.get("total", 0) if off == 0 else total
            posts = r.get("jobPostings", [])
            if not posts: break
            for j in posts:
                if "externalPath" not in j or j["externalPath"] in seen: continue
                seen.add(j["externalPath"])
                out.append(dict(id=j["externalPath"], title=j["title"], location=j.get("locationsText", ""),
                                url=f"https://{s['host']}/en-US/{s['site']}{j['externalPath']}", text="", posted=j.get("postedOn", ""),
                                _detail=f"{base}{j['externalPath']}"))
            off += 20; time.sleep(0.3)
    return out

def amazon(s):
    out, seen = [], set()
    for q in ("intern", "internship", "co-op"):
        off, hits = 0, 1
        while off < hits and off < 2000:
            r = S.get("https://www.amazon.jobs/en/search.json", params={"base_query": q, "country": "IND",
                      "result_limit": 100, "offset": off, "sort": "recent"}, timeout=T).json()
            hits = r.get("hits", 0)
            for j in r["jobs"]:
                if j["id_icims"] in seen: continue
                seen.add(j["id_icims"])
                out.append(dict(id=j["id_icims"], title=j["title"], location=j.get("normalized_location", ""),
                                url="https://www.amazon.jobs" + j["job_path"], posted=j.get("posted_date", ""),
                                text=j.get("description", "") + "\n" + j.get("basic_qualifications", "")))
            if not r["jobs"]: break
            off += 100
    return out

def apple(s):
    out = []
    for page in (1, 2, 3):
        h = S.get("https://jobs.apple.com/en-in/search", params={"search": "intern", "location": "india-INDC",
                  "sort": "newest", "page": page}, timeout=T).text
        m = re.search(r'window.__staticRouterHydrationData = JSON.parse\((".*?")\);', h, re.S)
        res = json.loads(json.loads(m.group(1)))["loaderData"]["search"].get("searchResults") or []
        for j in res:
            out.append(dict(id=j["positionId"], title=j["postingTitle"],
                            location=", ".join(l.get("name", "") for l in j.get("locations", [])),
                            url=f"https://jobs.apple.com/en-in/details/{j['positionId']}",
                            posted=j.get("postingDate", ""), text=j.get("jobSummary", "")))
        if len(res) < 20: break
    return out

def atlassian(s):
    r = S.get("https://www.atlassian.com/endpoint/careers/listings", timeout=T).json()
    return [dict(id=j["id"], title=j["title"], location="; ".join(j.get("locations", [])),
                 url=(j.get("portalJobPost") or {}).get("portalUrl", "https://www.atlassian.com/company/careers"),
                 posted=(j.get("portalJobPost") or {}).get("updatedDate", "")[:10],
                 text=re.sub("<[^>]+>", " ", (j.get("overview") or "") + (j.get("responsibilities") or ""))) for j in r]

def rippling_ats(s):
    out, page, pages = [], 0, 1
    while page < pages and page < 30:
        r = S.get(f"https://ats.rippling.com/api/v2/board/{s['slug']}/jobs", params={"page": page, "pageSize": 100}, timeout=T).json()
        pages = r.get("totalPages", 0)
        out += [dict(id=j["id"], title=j["name"], url=j["url"], text="",
                     location="; ".join(f"{l.get('name','')} {l.get('country','')}" for l in j.get("locations", [])))
                for j in r["items"]]
        page += 1
    return out

def intuit(s):
    out = []
    for p in (1, 2, 3):
        r = S.get("https://jobs.intuit.com/search-jobs/results", headers={"X-Requested-With": "XMLHttpRequest"},
                  params={"ActiveFacetID": 0, "CurrentPage": p, "RecordsPerPage": 50, "Distance": 50, "RadiusUnitType": 0,
                          "Keywords": "intern", "Location": "", "ShowRadius": "False", "IsPagination": "False",
                          "FacetType": 0, "SearchResultsModuleName": "Search Results",
                          "SearchFiltersModuleName": "Search Filters", "SortCriteria": 0, "SortDirection": 0, "SearchType": 5}, timeout=T).json()
        h = r.get("results", "")
        items = re.findall(r'<a[^>]+href="(/job/[^"]+)"[^>]*>(.*?)</a>', h, re.S)
        for href, inner in items:
            title = re.search(r"<h2>(.*?)</h2>", inner, re.S)
            loc = re.search(r'job-location[^>]*>(.*?)<', inner, re.S)
            out.append(dict(id=href, title=html.unescape(title.group(1)) if title else href.split("/")[3].replace("-", " "),
                            location=html.unescape(loc.group(1)).strip() if loc else href.split("/")[2],
                            url="https://jobs.intuit.com" + href, text=""))
        if len(items) < 50: break
    return out

def google(s):
    out = []
    for page in (1, 2, 3):
        h = S.get("https://www.google.com/about/careers/applications/jobs/results/",
                  params={"q": "intern", "location": "India", "target_level": "INTERN_AND_APPRENTICE", "page": page}, timeout=T).text
        ids = list(dict.fromkeys(re.findall(r'href="jobs/results/(\d+-[a-z0-9-]+)', h)))
        for slug in ids:
            jid, name = slug.split("-", 1)
            out.append(dict(id=jid, title=name.replace("-", " ").title(), location="India",
                            url=f"https://www.google.com/about/careers/applications/jobs/results/{slug}", text=""))
        if len(ids) < 20: break
    return out

def eightfold(s):
    """Microsoft, Qualcomm etc. (Eightfold 'pcsx' API; needs a session cookie from the careers page)."""
    host = s["host"]
    S.get(f"https://{host}/careers?query=intern&location=India", timeout=T)
    out, start = [], 0
    while start < 500:
        r = S.get(f"https://{host}/api/pcsx/search", headers={"Accept": "application/json"},
                  params={"domain": s["domain"], "query": "intern", "location": "India", "start": start,
                          "sort_by": "timestamp"}, timeout=T).json()
        pos = (r.get("data") or {}).get("positions") or []
        for j in pos:
            out.append(dict(id=j["id"], title=j["name"], location="; ".join(j.get("locations", [])),
                            url=f"https://{host}/careers/job/{j['id']}",
                            posted=time.strftime("%Y-%m-%d", time.gmtime(j["postedTs"])) if j.get("postedTs") else "",
                            text=j.get("department", "")))
        if len(pos) < 10: break
        start += len(pos)
    return out

def deshaw(s):
    h = S.get("https://www.deshawindia.com/careers", timeout=T).text
    out = []
    for slug in dict.fromkeys(re.findall(r'href="/careers/([a-z0-9-]+-\d{3,6})"', h)):
        if slug.startswith("all-positions"): continue
        out.append(dict(id=slug, title=slug.rsplit("-", 1)[0].replace("-", " ").title(), location="India",
                        url=f"https://www.deshawindia.com/careers/{slug}", text=""))
    return out

def oracle(s):
    u = (f"https://{s['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
         f"?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={s['site']},keyword=intern india,sortBy=POSTING_DATES_DESC,limit=200")
    items = S.get(u, timeout=T).json()["items"][0]["requisitionList"]
    return [dict(id=j["Id"], title=j["Title"], location=j.get("PrimaryLocation", ""),
                 url=f"https://{s['host'].replace('fa.', 'fa.')}/hcmUI/CandidateExperience/en/sites/{s['site']}/job/{j['Id']}",
                 posted=j.get("PostedDate", ""), text=j.get("ShortDescriptionStr", "")) for j in items]

def linkedin(s):
    out = []
    window = max(CFG["poll_seconds"] * 3, 3600)  # overlap windows so nothing slips through
    for q in ("software intern", "machine learning intern"):
        r = S.get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                  params={"keywords": f"{s['company']} {q}", "location": "India", "f_TPR": f"r{window}", "start": 0}, timeout=T)
        for m in re.finditer(r'data-entity-urn="urn:li:jobPosting:(\d+)".*?base-search-card__title">\s*(.*?)\s*</h3>.*?'
                             r'base-search-card__subtitle">.*?>\s*(.*?)\s*</a>.*?job-search-card__location">\s*(.*?)\s*</span>',
                             r.text, re.S):
            jid, title, comp, loc = m.groups()
            if s["company"].lower().replace(".", "").replace(" ", "") not in comp.lower().replace(".", "").replace(" ", ""):
                continue  # keyword search returns other companies too
            out.append(dict(id=jid, title=html.unescape(title), location=html.unescape(loc),
                            url=f"https://www.linkedin.com/jobs/view/{jid}", text=""))
    return out

# ---- Amazon job-ID scan: catches unlisted postings (not in site search) ----
def _kv(k, default=None):
    r = db.execute("select v from kv where k=?", (k,)).fetchone()
    return int(r[0]) if r else default
def _kvset(k, v):
    db.execute("insert or replace into kv values (?,?)", (k, str(v))); db.commit()

def _amazon_probe(i):
    """Return job dict if ID i is an India SWE/ML intern posting, else None. Cheap: redirect slug first."""
    r = S.get(f"https://www.amazon.jobs/en/jobs/{i}", allow_redirects=False, timeout=T)
    if r.status_code != 302:
        return "missing"
    url = r.headers["Location"]
    slug = url.rsplit("/", 1)[-1]
    if not re.search(r"intern|co-op", slug):
        return None
    page = S.get(url, timeout=T).text
    title = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    title = html.unescape(re.sub("<[^>]+>", "", title.group(1))).strip() if title else slug.replace("-", " ")
    text = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", page, flags=re.S)
    india = re.search(CFG["location_match"], text)
    return dict(id=str(i), title=title, location="India" if india else "non-India",
                url=url, text=re.sub(r"\s+", " ", text)[:6000])

def amazon_idscan(s):
    if _kv("amz_frontier") is None:  # init from newest searchable ID
        top = max(int(j["id_icims"]) for j in S.get("https://www.amazon.jobs/en/search.json",
                  params={"sort": "recent", "result_limit": 50}, timeout=T).json()["jobs"])
        _kvset("amz_frontier", top); _kvset("amz_back", top)
    out, f, misses = [], _kv("amz_frontier"), 0
    # 1) forward: new IDs above the frontier (stop after a run of 404s)
    i = f + 1
    while misses < s.get("forward_misses", 40) and i < f + s.get("forward_max", 150):
        j = _amazon_probe(i)
        if j == "missing":
            misses += 1
        else:
            misses, f = 0, i
            if j: out.append(j)
        i += 1; time.sleep(0.3)
    _kvset("amz_frontier", f)
    # 2) backward sweep: older IDs that get published late (like 10541345). Wraps around.
    b = _kv("amz_back")
    for i in range(b, b - s.get("back_per_cycle", 60), -1):
        j = _amazon_probe(i)
        if j and j != "missing":
            j["posted"] = "unknown (older unlisted ID, found by background sweep)"; out.append(j)
        time.sleep(0.3)
    b -= s.get("back_per_cycle", 60)
    _kvset("amz_back", b if b > f - s.get("back_depth", 80000) else f)
    return out

# ---- Telegram public channels (t.me/s/<name> web preview, no login) ----
CAREER_DOMAINS = r"(amazon\.jobs|myworkdayjobs\.com|greenhouse\.io|lever\.co|ashbyhq\.com|careers\.google\.com|google\.com/about/careers|careers\.microsoft\.com|metacareers\.com|jobs\.apple\.com|uber\.com/[a-z/-]*careers|oraclecloud\.com|deshaw\.com|sprinklr\.com|rippling\.com|atlassian\.com|salesforce\.com|linkedin\.com/jobs|qualcomm|intuit\.com|walmart)"

def telegram(s):
    page = S.get(f"https://t.me/s/{s['channel']}", timeout=T).text
    out = []
    for m in re.finditer(r'data-post="([^"]+)".*?tgme_widget_message_text[^>]*>(.*?)</div>', page, re.S):
        pid, body = m.groups()
        links = re.findall(r'href="(https?://[^"]+)"', body)
        text = html.unescape(re.sub(r"<br/?>", "\n", body)); text = re.sub("<[^>]+>", "", text)
        jobs = [l for l in links if re.search(CAREER_DOMAINS, l)]
        if not jobs:
            continue
        out.append(dict(id=pid, title=" ".join(text.split())[:140], location=text,
                        url=html.unescape(jobs[0]), text=text[:4000]))
    return out

ADAPTERS = dict(greenhouse=greenhouse, lever=lever, ashby=ashby, workday=workday,
                amazon=amazon, atlassian=atlassian, rippling_ats=rippling_ats, intuit=intuit, google=google, eightfold=eightfold, deshaw=deshaw, amazon_idscan=amazon_idscan, telegram=telegram, apple=apple, oracle=oracle, linkedin=linkedin)

# ---------- filtering / verification / notify ----------

MUST, DOMAIN = re.compile(CFG["title_must_match"]), re.compile(CFG["title_domain_match"])
LOC = re.compile(CFG["location_match"])
EXCL = re.compile(CFG.get("title_exclude", "(?!x)x"))

def keyword_ok(j):
    return MUST.search(j["title"]) and DOMAIN.search(j["title"]) and LOC.search(j["location"] or "") and not EXCL.search(j["title"])

def fetch_detail(j):
    if j.get("_detail") and not j["text"]:
        try:
            j["text"] = S.get(j["_detail"], timeout=T).json()["jobPostingInfo"].get("jobDescription", "")
        except Exception:
            pass
    j["text"] = re.sub(r"<[^>]+>", " ", j["text"] or "")[:6000]

_claude = None
def claude_verify(company, j):
    """Returns (is_match, one-line reason). Falls back to keyword result if no API key."""
    global _claude
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return True, "keyword match"
    try:
        import anthropic
        _claude = _claude or anthropic.Anthropic()
    except Exception:
        return True, "keyword match (Claude unavailable)"
    prompt = f"""Candidate: {CFG['candidate_profile']}
Job at {company}: {j['title']} — {j['location']}
Description (may be empty):
{j['text']}

Is this an internship the candidate is eligible for and that is in SWE/SDE or ML/AI/applied science?
Reject full-time/new-grad roles, non-engineering interns (sales, HR, finance, design), and roles
explicitly requiring a graduation year other than 2027/2028. If unclear, lean towards match.
Reply with JSON only: {{"match": true|false, "batch": "2027|2028|both|unknown", "reason": "<15 words"}}"""
    try:
        r = _claude.messages.create(model=CFG["claude_model"], max_tokens=150,
                                    messages=[{"role": "user", "content": prompt}])
        d = json.loads(re.search(r"\{.*\}", r.content[0].text, re.S).group())
        return bool(d["match"]), f"[{d.get('batch','?')}] {d.get('reason','')}"
    except Exception as e:
        return True, f"keyword match (Claude error: {e.__class__.__name__})"

LAST_CHECK = {}
def posted_line(company, j):
    if j.get("posted"):
        return f"Posted: {j['posted']}"
    prev = LAST_CHECK.get(company)
    if prev:
        return f"Posted: between {time.strftime('%d %b %H:%M', time.localtime(prev))} and now (no date on listing)"
    return "Posted: within ~1 day (no date on listing)"

def notify(company, j, reason):
    S.post(f"https://ntfy.sh/{CFG['ntfy_topic']}", data=f"{j['title']}\n{j['location'][:120]}\n{posted_line(company, j)}\n{reason}".encode(),
           headers={"Title": f"{company} internship".encode(), "Click": j["url"],
                    "Tags": "rotating_light", "Priority": "high"}, timeout=T)

# ---------- main loop ----------

db = sqlite3.connect(ROOT / "seen.db")
db.execute("create table if not exists seen (key text primary key, ts real)")
db.execute("create table if not exists kv (k text primary key, v text)")

def run_once(mode):
    for s in CFG["sources"]:
        try:
            jobs = ADAPTERS[s["type"]](s)
        except Exception as e:
            print(f"  !! {s['company']:<14} {s['type']:<10} ERROR {e.__class__.__name__}: {str(e)[:80]}")
            continue
        hits = [j for j in jobs if keyword_ok(j)]
        if mode == "check":
            print(f"  {s['company']:<14} {s['type']:<10} {len(jobs):>4} jobs, {len(hits):>3} intern-ish"
                  + (f"  e.g. {hits[0]['title'][:60]}" if hits else ""))
            continue
        for j in hits:
            key = f"{s['company']}|{j['id']}"
            if db.execute("select 1 from seen where key=?", (key,)).fetchone():
                continue
            db.execute("insert into seen values (?,?)", (key, time.time())); db.commit()
            if mode == "seed":
                continue
            fetch_detail(j)
            ok, reason = claude_verify(s["company"], j)
            print(f"{time.strftime('%H:%M')} {'MATCH' if ok else 'skip '} {s['company']}: {j['title']} — {reason}")
            if ok:
                notify(s["company"], j, reason)
        LAST_CHECK[s["company"]] = time.time()
        time.sleep(1)

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    mode = {"--check": "check", "--seed": "seed"}.get(arg, "run")
    if mode != "run":
        run_once(mode); sys.exit()
    if CFG["ntfy_topic"].startswith("CHANGE-ME"):
        sys.exit("Set ntfy_topic in config.yaml first.")
    if not db.execute("select 1 from seen limit 1").fetchone():
        print("First run: seeding existing postings (no alerts)…"); run_once("seed")
        if arg == "--once": sys.exit()
    if arg == "--once":   # single pass, for GitHub Actions
        run_once("run"); sys.exit()
    while True:
        try:
            run_once("run")
        except Exception:
            traceback.print_exc()
        time.sleep(CFG["poll_seconds"])
