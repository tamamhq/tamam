"""iOS's side of fast replay (graduation.platform): open a device for replay, and build the AI
agent — fresh, or attached to that device when it takes over a replay that missed."""

from __future__ import annotations

from typing import Any

from tamam.config import get_settings
from tamam.executors.base import ExecutionSpec, RunServices
from tamam.executors.ios.executor import IOSAgentExecutor
from tamam.executors.ios.perceiver import IOSPerceiver
from tamam.executors.ios.session import DeviceSession


class IOSDevice:
    def __init__(self, provisioner: IOSAgentExecutor, driver: Any) -> None:
        self._prov = provisioner
        self.driver = driver
        self.session = DeviceSession(driver, IOSPerceiver(),
                                     reconnect=lambda: provisioner._try_reconnect(driver))

    @property
    def reconnects_used(self) -> int:
        return getattr(self._prov, "_reconnects", 0)

    async def stop(self) -> str | None:
        try:
            return await self.driver.stop()
        except Exception:
            return None

    async def release(self) -> None:
        try:
            await self._prov._safe_cleanup()
        except Exception:
            pass


class IOSReplayPlatform:
    def __init__(self) -> None:
        self._services: RunServices | None = None

    def attach(self, services: RunServices) -> None:
        self._services = services

    async def open(self, spec: ExecutionSpec) -> IOSDevice:
        prov = IOSAgentExecutor()     # its provisioning: provider, driver, reconnects
        if self._services is not None:
            prov.attach(self._services)
        prov._reconnects = 0
        prov._max_reconnects = get_settings().max_session_reconnects
        driver = None
        try:
            driver = await prov._build_driver(spec)
            await prov._start_with_reconnect(driver)
        except BaseException:
            if driver is not None:
                await IOSDevice(prov, driver).stop()
            await prov._safe_cleanup()
            raise
        return IOSDevice(prov, driver)

    def agent(self, device: IOSDevice | None = None, **handoff: Any) -> IOSAgentExecutor:
        if device is None:
            return IOSAgentExecutor()
        ai = IOSAgentExecutor(driver=device.driver, attached=True,
                              reconnects_used=device.reconnects_used, **handoff)
        if self._services is not None:
            ai.attach(self._services)    # the hand-off continues the replay's live view
        return ai
