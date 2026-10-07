"""OpenAI-compatible chat client for the pro agent (standard library only).

Configuration (environment, or a local .env next to agent.py that is never packed):
    OPENAI_API_KEY    required (KIMI_API_KEY is accepted as an alternate name)
    OPENAI_BASE_URL   default https://api.kimi.com/coding/v1 (Kimi Coding Plan; outside mainland China
                      use https://api.kimi.ai/coding/v1). On the platform this is injected automatically.
    OPENAI_MODEL      default k3

Calls never block the decision loop: `submit()` starts the request on a background thread and returns a
handle; the agent picks the answer up with `result()` on a later decision. The wall clock keeps running
while the model thinks; token charges still apply to every completed request.
k3 only accepts the default temperature, so none is sent.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
DEFAULT_MODEL = "k3"
_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


class CompletionTruncated(ValueError):
    """The provider exhausted its completion budget before delivering valid JSON."""


def api_key() -> str:
    return os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("KIMI_API_KEY", "").strip()


def load_dotenv(path: str) -> None:
    """Fill missing environment variables from a local .env (for local runs only)."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name, value = name.strip(), value.strip().strip('"').strip("'")
                if name and value and not os.environ.get(name):
                    os.environ[name] = value
    except OSError:
        pass


def user_json(user: dict) -> str:
    """Send actual Unicode text, rather than literal six-character escape sequences."""
    return json.dumps(user, ensure_ascii=False, separators=(",", ":"))


class Call:
    """One background chat completion."""

    def __init__(self, client: "LLMClient", tag: str, system: str, user: dict, timeout: float):
        self.tag = tag
        self.answer = None
        self.error = None
        self.seconds = 0.0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(client, system, user, timeout), daemon=True)
        self._thread.start()

    def _run(self, client, system, user, timeout) -> None:
        started = time.monotonic()
        for attempt in range(client.max_retries):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0 or client.disabled_reason:
                self.error = client.disabled_reason or "timeout"
                break
            try:
                self.answer = client._request(system, user, remaining, self.tag)
                self.error = None
                break
            except urllib.error.HTTPError as exc:
                self.error = f"HTTP_{exc.code}"
                if exc.code in (401, 402):
                    client.disable(self.error)
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    break  # auth/balance/parameter errors cannot be fixed by retrying
            except (urllib.error.URLError, OSError) as exc:
                self.error = type(exc).__name__
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                self.error = type(exc).__name__
                break  # a delivered, billed reply must not trigger blind paid retries
            if attempt + 1 < client.max_retries:
                delay = 1.0 + attempt
                if time.monotonic() - started + delay >= timeout:
                    break
                time.sleep(delay)
        self.seconds = time.monotonic() - started
        client._finished(self, system, user)
        self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, seconds: float) -> bool:
        return self._done.wait(max(0.0, seconds))


