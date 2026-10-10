# Devices

The iOS agent drives devices through **Appium** with the **XCUITest** driver
(WebDriverAgent on the device). Where that device lives is a *provider*
(`executors/ios/providers/`). The agent, perceiver,
hooks and replay are identical on every provider — only how the session is opened differs.

Choose with `TAMAM_IOS_BACKEND`:

| Backend | Provider | Use it for |
|---|---|---|
| `simulator` (default) | `SimulatorProvider` | local development and the eval suite |
| `device` | `LocalDeviceProvider` | a physical iPhone plugged into this Mac |
| `cloud` | `CloudProvider` / `AWSDeviceFarmProvider` | real devices in a farm, from a server |

## Local simulator

A local Appium server (`TAMAM_APPIUM_SERVER_URL`, default `http://127.0.0.1:4723`) and an iOS
Simulator runtime; Appium boots the simulator. The app is a simulator build (`.app`). Setup steps:
[setup](../setup.md).

- Screen recording uses `xcrun simctl`.
- `TAMAM_IOS_SIM_LAUNCH_ARGS` passes launch arguments to the app (e.g. a test-only flag).
- `TAMAM_IOS_WAIT_FOR_IDLE_S` tunes how long XCUITest waits for the app to go idle per query.

## Physical device on this Mac

`TAMAM_IOS_BACKEND=device` with `TAMAM_IOS_DEVICE_UDID`. WebDriverAgent must be signed for the
device: set `TAMAM_IOS_TEAM_ID` (your Apple Team ID) and a unique `TAMAM_WDA_BUNDLE_ID`. The app
must be a device build (`.ipa`) signed for that device.

## Cloud device farms

`TAMAM_IOS_BACKEND=cloud` and `TAMAM_IOS_CLOUD` picks the vendor.

**Subscription clouds** — `lambdatest`, `browserstack`, `sauce` (`CloudProvider`). All speak
Appium; only the hub URL, the capability names and the app-upload API differ. Credentials:
`TAMAM_CLOUD_USERNAME`, `TAMAM_CLOUD_ACCESS_KEY` (+ `TAMAM_SAUCE_REGION`). Uploads are cached by
build, so re-running the same build skips the slow upload. The session video is copied into
our own storage on BrowserStack; other vendors may return a link to the vendor's copy.

**AWS Device Farm** — `aws_device_farm` (`AWSDeviceFarmProvider`). Pay per device-minute
(1,000 free minutes), no subscription. Device Farm publishes a per-session Appium endpoint:

```
upload the .ipa → create a remote-access session on a device → wait until RUNNING
  → drive its Appium endpoint (same driver as every other backend) → stop the session
```

- Credentials are passed explicitly (`TAMAM_AWS_ACCESS_KEY_ID`, `TAMAM_AWS_SECRET_ACCESS_KEY`),
  never from the ambient `AWS_*` environment, so they can't leak into other AWS calls.
- `TAMAM_AWS_DEVICEFARM_PROJECT_ARN` (required), `TAMAM_AWS_REGION` (Device Farm is `us-west-2`),
  `TAMAM_AWS_DEVICEFARM_APPIUM_VERSION`, `TAMAM_AWS_DEVICEFARM_MIN_IOS` (lowest iOS version to pick).
- App uploads are cached per build; sessions are always stopped in cleanup.
- **Billing:** measure cost with the session's `deviceMinutes.metered` (what is billed), not
  `deviceMinutes.total`.

Per test you can choose the device model and iOS version (`device`, `ios_platform_version`),
override the server's backend (`ios_target`), and keep the app installed between runs
(`reuse_session`, e.g. to stay logged in; otherwise the app is removed at teardown on cloud
devices). Deleting a build also removes the copies uploaded to vendors.

## Session recovery

Whatever the provider, the run's `DeviceSession` (`executors/ios/session.py`) handles a
wedged read or a dropped Appium session by reconnecting, up to
`TAMAM_MAX_SESSION_RECONNECTS` times per run. Command and read timeouts:
`TAMAM_DRIVER_COMMAND_TIMEOUT_S`, `TAMAM_DRIVER_READ_TIMEOUT_S`.
