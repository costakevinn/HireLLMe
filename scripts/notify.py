"""Sends a short status email to yourself, created directly in a Gmail label (default: the inbox,
so it is seen). Used by the pipeline (triage problems, token about to expire) and by the
watchdog workflow (missed or failed runs).

Every text can be overridden in the "messages" block of config.json, like the job emails.

Usage:
  python scripts/notify.py missed --when 04:07 --result success --url <run> --actions <page>
  python scripts/notify.py failed --when 04:07 --result failure --url <run> --actions <page>
  python scripts/notify.py triage --result "limit: usage limit reached" --pending 42
  python scripts/notify.py token --days 30
  python scripts/notify.py test --actions <page>
"""
import argparse

import common
import gmail

MESSAGES = {
    "missed_subject": "HireLLMe: the {when} run did not happen",
    "missed_body": ("GitHub did not start the {when} run. The watchdog started it at {now} "
                    "and it finished with: {result}.\n\nRun: {url}"),
    "failed_subject": "HireLLMe: the {when} run failed",
    "failed_body": ("The {when} run finished with an error. The watchdog ran it again "
                    "and it finished with: {result}.\n\nRun: {url}\nRun it by hand: {actions}"),
    "triage_subject": "HireLLMe: the triage needs your attention",
    "triage_body": ("The triage could not finish.\n\nProblem: {result}\nJobs waiting: {pending}\n\n"
                    "Nothing was lost: the jobs stay in the queue and are triaged as soon as the "
                    "problem is solved.\n\nWhat to check:\n"
                    "- setup: the LLM key or token (the Claude Code token lasts 1 year; create a new "
                    "one with: claude setup-token), and the provider and model in config.json\n"
                    "- limit: the plan or free quota ran out several runs in a row; wait, lower "
                    "max_jobs_per_run, or use another provider"),
    "token_subject": "HireLLMe: the Claude token expires in {days} day(s)",
    "token_body": ("The Claude Code token (CLAUDE_CODE_OAUTH_TOKEN) expires in {days} day(s).\n\n"
                   "Create a new one with: claude setup-token\n"
                   "Then update the secret on GitHub and 'token_created' in config.json."),
    "test_subject": "HireLLMe: watchdog test",
    "test_body": "This is a test of the watchdog notice. Nothing needs to be done.\n\nRuns: {actions}",
}


def main():
    p = argparse.ArgumentParser(description="Send a status email to yourself")
    p.add_argument("kind", choices=["missed", "failed", "triage", "token", "test"])
    p.add_argument("--when", default="")
    p.add_argument("--result", default="")
    p.add_argument("--url", default="")
    p.add_argument("--actions", default="")
    p.add_argument("--pending", default="")
    p.add_argument("--days", default="")
    a = p.parse_args()

    cfg = common.config()
    texts = {**MESSAGES, **cfg.get("messages", {})}
    values = {"when": a.when, "result": a.result, "url": a.url, "actions": a.actions,
              "pending": a.pending, "days": a.days, "now": common.iso(common.now())[11:16] + " UTC"}
    subject = texts[f"{a.kind}_subject"].format(**values)
    body = texts[f"{a.kind}_body"].format(**values)
    gmail.create(cfg["gmail"].get("alerts", "INBOX"), [(subject, body)])
    print(f"notice sent: {subject}")


if __name__ == "__main__":
    main()