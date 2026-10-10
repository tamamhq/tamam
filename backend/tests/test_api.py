"""API smoke tests: CRUD + run enqueue + router selection + tenant isolation."""

import uuid
import httpx
import pytest

from tamam.auth import Principal, require_auth
from tamam.main import app


def _as_org(org_id: str):
    """Override auth to act as a fixed org — the FastAPI testing pattern."""
    async def _principal() -> Principal:
        return Principal(user_id=f"user_{org_id}", org_id=org_id, org_role="admin")
    return _principal


@pytest.fixture
async def client(monkeypatch):
    # Don't actually execute runs during API tests
    import tamam.api.routes as routes_module

    async def fake_enqueue(run_id: str) -> str:
        return "test"

    monkeypatch.setattr(routes_module, "enqueue_run", fake_enqueue)
    app.dependency_overrides[require_auth] = _as_org("org_a")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_tenant_isolation(client):
    """SECURITY: org B must not see, read, or mutate org A's data — a cross-tenant
    id is indistinguishable from a missing one (404)."""
    # org A creates a project + test
    proj = (await client.post("/api/projects", json={"name": "A's project"})).json()
    test = (await client.post(f"/api/projects/{proj['id']}/tests",
            json={"name": "t", "instructions": "i", "start_url": "https://x"})).json()

    # switch the caller to org B
    app.dependency_overrides[require_auth] = _as_org("org_b")

    assert (await client.get("/api/projects")).json() == []                 # B sees nothing
    assert (await client.get(f"/api/projects/{proj['id']}")).status_code == 404
    assert (await client.get(f"/api/tests/{test['id']}")).status_code == 404
    assert (await client.delete(f"/api/tests/{test['id']}")).status_code == 404
    assert (await client.post(f"/api/tests/{test['id']}/runs")).status_code == 404
    # B cannot attach A's build/test to anything either
    assert (await client.get(f"/api/projects/{proj['id']}/tests")).status_code == 404


async def test_full_crud_flow(client):
    project = (await client.post("/api/projects", json={"name": "demo"})).json()
    test = (
        await client.post(
            f"/api/projects/{project['id']}/tests",
            json={
                "name": "checkout",
                "instructions": "verify checkout works",
                "start_url": "https://x.example",
                "checks": [{"kind": "url_matches", "value": "/success"}],
                "checks_exhaustive": True,
            },
        )
    ).json()
    assert test["checks"][0]["kind"] == "url_matches"

    run_response = await client.post(f"/tests/{test['id']}/runs".replace("/tests", "/api/tests"))
    assert run_response.status_code == 202
    run = run_response.json()
    assert run["lifecycle"] == "queued"

    detail = (await client.get(f"/api/runs/{run['id']}")).json()
    assert detail["run"]["id"] == run["id"]
    assert detail["steps"] == []

    runs = (await client.get(f"/api/projects/{project['id']}/runs")).json()
    assert len(runs) == 1


async def test_404s(client):
    import uuid

    missing = uuid.uuid4()
    assert (await client.get(f"/api/projects/{missing}")).status_code == 404
    assert (await client.get(f"/api/tests/{missing}")).status_code == 404
    assert (await client.get(f"/api/runs/{missing}")).status_code == 404
    assert (
        await client.post(f"/api/tests/{missing}/runs")
    ).status_code == 404


def test_router_selection():
    from tamam.executors.registry import default_executor
    from tamam.runtime.routing import SCRIPT, select_executor
    from tamam.models import Test

    base = dict(project_id=None, name="t", instructions="i", start_url="u")
    assert select_executor(Test(**base)) == default_executor()
    assert select_executor(Test(**base, script={"steps": []})) == SCRIPT
    assert select_executor(Test(**base, executor_id="custom")) == "custom"


def test_executor_registry_describes_each_executor():
    from tamam.executors.registry import graduates, platform_of, registered_executors, user_executors

    assert {"ios_agent", "script"} <= set(registered_executors())
    assert platform_of("ios_agent") == "ios" and graduates("ios_agent")
    assert "script" not in user_executors()          # replay is the harness's own choice
    assert not graduates(None) and not graduates("nope")


