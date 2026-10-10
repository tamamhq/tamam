"""Live smoke: the iOS perceiver parses a real Simulator's accessibility source.
Opt in with TAMAM_RUN_IOS_TESTS=1 (needs a running Appium server + a booted app).

Env:
  TAMAM_IOS_APP     path to the simulator .app
  TAMAM_IOS_BUNDLE  app bundle id (optional)
  TAMAM_IOS_DEVICE  simulator device name (default "iPhone 15")
"""

import os

import pytest

from tamam.executors.base import AppTarget

pytestmark = pytest.mark.skipif(
    not os.environ.get("TAMAM_RUN_IOS_TESTS"),
    reason="needs Appium + iOS Simulator; opt in with TAMAM_RUN_IOS_TESTS=1",
)


async def test_launch_observe_screenshot():
    from tamam.executors.base import ExecutionSpec
    from tamam.executors.ios.perceiver import IOSPerceiver
    from tamam.executors.ios.providers.simulator import SimulatorProvider

    spec = ExecutionSpec(instructions="smoke", target=AppTarget(build_ref=os.environ.get("TAMAM_IOS_APP"), bundle_id=os.environ.get("TAMAM_IOS_BUNDLE"), device=os.environ.get("TAMAM_IOS_DEVICE", "iPhone 15")))
    provider = SimulatorProvider()
    driver = await provider.driver(spec)
    await driver.start()
    try:
        space = await IOSPerceiver().observe(driver)
        assert space.screen_w > 0 and space.screen_h > 0
        assert space.elements, "perceiver found no on-screen elements"
        shot = await driver.screenshot()
        assert os.path.exists(shot)
    finally:
        await driver.stop()
        await provider.cleanup()
