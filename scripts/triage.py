"""Step 3, TRIAGE: clean.csv + profile.md -> LLM -> triaged.csv (gold).

Only the clean jobs not yet in triaged.csv are sent (the same job is never paid for twice),
newest first, in batches ('triage.batch_size'), with title, company, location, sources and
part of the description (read from the raw layer). Any provider works (see scripts/llm.py).

Nothing is ever lost, and nothing expires:
  - before any batch, a tiny health check; if the provider is not available, nothing is sent
  - 'max_jobs_per_run' (0 = no limit) caps the jobs sent per run; the rest wait for the next run
  - limit / provider down: triage stops, the remaining jobs wait for the next run
  - an answer out of format: the batch is split in two and asked again; a single job that still
    gets no valid answer is saved as "untriaged" (delivered without a decision)
  - 'deliver_untriaged_after_days' (0 = off): jobs waiting longer than that are saved as
    "untriaged" without calling the LLM, so they still reach the inbox

Jobs you send yourself (source "manual") go first and are never rejected:
  - a label tag in the subject (#great / #ready, see "manual" in config.json) sets the category;
    the LLM still picks the resume and writes the reason, and if it is not available the job is
    delivered anyway
  - without a label tag the LLM chooses between great and normal
  - a resume tag (#ml) sets the resume
  - sending again a job that was already triaged (for example, rejected) delivers it again

Every run appends one line to raw/triage.jsonl: jobs sent and decided, tokens used, and why it
stopped. Exit code 3 means "tell the user": a setup problem (key, model, URL) or a limit that has
lasted 'alert_after_runs' runs in a row. The pipeline turns it into a notice email.

Usage (in the container):
  ./run.sh triage                 triage the pending jobs
  ./run.sh triage --limit 5       only the 5 newest (cheap test)
  ./run.sh triage --dry-run       build the requests and show their size, without calling the LLM
  ./run.sh triage --check         only the health check: does the LLM answer? (a few tokens)
"""
import argparse
import json
import sys
from datetime import timedelta

import common
import llm

PROMPT = common.ROOT / "prompts" / "triage.md"
COLUMNS = ["id", "triaged_at", "category", "resume", "language", "reason", "title", "company", "location",
           "sources", "deadline", "url", "other_links", "delivered_at"]
CATEGORIES = {"great", "normal", "rejected"}
UNTRIAGED = "untriaged"
ALERT = 3                      # exit code: the user must be told


def descriptions():
    """url -> the longest description seen in the raw layer."""
    d = {}
    for r in common.read_jsonl(common.JOBS):
        text = r.get("description") or ""
        if len(text) > len(d.get(r["url"], "")):
            d[r["url"]] = text
    return d


def manual_hints():
    """url -> the latest job you sent yourself: {'tag', 'resume_tag', 'collected_at'}."""
    hints = {}
    for r in common.read_jsonl(common.JOBS):
        if r.get("source") == "manual" and r.get("collected_at", "") >= hints.get(r["url"], {}).get("collected_at", ""):
            hints[r["url"]] = {"tag": r.get("tag", ""), "resume_tag": r.get("resume_tag", ""),
                               "collected_at": r.get("collected_at", "")}
    return hints


def hint_for(j, hints):
    for u in [j["url"]] + j.get("other_links", "").split():
        if u in hints:
            return hints[u]
    return None


def apply_hint(d, h, prefix="sent by you"):
    """Your choice wins: the label tag sets the category, a job you sent is never rejected,
    and the resume tag sets the resume. The reason starts with 'prefix' ("manual.reason_prefix")."""
    if not h:
        return d
    d = dict(d)
    d["reason"] = f"{prefix} · {d['reason']}" if d.get("reason") else prefix
    if h["tag"]:
        d["category"] = h["tag"]
    elif d["category"] in ("rejected", UNTRIAGED):
        d["category"] = "normal"
    if h["resume_tag"]:
        d["resume"] = h["resume_tag"]
    return d


def rescue(rows, hints, stamp, prefix):
    """A job already triaged that you sent again afterwards: deliver it again with your choice."""
    n = 0
    for r in rows:
        h = hint_for(r, hints)
        if not h or h["collected_at"] <= r["triaged_at"]:
            continue
        d = apply_hint({"category": r["category"], "resume": r["resume"], "reason": r["reason"]}, h, prefix)
        r["triaged_at"] = stamp                            # marks this request as handled
        if (d["category"], d["resume"]) != (r["category"], r["resume"]):
            r["category"], r["resume"], r["delivered_at"] = d["category"], d["resume"], ""
            r["reason"] = d["reason"][:200]
            n += 1
    return n


