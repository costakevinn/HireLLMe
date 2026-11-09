"""TESTING tool: puts Gmail and the data back to the state before the first run.

  - emails in the processed and manual labels -> back to the inbox label (unread)
  - emails in the great, ready and summary labels (created by HireLLMe) -> Gmail trash
  - deletes raw/, clean.csv and triaged.csv (config.json and profile.md stay)

Usage (in the container):
  ./run.sh reset          shows what it would do (changes nothing)
  ./run.sh reset --yes    does it
"""
import argparse
import shutil

import common
import gmail


def main():
    p = argparse.ArgumentParser(description="TESTING: back to the initial state")
    p.add_argument("--yes", action="store_true", help="really do it (without it, only shows)")
    a = p.parse_args()

    labels = common.config()["gmail"]
    give_back = {k: gmail.uids(labels[k]) for k in ("processed", "manual")}
    to_trash = {k: gmail.uids(labels[k]) for k in ("great", "ready", "summary")}
    files = [f for f in (common.RAW, common.CLEAN, common.TRIAGED) if f.exists()]

    for k, found in give_back.items():
        print(f"back to {labels['inbox']}: {len(found):5} email(s) from {labels[k]}")
    for k, found in to_trash.items():
        print(f"to the trash:      {len(found):5} email(s) from {labels[k]}")
    print("delete files:", ", ".join(f.name for f in files) or "none")
    if not a.yes:
        print("(dry run: nothing changed; use --yes to do it)")
        return

    for k, found in give_back.items():
        gmail.move(found, labels[k], labels["inbox"], seen=False)
    trash = gmail.trash()
    for k, found in to_trash.items():
        gmail.move(found, labels[k], trash, seen=True)
    for f in files:
        shutil.rmtree(f) if f.is_dir() else f.unlink()
    print("done: initial state restored")


if __name__ == "__main__":
    main()