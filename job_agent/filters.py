"""Free (no-LLM) processing: dedupe keys, boilerplate trimming, hard filters and keyword pre-scoring."""

import hashlib
import re

from .config import Preferences

# Title keywords -> weight. Drives which listings get LLM-screened first; it never rejects on its own
# unless nothing at all matches.
TITLE_WEIGHTS = {
    "technical account manager": 12, "tam": 6, "account manager": 3,
    "support engineer": 9, "technical support": 7, "support": 2,
    "solutions engineer": 8, "solution engineer": 8, "solutions architect": 6,
    "customer engineer": 8, "customer success engineer": 8, "forward deployed": 9,
    "customer success": 6, "client success": 6, "customer experience": 3, "onboarding": 4,
    "implementation": 5, "integration": 4, "escalation": 6,
    "it": 5, "systems engineer": 6, "systems administrator": 6, "sysadmin": 6,
    "infrastructure": 4, "administrator": 3, "help desk": 2, "account management": 5,
    "security": 3, "network": 3, "cloud": 2, "devops": 3, "site reliability": 3, "sre": 3,
    "ai": 6, "ml": 3, "llm": 7, "genai": 6, "agent": 4,
    "software engineer": 3, "developer": 2, "engineer": 1,
}
DESC_WEIGHTS = {
    "python": 1, "aws": 2, "llm": 2, "claude": 2, "openai": 1, "langgraph": 2, "mcp": 2,
    "api": 1, "rest": 0.5, "docker": 1, "typescript": 0.5, "customer": 1, "escalation": 2,
    "enterprise": 1, "stakeholder": 1, "technical account": 2, "security+": 1, "network": 1,
    "remote": 1, "hybrid": 0.5,
}

NON_US = re.compile(
    r"\b(canada|toronto|vancouver|montreal|mexico|brazil|argentina|colombia|chile|latam|"
    r"uk|united kingdom|england|london|ireland|dublin|germany|berlin|munich|france|paris|spain|madrid|"
    r"barcelona|portugal|lisbon|italy|netherlands|amsterdam|belgium|switzerland|zurich|austria|poland|"
    r"warsaw|sweden|stockholm|denmark|copenhagen|norway|finland|czech|prague|romania|ukraine|europe|eu|emea|"
    r"israel|tel aviv|uae|dubai|india|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|"
    r"singapore|philippines|manila|vietnam|indonesia|malaysia|thailand|japan|tokyo|korea|seoul|china|"
    r"hong kong|taiwan|australia|sydney|melbourne|canberra|new zealand|apac|africa|nigeria|kenya|egypt|"
    r"south africa|pakistan|turkey)\b",
    re.I,
)
US_WORDS = re.compile(
    r"\b(united states|usa|u\.s\.|us|americas?|north america|anywhere|worldwide|global|san francisco|"
    r"new york|seattle|austin|boston|chicago|denver|los angeles|san diego|atlanta)\b",
    re.I,
)
US_STATES = re.compile(  # case-sensitive on purpose: "Austin, TX"
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|"
    r"NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b"
)

BOILERPLATE = re.compile(
    r"(equal (employment )?opportunity employer|we are an equal opportunity|eeo statement|"
    r"reasonable accommodation|e-verify|pay transparency|privacy notice|"
    r"know your rights|#li-|applicants? (with|requiring) disabilit)",
    re.I,
)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fingerprint(job: dict) -> str:
    """Same company + same normalised title = same opening, even if posted on several boards."""
    company = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|the)\b", "", norm(job["company"])).strip()
    title = re.sub(r"\b(remote|hybrid|us|usa|united states)\b", "", norm(job["title"])).strip()
    return f"{company}|{title}"


def content_hash(job: dict) -> str:
    key = "|".join([norm(job["title"]), norm(job["description"]), str(job.get("salary_min")), str(job.get("salary_max"))])
    return hashlib.sha1(key.encode()).hexdigest()


def materially_changed(old: dict, new: dict) -> bool:
    """Ignore cosmetic re-posts; a changed title, pay range or substantially rewritten description counts."""
    if norm(old["title"]) != norm(new["title"]):
        return True
    if (old["salary_min"], old["salary_max"]) != (new.get("salary_min"), new.get("salary_max")) and new.get("salary_max"):
        return True
    a, b = set(norm(old["description"]).split()), set(norm(new["description"]).split())
    if not a or not b:
        return False
    return len(a & b) / len(a | b) < 0.85


def trim_description(text: str, limit: int) -> str:
    """Cut legal/EEO boilerplate (only when it starts after the first third) and cap the length."""
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    m = BOILERPLATE.search(text, len(text) // 3)
    if m:
        text = text[: m.start()].rstrip()
    return text if len(text) <= limit else text[:limit] + "\n[...truncated]"


def title_score(title: str) -> float:
    # Keyword must start at a word boundary but may continue ("architect" matches "architects"),
    # except very short keys like "it"/"ai", which must be whole words.
    padded = f" {norm(title)} "
    return sum(w for k, w in TITLE_WEIGHTS.items()
               if (f" {norm(k)} " if len(k) <= 3 else f" {norm(k)}") in padded)


def looks_non_us(location: str) -> bool:
    """True when a location names somewhere outside the US and nothing US-based or worldwide."""
    loc = location or ""
    return bool(NON_US.search(loc)) and not US_WORDS.search(loc) and not US_STATES.search(loc)


def hard_filter(job: dict, cfg, prefs: Preferences) -> str | None:
    """Return a rejection reason, or None if the job may proceed. Only rules that are never wrong live here."""
    title = f" {job['title'].lower()} "
    for bad in cfg.search["title_blocklist"]:
        if bad.lower() in title:
            return f"title blocklist: {bad.strip()}"
    if title_score(job["title"]) <= 0:
        return "title not relevant"
    if looks_non_us(job.get("location") or "") or looks_non_us(job["title"]):
        return "outside the US"
    company = norm(job["company"])
    for bad in prefs.avoid_companies:
        if norm(bad) and norm(bad) in company:
            return f"avoided company: {bad}"
    if job.get("salary_max") and job["salary_max"] < prefs.min_salary:
        return f"pay ${job['salary_max']:,.0f} below floor"
    loc = (job.get("location") or "").lower()
    desc = (job.get("description") or "").lower()
    home_city = cfg.home_location.split(",")[0].lower()
    nationwide = norm(loc) in ("", "usa", "us", "united states")
    if (job.get("is_remote") is False and not nationwide and home_city not in loc
            and "remote" not in loc and "hybrid" not in desc and "remote" not in desc):
        return "on-site only"
    return None


def prescore(job: dict) -> float:
    score = title_score(job["title"])
    desc = (job.get("description") or "").lower()
    score += sum(w for k, w in DESC_WEIGHTS.items() if k in desc)
    if job.get("salary_max") and job["salary_max"] >= 100000:
        score += 3
    return score
