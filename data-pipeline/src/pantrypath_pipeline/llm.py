"""Provider-neutral LLM client: send a prompt, get a JSON object back, cache it.

Any provider works. Set LLM_API_KEY in the repo-root .env; the provider is guessed from
the key's format (or set LLM_PROVIDER), and LLM_MODEL overrides the default model in
config/pipeline.yaml (llm.models). Claude is called through the official anthropic SDK;
Gemini and OpenAI through plain HTTP, so no other vendor SDK is needed.

Every reply is cached in llm.cache_dir, keyed by the prompt and the JSON schema only (not
by provider or model). Reruns cost nothing, and anyone can replay a run without a key:
with LLM_API_KEY empty, get_client() returns a ReplayClient that only reads the cache.
Reproducibility comes from this cache, not from temperature: Claude's API no longer
accepts sampling settings.
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from pantrypath_pipeline.config import Config, secret
from pantrypath_pipeline.errors import LLMError

log = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "gemini", "openai")
# Claude models that take server-side refusal fallbacks in the "default" form
FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


@dataclass(frozen=True)
class Usage:
    """Tokens billed for one call. output_tokens includes thinking tokens."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass
class LLMReply:
    data: dict[str, Any]
    usage: Usage
    model: str  # the model that actually answered
    provider: str
    cached: bool = False
    # Usage per model; more than one entry only when a fallback model answered part of a call
    usage_by_model: dict[str, Usage] = field(default_factory=dict)
    raw_text: str = ""

    def __post_init__(self) -> None:
        if not self.usage_by_model:
            self.usage_by_model = {self.model: self.usage}


class LLMReplyError(LLMError):
    """The LLM answered, but the reply can't be used (not JSON, cut off, or declined).

    Tokens were still spent, so `reply` carries the usage (its data is empty).
    """

    def __init__(self, message: str, reply: LLMReply):
        super().__init__(message)
        self.reply = reply


class LLMClient(Protocol):
    provider: str
    model: str

    def complete_json(self, prompt: str, schema: dict, *, purpose: str) -> LLMReply: ...


# ---------------------------------------------------------------- helpers


