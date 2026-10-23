"""Change Midna's own code from a Signal request: Claude Code edits, the tests gate it, git commits and pushes.

Flow: require a clean working tree -> headless Claude Code (`claude -p`, file tools only, no shell) makes
the change -> tests/edge_cases.py must pass (one retry where Claude Code sees the failures) -> commit and
push -> the agent exits with RESTART_CODE and the supervisor in __main__ starts it again on the new code.
A change that fails is stashed (`git stash list`), never thrown away.

Only the candidate's own message text is passed to Claude Code, never LLM output or job listing text,
so a crafted listing can't write code.
"""

import json
import logging
import os
import shutil
import subprocess
import sys

from .config import ROOT

log = logging.getLogger(__name__)

RESTART_CODE = 3  # agent exit code meaning "restart me, my code changed"
CLAUDE_TIMEOUT = 20 * 60
# Never committed, even if the change edits .gitignore.
PROTECTED = (".env", "data/", "logs/", ".venv/")

INSTRUCTIONS = """\
You are changing Midna, the job-finding agent in this repository, at the request of her owner, sent from
their phone over Signal. Read CLAUDE.md first. Make the change they ask for, keeping it as small as fits.
- If behaviour changes, keep README.md and CLAUDE.md in sync, and update or add a scenario in
  tests/edge_cases.py when it covers that behaviour. The tests are run after you finish; a change that
  fails them is rejected.
- Don't touch .env, data/, logs/ or the resume PDF. Don't run git.
- If the request is unclear or unsafe to do unattended, change nothing and say why.
Finish with one or two plain sentences saying what you changed. They are relayed to the owner's phone.

Owner's request:
{request}
"""

RETRY = """\
Your change made tests/edge_cases.py fail. Fix it (the change is still in the working tree). Output:
{output}
"""


def _git(*args, root=ROOT, stdin=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def _claude(prompt: str, root) -> tuple[str, float]:
    """Run headless Claude Code; returns (its summary, cost in USD)."""
    cli = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude") or "claude"
    # Without the API key in its environment, Claude Code uses its own login instead of billing that key.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    r = subprocess.run(
        [cli, "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
         "--allowedTools", "Read", "Edit", "Write", "Glob", "Grep", "--disallowedTools", "Bash"],
        input=prompt, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=CLAUDE_TIMEOUT, env=env,
    )
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise RuntimeError(f"Claude Code failed ({r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
    if out.get("is_error"):
        raise RuntimeError(f"Claude Code failed: {str(out.get('result'))[:300]}")
    return str(out.get("result") or "").strip(), float(out.get("total_cost_usd") or 0)


def _tests(root) -> tuple[bool, str]:
    r = subprocess.run([sys.executable, "tests/edge_cases.py"], cwd=root, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = r.stdout + r.stderr
    passed = r.returncode == 0 and "PASS " in out and "FAIL " not in out
    lines = out.strip().splitlines()
    report = [line for line in lines if line.startswith("FAIL ")]
    if r.returncode != 0 and lines:
        report.append(lines[-1])  # the exception, if the script crashed
    return passed, "\n".join(report)[-1500:]


def _stash(request: str, root):
    _git("stash", "push", "--include-untracked", "-m", f"midna: rejected change: {request[:60]}", root=root)


def apply_change(request: str, root=ROOT) -> tuple[bool, str]:
    """Returns (committed, message for the candidate)."""
    if _git("status", "--porcelain", root=root).stdout.strip():
        return False, "The repo has uncommitted changes, so I won't touch the code until those are committed or stashed."
    cost, summary = 0.0, ""
    try:
        summary, c = _claude(INSTRUCTIONS.format(request=request), root)
        cost += c
        passed, failures = _tests(root)
        if not passed and _git("status", "--porcelain", root=root).stdout.strip():
            summary, c = _claude(INSTRUCTIONS.format(request=request) + "\n" + RETRY.format(output=failures), root)
            cost += c
            passed, failures = _tests(root)
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
        log.warning("self-update failed: %s", e)
        _stash(request, root)
        return False, f"Couldn't make that change: {e}"
    log.info("self-update: claude cost $%.3f, tests %s, summary %r", cost, "passed" if passed else "failed", summary)

    if not _git("status", "--porcelain", root=root).stdout.strip():
        return False, f"No code changed. {summary}"
    if not passed:
        _stash(request, root)
        return False, f"The tests failed, so I didn't keep it (it's in git stash).\n{failures[:500]}"

    _git("add", "-A", root=root)
    staged = _git("diff", "--cached", "--name-only", root=root).stdout.split()
    bad = [f for f in staged if f.startswith(PROTECTED) or f.lower().endswith(".pdf")]
    if bad:
        _git("reset", "-q", root=root)
        _stash(request, root)
        return False, f"That change touched files I never commit ({', '.join(bad)}), so I didn't keep it (it's in git stash)."
    title = (summary.splitlines() or ["Change requested over Signal"])[0][:72]
    msg = f"Midna: {title}\n\n{summary}\n\nRequested over Signal: {request}\n"
    commit = _git("commit", "-q", "-F", "-", root=root, stdin=msg)
    if commit.returncode != 0:
        _git("reset", "-q", root=root)
        _stash(request, root)
        return False, f"git commit failed: {commit.stderr.strip()[:200]}"
    sha = _git("rev-parse", "--short", "HEAD", root=root).stdout.strip()
    push = _git("push", root=root)
    pushed = "pushed" if push.returncode == 0 else f"committed but the push failed ({push.stderr.strip()[:150]})"
    return True, f"{summary}\n\n{sha} {pushed}. Claude Code cost ${cost:.2f}."
