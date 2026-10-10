"""AWSDeviceFarmProvider — proven with a fake boto3 client and a fake Appium session.
No AWS calls, no devices, no cost."""

import pytest

from tamam.executors.base import AppTarget
from tamam.executors.base import ExecutionSpec
from tamam.executors.ios.providers.aws_device_farm import AWSDeviceFarmProvider


class FakeDF:
    """Minimal stand-in for the boto3 devicefarm client (snake_case ops)."""

    def __init__(self, devices=None, endpoint="https://ep.devicefarm/wd/hub", status="RUNNING"):
        self._devices = devices if devices is not None else _DEVICES
        self._endpoint = endpoint
        self._status = status
        self.stopped = False

    def list_devices(self, **kw):
        return {"devices": self._devices}

    def create_remote_access_session(self, **kw):
        self.created = kw
        return {"remoteAccessSession": {"arn": "sess-arn"}}

    def get_remote_access_session(self, **kw):
        return {"remoteAccessSession": {
            "status": self._status,
            "endpoint": "wss://console/video",  # the wss console socket — must NOT be used
            "endpoints": {"remoteDriverEndpoint": self._endpoint},  # the https Appium URL
            "message": "boom"}}

    def stop_remote_access_session(self, **kw):
        self.stopped = True
        self.stop_calls = getattr(self, 'stop_calls', 0) + 1
        return {}

    def list_artifacts(self, **kw):
        return {"artifacts": [
            {"type": "DEVICE_LOG", "url": "https://x/log"},
            {"type": "VIDEO", "url": "https://x/video.mp4"},
        ]}


_DEVICES = [
    {"platform": "IOS", "formFactor": "PHONE", "os": "18.6.2", "name": "iPhone 11",
     "availability": "HIGHLY_AVAILABLE", "arn": "arn:ios18"},
    {"platform": "IOS", "formFactor": "PHONE", "os": "26.3.1", "name": "iPhone 17 Pro",
     "availability": "HIGHLY_AVAILABLE", "arn": "arn:ios26-pro"},
    {"platform": "IOS", "formFactor": "PHONE", "os": "26.6", "name": "iPhone 12",
     "availability": "HIGHLY_AVAILABLE", "arn": "arn:ios266"},
    {"platform": "ANDROID", "formFactor": "PHONE", "os": "14", "name": "Pixel",
     "availability": "HIGHLY_AVAILABLE", "arn": "arn:android"},
]


def _provider(df, min_ios=26.0):
    p = AWSDeviceFarmProvider()
    p._key, p._secret, p._project = "k", "s", "arn:proj"
    p._min_ios = min_ios
    p._client = df  # inject the fake (bypasses boto3.client)
    return p


async def test_pick_device_selects_newest_ios_at_or_above_min():
    p = _provider(FakeDF())
    arn = await p._pick_device()
    assert arn == "arn:ios26-pro"  # Pro model preferred (less contended), both highly available


async def test_pick_device_raises_when_none_meet_min():
    df = FakeDF(devices=[d for d in _DEVICES if d["platform"] == "IOS" and d["os"].startswith("18")])
    with pytest.raises(RuntimeError, match="no iOS phone with OS"):
        await _provider(df)._pick_device()


async def test_driver_creates_session_and_connects_to_endpoint(monkeypatch):
    calls = {}

    def fake_remote(url, options=None):
        calls["url"] = url
        calls["bundle"] = getattr(options, "bundle_id", None)
        return object()  # AppiumDriver just stores it; no methods called until start()

    monkeypatch.setattr("appium.webdriver.Remote", fake_remote)
    df = FakeDF()
    p = _provider(df)
    spec = ExecutionSpec(instructions="x", max_steps=5, target=AppTarget(bundle_id="com.example.app"))  # no app_build_ref → skip upload
    drv = await p.driver(spec)

    assert drv is not None
    assert calls["url"] == "https://ep.devicefarm/wd/hub"     # connected to the session endpoint
    assert calls["bundle"] == "com.example.app"               # launches the installed app
    assert df.created["deviceArn"] == "arn:ios26-pro"          # Pro model preferred
    assert df.created["interactionMode"] == "INTERACTIVE"
    # cleanup stops the remote-access session (stops the meter)
    await p.cleanup()
    assert df.stopped is True


