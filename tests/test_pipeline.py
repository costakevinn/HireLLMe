"""HireLLMe tests: no internet, no Gmail and no LLM (made-up data only).

Run:  docker compose run --rm app python -m unittest discover -s tests -v
"""
import base64
import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import clean     # noqa: E402
import common    # noqa: E402
import deliver   # noqa: E402
import sources   # noqa: E402
import triage    # noqa: E402

NOW = datetime(2026, 11, 9, 7, 7, tzinfo=timezone.utc)

LINKEDIN = """Your job alert for data scientist

Junior Data Scientist
Company A
São Paulo, SP

View job: https://www.linkedin.com/comm/jobs/view/111?otpToken=SECRET&trk=x

---------------------------------------------------------

Data Engineer
Company B
Remote

1 school alum
View job: https://www.linkedin.com/comm/jobs/view/222?otpToken=SECRET
"""

INDEED_LIST = """2 new Data Analyst jobs

Data Analyst
Company C - Vitória, ES
SQL and Python for reports.
1 day ago
https://engage.indeed.com/f/a/AAA~~/BBB

See all jobs

https://engage.indeed.com/f/a/CCC~~/DDD
"""


def cts_link(jk):
    """A cts.indeed.com/v3/ link with the destination compressed inside, as Indeed builds them."""
    payload = json.dumps({"u": f"https://br.indeed.com/rc/clk?jk={jk}&from=email"}).encode()
    return "https://cts.indeed.com/v3/" + base64.urlsafe_b64encode(gzip.compress(payload)).decode().rstrip("=")


def message(sender, body, subject="alert"):
    m = EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = sender, subject, "<test@example>"
    m.set_content(body)
    return m


class TestSources(unittest.TestCase):
    def setUp(self):
        self._jk = sources.indeed_jk
        sources.indeed_jk = lambda link: "0123456789abcdef"     # no network

    def tearDown(self):
        sources.indeed_jk = self._jk

    def test_linkedin_reads_jobs_and_drops_the_login_token(self):
        jobs = sources.read_linkedin(LINKEDIN)
        self.assertEqual([j["url"] for j in jobs], ["https://www.linkedin.com/jobs/view/111",
                                                    "https://www.linkedin.com/jobs/view/222"])
        self.assertEqual((jobs[0]["title"], jobs[0]["company"], jobs[0]["location"]),
                         ("Junior Data Scientist", "Company A", "São Paulo, SP"))
        self.assertEqual(jobs[1]["title"], "Data Engineer")
        self.assertNotIn("SECRET", json.dumps(jobs))

    def test_indeed_list(self):
        jobs, failures = sources.read_indeed(INDEED_LIST)
        self.assertEqual((len(jobs), failures), (1, 0))
        j = jobs[0]
        self.assertEqual(j["url"], "https://br.indeed.com/viewjob?jk=0123456789abcdef")
        self.assertEqual((j["title"], j["company"], j["location"]), ("Data Analyst", "Company C", "Vitória, ES"))
        self.assertIn("SQL and Python", j["description"])

    def test_indeed_single_job_decodes_the_id_offline(self):
        sources.indeed_jk = lambda link: None                    # any network attempt would fail
        body = (f"Olá, Ana,\n\nCom base no seu currículo...\n\nData Engineer\nCompany D\nRemoto\n"
                f"Ambiente de trabalho: Home Office\n\nVer vaga: {cts_link('fedcba9876543210')}\n")
        status, jobs, _ = sources.read_email(message("Indeed <donotreply@match.indeed.com>", body), NOW, "")
        self.assertEqual(status, "ok")
        self.assertEqual(jobs[0]["url"], "https://br.indeed.com/viewjob?jk=fedcba9876543210")
        self.assertEqual((jobs[0]["title"], jobs[0]["company"]), ("Data Engineer", "Company D"))

    def test_links_sent_by_the_user_are_cleaned(self):
        body = ("https://www.linkedin.com/comm/jobs/view/333?otpToken=SECRET "
                "https://company.gupy.io/job/eyJ9?jobBoardSource=x https://site.example/job/9")
        status, jobs, _ = sources.read_email(message("Ana <ana@example.com>", body, "Fwd: Good job"),
                                             NOW, "ana@example.com")
        self.assertEqual(status, "ok")
        self.assertEqual([j["url"] for j in jobs], ["https://www.linkedin.com/jobs/view/333",
                                                    "https://company.gupy.io/job/eyJ9",
                                                    "https://site.example/job/9"])
        self.assertEqual(jobs[0]["title"], "Good job")

    def test_permanent_errors(self):
        status, _, _ = sources.read_email(message("Shop <promo@shop.example>", "sale"), NOW, "")
        self.assertEqual(status, "permanent")
        status, _, reason = sources.read_email(message("x <jobalerts-noreply@linkedin.com>", "no jobs"), NOW, "")
        self.assertEqual((status, reason), ("permanent", "no job found"))


