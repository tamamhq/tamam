# Setup

The [README quick start](../README.md#quick-start) gets the demo app running. This page covers
testing your own app and the settings you may want to change.

## Your own app

Tamam needs a **simulator build** of your app (a `.app` folder), not an `.ipa` for devices:

```bash
xcodebuild -scheme YourApp -sdk iphonesimulator -configuration Debug \
  -derivedDataPath build build
# → build/Build/Products/Debug-iphonesimulator/YourApp.app
```

Then write the test the way you would explain it to a colleague:

```bash
cd backend
uv run tamam run --app ../build/Build/Products/Debug-iphonesimulator/YourApp.app \
  --bundle com.you.yourapp \
  "Open the Profile tab, change the display name to Sam, and verify the header shows Sam"
```

Good tests name the screens and buttons as a user sees them and end with something checkable
("verify the total is $35"). See [how a run works](guide/how-a-run-works.md) for what the
agent does with them.

Useful flags: `--device "iPhone 17 Pro"` picks the simulator (default: an installed iPhone),
`--max-steps 40` allows longer flows (default 30), `--json` prints a machine-readable result,
`--verbose` shows the harness logs. `uv run tamam run --help` lists the demo test ids.
Exit codes: `0` PASS, `1` FAIL, `2` inconclusive, `3` a setup or usage problem.

## Settings

Tamam reads settings from environment variables prefixed `TAMAM_` (or a `backend/.env`
file; copy [`backend/.env.example`](../backend/.env.example)). You only need
`ANTHROPIC_API_KEY` to start. Every setting is listed in
[configuration](guide/configuration.md).

## Real devices and device farms

A physical iPhone attached to your Mac, or a cloud device farm (AWS Device Farm, BrowserStack,
LambdaTest, Sauce Labs), works too. Both need extra signing setup: see [devices](guide/devices.md).

## The dashboard (optional)

A web dashboard for managing tests, builds and run history: see [dashboard](dashboard.md).
