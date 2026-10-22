import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"


class Preferences(BaseModel):
    """What you're looking for. Seeded from your request, then rewritten by your Signal feedback."""

    min_salary: int
    target_salary: int
    work_arrangement: str
    employment_type: str
    target_roles: list[str]
    prefer: list[str]
    avoid: list[str]
    avoid_companies: list[str]


DEFAULT_PREFERENCES = Preferences(
    min_salary=70000,
    target_salary=100000,
    work_arrangement="Remote (US) strongly preferred. Hybrid only if commutable from San Diego, CA. No on-site-only roles.",
    employment_type="Full-time preferred; an exceptional part-time role is acceptable.",
    target_roles=[
        "Technical Account Manager",
        "AI support / AI solutions engineering",
        "Support engineer / technical support engineer",
        "IT (systems, infrastructure, administration)",
        "Customer / forward deployed engineer",
        "Customer success (CSM), especially technical, enterprise SaaS, or AI-product CSM roles",
        "Software engineering (lowest priority: wants to move away from pure SWE)",
    ],
    prefer=[
        "Customer-facing technical roles that use the AWS, AI integration, and escalation background",
        "Companies building or deploying AI/LLM products",
    ],
    avoid=[
        "Quota-carrying sales roles",
        "Pure heads-down software engineering with no customer or stakeholder contact",
    ],
    avoid_companies=[],
)


@dataclass
class Settings:
    raw: dict
    tz: ZoneInfo
    signal_cli: str
    signal_mode: str
    bot_number: str | None
    user_number: str | None

    def __getattr__(self, name):
        try:
            return self.raw[name]
        except KeyError as e:
            raise AttributeError(name) from e


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    with open(ROOT / "config.toml", "rb") as f:
        raw = tomllib.load(f)
    DATA.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    return Settings(
        raw=raw,
        tz=ZoneInfo(raw["timezone"]),
        signal_cli=os.getenv("SIGNAL_CLI_PATH", "signal-cli"),
        signal_mode=os.getenv("SIGNAL_MODE", "note_to_self" if os.getenv("SIGNAL_USER_NUMBER") else "console"),
        bot_number=os.getenv("SIGNAL_BOT_NUMBER") or None,
        user_number=os.getenv("SIGNAL_USER_NUMBER") or None,
    )


PREFS_PATH = DATA / "preferences.json"


def load_preferences() -> Preferences:
    if not PREFS_PATH.exists():
        save_preferences(DEFAULT_PREFERENCES)
    return Preferences.model_validate_json(PREFS_PATH.read_text())


def save_preferences(prefs: Preferences) -> None:
    PREFS_PATH.write_text(json.dumps(prefs.model_dump(), indent=2) + "\n")
