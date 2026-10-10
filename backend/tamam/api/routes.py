from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import update as sa_update
from sqlmodel import select
from sse_starlette.sse import EventSourceResponse

from tamam.auth import Principal, PrincipalDep
from tamam.config import get_settings
from tamam.db import get_session
from tamam.dispatch import enqueue_run
from tamam.harness.checks import Check
from tamam.runtime.events import get_bus
from tamam.models import Build, Lifecycle, Project, RunStepEvent, Test, TestRun, TestScript
from tamam.storage import LocalArtifactStorage, get_storage


def _artifact_access_url(key: str | None) -> str | None:
    """Turn a stored artifact key into a short-lived, signed browser URL. Remote
    URLs (legacy pass-through) are returned as-is. Callers MUST have already
    authorized the request (owns the run/build) before minting a URL."""
    if not key:
        return None
    if key.startswith(("http://", "https://")):
        return key
    return get_storage().signed_url(key)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── tenant ownership helpers: every resource is reached only via the caller's org.
# A cross-tenant id looks exactly like a missing one (404) — we never reveal that
# another org's resource exists.

async def _owned_project(db, project_id: uuid.UUID, p: Principal) -> Project:
    project = await db.get(Project, project_id)
    if project is None or project.org_id != p.org_id:
        raise HTTPException(404, "Project not found")
    return project


async def _owned_test(db, test_id: uuid.UUID, p: Principal) -> Test:
    test = await db.get(Test, test_id)
    if test is not None:
        await _owned_project(db, test.project_id, p)  # 404s if wrong org
    if test is None:
        raise HTTPException(404, "Test not found")
    return test


async def _owned_build(db, build_id: uuid.UUID, p: Principal) -> Build:
    build = await db.get(Build, build_id)
    if build is not None:
        await _owned_project(db, build.project_id, p)
    if build is None:
        raise HTTPException(404, "Build not found")
    return build


async def _owned_run(db, run_id: uuid.UUID, p: Principal) -> TestRun:
    run = await db.get(TestRun, run_id)
    if run is not None:
        await _owned_test(db, run.test_id, p)
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


# ---------- Form schema ----------

@router.get("/executors")
async def executors(_: Principal = PrincipalDep) -> list[str]:
    """Executors the test form can offer (replay is chosen by the harness, not offered)."""
    from tamam.executors.registry import user_executors

    return user_executors()


@router.get("/ios/devices")
async def ios_devices(_: Principal = PrincipalDep) -> list[dict]:
    """Real cloud iOS devices ({device, versions[]}, newest-OS-first) for the test
    form's device/OS picker — so a test can only target a device+OS the farm has."""
    from tamam.executors.devices import list_devices

    return await list_devices("ios")


@router.get("/check-kinds")
async def check_kinds(_: Principal = PrincipalDep) -> list:
    """Descriptors for the checks builder. The FE renders these verbatim and knows
    nothing about check semantics. Harness internals (executors, prompt versions)
    are deliberately NOT exposed — they are ops concerns, not UI concerns."""
    from tamam.harness.checks import check_descriptors

    return check_descriptors()


# ---------- Projects ----------

class ProjectIn(BaseModel):
    name: str
    description: str = ""
    base_url: str = ""


@router.post("/projects", status_code=201)
async def create_project(body: ProjectIn, p: Principal = PrincipalDep) -> Project:
    async with get_session() as db:
        project = Project(org_id=p.org_id, **body.model_dump())  # stamped with the caller's org
        db.add(project)
        await db.commit()
        await db.refresh(project)
        return project


@router.get("/projects")
async def list_projects(p: Principal = PrincipalDep) -> list[Project]:
    async with get_session() as db:
        rows = await db.execute(select(Project).where(Project.org_id == p.org_id))
        return list(rows.scalars().all())


@router.get("/projects/{project_id}")
async def get_project(project_id: uuid.UUID, p: Principal = PrincipalDep) -> Project:
    async with get_session() as db:
        return await _owned_project(db, project_id, p)


# ---------- Builds ----------

class BuildIn(BaseModel):
    name: str
    app_path: str            # local .ipa/.app path, or a vendor/URL reference
    bundle_id: str | None = None