def build_jobs(batch, desc, max_description, hints=None):
    items = []
    for j in batch:
        links = [j["url"]] + j["other_links"].split()
        text = max((desc.get(u, "") for u in links), key=len)
        item = {"id": j["id"], "title": j["title"], "company": j["company"], "location": j["location"],
                "sources": j["sources"]}
        if text:
            item["description"] = text[:max_description]
        if hints and hint_for(j, hints):
            item["sent_by_candidate"] = True
        items.append(item)
    return items


def parse(text, ids, resumes, languages):
    """Extracts the JSON array from the answer and validates each decision. Returns {id: decision}."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise llm.LLMError("output", f"no JSON array in the answer: {text[:300]}")
    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise llm.LLMError("output", f"invalid JSON in the answer: {e}")
    valid = {"category": CATEGORIES, "resume": set(resumes), "language": set(languages)}
    decisions = {}
    for d in items:
        if not isinstance(d, dict) or d.get("id") not in ids:
            continue
        d["language"] = str(d.get("language", "")).upper()
        if all(str(d.get(k)) in ok for k, ok in valid.items()):
            decisions[d["id"]] = d
    return decisions


def row(j, stamp, category, resume="", language="", reason=""):
    return {**{k: j.get(k, "") for k in COLUMNS}, "triaged_at": stamp, "category": category,
            "resume": resume, "language": language, "reason": str(reason)[:200], "delivered_at": ""}


class Triage:
    """Sends batches, splits them when the answer is out of format, adds up tokens."""

    def __init__(self, cfg, system, header, profile, desc, hints):
        self.cfg, self.system, self.header, self.profile, self.desc = cfg, system, header, profile, desc
        self.hints = hints
        t = cfg["triage"]
        self.resumes, self.languages = t["resumes"], [x.upper() for x in t["languages"]]
        self.max_description = t["max_description"]
        self.usage = {"input": 0, "output": 0}

    def request(self, batch):
        return (f"Triage the jobs below following your instructions.\n\n{self.header}\n\n"
                f"# PROFILE\n\n{self.profile}\n\n# JOBS\n\n"
                + json.dumps(build_jobs(batch, self.desc, self.max_description, self.hints),
                             ensure_ascii=False, indent=1))

    def decide(self, batch):
        """Returns {id: decision}; a job that keeps failing alone gets the 'untriaged' decision.
        Raises LLMError for setup / limit / transient (the caller stops)."""
        try:
            text, usage = llm.complete(self.system, self.request(batch), self.cfg)
            self.usage["input"] += usage["input"]
            self.usage["output"] += usage["output"]
            return parse(text, {j["id"] for j in batch}, self.resumes, self.languages)
        except llm.LLMError as e:
            if e.kind != "output":
                raise
            if len(batch) == 1:
                print(f"  ! no valid answer for {batch[0]['id']}: saved as {UNTRIAGED}")
                return {batch[0]["id"]: {"category": UNTRIAGED, "reason": "the model gave no valid answer"}}
            half = len(batch) // 2
            print(f"  ! answer out of format ({e.message[:80]}); asking again in two halves")
            return {**self.decide(batch[:half]), **self.decide(batch[half:])}


def previous_stops():
    """How many runs in a row (most recent first) stopped for a limit or an unavailable provider."""
    n = 0
    for r in reversed(common.read_jsonl(common.TRIAGE_LOG)):
        if r.get("stopped") not in ("limit", "transient"):
            break
        n += 1
    return n


def main():
    p = argparse.ArgumentParser(description="Step 3: triage the clean jobs with the LLM")
    p.add_argument("--limit", type=int, help="triage at most the N newest jobs (test)")
    p.add_argument("--dry-run", action="store_true", help="build the requests without calling the LLM")
    p.add_argument("--check", action="store_true", help="only check that the LLM answers")
    a = p.parse_args()

    cfg = common.config()
    t = cfg["triage"]
    if a.check:
        try:
            s = llm.settings(cfg)
            usage = llm.preflight(cfg)
        except llm.LLMError as e:
            print(f"LLM not available ({e.kind}): {e.message}")
            sys.exit(ALERT)
        print(f"LLM ok: {s['provider']} / {s['model']} (tokens in {usage['input']} out {usage['output']})")
        return
    if not common.PROFILE.exists():
        raise SystemExit(f"Missing {common.PROFILE}: the triage is based on it.")
    profile = common.PROFILE.read_text(encoding="utf-8")
    started = common.now()
    stamp = common.iso(started)
    rows = common.read_csv(common.TRIAGED)
    hints = manual_hints()
    prefix = cfg.get("manual", {}).get("reason_prefix", "sent by you")
    done = {r["id"] for r in rows}
    pending = [j for j in common.read_csv(common.CLEAN) if j["id"] not in done]
    pending.sort(key=lambda j: j.get("first_seen", ""), reverse=True)          # newest first
    pending.sort(key=lambda j: hint_for(j, hints) is None)                     # yours before all

    # waiting too long: delivered without a decision (no tokens spent)
    after = t.get("deliver_untriaged_after_days", 0)
    late = []
    if after:
        cutoff = common.iso(started - timedelta(days=after))
        late = [j for j in pending if j.get("first_seen", stamp) < cutoff and not hint_for(j, hints)]
        pending = [j for j in pending if j not in late]

    budget = a.limit or t.get("max_jobs_per_run", 0)
    queue = pending[:budget] if budget else pending
    print(f"jobs awaiting triage: {len(pending)} | this run: {len(queue)}"
          f"{f' (budget {budget})' if budget and len(pending) > budget else ''}"
          f"{f' | waiting over {after} days: {len(late)} -> {UNTRIAGED}' if late else ''}")

    s = llm.settings(cfg)
    n = t["batch_size"]
    header = (f"Resume names: {', '.join(t['resumes'])}. Language codes: {', '.join(x.upper() for x in t['languages'])}. "
              f"Write each reason in {t.get('reason_language', 'English')}. "
              "Jobs with \"sent_by_candidate\": true were chosen by the candidate: never reject them "
              "(use great or normal).")
    work = Triage(cfg, PROMPT.read_text(encoding="utf-8"), header, profile, descriptions(), hints)

    if a.dry_run:
        total = 0
        for i in range(0, len(queue), n):
            size = len(work.request(queue[i:i + n]))
            total += size
            print(f"  request {i // n + 1}: {len(queue[i:i + n])} jobs, {size} characters")
        print(f"(dry run: {s['provider']} / {s['model']}, about {total // 4} input tokens)")
        return

    rescued = rescue(rows, hints, stamp, prefix)
    if rescued:
        print(f"sent again by you: {rescued} job(s) delivered again with your choice")
    if late:
        rows += [row(j, stamp, UNTRIAGED, reason=f"waited over {after} days for triage") for j in late]
    if rescued or late:
        common.write_csv(common.TRIAGED, COLUMNS, rows)

    log = {"started": stamp, "provider": s["provider"], "model": s["model"], "pending": len(pending),
           "sent": 0, "decided": 0, "untriaged": len(late), "usage": work.usage, "stopped": None, "message": ""}
    totals = dict.fromkeys(sorted(CATEGORIES | {UNTRIAGED}), 0)
    totals[UNTRIAGED] = len(late)
    try:
        if queue:
            usage = llm.preflight(cfg)
            work.usage["input"] += usage["input"]
            work.usage["output"] += usage["output"]
        for i in range(0, len(queue), n):
            batch = queue[i:i + n]
            decisions = work.decide(batch)
            log["sent"] += len(batch)
            new = []
            for j in batch:
                d = decisions.get(j["id"])
                if d is None:
                    continue                               # no valid decision: stays pending
                d = apply_hint(d, hint_for(j, hints), prefix)
                totals[d["category"]] += 1
                new.append(row(j, stamp, d["category"], d.get("resume", ""), d.get("language", ""),
                               d.get("reason", "")))
            rows += new
            log["decided"] += len(new)
            common.write_csv(common.TRIAGED, COLUMNS, rows)    # saved after every request
            print(f"  request {i // n + 1}: {len(batch)} jobs -> {len(new)} decided "
                  f"({len(batch) - len(new)} without a valid answer stay pending)")
    except llm.LLMError as e:
        log["stopped"], log["message"] = e.kind, e.message
        print(f"  ! triage stopped ({e.kind}): {e.message}")
        # jobs you sent with a label tag do not wait for the LLM
        decided = {r["id"] for r in rows}
        mine = [(j, hint_for(j, hints)) for j in pending if j["id"] not in decided]
        mine = [row(j, stamp, h["tag"], h["resume_tag"], reason=prefix) for j, h in mine if h and h["tag"]]
        if mine:
            rows += mine
            log["decided"] += len(mine)
            totals["great"] += sum(r["category"] == "great" for r in mine)
            totals["normal"] += sum(r["category"] == "normal" for r in mine)
            common.write_csv(common.TRIAGED, COLUMNS, rows)
            print(f"    {len(mine)} job(s) you sent with a tag delivered without the LLM")
        print(f"    {len(pending) - log['decided']} job(s) wait for the next run")

    log["finished"] = common.iso(common.now())
    common.append_jsonl(common.TRIAGE_LOG, [log])
    print(f"triage: great {totals['great']} | normal {totals['normal']} | rejected {totals['rejected']} | "
          f"{UNTRIAGED} {totals[UNTRIAGED]} | tokens in {work.usage['input']} out {work.usage['output']}")

    if log["stopped"] == "setup":
        sys.exit(ALERT)
    if log["stopped"] in ("limit", "transient"):
        in_a_row = previous_stops()                         # this run included
        if in_a_row >= t.get("alert_after_runs", 3):
            print(f"  ! {in_a_row} runs in a row without finishing the triage")
            sys.exit(ALERT)


if __name__ == "__main__":
    main()