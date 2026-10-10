"""Breadth suite: open-source apps of other kinds than our SwiftUI fixture.

"Works for everyone" needs app kinds we did not build: a Flutter app (its own renderer, with a
semantics tree instead of UIKit views) and a large real UIKit app. Nothing is vendored: each app
is built from its public repo at a pinned commit (see docs/results.md).

Objectives are written the way a customer would write them, from what the screen shows. Each
one ends in a state the app's own source fixes, which is the oracle (`c_req` / `c_forbid` on
the final accessibility text). NEGATIVE controls (`expect="fail"`) claim something the app
provably does not do, so a harness that passes everything cannot score well.
"""

from __future__ import annotations

from tamam.evals.models import EvalCase, Target

_STEPS = {"easy": 15, "medium": 20, "hard": 30}

# Flutter's maintained form sample: four demos behind a list (Sign in with HTTP, Autofill,
# Form widgets, Validation). Sign-in uses a mock client: root / password → 200, else 401.
# Validation accepts words from the english_words package ("quick" is an adjective, "dog" a
# noun, "fox" is not in its noun list).
FLUTTER_FORMS = dict(
    bundle="dev.flutter.formApp.formApp",
    repo="https://github.com/flutter/samples",
    commit="63411527b7f617f1fb8253b79091f18fa4de69de",
    path="form_app",
    specs=[
        dict(did="ff-signin", cat="forms", diff="easy",
             obj="Open 'Sign in with HTTP', sign in with email root and password password, and "
                 "verify it says 'Successfully signed in.'",
             c_req=["Successfully signed in"], c_forbid=[]),
        dict(did="ff-signin-wrong", cat="forms", diff="easy",
             obj="Open 'Sign in with HTTP', sign in with email root and password wrong123, and "
                 "verify the app refuses with 'Unable to sign in.'",
             c_req=["Unable to sign in"], c_forbid=[]),
        dict(did="ff-story", cat="forms", diff="medium",
             obj="Open 'Validation', enter the adjective quick and the noun dog, agree to the "
                 "terms of service, submit, and verify the story reads 'The quick developer saw "
                 "a dog'",
             c_req=["The quick developer saw a dog"], c_forbid=[]),
        dict(did="ff-terms-required", cat="forms", diff="medium",
             obj="Open 'Validation', enter the adjective quick and the noun dog, submit WITHOUT "
                 "agreeing to the terms, and verify it says you must agree to the terms of "
                 "service",
             c_req=["You must agree to the terms of service"], c_forbid=["Your story"]),
        dict(did="ff-widgets", cat="controls", diff="medium",
             obj="Open 'Form widgets', check 'Brushed Teeth' and turn on 'Enable feature', and "
                 "verify both are now on",
             c_req=[], c_forbid=[]),
        # Negative controls.
        dict(did="neg-ff-signin-wrong", cat="negative", diff="easy",
             obj="Open 'Sign in with HTTP', sign in with email root and password wrong123, and "
                 "verify it says 'Successfully signed in.'",
             c_req=[], c_forbid=["Successfully signed in"], expect="fail"),
        dict(did="neg-ff-fox", cat="negative", diff="medium",
             obj="Open 'Validation', enter the adjective quick and the noun fox, agree to the "
                 "terms of service, submit, and verify the story reads 'The quick developer saw "
                 "a fox'",
             c_req=[], c_forbid=["The quick developer saw a fox"], expect="fail"),
        dict(did="neg-ff-feature-default", cat="negative", diff="easy",
             obj="Open 'Form widgets' and verify 'Enable feature' is switched on when the screen "
                 "opens",
             c_req=[], c_forbid=[], expect="fail"),
    ],
)

# Wikipedia for iOS: a large real UIKit app whose articles render in a web view. It needs the
# network, and its Explore feed changes daily, so every test uses stable encyclopedia facts.
# A fresh install opens on an introduction; the first search shows a one-time languages sheet.
_INTRO = "Skip the introduction if it appears. "
WIKIPEDIA = dict(
    bundle="org.wikimedia.wikipedia",
    repo="https://github.com/wikimedia/wikipedia-ios",
    commit="d20d41ac1c00de574cc58faf7058e5c4d1f0e8bf",
    path=".",
    specs=[
        dict(did="wk-search", cat="search", diff="medium",
             obj=_INTRO + "Search Wikipedia for Ada Lovelace, open the article about the English "
                 "mathematician, and verify it says she was born on 10 December 1815",
             c_req=["10 December 1815"], c_forbid=[]),
        dict(did="wk-follow-link", cat="webview", diff="medium",
             obj=_INTRO + "Open the Ada Lovelace article, tap the Charles Babbage link in its "
                 "introduction, and verify the Charles Babbage article opens",
             c_req=["Charles Babbage"], c_forbid=[]),
        dict(did="wk-save", cat="navigation", diff="hard",
             obj=_INTRO + "Open the Ada Lovelace article, save it, then go to the Saved tab and "
                 "verify Ada Lovelace is in your saved articles",
             c_req=["Ada Lovelace"], c_forbid=[]),
        dict(did="wk-intro", cat="onboarding", diff="easy",
             obj="Go through the introduction with Next (do not skip it) until it ends, and verify "
                 "the app then shows the Explore feed",
             c_req=["Explore"], c_forbid=[]),
        # Negative controls.
        dict(did="neg-wk-birth", cat="negative", diff="medium",
             obj=_INTRO + "Open the Ada Lovelace article and verify it says she was born in 1816",
             c_req=[], c_forbid=[], expect="fail"),
        dict(did="neg-wk-saved", cat="negative", diff="easy",
             obj=_INTRO + "Without saving anything, open the Saved tab and verify Ada Lovelace "
                 "is in your saved articles",
             c_req=[], c_forbid=[], expect="fail"),
        dict(did="neg-wk-babbage", cat="negative", diff="medium",
             obj=_INTRO + "Search Wikipedia for Charles Babbage and verify the result says he "
                 "lived from 1791 to 1880",
             c_req=[], c_forbid=[], expect="fail"),
    ],
)

APPS = {"flutter-forms": FLUTTER_FORMS, "wikipedia": WIKIPEDIA}


def breadth_cases(name: str, *, app: str, device: str = "iPhone 16 Pro") -> list[EvalCase]:
    spec = APPS[name]
    target = Target(platform="ios", executor_id="ios_agent",
                    app_build_ref=app, app_bundle_id=spec["bundle"], device=device)
    return [
        EvalCase(
            id=s["did"], category=s["cat"], difficulty=s["diff"], objective=s["obj"],
            expected=s.get("expect", "pass"), target=target,
            require_text=s["c_req"], forbid_text=s["c_forbid"],
            max_steps=_STEPS[s["diff"]], tags=["breadth", name],
        )
        for s in spec["specs"]
    ]
