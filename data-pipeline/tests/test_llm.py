"""LLM client: provider detection, the three adapters (faked), cache/replay, usage math.

No test calls a real API: adapters get a fake SDK client or a fake requests.post.
"""

import json
import os
from types import SimpleNamespace

import pytest

from pantrypath_pipeline import llm
from pantrypath_pipeline.errors import LLMError
from pantrypath_pipeline.llm import (
    AnthropicClient,
    CachedClient,
    GeminiClient,
    LLMReply,
    LLMReplyError,
    OpenAIClient,
    ReplayClient,
    ResponseCache,
    Usage,
    cache_key,
    detect_provider,
    estimate_usd,
    get_client,
    rates_for,
    summarize_usage,
    to_gemini_schema,
    write_usage_report,
)

SCHEMA = {
    "type": "object",
    "properties": {"dish": {"type": "string"},
                   "note": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
    "required": ["dish", "note"],
    "additionalProperties": False,
}
OPUS = {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0}


# ---------------------------------------------------------------- provider choice


@pytest.mark.parametrize(("key", "provider"), [
    ("sk-ant-api03-FAKEFAKE", "anthropic"),
    ("AIzaFAKEFAKE", "gemini"),
    ("sk-proj-FAKEFAKE", "openai"),
])
def test_provider_is_guessed_from_key_format(key, provider):
    assert detect_provider(key) == provider


def test_explicit_provider_wins_and_unknown_formats_fail():
    assert detect_provider("whatever", "Anthropic") == "anthropic"
    with pytest.raises(LLMError, match="LLM_PROVIDER"):
        detect_provider("not-a-known-format")
    with pytest.raises(LLMError, match="not supported"):
        detect_provider("sk-ant-x", "mistral")


def test_no_key_means_replay_only(cfg):
    assert isinstance(get_client(cfg), ReplayClient)


def test_env_model_and_provider_win_over_config(cfg, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-FAKE-NOT-A-REAL-KEY")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    client = get_client(cfg)
    assert isinstance(client, CachedClient) and isinstance(client.inner, AnthropicClient)
    assert client.model == "claude-sonnet-5-5"
    assert client.inner.fallbacks is True

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("LLM_MODEL")
    client = get_client(cfg)
    assert isinstance(client.inner, GeminiClient)
    assert client.model == cfg["llm"]["models"]["gemini"]


def test_replay_setting_ignores_the_key(cfg, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-FAKE")
    cfg.data["llm"]["provider"] = "replay"
    assert isinstance(get_client(cfg), ReplayClient)


# ---------------------------------------------------------------- Anthropic (fake SDK)


class FakeSDK:
    """Stands in for anthropic.Anthropic: records requests, returns canned responses."""

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.messages = SimpleNamespace(create=self._create(beta=False))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create(beta=True)))

    def _create(self, beta):
        def create(**kwargs):
            self.calls.append((beta, kwargs))
            return self.response
        return create


def anthropic_response(text, stop_reason="end_turn", model="claude-opus-5-5", iterations=None):
    usage = SimpleNamespace(input_tokens=1200, output_tokens=800, cache_read_input_tokens=0,
                            cache_creation_input_tokens=0, iterations=iterations)
    content = [SimpleNamespace(type="thinking", thinking=""),
               SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop_reason, stop_details=None,
                           model=model, usage=usage)


def test_anthropic_request_shape_and_reply(cfg):
    sdk = FakeSDK(anthropic_response('{"dish": "lasagna", "note": null}'))
    client = AnthropicClient("sk-ant-fake", "claude-opus-5-5", cfg["llm"], sdk_client=sdk)
    reply = client.complete_json("prompt", SCHEMA, purpose="test")
    assert reply.data == {"dish": "lasagna", "note": None}
    assert reply.usage == Usage(1200, 800, 0, 0)
    beta, kwargs = sdk.calls[0]
    assert beta and kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == [llm.FALLBACK_BETA]
    assert kwargs["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA},
                                       "effort": cfg["llm"]["effort"]}
    assert "temperature" not in kwargs and "thinking" not in kwargs


def test_anthropic_without_fallback_support_uses_plain_endpoint(cfg):
    sdk = FakeSDK(anthropic_response('{"dish": "x", "note": null}', model="claude-haiku-5-5"))
    AnthropicClient("sk-ant-fake", "claude-haiku-5-5", cfg["llm"], sdk_client=sdk).complete_json(
        "prompt", SCHEMA, purpose="test")
    beta, kwargs = sdk.calls[0]
    assert not beta and "fallbacks" not in kwargs


@pytest.mark.parametrize(("stop_reason", "message"), [
    ("refusal", "declined"), ("max_tokens", "cut off")])
def test_anthropic_unusable_stop_reasons(cfg, stop_reason, message):
    sdk = FakeSDK(anthropic_response("{", stop_reason=stop_reason))
    client = AnthropicClient("sk-ant-fake", "claude-opus-5-5", cfg["llm"], sdk_client=sdk)
    with pytest.raises(LLMReplyError, match=message) as info:
        client.complete_json("prompt", SCHEMA, purpose="test")
    assert info.value.reply.usage.output_tokens == 800  # tokens were still spent


def test_anthropic_fallback_usage_is_split_by_model(cfg):
    no_cache = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    iterations = [
        SimpleNamespace(type="message", model="claude-opus-5-5", input_tokens=1000,
                        output_tokens=10, **no_cache),
        SimpleNamespace(type="fallback_message", model="claude-opus-4-8", input_tokens=1000,
                        output_tokens=500, **no_cache),
    ]
    sdk = FakeSDK(anthropic_response('{"dish": "x", "note": null}', model="claude-opus-4-8",
                                     iterations=iterations))
    reply = AnthropicClient("sk-ant-fake", "claude-opus-5-5", cfg["llm"],
                            sdk_client=sdk).complete_json("p", SCHEMA, purpose="t")
    assert reply.model == "claude-opus-4-8"
    assert reply.usage_by_model == {"claude-opus-5-5": Usage(1000, 10),
                                    "claude-opus-4-8": Usage(1000, 500)}
    assert reply.usage == Usage(2000, 510)


def test_anthropic_reply_that_is_not_json(cfg):
    sdk = FakeSDK(anthropic_response("Sure! Here is the dish."))
    client = AnthropicClient("sk-ant-fake", "claude-opus-5-5", cfg["llm"], sdk_client=sdk)
    with pytest.raises(LLMReplyError, match="not a JSON object"):
        client.complete_json("prompt", SCHEMA, purpose="test")


# ---------------------------------------------------------------- Gemini / OpenAI (fake HTTP)


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        status, payload = self.responses.pop(0)
        return SimpleNamespace(status_code=status, json=lambda: payload, text=str(payload))


@pytest.fixture
def fast_retries(cfg):
    cfg.data["llm"]["retry_wait_max_s"] = 0
    return cfg


def gemini_payload(text, finish="STOP"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 150,
                              "thoughtsTokenCount": 50, "cachedContentTokenCount": 100},
            "modelVersion": "gemini-2.5-flash"}


