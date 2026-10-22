"""Thin wrapper over the Anthropic SDK that records spend and enforces the daily budget."""

import logging
from datetime import datetime

import anthropic

from .db import DB, now_iso

log = logging.getLogger(__name__)

# USD per million tokens: (input, output). Cache reads bill at 0.1x input, cache writes at 1.25x.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


class BudgetExceeded(Exception):
    pass


class LLMFailed(Exception):
    pass


class LLM:
    def __init__(self, db: DB, cfg):
        self.db = db
        self.cfg = cfg
        self.client = anthropic.Anthropic(max_retries=4, timeout=300)

    def today(self) -> str:
        return datetime.now(self.cfg.tz).date().isoformat()

    def spent_today(self) -> float:
        row = self.db.one("SELECT COALESCE(SUM(cost_usd),0) AS c FROM llm_usage WHERE day=?", self.today())
        return row["c"]

    def budget_left(self) -> float:
        return self.cfg.llm["daily_budget_usd"] - self.spent_today()

    def parse(self, *, purpose, model, system, content, schema, effort, max_tokens=16000):
        """One structured-output call. `system` is cached, so keep it identical across calls in a cycle."""
        if self.budget_left() <= 0:
            raise BudgetExceeded(f"daily LLM budget ${self.cfg.llm['daily_budget_usd']:.2f} reached")
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_format=schema,
            output_config={"effort": effort},
        )
        try:
            if model in FALLBACK_MODELS:
                resp = self.client.beta.messages.parse(
                    **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
            else:
                resp = self.client.messages.parse(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            raise
        except anthropic.BadRequestError as e:
            raise LLMFailed(f"{purpose}: bad request: {e.message}") from e
        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMFailed(f"{purpose}: {type(e).__name__}: {e}") from e
        self._record(purpose, model, resp)
        if resp.stop_reason == "refusal":
            raise LLMFailed(f"{purpose}: refused ({getattr(resp.stop_details, 'category', None)})")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise LLMFailed(f"{purpose}: no parsable output (stop_reason={resp.stop_reason})")
        return resp.parsed_output

    def _record(self, purpose, model, resp):
        u = resp.usage
        cr, cw = u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0
        pin, pout = PRICES.get(model, PRICES["claude-opus-5-5"])
        cost = (u.input_tokens * pin + cr * pin * 0.1 + cw * pin * 1.25 + u.output_tokens * pout) / 1e6
        self.db.x(
            "INSERT INTO llm_usage(ts,day,model,purpose,input_tokens,output_tokens,cache_read,cache_write,cost_usd)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            now_iso(), self.today(), model, purpose, u.input_tokens, u.output_tokens, cr, cw, cost,
        )
        log.info("llm %s %s in=%d out=%d cache_r=%d cache_w=%d $%.4f req=%s",
                 purpose, model, u.input_tokens, u.output_tokens, cr, cw, cost, resp._request_id)
