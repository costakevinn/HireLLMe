"""Job sources: LinkedIn and Indeed alert emails, links sent by the user, and the Gupy search.

Every job is a dict: source, url (always clean), title, company, location, description, origin.
Security: the original LinkedIn link carries a login token (otpToken) and is NEVER stored;
the clean link is rebuilt from the job id.

Some literals below are in Portuguese on purpose: they match the text of real alert emails
sent in Portuguese (for example "Ver vaga:" in Indeed emails).

Settings (config.json, all optional; configure() reads them):
  "sources": ["linkedin", "indeed", "gupy", "manual"]   which sources are on (default: all)
  "indeed": {"domain": "br.indeed.com",                  the Indeed site of your country
             "resolve_links": true}                      follow the email links to get the job id;
                                                         false keeps the email link as it is
  "gupy": {"pause_seconds": 1}                           pause between Gupy requests
  "manual": {"great_tags": ["great"],                    tags in the subject of a job you send yourself:
             "ready_tags": ["ready"]}                    #great or #ready chooses the label; a resume
                                                         name (#ml) chooses the resume
"""
import base64
import email.utils
import gzip
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import common

USER_AGENT = {"User-Agent": "Mozilla/5.0 (HireLLMe)"}
GUPY_API = "https://portal.gupy.io/api/job-search/jobs"
ALL_SOURCES = ["linkedin", "indeed", "gupy", "manual"]
SETTINGS = {"sources": ALL_SOURCES, "indeed_domain": "br.indeed.com", "resolve_links": True, "gupy_pause": 1,
            "great_tags": ["great"], "ready_tags": ["ready"], "resumes": []}


def configure(cfg):
    """Reads the optional source settings from config.json (see the top of this file)."""
    manual = cfg.get("manual", {})
    SETTINGS.update({"sources": cfg.get("sources", ALL_SOURCES),
                     "indeed_domain": cfg.get("indeed", {}).get("domain", "br.indeed.com"),
                     "resolve_links": cfg.get("indeed", {}).get("resolve_links", True),
                     "gupy_pause": cfg.get("gupy", {}).get("pause_seconds", 1),
                     "great_tags": [t.lower() for t in manual.get("great_tags", ["great"])],
                     "ready_tags": [t.lower() for t in manual.get("ready_tags", ["ready"])],
                     "resumes": [r.lower() for r in cfg.get("triage", {}).get("resumes", [])]})


def enabled(source):
    return source in SETTINGS["sources"]


def indeed_url(jk):
    return f"https://{SETTINGS['indeed_domain']}/viewjob?jk={jk}"


def job(source, url, title="", company="", location="", description=""):
    return {"source": source, "url": url, "title": title.strip(), "company": company.strip(),
            "location": location.strip(), "description": description.strip()}


def paragraphs(text):
    """Splits the text into blocks separated by blank lines (a list of lists of lines)."""
    blocks, current = [], []
    for line in text.splitlines():
        line = line.strip()
        if line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def strip_html(text):
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


# ---------------------------------------------------------------- LinkedIn

RE_LINKEDIN = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")


def read_linkedin(text):
    """Block: title / company / location / ... / "View job: link". Sometimes the title is in the previous block."""
    jobs = []
    blocks = paragraphs(text)
    for i, block in enumerate(blocks):
        for j, line in enumerate(block):
            m = RE_LINKEDIN.search(line)
            if not m:
                continue
            before = block[:j]
            if len(before) < 3 and i > 0:
                before = blocks[i - 1]
            title, company, location = (before + ["", "", ""])[:3]
            jobs.append(job("linkedin", f"https://www.linkedin.com/jobs/view/{m.group(1)}", title, company, location))
    return jobs


# ---------------------------------------------------------------- Indeed

RE_INDEED_LINK = re.compile(r"https://[\w.-]*indeed\.[a-z.]+/\S+")   # engage.indeed.com/f/a/..., br.indeed.com/rc/clk/dl?...
RE_JK = re.compile(r"[?&]jk=([0-9a-f]{16})")
RE_CTS = re.compile(r"https://cts\.indeed\.com/v3/([A-Za-z0-9_-]+)")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def indeed_jk(link):
    """Follows the email link's redirects until the job id (jk) shows up, without opening the job page."""
    m = RE_JK.search(link)
    if m:
        return m.group(1)
    opener = urllib.request.build_opener(_NoRedirect)
    url = link
    for _ in range(5):
        try:
            opener.open(urllib.request.Request(url, headers=USER_AGENT), timeout=15)
            return None
        except urllib.error.HTTPError as e:
            target = e.headers.get("Location")
            if not target:
                return None
            url = urllib.parse.urljoin(url, target)
            m = RE_JK.search(url)
            if m:
                return m.group(1)
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
    return None