def test_gemini_request_and_usage(fast_retries, monkeypatch):
    http = FakeHTTP([(429, {"error": "slow down"}), (200, gemini_payload('{"dish": "pho"}'))])
    monkeypatch.setattr(llm.requests, "post", http.post)
    client = GeminiClient("AIzaFAKE", "gemini-2.5-flash", fast_retries["llm"])
    reply = client.complete_json("prompt", SCHEMA, purpose="test")
    assert reply.data == {"dish": "pho"}
    assert reply.usage == Usage(input_tokens=800, output_tokens=200, cache_read_tokens=100)
    assert len(http.requests) == 2  # the 429 was retried
    sent = http.requests[-1]
    assert sent["headers"] == {"x-goog-api-key": "AIzaFAKE"}
    config = sent["json"]["generationConfig"]
    assert config["temperature"] == 0 and config["responseMimeType"] == "application/json"
    assert "additionalProperties" not in json.dumps(config["responseSchema"])
    assert config["responseSchema"]["properties"]["note"] == {"type": "string", "nullable": True}


def test_gemini_cut_off_reply(fast_retries, monkeypatch):
    http = FakeHTTP([(200, gemini_payload('{"dish": ', finish="MAX_TOKENS"))])
    monkeypatch.setattr(llm.requests, "post", http.post)
    with pytest.raises(LLMReplyError, match="cut off"):
        GeminiClient("AIzaFAKE", "gemini-2.5-flash", fast_retries["llm"]).complete_json(
            "prompt", SCHEMA, purpose="test")


