"""Eval store — the first reliability metric: judge vs. self-verdict agreement.

Disagreement (disputed runs) is the signal that either the executor's
self-assessment or the judge is drifting. Trend this over time before touching
any prompt (code-before-prompt: measure first).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel
from sqlmodel import select

from tamam.db import get_session
from tamam.models import Lifecycle, Test, TestRun


class AgreementStats(BaseModel):
    total_finished: int
    with_both_verdicts: int
    disputed: int

    @property
    def agreement_rate(self) -> float | None:
        if self.with_both_verdicts == 0:
            return None
        return 1 - (self.disputed / self.with_both_verdicts)


async def agreement_stats(project_id: uuid.UUID | None = None) -> AgreementStats:
    async with get_session() as db:
        query = select(TestRun).where(TestRun.lifecycle == Lifecycle.FINISHED)
        if project_id is not None:
            query = query.join(Test, TestRun.test_id == Test.id).where(  # type: ignore[arg-type]
                Test.project_id == project_id
            )
        runs = (await db.execute(query)).scalars().all()

    both = [r for r in runs if r.judge_verdict is not None and r.self_verdict is not None]
    return AgreementStats(
        total_finished=len(runs),
        with_both_verdicts=len(both),
        disputed=sum(1 for r in both if r.disputed),
    )
