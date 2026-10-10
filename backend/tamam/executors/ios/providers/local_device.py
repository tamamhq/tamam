"""A real iPhone attached to this Mac, driven by a local Appium server (signed WebDriverAgent)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse

from tamam.config import get_settings
from tamam.executors.base import ExecutionSpec, RunServices
from tamam.executors.ios.driver import AppiumDriver
from tamam.executors.ios.providers.common import apply_alert_policy, apply_host_ports, resolve_local_app

logger = logging.getLogger(__name__)


class LocalDeviceProvider:
    """A real iPhone attached to THIS Mac, driven via the local Appium server — the
    free, self-hosted real-device path. Appium builds + signs WebDriverAgent with your
    Apple Team (free Apple ID works; 7-day certs). Needs the device UDID and team id."""

    def __init__(self, services: RunServices | None = None, server_url: str | None = None) -> None:
        s = get_settings()
        self._server = server_url or s.appium_server_url
        self._udid = s.ios_device_udid
        self._team = s.ios_team_id
        self._wda_bundle = s.wda_bundle_id
        self._driver: AppiumDriver | None = None

    async def driver(self, spec: ExecutionSpec) -> AppiumDriver:
        if not self._udid:
            raise RuntimeError("Real device: set TAMAM_IOS_DEVICE_UDID (see the attached iPhone)")
        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
        except ImportError as exc:
            raise RuntimeError("iOS deps not installed — run `uv sync --all-extras`") from exc

        opts = XCUITestOptions()
        opts.platform_name = "iOS"
        opts.automation_name = "XCUITest"
        opts.udid = self._udid
        app = await resolve_local_app(spec.app.build_ref or "")
        if app:
            opts.app = app
        if spec.app.bundle_id:
            opts.bundle_id = (spec.app.bundle_id or "").strip()
        apply_alert_policy(opts)
        opts.set_capability("newCommandTimeout", 300)
        opts.set_capability("waitForQuiescence", False)
        mjpeg_port = apply_host_ports(opts)
        # Sign WebDriverAgent with your Apple Team so it installs on the real device.
        if self._team:
            opts.set_capability("xcodeOrgId", self._team)
            opts.set_capability("xcodeSigningId", "Apple Development")
        if self._wda_bundle:
            opts.set_capability("updatedWDABundleId", self._wda_bundle)

        session = await asyncio.to_thread(webdriver.Remote, self._server, options=opts)
        run_dir = Path(get_settings().artifacts_dir) / str(uuid.uuid4())
        # Real device: no local simctl video; MJPEG live preview still works via the
        # forwarded WDA stream.
        self._driver = AppiumDriver(session, run_dir, local_simulator=False)
        host = urlparse(self._server).hostname or "127.0.0.1"
        self._driver.mjpeg_url = f"http://{host}:{mjpeg_port}"
        return self._driver

    async def cleanup(self) -> None:
        self._driver = None