def test_openai_request_and_usage(fast_retries, monkeypatch):
    payload = {"model": "gpt-5.4-mini-2026-03-17",
               "choices": [{"message": {"content": '{"dish": "ramen", "note": null}'},
                            "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 1000, "completion_tokens": 300,
                         "prompt_tokens_details": {"cached_tokens": 200}}}
    http = FakeHTTP([(200, payload)])
    monkeypatch.setattr(llm.requests, "post", http.post)
    reply = OpenAIClient("sk-proj-FAKE", "gpt-5.4-mini", fast_retries["llm"]).complete_json(
        "prompt", SCHEMA, purpose="refine_dish:ramen")
    body = http.requests[0]["json"]
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "refine_dish_ramen"
    assert "temperature" not in body  # OpenAI isn't in llm.temperature_providers
    assert reply.usage == Usage(input_tokens=800, output_tokens=300, cache_read_tokens=200)
    assert reply.model == "gpt-5.4-mini-2026-03-17"


def test_http_errors(fast_retries, monkeypatch):
    monkeypatch.setattr(llm.requests, "post", FakeHTTP([(401, {"error": "bad key"})]).post)
    with pytest.raises(LLMError, match="rejected LLM_API_KEY"):
        OpenAIClient("sk-x", "gpt-5.4-mini", fast_retries["llm"]).complete_json(
            "p", SCHEMA, purpose="t")
    attempts = fast_retries["llm"]["max_attempts"]
    monkeypatch.setattr(llm.requests, "post", FakeHTTP([(503, "down")] * attempts).post)
    with pytest.raises(LLMError, match=f"after {attempts} attempts"):
        OpenAIClient("sk-x", "gpt-5.4-mini", fast_retries["llm"]).complete_json(
            "p", SCHEMA, purpose="t")


def test_gemini_schema_conversion_leaves_plain_schemas_alone():
    plain = {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}
    assert to_gemini_schema(plain) == plain


# ---------------------------------------------------------------- cache and replay


class CountingClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self, reply_data=None, error=None):
        self.calls = 0
        self.reply_data = reply_data
        self.error = error

    def complete_json(self, prompt, schema, *, purpose):
        self.calls += 1
        reply = LLMReply(data=self.reply_data or {}, usage=Usage(100, 20), model=self.model,
                         provider=self.provider, raw_text=json.dumps(self.reply_data))
        if self.error:
            raise LLMReplyError(self.error, reply)
        return reply


def test_cache_key_ignores_provider_but_not_schema():
    assert cache_key("p", SCHEMA) == cache_key("p", json.loads(json.dumps(SCHEMA)))
    assert cache_key("p", SCHEMA) != cache_key("p", {**SCHEMA, "required": ["dish"]})
    assert cache_key("p", SCHEMA) != cache_key("q", SCHEMA)


def test_cached_client_calls_once_then_replays(tmp_path):
    inner = CountingClient({"dish": "lasagna"})
    cache = ResponseCache(tmp_path)
    client = CachedClient(inner, cache)
    first = client.complete_json("prompt", SCHEMA, purpose="t")
    second = client.complete_json("prompt", SCHEMA, purpose="t")
    assert inner.calls == 1
    assert not first.cached and second.cached
    assert second.data == {"dish": "lasagna"} and second.usage == Usage(100, 20)
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["request"]["prompt"] == "prompt" and record["model"] == "fake-model"

    # Anyone without a key can replay the same prompt from the cache
    assert ReplayClient(cache).complete_json("prompt", SCHEMA, purpose="t").data == {
        "dish": "lasagna"}


def test_unusable_replies_are_cached_and_raised_again(tmp_path):
    inner = CountingClient({"oops": 1}, error="The LLM reply is not a JSON object")
    client = CachedClient(inner, ResponseCache(tmp_path))
    for _ in range(2):
        with pytest.raises(LLMReplyError, match="not a JSON object") as info:
            client.complete_json("prompt", SCHEMA, purpose="t")
    assert inner.calls == 1 and info.value.reply.cached


