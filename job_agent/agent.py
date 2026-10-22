"""The long-running agent: schedules searches and digests, and handles your Signal replies."""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel

from .config import Preferences, load_preferences, save_preferences
from .db import DB, now_iso
from .llm import LLM, BudgetExceeded, LLMFailed
from .pipeline import Pipeline
from .profile import load_profile
from .prompts import FEEDBACK_SYSTEM, feedback_task
from .signal_client import Signal

log = logging.getLogger(__name__)

HELP = """Eee hee! Lost already? Fine, here's what I answer to:
• more — I'll go dig up picks right now
• pause [hours] — I'll lurk in your shadow and keep quiet (default 12h)
• resume — call me back out
• status — what I've been up to
• prefs — what I'm hunting for on your behalf
Anything else I take as orders, like "#12 too sales-heavy", "more AI support roles" or "no crypto companies". Don't make me guess."""


class JobFeedback(BaseModel):
    ref: int
    sentiment: Literal["positive", "negative", "applied", "neutral"]
    reason: str


class FeedbackOut(BaseModel):
    reply: str
    updated_preferences: Preferences
    job_feedback: list[JobFeedback]
    send_now: bool
    pause_hours: int


def _hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = DB()
        self.llm = LLM(self.db, cfg)
        self.signal = Signal(cfg, self.db)
        self._profile = None
        self.pipeline = Pipeline(self.db, self.llm, cfg, self.profile)

    def profile(self) -> str:
        if self._profile is None:
            self._profile = load_profile(self.db, self.llm, self.cfg, load_preferences())
        return self._profile

    # ---------- time helpers ----------

    def now(self) -> datetime:
        return datetime.now(self.cfg.tz)

    def in_active_hours(self) -> bool:
        s = self.cfg.schedule
        t = self.now()
        start = t.replace(hour=_hm(s["active_start"])[0], minute=_hm(s["active_start"])[1], second=0)
        end = t.replace(hour=_hm(s["active_end"])[0], minute=_hm(s["active_end"])[1], second=0)
        return start <= t < end

    def day_start_utc(self) -> str:
        start = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc).isoformat(timespec="seconds")

    def paused(self) -> bool:
        until = self.db.get("paused_until")
        return bool(until) and datetime.fromisoformat(until) > self.now()

    # ---------- sending ----------

    def format_job(self, r, ref: int) -> str:
        bits = [r["location"] or "Location n/a"]
        if r["salary_max"]:
            lo = r["salary_min"] or r["salary_max"]
            bits.append(f"${lo / 1000:.0f}k–${r['salary_max'] / 1000:.0f}k" if lo != r["salary_max"] else f"${lo / 1000:.0f}k")
        bits.append(r["site"].replace("_", " ").title())
        tag = " (updated listing)" if r["updated"] else ""
        return (f"#{ref} {r['title']} — {r['company']}{tag}\n{' · '.join(bits)}\n"
                f"{r['blurb'] or r['summary']}\n{r['url']}")

    def send_jobs(self, rows, kind: str) -> int:
        """A job only counts as sent once Signal accepted the message. The `sent` rows (which give each
        job its #number) are written in a transaction that is rolled back if delivery fails, including
        if the process dies mid-send, so an undelivered job is offered again next time."""
        if not rows:
            return 0
        self.db.x("BEGIN IMMEDIATE")
        try:
            parts, refs = [], []
            for r in rows:
                cur = self.db.x("INSERT INTO sent(job_id,sent_at,kind,content_hash) VALUES(?,?,?,?)",
                                r["id"], now_iso(), kind, r["content_hash"])
                refs.append(cur.lastrowid)
                parts.append(self.format_job(r, cur.lastrowid))
            header = ("Hey! Drop whatever you're doing. This one's actually good, so don't let it slip away."
                      if kind == "urgent" else
                      f"Eee hee! I went prowling the job boards. {len(rows)} worth your time:")
            text = (header + "\n\n" + "\n\n".join(parts)
                    + f"\n\nWell? Tell me what you think (like \"#{refs[0]} not for me, too junior\"). "
                    "I'm not doing this for fun, you know.")
            delivered = self.signal.send(text)
        except BaseException:
            self.db.x("ROLLBACK")
            raise
        if not delivered:
            self.db.x("ROLLBACK")
            return 0
        self.db.x(f"UPDATE jobs SET updated=0 WHERE id IN ({','.join('?' * len(rows))})", *[r["id"] for r in rows])
        self.db.x("COMMIT")
        self.db.log_message("out", text)
        return len(rows)

    def send_text(self, text: str):
        if self.signal.send(text):
            self.db.log_message("out", text)

    # ---------- scheduled work ----------

    def due_digest_slot(self) -> str | None:
        """The latest digest time that has passed today and hasn't run. Missed earlier slots are skipped."""
        t = self.now()
        due = None
        for slot in self.cfg.schedule["digest_times"]:
            h, m = _hm(slot)
            if t >= t.replace(hour=h, minute=m, second=0) and not self.db.get(f"digest:{t.date()}:{slot}"):
                due = slot
        return due

    def run_digest(self, slot: str | None = None, forced=False) -> int:
        if slot:
            for s in self.cfg.schedule["digest_times"]:  # mark this and any earlier missed slot as done
                if _hm(s) <= _hm(slot):
                    self.db.set(f"digest:{self.now().date()}:{s}", now_iso())
        rows = self.pipeline.pick_digest()
        n = self.send_jobs(rows, "digest")
        if forced and n == 0:
            self.send_text("Hmph. Nothing out there worth your time yet. I'll keep hunting... you owe me.")
        log.info("digest %s sent %d", slot or "manual", n)
        return n

    def maybe_urgent(self):
        if not self.in_active_hours() or self.paused():
            return
        count = self.db.one("SELECT COUNT(*) AS c FROM sent WHERE kind='urgent' AND sent_at>=?", self.day_start_utc())["c"]
        if count >= self.cfg.delivery["max_urgent_per_day"]:
            return
        rows = self.pipeline.pick_urgent()
        if rows:
            self.send_jobs(rows[:1], "urgent")
            log.info("urgent sent")

    def search_and_screen(self):
        try:
            self.pipeline.search_cycle()
            self.pipeline.screen_pending()
        except BudgetExceeded as e:
            log.warning("%s", e)
        self.maybe_urgent()

    # ---------- inbound ----------

    def handle_message(self, text: str):
        self.db.log_message("in", text)
        cmd = text.strip().lower()
        if cmd in ("help", "?", "commands"):
            return self.send_text(HELP)
        if cmd in ("more", "send now", "send", "jobs"):
            return self.run_digest(forced=True)
        if cmd == "resume":
            self.db.set("paused_until", "")
            return self.send_text("Back out of the shadows. Next picks at the usual time, so try to keep up.")
        m = re.fullmatch(r"pause(?:\s+(\d+)\s*h?(?:ours?)?)?", cmd)
        if m:
            return self._pause(int(m.group(1) or 12))
        if cmd == "status":
            return self.send_text(self.status_text())
        if cmd in ("prefs", "preferences"):
            p = load_preferences()
            return self.send_text("Here's what I'm hunting for you:\n• " + "\n• ".join(p.target_roles) +
                                  f"\nPay: ${p.min_salary:,}+ (ideally ${p.target_salary:,}+)\n{p.work_arrangement}"
                                  "\nPrefer:\n• " + "\n• ".join(p.prefer or ["—"]) +
                                  "\nAvoid:\n• " + "\n• ".join(p.avoid or ["—"]) +
                                  (f"\nAvoided companies: {', '.join(p.avoid_companies)}" if p.avoid_companies else ""))
        self._feedback(text)

    def _pause(self, hours: int):
        until = self.now() + timedelta(hours=hours)
        self.db.set("paused_until", until.isoformat())
        self.send_text(f"Fine, I'll lurk in your shadow until {until:%a %-I:%M %p}. Say \"resume\" if you miss me sooner. Eee hee!")

    def _feedback(self, text: str):
        prefs = load_preferences()
        sent = self.db.q("SELECT s.ref, j.title, j.company FROM sent s JOIN jobs j ON j.id=s.job_id ORDER BY s.ref DESC LIMIT 20")
        try:
            out = self.llm.parse(
                purpose="feedback", model=self.cfg.llm["judge_model"],
                system=FEEDBACK_SYSTEM.format(home=self.cfg.home_location),
                content=feedback_task(json.dumps(prefs.model_dump(), indent=1),
                                      [f"#{r['ref']} {r['title']} — {r['company']}" for r in sent], text),
                schema=FeedbackOut, effort="low",
            )
        except (BudgetExceeded, LLMFailed) as e:
            log.warning("feedback not processed: %s", e)
            return self.send_text("Ugh, my magic's tapped out right now (the AI is unavailable or today's budget is spent). "
                                  "If that was something I should remember, tell me again later.")
        for fb in out.job_feedback:
            row = self.db.one("SELECT job_id FROM sent WHERE ref=?", fb.ref)
            if row:
                self.db.x("INSERT INTO feedback(job_id,sentiment,reason,created_at) VALUES(?,?,?,?)",
                          row["job_id"], fb.sentiment, fb.reason, now_iso())
        if out.updated_preferences.model_dump() != prefs.model_dump():
            save_preferences(out.updated_preferences)
            self.pipeline.requeue_for_judging()
            log.info("preferences updated: %s", out.updated_preferences.model_dump())
        if out.job_feedback:
            self.pipeline.requeue_for_judging()
        self.send_text(out.reply)
        if out.pause_hours > 0:
            self._pause(out.pause_hours)
        if out.send_now:
            self.run_digest(forced=True)

    def status_text(self) -> str:
        day_start = self.day_start_utc()
        c = lambda sql, *a: self.db.one(sql, *a)[0]  # noqa: E731
        return (
            "Here's what I've been up to while you weren't looking:\n"
            f"Listings seen today: {c('SELECT COUNT(*) FROM jobs WHERE first_seen>=?', day_start)}\n"
            f"Screened by AI today: {c('SELECT COUNT(*) FROM jobs WHERE first_seen>=? AND score IS NOT NULL', day_start)}\n"
            f"Waiting to be sent (score ≥ {self.cfg.delivery['min_score']}): "
            f"{len(self.pipeline.candidates(self.cfg.delivery['min_score'], 1000))}\n"
            f"Sent total: {c('SELECT COUNT(*) FROM sent')}\n"
            f"LLM spend today: ${self.llm.spent_today():.3f} of ${self.cfg.llm['daily_budget_usd']:.2f}\n"
            f"Last search: {self.db.get('last_search', 'never')}\n"
            f"{'Paused until ' + self.db.get('paused_until') if self.paused() else 'Active'}"
        )

    # ---------- main loop ----------

    def poll_inbox(self):
        for text in self.signal.receive():
            try:
                self.handle_message(text)
            except Exception:
                log.exception("failed handling message %r", text)

    def run_forever(self):
        log.info("agent started")
        self.profile()
        sched = self.cfg.schedule
        last_poll = 0.0
        while True:
            try:
                if time.time() - last_poll >= sched["signal_poll_seconds"]:
                    last_poll = time.time()
                    self.poll_inbox()

                last = self.db.get("last_search")
                if not last or datetime.fromisoformat(last) <= self.now() - timedelta(minutes=sched["search_interval_minutes"]):
                    self.search_and_screen()

                slot = self.due_digest_slot()
                if slot:
                    if self.in_active_hours() and not self.paused():
                        self.run_digest(slot)
                    else:
                        self.db.set(f"digest:{self.now().date()}:{slot}", "skipped")
            except Exception:
                log.exception("loop iteration failed")
            time.sleep(5)