def _validate_build_ref(project_id: uuid.UUID, app_path: str) -> None:
    """Guard the JSON build-registration path against referencing a binary that isn't the
    caller's own upload. `app_path` is later resolved by the provider either as a storage
    KEY (fetched from the shared object store) or, in dev, as a local filesystem path — so
    an unvalidated value is a cross-tenant / arbitrary-file read primitive (a customer's
    pre-release build is the crown jewel). Rules:
      • a storage key MUST be scoped to THIS project: `builds/{project_id}/…`
      • any other `builds/…` key is another project's binary → reject
      • a bare filesystem path (eval fixtures, local simulator) is DEV-ONLY
      • prod uploads should use the multipart /builds/upload endpoint, which mints a
        correctly-scoped key
    """
    ref = (app_path or "").strip()
    if not ref:
        raise HTTPException(400, "app_path is required")
    if ref.startswith(f"builds/{project_id}/"):
        return  # our own upload key for this project
    if ref.startswith("builds/"):
        raise HTTPException(
            400, "app_path references another project's build; upload the binary to THIS "
                 "project via /builds/upload")
    # Not a storage key → a local filesystem path or other raw reference. Allowed only in
    # local dev (eval fixtures / local simulator); never resolvable in multi-tenant prod.
    if not get_settings().auth_dev_mode:
        raise HTTPException(
            400, "app_path must be a build key for this project; register binaries via "
                 "/builds/upload")


@router.post("/projects/{project_id}/builds", status_code=201)
async def create_build(project_id: uuid.UUID, body: BuildIn, p: Principal = PrincipalDep) -> Build:
    _validate_build_ref(project_id, body.app_path)
    async with get_session() as db:
        await _owned_project(db, project_id, p)
        build = Build(project_id=project_id, **body.model_dump())
        db.add(build)
        await db.commit()
        await db.refresh(build)
        return build


# iOS builds are .ipa archives (a .app bundle zipped up); accept zip variants too.
_ALLOWED_BUILD_EXTS = {".ipa", ".zip", ".app"}


@router.post("/projects/{project_id}/builds/upload", status_code=201)
async def upload_build(
    project_id: uuid.UUID,
    file: UploadFile = File(...),
    name: str = Form(...),
    bundle_id: str | None = Form(None),
    p: Principal = PrincipalDep,
) -> Build:
    """Upload an .ipa/.app once, at the project level, and reuse it across tests."""
    settings = get_settings()
    # Sanitize the client filename to its basename — never trust it for a path.
    safe_name = Path(file.filename or "app.ipa").name or "app.ipa"
    ext = Path(safe_name).suffix.lower()
    # Allow-list the extensions we accept — reject anything else up front.
    if ext not in _ALLOWED_BUILD_EXTS:
        raise HTTPException(
            400,
            f"Unsupported file type '{ext or '?'}'. Allowed: {', '.join(sorted(_ALLOWED_BUILD_EXTS))}",
        )
    max_bytes = settings.max_upload_mb * 1024 * 1024

    async with get_session() as db:
        await _owned_project(db, project_id, p)
        # Stage to a local temp file while we stream + validate, then hand off to
        # storage. The DB stores the KEY; the binary lives in the (private) store
        # so the separate worker can fetch it for the device farm.
        stage_dir = settings.artifacts_dir / "_staging"
        stage_dir.mkdir(parents=True, exist_ok=True)
        stage = stage_dir / f"{uuid.uuid4()}-{safe_name}"
        key = f"builds/{project_id}/{uuid.uuid4()}-{safe_name}"
        size = 0
        first = True
        try:
            with open(stage, "wb") as out:
                while chunk := await file.read(1 << 20):
                    if first:
                        first = False
                        # .ipa/.zip are ZIP archives — verify the magic bytes so a
                        # renamed arbitrary file can't masquerade as a build.
                        if ext in {".ipa", ".zip"} and not chunk.startswith(b"PK"):
                            raise HTTPException(400, "File is not a valid .ipa/.zip archive")
                    size += len(chunk)
                    if size > max_bytes:
                        raise HTTPException(
                            413, f"File exceeds the {settings.max_upload_mb} MB limit"
                        )
                    out.write(chunk)
            if size == 0:
                raise HTTPException(400, "Uploaded file is empty")
            await get_storage().put(stage, key)
        except HTTPException:
            raise
        finally:
            stage.unlink(missing_ok=True)  # never leave the staged temp behind
        build = Build(project_id=project_id, name=name, app_path=key, bundle_id=bundle_id)
        db.add(build)
        await db.commit()
        await db.refresh(build)
        return build


