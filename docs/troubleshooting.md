# Troubleshooting

Start with `uv run tamam doctor` (inside `backend/`): it checks each requirement and prints the
fix for anything missing.

**`uv run tamam run` hangs for a minute or two on the first run.**
Appium is building WebDriverAgent, the helper app that drives the simulator. It happens once
per Xcode version.

**"Could not connect to Appium" / "Appium server running ✗".**
Start `appium` in a second terminal and leave it running. If you use a different port, set
`TAMAM_APPIUM_SERVER_URL`.

**"No '…' simulator on this Mac".**
Pick one of the listed models with `--device`, or install a runtime in Xcode › Settings ›
Components.

**WebDriverAgent fails to build (`xcodebuild failed with code 65`).**
Open Xcode once to accept its licence and install components, then
`sudo xcode-select -s /Applications/Xcode.app` and try again.

**The run ends INCONCLUSIVE.**
That is Tamam refusing to guess: the agent could not prove every part of the test from the
screen. The reason line says which part. Re-run with `--verbose`, or make the test more
specific (name the exact button or value to check).
See [verdicts and trust](guide/verdicts-and-trust.md).

**"The demo app isn't built yet".**
Run `./fixtures/ios/build.sh` from the repository root (`../fixtures/ios/build.sh` from
`backend/`).

**OCR errors mentioning tesseract.**
`brew install tesseract`. Tamam reads text the accessibility tree doesn't expose.

**Something else.** Open an issue with the output of `tamam doctor` and of the failing run with
`--verbose`. Remove any credentials from the output first.
