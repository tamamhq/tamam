"""The `tamam` command: doctor reports what to fix; run builds the right case and reports the
harness verdict (never the agent's own word)."""

import pytest

from tamam import cli


def test_doctor_lists_every_problem_with_its_fix(monkeypatch, capsys):
    monkeypatch.setattr(cli, "checks", lambda: [
        cli.Check("Xcode", True, "Xcode 26"),
        cli.Check("Appium server running", False, "http://127.0.0.1:4723", "Start it: appium"),
    ])
    assert cli.doctor() == 3
    out = capsys.readouterr().out
    assert "✓ Xcode" in out and "✗ Appium server running" in out and "→ Start it: appium" in out
    assert "1 thing(s) to fix" in out


def test_a_fixture_case_uses_the_buggy_build_when_asked(monkeypatch, tmp_path):
    for v in ("Correct", "Buggy"):
        (tmp_path / v / "Shoply.app").mkdir(parents=True)
    monkeypatch.setattr(cli, "FIXTURE_DIR", tmp_path)
    ok = cli._fixture_case("checkout", buggy=False, device="iPhone 16 Pro", max_steps=30)
    bug = cli._fixture_case("checkout", buggy=True, device="iPhone 16 Pro", max_steps=12)
    assert ok.target.app_build_ref.endswith("Correct/Shoply.app") and ok.expected == "pass"
    assert bug.target.app_build_ref.endswith("Buggy/Shoply.app") and bug.expected == "fail"
    assert bug.max_steps == 12
    with pytest.raises(cli.UsageError, match="Unknown fixture case"):
        cli._fixture_case("nope", buggy=False, device="x", max_steps=30)


def test_run_reports_the_harness_verdict_and_exit_code(monkeypatch, capsys, tmp_path):
    from tamam.executors.base import ExecutionResult
    from tamam.harness.verdict import Outcome

    class Result:
        outcome, reason = Outcome.FAILED, "Total shows $30, not $35"
        evidence = ExecutionResult(video_path="/tmp/run.mp4")
        usage = {"duration_s": 41.2, "total_cost": 0.31}

    async def fake(case):
        return Result()

    for v in ("Correct", "Buggy"):
        (tmp_path / v / "Shoply.app").mkdir(parents=True)
    monkeypatch.setattr(cli, "FIXTURE_DIR", tmp_path)
    monkeypatch.setattr("tamam.evals.runner.execute_case", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "available_iphones", lambda: ["iPhone 16 Pro"])
    code = cli.main(["run", "--fixture", "shoply", "--case", "cart-total", "--buggy"])
    out = capsys.readouterr().out
    assert code == 1 and "FAIL — Total shows $30, not $35" in out and "$0.31" in out and "/tmp/run.mp4" in out


def test_usage_problems_exit_3_not_the_fail_code(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cli, "available_iphones", lambda: ["iPhone 17 Pro"])
    assert cli.main(["run", "Open Settings"]) == 3
    assert "--fixture shoply" in capsys.readouterr().err
    assert cli.main(["run", "--fixture", "shoply", "--device", "iPhone 4"]) == 3
    assert "Try one of: iPhone 17 Pro" in capsys.readouterr().err


def test_run_checks_the_api_key_before_starting(monkeypatch, capsys):
    from tamam.config import get_settings
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    assert cli.main(["run", "--fixture", "shoply"]) == 3
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_the_default_device_falls_back_to_an_installed_iphone():
    assert cli.pick_device(None, ["iPhone 17 Pro", "iPhone 16 Pro"]) == "iPhone 16 Pro"
    assert cli.pick_device(None, ["iPhone 17 Pro", "iPhone Air"]) == "iPhone 17 Pro"
    assert cli.pick_device(None, []) == "iPhone 16 Pro"     # can't tell: let Appium decide
    assert cli.pick_device("iPhone Air", ["iPhone Air"]) == "iPhone Air"
