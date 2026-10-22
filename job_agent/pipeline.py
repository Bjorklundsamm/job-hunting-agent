"""Search -> dedupe -> free filters -> cheap LLM screen -> strong LLM judge -> pick what to send."""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel

from . import filters, sources
from .config import load_preferences
from .db import now_iso
from .llm import BudgetExceeded, LLMFailed
from .prompts import JUDGE_TASK, SCREEN_TASK, scoring_system

log = logging.getLogger(__name__)

# Consecutive LLM failures (error, refusal, or the listing missing from the reply) before giving up on a listing.
MAX_ATTEMPTS = 3
# How equally-scored listings are ordered: higher score; then found more recently (by day, since everything
# found in one cycle shares a timestamp); then listed pay, highest first; then id, so the order is stable.
RANK_ORDER = "score DESC, substr(first_seen,1,10) DESC, salary_max IS NULL, salary_max DESC, id"


class ScreenResult(BaseModel):
    job_id: str
    score: int
    remote_status: Literal["remote", "hybrid_commutable", "hybrid_far", "onsite", "unclear"]
    blockers: list[str]
    summary: str


class ScreenBatch(BaseModel):
    results: list[ScreenResult]


class JudgeResult(BaseModel):
    job_id: str
    final_score: int
    recommend: bool
    blurb: str
    reason_if_not: str


class JudgeBatch(BaseModel):
    results: list[JudgeResult]


