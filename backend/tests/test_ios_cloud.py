"""CloudProvider — the vendor-agnostic real-device backend (LambdaTest/BrowserStack/
Sauce). What's testable without creds/network: the creds guard, the per-vendor hub
URL, and the app-reference passthrough."""

import pytest

from tamam.executors.base import AppTarget
from tamam.executors.base import ExecutionSpec
from tamam.executors.ios.providers.cloud import CloudProvider


async def test_cloud_requires_creds():
    p = CloudProvider()
    p._user, p._key = None, None
    spec = ExecutionSpec(instructions="x", target=AppTarget(bundle_id="com.x", device="iPhone 15"))
    with pytest.raises(RuntimeError, match="creds"):
        await p.driver(spec)


async def test_cloud_app_ref_passthrough():
    p = CloudProvider()
    for ref in ("lt://ABC", "bs://XYZ", "storage:123", "https://x/app.ipa"):
        assert await p._upload_app(ref) == ref


def test_cloud_hub_by_vendor():
    p = CloudProvider()
    p._vendor = "lambdatest"
    assert "lambdatest.com" in p._hub() and p._opts_key() == "lt:options"
    p._vendor = "browserstack"
    assert "browserstack.com" in p._hub() and p._opts_key() == "bstack:options"
    p._vendor, p._region = "sauce", "eu-central-1"
    assert p._hub() == "https://ondemand.eu-central-1.saucelabs.com/wd/hub"


# ---------- delete_vendor_app: right-to-deletion at the device farm ----------

class _FakeResp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


def _fake_httpx(sink, status=200):
    class _FakeClient:
        def __init__(self, *a, **kw):
            self._auth = kw.get("auth")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def delete(self, url):
            sink.append((url, self._auth))
            return _FakeResp(status)

    return _FakeClient


async def test_delete_vendor_app_endpoints(monkeypatch):
    """Each vendor's delete hits the right URL with the configured creds."""
    import httpx

    from tamam.config import get_settings
    from tamam.executors.ios.providers.cloud import delete_app as delete_vendor_app

    s = get_settings()
    monkeypatch.setattr(s, "cloud_username", "u", raising=False)
    monkeypatch.setattr(s, "cloud_access_key", "k", raising=False)
    monkeypatch.setattr(s, "sauce_region", "us-west-1", raising=False)

    calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx(calls))

    await delete_vendor_app("browserstack", "bs://ABC")
    await delete_vendor_app("sauce", "storage:XYZ")
    await delete_vendor_app("lambdatest", "lt://123")

    urls = [u for u, _ in calls]
    assert urls[0] == "https://api-cloud.browserstack.com/app-automate/app/delete/ABC"
    assert urls[1] == "https://api.us-west-1.saucelabs.com/v1/storage/files/XYZ"
    assert urls[2] == "https://manual-api.lambdatest.com/app/delete/123"
    assert all(auth == ("u", "k") for _, auth in calls)  # authenticated


async def test_delete_vendor_app_404_is_success(monkeypatch):
    """Already-gone (404) must not raise — deletion is idempotent."""
    import httpx

    from tamam.config import get_settings
    from tamam.executors.ios.providers.cloud import delete_app as delete_vendor_app

    s = get_settings()
    monkeypatch.setattr(s, "cloud_username", "u", raising=False)
    monkeypatch.setattr(s, "cloud_access_key", "k", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx([], status=404))

    await delete_vendor_app("browserstack", "bs://GONE")  # no raise


async def test_delete_vendor_app_requires_creds(monkeypatch):
    from tamam.config import get_settings
    from tamam.executors.ios.providers.cloud import delete_app as delete_vendor_app

    s = get_settings()
    monkeypatch.setattr(s, "cloud_username", None, raising=False)
    monkeypatch.setattr(s, "cloud_access_key", None, raising=False)
    with pytest.raises(RuntimeError, match="creds"):
        await delete_vendor_app("browserstack", "bs://ABC")


async def test_persist_vendor_ref_records_on_build():
    """A cloud upload is recorded on its Build so it can later be deleted."""
    import uuid as _uuid

    from tamam.db import get_session
    from tamam.runtime.builds import record_vendor_ref as _persist_vendor_ref
    from tamam.models import Build, Project

    async with get_session() as db:
        proj = Project(org_id="org_dev", name="p")
        db.add(proj)
        await db.commit()
        await db.refresh(proj)
        build = Build(project_id=proj.id, name="b", app_path="builds/x/app.ipa")
        db.add(build)
        await db.commit()
        await db.refresh(build)
        bid = build.id

    await _persist_vendor_ref(str(bid), "browserstack", "bs://ABC")

    async with get_session() as db:
        row = await db.get(Build, _uuid.UUID(str(bid)))
        assert row.vendor_refs == {"browserstack": "bs://ABC"}


# ---------- iOS device catalog (the device/OS picker source of truth) ----------

def _fake_httpx_get(payload, status=200):
    class _Resp:
        def __init__(self):
            self.status_code = status

        def json(self):
            return payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("http")

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    return _Client


async def test_list_ios_devices_groups_and_sorts_newest_first(monkeypatch):
    import httpx

    from tamam.config import get_settings
    from tamam.executors.ios.providers import cloud

    cloud._DEVICE_CATALOG_CACHE.clear()
    s = get_settings()
    monkeypatch.setattr(s, "ios_cloud", "browserstack", raising=False)
    monkeypatch.setattr(s, "cloud_username", "u", raising=False)
    monkeypatch.setattr(s, "cloud_access_key", "k", raising=False)

    payload = [
        {"device": "iPhone 14", "os": "ios", "os_version": "16"},
        {"device": "iPhone 14", "os": "ios", "os_version": "26"},
        {"device": "iPhone 17 Pro", "os": "ios", "os_version": "26"},
        {"device": "Galaxy S23", "os": "android", "os_version": "14"},  # filtered out
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx_get(payload))

    devices = await cloud.list_devices()
    names = [d["device"] for d in devices]
    assert "Galaxy S23" not in names                     # android filtered
    assert devices[0]["versions"][0] == "26"             # newest OS first
    iphone14 = next(d for d in devices if d["device"] == "iPhone 14")
    assert iphone14["versions"] == ["26", "16"]          # per-device versions, newest first


