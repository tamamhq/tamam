"""Device catalogs and farm app copies, for the API — without the API importing a platform.

Each platform registers a module offering `list_devices()` (the farm's devices and OS versions,
for the test form's picker) and `delete_app(vendor, app_ref)` (purge an uploaded build's copy)."""

from __future__ import annotations

from typing import Any

from tamam.plugins import Registry

CATALOGS: Registry = Registry("device_catalogs")


def _ios() -> Any:
    from tamam.executors.ios.providers import cloud
    return cloud


CATALOGS.register("ios", _ios)


async def list_devices(platform: str = "ios") -> list[dict]:
    return await CATALOGS.create(platform).list_devices()


async def delete_uploaded_app(vendor: str, app_ref: str, platform: str = "ios") -> None:
    await CATALOGS.create(platform).delete_app(vendor, app_ref)