class Pipeline:
    def __init__(self, db, llm, cfg, profile_loader):
        self.db, self.llm, self.cfg = db, llm, cfg
        self._profile_loader = profile_loader

    # ---------- ingest ----------

    def search_cycle(self, site: str | None = None) -> dict:
        """Search the next source in the rotation (or `site`), then store what it returns.

        Query sources run the next few queries from the rotating list; feed sources run once.
        """
        s = self.cfg.search
        if site is None:
            i = int(self.db.get("cycle_index", 0))
            site = s["sources"][i % len(s["sources"])]
            self.db.set("cycle_index", i + 1)
        if site in sources.FEED_SOURCES:
            queries = [None]
        else:
            qi = int(self.db.get("query_index", 0))
            queries = [s["queries"][(qi + k) % len(s["queries"])] for k in range(s["queries_per_cycle"])]
            self.db.set("query_index", qi + s["queries_per_cycle"])

        # Snapshot (not a live lookup): company boards are fetched from worker threads.
        known = {r["id"]: r["date_posted"] for r in self.db.q("SELECT id, date_posted FROM jobs WHERE site=?", site)}
        if site == "company_boards":
            known = {r["id"]: r["date_posted"] for r in self.db.q("SELECT id, date_posted FROM jobs WHERE site='greenhouse'")}

        prefs = load_preferences()
        stats = {"site": site, "queries": queries, "fetched": 0, "new": 0, "updated": 0, "filtered": 0, "duplicate": 0}
        for q in queries:
            for job in sources.fetch(site, q, self.cfg, known.get):
                stats["fetched"] += 1
                result = self.ingest(job, prefs)
                stats[result] = stats.get(result, 0) + 1
        self.db.set("last_search", now_iso())
        log.info("search cycle %s", stats)
        return stats

    def ingest(self, job: dict, prefs) -> str:
        if job.get("stub"):  # source says "still listed, unchanged"
            self.db.x("UPDATE jobs SET last_seen=? WHERE id=?", now_iso(), job["id"])
            return "seen"
        job["description"] = filters.trim_description(job["description"], self.cfg.llm["max_description_chars"])
        h, fp, now = filters.content_hash(job), filters.fingerprint(job), now_iso()
        old = self.db.one("SELECT * FROM jobs WHERE id=?", job["id"])
        if old:
            if old["content_hash"] == h or not filters.materially_changed(dict(old), job):
                self.db.x("UPDATE jobs SET last_seen=?,date_posted=? WHERE id=?", now, job["date_posted"], job["id"])
                return "seen"
            was_sent = self.db.one("SELECT 1 FROM sent WHERE job_id=?", job["id"]) is not None
            self.db.x(
                "UPDATE jobs SET title=?,company=?,location=?,salary_min=?,salary_max=?,description=?,last_seen=?,"
                "date_posted=?,content_hash=?,status='new',reason=NULL,score=NULL,summary=NULL,judged=0,blurb=NULL,updated=? WHERE id=?",
                job["title"], job["company"], job["location"], job["salary_min"], job["salary_max"],
                job["description"], now, job["date_posted"], h, int(was_sent), job["id"],
            )
            return "updated"

        status, reason = "new", filters.hard_filter(job, self.cfg, prefs)
        if reason:
            status = "filtered"
        else:
            # Same company + title seen recently (another board, or another city of the same posting).
            # Outside the window it's treated as a new opening, e.g. a role reposted months later.
            dup = self.db.one(
                "SELECT id FROM jobs WHERE fingerprint=? AND status NOT IN ('filtered','duplicate') AND last_seen>=?",
                fp, self._days_ago(self.cfg.delivery["dedupe_window_days"]),
            )
            if dup:
                status, reason = "duplicate", f"same opening as {dup['id']}"
                self.db.x("UPDATE jobs SET last_seen=? WHERE id=?", now, dup["id"])
        self.db.x(
            "INSERT INTO jobs(id,site,url,title,company,location,is_remote,salary_min,salary_max,description,"
            "date_posted,first_seen,last_seen,content_hash,fingerprint,prescore,status,reason)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            job["id"], job["site"], job["url"], job["title"], job["company"], job["location"],
            None if job["is_remote"] is None else int(job["is_remote"]), job["salary_min"], job["salary_max"],
            job["description"], job["date_posted"], now, now, h, fp, filters.prescore(job), status, reason,
        )
        return status

    # ---------- LLM stages ----------

    def _system(self) -> str:
        prefs = load_preferences()
        rows = self.db.q(
            "SELECT f.sentiment, f.reason, j.title, j.company FROM feedback f LEFT JOIN jobs j ON j.id=f.job_id"
            " ORDER BY f.id DESC LIMIT 15"
        )
        fb = [f"- {r['sentiment']}: {r['title'] or '?'} at {r['company'] or '?'} — {r['reason']}" for r in rows]
        return scoring_system(self._profile_loader(), json.dumps(prefs.model_dump(), indent=1), fb, self.cfg.home_location)

    @staticmethod
    def _listing(r) -> str:
        pay = ""
        if r["salary_max"]:
            pay = f"${(r['salary_min'] or r['salary_max']):,.0f}-${r['salary_max']:,.0f}/yr"
        return (
            f'<listing job_id="{r["id"]}">\nTitle: {r["title"]}\nCompany: {r["company"]}\n'
            f"Location: {r['location']} (board says remote: {'yes' if r['is_remote'] else 'unknown/no'})\n"
            f"Listed pay: {pay or 'not listed'}\n\n{r['description']}\n</listing>"
        )

    def screen_pending(self) -> int:
        """Cheap-model screening of the highest-prescore unscreened listings."""
        llm_cfg = self.cfg.llm
        cutoff = self._days_ago(self.cfg.delivery["candidate_max_age_days"])
        # Fresh finds first (so new posts aren't stuck behind a backlog), then best pre-score.
        fresh = self._days_ago(self.cfg.schedule["search_interval_minutes"] / 1440)
        rows = self.db.q(
            "SELECT * FROM jobs WHERE status='new' AND last_seen>=? AND prescore>0 AND attempts<?"
            " ORDER BY first_seen>=? DESC, prescore DESC, first_seen DESC LIMIT ?",
            cutoff, MAX_ATTEMPTS, fresh, llm_cfg["max_screen_per_cycle"],
        )
        if not rows:
            return 0
        system = self._system()
        done = 0
        size = llm_cfg["screen_batch_size"]
        for k in range(0, len(rows), size):
            batch = rows[k : k + size]
            # Counted up front: a listing the model keeps failing on or omitting is dropped after
            # MAX_ATTEMPTS instead of being paid for every cycle forever.
            self._bump_attempts(batch)
            try:
                out = self.llm.parse(
                    purpose="screen", model=llm_cfg["screen_model"], system=system,
                    content=SCREEN_TASK.format(listings="\n\n".join(self._listing(r) for r in batch)),
                    schema=ScreenBatch, effort=llm_cfg["screen_effort"],
                )
            except BudgetExceeded as e:
                log.warning("%s; screening paused until tomorrow", e)
                break
            except LLMFailed as e:
                log.warning("%s", e)
                continue
            ids = {r["id"] for r in batch}
            for res in out.results:
                if res.job_id not in ids:
                    continue
                blocked = res.remote_status in ("hybrid_far", "onsite")
                status = "rejected" if blocked or res.score < 50 else "screened"
                reason = "; ".join(res.blockers) or (res.remote_status if blocked else None)
                self.db.x("UPDATE jobs SET status=?,score=?,summary=?,reason=?,attempts=0 WHERE id=?",
                          status, res.score, res.summary, reason, res.job_id)
                if blocked:
                    self._release_duplicates(res.job_id)
                done += 1
        # Listings that never earned a single keyword hit are not worth an LLM call.
        self.db.x("UPDATE jobs SET status='filtered', reason='no relevant keywords' WHERE status='new' AND prescore<=0")
        self.db.x("UPDATE jobs SET status='filtered', reason='LLM failed repeatedly' WHERE status='new' AND attempts>=?",
                  MAX_ATTEMPTS)
        log.info("screened %d listings", done)
        return done

    def _bump_attempts(self, rows):
        self.db.x(f"UPDATE jobs SET attempts=attempts+1 WHERE id IN ({','.join('?' * len(rows))})",
                  *[r["id"] for r in rows])

    def _release_duplicates(self, job_id):
        """The copy we kept was rejected for its location (e.g. the SF on-site variant of a posting that
        also lists Remote). Its same-title siblings were parked as duplicates; let them be screened."""
        self.db.x(
            "UPDATE jobs SET status='new', reason=NULL WHERE status='duplicate'"
            " AND fingerprint=(SELECT fingerprint FROM jobs WHERE id=?) AND location!=(SELECT location FROM jobs WHERE id=?)",
            job_id, job_id,
        )

    def judge(self, rows) -> list:
        """Strong-model final read of a shortlist. Returns rows that are recommended, best first."""
        if not rows:
            return []
        llm_cfg = self.cfg.llm
        self._bump_attempts(rows)
        out = self.llm.parse(
            purpose="judge", model=llm_cfg["judge_model"], system=self._system(),
            content=JUDGE_TASK.format(listings="\n\n".join(self._listing(r) for r in rows)),
            schema=JudgeBatch, effort=llm_cfg["judge_effort"],
        )
        ids = {r["id"] for r in rows}
        for res in out.results:
            if res.job_id not in ids:
                continue
            if res.recommend:
                self.db.x("UPDATE jobs SET judged=1,score=?,blurb=?,attempts=0 WHERE id=?",
                          res.final_score, res.blurb, res.job_id)
            else:
                self.db.x("UPDATE jobs SET judged=1,score=?,status='rejected',reason=?,attempts=0 WHERE id=?",
                          res.final_score, f"judge: {res.reason_if_not}", res.job_id)
        return self.db.q(
            f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(ids))}) AND status='screened' AND judged=1"
            f" ORDER BY {RANK_ORDER}", *ids,
        )

    # ---------- selection ----------

    @staticmethod
    def _days_ago(days: float) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")

    def _eligible_sql(self) -> str:
        """A screened listing may be sent unless it (or the same opening on another board, sent within the
        dedupe window) has already been sent. A sent listing comes back only after a material update,
        and never if you reacted to it negatively or already applied."""
        return (
            "status='screened' AND last_seen>=? AND score>=? AND attempts<? AND ("
            " (id NOT IN (SELECT job_id FROM sent)"
            "  AND fingerprint NOT IN (SELECT j2.fingerprint FROM sent s JOIN jobs j2 ON j2.id=s.job_id"
            "                          WHERE s.sent_at>=?))"
            " OR (updated=1 AND content_hash NOT IN (SELECT content_hash FROM sent WHERE job_id=jobs.id)"
            "     AND id NOT IN (SELECT job_id FROM feedback WHERE sentiment IN ('negative','applied'))))"
        )

    def candidates(self, min_score: int, limit: int, fresh_hours: float | None = None):
        d = self.cfg.delivery
        args = [self._days_ago(d["candidate_max_age_days"]), min_score, MAX_ATTEMPTS, self._days_ago(d["dedupe_window_days"])]
        extra = ""
        if fresh_hours:
            extra, args = " AND first_seen>=?", args + [self._days_ago(fresh_hours / 24)]
        rows = self.db.q(f"SELECT * FROM jobs WHERE {self._eligible_sql()}{extra} ORDER BY {RANK_ORDER} LIMIT ?",
                         *args, limit)
        return self._recheck(rows)

    def _recheck(self, rows) -> list:
        """Re-apply the free filters with today's preferences: you may have added an avoided company or
        raised your pay floor since these were screened."""
        prefs = load_preferences()
        ok = []
        for r in rows:
            reason = filters.hard_filter(dict(r), self.cfg, prefs)
            if reason:
                self.db.x("UPDATE jobs SET status='filtered', reason=? WHERE id=?", reason, r["id"])
            else:
                ok.append(r)
        return ok

    def _diverse(self, rows, n) -> list:
        """Best-first, but at most one job per company in a single message."""
        picked, companies = [], set()
        for r in rows:
            key = filters.norm(r["company"])
            if key in companies:
                continue
            picked.append(r)
            companies.add(key)
            if len(picked) == n:
                break
        return picked

    def _judge_safely(self, rows) -> bool:
        try:
            self.judge(rows)
            return True
        except (BudgetExceeded, LLMFailed) as e:
            log.warning("judge skipped: %s", e)
            return False

    def pick_digest(self) -> list:
        d = self.cfg.delivery
        pool = self.candidates(d["shortlist_score"], 40)
        unjudged = [r for r in pool if not r["judged"]][: self.cfg.llm["max_judge_candidates"]]
        if unjudged:
            # If the judge is unavailable, still send from listings it already approved earlier.
            self._judge_safely(unjudged)
        return self._diverse([r for r in self.candidates(d["min_score"], 40) if r["judged"]], d["max_per_digest"])

    def pick_urgent(self) -> list:
        """Only listings first seen in the last `urgent_fresh_hours` qualify: urgent means new, not backlog."""
        d = self.cfg.delivery
        hours = d["urgent_fresh_hours"]
        pool = [r for r in self.candidates(d["urgent_shortlist_score"], 3, hours) if not r["judged"]]
        if pool and not self._judge_safely(pool):
            return []
        return [r for r in self.candidates(d["urgent_score"], 1, hours) if r["judged"]]

    def requeue_for_judging(self):
        """After preferences change, let the judge re-read unsent candidates with the new preferences."""
        self.db.x("UPDATE jobs SET judged=0, attempts=0 WHERE status='screened' AND id NOT IN (SELECT job_id FROM sent)")
