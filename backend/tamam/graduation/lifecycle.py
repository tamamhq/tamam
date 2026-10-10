"""After a run: decide whether it becomes the test's fast replay, store the script, and record
the decision on the run (the user always sees it — "Fast mode ready" or why not).

Settings: the project's {"graduation": {"fast_mode": "auto" | "off"}}, overridden per test by
test.graduation (None = inherit). Default: auto.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import update
from sqlmodel import func, select

from tamam.graduation.compiler import compile_run, instructions_hash, risky_step_reasons
from tamam.graduation.script import CompiledScript
from tamam.models import Outcome, Project, Test, TestRun, TestScript

logger = logging.getLogger(__name__)


def fast_mode_setting(project: Project | None, test: Test | None) -> str:
    own = (test.graduation or {}).get("fast_mode") if test is not None else None
    if own in ("auto", "off"):
        return own
    proj = ((project.settings or {}).get("graduation") or {}).get("fast_mode") if project else None
    return proj if proj in ("auto", "off") else "auto"


def promote_after() -> int:
    """Agreeing probation runs (replay passed AND the full AI run passed) before a script runs
    on its own."""
    from tamam.config import get_settings
    return max(1, get_settings().graduation_promote_after)


RETIRE_AFTER_MISSES = 3   # an active script that keeps missing stops costing a replay per run


def probation_agreement(replay_verdict: str | None, ai_passed: bool | None, outcome) -> bool | None:  # noqa: ANN001
    """Did replay and the full AI run reach the same verdict? None when either side did not
    reach one (a miss, device trouble, an AI error) — that is no evidence about the script.
    The AI side is the run's FINAL outcome (checks or a judge may overturn the agent)."""
    if replay_verdict not in ("passed", "failed") or ai_passed is None:
        return None
    if outcome not in (Outcome.PASSED, Outcome.FAILED):
        return None
    return (replay_verdict == "passed") == (outcome == Outcome.PASSED)


def run_target(test: Test) -> str:
    """Where a run of `test` executes: "simulator", "device", or "cloud:<vendor>". Screens differ
    across targets (a fresh farm device shows onboarding; reads are slower), so a script is only
    trusted on the target it proved itself on."""
    from tamam.config import get_settings
    s = get_settings()
    backend = test.ios_target or s.ios_backend
    return f"cloud:{s.ios_cloud}" if backend == "cloud" else backend


def proof_stamp(test: Test) -> dict:
    """Where a script must have proven itself to replay alone: the app build, the device
    target, the device model and the iOS version. Any difference → probation there."""
    return {"build": str(test.build_id) if test.build_id else (test.app_build_ref or None),
            "target": run_target(test), "device": test.device or None,
            "os": test.ios_platform_version or None}


_STAMP_WORDS = {"build": "new app build", "target": "a different device target",
                "device": "a different device", "os": "a different iOS version"}


def _stamp_change(proven: dict, here: dict) -> str | None:
    """Why `proven` (the script's stamp) is not `here`, or None. A missing or legacy stamp is
    a change: a script never proven anywhere known is not trusted to run alone."""
    if not proven or any(k not in proven for k in here):
        return "not yet proven on this build and device"
    for k, word in _STAMP_WORDS.items():
        if proven.get(k) != here.get(k):
            return word if k == "build" else f"{word} ({proven.get(k) or 'default'} → {here.get(k) or 'default'})"
    return None


def risky_actions_allowed(project: Project | None, test: Test | None) -> bool:
    """Whether a test that deletes / pays / sends may be replayed (test overrides project)."""
    own = (test.graduation or {}).get("allow_risky_actions") if test is not None else None
    if isinstance(own, bool):
        return own
    proj = ((project.settings or {}).get("graduation") or {}).get("allow_risky_actions") if project else None
    return proj is True


