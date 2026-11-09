"""Shared base: paths, configuration, UTC time, JSONL and CSV files.

The data folder comes from HIRELLME_DATA (inside the container it is always /data).
Without it, data/ in the project is used.

Data layout (each file has a .example next to it in the repository):
  config.json      labels, search terms, triage settings, message texts
  profile.md       the candidate profile the LLM triages against
  raw/jobs.jsonl   bronze: every job collected, append-only
  raw/runs.jsonl   one line per collection run
  raw/triage.jsonl one line per triage run (tokens used, why it stopped)
  clean.csv        silver: one row per unique job (rebuilt from raw on every run)
  triaged.csv      gold: the LLM decision for each job and when it was delivered
"""
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ["HIRELLME_DATA"]) if os.environ.get("HIRELLME_DATA") else ROOT / "data"

CONFIG = DATA / "config.json"
PROFILE = DATA / "profile.md"
RAW = DATA / "raw"
JOBS = RAW / "jobs.jsonl"
RUNS = RAW / "runs.jsonl"
TRIAGE_LOG = RAW / "triage.jsonl"
CLEAN = DATA / "clean.csv"
TRIAGED = DATA / "triaged.csv"


def config():
    if not CONFIG.exists():
        raise SystemExit(f"Missing {CONFIG} (copy data/config.json.example to start).")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- time (always UTC)

def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_iso(text):
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- files

def read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, columns, rows):
    """Rewrites the whole file (header + rows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)