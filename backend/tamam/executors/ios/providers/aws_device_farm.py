"""AWSDeviceFarmProvider — real iOS devices via AWS Device Farm's managed Appium endpoint.

Unlike the subscription clouds (BrowserStack/LambdaTest/Sauce, see CloudProvider), Device
Farm is pay-per-minute ($0.17/device-min, 1000 free) and exposes Appium differently: you
open a *remote-access session* on a device, which publishes a per-session Appium server URL
you then drive like any other hub. Flow (all boto3, us-west-2 only):

    createUpload(IOS_APP) → PUT the .ipa to the presigned URL → poll getUpload=SUCCEEDED
      → createRemoteAccessSession(projectArn, deviceArn, appArn, interactionMode)
      → poll getRemoteAccessSession until status=RUNNING and `endpoint` is populated
      → webdriver.Remote(endpoint, XCUITest caps)   ← same AppiumDriver as every other backend
      → stopRemoteAccessSession on cleanup

Behind the same DeviceProvider seam, so the executor/perceiver/loop are unchanged. Creds are
passed EXPLICITLY to the client (never the ambient AWS_* env) so they can't leak into other
AWS calls in the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from tamam.config import get_settings
from tamam.executors.base import ExecutionSpec, RunServices, record_upload
from tamam.executors.ios.driver import AppiumDriver

logger = logging.getLogger(__name__)

# path (storage key) -> Device Farm app ARN, so re-running a build skips the re-upload.
_APP_ARN_CACHE: dict[str, str] = {}


class AWSDeviceFarmProvider:
    def __init__(self, services: RunServices | None = None) -> None:
        s = get_settings()
        self._services = services
        self._region = s.aws_region or "us-west-2"
        self._key = s.aws_access_key_id
        self._secret = s.aws_secret_access_key
        self._project = s.aws_devicefarm_project_arn
        self._min_ios = s.aws_devicefarm_min_ios
        self._client: Any = None
        self._driver: AppiumDriver | None = None
        self._session_arn: str | None = None
        self._stopped = False  # so we stop the session exactly once (fetch-video vs cleanup)

    def _df(self) -> Any:
        """Lazy boto3 client with EXPLICIT creds (not ambient AWS_* env)."""
        if self._client is None:
            import boto3
            self._client = boto3.client(
                "devicefarm", region_name=self._region,
                aws_access_key_id=self._key, aws_secret_access_key=self._secret,
            )
        return self._client

    async def _call(self, op: str, **kw: Any) -> Any:
        """Run a blocking boto3 op in a thread."""
        return await asyncio.to_thread(lambda: getattr(self._df(), op)(**kw))

    async def _pick_device(self) -> str:
        """Pick an iOS-26+ phone, re-checking availability EACH run (not a pinned unit):
        requesting one specific busy device makes the session queue ("Pending Device"),
        so we pick a currently-available one. Ranking: most-available first, then a Pro
        model (fewer contended, the stated default), then newest OS. AWS suggests avoiding
        a single pinned device for exactly this reason."""
        devs = (await self._call("list_devices")).get("devices", [])

        def os_num(d: dict) -> float:
            try:
                return float(".".join((d.get("os") or "0").split(".")[:2]))
            except ValueError:
                return 0.0

        def avail_rank(d: dict) -> int:
            return {"HIGHLY_AVAILABLE": 2, "AVAILABLE": 1}.get(d.get("availability"), 0)

        cand = [
            d for d in devs
            if d.get("platform") == "IOS" and d.get("formFactor") == "PHONE"
            and os_num(d) >= self._min_ios
        ]
        if not cand:
            raise RuntimeError(
                f"no iOS phone with OS >= {self._min_ios} in Device Farm")
        cand.sort(
            key=lambda d: (avail_rank(d), "pro" in (d.get("name", "").lower()), os_num(d)),
            reverse=True,
        )
        pick = cand[0]
        logger.info("Device Farm device: %s (iOS %s, availability=%s)",
                    pick.get("name"), pick.get("os"), pick.get("availability"))
        return pick["arn"]

    async def _upload_app(self, path: str) -> tuple[str, str | None]:
        """Upload the .ipa to Device Farm; return (app_arn, bundle_id_from_metadata).
        Cached by storage key so re-runs skip the (slow) upload."""
        if path in _APP_ARN_CACHE:
            return _APP_ARN_CACHE[path], None
        import httpx

        from tamam.storage import get_storage

        data = await get_storage().get_bytes(path)
        name = Path(path).name if Path(path).suffix else f"{Path(path).name}.ipa"
        up = (await self._call("create_upload", projectArn=self._project, name=name,
                               type="IOS_APP", contentType="application/octet-stream"))["upload"]
        arn, url = up["arn"], up["url"]
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.put(url, content=data,
                            headers={"Content-Type": "application/octet-stream"})
            r.raise_for_status()
        bundle = await self._await_upload(arn)
        _APP_ARN_CACHE[path] = arn
        return arn, bundle

    async def _await_upload(self, arn: str, timeout_s: float = 300) -> str | None:
        """Poll getUpload until SUCCEEDED; return the app's bundle id from metadata if present."""
        for _ in range(int(timeout_s // 3)):
            up = (await self._call("get_upload", arn=arn))["upload"]
            status = up.get("status")
            if status == "SUCCEEDED":
                meta = up.get("metadata")
                if meta:
                    try:
                        m = json.loads(meta)
                        for k in ("bundleId", "CFBundleIdentifier", "bundle_id", "identifier"):
                            if m.get(k):
                                return m[k]
                    except (ValueError, TypeError):
                        pass
                return None
            if status in ("FAILED", "ERRORED"):
                raise RuntimeError(f"Device Farm app upload {status}: {up.get('message')}")
            await asyncio.sleep(3)
        raise RuntimeError("Device Farm app upload did not finish in time")

    async def _await_endpoint(self, arn: str, timeout_s: float = 300) -> str:
        """Poll the remote-access session until it's RUNNING with an Appium endpoint URL.
        The WebDriver URL is `endpoints.remoteDriverEndpoint` (an https:// URL) — NOT the
        top-level `endpoint` field, which is the wss:// interactive-console/video socket."""
        for _ in range(int(timeout_s // 3)):
            sess = (await self._call("get_remote_access_session", arn=arn))["remoteAccessSession"]
            status = sess.get("status")
            endpoint = (sess.get("endpoints") or {}).get("remoteDriverEndpoint")
            if endpoint and status == "RUNNING":
                return endpoint
            if status in ("FAILED", "ERRORED", "STOPPING", "COMPLETED"):
                raise RuntimeError(
                    f"remote-access session {status}: {sess.get('message')}")
            await asyncio.sleep(3)
        raise RuntimeError("Device Farm session did not expose an Appium endpoint in time")

    async def driver(self, spec: ExecutionSpec) -> AppiumDriver:
        if not (self._key and self._secret and self._project):
            raise RuntimeError(
                "AWS Device Farm not configured — set TAMAM_AWS_ACCESS_KEY_ID / "
                "TAMAM_AWS_SECRET_ACCESS_KEY / TAMAM_AWS_DEVICEFARM_PROJECT_ARN")
        from appium import webdriver
        from appium.options.ios import XCUITestOptions

        raw_ref = (spec.app.build_ref or "").strip()
        app_arn, meta_bundle = (await self._upload_app(raw_ref)) if raw_ref else (None, None)
        # Record what we put on Device Farm so build-deletion can purge it (right-to-
        # deletion), mirroring the other vendors. Best-effort — never break a run on it.
        if app_arn:
            await record_upload(self._services, spec.app.build_id, "aws_device_farm", app_arn)
        device_arn = await self._pick_device()

        sess = (await self._call(
            "create_remote_access_session",
            projectArn=self._project, deviceArn=device_arn,
            name="tamam", interactionMode="INTERACTIVE",
            skipAppResign=False, **({"appArn": app_arn} if app_arn else {}),
            **self._session_configuration(),
        ))["remoteAccessSession"]
        self._session_arn = sess["arn"]
        # From here on a session is billing device-minutes — if ANYTHING fails (endpoint
        # never ready, WDA/launch error), stop it immediately so it doesn't sit open until
        # the idle/150-min timeout and drain quota. (The executor's finally also calls
        # cleanup(); this is belt-and-suspenders so the provider never orphans a session.)
        try:
            return await self._connect(spec, app_arn, meta_bundle, XCUITestOptions, webdriver)
        except Exception:
            await self.cleanup()
            raise

    @staticmethod
    def _session_configuration() -> dict:
        """Pick the Appium server major version for the remote session. Unset, Device Farm
        uses 2 on iOS < 27, whose WebDriverAgent ignores settings the harness relies on
        (see config.aws_devicefarm_appium_version)."""
        version = (get_settings().aws_devicefarm_appium_version or "").strip()
        if not version:
            return {}
        return {"configuration": {"parameters": {"appium:version": version}}}

    async def _connect(self, spec, app_arn, meta_bundle, XCUITestOptions, webdriver):  # noqa: ANN001,N803
        endpoint = await self._await_endpoint(self._session_arn)

        opts = XCUITestOptions()
        opts.platform_name = "iOS"
        opts.automation_name = "XCUITest"
        # An app passed as appArn at session creation is auto-injected as the default
        # `appium:app`, so it installs+launches on connect — don't also set bundle_id
        # (avoids an app-vs-bundleId conflict). Only when NO app was uploaded (an
        # already-installed app) do we launch by bundle id.
        if not app_arn:
            bundle = (spec.app.bundle_id or "").strip() or (meta_bundle or "")
            if bundle:
                opts.bundle_id = bundle
        from tamam.executors.ios.providers.common import apply_alert_policy
        apply_alert_policy(opts)
        # noReset: Device Farm ALREADY installs the app (appArn) on a per-session clean
        # device, so isolation is handled at the farm level. Letting XCUITest ALSO run
        # its reset flow (remove + reinstall + immediate launch) triggers a FrontBoard
        # race — "Application ... is unknown to FrontBoard" — because the just-reinstalled
        # bundle isn't registered yet. So skip Appium's reinstall and just launch the
        # already-installed app.
        opts.set_capability("noReset", True)

        session = await asyncio.to_thread(webdriver.Remote, endpoint, options=opts)
        run_dir = Path(get_settings().artifacts_dir) / str(uuid.uuid4())
        self._driver = AppiumDriver(session, run_dir, remote=True)

        # Reconnect hook: the remote-access session (and its endpoint) outlives a WDA/app
        # crash, so on a dead session we just open a NEW Appium session on the SAME
        # endpoint — no re-provisioning, no new device-minutes for setup. Relaunches the app.
        async def _reconnect() -> None:
            logger.warning("Device Farm session died — reconnecting on the same endpoint")
            new = await asyncio.to_thread(webdriver.Remote, endpoint, options=opts)
            self._driver._d = new
            self._driver.session_id = getattr(new, "session_id", None)
            await self._driver._tune_perception()

        self._driver._reconnect = _reconnect
        # Pull the session recording after the run (DF records INTERACTIVE sessions). The
        # driver calls this in stop() after the Appium session quits.
        self._driver.remote_video_fetcher = self._fetch_video
        return self._driver

    async def _stop_session(self) -> None:
        """Stop the remote-access session exactly once (stops the device-minute meter)."""
        if self._session_arn and not self._stopped:
            self._stopped = True
            try:
                await self._call("stop_remote_access_session", arn=self._session_arn)
            except Exception as exc:
                logger.warning("could not stop Device Farm session: %s", exc)

    @staticmethod
    def _video_url(artifacts: list[dict]) -> str | None:
        vids = [a for a in artifacts if a.get("type") == "VIDEO" and a.get("url")]
        return vids[-1]["url"] if vids else None

    async def _fetch_video(self, _session_id: Any = None) -> str | None:
        """Stop the session so DF finalizes artifacts, poll for the VIDEO, and download it
        into local artifacts (durable). Best-effort — returns None (or the presigned URL)
        rather than ever breaking teardown."""
        if not self._session_arn:
            return None
        await self._stop_session()
        url = None
        for _ in range(30):  # ~90s for the recording to finalize after the session ends
            try:
                arts = (await self._call("list_artifacts", arn=self._session_arn,
                                         type="FILE")).get("artifacts", [])
            except Exception:
                arts = []
            url = self._video_url(arts)
            if url:
                break
            await asyncio.sleep(3)
        if not url:
            logger.info("no Device Farm video artifact found for the session")
            return None
        import httpx
        dest = Path(get_settings().artifacts_dir) / f"{uuid.uuid4()}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=300, follow_redirects=True) as c:
                async with c.stream("GET", url) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        async for chunk in r.aiter_bytes():
                            f.write(chunk)
            return str(dest)
        except Exception as exc:
            logger.warning("Device Farm video download failed (%s); referencing URL", exc)
            dest.unlink(missing_ok=True)
            return url

    async def cleanup(self) -> None:
        await self._stop_session()  # idempotent; the video fetch may have already stopped it
        self._session_arn = None
        self._driver = None
