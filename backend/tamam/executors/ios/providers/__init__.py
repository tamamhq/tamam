"""Which device an iOS run uses — one provider per kind, chosen from the run's target and
settings. A new device farm is one more provider registered here (or from a plugin, entry-point
group `tamam.ios_device_providers`)."""

from __future__ import annotations

from typing import Any

from tamam.config import get_settings
from tamam.executors.base import ExecutionSpec, RunServices
from tamam.plugins import Registry

DEVICE_PROVIDERS: Registry = Registry("ios_device_providers")


def _simulator(services: RunServices | None) -> Any:
    from tamam.executors.ios.providers.simulator import SimulatorProvider
    return SimulatorProvider(services)


def _local_device(services: RunServices | None) -> Any:
    from tamam.executors.ios.providers.local_device import LocalDeviceProvider
    return LocalDeviceProvider(services)


def _cloud(services: RunServices | None) -> Any:
    from tamam.executors.ios.providers.cloud import CloudProvider
    return CloudProvider(services)


def _aws_device_farm(services: RunServices | None) -> Any:
    from tamam.executors.ios.providers.aws_device_farm import AWSDeviceFarmProvider
    return AWSDeviceFarmProvider(services)


DEVICE_PROVIDERS.register("simulator", _simulator)
DEVICE_PROVIDERS.register("device", _local_device)
DEVICE_PROVIDERS.register("cloud", _cloud)                       # LambdaTest, BrowserStack, Sauce
DEVICE_PROVIDERS.register("aws_device_farm", _aws_device_farm)   # managed Appium endpoint


def provider_name(spec: ExecutionSpec) -> str:
    """The run's own backend (chosen per test) wins over the server default. AWS Device Farm is
    a "cloud" backend but its own provider (remote-access sessions, not a fixed hub)."""
    s = get_settings()
    backend = spec.app.backend or s.ios_backend
    if backend == "cloud" and s.ios_cloud in DEVICE_PROVIDERS:
        return s.ios_cloud
    return backend if backend in DEVICE_PROVIDERS else "simulator"


def build_provider(spec: ExecutionSpec, services: RunServices | None = None) -> Any:
    return DEVICE_PROVIDERS.create(provider_name(spec), services)
