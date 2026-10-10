"""Which device an iOS run gets, and farm uploads recorded through the host (never the DB)."""

import pytest

from tamam.config import get_settings
from tamam.executors.base import AppTarget, ExecutionSpec, RunServices, record_upload
from tamam.executors.ios.providers import DEVICE_PROVIDERS, build_provider, provider_name


def _spec(backend=None):
    return ExecutionSpec(instructions="x", target=AppTarget(backend=backend))


@pytest.mark.parametrize("backend,cloud,want", [
    (None, "lambdatest", "simulator"),                 # server default
    ("device", "lambdatest", "device"),
    ("cloud", "browserstack", "cloud"),                # subscription clouds share one provider
    ("cloud", "aws_device_farm", "aws_device_farm"),   # its own provider
    ("teleport", "lambdatest", "simulator"),           # unknown → the safe default
])
def test_the_run_target_picks_the_provider(monkeypatch, backend, cloud, want):
    s = get_settings()
    monkeypatch.setattr(s, "ios_backend", "simulator")
    monkeypatch.setattr(s, "ios_cloud", cloud)
    assert provider_name(_spec(backend)) == want


def test_a_provider_is_a_drop_in(monkeypatch):
    made = []
    monkeypatch.setattr(DEVICE_PROVIDERS, "_entries", dict(DEVICE_PROVIDERS._entries))  # restored after
    DEVICE_PROVIDERS.register("simulator", lambda svc: made.append(svc) or "mine", replace=True)
    services = RunServices()
    assert build_provider(_spec(), services) == "mine" and made == [services]


async def test_uploads_are_recorded_through_the_host_and_never_break_a_run():
    seen = []

    async def record(build_id, vendor, ref):
        seen.append((build_id, vendor, ref))

    await record_upload(RunServices(record_upload=record), "b1", "browserstack", "bs://1")
    await record_upload(RunServices(), "b1", "browserstack", "bs://1")      # CLI / evals: no host
    await record_upload(RunServices(record_upload=record), None, "x", "y")  # no owning build

    async def broken(*a):
        raise RuntimeError("db down")
    await record_upload(RunServices(record_upload=broken), "b1", "sauce", "storage:1")
    assert seen == [("b1", "browserstack", "bs://1")]
