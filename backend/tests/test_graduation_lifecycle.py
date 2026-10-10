"""After a run: settings, probation scripts, and the decision recorded on the run."""

from sqlmodel import select

from tamam.db import get_session
from tamam.graduation.lifecycle import fast_mode_setting, graduate
from tamam.models import Outcome, Project, Test, TestRun, TestScript
from tests.test_graduation_compiler import CART, cart_run


async def _setup(project_settings=None, test_graduation=None, outcome=Outcome.PASSED):
    async with get_session() as db:
        p = Project(org_id="o", name="P", settings=project_settings or {})
        db.add(p); await db.flush()
        t = Test(project_id=p.id, name="T", instructions=CART, graduation=test_graduation)
        db.add(t); await db.flush()
        from tamam.graduation.lifecycle import proof_stamp
        # As runtime/session.py freezes it: where the run ran, and the script epoch it replayed.
        r = TestRun(test_id=t.id, outcome=outcome, executor_id="ios_agent",
                    spec_snapshot={"proof": proof_stamp(t), "script_epoch": 0})
        db.add(r); await db.commit()
        return p.id, t.id, r.id


async def _graduate(run_id):
    recs, clauses = cart_run()
    async with get_session() as db:
        run = await db.get(TestRun, run_id)
        out = await graduate(db, run, recs, clauses)
        await db.commit()
        scripts = (await db.execute(select(TestScript).where(TestScript.test_id == run.test_id)
                                    .order_by(TestScript.version))).scalars().all()
    return out, scripts


def test_test_setting_overrides_project_default():
    p, t = Project(org_id="o", name="P", settings={"graduation": {"fast_mode": "off"}}), Test(
        project_id=None, name="T", instructions="x")
    assert fast_mode_setting(p, t) == "off"
    t.graduation = {"fast_mode": "auto"}
    assert fast_mode_setting(p, t) == "auto"
    assert fast_mode_setting(Project(org_id="o", name="P"), Test(project_id=None, name="T", instructions="x")) == "auto"


async def test_a_passing_run_becomes_a_probation_script():
    _, _, rid = await _setup()
    out, scripts = await _graduate(rid)
    assert out["ready"] and out["summary"] == "1 of 1 checks by code, 0 by an AI look on the screen"
    assert [(s.version, s.status) for s in scripts] == [(1, "probation")]
    assert scripts[0].body["schema"] == 1 and scripts[0].body["checks"][0]["text"] == "$35"


async def test_a_newer_pass_supersedes_the_old_probation_script():
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        r2 = TestRun(test_id=tid, outcome=Outcome.PASSED, executor_id="ios_agent")
        db.add(r2); await db.commit(); rid2 = r2.id
    _, scripts = await _graduate(rid2)
    assert [(s.version, s.status) for s in scripts] == [(1, "superseded"), (2, "probation")]


async def test_fast_mode_off_records_why_and_stores_nothing():
    _, _, rid = await _setup(test_graduation={"fast_mode": "off"})
    out, scripts = await _graduate(rid)
    assert out == {"ready": False, "reasons": ["Fast mode is off for this test."]} and scripts == []


async def test_a_failed_run_never_graduates():
    _, _, rid = await _setup(outcome=Outcome.FAILED)
    out, scripts = await _graduate(rid)
    assert not out["ready"] and scripts == []


