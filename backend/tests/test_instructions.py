"""Per-platform execution prompts: iOS must get the same objective the evals send."""

from tamam.harness.instructions import execution_prompt_for


def test_ios_gets_the_raw_objective_not_the_web_prompt():
    prompt = execution_prompt_for("ios", "", "Filter by Toys, then save the search")
    assert prompt == "Filter by Toys, then save the search"
    assert "Navigate to" not in prompt and "multi-action" not in prompt


def test_web_keeps_the_web_prompt():
    prompt = execution_prompt_for("web", "https://x.example", "check out")
    assert "Navigate to https://x.example" in prompt and "check out" in prompt
