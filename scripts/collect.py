"""Step 1, COLLECT: Gmail alerts + Gupy search -> raw layer (bronze).

Writes (append-only, never deletes):
  raw/jobs.jsonl   one job per line, with its origin and the collection time
  raw/runs.jsonl   one line per run (history, and the reference point for the Gupy search)

Gmail (the inbox label is the queue of unprocessed alerts):
  ok, or already read before (same Message-ID)   -> processed label, marked read
  source turned off ("sources" in config.json)    -> processed label, marked read
  TRANSIENT error (network, Indeed did not answer) -> stays in the inbox label; retried next run
      ... older than 'days_until_manual'           -> manual label, unread
  PERMANENT error (no job found, unknown format)   -> manual label, unread
  Order: write the jobs -> record the run -> move the emails.

  Two ways to fill the queue:
    - Gmail filters put the alerts in a label ("gmail": {"inbox": "JOBS"}), or
    - no filters at all: "gmail": {"inbox": "INBOX", "senders": [...]} reads only the messages
      from those senders in the inbox, and moving them out works like archiving.

First run (raw/runs.jsonl is still empty): only the last 'first_run_days' days are read, so an
inbox full of old alerts does not turn into hundreds of stale jobs. Older alerts are not read,
only moved to the processed label. After that, every run reads everything in the queue.

Gupy: jobs published since the start of the last run in which Gupy worked (minus the overlap);
on the first run, the last 'first_run_days' days. If Gupy fails, the rest of the run goes on and
the next run searches again.

Usage (in the container):
  ./run.sh collect                        Gmail + Gupy, writes and moves
  ./run.sh collect --dry-run              shows what it would do, writes and moves nothing
  ./run.sh collect --days 30              read only the last 30 days (any run)
  ./run.sh collect --eml a.eml --no-gupy --dry-run   test with downloaded emails
"""
import argparse
import email
import os
from datetime import timedelta
from email import policy
from pathlib import Path

import common
import gmail
import sources


def last_gupy_run():
    """Start of the last run in which Gupy worked (None if never)."""
    ok = [r for r in common.read_jsonl(common.RUNS) if r.get("gupy_ok", True)]
    return common.from_iso(ok[-1]["started"]) if ok else None


def collected_message_ids():
    return {j["origin"].get("message_id") for j in common.read_jsonl(common.JOBS)
            if j.get("origin", {}).get("type") == "email" and j["origin"].get("message_id")}


def emails_from_files(files):
    messages = []
    for f in files:
        raw = Path(f).read_bytes()
        date = email.message_from_bytes(raw, policy=policy.default)["Date"]
        try:
            received = email.utils.parsedate_to_datetime(date)
        except Exception:
            received = common.now()
        messages.append({"uid": None, "raw": raw, "received": received})
    return messages


