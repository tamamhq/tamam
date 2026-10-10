"""Run cost accounting and prompt-cache settings."""

from tamam.executors.ios.brain import PydanticAIDecider
from tamam.harness.pricing import cost_usd


def test_cached_tokens_are_billed_at_the_cache_price():
    # 3 calls x 5,587 input: first writes 5,305, next two read it (measured live 2026-09-27)
    full = cost_usd("anthropic:claude-sonnet-5", input_tokens=16761, output_tokens=0)
    cached = cost_usd("anthropic:claude-sonnet-5", input_tokens=16761, output_tokens=0,
                      cache_write_tokens=5305, cache_read_tokens=10610)
    assert cached < full * 0.55


def test_unknown_model_has_no_cost_rather_than_a_guess():
    assert cost_usd("somevendor:model-x", input_tokens=1000, output_tokens=10) is None


def test_brain_caches_the_static_prefix_on_anthropic_only():
    s = PydanticAIDecider("anthropic:claude-sonnet-5", cache=True).model_settings()
    assert s["anthropic_cache_tool_definitions"] and s["anthropic_cache_instructions"]
    assert PydanticAIDecider("anthropic:claude-sonnet-5", cache=False).model_settings() is None
    assert PydanticAIDecider("openai:gpt-6-luna", cache=True).model_settings() is None