def test_replay_miss_explains_what_to_do(tmp_path):
    with pytest.raises(LLMError, match="LLM_API_KEY is empty"):
        ReplayClient(ResponseCache(tmp_path)).complete_json("new prompt", SCHEMA, purpose="t")


def test_corrupt_cache_file_is_a_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.path(cache_key("p", SCHEMA)).write_text("{not json")
    assert cache.get(cache_key("p", SCHEMA)) is None
    assert not list(tmp_path.glob(".*.tmp"))
    assert os.listdir(tmp_path)  # left for inspection, not deleted


# ---------------------------------------------------------------- usage and cost


def test_cost_math():
    assert estimate_usd(Usage(1_000_000, 100_000), OPUS) == pytest.approx(6.0)
    assert estimate_usd(Usage(0, 0, cache_read_tokens=1_000_000), OPUS) == pytest.approx(0.2)
    assert estimate_usd(Usage(10, 10, cache_write_tokens=10), {"input": 1, "output": 1}) is None
    assert estimate_usd(Usage(10, 10), None) is None


def test_dated_model_ids_find_their_rates():
    prices = {"gpt-5.4-mini": {"input": 0.75}, "gpt-5.4": {"input": 2.5}}
    assert rates_for("gpt-5.4-mini-2026-03-17", prices) == {"input": 0.75}
    assert rates_for("claude-opus-5-5", prices) is None


def test_usage_summary_counts_only_real_calls():
    real = LLMReply({}, Usage(1000, 500), "claude-opus-5-5", "anthropic")
    cached = LLMReply({}, Usage(1000, 500), "claude-opus-5-5", "anthropic", cached=True)
    fallback = LLMReply({}, Usage(2000, 510), "claude-opus-4-8", "anthropic", usage_by_model={
        "claude-opus-5-5": Usage(1000, 10), "claude-opus-4-8": Usage(1000, 500)})
    prices = {"claude-opus-5-5": OPUS,
              "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.5}}
    summary = summarize_usage([real, cached, fallback], prices)
    totals = summary["totals"]
    assert (totals["calls_real"], totals["calls_cached"]) == (2, 1)
    assert totals["input_tokens"] == 3000 and totals["output_tokens"] == 1010
    opus = summary["models"]["claude-opus-5-5"]
    assert (opus["calls_real"], opus["calls_cached"]) == (2, 1)
    assert opus["input_tokens"] == 2000 and opus["output_tokens"] == 510
    assert opus["est_cost_usd"] == pytest.approx((2000 * 4 + 510 * 20) / 1e6)
    assert totals["est_cost_usd"] == pytest.approx(
        opus["est_cost_usd"] + (1000 * 5 + 500 * 25) / 1e6)


def test_usage_report_file(cfg):
    report = write_usage_report(cfg, [LLMReply({}, Usage(10, 5), "claude-opus-5-5", "anthropic")])
    saved = json.loads(cfg.path(cfg["llm"]["usage_report"]).read_text())
    assert saved["totals"] == report["totals"]
    assert saved["prices_checked"] == cfg["llm"]["prices_checked"]
    assert saved["models"]["claude-opus-5-5"]["rates_usd_per_mtok"]["output"] == 20.0


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")
def test_live_llm_round_trip(cfg, tmp_path):  # pragma: no cover - costs a few cents
    from dotenv import dotenv_values

    env = dotenv_values(cfg.path("../.env").resolve())  # conftest hides the real key
    key = env.get("LLM_API_KEY")
    if not key:
        pytest.skip("LLM_API_KEY is empty in .env")
    provider = detect_provider(key, env.get("LLM_PROVIDER") or None)
    model = env.get("LLM_MODEL") or cfg["llm"]["models"][provider]
    client = llm.make_provider_client(provider, key, model, cfg["llm"])
    reply = client.complete_json("Name one Italian pasta dish. Put null in note.", SCHEMA,
                                 purpose="live_test")
    assert isinstance(reply.data["dish"], str)