@router.get("/projects/{project_id}/builds")
async def list_builds(project_id: uuid.UUID, p: Principal = PrincipalDep) -> list[Build]:
    async with get_session() as db:
        await _owned_project(db, project_id, p)
        rows = await db.execute(select(Build).where(Build.project_id == project_id))
        return list(rows.scalars().all())


@router.delete("/builds/{build_id}", status_code=204)
async def delete_build(build_id: uuid.UUID, p: Principal = PrincipalDep) -> None:
    """Delete a build and PURGE THE BINARY EVERYWHERE we put it — the private
    object store and every device-farm vendor it was uploaded to. This is the
    right-to-deletion path for a customer's pre-release IP: 'delete' must mean the
    binary is gone, not just the DB row."""
    from sqlalchemy import delete

    async with get_session() as db:
        build = await _owned_build(db, build_id, p)  # 404 if not owned
        app_path = build.app_path
        vendor_refs = dict(build.vendor_refs or {})

    # 1) Purge the binary from our own private storage. Only keys WE minted
    #    ("builds/...") — never a legacy user-supplied path or a vendor URL. Await
    #    it (not best-effort): if our durable copy can't be removed we must surface
    #    that rather than claim deletion, so the caller can retry.
    if app_path and app_path.startswith("builds/"):
        await get_storage().delete(app_path)

    # 2) Purge every vendor copy we recorded. Best-effort per vendor: a farm API
    #    failure must not block deleting the rest, but we log exactly which copy
    #    could not be purged so it can be handled manually.
    from tamam.executors.devices import delete_uploaded_app

    for vendor, ref in vendor_refs.items():
        try:
            await delete_uploaded_app(vendor, ref)
        except Exception:
            logger.warning("could not delete build %s from vendor %s (ref %s)",
                           build_id, vendor, ref, exc_info=True)

    # 3) Now drop the DB rows.
    async with get_session() as db:
        await db.execute(sa_update(Test).where(Test.build_id == build_id).values(build_id=None))
        await db.execute(delete(Build).where(Build.id == build_id))
        await db.commit()


# ---------- Tests ----------

class TestIn(BaseModel):
    name: str
    instructions: str
    start_url: str = ""  # optional — unused for mobile tests
    checks: list[Check] = Field(default_factory=list)
    checks_exhaustive: bool = False
    executor_id: str | None = None
    # Mobile target (ios_agent)
    build_id: uuid.UUID | None = None  # the app under test (a Project build)
    app_build_ref: str | None = None   # legacy/override
    app_bundle_id: str | None = None
    device: str | None = None
    ios_platform_version: str | None = None  # cloud OS version, e.g. "26"
    ios_target: Literal["simulator", "cloud", "device"] | None = None
    reuse_session: bool = False        # persist app login across runs; default clean


@router.post("/projects/{project_id}/tests", status_code=201)
async def create_test(project_id: uuid.UUID, body: TestIn, p: Principal = PrincipalDep) -> Test:
    async with get_session() as db:
        await _owned_project(db, project_id, p)
        if body.build_id is not None:
            await _owned_build(db, body.build_id, p)  # can't reference another org's build
        test = Test(
            project_id=project_id,
            **body.model_dump(exclude={"checks"}),
            checks=[c.model_dump() for c in body.checks],
        )
        db.add(test)
        await db.commit()
        await db.refresh(test)
        return test


@router.get("/projects/{project_id}/tests")
async def list_tests(project_id: uuid.UUID, p: Principal = PrincipalDep) -> list[Test]:
    async with get_session() as db:
        await _owned_project(db, project_id, p)
        rows = await db.execute(select(Test).where(Test.project_id == project_id))
        return list(rows.scalars().all())


@router.get("/tests/{test_id}")
async def get_test(test_id: uuid.UUID, p: Principal = PrincipalDep) -> Test:
    async with get_session() as db:
        return await _owned_test(db, test_id, p)


class GraduationSettings(BaseModel):
    fast_mode: Literal["auto", "off"] | None = None   # None = inherit the project's
    allow_risky_actions: bool | None = None           # replay a test that deletes / pays / sends


class TestPatch(BaseModel):
    graduation: GraduationSettings | None = None
    name: str | None = None
    instructions: str | None = None
    start_url: str | None = None
    checks_exhaustive: bool | None = None
    executor_id: str | None = None
    build_id: uuid.UUID | None = None
    app_build_ref: str | None = None
    app_bundle_id: str | None = None
    device: str | None = None
    ios_platform_version: str | None = None
    ios_target: Literal["simulator", "cloud", "device"] | None = None
    reuse_session: bool | None = None


