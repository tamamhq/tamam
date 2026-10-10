# Results

How often the verdict is right, measured on apps whose correct answer is known. Each suite mixes
tests that should pass with **negative controls**: tests that claim something the app does not
do, so a tester that passes everything scores badly.

These are our own suites, not a third-party benchmark.

| App | Kind | Runs | Right verdict | Inconclusive | Wrong verdict |
|---|---|---|---|---|---|
| Shoply (bundled) | SwiftUI, correct + buggy builds | 402 | 99% | 1% | 0 |
| Flutter `form_app` | Flutter (own renderer) | 8 | 8 | 0 | 0 |
| Wikipedia | large UIKit app, web-view articles, live network | 7 | 4 | 3 | 0 |

- **Shoply**: 402 saved runs of the Shoply suite, not counting one run interrupted by an API
  outage. Every miss was inconclusive, never a wrong verdict. One rare false pass is known and
  documented in [known limits](known-limits.md).
- **Flutter** (latest run): every test right, including the three negative controls. An earlier
  version passed a false claim that a switch was on; on/off claims are now checked against the
  app's own state.
- **Wikipedia** (latest run): the three inconclusive runs were the agent not finding the Saved
  tab, a run stopped for making no progress, and one step the agent could not back with
  on-screen evidence. All three negative controls were caught.
- **Wikipedia and Flutter are not bundled.** Build them yourself from their public repositories
  at the pinned commits below, then run the suite.

## Run it yourself

Shoply: see [evals and the regression gate](guide/evals-and-regression-gate.md).

Flutter (Flutter SDK 3.47):

```sh
git clone https://github.com/flutter/samples && cd samples && git checkout 63411527b7f617f1fb8253b79091f18fa4de69de
cd form_app/ios && pod install && cd ..
flutter build ios --simulator --debug        # → build/ios/iphonesimulator/Runner.app
```

Wikipedia (Xcode 26):

```sh
git clone https://github.com/wikimedia/wikipedia-ios && cd wikipedia-ios && git checkout d20d41ac1c00de574cc58faf7058e5c4d1f0e8bf
./scripts/setup_bundle_id ci
xcodebuild -project Wikipedia.xcodeproj -scheme Wikipedia -configuration Debug \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath ../dd-wikipedia CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build
```

Build Wikipedia ad-hoc signed as above: an unsigned build aborts at launch.

```sh
cd backend
uv run python scripts/breadth_eval.py flutter-forms --app <Runner.app>
uv run python scripts/breadth_eval.py wikipedia --app <Wikipedia.app>
```

The tests and their expected outcomes are in `backend/tamam/evals/breadth.py`.

Wikipedia and Flutter are named only to identify the apps tested; this project is not
affiliated with or endorsed by the Wikimedia Foundation, Google or Apple.