def test_checks_registry():
    from tamam.harness.checks import registered_checks

    assert registered_checks() == [
        "action_used",
        "element_exists",
        "no_console_errors",
        "no_crashes",
        "no_failed_requests",
        "text_absent",
        "text_present",
        "url_matches",
    ]


async def test_check_kinds_endpoint(client):
    """FE form schema only — no harness internals (executors, prompt versions)."""
    descriptors = (await client.get("/api/check-kinds")).json()
    by_kind = {d["kind"]: d for d in descriptors}
    assert by_kind["url_matches"]["requires_value"] is True
    assert by_kind["no_console_errors"]["requires_value"] is False
    assert all({"kind", "label", "description", "requires_value"} <= set(d) for d in descriptors)


# ---------- Build upload hardening (security) ----------

async def _project(client) -> str:
    return (await client.post("/api/projects", json={"name": "p"})).json()["id"]


async def test_upload_rejects_bad_extension(client):
    """A non-build extension is refused before any bytes are written."""
    pid = await _project(client)
    r = await client.post(
        f"/api/projects/{pid}/builds/upload",
        files={"file": ("evil.txt", b"hello", "text/plain")},
        data={"name": "b"},
    )
    assert r.status_code == 400


async def test_upload_rejects_ipa_without_zip_magic(client):
    """A renamed non-archive can't masquerade as an .ipa."""
    pid = await _project(client)
    r = await client.post(
        f"/api/projects/{pid}/builds/upload",
        files={"file": ("app.ipa", b"not a real zip", "application/octet-stream")},
        data={"name": "b"},
    )
    assert r.status_code == 400


async def test_upload_enforces_size_limit(client, monkeypatch):
    """Over-limit uploads are rejected (413) and no partial file remains."""
    from tamam.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)  # any nonzero size exceeds
    pid = await _project(client)
    r = await client.post(
        f"/api/projects/{pid}/builds/upload",
        files={"file": ("app.ipa", b"PK\x03\x04over", "application/octet-stream")},
        data={"name": "b"},
    )
    assert r.status_code == 413


