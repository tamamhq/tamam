"""Build bookkeeping the runtime does for executors (they never touch the database)."""

from __future__ import annotations

import uuid

from tamam.db import get_session
from tamam.models import Build


async def record_vendor_ref(build_id: str, vendor: str, app_ref: str) -> None:
    """Record a device-farm upload on its Build, so deleting the build deletes the farm's copy
    (and a worker restart doesn't upload it again)."""
    async with get_session() as db:
        build = await db.get(Build, uuid.UUID(str(build_id)))
        if build is None:
            return
        refs = dict(build.vendor_refs or {})
        if refs.get(vendor) == app_ref:
            return
        refs[vendor] = app_ref
        build.vendor_refs = refs
        db.add(build)
        await db.commit()