@router.patch("/tests/{test_id}")
async def update_test(test_id: uuid.UUID, body: TestPatch, p: Principal = PrincipalDep) -> Test:
    async with get_session() as db:
        test = await _owned_test(db, test_id, p)
        if body.build_id is not None:
            await _owned_build(db, body.build_id, p)
        for field, value in body.model_dump(exclude_unset=True).items():
            if field == "graduation":
                # Merge: changing one key must not drop the others (a dropped test-level
                # allow_risky_actions=false would silently inherit the project's true).
                merged = {**(test.graduation or {}), **(value or {})}
                value = {k: v for k, v in merged.items() if v is not None} or None
            setattr(test, field, value)
        db.add(test)
        await db.commit()
        await db.refresh(test)
        return test


@router.get("/tests/{test_id}/script")
async def get_test_script(test_id: uuid.UUID, p: Principal = PrincipalDep) -> dict:
    """The test's current fast-replay script (newest probation or active), if any."""
    async with get_session() as db:
        await _owned_test(db, test_id, p)
        script = (await db.execute(
            select(TestScript).where(TestScript.test_id == test_id,
                                     TestScript.status.in_(("probation", "active")))
            .order_by(TestScript.version.desc()))).scalars().first()
    if script is None:
        return {"script": None}
    return {"script": {"id": str(script.id), "version": script.version, "status": script.status,
                       "source_run_id": str(script.source_run_id) if script.source_run_id else None,
                       "created_at": script.created_at, "body": script.body}}


@router.delete("/tests/{test_id}", status_code=204)
async def delete_test(test_id: uuid.UUID, p: Principal = PrincipalDep) -> None:
    from sqlalchemy import delete

    async with get_session() as db:
        await _owned_test(db, test_id, p)  # 404 if not owned
        # The runs' stored files (screens, video, step trace) go with the test: an app's
        # screens are customer IP and a trace is the record of what was typed.
        keys = await _run_storage_keys(db, test_id)
        # Delete children before the test to satisfy FKs. Core deletes execute in
        # statement order (ORM flush ordering can't reorder them).
        run_ids = select(TestRun.id).where(TestRun.test_id == test_id).scalar_subquery()
        await db.execute(delete(RunStepEvent).where(RunStepEvent.run_id.in_(run_ids)))
        await db.execute(delete(TestRun).where(TestRun.test_id == test_id))
        await db.execute(delete(TestScript).where(TestScript.test_id == test_id))
        await db.execute(delete(Test).where(Test.id == test_id))
        await db.commit()
    await _delete_stored(keys)


async def _run_storage_keys(db, test_id: uuid.UUID) -> list[str]:
    """Every storage KEY the test's runs hold (URLs to external hosts are not ours)."""
    runs = (await db.execute(select(TestRun).where(TestRun.test_id == test_id))).scalars().all()
    keys = [k for r in runs for k in (r.video_url, r.trace_key)]
    if runs:
        evs = (await db.execute(select(RunStepEvent).where(
            RunStepEvent.run_id.in_([r.id for r in runs])))).scalars().all()
        keys += [k for e in evs for k in (e.screenshot_url, e.annotated_url)]
    return sorted({k for k in keys if k and not k.startswith(("http://", "https://"))})


async def _delete_stored(keys: list[str]) -> None:
    """Best effort: the rows are already gone; a storage hiccup is logged, not raised."""
    storage = get_storage()
    for k in keys:
        try:
            await storage.delete(k)
        except Exception:
            logging.getLogger(__name__).warning("could not delete stored %s", k, exc_info=True)


# ---------- Runs ----------

class RunIn(BaseModel):
    max_steps: int | None = None
    max_wall_clock_s: int | None = None