async def current_script(db, test: Test) -> TestScript | None:
    """The script a run of `test` should replay: active first, else probation — only while
    fast mode is on and the script was compiled from the test's CURRENT text (an edited test
    retires its script: the old steps answer a different question)."""
    from tamam.config import get_settings
    if not get_settings().graduation_replay:
        return None       # replay is off in this environment (scripts are still compiled)
    project = await db.get(Project, test.project_id)
    if fast_mode_setting(project, test) == "off":
        return None
    for status in ("active", "probation"):
        script = (await db.execute(select(TestScript).where(
            TestScript.test_id == test.id, TestScript.status == status)
            .order_by(TestScript.version.desc()))).scalars().first()
        if script is None:
            continue
        if script.test_text_hash != instructions_hash(test.instructions):
            script.status = "retired"
            db.add(script)
            await db.flush()     # committed with the run's own preparation
            continue
        if not risky_actions_allowed(project, test) and risky_step_reasons(
                CompiledScript.model_validate(script.body or {}).steps):
            # Compiled before the risk rule existed, or risky actions were allowed then and
            # are not now: never replay it.
            continue
        here = proof_stamp(test)
        why = _stamp_change((script.body or {}).get("app") or {}, here)
        if why:
            # It must earn trust again where it will now run. Re-stamp it here and start a new
            # epoch: only runs on this epoch count toward promotion (a run already in flight
            # elsewhere cannot), and once promoted here it STAYS active.
            demote = script.status == "active"
            if demote:
                newer = (await db.execute(select(TestScript).where(
                    TestScript.test_id == test.id, TestScript.status == "probation",
                    TestScript.version > script.version))).scalars().first()
                if newer is not None:
                    # A newer candidate exists; it, not this one, proves itself here.
                    script.status = "superseded"
                    db.add(script)
                    await db.flush()
                    continue
                script.status = "probation"
            script.epoch = (script.epoch or 0) + 1
            script.stats = {**(script.stats or {}), "agree": 0, "agree_epoch": script.epoch,
                            **({"demoted": why} if demote else {})}
            script.body = {**(script.body or {}), "app": here}
            db.add(script)
            await db.flush()
        return script
    return None


async def after_script_run(db, run: TestRun, info: dict) -> dict:
    """Update the replayed script from how the run went; return the decision for the run.
      fast pass / fast fail   → stats only (a fail means the script caught a bug)
      probation, agreeing     → one step closer to active (promote_after() in a row)
      probation, disagreeing  → retired: replay and the AI saw different things
      fallback (replay missed)→ stats; the AI run may compile a replacement (caller)"""
    script = await db.get(TestScript, uuid.UUID(info["script_id"])) if info.get("script_id") else None
    mode = info.get("mode")
    replay = info.get("replay") or {}
    # What the run page shows: how replay went, where it stopped and what it saw instead.
    out = {"mode": mode, "replay": {k: replay.get(k) for k in (
        "verdict", "reason", "seconds", "checks", "at_step", "steps_done", "seen", "ai_looks", "notes")}}
    if info.get("proven_by_replay"):
        out["proven_by_replay"] = info["proven_by_replay"]
    if script is None:
        return out
    out["script_version"] = script.version
    stats = dict(script.stats or {})
    # Agreement counts only for runs on the script's current epoch (the build/target it is
    # proving itself on now); a count left from another epoch starts again from zero.
    same_place = (run.spec_snapshot or {}).get("script_epoch") == script.epoch
    agreed_so_far = stats.get("agree", 0) if stats.get("agree_epoch") == script.epoch else 0
    stats["last_mode"] = mode
    if mode == "fast":
        stats["passes" if replay.get("verdict") == "passed" else "fails"] = \
            stats.get("passes" if replay.get("verdict") == "passed" else "fails", 0) + 1
    elif mode == "probation":
        agree = None if run.disputed else probation_agreement(
            replay.get("verdict"), info.get("ai_passed"), run.outcome)
        out["agree"] = agree
        if agree is True and replay.get("verdict") == "passed" and not same_place:
            out["agree_elsewhere"] = True     # ran on a build/target the script has since left
        elif agree is True and replay.get("verdict") == "passed":
            stats["agree"], stats["agree_epoch"] = agreed_so_far + 1, script.epoch
            can_run_alone = CompiledScript.model_validate(script.body or {}).all_code_checked
            if stats["agree"] >= promote_after() and not can_run_alone:
                out["stays_in_probation"] = ("Some checks need an AI look, so a full AI run keeps "
                                             "deciding this test's verdict.")
            elif stats["agree"] >= promote_after():
                # Conditional: never revive a script another run retired or replaced meanwhile,
                # and never promote it if it was re-stamped elsewhere since this run started.
                done = await db.execute(update(TestScript).where(
                    TestScript.id == script.id, TestScript.status == "probation",
                    TestScript.epoch == script.epoch).values(status="active"))
                if done.rowcount:
                    script.status = "active"
                    stats["agree"] = 0
                    out["promoted"] = True
        elif agree is False:
            stats["disagree"] = stats.get("disagree", 0) + 1
            script.status = "retired"
            out["retired"] = "Fast replay and the full AI run disagreed."
        else:
            stats["agree"] = 0       # agreement must be IN A ROW; a miss or trouble resets it
    elif mode in ("fallback", "handoff"):
        stats["misses"] = stats.get("misses", 0) + 1
        stats["misses_in_a_row"] = stats.get("misses_in_a_row", 0) + 1
        if stats["misses_in_a_row"] >= RETIRE_AFTER_MISSES:
            script.status = "retired"
            out["retired"] = f"Fast replay missed {RETIRE_AFTER_MISSES} runs in a row."
    if mode == "fast":
        stats["misses_in_a_row"] = 0
    script.stats = stats
    db.add(script)
    out["script_status"] = script.status
    out["agree_in_a_row"] = stats.get("agree", 0) if stats.get("agree_epoch") == script.epoch else 0
    out["promote_after"] = promote_after()
    return out


