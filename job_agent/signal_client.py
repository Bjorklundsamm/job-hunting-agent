"""Signal I/O through signal-cli.

Modes (SIGNAL_MODE in .env):
- note_to_self: signal-cli is a linked device on YOUR account. The agent posts into your "Note to Self"
  chat (with --notify-self so your phone notifies), and reads what you type there.
- bot: signal-cli is registered with a separate number and messages your number directly.
- console: nothing configured; messages are printed (for testing).

In note_to_self mode both you and the agent write as "you", so inbound messages are accepted only if:
1. they are a sync of a message you sent from another device (your phone) TO yourself, and
2. their timestamp isn't one the agent sent, and
3. they don't start with the agent's marker (every agent message starts with it).
Messages you send to other people also arrive as syncs; rule 1 ignores them.
"""

import json
import logging
import os
import subprocess
from collections import deque

log = logging.getLogger(__name__)

MARKER = "🌘"  # starts every message the agent sends
OLD_MARKERS = ("🤖",)  # markers used by earlier versions


class Signal:
    def __init__(self, cfg, db=None):
        self.cli, self.db = cfg.signal_cli, db
        self.mode = cfg.signal_mode
        self.user = cfg.user_number
        self.account = self.user if self.mode == "note_to_self" else cfg.bot_number
        if self.mode != "console" and not (self.account and self.user):
            log.warning("Signal numbers missing for mode %s; falling back to console mode", self.mode)
            self.mode = "console"
        if self.mode == "console":
            log.warning("console mode: messages are printed, not sent")
        self._sent_ts = deque(maxlen=500)
        if db and db.get("signal_sent_ts"):
            self._sent_ts.extend(json.loads(db.get("signal_sent_ts")))

    def _run(self, *args, stdin=None, timeout=90):
        # signal-cli runs on Java; force UTF-8 on its pipes so emoji survive on Windows, whose default
        # console encodings are not UTF-8.
        env = {**os.environ, "SIGNAL_CLI_OPTS": "-Dstdout.encoding=UTF-8 -Dstderr.encoding=UTF-8 "
               "-Dstdin.encoding=UTF-8 -Dfile.encoding=UTF-8"}
        return subprocess.run([self.cli, "-a", self.account, *args], input=stdin, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)

    def _remember(self, ts):
        self._sent_ts.append(ts)
        if self.db:
            self.db.set("signal_sent_ts", json.dumps(list(self._sent_ts)))

    def send(self, text: str) -> bool:
        text = f"{MARKER} {text}"
        if self.mode == "console":
            print(f"\n----- [signal -> you] -----\n{text}\n---------------------------\n", flush=True)
            return True
        target = ["--note-to-self", "--notify-self"] if self.mode == "note_to_self" else [self.user]
        try:
            # The text goes through stdin, not the command line: on Windows signal-cli is a .bat file,
            # where cmd.exe would mangle emoji and could interpret characters like & or | in job titles.
            r = self._run("-o", "json", "send", "--message-from-stdin", *target, stdin=text)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.error("signal send failed: %s", e)
            return False
        if r.returncode != 0:
            log.error("signal send failed (%s): %s", r.returncode, r.stderr.strip()[:500])
            return False
        for line in r.stdout.splitlines():
            try:
                ts = json.loads(line).get("timestamp")
            except (json.JSONDecodeError, AttributeError):
                continue
            if ts:
                self._remember(ts)
        return True

    def receive(self) -> list[str]:
        """Your messages since the last call (anything else is ignored)."""
        if self.mode == "console":
            return []
        try:
            r = self._run("-o", "json", "receive", "-t", "5", "--ignore-attachments", "--ignore-stories")
        except (OSError, subprocess.TimeoutExpired) as e:
            log.error("signal receive failed: %s", e)
            return []
        if r.returncode != 0:
            log.error("signal receive failed (%s): %s", r.returncode, r.stderr.strip()[:500])
        texts = []
        for line in r.stdout.splitlines():
            try:
                env = json.loads(line).get("envelope", {})
            except json.JSONDecodeError:
                continue
            text = self.accept(env)
            if text:
                texts.append(text)
        return texts

    def accept(self, env: dict) -> str | None:
        """Return the message text if this envelope is a message from you to the agent, else None."""
        if self.mode == "note_to_self":
            sent = (env.get("syncMessage") or {}).get("sentMessage") or {}
            dest = sent.get("destinationNumber") or sent.get("destination")
            text, ts = sent.get("message"), sent.get("timestamp") or env.get("timestamp")
            if not text or dest != self.account:
                return None  # not typed into Note to Self (e.g. a message you sent someone else)
        else:
            sender = env.get("sourceNumber") or env.get("source")
            msg = env.get("dataMessage") or {}
            text, ts = msg.get("message"), msg.get("timestamp") or env.get("timestamp")
            if not text or sender != self.user:
                return None
        if ts in self._sent_ts or text.lstrip().startswith((MARKER, *OLD_MARKERS)):
            return None  # the agent's own message coming back
        return text.strip()
