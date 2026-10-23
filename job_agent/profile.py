"""Build (once) a deep candidate profile from the resume PDF; rebuild only when the resume changes."""

import base64
import hashlib
import logging

from pydantic import BaseModel

from .config import DATA, ROOT
from .prompts import PROFILE_INSTRUCTIONS

log = logging.getLogger(__name__)
PROFILE_PATH = DATA / "profile.md"


class ProfileOut(BaseModel):
    profile_markdown: str


def resume_hash(cfg) -> str:
    return hashlib.sha1((ROOT / cfg.resume_path).read_bytes()).hexdigest()


def load_profile(db, llm, cfg, prefs, force=False) -> str:
    """profile.md is yours to edit; it is only regenerated when the resume file changes (or `force`)."""
    h = resume_hash(cfg)
    if PROFILE_PATH.exists() and db.get("resume_hash") == h and not force:
        return PROFILE_PATH.read_text(encoding="utf-8")
    log.info("building candidate profile from resume")
    pdf = base64.standard_b64encode((ROOT / cfg.resume_path).read_bytes()).decode()
    goals = (
        f"Target roles: {'; '.join(prefs.target_roles)}. {prefs.work_arrangement} "
        f"Pay floor ${prefs.min_salary:,}, ideally ${prefs.target_salary:,}+. {prefs.employment_type}"
    )
    out = llm.parse(
        purpose="profile",
        model=cfg.llm["judge_model"],
        system="You are an experienced technical recruiter and career coach.",
        content=[
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf}},
            {"type": "text", "text": PROFILE_INSTRUCTIONS.format(goals=goals)},
        ],
        schema=ProfileOut,
        effort="high",
    )
    PROFILE_PATH.write_text(out.profile_markdown.strip() + "\n", encoding="utf-8")
    db.set("resume_hash", h)
    return PROFILE_PATH.read_text(encoding="utf-8")