def cts_jk(link):
    """cts.indeed.com/v3/ links carry the destination inside (gzip + base64): the jk is read offline."""
    m = RE_CTS.search(link)
    if m:
        try:
            payload = gzip.decompress(base64.urlsafe_b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)))
            found = RE_JK.search(payload.decode("utf-8", "replace"))
            if found:
                return found.group(1)
        except (ValueError, OSError, EOFError):
            pass
    return indeed_jk(link) if SETTINGS["resolve_links"] else None


def read_indeed(text):
    """Alert with a list. Block: title / "Company - Location" / [salary] / snippet / age / link.
    The job id (jk) is read from the link, or found by following it (10 at a time).
    Returns (jobs, failures); a failure is a link that did not answer."""
    blocks = [b for b in paragraphs(text) if len(b) >= 3 and RE_INDEED_LINK.fullmatch(b[-1])]
    if SETTINGS["resolve_links"]:
        with ThreadPoolExecutor(max_workers=10) as pool:
            urls = [indeed_url(jk) if jk else None for jk in pool.map(indeed_jk, [b[-1] for b in blocks])]
        # sponsored jobs (/pagead/ links) carry no job id and Indeed refuses to resolve them (403):
        # the email link itself is kept, it works in the browser
        urls = [u or (b[-1] if "/pagead/" in b[-1] else None) for u, b in zip(urls, blocks)]
    else:
        urls = [b[-1] for b in blocks]                    # the email link, as it is
    jobs, failures = [], 0
    for block, url in zip(blocks, urls):
        if not url:
            failures += 1
            continue
        company, _, location = block[1].partition(" - ")
        jobs.append(job("indeed", url, block[0], company, location, "\n".join(block[2:-1])))
    return jobs, failures


def read_indeed_single(text):
    """Email with ONE job ("<title> na empresa <company>"): block title / company / location / details,
    then the line "Ver vaga: <link>" ("Ver vaga" = "View job" in the Portuguese emails)."""
    blocks = paragraphs(text)
    for k, block in enumerate(blocks):
        if block[0].startswith("Ver vaga:"):
            break
    else:
        return [], 0
    is_benefits = lambda b: b[0].startswith("Benef")      # "Benefícios" = benefits
    job_block = next((b for b in blocks[:k] if len(b) >= 3 and not is_benefits(b)), None)
    if job_block is None:
        return [], 0
    extras = [line for b in blocks[:k] if b is not job_block and is_benefits(b) for line in b]
    link = block[0].split(":", 1)[1].strip()
    jk = cts_jk(link)
    if not jk and not SETTINGS["resolve_links"]:
        url = link
    elif not jk:
        return [], 1
    else:
        url = indeed_url(jk)
    return [job("indeed", url, job_block[0], job_block[1], job_block[2],
                "\n".join(job_block[3:] + extras))], 0


# ---------------------------------------------------------------- links sent by the user

RE_URL = re.compile(r"https?://[^\s<>\"')\]]+")
RE_FORWARD = re.compile(r"^\s*(fwd?|enc|res)\s*:\s*", re.I)   # forward/reply prefixes, English and Portuguese
RE_TAG = re.compile(r"#([\w-]+)")


def subject_tags(subject):
    """'#great #ml Data Scientist Acme' -> ('Data Scientist Acme', 'great', 'ml').
    The label tag is 'great', 'normal' or '' (none); the resume tag is a resume name or ''."""
    label, resume = "", ""
    for tag in (t.lower() for t in RE_TAG.findall(subject)):
        if tag in SETTINGS["great_tags"]:
            label = "great"
        elif tag in SETTINGS["ready_tags"]:
            label = "normal"
        elif tag in SETTINGS["resumes"]:
            resume = tag
    title = re.sub(r"\s+", " ", RE_TAG.sub("", subject)).strip()
    return title, label, resume


def subject_fields(subject):
    """'Data Scientist · Acme · Remote' -> ('Data Scientist', 'Acme', 'Remote'); '|' also separates."""
    parts = [p.strip() for p in re.split(r"\s*[·|]\s*", subject) if p.strip()]
    title, company, location = (parts + ["", "", ""])[:3]
    return title, company, " · ".join([location] + parts[3:]) if len(parts) > 3 else location


