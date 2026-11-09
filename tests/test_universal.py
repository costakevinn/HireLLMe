"""Tests for the provider-independent parts: LLM error kinds, source settings, untriaged jobs.
No network, no LLM, no Gmail."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import deliver  # noqa: E402
import llm  # noqa: E402
import sources  # noqa: E402
import triage  # noqa: E402

INDEED_ALERT = """Indeed Job Alert

Data Analyst
Acme - Sao Paulo, SP
snippet
2 days ago
https://br.indeed.com/rc/clk/dl?jk=0123456789abcdef&from=ja

Data Engineer
Sponsor Co - Remote
snippet
1 day ago
https://br.indeed.com/pagead/clk/dl?ad=x&mo=r
"""


class TestLLMErrors(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(llm.classify("Claude AI usage limit reached"), "limit")
        self.assertEqual(llm.classify("x", 429), "limit")
        self.assertEqual(llm.classify("API Error: 401 OAuth token has expired"), "setup")
        self.assertEqual(llm.classify("model not found", 404), "setup")
        self.assertEqual(llm.classify("Overloaded", 529), "transient")
        self.assertEqual(llm.classify("something odd"), "transient")

    def test_unknown_provider(self):
        with self.assertRaises(llm.LLMError) as e:
            llm.settings({"triage": {"provider": "nope"}})
        self.assertEqual(e.exception.kind, "setup")

    def test_answer_without_json_is_output_error(self):
        with self.assertRaises(llm.LLMError) as e:
            triage.parse("sorry, no json", {"a"}, ["ml"], ["EN"])
        self.assertEqual(e.exception.kind, "output")


class TestSources(unittest.TestCase):
    def tearDown(self):
        sources.configure({})

    def test_new_indeed_links_and_sponsored(self):
        sources.configure({"indeed": {"domain": "www.indeed.com"}})
        original = sources.indeed_jk
        sources.indeed_jk = lambda link: (sources.RE_JK.search(link) or [None, None])[1]   # no network
        try:
            jobs, failures = sources.read_indeed(INDEED_ALERT)
        finally:
            sources.indeed_jk = original
        self.assertEqual(failures, 0)
        self.assertEqual(jobs[0]["url"], "https://www.indeed.com/viewjob?jk=0123456789abcdef")
        self.assertIn("/pagead/", jobs[1]["url"])            # sponsored: the email link is kept

    def test_turned_off_source_is_skipped(self):
        from email.message import EmailMessage
        sources.configure({"sources": ["linkedin"]})
        msg = EmailMessage()
        msg["From"] = "Indeed <donotreply@jobalert.indeed.com>"
        msg.set_content(INDEED_ALERT)
        status, jobs, _ = sources.read_email(msg, None, "me@example.com")
        self.assertEqual((status, jobs), ("skipped", []))


class TestJobsYouSend(unittest.TestCase):
    def setUp(self):
        sources.configure({"manual": {"great_tags": ["otima", "great"], "ready_tags": ["pronta"]},
                           "triage": {"resumes": ["ml", "data"]}})

    def tearDown(self):
        sources.configure({})

    def test_subject_tags(self):
        self.assertEqual(sources.subject_tags("#otima #ML Data Scientist Acme"), ("Data Scientist Acme", "great", "ml"))
        self.assertEqual(sources.subject_tags("#pronta Analyst"), ("Analyst", "normal", ""))
        self.assertEqual(sources.subject_tags("Analyst #unknown"), ("Analyst", "", ""))

    def test_subject_fields(self):
        self.assertEqual(sources.subject_fields("Data Scientist · Acme · Remote"), ("Data Scientist", "Acme", "Remote"))
        self.assertEqual(sources.subject_fields("Data Scientist | Acme"), ("Data Scientist", "Acme", ""))
        self.assertEqual(sources.subject_fields("Data Scientist"), ("Data Scientist", "", ""))
        jobs = sources.read_manual("https://example.com/job/1", "#otima Data Scientist · Acme · Remote")
        self.assertEqual([jobs[0][k] for k in ("title", "company", "location", "tag")],
                         ["Data Scientist", "Acme", "Remote", "great"])

    def test_your_choice_wins(self):
        llm_said = {"category": "rejected", "resume": "data", "reason": "too senior"}
        no_tag = {"tag": "", "resume_tag": "", "collected_at": ""}
        tagged = {"tag": "great", "resume_tag": "ml", "collected_at": ""}
        self.assertEqual(triage.apply_hint(llm_said, no_tag)["category"], "normal")     # never rejected
        d = triage.apply_hint(llm_said, tagged)
        self.assertEqual((d["category"], d["resume"]), ("great", "ml"))
        self.assertTrue(d["reason"].startswith("sent by you"))
        self.assertEqual(triage.apply_hint(llm_said, None), llm_said)                   # not yours: unchanged


class TestUntriaged(unittest.TestCase):
    def test_subject(self):
        j = {"id": "a1", "category": triage.UNTRIAGED, "resume": "", "language": "", "reason": "",
             "title": "T", "company": "C", "location": "L", "sources": "gupy", "deadline": "",
             "url": "u", "other_links": "", "triaged_at": "2026-11-09T10:00:00Z"}
        subject, body = deliver.job_email(j, deliver.MESSAGES)
        self.assertEqual(subject, "[not triaged] T · C · L")
        self.assertIn("Resume:", body)


if __name__ == "__main__":
    unittest.main()