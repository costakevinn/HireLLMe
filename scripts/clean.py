"""Step 2, CLEAN: raw layer -> clean.csv (silver), rebuilt from scratch on every run.

  1. merges every occurrence of the same link (first time seen, every source);
  2. drops jobs whose title matches 'clean.exclude_title' in the config;
  3. merges the same job across sources (normalized company + title);
  4. writes one row per job, oldest first.

The description stays in the raw layer (it is long); the triage step reads it from there by link.
The job id is a hash of the link, so the same job always has the same id.

Usage (in the container):  ./run.sh clean
"""
import hashlib
import re
import unicodedata

import common

COLUMNS = ["id", "first_seen", "title", "company", "location", "sources", "deadline", "url", "other_links"]


def normalize(text):
    """Lowercase, no accents, no punctuation, and no leading "pessoa" (Brazilian postings often
    write "Pessoa Analista de Dados", i.e. "Person Data Analyst", for the same role)."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return re.sub(r"^pessoa ", "", t)


def job_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:8]


def main():
    raw = common.read_jsonl(common.JOBS)
    if not raw:
        raise SystemExit(f"No jobs in {common.JOBS} yet (run ./run.sh collect).")
    exclude = re.compile(common.config()["clean"]["exclude_title"], re.I)

    # 1. by link
    by_url = {}
    for r in raw:
        j = by_url.get(r["url"])
        if j is None:
            by_url[r["url"]] = {"url": r["url"], "first_seen": r["collected_at"], "title": r["title"],
                                "company": r["company"], "location": r["location"], "sources": {r["source"]},
                                "deadline": r.get("deadline", ""), "other_links": []}
        else:
            j["sources"].add(r["source"])
            j["first_seen"] = min(j["first_seen"], r["collected_at"])
            j["deadline"] = j["deadline"] or r.get("deadline", "")

    # 2. excluded titles
    jobs = [j for j in by_url.values() if not exclude.search(j["title"])]
    excluded = len(by_url) - len(jobs)

    # 3. the same job across sources
    jobs.sort(key=lambda j: j["first_seen"])
    by_key, merged = {}, 0
    for j in jobs:
        key = (normalize(j["company"]), normalize(j["title"]))
        owner = by_key.get(key)
        if owner is None:
            by_key[key] = j
        else:
            owner["sources"] |= j["sources"]
            owner["deadline"] = owner["deadline"] or j["deadline"]
            owner["other_links"].append(j["url"])
            merged += 1

    # 4. write
    common.write_csv(common.CLEAN, COLUMNS, [
        {**j, "id": job_id(j["url"]), "sources": ",".join(sorted(j["sources"])), "other_links": " ".join(j["other_links"])}
        for j in by_key.values()])

    print(f"raw: {len(raw)} | unique links: {len(by_url)} | excluded by title: {excluded} | "
          f"merged across sources: {merged} | clean: {len(by_key)}")
    print(f"written to {common.CLEAN}")


if __name__ == "__main__":
    main()