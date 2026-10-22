import sqlite3
from datetime import datetime, timezone

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,            -- site:native_id
    site TEXT, url TEXT, title TEXT, company TEXT, location TEXT,
    is_remote INTEGER, salary_min REAL, salary_max REAL,   -- annualised USD
    description TEXT, date_posted TEXT,
    first_seen TEXT, last_seen TEXT,
    content_hash TEXT, fingerprint TEXT,
    prescore REAL DEFAULT 0,
    -- new -> screened | rejected | filtered | duplicate
    status TEXT DEFAULT 'new', reason TEXT,
    score INTEGER, summary TEXT,
    judged INTEGER DEFAULT 0, blurb TEXT,
    updated INTEGER DEFAULT 0,      -- materially changed since it was last sent
    attempts INTEGER DEFAULT 0      -- consecutive failed LLM attempts
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, score);
CREATE INDEX IF NOT EXISTS jobs_fp ON jobs(fingerprint);

CREATE TABLE IF NOT EXISTS sent (
    ref INTEGER PRIMARY KEY AUTOINCREMENT,   -- the #number you see in Signal
    job_id TEXT, sent_at TEXT, kind TEXT, content_hash TEXT
);
CREATE INDEX IF NOT EXISTS sent_job ON sent(job_id);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT, sentiment TEXT, reason TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT, text TEXT, ts TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, day TEXT, model TEXT, purpose TEXT,
    input_tokens INTEGER, output_tokens INTEGER,
    cache_read INTEGER, cache_write INTEGER, cost_usd REAL
);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path=DATA / "agent.db"):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=15000")  # a manual CLI run alongside the agent waits instead of failing
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        cols = {r["name"] for r in self.q("PRAGMA table_info(jobs)")}
        if "attempts" not in cols:
            self.x("ALTER TABLE jobs ADD COLUMN attempts INTEGER DEFAULT 0")

    def q(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql, *args):
        return self.conn.execute(sql, args)

    def get(self, key, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", key)
        return row["value"] if row else default

    def set(self, key, value):
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, str(value))

    def log_message(self, direction, text):
        self.x("INSERT INTO messages(direction,text,ts) VALUES(?,?,?)", direction, text, now_iso())