async def test_list_ios_devices_falls_back_without_creds(monkeypatch):
    from tamam.config import get_settings
    from tamam.executors.ios.providers import cloud

    cloud._DEVICE_CATALOG_CACHE.clear()
    s = get_settings()
    monkeypatch.setattr(s, "ios_cloud", "browserstack", raising=False)
    monkeypatch.setattr(s, "cloud_username", None, raising=False)
    monkeypatch.setattr(s, "cloud_access_key", None, raising=False)

    devices = await cloud.list_devices()
    assert devices and all("device" in d and d["versions"] for d in devices)  # non-empty fallback


async def test_configure_vendor_prefers_per_test_os_version():
    """The per-test OS version (from the picker) must win over the server default so
    a device+OS the app requires is actually requested."""
    from tamam.executors.base import ExecutionSpec
    from tamam.executors.ios.providers.cloud import CloudProvider

    p = CloudProvider()
    p._vendor, p._user, p._key = "browserstack", "u", "k"

    class _Opts:
        def __init__(self):
            self.caps = {}

        def set_capability(self, k, v):
            self.caps[k] = v

    opts = _Opts()
    spec = ExecutionSpec(instructions="x", target=AppTarget(device="iPhone 15", os_version="26"))
    p._configure_vendor(opts, spec)
    assert opts.caps["bstack:options"]["osVersion"] == "26"
    assert opts.caps["bstack:options"]["deviceName"] == "iPhone 15"


# ---------- clean-state isolation on shared cloud devices ----------

class _FakeCloudSession:
    def quit(self): pass


def _capture_remote(monkeypatch, sink):
    import appium.webdriver as awd

    def fake_remote(url, options=None):
        sink["caps"] = options.to_capabilities() if options else {}
        return _FakeCloudSession()

    monkeypatch.setattr(awd, "Remote", fake_remote)


def _flat(caps):
    return {k.split(":")[-1]: v for k, v in caps.items()}


async def test_cloud_defaults_to_clean_state(monkeypatch):
    """Default (reuse_session False): fullReset on (wipes keychain/login), and the app
    is scheduled for teardown uninstall — so no session lingers on the shared device."""
    from tamam.executors.base import ExecutionSpec
    from tamam.executors.ios.providers.cloud import CloudProvider

    sink = {}
    _capture_remote(monkeypatch, sink)
    p = CloudProvider()
    p._vendor, p._user, p._key = "browserstack", "u", "k"
    spec = ExecutionSpec(instructions="x", target=AppTarget(bundle_id="com.example.shop", device="iPhone 15"))
    drv = await p.driver(spec)
    caps = _flat(sink["caps"])
    assert caps.get("fullReset") is True
    assert caps.get("noReset") in (None, False)
    assert drv.teardown_uninstall is True
    assert drv.teardown_uninstall_bundle == "com.example.shop"


async def test_cloud_reuse_session_persists_state(monkeypatch):
    """Opt-in reuse_session: noReset on, fullReset off, and NO teardown uninstall —
    the 'log in once, test many' flow."""
    from tamam.executors.base import ExecutionSpec
    from tamam.executors.ios.providers.cloud import CloudProvider

    sink = {}
    _capture_remote(monkeypatch, sink)
    p = CloudProvider()
    p._vendor, p._user, p._key = "browserstack", "u", "k"
    spec = ExecutionSpec(instructions="x", target=AppTarget(bundle_id="com.example.shop", device="iPhone 15", reuse_session=True))
    drv = await p.driver(spec)
    caps = _flat(sink["caps"])
    assert caps.get("noReset") is True
    assert caps.get("fullReset") in (None, False)
    assert drv.teardown_uninstall is False


async def test_teardown_uninstalls_before_quit():
    """The app is removed BEFORE the session ends, so its keychain/session is gone."""
    from tamam.executors.ios.driver import AppiumDriver

    calls = []

    class FakeSession:
        def remove_app(self, b): calls.append(("remove", b))
        def quit(self): calls.append(("quit",))

    d = AppiumDriver(FakeSession(), "/tmp/tamam-test-teardown", remote=True)
    d.teardown_uninstall = True
    d.teardown_uninstall_bundle = "com.example.shop"
    await d.stop()
    assert ("remove", "com.example.shop") in calls
    assert calls.index(("remove", "com.example.shop")) < calls.index(("quit",))


async def test_teardown_resolves_bundle_from_session_caps():
    """When the test set no bundle id (build-based test), teardown resolves it from
    the session capabilities the farm fills in — so uninstall still runs."""
    from tamam.executors.ios.driver import AppiumDriver

    calls = []

    class FakeSession:
        capabilities = {"bundleId": "com.resolved.fromcaps", "udid": "U"}
        session_id = "s"

        def get_window_size(self): return {"width": 390, "height": 844}
        def remove_app(self, b): calls.append(b)
        def quit(self): pass

    d = AppiumDriver(FakeSession(), "/tmp/tamam-test-teardown2", remote=True)
    d.teardown_uninstall = True  # no explicit bundle hint
    await d.start()               # captures bundle from caps
    await d.stop()
    assert calls == ["com.resolved.fromcaps"]
