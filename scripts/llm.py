"""One way to ask any LLM: complete(system, prompt, cfg) -> (text, usage).

Providers (config "triage" -> "provider"):
  claude-code        Claude Pro/Max plan through Claude Code (CLAUDE_CODE_OAUTH_TOKEN)
  anthropic          Anthropic API (LLM_API_KEY)
  openai-compatible  any OpenAI-style API: OpenAI, Gemini, Groq, OpenRouter, Ollama
                     (LLM_API_KEY and "base_url")

Every failure becomes an LLMError with one of four kinds, so the pipeline
reacts the same way whatever the provider:
  setup      key missing, invalid, revoked or expired; unknown model; bad URL
             -> stop now and tell the user what to fix
  limit      rate limit, quota, plan limit, no credit left
             -> stop; jobs wait in the queue for the next run
  transient  timeout, provider down, server error
             -> retried here a few times, then treated like limit
  output     the reply is not what was asked for
             -> the caller decides (retry smaller, or set the jobs aside)
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

PROVIDERS = ("claude-code", "anthropic", "openai-compatible")
RETRY_WAIT = (10, 30)          # seconds before the 2nd and 3rd try of a transient error


class LLMError(RuntimeError):         # a RuntimeError, so older callers and tests still catch it
    def __init__(self, kind, message):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


def settings(cfg):
    """The LLM part of the config, with defaults."""
    t = cfg.get("triage", {})
    provider = t.get("provider", "claude-code")
    if provider not in PROVIDERS:
        raise LLMError("setup", f"unknown provider '{provider}' (use one of {', '.join(PROVIDERS)})")
    return {
        "provider": provider,
        "model": t.get("model", "haiku"),
        "base_url": (t.get("base_url") or "").rstrip("/"),
        "max_tokens": int(t.get("max_tokens", 8000)),
        "timeout": int(t.get("timeout_seconds", 600)),
    }


def estimate_tokens(text):
    """Rough count (about 4 characters per token), good enough for a budget."""
    return len(text) // 4


def complete(system, prompt, cfg):
    """Ask the model once (retrying transient errors). Returns (text, usage)."""
    s = settings(cfg)
    call = {"claude-code": _claude_code, "anthropic": _anthropic,
            "openai-compatible": _openai}[s["provider"]]
    for attempt in range(len(RETRY_WAIT) + 1):
        try:
            return call(system, prompt, s)
        except LLMError as e:
            if e.kind != "transient" or attempt == len(RETRY_WAIT):
                raise
            print(f"  ! {e.message}; trying again in {RETRY_WAIT[attempt]} s")
            time.sleep(RETRY_WAIT[attempt])


def preflight(cfg):
    """A tiny request before the real work: is the provider reachable right now?"""
    text, usage = complete("You are a health check.", "Reply with the word OK and nothing else.", cfg)
    if "ok" not in text.lower():
        raise LLMError("output", f"health check got an unexpected reply: {text[:80]!r}")
    return usage


# ------------------------------------------------------------ error kinds

_LIMIT = re.compile(r"rate.?limit|usage limit|limit reached|quota|resource.?exhausted|"
                    r"credit balance|insufficient|too many requests|\b429\b", re.I)
_SETUP = re.compile(r"authenticat|unauthori[sz]ed|invalid.{0,20}(key|token)|api key|"
                    r"expired|revoked|permission|forbidden|not.?found|does not exist|"
                    r"\b40[134]\b", re.I)
_TRANSIENT = re.compile(r"overloaded|timed? ?out|unavailable|internal server error|"
                        r"bad gateway|connection|\b5\d\d\b", re.I)


def classify(message, status=None):
    """Turn a provider's error (HTTP status and/or text) into one of the four kinds."""
    if status == 429 or _LIMIT.search(message):
        return "limit"
    if status in (401, 403, 404) or _SETUP.search(message):
        return "setup"
    if (status and status >= 500) or _TRANSIENT.search(message):
        return "transient"
    if status and 400 <= status < 500:
        return "setup"                # some other bad request: config is the likely cause
    return "transient"


# ------------------------------------------------------------ providers

def _claude_code(system, prompt, s):
    if os.environ.get("ANTHROPIC_API_KEY"):
        # with this set, Claude Code would bill the paid API instead of the plan
        raise LLMError("setup", "ANTHROPIC_API_KEY is set; remove it (claude-code uses the plan token)")
    if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise LLMError("setup", "CLAUDE_CODE_OAUTH_TOKEN is missing (create it with: claude setup-token)")
    cmd = ["claude", "-p", "--model", s["model"], "--system-prompt", system, "--tools", "",
           "--output-format", "json", "--no-session-persistence"]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=s["timeout"])
    except subprocess.TimeoutExpired:
        raise LLMError("transient", f"no answer after {s['timeout']} s")
    except FileNotFoundError:
        raise LLMError("setup", "the claude command is not installed")
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        message = (r.stderr or r.stdout or f"exit code {r.returncode}").strip()[:300]
        raise LLMError(classify(message), message)
    if out.get("is_error") or r.returncode != 0:
        message = str(out.get("result") or out.get("subtype") or r.stderr).strip()[:300]
        raise LLMError(classify(message), message)
    u = out.get("usage") or {}
    usage = {"input": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
             + u.get("cache_read_input_tokens", 0),
             "output": u.get("output_tokens", 0)}
    return out.get("result", ""), usage


def _post(url, headers, body, timeout):
    """POST JSON, return the decoded answer or raise an LLMError."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        message = f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}"
        raise LLMError(classify(message, e.code), message)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise LLMError("transient", str(getattr(e, "reason", e)))
    except json.JSONDecodeError:
        raise LLMError("transient", "the provider sent an answer that is not JSON")


def _api_key():
    key = os.environ.get("LLM_API_KEY")
    if not key:
        raise LLMError("setup", "LLM_API_KEY is missing")
    return key


def _anthropic(system, prompt, s):
    out = _post(s["base_url"] or "https://api.anthropic.com/v1/messages",
                {"x-api-key": _api_key(), "anthropic-version": "2023-06-01"},
                {"model": s["model"], "max_tokens": s["max_tokens"], "system": system,
                 "messages": [{"role": "user", "content": prompt}]},
                s["timeout"])
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    u = out.get("usage") or {}
    return text, {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)}


def _openai(system, prompt, s):
    if not s["base_url"]:
        raise LLMError("setup", "base_url is missing (e.g. https://api.groq.com/openai/v1)")
    local = "localhost" in s["base_url"] or "127.0.0.1" in s["base_url"]
    key = os.environ.get("LLM_API_KEY") if local else _api_key()   # Ollama needs no key
    out = _post(f"{s['base_url']}/chat/completions",
                {"Authorization": f"Bearer {key}"} if key else {},
                {"model": s["model"], "max_tokens": s["max_tokens"],
                 "messages": [{"role": "system", "content": system},
                              {"role": "user", "content": prompt}]},
                s["timeout"])
    try:
        text = out["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError("output", f"unexpected answer: {json.dumps(out)[:200]}")
    u = out.get("usage") or {}
    return text, {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0)}