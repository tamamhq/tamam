"""A local Appium server driving an iOS Simulator (unsigned .app, no signing)."""

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


class SimulatorProvider:
    """Local Appium server driving an iOS Simulator. Unsigned .app, no signing."""

    def __init__(self, services: RunServices | None = None, server_url: str | None = None) -> None:
        settings = get_settings()
        self._server = server_url or getattr(settings, "appium_server_url", "http://127.0.0.1:4723")
        self._driver: AppiumDriver | None = None

    async def driver(self, spec: ExecutionSpec) -> AppiumDriver:
        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
        except ImportError as exc:
            raise RuntimeError(
                "iOS deps not installed — run `uv sync --extra ios` and start an Appium server"
            ) from exc

        opts = XCUITestOptions()
        opts.platform_name = "iOS"
        opts.automation_name = "XCUITest"
        # Trim stray whitespace — a pasted " com.apple.Preferences" is a different,
        # unknown bundle id to XCUITest.
        opts.device_name = (spec.app.device or "").strip() or "iPhone 15"
        # Pin to a specific booted simulator when a udid is configured. Without this,
        # XCUITest picks/creates its own ephemeral clone ("appiumTest-…") per session, so
        # any media or state we seed onto a known simulator would be invisible to the run.
        # Pinning also lets a human watch ONE predictable Simulator window.
        sim_udid = (get_settings().ios_device_udid or "").strip()
        if sim_udid:
            opts.set_capability("udid", sim_udid)
        app = await resolve_local_app(spec.app.build_ref or "")
        bundle = (spec.app.bundle_id or "").strip()
        if app:
            opts.app = app                         # path to the .app/.zip (simulator build)
            if spec.app.reuse_session:
                # "Log in once, test many": reinstalling wipes the app's data and
                # keychain, which would destroy the persisted login before step 1.
                # The trade-off is an already-installed build is NOT replaced — which
                # is why eval variants (correct/buggy sharing a bundle id) must keep
                # reuse_session False, so the branch below still forces the install.
                opts.set_capability("noReset", True)
            else:
                # Force install of THIS exact build every session — critical when eval
                # variants (correct/buggy) share a bundle id, else a stale build is tested.
                opts.set_capability("enforceAppInstall", True)
        else:
            # Preinstalled app (by bundle id) — do NOT reset, so login/data persist
            # across runs (essential for authenticated flows: sign in once, then the
            # agent tests everything after).
            opts.set_capability("noReset", True)
        if bundle:
            opts.bundle_id = bundle
        # Launch arguments for the app under test (e.g. a test-login hook). Lets a
        # test target auto-configure itself instead of the agent typing secrets.
        launch_args = get_settings().ios_sim_launch_args
        if launch_args:
            opts.set_capability("processArguments", {"args": list(launch_args), "env": {}})
        apply_alert_policy(opts)
        opts.set_capability("newCommandTimeout", 300)
        # Performance: skip the slow app-idle wait.
        opts.set_capability("waitForQuiescence", False)
        # Live preview: WDA's own MJPEG stream on a separate port — independent of the
        # command session, so streaming frames never contend with the agent loop.
        mjpeg_port = apply_host_ports(opts)

        session = await asyncio.to_thread(webdriver.Remote, self._server, options=opts)
        # Keep frames small + smooth for the live view (best effort).
        try:
            await asyncio.to_thread(session.update_settings, {
                "mjpegServerFramerate": 10,
                "mjpegServerScreenshotQuality": 25,
                "mjpegScalingFactor": 50,
            })
        except Exception:
            pass
        run_dir = Path(get_settings().artifacts_dir) / str(uuid.uuid4())
        self._driver = AppiumDriver(session, run_dir)
        host = urlparse(self._server).hostname or "127.0.0.1"
        self._driver.mjpeg_url = f"http://{host}:{mjpeg_port}"
        self._driver._reconnect = self._make_reconnect(webdriver, opts)
        return self._driver

    def _make_reconnect(self, webdriver, opts):  # noqa: ANN001
        """Reconnect hook: a wedged/dead WDA session on the simulator used to END the run
        ("reconnect not supported for this backend"), so one slow screen on a degraded app
        cost the whole test. Open a fresh session on the same local server instead:
        noReset (never reinstall — that would wipe the login and the run's progress) and
        a forced relaunch of the app. The old session is quit first, bounded, so the new
        one does not queue behind it."""
        async def _reconnect() -> None:
            drv = self._driver
            logger.warning("simulator session wedged/dead — opening a fresh session")
            await drv._teardown_step(drv._d.quit, drv._cmd_timeout, None, "wedged session quit")
            opts.set_capability("noReset", True)
            opts.set_capability("enforceAppInstall", False)
            opts.set_capability("forceAppLaunch", True)
            new = await asyncio.to_thread(webdriver.Remote, self._server, options=opts)
            drv._d = new
            drv.session_id = getattr(new, "session_id", None)
            await drv._tune_perception()
        return _reconnect

    async def cleanup(self) -> None:
        # The driver owns session teardown (stop() quits it); nothing extra here.
        self._driver = None