class LLMClient:
    def __init__(self, log=lambda text: None, call_timeout: float = 90.0, max_calls: int = 1500,
                 max_retries: int = 2, max_in_flight: int = 4):
        self.log = log
        self.base_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/") or DEFAULT_BASE_URL
        self.key = api_key()
        self.model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
        self.call_timeout = float(os.environ.get("PRO_MODEL_CALL_TIMEOUT", call_timeout))
        self.max_calls = int(os.environ.get("PRO_MODEL_MAX_CALLS", max_calls))
        self.max_retries = max(1, int(os.environ.get("PRO_MODEL_MAX_RETRIES", max_retries)))
        self.max_in_flight = int(os.environ.get("PRO_MODEL_MAX_IN_FLIGHT", max_in_flight))
        self.cache_size = max(0, int(os.environ.get("PRO_MODEL_CACHE_SIZE", "128")))
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._pending = {}
        self.disabled_reason = None
        self.stats = {"requests": 0, "cache_hits": 0, "prompt_tokens": 0,
                      "prompt_cache_hit_tokens": 0, "completion_tokens": 0,
                      "reasoning_tokens": 0, "missing_usage": 0, "estimated_cny": 0.0}
        deepseek_flash = (urllib.parse.urlparse(self.base_url).hostname == "api.deepseek.com"
                          and self.model == "deepseek-flash")
        self._priced_flash = deepseek_flash
        self.input_price = float(os.environ.get("PRO_MODEL_INPUT_CNY_PER_M", "2"))
        self.cached_price = float(os.environ.get("PRO_MODEL_CACHED_CNY_PER_M", ".04"))
        self.output_price = float(os.environ.get("PRO_MODEL_OUTPUT_CNY_PER_M", "8"))
        thinking = os.environ.get("PRO_MODEL_THINKING", "auto").strip().lower()
        if thinking not in ("auto", "enabled", "disabled"):
            raise ValueError("PRO_MODEL_THINKING must be auto, enabled or disabled")
        # Flash defaults to thinking on the official API; bounded advisory stages
        # need final JSON rather than spending the entire budget on reasoning.
        if thinking == "auto":
            thinking = "disabled" if deepseek_flash else None
        self.thinking = thinking
        self.max_tokens = int(os.environ.get("PRO_MODEL_MAX_TOKENS",
                                             "8192" if deepseek_flash and thinking == "enabled" else "2000"))
        self.reasoning_effort = os.environ.get("PRO_MODEL_REASONING_EFFORT", "low" if deepseek_flash else "").strip()
        self.calls: list[Call] = []
        self.ok = 0
        self.failed = 0

    def _cache_key(self, tag, system, user):
        content = json.dumps([tag, system, user], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode("utf-8")).digest()

    def disable(self, reason):
        with self._lock:
            if not self.disabled_reason:
                self.disabled_reason = reason
                self.log(f"llm: disabled for this run ({reason}); rules decide")

    def _finished(self, call, system, user):
        with self._lock:
            key = self._cache_key(call.tag, system, user)
            if self.cache_size and call.answer is not None:
                self._cache[key] = copy.deepcopy(call.answer)
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            if self._pending.get(key) is call:
                self._pending.pop(key, None)

    def _record_usage(self, tag, data):
        usage = data.get("usage")
        with self._lock:
            if not isinstance(usage, dict) or "prompt_tokens" not in usage or "completion_tokens" not in usage:
                self.stats["missing_usage"] += 1
                self.log(f"llm: usage {tag} unavailable; cost unknown")
                return
            def count(name):
                try:
                    return max(0, int(usage.get(name, 0)))
                except (TypeError, ValueError):
                    return 0
            prompt, completion = count("prompt_tokens"), count("completion_tokens")
            hit = min(prompt, count("prompt_cache_hit_tokens"))
            details = usage.get("completion_tokens_details") or {}
            reasoning = details.get("reasoning_tokens", 0) if isinstance(details, dict) else 0
            try:
                reasoning = max(0, int(reasoning))
            except (TypeError, ValueError):
                reasoning = 0
            for name, value in (("prompt_tokens", prompt), ("prompt_cache_hit_tokens", hit),
                                ("completion_tokens", completion), ("reasoning_tokens", reasoning)):
                self.stats[name] += value
            # Conservative Flash estimate at peak prices, not an invoice. Reasoning
            # is included in completion_tokens and must never be charged twice.
            cost = ((prompt - hit) * self.input_price + hit * self.cached_price
                    + completion * self.output_price) / 1_000_000 if self._priced_flash else None
            if cost is not None:
                self.stats["estimated_cny"] += cost
            record = {"stage": tag, "input": prompt, "cached_input": hit, "output": completion,
                      "reasoning": reasoning, "estimated_cny_peak": cost,
                      "run_estimated_cny_peak": self.stats["estimated_cny"] if self._priced_flash else None}
            self.log("llm: usage " + json.dumps(record, separators=(",", ":")))

    def _request(self, system: str, user: dict, timeout: float, tag: str = "request") -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user_json(user)}],
            "max_tokens": self.max_tokens,
        }
        if self.thinking is not None:
            payload["thinking"] = {"type": self.thinking}
        if self.thinking == "enabled" and self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/chat/completions", data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer " + self.key})
        with self._lock:
            if self.disabled_reason:
                raise ValueError(self.disabled_reason)
            if self.stats["requests"] >= self.max_calls:
                raise ValueError("request limit reached")
            self.stats["requests"] += 1
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("response is not a JSON object")
        self._record_usage(tag, data)
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise CompletionTruncated("completion token limit reached")
        text = choice["message"]["content"] or ""
        match = _JSON_OBJECT.search(text)
        if not match:
            raise ValueError("no JSON object in the reply")
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("reply is not a JSON object")
        return parsed

    def in_flight(self) -> int:
        return sum(1 for call in self.calls if not call.done())

    def submit(self, tag: str, system: str, user: dict, wallclock_left: float):
        """Start a call in the background; None when the run's limits say no."""
        timeout = min(self.call_timeout, wallclock_left - 30.0)
        key = self._cache_key(tag, system, user)
        with self._lock:
            if key in self._cache:
                self.stats["cache_hits"] += 1
                self._cache.move_to_end(key)
                call = Call.__new__(Call)
                call.tag, call.answer, call.error, call.seconds = tag, copy.deepcopy(self._cache[key]), None, 0.0
                call._done = threading.Event()
                call._done.set()
                self.log(f"llm: {tag} exact cache hit; no API request")
                return call
            if key in self._pending:
                self.stats["cache_hits"] += 1
                return self._pending[key]
            if (self.disabled_reason or self.stats["requests"] >= self.max_calls
                    or timeout < 5.0 or self.in_flight() >= self.max_in_flight):
                return None
            # Snapshot inputs before the background thread; callers may update
            # their evidence while this request is still in flight.
            call = Call(self, tag, system, copy.deepcopy(user), timeout)
            self.calls.append(call)
            self._pending[key] = call
            return call

    def collect(self, call):
        """The parsed answer of a finished call (None while running or after a failure). Logs once."""
        if call is None or not call.done():
            return None
        if not getattr(call, "_logged", False):
            call._logged = True
            if call.answer is not None:
                self.ok += 1
            else:
                self.failed += 1
                self.log(f"llm: {call.tag} failed ({call.error}); rules decide")   # never log the key
        return call.answer
