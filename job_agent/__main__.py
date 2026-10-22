"""Usage: uv run python -m job_agent <command>

  run              start the agent (searches, digests, Signal replies) — what launchd runs
  search [source]  run one search + screening cycle now (next source in rotation, or the one named)
  search --no-llm  search only, no LLM screening (free; good for checking scrapers)
  digest           pick and send a digest now
  say "<text>"     handle a message as if you'd sent it over Signal
  profile          rebuild data/profile.md from the resume
  status           print stats
  test-signal      send a test message over Signal
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOGS, load_settings


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cfg = load_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), RotatingFileHandler(LOGS / "agent.log", maxBytes=5_000_000, backupCount=3)],
    )
    for noisy in ("httpx", "JobSpy", "jobspy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from .agent import Agent

    agent = Agent(cfg)
    cmd = args[0]
    if cmd == "run":
        agent.run_forever()
    elif cmd == "search":
        site = next((a for a in args[1:] if not a.startswith("--")), None)
        stats = agent.pipeline.search_cycle(site)
        print(stats)
        if "--no-llm" not in args:
            print("screened:", agent.pipeline.screen_pending())
            agent.maybe_urgent()
    elif cmd == "digest":
        print("sent:", agent.run_digest(forced=True))
    elif cmd == "say":
        agent.handle_message(" ".join(args[1:]))
    elif cmd == "profile":
        agent._profile = None
        from .config import load_preferences
        from .profile import load_profile

        print(load_profile(agent.db, agent.llm, cfg, load_preferences(), force=True))
    elif cmd == "status":
        print(agent.status_text())
    elif cmd == "test-signal":
        ok = agent.signal.send("Eee hee! So you're the one I'm stuck helping. I'm Midna, and I'll be hunting jobs for you. "
                                 "Reply \"help\" if you're lost already.")
        print("sent" if ok else "FAILED — see logs/agent.log")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