def read_manual(text, subject):
    """Every link in the body becomes a job. LinkedIn, Indeed and Gupy links are cleaned.
    The email subject, without its tags (see subject_tags), gives the title, and optionally the
    company and location: '#great Data Scientist · Acme · Remote'."""
    subject, label, resume = subject_tags(subject)
    title, company, location = subject_fields(subject)
    jobs, seen = [], set()
    for url in RE_URL.findall(text):
        m = RE_LINKEDIN.search(url)
        if m:
            url = f"https://www.linkedin.com/jobs/view/{m.group(1)}"
        elif "indeed." in url and RE_JK.search(url):
            url = indeed_url(RE_JK.search(url).group(1))
        elif "gupy.io/job" in url:
            url = url.split("?")[0]
        if url not in seen:
            seen.add(url)
            j = job("manual", url, title, company, location)
            j["tag"], j["resume_tag"] = label, resume
            jobs.append(j)
    return jobs


# ---------------------------------------------------------------- email -> jobs

def read_email(msg, received, user):
    """Returns (status, jobs, reason). status: 'ok', 'skipped' (source turned off), 'transient'
    (try again later) or 'permanent' (manual)."""
    try:
        sender = email.utils.parseaddr(str(msg["From"] or ""))[1].lower()
        subject = str(msg["Subject"] or "")
        body = msg.get_body(("plain",))
        if body is None:
            return "permanent", [], "no plain-text body"
        text = body.get_content()
        failures = 0
        source = ("linkedin" if sender.endswith("linkedin.com") else "indeed" if sender.endswith("indeed.com")
                  else "manual" if user and sender == user.lower() else None)
        if source and not enabled(source):
            return "skipped", [], f"source {source} is turned off"
        if sender.endswith("linkedin.com"):
            jobs = read_linkedin(text)
        elif sender.endswith("indeed.com"):
            jobs, failures = read_indeed(text)
            if not jobs and not failures:                 # single-job email
                jobs, failures = read_indeed_single(text)
        elif user and sender == user.lower():
            jobs = read_manual(text, RE_FORWARD.sub("", subject))
        else:
            return "permanent", [], f"unknown sender ({sender})"
    except Exception as e:
        return "permanent", [], f"could not read: {e}"
    origin = {"type": "email", "sender": sender, "subject": subject,
              "received": common.iso(received), "message_id": str(msg["Message-ID"] or "")}
    for j in jobs:
        j["origin"] = origin
    if failures:
        return "transient", jobs, f"{failures} Indeed link(s) did not answer"
    if not jobs:
        return "permanent", [], "no job found"
    return "ok", jobs, ""


# ---------------------------------------------------------------- Gupy

class GupyError(Exception):
    pass


def search_gupy(terms, since):
    """Gupy jobs published since 'since', for every search term (the same job only once)."""
    seen, jobs = set(), []
    for term in terms:
        offset = 0
        while offset < 500:
            query = urllib.parse.urlencode({"jobName": term, "limit": 100, "offset": offset})
            try:
                with urllib.request.urlopen(urllib.request.Request(f"{GUPY_API}?{query}", headers=USER_AGENT),
                                            timeout=30) as r:
                    data = json.load(r)
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                raise GupyError(f"'{term}': {e}")
            time.sleep(SETTINGS["gupy_pause"])               # be gentle with the server
            for g in data.get("data", []):
                try:
                    published = common.from_iso(g.get("publishedDate") or "")
                except ValueError:
                    continue
                if g.get("id") in seen or published < since:
                    continue
                seen.add(g.get("id"))
                mode = {"remote": "Remote", "hybrid": "Hybrid", "on-site": "On-site"}.get(
                    g.get("workplaceType"), g.get("workplaceType") or "")
                city = ", ".join(x for x in (g.get("city"), g.get("state")) if x)
                j = job("gupy", (g.get("jobUrl") or "").split("?")[0], g.get("name", ""), g.get("careerPageName", ""),
                        " · ".join(x for x in (mode, city) if x), strip_html(g.get("description", "")))
                j["published"] = common.iso(published)
                j["deadline"] = (g.get("applicationDeadline") or "")[:10]
                j["origin"] = {"type": "search", "term": term}
                jobs.append(j)
            offset += 100
            if offset >= data.get("pagination", {}).get("total", 0):
                break
    return jobs