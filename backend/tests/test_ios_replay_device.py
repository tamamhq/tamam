"""iOS's side of fast replay: a device that fails to start is stopped and released."""

import pytest

from tamam.executors.base import ExecutionSpec
from tamam.executors.ios import replay_device
from tamam.executors.ios.executor import IOSAgentExecutor


async def test_a_device_that_fails_to_start_is_stopped_and_released(monkeypatch):
    calls = []

    class Driver:
        async def stop(self):
            calls.append("stop")

    async def build(self, spec):
        return Driver()

    async def start(self, driver):
        raise RuntimeError("WebDriverAgent did not start")

    async def cleanup(self):
        calls.append("release")

    monkeypatch.setattr(IOSAgentExecutor, "_build_driver", build)
    monkeypatch.setattr(IOSAgentExecutor, "_start_with_reconnect", start)
    monkeypatch.setattr(IOSAgentExecutor, "_safe_cleanup", cleanup)
    with pytest.raises(RuntimeError):
        await replay_device.IOSReplayPlatform().open(ExecutionSpec(instructions="x"))
    assert calls == ["stop", "release"]


def test_the_hand_off_agent_is_attached_to_the_open_device():
    platform = replay_device.IOSReplayPlatform()
    device = replay_device.IOSDevice.__new__(replay_device.IOSDevice)
    device.driver, device._prov = object(), type("P", (), {"_reconnects": 1})()
    ai = platform.agent(device, preset_clauses=[], seed_history=["note"])
    assert ai._driver is device.driver and ai._attached and ai._reconnects_used == 1
    fresh = platform.agent()
    assert fresh._driver is None and not fresh._attached
