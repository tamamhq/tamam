"""What every iOS device provider shares: resolving a build to a local file, and the
WebDriverAgent capabilities set the same way everywhere."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from tamam.config import get_settings

logger = logging.getLogger(__name__)


async def resolve_local_app(ref: str) -> str:
    """Resolve a build reference to a local path Appium can install.

    - An existing local path (eval fixtures, dev) is used as-is.
    - Otherwise it's a storage KEY for an uploaded build: fetch the bytes into a
      temp file (extension preserved) so Appium can install/unzip it. This is what
      makes uploaded builds work on the local simulator now that the DB stores a
      storage key rather than a filesystem path."""
    ref = ref.strip()
    if not ref:
        return ref
    # Appium runs as a separate process, so any path we hand it MUST be absolute
    # (a relative path resolves against Appium's cwd, not ours).
    if Path(ref).exists():
        return str(Path(ref).resolve())
    from tamam.storage import get_storage

    data = await get_storage().get_bytes(ref)
    suffix = Path(ref).suffix or ".zip"
    dest = (Path(get_settings().artifacts_dir) / "_builds" / f"{uuid.uuid4()}{suffix}").resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return str(dest)


MJPEG_PORT = 9100


def apply_host_ports(opts: Any) -> int:
    """Set the local WebDriverAgent's host ports; returns the MJPEG port in use.

    wdaLocalPort is sent only when configured, so a single-session host keeps Appium's
    default (8100). Distinct ports let two sessions share one Mac without one WDA
    answering the other's commands."""
    s = get_settings()
    if s.ios_wda_local_port:
        opts.set_capability("wdaLocalPort", int(s.ios_wda_local_port))
    port = int(s.ios_mjpeg_server_port or MJPEG_PORT)
    opts.set_capability("mjpegServerPort", port)
    return port


def apply_alert_policy(opts: Any) -> None:
    """Alert handling for every provider. By default WebDriverAgent's autoAcceptAlerts
    stays OFF: it accepted the APP's own dialogs too (a "Couldn't save" error vanished
    before the agent could see it) and polled the app's tree every 2s. iOS permission
    prompts are instead surfaced by respectSystemAlerts (driver) and accepted by the
    perceiver, on the record. `ios_auto_accept_alerts` restores the old behaviour."""
    if get_settings().ios_auto_accept_alerts:
        opts.set_capability("autoAcceptAlerts", True)