async def test_agreeing_probation_runs_make_a_script_active_and_disagreement_retires(monkeypatch):
    from tamam.config import get_settings
    from tamam.graduation.lifecycle import after_script_run
    monkeypatch.setattr(get_settings(), "graduation_promote_after", 2)
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        script = (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first()
        sid = str(script.id)
        run = await db.get(TestRun, rid)
        ok = {"mode": "probation", "ai_passed": True, "script_id": sid, "replay": {"verdict": "passed"}}
        first = await after_script_run(db, run, ok)
        second = await after_script_run(db, run, ok)
        await db.commit()
        assert first["script_status"] == "probation" and second.get("promoted")
        assert (await db.get(TestScript, script.id)).status == "active"
        run.outcome = Outcome.FAILED          # replay passed, the full AI run failed
        out = await after_script_run(db, run, {"mode": "probation", "ai_passed": False, "script_id": sid,
                                               "replay": {"verdict": "passed"}})
        assert out["script_status"] == "retired"


async def test_an_edited_test_retires_its_script(monkeypatch):
    from tamam.config import get_settings
    from tamam.graduation.lifecycle import current_script
    monkeypatch.setattr(get_settings(), "graduation_replay", True)
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        test = await db.get(Test, tid)
        assert (await current_script(db, test)) is not None
        test.instructions = CART + " and check out"
        db.add(test); await db.commit()
        assert (await current_script(db, test)) is None
        assert (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first().status == "retired"


async def test_fast_mode_off_means_no_replay():
    from tamam.graduation.lifecycle import current_script
    _, tid, rid = await _setup(test_graduation={"fast_mode": "auto"})
    await _graduate(rid)
    async with get_session() as db:
        test = await db.get(Test, tid)
        test.graduation = {"fast_mode": "off"}
        assert (await current_script(db, test)) is None


def test_agreement_needs_both_verdicts():
    from tamam.graduation.lifecycle import probation_agreement
    assert probation_agreement("passed", True, Outcome.PASSED) is True
    assert probation_agreement("passed", True, Outcome.FAILED) is False      # checks overturned the agent
    assert probation_agreement("miss", True, Outcome.PASSED) is None          # a miss is no evidence
    assert probation_agreement("passed", None, Outcome.ERROR) is None         # AI run had trouble


async def test_replay_is_off_unless_turned_on():
    from tamam.graduation.lifecycle import current_script
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        assert (await current_script(db, await db.get(Test, tid))) is None


async def test_an_active_script_is_retired_after_repeated_misses(monkeypatch):
    from tamam.graduation.lifecycle import RETIRE_AFTER_MISSES, after_script_run
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        script = (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first()
        script.status = "active"
        run = await db.get(TestRun, rid)
        miss = {"mode": "fallback", "script_id": str(script.id), "replay": {"verdict": "miss"}}
        for _ in range(RETIRE_AFTER_MISSES):
            out = await after_script_run(db, run, miss)
        assert out["script_status"] == "retired"


async def test_a_script_needing_ai_looks_never_runs_alone(monkeypatch):
    from tamam.config import get_settings
    from tamam.graduation.lifecycle import after_script_run
    monkeypatch.setattr(get_settings(), "graduation_promote_after", 1)
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        script = (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first()
        body = dict(script.body)
        body["coverage"] = [dict(c, checked_by_code=False) for c in body["coverage"]]
        script.body = body
        run = await db.get(TestRun, rid)
        out = await after_script_run(db, run, {"mode": "probation", "ai_passed": True,
                                               "script_id": str(script.id), "replay": {"verdict": "passed"}})
        assert "stays_in_probation" in out and script.status == "probation"


async def test_a_disputed_run_is_no_evidence_for_promotion():
    from tamam.graduation.lifecycle import after_script_run
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        script = (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first()
        run = await db.get(TestRun, rid)
        run.disputed = True
        out = await after_script_run(db, run, {"mode": "probation", "ai_passed": True,
                                               "script_id": str(script.id), "replay": {"verdict": "passed"}})
        assert out["agree"] is None and out["agree_in_a_row"] == 0


async def test_a_new_app_build_sends_an_active_script_back_to_probation(monkeypatch):
    import uuid as _uuid

    from tamam.config import get_settings
    from tamam.graduation.lifecycle import current_script
    monkeypatch.setattr(get_settings(), "graduation_replay", True)
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        script = (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first()
        script.status = "active"
        script.body = {**script.body, "app": {**script.body["app"], "build": str(_uuid.uuid4())}}
        await db.commit()
        test = await db.get(Test, tid)
        got = await current_script(db, test)
        assert got.status == "probation" and got.stats["demoted"] == "new app build"
        assert got.epoch == 1


async def test_a_hand_off_run_never_compiles_a_script_from_its_partial_trace(monkeypatch):
    import tamam.runtime.session as sess
    from tamam.executors.base import ExecutionResult
    _, tid, rid = await _setup()
    recs, clauses = cart_run()
    called = []

    async def fake_graduate(*a, **k):
        called.append(True)
        return {"ready": True}
    monkeypatch.setattr(sess, "graduate", fake_graduate)
    ev = ExecutionResult(clauses=clauses, usage_extra={"step_trace": recs, "graduation": {
        "mode": "handoff", "replay": {"verdict": "miss"}, "script_id": None}})
    await sess._graduate(rid, recs, ev)
    assert called == []


def test_risky_actions_setting_test_overrides_project():
    from tamam.graduation.lifecycle import risky_actions_allowed
    p = Project(org_id="o", name="P", settings={"graduation": {"allow_risky_actions": True}})
    t = Test(project_id=None, name="T", instructions="x")
    assert risky_actions_allowed(p, t) is True
    t.graduation = {"allow_risky_actions": False}
    assert risky_actions_allowed(p, t) is False
    assert risky_actions_allowed(Project(org_id="o", name="P"), Test(project_id=None, name="T", instructions="x")) is False


async def test_a_stored_risky_script_never_replays_unless_allowed(monkeypatch):
    """A script compiled before the risk rule (or while risky actions were allowed) is
    re-checked every time it would run: turning the setting off stops it at once."""
    from tamam.config import get_settings
    from tamam.graduation.lifecycle import current_script
    monkeypatch.setattr(get_settings(), "graduation_replay", True)
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        script = (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first()
        body = dict(script.body)
        body["steps"] = [*body["steps"], {"id": "sx", "do": "tap", "say": 'Tap "Pay"', "source_step": 9,
                                          "target": {"type": "Button", "label": "Pay", "locators": []}}]
        script.body = body
        db.add(script); await db.commit()
        test = await db.get(Test, tid)
        assert (await current_script(db, test)) is None
        test.graduation = {"allow_risky_actions": True}
        db.add(test); await db.commit()
        assert (await current_script(db, test)) is not None


async def test_a_script_proven_on_the_simulator_is_on_probation_on_device_farm(monkeypatch):
    """screens differ across targets (onboarding on a fresh farm device, slower reads)."""
    from tamam.config import get_settings
    from tamam.graduation.lifecycle import current_script, run_target
    monkeypatch.setattr(get_settings(), "graduation_replay", True)
    monkeypatch.setattr(get_settings(), "ios_cloud", "aws_device_farm")
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        script = (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first()
        script.status = "active"
        await db.commit()
        assert script.body["app"]["target"] == "simulator"      # stamped by the run that compiled it
        test = await db.get(Test, tid)
        assert run_target(test) == get_settings().ios_backend
        test.ios_target = "cloud"
        assert run_target(test) == "cloud:aws_device_farm"
        got = await current_script(db, test)
        assert got.status == "probation"
        assert got.stats["demoted"] == "a different device target (simulator → cloud:aws_device_farm)"
        assert got.body["app"]["target"] == "cloud:aws_device_farm"   # now proving itself THERE


async def test_a_script_promoted_again_after_a_new_build_stays_active(monkeypatch):
    """Without re-stamping the build, a re-promoted script was demoted again on every run."""
    import uuid as _uuid

    from tamam.config import get_settings
    from tamam.graduation.lifecycle import current_script
    monkeypatch.setattr(get_settings(), "graduation_replay", True)
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        script = (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first()
        script.status = "active"
        script.body = {**script.body, "app": {**script.body["app"], "build": str(_uuid.uuid4())}}
        await db.commit()
        test = await db.get(Test, tid)
        got = await current_script(db, test)
        assert got.status == "probation"
        got.status = "active"            # earned trust on the new build
        db.add(got); await db.commit()
        again = await current_script(db, test)
        assert again.status == "active"


async def _active_script(tid, **app):
    async with get_session() as db:
        script = (await db.execute(select(TestScript).where(TestScript.test_id == tid))).scalars().first()
        script.status = "active"
        if app:
            script.body = {**script.body, "app": app}
        await db.commit()
        return script.id


async def test_a_run_from_before_a_re_stamp_never_counts_toward_promotion(monkeypatch):
    """Review a run already in flight on the old target must not promote the script on
    the new one — it ran on the old epoch."""
    from tamam.config import get_settings
    from tamam.graduation.lifecycle import after_script_run, current_script
    monkeypatch.setattr(get_settings(), "graduation_replay", True)
    monkeypatch.setattr(get_settings(), "graduation_promote_after", 1)
    _, tid, rid = await _setup()
    await _graduate(rid)
    async with get_session() as db:
        test = await db.get(Test, tid)
        test.device = "iPhone SE"                  # the next run targets another device
        db.add(test); await db.commit()
        script = await current_script(db, test)    # re-stamped → epoch 1
        await db.commit()
        assert script.epoch == 1
        old_run = await db.get(TestRun, rid)       # prepared on epoch 0
        out = await after_script_run(db, old_run, {"mode": "probation", "ai_passed": True,
                                                   "script_id": str(script.id), "replay": {"verdict": "passed"}})
        assert out.get("agree_elsewhere") and not out.get("promoted") and out["script_status"] == "probation"


async def test_a_script_with_no_stamp_is_never_trusted_to_run_alone(monkeypatch):
    """Scripts compiled before stamps existed (or by a run with no frozen stamp) re-prove themselves."""
    from tamam.config import get_settings
    from tamam.graduation.lifecycle import current_script
    monkeypatch.setattr(get_settings(), "graduation_replay", True)
    _, tid, rid = await _setup()
    await _graduate(rid)
    await _active_script(tid, build_id=None)          # a legacy stamp
    async with get_session() as db:
        got = await current_script(db, await db.get(Test, tid))
        assert got.status == "probation" and got.stats["demoted"] == "not yet proven on this build and device"


async def test_a_different_device_model_sends_an_active_script_to_probation(monkeypatch):
    from tamam.config import get_settings
    from tamam.graduation.lifecycle import current_script
    monkeypatch.setattr(get_settings(), "graduation_replay", True)
    _, tid, rid = await _setup()
    await _graduate(rid)
    await _active_script(tid)
    async with get_session() as db:
        test = await db.get(Test, tid)
        assert (await current_script(db, test)).status == "active"      # same place: stays active
        test.device = "iPhone SE"
        got = await current_script(db, test)
        assert got.status == "probation" and got.stats["demoted"].startswith("a different device")


async def test_a_demoted_script_gives_way_to_a_newer_candidate(monkeypatch):
    """Review no orphan stuck in probation — the newer candidate proves itself instead."""
    import uuid as _uuid

    from tamam.config import get_settings
    from tamam.graduation.lifecycle import current_script
    monkeypatch.setattr(get_settings(), "graduation_replay", True)
    _, tid, rid = await _setup()
    await _graduate(rid)
    old_id = await _active_script(tid)
    async with get_session() as db:
        old = await db.get(TestScript, old_id)
        db.add(TestScript(test_id=tid, version=old.version + 1, status="probation", body=old.body,
                          test_text_hash=old.test_text_hash))
        old.body = {**old.body, "app": {**old.body["app"], "build": str(_uuid.uuid4())}}
        await db.commit()
        got = await current_script(db, await db.get(Test, tid))
        assert got.version == old.version + 1
        assert (await db.get(TestScript, old_id)).status == "superseded"
