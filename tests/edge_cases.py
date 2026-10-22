"""Edge-case scenarios for selection, dedupe, resends and failure handling.

Runs against a throwaway database with a fake LLM and fake Signal; costs nothing.
Usage: .venv/bin/python tests/edge_cases.py
"""
import re, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
SP = Path(tempfile.mkdtemp()); dbp = SP / "edge.db"
import job_agent.config as C
C.PREFS_PATH = SP / "edge_prefs.json"
import job_agent.agent as A, job_agent.pipeline as P
from job_agent.db import DB
A.DB = lambda: DB(dbp)
A.load_profile = lambda *a, **k: "PROFILE"

SCREEN = {}   # job_id -> (score, remote_status) ; missing => model omits it
JUDGE = {}    # job_id -> final_score (recommend if >= 0)
FAIL = {"judge": False}
def fake_parse(self, *, purpose, model, system, content, schema, effort, max_tokens=16000):
    ids = re.findall(r'job_id="([^"]+)"', str(content))
    if schema is P.ScreenBatch:
        return schema(results=[dict(job_id=i, score=SCREEN[i][0], remote_status=SCREEN[i][1], blockers=[], summary="s") for i in ids if i in SCREEN])
    if schema is P.JudgeBatch:
        if FAIL["judge"]: raise A.LLMFailed("judge down")
        return schema(results=[dict(job_id=i, final_score=abs(JUDGE.get(i, 90)), recommend=JUDGE.get(i, 90) >= 0, blurb="b", reason_if_not="x") for i in ids])
A.LLM.parse = fake_parse

SENDS = []; DELIVER = {"ok": True}
cfg = C.load_settings()
ag = A.Agent(cfg)
def fake_send(text):
    if DELIVER["ok"]: SENDS.append(text)
    return DELIVER["ok"]
ag.signal.send = fake_send
pl = ag.pipeline
prefs = C.load_preferences()

def job(i, title="Technical Account Manager", company=None, loc="Remote (United States)", sal=120000, desc="customer escalation aws llm remote", site="greenhouse"):
    return dict(id=f"{site}:{i}", site=site, url=f"https://x/{i}", title=title, company=company or f"Co{i}", location=loc,
                is_remote=True, salary_min=sal and sal - 20000, salary_max=sal, description=desc + f" unique{i}", date_posted="")
def sent_ids(): return [r["job_id"] for r in ag.db.q("SELECT job_id FROM sent ORDER BY ref")]
def digest():
    before = len(SENDS); ag.run_digest(); return len(SENDS) > before
ok = lambda c, msg: print(("PASS " if c else "FAIL ") + msg)

# 1. Ten perfect scores: who gets sent?
for i in range(10):
    j = job(i, sal=100000 + i * 10000, company="Same Co" if i in (8, 9) else None,  # 8 & 9: same company, different roles, highest pay
            title="Technical Account Manager" if i != 8 else "Support Engineer")
    pl.ingest(j, prefs); SCREEN[j["id"]] = (100, "remote"); JUDGE[j["id"]] = 100
pl.screen_pending(); digest()
first = sent_ids()
ok(first == ["greenhouse:9", "greenhouse:7", "greenhouse:6"], f"10 ties -> highest listed pay first, max one per company: {first}")
ok("greenhouse:8" not in first, "Same Co's second job held back while its sibling was sent in the same message")
digest(); ok(sent_ids()[3:] == ["greenhouse:8", "greenhouse:5", "greenhouse:4"], f"next digest: held-back job first, then down the list, no repeats: {sent_ids()[3:]}")

# 2. Judge breaks ties when it scores differently
JUDGE["greenhouse:0"] = 99; JUDGE["greenhouse:1"] = 100; JUDGE["greenhouse:2"] = 98
ag.db.x("UPDATE jobs SET judged=0 WHERE id IN ('greenhouse:0','greenhouse:1','greenhouse:2')")
digest(); ok(sent_ids()[6:9] == ["greenhouse:3", "greenhouse:1", "greenhouse:0"], f"judge scores decide order (3 & 1 tie at 100 -> higher pay first; 2 at 98 waits): {sent_ids()[6:9]}")

# 3. Failed delivery: nothing recorded, job offered again
j = job(20); pl.ingest(j, prefs); SCREEN[j["id"]] = (95, "remote"); pl.screen_pending()
DELIVER["ok"] = False; digest(); DELIVER["ok"] = True
ok("greenhouse:20" not in sent_ids(), "failed Signal send is rolled back")
digest(); ok("greenhouse:20" in sent_ids(), "job is sent on the next successful digest")

# 4. Same opening on two boards
a = job(30, title="Support Engineer", company="Dup Inc", site="linkedin"); b = job(31, title="Support Engineer", company="Dup Inc", site="indeed")
print("   ingest:", pl.ingest(a, prefs), pl.ingest(b, prefs))
SCREEN[a["id"]] = (90, "remote"); pl.screen_pending(); digest()
ok("linkedin:30" in sent_ids() and "indeed:31" not in sent_ids(), "cross-board duplicate never sent twice")