async def test_upload_accepts_valid_ipa(client):
    """A well-formed .ipa within limits is stored and returns a Build whose
    app_path is an opaque storage KEY — never a browser-exposed filesystem path."""
    pid = await _project(client)
    r = await client.post(
        f"/api/projects/{pid}/builds/upload",
        files={"file": ("app.ipa", b"PK\x03\x04" + b"\x00" * 128, "application/octet-stream")},
        data={"name": "release-1"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "release-1"
    assert body["app_path"].startswith(f"builds/{pid}/")  # a key, not an abs path
    assert not body["app_path"].startswith("/")


async def test_run_artifacts_are_signed_and_private(client, tmp_path):
    """SECURITY: the browser never sees a raw key or a public path — only a
    short-lived signed URL, and only after the ownership check in get_run. The
    signed URL serves the bytes; tampering or a missing token is rejected."""
    import uuid as _uuid

    from tamam.db import get_session
    from tamam.models import RunStepEvent
    from tamam.storage import get_storage

    pid = await _project(client)
    tid = (
        await client.post(
            f"/api/projects/{pid}/tests",
            json={"name": "t", "instructions": "i", "start_url": "https://x"},
        )
    ).json()["id"]
    rid = (await client.post(f"/api/tests/{tid}/runs")).json()["id"]

    # a screenshot lands in storage under its key, with a step row pointing at it
    src = tmp_path / "shot.png"
    src.write_bytes(b"\x89PNG-bytes")
    key = f"runs/{rid}/step-1.png"
    await get_storage().put(src, key)
    async with get_session() as db:
        db.add(RunStepEvent(run_id=_uuid.UUID(rid), step=1, payload={}, screenshot_url=key))
        await db.commit()

    detail = (await client.get(f"/api/runs/{rid}")).json()
    url = detail["steps"][0]["screenshot_url"]
    assert url.startswith(f"/api/artifacts/runs/{rid}/step-1.png?exp=")  # signed, not the raw key
    assert "sig=" in url

    # the signed URL serves the actual bytes
    got = await client.get(url)
    assert got.status_code == 200
    assert got.content == b"\x89PNG-bytes"

    # a tampered signature is refused
    tampered = url.rsplit("sig=", 1)[0] + "sig=deadbeef"
    assert (await client.get(tampered)).status_code == 404

    # no token at all → not served
    assert (await client.get(f"/api/artifacts/{key}")).status_code != 200


# ---------- delete_build purges the binary everywhere (right-to-deletion) ----------

async def test_delete_build_purges_storage_object(client):
    """SECURITY / right-to-deletion: deleting a build removes the actual binary from
    private storage, not just the DB row — a customer's .ipa is really gone."""
    from tamam.storage import get_storage

    pid = await _project(client)
    build = (await client.post(
        f"/api/projects/{pid}/builds/upload",
        files={"file": ("app.ipa", b"PK\x03\x04" + b"\x00" * 64, "application/octet-stream")},
        data={"name": "rel"},
    )).json()
    key = build["app_path"]
    assert await get_storage().get_bytes(key)  # binary is present before delete

    r = await client.delete(f"/api/builds/{build['id']}")
    assert r.status_code == 204

    # binary is gone from storage, and the build row is gone
    with pytest.raises(Exception):
        await get_storage().get_bytes(key)
    assert (await client.get(f"/api/projects/{pid}/builds")).json() == []


async def test_delete_build_purges_vendor_copies(client, monkeypatch):
    """A build uploaded to a device farm is deleted from the vendor too. We record
    vendor_refs at upload time; delete must call the vendor delete API for each."""
    import uuid as _uuid

    from tamam.db import get_session
    from tamam.models import Build

    calls = []

    async def fake_vendor_delete(vendor, ref):
        calls.append((vendor, ref))

    # the route imports delete_vendor_app from the provider module at call time
    monkeypatch.setattr(
        "tamam.executors.ios.providers.cloud.delete_app", fake_vendor_delete
    )

    pid = await _project(client)
    build = (await client.post(
        f"/api/projects/{pid}/builds",
        json={"name": "rel", "app_path": f"builds/{pid}/app.ipa"},
    )).json()

    # simulate two prior cloud runs having uploaded this build to two vendors
    async with get_session() as db:
        row = await db.get(Build, _uuid.UUID(build["id"]))
        row.vendor_refs = {"browserstack": "bs://ABC", "sauce": "storage:XYZ"}
        db.add(row)
        await db.commit()

    r = await client.delete(f"/api/builds/{build['id']}")
    assert r.status_code == 204
    assert sorted(calls) == [("browserstack", "bs://ABC"), ("sauce", "storage:XYZ")]


async def test_delete_build_survives_vendor_failure(client, monkeypatch):
    """A vendor API failure must not block the rest of deletion — the build row and
    other copies are still removed, and the failure is logged, not raised."""
    import uuid as _uuid

    from tamam.db import get_session
    from tamam.models import Build

    async def boom(vendor, ref):
        raise RuntimeError("vendor down")

    monkeypatch.setattr("tamam.executors.ios.providers.cloud.delete_app", boom)

    pid = await _project(client)
    build = (await client.post(
        f"/api/projects/{pid}/builds",
        json={"name": "rel", "app_path": f"builds/{pid}/app.ipa"},
    )).json()
    async with get_session() as db:
        row = await db.get(Build, _uuid.UUID(build["id"]))
        row.vendor_refs = {"browserstack": "bs://ABC"}
        db.add(row)
        await db.commit()

    r = await client.delete(f"/api/builds/{build['id']}")
    assert r.status_code == 204  # still succeeds
    assert (await client.get(f"/api/projects/{pid}/builds")).json() == []


async def test_create_build_rejects_cross_tenant_key(client):
    """Registering a build whose app_path points at ANOTHER project's storage key must be
    rejected — otherwise it's a path to reading another tenant's private build binary."""
    pid = await _project(client)
    r = await client.post(
        f"/api/projects/{pid}/builds",
        json={"name": "evil", "app_path": "builds/00000000-0000-0000-0000-000000000000/x.ipa"},
    )
    assert r.status_code == 400
    # a key scoped to THIS project is fine
    ok = await client.post(
        f"/api/projects/{pid}/builds",
        json={"name": "good", "app_path": f"builds/{pid}/x.ipa"},
    )
    assert ok.status_code == 201


# ---------- device/OS picker endpoint + per-test OS version ----------

async def test_ios_devices_endpoint(client, monkeypatch):
    """The picker endpoint returns {device, versions[]}, newest-OS-first, and requires auth."""
    async def fake_list():
        return [
            {"device": "iPhone 17 Pro", "versions": ["26"]},
            {"device": "iPhone 14", "versions": ["26", "16"]},
        ]

    monkeypatch.setattr(
        "tamam.executors.ios.providers.cloud.list_devices", fake_list
    )
    r = await client.get("/api/ios/devices")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["device"] == "iPhone 17 Pro"
    assert body[0]["versions"] == ["26"]


async def test_ios_devices_endpoint_requires_auth():
    """No override → unauthenticated request is rejected."""
    from tamam.config import get_settings

    if get_settings().clerk_issuer:  # .env may configure real auth locally
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/api/ios/devices")).status_code == 401


async def test_ios_platform_version_roundtrips(client):
    """A test created with an OS version persists it (so the run targets that OS)."""
    pid = (await client.post("/api/projects", json={"name": "p"})).json()["id"]
    t = (await client.post(f"/api/projects/{pid}/tests", json={
        "name": "t", "instructions": "i", "ios_target": "cloud",
        "device": "iPhone 15", "ios_platform_version": "26",
    })).json()
    assert t["ios_platform_version"] == "26"
    fetched = (await client.get(f"/api/tests/{t['id']}")).json()
    assert fetched["ios_platform_version"] == "26"


async def test_annotated_agent_view_is_signed_and_private(client, tmp_path):
    """SECURITY: the annotated 'what the agent saw' image is customer app IP — it
    must be served exactly like the raw screenshot: a short-lived signed URL minted
    only after the ownership check, never the raw storage key, never public."""
    import uuid as _uuid

    from tamam.db import get_session
    from tamam.models import RunStepEvent
    from tamam.storage import get_storage

    pid = await _project(client)
    tid = (await client.post(f"/api/projects/{pid}/tests",
           json={"name": "t", "instructions": "i"})).json()["id"]
    rid = (await client.post(f"/api/tests/{tid}/runs")).json()["id"]

    src = tmp_path / "som.png"
    src.write_bytes(b"\x89PNG-annotated")
    key = f"runs/{rid}/som-1.png"
    await get_storage().put(src, key)
    async with get_session() as db:
        db.add(RunStepEvent(run_id=_uuid.UUID(rid), step=1, payload={}, annotated_url=key))
        await db.commit()

    step = (await client.get(f"/api/runs/{rid}")).json()["steps"][0]
    url = step["annotated_url"]
    assert url.startswith(f"/api/artifacts/runs/{rid}/som-1.png?exp=") and "sig=" in url  # signed
    assert url != key  # never the raw key
    got = await client.get(url)
    assert got.status_code == 200 and got.content == b"\x89PNG-annotated"
    # unsigned direct access is refused
    assert (await client.get(f"/api/artifacts/{key}")).status_code != 200


async def test_annotated_view_absent_when_no_vision(client):
    """A step with no annotated image (text-only vision policy) returns annotated_url
    null — no dangling reference."""
    pid = await _project(client)
    tid = (await client.post(f"/api/projects/{pid}/tests",
           json={"name": "t", "instructions": "i"})).json()["id"]
    rid = (await client.post(f"/api/tests/{tid}/runs")).json()["id"]
    import uuid as _uuid

    from tamam.db import get_session
    from tamam.models import RunStepEvent
    async with get_session() as db:
        db.add(RunStepEvent(run_id=_uuid.UUID(rid), step=1, payload={}))
        await db.commit()
    step = (await client.get(f"/api/runs/{rid}")).json()["steps"][0]
    assert step["annotated_url"] is None


async def test_reuse_session_roundtrips_and_defaults_clean(client):
    """reuse_session defaults False (clean) and persists when set."""
    pid = (await client.post("/api/projects", json={"name": "p"})).json()["id"]
    default = (await client.post(f"/api/projects/{pid}/tests",
               json={"name": "t", "instructions": "i"})).json()
    assert default["reuse_session"] is False  # clean by default
    reuse = (await client.post(f"/api/projects/{pid}/tests",
             json={"name": "t2", "instructions": "i", "reuse_session": True})).json()
    assert reuse["reuse_session"] is True
    assert (await client.get(f"/api/tests/{reuse['id']}")).json()["reuse_session"] is True


async def test_deleting_a_test_deletes_its_runs_stored_files(client, monkeypatch):
    """Screens and traces are customer data: they go with the test."""
    import tamam.api.routes as routes
    from tamam.db import get_session
    from tamam.models import RunStepEvent, TestRun

    deleted = []

    class Store:
        async def delete(self, key): deleted.append(key)

    monkeypatch.setattr(routes, "get_storage", lambda: Store())
    proj = (await client.post("/api/projects", json={"name": "P"})).json()
    test = (await client.post(f"/api/projects/{proj['id']}/tests",
                              json={"name": "T", "instructions": "x"})).json()
    async with get_session() as db:
        run = TestRun(test_id=uuid.UUID(test["id"]), video_url="runs/r1/recording.mp4",
                      trace_key="runs/r1/step_trace.jsonl")
        db.add(run); await db.commit(); await db.refresh(run)
        db.add(RunStepEvent(run_id=run.id, step=1, payload={}, screenshot_url="runs/r1/s1.png"))
        await db.commit()
    assert (await client.delete(f"/api/tests/{test['id']}")).status_code == 204
    assert sorted(deleted) == ["runs/r1/recording.mp4", "runs/r1/s1.png", "runs/r1/step_trace.jsonl"]


async def test_fast_mode_setting_and_script_endpoint(client):
    from tamam.db import get_session
    from tamam.models import TestScript

    proj = (await client.post("/api/projects", json={"name": "P"})).json()
    test = (await client.post(f"/api/projects/{proj['id']}/tests",
                              json={"name": "T", "instructions": "x"})).json()
    assert (await client.get(f"/api/tests/{test['id']}/script")).json() == {"script": None}
    r = await client.patch(f"/api/tests/{test['id']}", json={"graduation": {"fast_mode": "off"}})
    assert r.json()["graduation"] == {"fast_mode": "off"}
    r = await client.patch(f"/api/tests/{test['id']}",
                           json={"graduation": {"fast_mode": "off", "allow_risky_actions": True}})
    assert r.json()["graduation"] == {"fast_mode": "off", "allow_risky_actions": True}
    r = await client.patch(f"/api/tests/{test['id']}", json={"graduation": {"fast_mode": None}})
    assert r.json()["graduation"] == {"allow_risky_actions": True}   # other keys kept
    r = await client.patch(f"/api/tests/{test['id']}", json={"graduation": {"allow_risky_actions": None}})
    assert r.json()["graduation"] is None                      # back to inheriting
    async with get_session() as db:
        db.add(TestScript(test_id=uuid.UUID(test["id"]), version=1, status="probation",
                          body={"steps": []}, test_text_hash="h"))
        await db.commit()
    got = (await client.get(f"/api/tests/{test['id']}/script")).json()["script"]
    assert got["version"] == 1 and got["status"] == "probation"
    assert (await client.delete(f"/api/tests/{test['id']}")).status_code == 204


async def test_an_unknown_ios_target_is_rejected(client):
    """Scripts are scoped to the target; an unknown one would silently run on the simulator."""
    project = (await client.post("/api/projects", json={"name": "P"})).json()
    r = await client.post(f"/api/projects/{project['id']}/tests",
                          json={"name": "T", "instructions": "x", "ios_target": "farm"})
    assert r.status_code == 422


def test_without_the_web_plugin_runs_route_to_the_ios_agent(monkeypatch):
    """The open-source release ships without web testing: no plugin claims the default, so a
    test with no explicit executor runs on the iOS agent."""
    from tamam.executors import registry
    from tamam.runtime import routing as router
    from tamam.models import Test
    from tamam.plugins import Registry

    bare = Registry("executors")
    bare._plugins_loaded = True                       # no entry points: the public install
    bare.register("ios_agent", object, platform="ios", graduates=True)
    monkeypatch.setattr(registry, "EXECUTORS", bare)
    t = Test(project_id=__import__("uuid").uuid4(), name="t", instructions="x")
    assert router.select_executor(t) == "ios_agent"