def detect_provider(api_key: str, explicit: str | None = None) -> str:
    """Which provider a key belongs to: LLM_PROVIDER if given, else the key's format."""
    if explicit:
        explicit = explicit.strip().lower()
        if explicit not in PROVIDERS:
            raise LLMError(f"LLM_PROVIDER={explicit!r} is not supported; use one of {PROVIDERS}")
        return explicit
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("AIza"):
        return "gemini"
    if api_key.startswith("sk-"):
        return "openai"
    raise LLMError(
        "Can't tell which LLM provider LLM_API_KEY belongs to; "
        "set LLM_PROVIDER in .env (anthropic | gemini | openai)"
    )


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a reply as one JSON object (tolerating a ```json fence)."""
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, got {type(data).__name__}")
    return data


def _with_data(reply: LLMReply) -> LLMReply:
    try:
        reply.data = parse_json_object(reply.raw_text)
    except ValueError as exc:
        raise LLMReplyError(f"The LLM reply is not a JSON object: {exc}", reply) from exc
    return reply


def _log_call(reply: LLMReply, purpose: str) -> None:
    u = reply.usage
    log.info(
        "LLM call %s: model=%s input=%d output=%d cache_read=%d cache_write=%d",
        purpose, reply.model, u.input_tokens, u.output_tokens,
        u.cache_read_tokens, u.cache_write_tokens,
    )


def _temperature(provider: str, settings: dict[str, Any]) -> float | None:
    if provider in settings.get("temperature_providers", []):
        value = settings.get("temperature")
        return None if value is None else float(value)
    return None


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write JSON via a temp file + rename, so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- providers


class AnthropicClient:
    """Claude through the official SDK, with JSON output constrained by the schema.

    No temperature: since API version 2026-10-21 Claude rejects sampling settings.
    Opus 5.5 always thinks; llm.effort sets how much (thinking is billed as output).
    """

    provider = "anthropic"

    def __init__(self, api_key: str, model: str, settings: dict[str, Any], sdk_client: Any = None):
        import anthropic  # only needed when Claude is the provider

        self._sdk = anthropic
        self.model = model
        self.max_tokens = int(settings.get("max_output_tokens", 16000))
        self.effort = settings.get("effort")
        self.fallbacks = bool(settings.get("refusal_fallbacks", True)) and model in FALLBACK_MODELS
        self._client = sdk_client or anthropic.Anthropic(
            api_key=api_key,
            timeout=float(settings.get("timeout_s", 180)),
            max_retries=max(int(settings.get("max_attempts", 4)) - 1, 0),  # SDK retries 429/5xx
        )

    def complete_json(self, prompt: str, schema: dict, *, purpose: str) -> LLMReply:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        sdk = self._sdk
        try:
            if self.fallbacks:
                response = self._client.beta.messages.create(
                    **request, fallbacks="default", betas=[FALLBACK_BETA]
                )
            else:
                response = self._client.messages.create(**request)
        except sdk.AuthenticationError as exc:
            raise LLMError("The LLM API rejected LLM_API_KEY (401); check the key in .env") from exc
        except sdk.PermissionDeniedError as exc:
            raise LLMError(f"LLM_API_KEY may not use {self.model} (403): {exc.message}") from exc
        except sdk.NotFoundError as exc:
            raise LLMError(f"Model {self.model!r} was not found; check LLM_MODEL") from exc
        except sdk.RateLimitError as exc:
            raise LLMError("LLM rate limit hit even after retries; lower llm.concurrency") from exc
        except sdk.APIStatusError as exc:
            raise LLMError(f"LLM request failed (HTTP {exc.status_code}): {exc.message}") from exc
        except sdk.APIConnectionError as exc:
            raise LLMError(f"Could not reach the LLM API: {exc}") from exc

        usage, by_model = _anthropic_usage(response, self.model)
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        reply = LLMReply(
            data={}, usage=usage, model=getattr(response, "model", None) or self.model,
            provider=self.provider, usage_by_model=by_model, raw_text=text,
        )
        _log_call(reply, purpose)
        if response.stop_reason == "refusal":
            category = getattr(getattr(response, "stop_details", None), "category", None)
            raise LLMReplyError(f"The LLM declined the request (category: {category})", reply)
        if response.stop_reason == "max_tokens":
            raise LLMReplyError(
                f"The LLM reply was cut off at {self.max_tokens} tokens; "
                "raise llm.max_output_tokens", reply,
            )
        return _with_data(reply)


def _anthropic_usage(response: Any, requested_model: str) -> tuple[Usage, dict[str, Usage]]:
    def convert(u: Any) -> Usage:
        return Usage(
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )

    iterations = getattr(response.usage, "iterations", None) or []
    if len(iterations) > 1:  # a fallback model answered part of the call: price each share
        by_model: dict[str, Usage] = {}
        for it in iterations:
            name = getattr(it, "model", None) or requested_model
            by_model[name] = by_model.get(name, Usage()) + convert(it)
        return sum(by_model.values(), Usage()), by_model
    total = convert(response.usage)
    return total, {getattr(response, "model", None) or requested_model: total}


class _TransientError(Exception):
    """Rate limit, server error or network problem: worth another try."""


def _snippet(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


def _post_json(url: str, *, headers: dict[str, str], body: dict, settings: dict) -> dict:
    """POST with retries on 429, 5xx and network errors; other errors fail at once."""
    attempts = int(settings.get("max_attempts", 4))

    @retry(
        retry=retry_if_exception_type(_TransientError),
        stop=stop_after_attempt(attempts),
        wait=wait_random_exponential(multiplier=1, max=float(settings.get("retry_wait_max_s", 60))),
        reraise=True,
    )
    def attempt() -> dict:
        try:
            resp = requests.post(
                url, headers=headers, json=body, timeout=float(settings.get("timeout_s", 180))
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise _TransientError(f"network error: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _TransientError(f"HTTP {resp.status_code}: {_snippet(resp.text)}")
        if resp.status_code in (401, 403):
            raise LLMError(
                f"The LLM API rejected LLM_API_KEY (HTTP {resp.status_code}); check the key in .env"
            )
        if resp.status_code >= 400:
            raise LLMError(f"LLM request failed (HTTP {resp.status_code}): {_snippet(resp.text)}")
        try:
            return resp.json()
        except ValueError as exc:
            raise _TransientError("the LLM API answered with something that isn't JSON") from exc

    try:
        return attempt()
    except _TransientError as exc:
        raise LLMError(f"LLM request still failing after {attempts} attempts: {exc}") from exc


def to_gemini_schema(schema: Any) -> Any:
    """Gemini's responseSchema is an OpenAPI subset: no additionalProperties, and an
    optional value is `nullable` rather than anyOf [..., null]."""
    if isinstance(schema, list):
        return [to_gemini_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "additionalProperties":
            continue
        if key == "anyOf":
            options = [o for o in value if o.get("type") != "null"]
            if len(options) == 1 and len(options) < len(value):
                out.update(to_gemini_schema(options[0]))
                out["nullable"] = True
                continue
        out[key] = to_gemini_schema(value)
    return out


class GeminiClient:
    """Gemini generateContent over plain HTTP, JSON constrained by responseSchema."""

    provider = "gemini"

    def __init__(self, api_key: str, model: str, settings: dict[str, Any]):
        self._key = api_key
        self.model = model
        self.settings = settings

    def complete_json(self, prompt: str, schema: dict, *, purpose: str) -> LLMReply:
        config: dict[str, Any] = {
            "maxOutputTokens": int(self.settings.get("max_output_tokens", 16000)),
            "responseMimeType": "application/json",
            "responseSchema": to_gemini_schema(schema),
        }
        temperature = _temperature(self.provider, self.settings)
        if temperature is not None:
            config["temperature"] = temperature
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": config}
        payload = _post_json(
            GEMINI_URL.format(model=self.model), headers={"x-goog-api-key": self._key},
            body=body, settings=self.settings,
        )
        meta = payload.get("usageMetadata") or {}
        cached = int(meta.get("cachedContentTokenCount") or 0)
        usage = Usage(
            input_tokens=max(int(meta.get("promptTokenCount") or 0) - cached, 0),
            output_tokens=int(meta.get("candidatesTokenCount") or 0)
            + int(meta.get("thoughtsTokenCount") or 0),
            cache_read_tokens=cached,
        )
        candidates = payload.get("candidates") or []
        candidate = candidates[0] if candidates else {}
        parts = (candidate.get("content") or {}).get("parts") or []
        reply = LLMReply(
            data={}, usage=usage, model=payload.get("modelVersion") or self.model,
            provider=self.provider,
            raw_text="".join(p.get("text", "") for p in parts if not p.get("thought")),
        )
        _log_call(reply, purpose)
        if not candidates:
            reason = (payload.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMReplyError(f"The LLM returned no answer ({reason})", reply)
        finish = candidate.get("finishReason")
        if finish == "MAX_TOKENS":
            raise LLMReplyError("The LLM reply was cut off; raise llm.max_output_tokens", reply)
        if finish not in (None, "STOP"):
            raise LLMReplyError(f"The LLM stopped early (finishReason {finish})", reply)
        return _with_data(reply)


class OpenAIClient:
    """OpenAI Chat Completions over plain HTTP, JSON constrained by a strict schema."""

    provider = "openai"

    def __init__(self, api_key: str, model: str, settings: dict[str, Any]):
        self._key = api_key
        self.model = model
        self.settings = settings

    def complete_json(self, prompt: str, schema: dict, *, purpose: str) -> LLMReply:
        name = re.sub(r"[^A-Za-z0-9_-]", "_", purpose)[:64] or "reply"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": int(self.settings.get("max_output_tokens", 16000)),
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": name, "schema": schema, "strict": True},
            },
        }
        temperature = _temperature(self.provider, self.settings)
        if temperature is not None:
            body["temperature"] = temperature
        payload = _post_json(
            OPENAI_URL, headers={"Authorization": f"Bearer {self._key}"}, body=body,
            settings=self.settings,
        )
        u = payload.get("usage") or {}
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usage = Usage(
            input_tokens=max(int(u.get("prompt_tokens") or 0) - cached, 0),
            output_tokens=int(u.get("completion_tokens") or 0),  # includes reasoning tokens
            cache_read_tokens=cached,
        )
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        reply = LLMReply(
            data={}, usage=usage, model=payload.get("model") or self.model,
            provider=self.provider, raw_text=message.get("content") or "",
        )
        _log_call(reply, purpose)
        if message.get("refusal"):
            raise LLMReplyError(f"The LLM declined the request: {message['refusal']}", reply)
        finish = choice.get("finish_reason")
        if finish == "length":
            raise LLMReplyError("The LLM reply was cut off; raise llm.max_output_tokens", reply)
        if finish not in (None, "stop"):
            raise LLMReplyError(f"The LLM stopped early (finish_reason {finish})", reply)
        return _with_data(reply)


# ---------------------------------------------------------------- cache


def cache_key(prompt: str, schema: dict) -> str:
    """Same prompt + same schema = same key, whichever provider or model answered."""
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{prompt}\n{canonical}".encode()).hexdigest()


class ResponseCache:
    """One JSON file per reply: the request, raw reply, parsed data, model and usage."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self.path(key)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            log.warning("Ignoring unreadable LLM cache file %s", path)
            return None

    def put(self, key: str, record: dict) -> None:
        write_json_atomic(self.path(key), record)


def usage_entry(reply: LLMReply) -> dict[str, Any]:
    """A reply's model, provider and usage as plain JSON (for records and reports)."""
    return {
        "model": reply.model,
        "provider": reply.provider,
        "cached": reply.cached,
        "usage_by_model": {m: asdict(u) for m, u in reply.usage_by_model.items()},
    }


def reply_from_entry(entry: dict[str, Any]) -> LLMReply:
    by_model = {m: Usage(**u) for m, u in (entry.get("usage_by_model") or {}).items()}
    return LLMReply(
        data={}, usage=sum(by_model.values(), Usage()), model=entry["model"],
        provider=entry["provider"], cached=bool(entry.get("cached")), usage_by_model=by_model,
    )


def _record(reply: LLMReply, *, key: str, purpose: str, prompt: str, schema: dict,
            error: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "purpose": purpose,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **usage_entry(reply),
        "usage": asdict(reply.usage),
        "error": error,
        "data": None if error else reply.data,
        "raw_text": reply.raw_text,
        "request": {"prompt": prompt, "schema": schema},
    }


def _replay(record: dict[str, Any]) -> LLMReply:
    reply = reply_from_entry({**record, "cached": True})
    reply.raw_text = record.get("raw_text", "")
    if record.get("error"):
        raise LLMReplyError(record["error"], reply)
    reply.data = record.get("data") or {}
    return reply


class CachedClient:
    """Wraps a provider client: answers from the cache when it can, else calls and stores.

    Unusable replies are cached too (and raised again on replay), so a replayed run takes
    exactly the same retry path as the original.
    """

    def __init__(self, inner: LLMClient, cache: ResponseCache):
        self.inner = inner
        self.cache = cache
        self.provider = inner.provider
        self.model = inner.model

    def complete_json(self, prompt: str, schema: dict, *, purpose: str) -> LLMReply:
        key = cache_key(prompt, schema)
        hit = self.cache.get(key)
        if hit is not None:
            return _replay(hit)
        try:
            reply = self.inner.complete_json(prompt, schema, purpose=purpose)
        except LLMReplyError as exc:
            self.cache.put(key, _record(exc.reply, key=key, purpose=purpose, prompt=prompt,
                                        schema=schema, error=str(exc)))
            raise
        self.cache.put(key, _record(reply, key=key, purpose=purpose, prompt=prompt, schema=schema))
        return reply


class ReplayClient:
    """Cache only: used when LLM_API_KEY is empty (graders, CI, reruns)."""

    provider = "replay"
    model = "replay"

    def __init__(self, cache: ResponseCache):
        self.cache = cache

    def complete_json(self, prompt: str, schema: dict, *, purpose: str) -> LLMReply:
        hit = self.cache.get(cache_key(prompt, schema))
        if hit is None:
            raise LLMError(
                f"No cached LLM reply for {purpose}, and LLM_API_KEY is empty. Set LLM_API_KEY "
                f"in .env to call an LLM, or `dvc pull` the cached replies into {self.cache.dir}"
            )
        return _replay(hit)


def make_provider_client(provider: str, api_key: str, model: str,
                         settings: dict[str, Any]) -> LLMClient:
    if provider == "anthropic":
        return AnthropicClient(api_key, model, settings)
    if provider == "gemini":
        return GeminiClient(api_key, model, settings)
    if provider == "openai":
        return OpenAIClient(api_key, model, settings)
    raise LLMError(f"Unknown LLM provider {provider!r}; use one of {PROVIDERS}")


def get_client(cfg: Config, cache_dir: Path | None = None) -> LLMClient:
    """The LLM client for this environment: env (LLM_*) wins over config/pipeline.yaml."""
    settings = cfg["llm"]
    cache = ResponseCache(cache_dir or cfg.path(settings["cache_dir"]))
    choice = (secret("LLM_PROVIDER") or str(settings.get("provider") or "auto")).strip().lower()
    api_key = secret("LLM_API_KEY")
    if choice == "replay" or not api_key:
        log.info("LLM: replaying cached replies only (%s)",
                 "llm.provider is replay" if choice == "replay" else "LLM_API_KEY is empty")
        return ReplayClient(cache)
    provider = detect_provider(api_key, None if choice == "auto" else choice)
    model = secret("LLM_MODEL") or (settings.get("models") or {}).get(provider)
    if not model:
        raise LLMError(
            f"No LLM model set for {provider}: set LLM_MODEL in .env "
            f"or llm.models.{provider} in config/pipeline.yaml"
        )
    log.info("LLM: provider %s, model %s", provider, model)
    return CachedClient(make_provider_client(provider, api_key, model, settings), cache)


# ---------------------------------------------------------------- usage report


def rates_for(model: str, prices: dict[str, dict]) -> dict | None:
    """Price row for a model; dated ids (gpt-5.4-mini-2026-03-17) match their base id."""
    if model in prices:
        return prices[model]
    matches = [m for m in prices if model.startswith(m)]
    return prices[max(matches, key=len)] if matches else None


def estimate_usd(usage: Usage, rates: dict | None) -> float | None:
    """Cost in USD, or None when a needed rate is unknown (no guess)."""
    if not rates:
        return None
    total = 0.0
    for name, tokens in (("input", usage.input_tokens), ("output", usage.output_tokens),
                         ("cache_read", usage.cache_read_tokens),
                         ("cache_write", usage.cache_write_tokens)):
        if tokens:
            if name not in rates:
                return None
            total += tokens * float(rates[name]) / 1_000_000
    return round(total, 6)


def summarize_usage(replies: Iterable[LLMReply], prices: dict[str, dict]) -> dict[str, Any]:
    """Totals and per-model calls, tokens and cost. Only real calls count tokens and cost:
    a cached reply costs nothing in this run. est_cost_usd is None when a rate is missing."""
    models: dict[str, dict[str, Any]] = {}
    real_usage: dict[str, Usage] = {}
    calls_real = calls_cached = 0
    for reply in replies:
        if reply.cached:
            calls_cached += 1
        else:
            calls_real += 1
        for model, usage in reply.usage_by_model.items():
            entry = models.setdefault(model, {"calls_real": 0, "calls_cached": 0})
            if reply.cached:
                entry["calls_cached"] += 1
            else:
                entry["calls_real"] += 1
                real_usage[model] = real_usage.get(model, Usage()) + usage
    total_cost: float | None = 0.0
    for model, entry in models.items():
        usage = real_usage.get(model, Usage())
        rates = rates_for(model, prices)
        cost = estimate_usd(usage, rates)
        entry.update(asdict(usage), rates_usd_per_mtok=rates, est_cost_usd=cost)
        total_cost = None if total_cost is None or cost is None else round(total_cost + cost, 6)
    totals = {"calls_real": calls_real, "calls_cached": calls_cached,
              **asdict(sum(real_usage.values(), Usage())), "est_cost_usd": total_cost}
    return {"totals": totals, "models": models}


def write_usage_report(cfg: Config, replies: Iterable[LLMReply]) -> dict[str, Any]:
    settings = cfg["llm"]
    summary = summarize_usage(replies, settings.get("prices_usd_per_mtok") or {})
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "prices_checked": settings.get("prices_checked"),
        "note": "Tokens from the providers' usage fields; cached replies cost nothing in "
                "this run. est_cost_usd is null when a rate is missing.",
        **summary,
    }
    write_json_atomic(cfg.path(settings["usage_report"]), report)
    return report