@router.post("/tests/{test_id}/runs", status_code=202)
async def create_run(
    test_id: uuid.UUID, body: RunIn | None = None, p: Principal = PrincipalDep
) -> TestRun:
    settings = get_settings()
    body = body or RunIn()
    async with get_session() as db:
        await _owned_test(db, test_id, p)
        run = TestRun(
            test_id=test_id,
            spec_snapshot={
                "max_steps": body.max_steps or settings.default_max_steps,
                "max_wall_clock_s": body.max_wall_clock_s or settings.default_max_wall_clock_s,
            },
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
    await enqueue_run(str(run.id))
    return run


@router.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, p: Principal = PrincipalDep) -> dict:
    async with get_session() as db:
        run = await _owned_run(db, run_id, p)  # 404 unless this org owns the run
        steps = (
            await db.execute(
                select(RunStepEvent)
                .where(RunStepEvent.run_id == run_id)
                .order_by(RunStepEvent.step)
            )
        ).scalars().all()
        # Ownership confirmed above → safe to mint signed artifact URLs. We store
        # the KEY in the DB and only ever hand the browser a short-lived signed URL.
        run_out = run.model_dump()
        run_out["video_url"] = _artifact_access_url(run.video_url)
        steps_out = []
        for s in steps:
            d = s.model_dump()
            # Ownership already confirmed above → safe to mint short-lived signed URLs
            # for BOTH the raw screen and the annotated view the model saw.
            d["screenshot_url"] = _artifact_access_url(s.screenshot_url)
            d["annotated_url"] = _artifact_access_url(s.annotated_url)
            steps_out.append(d)
        return {"run": run_out, "steps": steps_out}


@router.get("/artifacts/{key:path}")
async def get_artifact(
    key: str, exp: int = Query(...), sig: str = Query(...)
) -> FileResponse:
    """Serve a LOCAL-storage artifact, gated by the short-lived HMAC token minted
    in an authorized endpoint. No Clerk bearer here on purpose: <img>/<video> tags
    can't send headers, so access is proven by the unforgeable, time-limited
    signature instead. In S3 mode the browser fetches presigned URLs directly and
    never hits this route."""
    store = get_storage()
    if not isinstance(store, LocalArtifactStorage) or not store.verify(key, exp, sig):
        raise HTTPException(404)
    path = store.local_path(key)
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@router.get("/projects/{project_id}/runs")
async def list_runs(
    project_id: uuid.UUID, disputed: bool | None = None, p: Principal = PrincipalDep
) -> list[TestRun]:
    async with get_session() as db:
        await _owned_project(db, project_id, p)
        query = (
            select(TestRun)
            .join(Test, TestRun.test_id == Test.id)  # type: ignore[arg-type]
            .where(Test.project_id == project_id)
            .order_by(TestRun.created_at.desc())  # type: ignore[attr-defined]
        )
        if disputed is not None:
            query = query.where(TestRun.disputed == disputed)
        return list((await db.execute(query)).scalars().all())


@router.get("/tests/{test_id}/runs")
async def list_test_runs(test_id: uuid.UUID, p: Principal = PrincipalDep) -> list[TestRun]:
    """A single test's run history — the test detail page reads this for its metrics."""
    async with get_session() as db:
        await _owned_test(db, test_id, p)
        rows = await db.execute(
            select(TestRun)
            .where(TestRun.test_id == test_id)
            .order_by(TestRun.created_at.desc())  # type: ignore[attr-defined]
        )
        return list(rows.scalars().all())


@router.get("/runs/{run_id}/events")
async def run_events(run_id: uuid.UUID, token: str | None = None):
    """SSE stream of step events + lifecycle changes for a live run.

    Browser EventSource can't send an Authorization header, so the (short-lived)
    token is passed as `?token=` and verified the same way. Ownership is enforced:
    you only stream runs in your own org."""
    settings = get_settings()
    if settings.clerk_issuer:
        from tamam.auth import verify_token

        if not token:
            raise HTTPException(401, "Missing token")
        principal = verify_token(token, settings)
    elif settings.auth_dev_mode:
        principal = Principal(user_id="user_dev", org_id=settings.auth_dev_org_id)
    else:
        raise HTTPException(401, "Authentication is not configured")

    async with get_session() as db:
        run = await _owned_run(db, run_id, principal)
        finished = run.lifecycle == Lifecycle.FINISHED

    async def stream():
        # replay persisted steps first so late subscribers see the full history
        async with get_session() as db:
            steps = (
                await db.execute(
                    select(RunStepEvent)
                    .where(RunStepEvent.run_id == run_id)
                    .order_by(RunStepEvent.step)
                )
            ).scalars().all()
            for s in steps:
                yield {"event": "step", "data": s.model_dump_json()}
        if finished:
            yield {"event": "lifecycle", "data": '{"lifecycle": "finished"}'}
            return
        async for payload in get_bus().subscribe(str(run_id)):
            kind = payload.get("type", "step")
            import json

            yield {"event": kind, "data": json.dumps(payload, default=str)}

    return EventSourceResponse(stream())
