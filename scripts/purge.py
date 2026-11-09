"""Maintenance: moves ALERT emails older than N days to the Gmail trash.

Only touches the alert labels (inbox, processed, manual). The job emails created by HireLLMe
(great, ready, summary) are never touched. Gmail keeps the trash for 30 days.

Usage (in the container):
  ./run.sh purge                     shows what it would do (alerts older than 14 days)
  ./run.sh purge --days 14 --yes     does it
"""
import argparse
from datetime import timedelta

import common
import gmail


def main():
    p = argparse.ArgumentParser(description="Move alert emails older than N days to the Gmail trash")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--yes", action="store_true", help="really do it (without it, only shows)")
    a = p.parse_args()

    labels = common.config()["gmail"]
    cutoff = common.now() - timedelta(days=a.days)
    targets = {labels[k]: gmail.uids(labels[k], before=cutoff) for k in ("inbox", "processed", "manual")}
    print(f"alerts received before {cutoff:%Y-%m-%d} ({a.days} days):")
    for label, found in targets.items():
        print(f"  {len(found):5} in {label}")
    print(f"  total: {sum(len(f) for f in targets.values())} email(s) to the trash")
    if not a.yes:
        print("(dry run: nothing changed; use --yes to do it)")
        return
    trash = gmail.trash()
    for label, found in targets.items():
        gmail.move(found, label, trash, seen=True)
    print("done")


if __name__ == "__main__":
    main()