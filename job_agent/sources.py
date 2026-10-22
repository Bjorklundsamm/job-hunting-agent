"""Fetch listings from job boards and normalise them into plain dicts.

Every fetcher returns a list of dicts with the keys:
id, site, url, title, company, location, is_remote, salary_min, salary_max, description, date_posted
Salaries are annualised USD (None when the listing doesn't say). is_remote is True/False/None (unknown).

Two kinds of source:
- query sources search by keyword (LinkedIn, Indeed, ZipRecruiter, Himalayas, ...), once per query.
- feed sources have no useful search, so their whole feed is pulled once per cycle and the filters
  sort it out (RemoteOK, We Work Remotely, company career boards).
A fetcher may also return a stub {"id": ..., "stub": True} meaning "still listed, unchanged".
"""

import concurrent.futures as cf
import html
import logging
import math
import random
import re
import time
import tomllib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from . import filters
from .config import ROOT

log = logging.getLogger(__name__)

JOBSPY_SITES = {"linkedin", "indeed", "glassdoor", "google"}
QUERY_SOURCES = JOBSPY_SITES | {"zip_recruiter", "remotive", "himalayas"}
FEED_SOURCES = {"remoteok", "weworkremotely", "company_boards"}
ANNUAL = {"yearly": 1, "annual": 1, "year": 1, "monthly": 12, "month": 12, "weekly": 52, "week": 52,
          "daily": 260, "day": 260, "hourly": 2080, "hour": 2080}
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) job-agent/0.1"}
# Company-board titles must clear this keyword score: big companies list hundreds of unrelated roles.
BOARD_MIN_TITLE_SCORE = 5


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or f <= 0 else f


