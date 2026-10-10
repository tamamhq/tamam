from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tamam.api.routes import router
from tamam.config import get_settings
from tamam.db import dispose_db, migrate_db
from tamam.observability import init_observability

logger = logging.getLogger(__name__)

_REAPER_INTERVAL_S = 300


async def _reaper_loop(stop: asyncio.Event) -> None:
    """Periodically recover runs orphaned by a crash/shutdown/timeout so none is lost."""
    from tamam.runtime.session import reap_stale_runs

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=_REAPER_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        if stop.is_set():
            return
        try:
            await reap_stale_runs()
        except Exception:
            logger.exception("stale-run reaper iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_observability("tamam-api")
    await migrate_db()
    # Recover any runs orphaned by a previous crash/redeploy on boot, then keep reaping.
    from tamam.runtime.session import reap_stale_runs

    try:
        await reap_stale_runs()
    except Exception:
        logger.exception("startup stale-run reap failed")
    stop = asyncio.Event()
    reaper = asyncio.create_task(_reaper_loop(stop))
    try:
        yield
    finally:
        stop.set()
        reaper.cancel()
        await dispose_db()


app = FastAPI(title="Tamam", version="0.1.0", lifespan=lifespan)

# CORS: only enabled when origins are explicitly configured (SPA served from a different
# origin than the API). Default is no CORS — same-origin or the dev vite proxy needs none,
# and a wildcard would be an unnecessary exposure.
_cors_origins = get_settings().cors_allow_origins
if _cors_origins:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Dev mode has no sign-in: answer only local requests (see auth.is_local_request).
if get_settings().auth_dev_mode:
    from fastapi.responses import JSONResponse

    from tamam.auth import is_local_request

    @app.middleware("http")
    async def _local_only(request, call_next):  # noqa: ANN001
        if not is_local_request(request.headers.get("host"), request.headers.get("origin")):
            return JSONResponse({"detail": "dev mode only accepts local requests"}, 403)
        return await call_next(request)

app.include_router(router, prefix="/api")

# Artifacts are NOT served from a public static mount anymore — they're private,
# behind the token-guarded /api/artifacts endpoint (local) or presigned S3 URLs.
# Ensure the local artifacts dir exists for the local-storage backend.
get_settings().artifacts_dir.mkdir(parents=True, exist_ok=True)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
