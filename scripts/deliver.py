"""Step 4, DELIVER: triaged.csv -> one email per job in the Gmail labels.

For every decision not yet delivered (empty 'delivered_at'):
  great      -> 1 email in gmail.great      (the candidate tailors the resume)
  normal     -> 1 email in gmail.ready      (use the base resume named in the subject)
  untriaged  -> 1 email in gmail.untriaged  (default: gmail.ready), marked "not triaged":
                the LLM could not decide, so the candidate decides
  rejected   -> listed in the summary: 1 email per run in gmail.summary
Emails are created directly in the label (not in the inbox, no notification) and stay unread.
Then 'delivered_at' is filled in, so the same job is never delivered twice.

Every text below can be overridden in the "messages" block of config.json (for example, to read
the emails in another language).

Usage (in the container):
  ./run.sh deliver               deliver everything pending
  ./run.sh deliver --limit 2     only the first 2 great/normal/untriaged jobs (test; no summary)
  ./run.sh deliver --dry-run     show the emails without creating them
"""
import argparse

import common
import gmail
from triage import COLUMNS, UNTRIAGED

MESSAGES = {
    "subject": "[{resume} · {language}] {title} · {company} · {location}",
    "untriaged_subject": "[not triaged] {title} · {company} · {location}",
    "title": "Title", "company": "Company", "location": "Location", "resume": "Resume", "why": "Why",
    "link": "Link", "also_at": "Also at", "sources": "Sources", "deadline": "Deadline",
    "triaged": "Triaged", "id": "ID",
    "summary_subject": "Rejected: {count} job(s) · {date}",
    "summary_intro": ("Jobs the filter rejected in this run. If one should have passed,\n"
                      "adjust profile.md (the change applies from the next run on)."),
}


def job_email(j, t):
    template = t["untriaged_subject"] if j["category"] == UNTRIAGED else t["subject"]
    subject = template.format(**{**j, "resume": j["resume"] or "-", "language": j["language"] or "-"})
    resume = f"{j['resume']} ({j['language']})" if j["resume"] else "-"
    fields = [("title", j["title"]), ("company", j["company"]), ("location", j["location"]),
              ("resume", resume), ("why", j["reason"]), None,
              ("link", j["url"])]
    if j["other_links"]:
        fields.append(("also_at", j["other_links"].replace(" ", "  ")))
    fields += [None, ("sources", j["sources"]), ("deadline", j["deadline"] or "-"),
               ("triaged", j["triaged_at"][:10]), ("id", j["id"])]
    width = max(len(t[k]) for k, _ in filter(None, fields)) + 2
    lines = ["" if f is None else f"{t[f[0]] + ':':{width}}{f[1]}" for f in fields]
    return subject, "\n".join(lines)


def summary_email(rejected, today, t):
    subject = t["summary_subject"].format(count=len(rejected), date=today)
    lines = [t["summary_intro"], ""]
    for j in rejected:
        lines += [f"- {j['title']} · {j['company']} · {j['location']}",
                  f"  {t['why']}: {j['reason']}",
                  f"  {j['url']}", ""]
    return subject, "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Step 4: create one email per job in the Gmail labels")
    p.add_argument("--limit", type=int, help="deliver at most N great/normal/untriaged jobs (test; no summary)")
    p.add_argument("--dry-run", action="store_true", help="show the emails without creating them")
    a = p.parse_args()

    cfg = common.config()
    labels, texts = cfg["gmail"], {**MESSAGES, **cfg.get("messages", {})}
    rows = common.read_csv(common.TRIAGED)
    pending = [j for j in rows if not j["delivered_at"]]
    by = {c: [j for j in pending if j["category"] == c] for c in ("great", "normal", UNTRIAGED, "rejected")}
    if a.limit:
        chosen = (by["great"] + by["normal"] + by[UNTRIAGED])[:a.limit]
        by = {c: [j for j in chosen if j["category"] == c] for c in by}
    print(f"to deliver: great {len(by['great'])} | normal {len(by['normal'])} | {UNTRIAGED} {len(by[UNTRIAGED])} "
          f"| rejected {len(by['rejected'])} (in the summary)")
    if not any(by.values()):
        return

    now = common.now()
    batches = [(labels["great"], by["great"]), (labels["ready"], by["normal"]),
               (labels.get("untriaged", labels["ready"]), by[UNTRIAGED])]
    batches = [(label, jobs, [job_email(j, texts) for j in jobs]) for label, jobs in batches]
    if by["rejected"]:
        batches.append((labels["summary"], by["rejected"],
                        [summary_email(by["rejected"], common.iso(now)[:10], texts)]))

    if a.dry_run:
        for label, _, messages in batches:
            for subject, body in messages[:2]:
                print(f"\n--- {label}\nSubject: {subject}\n{body}")
            if len(messages) > 2:
                print(f"\n... and {len(messages) - 2} more email(s) in {label}")
        print("\n(dry run: no email created)")
        return

    delivered = set()
    for label, jobs, messages in batches:
        if not messages:
            continue
        gmail.create(label, messages)
        delivered |= {j["id"] for j in jobs}
        print(f"  {len(messages)} email(s) created in {label}")

    stamp = common.iso(now)
    for j in rows:
        if j["id"] in delivered:
            j["delivered_at"] = stamp
    common.write_csv(common.TRIAGED, COLUMNS, rows)
    print(f"delivered: {len(delivered)} job(s)")


if __name__ == "__main__":
    main()