def _str(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


def _annual(lo, hi, interval: str):
    mult = ANNUAL.get((interval or "").lower(), 1)
    if mult == 1 and (hi or lo or 0) < 1000:  # boards sometimes label hourly pay as yearly
        mult = ANNUAL["hourly"]
    return (lo * mult if lo else None), (hi * mult if hi else None)


def _iso_from_epoch(sec) -> str:
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return ""
    if sec > 1e11:  # milliseconds
        sec /= 1000
    return datetime.fromtimestamp(sec, timezone.utc).date().isoformat()


def strip_html(text: str) -> str:
    text = html.unescape(text or "") if "&lt;" in (text or "") else (text or "")
    text = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


_SAL = re.compile(r"\$?\s*(\d{2,3})(?:[,.](\d{3}))?\s*(k)?", re.I)


def _parse_salary_text(text: str):
    """Best-effort parse of '$90k - $120k' / '$95,000-$130,000' style strings."""
    vals = []
    for m in _SAL.finditer(text or ""):
        n = int(m.group(1))
        if m.group(2):
            n = n * 1000 + int(m.group(2))
        elif m.group(3):
            n *= 1000
        else:
            continue
        if 15000 <= n <= 1_000_000:
            vals.append(n)
    if not vals:
        return None, None
    return min(vals), max(vals)


# ---------------------------------------------------------------- query sources


def fetch_jobspy(site: str, query: str, cfg) -> list[dict]:
    from jobspy import scrape_jobs

    s = cfg.search
    kwargs = dict(
        site_name=[site],
        search_term=query,
        location="United States",
        is_remote=True,
        results_wanted=s["results_per_query"],
        hours_old=s["hours_old"],
        country_indeed="usa",
        verbose=0,
    )
    if site == "linkedin":
        kwargs["linkedin_fetch_description"] = True
    if site == "glassdoor":
        kwargs["fetch_description"] = True
    if site == "google":
        kwargs["google_search_term"] = f"remote {query} jobs in the United States posted in the last 3 days"
    df = scrape_jobs(**kwargs)
    out = []
    for r in df.to_dict("records"):
        usd = (_str(r.get("currency")) or "USD") == "USD"
        lo, hi = _annual(_num(r.get("min_amount")), _num(r.get("max_amount")), _str(r.get("interval")))
        out.append(
            dict(
                id=f"{site}:{r.get('id') or r.get('job_url')}",
                site=site,
                url=_str(r.get("job_url")) or _str(r.get("job_url_direct")),
                title=_str(r.get("title")),
                company=_str(r.get("company")),
                location=_str(r.get("location")),
                is_remote=bool(r.get("is_remote")) if r.get("is_remote") is not None else None,
                salary_min=lo if usd else None,
                salary_max=hi if usd else None,
                description=_str(r.get("description")),
                date_posted=_str(r.get("date_posted")),
            )
        )
    return out


ZR_BASE = "https://www.ziprecruiter.com"
ZR_REMOTE_TYPES = {2, 3}  # locationTypes enum: 2 = remote, 3 = remote optional (4 appears on hybrid roles)


def _zr_session():
    from jobspy.model import ScraperInput, Site
    from jobspy.util import TLSRotating, create_session

    session = create_session(TLSRotating(None), ScraperInput(site_type=[Site.ZIP_RECRUITER]))
    session.impersonate = "safari"
    return session


def fetch_ziprecruiter(query: str, cfg) -> list[dict]:
    """ZipRecruiter scraper for its current page format (jobspy 1.4's parser expects the old one).

    The search page embeds a `jobCards` list and `jobCount`; each job page embeds `jobDetails`
    with the full description.
    """
    from jobspy.ziprecruiter.util import page_values

    want = cfg.search["results_per_query"]
    session = _zr_session()
    params = {
        "search": query,
        "location": "United States",
        "refine_by_location_type": "only_remote",
        "days": max(1, math.ceil(cfg.search["hours_old"] / 24)),
    }
    cards, page = [], 1
    while len(cards) < want:
        path = "/jobs-search" if page == 1 else f"/jobs-search/{page}"
        r = session.get(ZR_BASE + path, params=params)
        r.raise_for_status()
        data = page_values(r.text, "jobCards", "jobCount")
        if not data["jobCards"]:
            break
        cards += data["jobCards"]
        if len(cards) >= data["jobCount"]:
            break
        page += 1
        time.sleep(random.uniform(2, 4))

    out = []
    for c in cards[:want]:
        pay = c.get("pay") or {}
        visible = (pay.get("metadata") or {}).get("visible")  # invisible pay is ZipRecruiter's own estimate
        loc = c.get("location") or {}
        types = {t.get("name") for t in c.get("locationTypes") or []}
        url = ZR_BASE + c["rawCanonicalZipJobPageUrl"]
        job = dict(
            id=f"zip_recruiter:{c.get('listingKey')}",
            site="zip_recruiter",
            url=url,
            title=c.get("title", ""),
            company=(c.get("company") or {}).get("name", ""),
            location=loc.get("displayName") or "Remote",
            is_remote=True if types & ZR_REMOTE_TYPES else None,
            salary_min=_num(pay.get("minAnnual")) if visible else None,
            salary_max=_num(pay.get("maxAnnual")) if visible else None,
            description=c.get("shortDescription", ""),
            date_posted=_iso_from_epoch(((c.get("status") or {}).get("postedAtUtc") or {}).get("seconds")),
        )
        if filters.title_score(job["title"]) > 0:  # only spend a page load on plausible titles
            try:
                time.sleep(random.uniform(1, 2))
                d = session.get(url, allow_redirects=True)
                d.raise_for_status()
                full = page_values(d.text, "jobDetails")["jobDetails"].get("htmlFullDescription")
                if full:
                    job["description"] = strip_html(full)
            except Exception as e:
                log.debug("zip_recruiter detail %s: %s", url, e)
        out.append(job)
    return out


def fetch_remotive(query: str, cfg) -> list[dict]:
    r = requests.get("https://remotive.com/api/remote-jobs", params={"search": query, "limit": 50}, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        lo, hi = _parse_salary_text(j.get("salary", ""))
        out.append(
            dict(
                id=f"remotive:{j['id']}",
                site="remotive",
                url=j["url"],
                title=j.get("title", ""),
                company=j.get("company_name", ""),
                location=f"Remote ({j.get('candidate_required_location') or 'unspecified'})",
                is_remote=True,
                salary_min=lo,
                salary_max=hi,
                description=strip_html(j.get("description", "")),
                date_posted=j.get("publication_date", ""),
            )
        )
    return out


def fetch_himalayas(query: str, cfg) -> list[dict]:
    r = requests.get("https://himalayas.app/jobs/api/search", params={"q": query}, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        usd = (j.get("currency") or "USD") == "USD"
        lo, hi = _annual(_num(j.get("minSalary")), _num(j.get("maxSalary")), j.get("salaryPeriod") or "")
        where = ", ".join(j.get("locationRestrictions") or []) or "anywhere"
        out.append(
            dict(
                id=f"himalayas:{j.get('guid')}",
                site="himalayas",
                url=j.get("applicationLink") or j.get("guid"),
                title=j.get("title", ""),
                company=j.get("companyName", ""),
                location=f"Remote ({where})",
                is_remote=True,
                salary_min=lo if usd else None,
                salary_max=hi if usd else None,
                description=strip_html(j.get("description", "")),
                date_posted=_iso_from_epoch(j.get("pubDate")),
            )
        )
    return out


# ---------------------------------------------------------------- feed sources


def fetch_remoteok(cfg) -> list[dict]:
    r = requests.get("https://remoteok.com/api", headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json():
        if not isinstance(j, dict) or not j.get("id"):
            continue  # first element is a legal notice
        out.append(
            dict(
                id=f"remoteok:{j['id']}",
                site="remoteok",
                url=j.get("url") or j.get("apply_url", ""),
                title=j.get("position", ""),
                company=j.get("company", ""),
                location=f"Remote ({j.get('location') or 'unspecified'})",
                is_remote=True,
                salary_min=_num(j.get("salary_min")),
                salary_max=_num(j.get("salary_max")),
                description=strip_html(j.get("description", "")),
                date_posted=j.get("date", ""),
            )
        )
    return out


WWR_FEEDS = [
    "https://weworkremotely.com/remote-jobs.rss",
    "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
]


def fetch_weworkremotely(cfg) -> list[dict]:
    out, seen = [], set()
    for feed in WWR_FEEDS:
        r = requests.get(feed, headers=UA, timeout=30)
        r.raise_for_status()
        for item in ET.fromstring(r.content).iter("item"):
            link = item.findtext("link") or item.findtext("guid") or ""
            if not link or link in seen:
                continue
            seen.add(link)
            company, _, title = (item.findtext("title") or "").partition(": ")
            if not title:
                company, title = "", company
            desc = strip_html(item.findtext("description") or "")
            lo, hi = _parse_salary_text(desc[:600])
            where = ", ".join(x for x in (item.findtext("region"), item.findtext("country"), item.findtext("state")) if x)
            out.append(
                dict(
                    id=f"weworkremotely:{link.rstrip('/').rsplit('/', 1)[-1]}",
                    site="weworkremotely",
                    url=link,
                    title=title.strip(),
                    company=company.strip(),
                    location=f"Remote ({where or 'unspecified'})",
                    is_remote=True,
                    salary_min=lo,
                    salary_max=hi,
                    description=desc,
                    date_posted=item.findtext("pubDate") or "",
                )
            )
    return out


def _board_job_ok(title: str, location: str, remote) -> bool:
    if filters.title_score(title) < BOARD_MIN_TITLE_SCORE:
        return False
    return remote is True or not filters.looks_non_us(location)


def _greenhouse(slug: str, known) -> list[dict]:
    base = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    r = requests.get(base, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        loc_type = next((m.get("value") for m in j.get("metadata") or [] if m.get("name") == "Location Type"), None)
        remote = True if "remote" in f"{loc} {loc_type}".lower() else (False if loc_type == "On-Site" else None)
        if not _board_job_ok(j.get("title", ""), loc, remote):
            continue
        jid = f"greenhouse:{slug}:{j['id']}"
        if known(jid) == j.get("updated_at"):  # unchanged since we stored it: skip the detail request
            out.append({"id": jid, "stub": True})
            continue
        try:
            d = requests.get(f"{base}/{j['id']}", params={"pay_transparency": "true"}, headers=UA, timeout=30).json()
        except Exception as e:
            log.debug("greenhouse detail %s: %s", jid, e)
            continue
        ranges = [p for p in d.get("pay_input_ranges") or [] if p.get("currency_type") == "USD"]
        out.append(
            dict(
                id=jid,
                site="greenhouse",
                url=j.get("absolute_url", ""),
                title=j.get("title", ""),
                company=j.get("company_name") or slug,
                location=f"{loc} ({loc_type})" if loc_type else loc,
                is_remote=remote,
                salary_min=min(p["min_cents"] for p in ranges) / 100 if ranges else None,
                salary_max=max(p["max_cents"] for p in ranges) / 100 if ranges else None,
                description=strip_html(d.get("content", "")),
                date_posted=j.get("updated_at", ""),
            )
        )
    return out


def _lever(slug: str, known) -> list[dict]:
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        loc = ", ".join(cats.get("allLocations") or []) or cats.get("location", "")
        wt = (j.get("workplaceType") or "").lower()
        remote = True if wt == "remote" else (False if wt == "onsite" else None)
        if not _board_job_ok(j.get("text", ""), f"{loc} {j.get('country', '')}", remote):
            continue
        sal = j.get("salaryRange") or {}
        lo, hi = (None, None)
        if sal.get("currency", "USD") == "USD":
            lo, hi = _annual(_num(sal.get("min")), _num(sal.get("max")), (sal.get("interval") or "").replace("per-", ""))
        lists = "\n\n".join(f"{x.get('text', '')}\n{strip_html(x.get('content', ''))}" for x in j.get("lists") or [])
        out.append(
            dict(
                id=f"lever:{slug}:{j['id']}",
                site="lever",
                url=j.get("hostedUrl", ""),
                title=j.get("text", ""),
                company=slug.title(),
                location=f"{loc} ({wt})" if wt else loc,
                is_remote=remote,
                salary_min=lo,
                salary_max=hi,
                description="\n\n".join(x for x in (j.get("descriptionPlain"), lists, j.get("additionalPlain")) if x),
                date_posted=_iso_from_epoch(j.get("createdAt")),
            )
        )
    return out


def _ashby(slug: str, known) -> list[dict]:
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
                     params={"includeCompensation": "true"}, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        loc = " | ".join(x for x in locs if x)
        wt = (j.get("workplaceType") or "").lower()
        remote = True if j.get("isRemote") or wt == "remote" else (False if wt == "onsite" else None)
        country = (((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")) or ""
        if not _board_job_ok(j.get("title", ""), f"{loc} {country}", remote):
            continue
        lo, hi = _parse_salary_text((j.get("compensation") or {}).get("scrapeableCompensationSalarySummary", ""))
        out.append(
            dict(
                id=f"ashby:{slug}:{j['id']}",
                site="ashby",
                url=j.get("jobUrl", ""),
                title=j.get("title", ""),
                company=slug.title(),
                location=f"{loc} ({wt})" if wt else loc,
                is_remote=remote,
                salary_min=lo,
                salary_max=hi,
                description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
                date_posted=(j.get("publishedAt") or "")[:10],
            )
        )
    return out


BOARD_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def fetch_company_boards(cfg, known) -> list[dict]:
    with open(ROOT / "companies.toml", "rb") as f:
        boards = [(ats, slug) for ats, slugs in tomllib.load(f).items() for slug in slugs if ats in BOARD_FETCHERS]

    def one(board):
        ats, slug = board
        try:
            return BOARD_FETCHERS[ats](slug, known)
        except Exception as e:
            log.warning("company board %s/%s failed: %s", ats, slug, e)
            return []

    with cf.ThreadPoolExecutor(6) as ex:
        results = list(ex.map(one, boards))
    log.info("company boards: %d boards, %d relevant listings", len(boards), sum(map(len, results)))
    return [j for res in results for j in res]


# ---------------------------------------------------------------- dispatch


def fetch(site: str, query: str | None, cfg, known=lambda _id: None) -> list[dict]:
    """`known(id)` returns the stored date_posted for an id we already have (or None)."""
    try:
        if site in JOBSPY_SITES:
            jobs = fetch_jobspy(site, query, cfg)
        elif site == "zip_recruiter":
            jobs = fetch_ziprecruiter(query, cfg)
        elif site == "remotive":
            jobs = fetch_remotive(query, cfg)
        elif site == "himalayas":
            jobs = fetch_himalayas(query, cfg)
        elif site == "remoteok":
            jobs = fetch_remoteok(cfg)
        elif site == "weworkremotely":
            jobs = fetch_weworkremotely(cfg)
        elif site == "company_boards":
            jobs = fetch_company_boards(cfg, known)
        else:
            log.warning("unknown source %s", site)
            return []
    except Exception as e:  # scrapers break and get rate-limited; one bad source must not stop the agent
        log.warning("fetch %s %r failed: %s", site, query, e)
        return []
    log.info("fetch %s %r -> %d", site, query, len(jobs))
    return [j for j in jobs if j.get("stub") or (j["title"] and j["url"])]