# 5. Same title, different city; the kept copy is on-site -> remote sibling released
c = job(40, title="Solutions Engineer", company="Multi", loc="San Francisco, CA"); d = job(41, title="Solutions Engineer", company="Multi", loc="Remote - US")
print("   ingest:", pl.ingest(c, prefs), pl.ingest(d, prefs))
SCREEN[c["id"]] = (85, "onsite"); SCREEN[d["id"]] = (85, "remote"); pl.screen_pending(); pl.screen_pending()
st = {r["id"]: r["status"] for r in ag.db.q("SELECT id,status FROM jobs WHERE id IN ('greenhouse:40','greenhouse:41')")}
ok(st == {"greenhouse:40": "rejected", "greenhouse:41": "screened"}, f"on-site copy rejected, remote copy screened: {st}")

# 6. Updated listing: resent once, tagged; not resent after negative feedback
sent9 = dict(ag.db.one("SELECT * FROM jobs WHERE id='greenhouse:9'"))
upd = job(9, sal=250000, company="Same Co"); print("   ingest:", pl.ingest(upd, prefs))
SCREEN["greenhouse:9"] = (100, "remote"); pl.screen_pending(); digest()
ok(SENDS[-1].count("(updated listing)") == 1 and sent_ids().count("greenhouse:9") == 2, "materially updated listing re-sent once, labelled")
pl.ingest(upd, prefs); digest(); ok(sent_ids().count("greenhouse:9") == 2, "unchanged re-sighting not re-sent")
ag.db.x("INSERT INTO feedback(job_id,sentiment,reason,created_at) VALUES('greenhouse:7','negative','meh','x')")
pl.ingest(job(7, sal=300000), prefs); SCREEN["greenhouse:7"] = (100, "remote"); pl.screen_pending(); digest()
ok(sent_ids().count("greenhouse:7") == 1, "update to a job you rejected is not re-sent")

# 7. Preferences changed after screening: avoided company / raised floor
for i, co, sal in [(50, "Avoid Me", 150000), (51, "LowPay", 80000)]:
    j = job(i, company=co, sal=sal); pl.ingest(j, prefs); SCREEN[j["id"]] = (99, "remote")
pl.screen_pending()
p2 = C.load_preferences(); p2.avoid_companies = ["Avoid Me"]; p2.min_salary = 90000; C.save_preferences(p2)
for _ in range(3): digest()
ok("greenhouse:50" not in sent_ids() and "greenhouse:51" not in sent_ids(), "avoided company / below new floor dropped at send time")
C.save_preferences(prefs)

# 8. Model keeps omitting a listing -> dropped after 3 tries
j = job(60); pl.ingest(j, prefs)
for _ in range(4): pl.screen_pending()
r = ag.db.one("SELECT status, reason, attempts FROM jobs WHERE id='greenhouse:60'")
ok(r["status"] == "filtered", f"listing the model never answers is retired: {dict(r)}")

# 9. Judge down: digest still sends previously approved jobs
j = job(69, sal=90000); pl.ingest(j, prefs); SCREEN[j["id"]] = (90, "remote"); pl.screen_pending()
pl.judge([ag.db.one("SELECT * FROM jobs WHERE id='greenhouse:69'")])   # approved earlier, not yet sent
j = job(70, sal=400000); pl.ingest(j, prefs); SCREEN[j["id"]] = (99, "remote"); pl.screen_pending()
FAIL["judge"] = True
n_before = len(sent_ids()); digest(); FAIL["judge"] = False
ok("greenhouse:69" in sent_ids() and "greenhouse:70" not in sent_ids(), "judge outage: approved backlog still sent, unjudged one waits")

# 10. Urgent only for fresh listings
ag.db.x("UPDATE jobs SET first_seen='2026-01-01T00:00:00+00:00' WHERE id='greenhouse:70'")
ok(pl.pick_urgent() == [], "old backlog listing never triggers an urgent message")
j = job(80); pl.ingest(j, prefs); SCREEN[j["id"]] = (97, "remote"); JUDGE[j["id"]] = 96; pl.screen_pending()
ok([r["id"] for r in pl.pick_urgent()] == ["greenhouse:80"], "fresh 96 triggers urgent")

# 11. Dedupe window: same title at same company sent > 45 days ago counts as a new opening
ag.db.x("UPDATE sent SET sent_at='2026-01-01T00:00:00+00:00' WHERE job_id='linkedin:30'")
ag.db.x("UPDATE jobs SET last_seen='2026-01-01T00:00:00+00:00' WHERE id='linkedin:30'")
e = job(32, title="Support Engineer", company="Dup Inc", site="ziprecruiter"); print("   ingest:", pl.ingest(e, prefs))
SCREEN[e["id"]] = (90, "remote"); pl.screen_pending()
ok(any(r["id"] == "ziprecruiter:32" for r in pl.candidates(78, 100)), "repost months later is eligible again")