class TestClean(unittest.TestCase):
    def test_merges_duplicates_excludes_titles_and_joins_sources(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "config.json").write_text(json.dumps({"clean": {"exclude_title": r"\bPcD\b"}}))
            base = {"location": "", "description": ""}
            raw = [
                {**base, "source": "linkedin", "url": "L1", "title": "Data Scientist", "company": "A", "collected_at": "2026-11-01T00:00:00Z"},
                {**base, "source": "linkedin", "url": "L1", "title": "Data Scientist", "company": "A", "collected_at": "2026-11-02T00:00:00Z"},
                {**base, "source": "indeed", "url": "I1", "title": "Analista de Dados", "company": "Company B", "collected_at": "2026-11-01T00:00:00Z"},
                {**base, "source": "gupy", "url": "G1", "title": "Pessoa Analista de Dados", "company": "Company B", "collected_at": "2026-11-02T00:00:00Z"},
                {**base, "source": "gupy", "url": "G2", "title": "Data Analyst PcD", "company": "C", "collected_at": "2026-11-01T00:00:00Z"},
            ]
            saved = (common.CONFIG, common.JOBS, common.CLEAN)
            common.CONFIG, common.JOBS, common.CLEAN = d / "config.json", d / "jobs.jsonl", d / "clean.csv"
            try:
                common.append_jsonl(common.JOBS, raw)
                clean.main()
                rows = common.read_csv(common.CLEAN)
            finally:
                common.CONFIG, common.JOBS, common.CLEAN = saved
        self.assertEqual(len(rows), 2)                     # L1 once; G2 excluded; I1 + G1 merged
        b = next(r for r in rows if r["url"] == "I1")
        self.assertEqual((b["sources"], b["other_links"]), ("gupy,indeed", "G1"))
        self.assertEqual(b["id"], clean.job_id("I1"))      # stable id: hash of the link


class TestTriageAndDeliver(unittest.TestCase):
    def test_only_valid_decisions_are_accepted(self):
        answer = ('Here: [{"id": "a", "category": "great", "resume": "ml", "language": "en", "reason": "ok"},'
                  ' {"id": "b", "category": "maybe", "resume": "ml", "language": "EN"},'
                  ' {"id": "c", "category": "normal", "resume": "other", "language": "EN"},'
                  ' {"id": "zzz", "category": "normal", "resume": "data", "language": "EN"}] end')
        decisions = triage.parse(answer, {"a", "b", "c"}, ["ml", "data", "general"], ["EN", "PT"])
        self.assertEqual(list(decisions), ["a"])           # b: bad category; c: unknown resume; zzz: unknown id
        self.assertEqual(decisions["a"]["language"], "EN")

    def test_email_subject_shows_the_resume(self):
        j = {k: "" for k in triage.COLUMNS}
        j.update(resume="data", language="PT", title="Data Engineer", company="Company D", location="Remote",
                 url="https://example/1", id="x1", triaged_at="2026-11-09T07:08:00Z")
        subject, body = deliver.job_email(j, deliver.MESSAGES)
        self.assertEqual(subject, "[data · PT] Data Engineer · Company D · Remote")
        self.assertIn("https://example/1", body)

    def test_messages_can_be_translated_in_the_config(self):
        j = {k: "" for k in triage.COLUMNS}
        j.update(resume="ml", language="PT", title="Cientista de Dados", company="X", location="Y",
                 url="u", id="x2", triaged_at="2026-11-09T07:08:00Z")
        _, body = deliver.job_email(j, {**deliver.MESSAGES, "title": "Cargo"})
        self.assertTrue(body.startswith("Cargo:"))

    def test_times_are_utc(self):
        self.assertEqual(common.iso(common.from_iso("2026-11-09T04:07:00-03:00")), "2026-11-09T07:07:00Z")


if __name__ == "__main__":
    unittest.main()