def main():
    p = argparse.ArgumentParser(description="Step 1: collect jobs into the raw layer")
    p.add_argument("--eml", nargs="+", help="read these downloaded emails instead of Gmail (test; moves nothing)")
    p.add_argument("--no-gmail", action="store_true")
    p.add_argument("--no-gupy", action="store_true")
    p.add_argument("--days", type=int, help="read only the last N days (default: first run only, see config)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen; write and move nothing")
    a = p.parse_args()

    cfg = common.config()
    sources.configure(cfg)
    labels, settings = cfg["gmail"], cfg["collect"]
    first_run = not common.read_jsonl(common.RUNS)
    days = a.days or (settings.get("first_run_days", 14) if first_run else None)
    cutoff = None
    user = os.environ.get("GMAIL_USER", "")
    started = common.now()
    if days:
        cutoff = started - timedelta(days=days)
        print(f"{'first run: ' if first_run and not a.days else ''}reading only the last {days} day(s)")
    jobs = []

    # --- Gmail
    inbox, senders = labels.get("inbox", "INBOX"), labels.get("senders")
    email_sources = any(sources.enabled(s) for s in ("linkedin", "indeed", "manual"))
    if a.eml:
        messages = emails_from_files(a.eml)
    elif not a.no_gmail and email_sources:
        if inbox.upper() == "INBOX" and not senders:
            raise SystemExit('Reading the whole inbox is not allowed: set "senders" or use a label as "inbox".')
        messages = gmail.read(inbox, cutoff, senders)
    else:
        messages = []
    already = collected_message_ids()
    target = {"processed": [], "manual": []}
    count = {"ok": 0, "already_read": 0, "old": 0, "skipped": 0, "transient": 0, "manual": 0}
    for m in messages:
        if m["raw"] is None:                           # too old: not read, only moved
            target["processed"].append(m["uid"])
            count["old"] += 1
            continue
        msg = email.message_from_bytes(m["raw"], policy=policy.default)
        subject = str(msg["Subject"] or "")[:70]
        message_id = str(msg["Message-ID"] or "")
        if message_id and message_id in already:
            target["processed"].append(m["uid"])
            count["already_read"] += 1
            continue
        status, found, reason = sources.read_email(msg, m["received"], user)
        if status == "ok":
            jobs += found
            target["processed"].append(m["uid"])
            count["ok"] += 1
        elif status == "skipped":
            target["processed"].append(m["uid"])
            count["skipped"] += 1
        elif status == "transient" and started - m["received"] < timedelta(days=settings["days_until_manual"]):
            count["transient"] += 1
            print(f"  ! retry next run: {subject} ({reason})")
        else:
            jobs += found                              # old transient error: keep what could be read
            target["manual"].append(m["uid"])
            count["manual"] += 1
            print(f"  ! {labels['manual']}: {subject} ({reason})")
        if message_id:
            already.add(message_id)

    # --- Gupy
    gupy_ok = True
    previous = last_gupy_run()
    since = (previous - timedelta(hours=settings["overlap_hours"])) if previous else \
        (cutoff or started - timedelta(days=settings.get("first_run_days", 14)))
    if not a.no_gupy and sources.enabled("gupy"):
        try:
            jobs += sources.search_gupy(cfg["gupy"]["search_terms"], since)
        except sources.GupyError as e:
            gupy_ok = False
            print(f"  ! Gupy failed ({e}); the next run searches again since {common.iso(since)}")

    for j in jobs:
        j["collected_at"] = common.iso(started)
    by_source = {}
    for j in jobs:
        by_source[j["source"]] = by_source.get(j["source"], 0) + 1

    print(f"emails: {len(messages)} (ok: {count['ok']}, already read: {count['already_read']}, old: {count['old']}, "
          f"turned off: {count['skipped']}, retry: {count['transient']}, manual: {count['manual']}) | "
          f"Gupy since {common.iso(since)}"
          f"{'' if gupy_ok else ' (FAILED)'} | jobs: {len(jobs)} {by_source}")
    for j in jobs:
        print(f"  + {j['url']}  # {j['title']} | {j['company']} | {j['location']} | {j['source']}")

    if a.dry_run:
        print(f"(dry run: nothing written; would move {len(target['processed'])} to {labels['processed']}, "
              f"{len(target['manual'])} to {labels['manual']})")
        return

    # 1st write, 2nd record the run, 3rd move the emails
    common.append_jsonl(common.JOBS, jobs)
    if a.eml:
        print(f"written to {common.JOBS}")
        return
    common.append_jsonl(common.RUNS, [{
        "started": common.iso(started), "finished": common.iso(common.now()), "gupy_since": common.iso(since),
        "gupy_ok": gupy_ok, "emails": count, "jobs": len(jobs), "sources": by_source}])
    gmail.move(target["processed"], inbox, labels["processed"], seen=True)
    gmail.move(target["manual"], inbox, labels["manual"], seen=False)
    print(f"written to {common.JOBS}; moved {len(target['processed'])} to {labels['processed']}, "
          f"{len(target['manual'])} to {labels['manual']}")


if __name__ == "__main__":
    main()