async def graduate(db, run: TestRun, records: list[dict], clauses: list[dict],
                   run_error: str | None = None, replace_active: bool = False) -> dict | None:
    """Compile `run` into a probation script when it is safe; return the decision to record on
    the run. Runs in its OWN session after the verdict is committed (see session._graduate),
    so nothing here can cost a run its verdict. Raises only on a version race, which the
    caller treats as "another run of this test graduated at the same moment"."""
    test = await db.get(Test, run.test_id)
    if test is None:
        return None
    project = await db.get(Project, test.project_id)
    if fast_mode_setting(project, test) == "off":
        return {"ready": False, "reasons": ["Fast mode is off for this test."]}
    # The text this run executed — the test may have been edited while it ran.
    text = (run.spec_snapshot or {}).get("instructions") or test.instructions
    passed = run.outcome == Outcome.PASSED
    decision = compile_run(records, clauses, text, passed=passed,
                           run_error=run_error, source_run_id=str(run.id),
                           allow_risky=risky_actions_allowed(project, test))
    if passed and run.disputed:
        decision.ready = False
        decision.reasons.insert(0, "The agent and the independent judge disagreed on this run.")
    out = decision.as_dict()
    if decision.script is not None:
        # Where this run proved it — frozen when the run started, never re-read from the
        # test (it may have changed while the run was in flight). No stamp → unproven.
        decision.script.app = dict((run.spec_snapshot or {}).get("proof") or {})
    if decision.ready and decision.script is not None:
        latest = (await db.execute(select(func.max(TestScript.version))
                                   .where(TestScript.test_id == test.id))).scalar() or 0
        # A newer probation script replaces an older one; an active script stays until its
        # replacement has earned trust (P2).
        replaced = ("probation", "active") if replace_active else ("probation",)
        for old in (await db.execute(select(TestScript).where(
                TestScript.test_id == test.id, TestScript.status.in_(replaced)))).scalars():
            old.status = "superseded"
            db.add(old)
        script = TestScript(test_id=test.id, version=latest + 1, status="probation",
                            body=decision.script.model_dump(mode="json", by_alias=True),
                            test_text_hash=decision.script.test_text_hash, source_run_id=run.id)
        db.add(script)
        await db.flush()   # unique (test_id, version): a concurrent graduation raises here
        out["script_id"] = str(script.id)
        out["version"] = script.version
    return out


__all__ = ["after_script_run", "current_script", "fast_mode_setting", "graduate"]
