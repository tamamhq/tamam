"""Subscription device clouds (LambdaTest, BrowserStack, Sauce Labs) behind one Appium hub,
plus their device catalog and app deletion. Building the session is blocking, so it runs in a thread."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from tamam.config import get_settings
from tamam.executors.base import ExecutionSpec, RunServices, record_upload
from tamam.executors.ios.driver import AppiumDriver
from tamam.executors.ios.providers.common import apply_alert_policy

logger = logging.getLogger(__name__)


# Uploaded-build cache: (vendor, abspath, size, mtime) → vendor app ref. Skips the
# slow re-upload of an unchanged .ipa on every cloud run.
_APP_UPLOAD_CACHE: dict[tuple, str] = {}

# Cache of the vendor's iOS device catalog: (vendor) → (fetched_at, devices). The
# catalog changes rarely (new OS a few times a year); refetch every few hours.
_DEVICE_CATALOG_CACHE: dict[str, tuple[float, list[dict]]] = {}
_DEVICE_CATALOG_TTL_S = 6 * 3600

# Fallback catalog when live vendor creds/API are unavailable (e.g. local dev): a
# small, current set so the picker still works. Prod queries the live list.


_FALLBACK_IOS_DEVICES = [
    {"device": "iPhone 17 Pro", "versions": ["26"]},
    {"device": "iPhone 17", "versions": ["26"]},
    {"device": "iPhone 15 Pro Max", "versions": ["26", "17"]},
    {"device": "iPhone 15", "versions": ["26", "17"]},
    {"device": "iPhone 14", "versions": ["26", "16"]},
]


def _os_version_key(v: str) -> tuple:
    """Sort key for an iOS version string ('26', '17.5') — numeric, newest first."""
    parts = []
    for chunk in str(v).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


async def list_devices(vendor: str | None = None) -> list[dict]:
    """The vendor's real iOS devices, grouped as {device, versions[]} and ordered
    newest-OS-first — the source of truth for the test form's device/OS picker, so
    a customer can only pick a device+OS the farm actually has (the fix for
    'iPhone 14 has OS lower than the app's required 26.0': we surface iOS 26 hosts).

    Cached for a few hours. Falls back to a small current set if the vendor API or
    creds are unavailable (local dev), so the picker never renders empty.
    """
    import time

    s = get_settings()
    vendor = vendor or s.ios_cloud
    cached = _DEVICE_CATALOG_CACHE.get(vendor)
    if cached and (time.time() - cached[0]) < _DEVICE_CATALOG_TTL_S:
        return cached[1]

    devices = await _fetch_ios_devices(vendor, s) or list(_FALLBACK_IOS_DEVICES)
    _DEVICE_CATALOG_CACHE[vendor] = (time.time(), devices)
    return devices


async def _fetch_ios_devices(vendor: str, s) -> list[dict] | None:  # noqa: ANN001
    """Live query of the vendor's iOS catalog. Returns None on any failure (no
    creds, network, unsupported vendor) so the caller can fall back."""
    if vendor != "browserstack":
        return None  # only BrowserStack implemented; others fall back for now
    if not (s.cloud_username and s.cloud_access_key):
        return None
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20, auth=(s.cloud_username, s.cloud_access_key)) as c:
            r = await c.get("https://api-cloud.browserstack.com/app-automate/devices.json")
            r.raise_for_status()
            raw = r.json()
    except Exception as exc:
        logger.warning("could not fetch %s device catalog: %s", vendor, exc)
        return None

    by_device: dict[str, set[str]] = {}
    for d in raw:
        if d.get("os") != "ios":
            continue
        name, ver = d.get("device"), str(d.get("os_version", ""))
        if name and ver:
            by_device.setdefault(name, set()).add(ver)
    devices = [
        {"device": name, "versions": sorted(vers, key=_os_version_key, reverse=True)}
        for name, vers in by_device.items()
    ]
    # Newest OS first, then device name — the picker defaults to devices[0].
    devices.sort(key=lambda e: (_os_version_key(e["versions"][0]), e["device"]), reverse=True)
    return devices


async def delete_app(vendor: str, app_ref: str) -> None:
    """Delete a previously-uploaded build from a device-farm vendor (right-to-
    deletion). Uses the currently-configured cloud creds. Raises on failure so the
    caller can log which vendor copy could not be purged.

    `app_ref` is the reference we stored at upload time: bs://ID, lt://ID, storage:ID,
    or (AWS Device Farm) an app upload ARN.
    """
    s = get_settings()
    if vendor == "aws_device_farm":
        # Different creds + SDK (boto3), not the httpx/cloud path below.
        import boto3
        if not (s.aws_access_key_id and s.aws_secret_access_key):
            raise RuntimeError(f"no AWS creds configured to delete Device Farm app {app_ref}")
        df = boto3.client("devicefarm", region_name=s.aws_region or "us-west-2",
                          aws_access_key_id=s.aws_access_key_id,
                          aws_secret_access_key=s.aws_secret_access_key)
        try:
            await asyncio.to_thread(df.delete_upload, arn=app_ref)
        except df.exceptions.NotFoundException:
            pass  # already gone → idempotent
        return
    import httpx

    user, key = s.cloud_username, s.cloud_access_key
    if not (user and key):
        raise RuntimeError(f"no cloud creds configured to delete {vendor} app {app_ref}")
    auth = (user, key)
    async with httpx.AsyncClient(timeout=60, auth=auth) as c:
        if vendor == "browserstack":
            app_id = app_ref.split("bs://", 1)[-1]
            r = await c.delete(
                f"https://api-cloud.browserstack.com/app-automate/app/delete/{app_id}")
        elif vendor == "sauce":
            file_id = app_ref.split("storage:", 1)[-1]
            r = await c.delete(
                f"https://api.{s.sauce_region}.saucelabs.com/v1/storage/files/{file_id}")
        elif vendor == "lambdatest":
            app_id = app_ref.split("lt://", 1)[-1]
            r = await c.delete(
                f"https://manual-api.lambdatest.com/app/delete/{app_id}")
        else:
            raise RuntimeError(f"unknown vendor {vendor!r}")
        # 404 == already gone → treat as success (idempotent deletion).
        if r.status_code not in (200, 202, 204, 404):
            r.raise_for_status()


class CloudProvider:
    """Real devices via a vendor-agnostic Appium cloud — LambdaTest (cheapest / free
    trial), BrowserStack, or Sauce. All speak Appium, so our AppiumDriver is unchanged;
    only the hub URL, the capabilities namespace, and the app-upload API differ.

    This is also the "test your TestFlight build" path: hand us the .ipa your CI ships
    to TestFlight and we upload it to the vendor + run it on a real device.

    Config: TAMAM_IOS_CLOUD (vendor) + TAMAM_CLOUD_USERNAME / TAMAM_CLOUD_ACCESS_KEY.
    """

    def __init__(self, services: RunServices | None = None) -> None:
        s = get_settings()
        self._services = services
        self._vendor = s.ios_cloud
        self._user = s.cloud_username
        self._key = s.cloud_access_key
        self._region = s.sauce_region
        self._driver: AppiumDriver | None = None

    def _hub(self) -> str:
        return {
            "lambdatest": "https://mobile-hub.lambdatest.com/wd/hub",
            "browserstack": "https://hub-cloud.browserstack.com/wd/hub",
            "sauce": f"https://ondemand.{self._region}.saucelabs.com/wd/hub",
        }[self._vendor]

    def _opts_key(self) -> str:
        return {"lambdatest": "lt:options", "browserstack": "bstack:options",
                "sauce": "sauce:options"}[self._vendor]

    def _configure_vendor(self, opts, spec) -> None:  # noqa: ANN001
        """Per-vendor capabilities — each cloud validates its own options schema
        strictly, so the exact keys and device/version placement differ. deviceName
        is required by the clouds, so default one when the test didn't specify it."""
        device = (spec.app.device or "").strip() or "iPhone 15"
        # Per-test OS version wins (chosen from the device/OS picker), then the server
        # default, then a current fallback. The old "16" default silently ran apps on
        # an OS below their minimum ("iPhone 14 has OS lower than required 26.0").
        version = (
            (spec.app.os_version or "").strip()
            or get_settings().ios_platform_version
            or "26"
        )
        if self._vendor == "browserstack":
            opts.set_capability("bstack:options", {
                "userName": self._user, "accessKey": self._key,
                "deviceName": device, "osVersion": version,
                "projectName": "tamam", "buildName": "tamam", "sessionName": "ios run",
            })
        elif self._vendor == "lambdatest":
            opts.set_capability("lt:options", {
                "user": self._user, "accessKey": self._key, "isRealMobile": True,
                "deviceName": device, "platformVersion": version,
                "build": "tamam", "name": "ios run",
            })
        else:  # sauce — device/version are standard appium caps; sauce:options is creds only
            if device:
                opts.device_name = device
            if version:
                opts.platform_version = version
            opts.set_capability("sauce:options",
                                {"username": self._user, "accessKey": self._key,
                                 "name": "ios run", "build": "tamam"})

    async def _upload_app(self, path: str) -> str:
        """Upload a local .ipa to the vendor's app storage; return its app reference.
        A vendor URL/reference (lt://, bs://, storage:, http) is passed through.

        Uploads are cached by (vendor, path, size, mtime) so re-running the same build
        skips the (slow) re-upload — the main source of cloud-run 'hang' at start."""
        if path.startswith(("lt://", "bs://", "storage:", "http://", "https://")):
            return path
        import httpx

        from tamam.storage import get_storage

        # `path` is a storage KEY — fetch the .ipa bytes from the (private) store so
        # this works from a worker that never received the upload on local disk.
        cache_key = (self._vendor, path)
        if cache_key in _APP_UPLOAD_CACHE:
            return _APP_UPLOAD_CACHE[cache_key]

        data = await get_storage().get_bytes(path)
        name = Path(path).name
        auth = (self._user or "", self._key or "")
        async with httpx.AsyncClient(timeout=600) as c:
            if self._vendor == "lambdatest":
                r = await c.post("https://manual-api.lambdatest.com/app/upload/realDevice",
                                 auth=auth, files={"appFile": (name, data)}, data={"name": name})
                r.raise_for_status()
                app_ref = r.json()["app_url"]                   # lt://APP_ID
            elif self._vendor == "browserstack":
                r = await c.post("https://api-cloud.browserstack.com/app-automate/upload",
                                 auth=auth, files={"file": (name, data)})
                r.raise_for_status()
                app_ref = r.json()["app_url"]                   # bs://APP_ID
            else:
                r = await c.post(f"https://api.{self._region}.saucelabs.com/v1/storage/upload",
                                 auth=auth, files={"payload": (name, data)}, data={"name": name})
                r.raise_for_status()
                app_ref = f"storage:{r.json()['item']['id']}"
        _APP_UPLOAD_CACHE[cache_key] = app_ref
        return app_ref

    async def _fetch_video(self, session_id: str) -> str | None:
        """Resolve the vendor's session recording. Preferred: download it into a
        local file so it flows into our private storage (durable, tenant-scoped).
        Fallback: if the download fails, return the vendor's (presigned) URL to
        reference directly. Best effort — returns None only if there's no video."""
        import httpx

        auth = (self._user or "", self._key or "")
        # The BrowserStack *API* call needs Basic auth to look up the session.
        async with httpx.AsyncClient(timeout=120, auth=auth) as api:
            video_url: str | None = None
            if self._vendor == "browserstack":
                for _ in range(6):  # video finalizes a few seconds after the session ends
                    r = await api.get(
                        f"https://api-cloud.browserstack.com/app-automate/sessions/{session_id}.json")
                    if r.status_code == 200:
                        video_url = (r.json().get("automation_session") or {}).get("video_url")
                        if video_url:
                            break
                    await asyncio.sleep(3)
            elif self._vendor == "sauce":
                # Sauce's URL is a test page, not a direct asset — pass it through.
                return f"https://app.{self._region}.saucelabs.com/tests/{session_id}"
        if not video_url:
            return None

        # The video_url is a PRESIGNED S3 URL — its auth is in the query string.
        # Do NOT send our vendor Basic-auth header here: an extra Authorization on a
        # presigned request makes S3 reject it (AccessDenied), and we'd save the
        # error XML as the "recording". Download with a clean, unauthenticated client.
        dest = Path(get_settings().artifacts_dir) / "_staging" / f"{session_id}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=300, follow_redirects=True) as dl:
                async with dl.stream("GET", video_url) as resp:
                    resp.raise_for_status()
                    ctype = resp.headers.get("content-type", "")
                    if "xml" in ctype or "json" in ctype or ctype.startswith("text/"):
                        raise ValueError(f"unexpected content-type {ctype!r} (not a video)")
                    size = 0
                    with open(dest, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            f.write(chunk)
            # A real recording is not tiny; a few hundred bytes is an error blob.
            if size < 1024 or dest.read_bytes()[:1].startswith(b"<"):
                raise ValueError(f"downloaded {size} bytes — looks like an error, not a video")
            return str(dest)
        except Exception as exc:
            logger.warning("recording download failed (%s) — referencing vendor URL", exc)
            dest.unlink(missing_ok=True)
            return video_url  # fallback: reference the presigned vendor URL directly

    async def driver(self, spec: ExecutionSpec) -> AppiumDriver:
        if not (self._user and self._key):
            raise RuntimeError(
                f"{self._vendor} creds missing — set TAMAM_CLOUD_USERNAME / TAMAM_CLOUD_ACCESS_KEY")
        from appium import webdriver
        from appium.options.ios import XCUITestOptions

        raw_ref = (spec.app.build_ref or "").strip()
        app_ref = await self._upload_app(raw_ref) if raw_ref else None
        # Record what we put on the vendor so it can be deleted later (right-to-
        # deletion) and so a worker restart doesn't re-upload. Best-effort: never
        # let bookkeeping break a run. Skip pass-through refs (already a vendor URL)
        # and legacy inline paths with no owning Build.
        if app_ref and app_ref != raw_ref:
            await record_upload(self._services, spec.app.build_id, self._vendor, app_ref)

        opts = XCUITestOptions()
        opts.platform_name = "iOS"
        opts.automation_name = "XCUITest"
        bundle = (spec.app.bundle_id or "").strip()
        if app_ref:
            opts.app = app_ref
        if bundle:
            opts.bundle_id = bundle
        apply_alert_policy(opts)
        # Isolation + hygiene on SHARED cloud devices. Default: request a clean start.
        # NOTE: BrowserStack neutralizes Appium's fullReset (it rewrites it to noReset
        # + its own customFullReset, which reinstalls the app but does NOT clear the
        # iOS Keychain) — so the RELIABLE clear is the teardown uninstall below, which
        # deletes the app and (iOS 10.3+) its keychain. fullReset is still requested as
        # a best-effort fresh install. Opt into reuse_session for "log in once" flows.
        if spec.app.reuse_session:
            opts.set_capability("noReset", True)
        else:
            opts.set_capability("fullReset", True)
        self._configure_vendor(opts, spec)

        session = await asyncio.to_thread(webdriver.Remote, self._hub(), options=opts)
        run_dir = Path(get_settings().artifacts_dir) / str(uuid.uuid4())
        self._driver = AppiumDriver(session, run_dir, remote=True)  # vendor records/streams
        self._driver.remote_video_fetcher = self._fetch_video  # pull the recording after run
        # Unless the test wants state reused, uninstall the app at teardown so no
        # authenticated session lingers on the shared device. The bundle is resolved at
        # runtime from the session caps if the test didn't set one (build-based tests).
        if not spec.app.reuse_session:
            self._driver.teardown_uninstall = True
            self._driver.teardown_uninstall_bundle = bundle or None
        return self._driver

    async def cleanup(self) -> None:
        self._driver = None
