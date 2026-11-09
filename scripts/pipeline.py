"""Full run: collect -> clean -> triage -> deliver.

Each step runs as a separate program and only works on what is pending, so if one step fails
the next ones still run and nothing is lost (it is picked up by the next run).

Notices (an email to yourself, see notify.py):
  - the triage asks for attention (exit code 3: LLM setup problem, or a limit that keeps
    coming back): the run itself is fine, the jobs wait in the queue
  - the Claude Code token expires in 30, 7 or 1 day(s) ('triage.token_created' in config.json;
    checked on the first run of the day)

At the end it prints a summary and exits with an error (code 1) if any step failed.

Usage (in the container):
  ./run.sh pipeline              the four steps
  ./run.sh pipeline --pending    only triage + deliver (whatever is still pending)
"""
import argparse
import subprocess
import sys
import time
from datetime import date

import common

FULL = ["collect", "clean", "triage", "deliver"]
PENDING = ["triage", "deliver"]
ATTENTION = 3                  # triage exit code: tell the user, but the run did not fail
TOKEN_DAYS = 365               # a token from 'claude setup-token' lasts one year
WARN_AT = (30, 7, 1)


def notify(*args):
    """Sends a notice; a notice that fails must not fail the run."""
    r = subprocess.run([sys.executable, "-u", str(common.ROOT / "scripts" / "notify.py"), *args])
    if r.returncode != 0:
        print("  ! the notice email could not be sent")


def check_token(cfg, today):
    """Warns 30, 7 and 1 day(s) before the Claude Code token expires (first run of the day only)."""
    t = cfg.get("triage", {})
    if t.get("provider", "claude-code") != "claude-code" or not t.get("token_created"):
        return
    if any(r["started"][:10] == today.isoformat() for r in common.read_jsonl(common.RUNS)):
        return                                             # not the first run today
    left = TOKEN_DAYS - (today - date.fromisoformat(t["token_created"])).days
    print(f"Claude token: {left} day(s) left")
    if left in WARN_AT or left <= 0:
        notify("token", "--days", str(max(left, 0)))


def main():
    p = argparse.ArgumentParser(description="Run the HireLLMe steps in order")
    p.add_argument("--pending", action="store_true", help="only triage + deliver")
    a = p.parse_args()

    started = common.now()
    print(f"=== run {'(pending) ' if a.pending else ''}{common.iso(started)}", flush=True)
    check_token(common.config(), started.date())

    results = {}
    for step in PENDING if a.pending else FULL:
        print(f"\n--- {step}", flush=True)
        t0 = time.time()
        r = subprocess.run([sys.executable, "-u", str(common.ROOT / "scripts" / f"{step}.py")])
        results[step] = (r.returncode, time.time() - t0)
        if step == "triage" and r.returncode == ATTENTION:
            last = (common.read_jsonl(common.TRIAGE_LOG) or [{}])[-1]
            notify("triage", "--result", f"{last.get('stopped')}: {last.get('message', '')}",
                   "--pending", str(last.get("pending", "?")))

    print("\n=== summary")
    for step, (code, seconds) in results.items():
        state = {0: "ok    ", ATTENTION: "NOTICE"}.get(code, "FAILED") if step == "triage" else \
            ("ok    " if code == 0 else "FAILED")
        print(f"  {step:8} {state}  ({seconds:.0f} s)")
    if any(code not in (0, ATTENTION) if step == "triage" else code != 0
           for step, (code, _) in results.items()):
        sys.exit(1)


if __name__ == "__main__":
    main()