async def test_await_endpoint_raises_on_errored_session():
    p = _provider(FakeDF(status="ERRORED"))
    with pytest.raises(RuntimeError, match="ERRORED"):
        await p._await_endpoint("sess-arn", timeout_s=3)


async def test_driver_requires_config():
    p = AWSDeviceFarmProvider()
    p._key, p._secret, p._project = None, None, None
    with pytest.raises(RuntimeError, match="not configured"):
        await p.driver(ExecutionSpec(instructions="x", max_steps=5))


async def test_delete_vendor_app_purges_device_farm_upload(monkeypatch):
    """Build deletion removes the Device Farm upload (right-to-deletion)."""
    from tamam.executors.ios.providers import cloud
    deleted = {}

    class FakeExc(Exception):
        pass

    class FakeDFClient:
        class exceptions:
            NotFoundException = FakeExc
        def delete_upload(self, arn):
            deleted["arn"] = arn
            return {}

    monkeypatch.setattr("boto3.client", lambda *a, **k: FakeDFClient())

    class S:
        aws_access_key_id = "k"; aws_secret_access_key = "s"; aws_region = "us-west-2"
    monkeypatch.setattr(cloud, "get_settings", lambda: S())
    await cloud.delete_app("aws_device_farm", "arn:aws:devicefarm:...:upload:abc")
    assert deleted["arn"] == "arn:aws:devicefarm:...:upload:abc"


async def test_session_stopped_when_connect_fails(monkeypatch):
    """If Appium connect fails after the session is created, the provider stops the
    session immediately so it can't drain device-minutes until the idle timeout."""
    def boom(url, options=None):
        raise RuntimeError("Unable to start WebDriverAgent session")

    monkeypatch.setattr("appium.webdriver.Remote", boom)
    df = FakeDF()
    p = _provider(df)
    spec = ExecutionSpec(instructions="x", max_steps=5)
    with pytest.raises(RuntimeError, match="WebDriverAgent"):
        await p.driver(spec)
    assert df.stopped is True  # session was stopped despite the failure


async def test_connect_sets_noreset_to_avoid_frontboard_race(monkeypatch):
    captured = {}

    def fake_remote(url, options=None):
        captured["noReset"] = options.get_capability("noReset") if hasattr(options, "get_capability") \
            else options.to_capabilities().get("appium:noReset")
        return object()

    monkeypatch.setattr("appium.webdriver.Remote", fake_remote)
    p = _provider(FakeDF())
    await p.driver(ExecutionSpec(instructions="x", max_steps=5))
    assert captured["noReset"] is True


def test_video_url_selection():
    assert AWSDeviceFarmProvider._video_url(
        [{"type": "DEVICE_LOG", "url": "l"}, {"type": "VIDEO", "url": "v.mp4"}]) == "v.mp4"
    assert AWSDeviceFarmProvider._video_url([{"type": "DEVICE_LOG", "url": "l"}]) is None


async def test_stop_session_is_idempotent():
    df = FakeDF()
    p = _provider(df)
    p._session_arn = "sess-arn"
    await p._stop_session()
    await p._stop_session()      # second call must not re-stop
    await p.cleanup()            # nor cleanup
    assert getattr(df, "stop_calls", 0) == 1


async def test_fetch_video_none_without_session():
    p = _provider(FakeDF())
    p._session_arn = None
    assert await p._fetch_video() is None


def test_device_farm_sessions_ask_for_appium_3(monkeypatch):
    """Device Farm defaults to Appium 2 on iOS < 27, whose WDA drops snapshotMaxChildren."""
    from tamam.config import get_settings
    from tamam.executors.ios.providers.aws_device_farm import AWSDeviceFarmProvider
    monkeypatch.setattr(get_settings(), "aws_devicefarm_appium_version", "3")
    assert AWSDeviceFarmProvider._session_configuration() == {
        "configuration": {"parameters": {"appium:version": "3"}}}
    monkeypatch.setattr(get_settings(), "aws_devicefarm_appium_version", "")
    assert AWSDeviceFarmProvider._session_configuration() == {}


def test_installed_boto3_can_send_the_appium_version():
    """The field only exists from botocore 1.43.104; an older one fails before the request."""
    import botocore.session
    shape = botocore.session.get_session().get_service_model("devicefarm").shape_for(
        "CreateRemoteAccessSessionConfiguration")
    assert "parameters" in shape.members
