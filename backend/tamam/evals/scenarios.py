"""iOS scenario set — real-world app flows against the Shoply fixture.

Backbone: our fixture app ("Shoply") in two builds. Each planted defect is a matched
pair — the SAME objective must PASS on the correct build and FAIL on the buggy build —
covering the flows real apps have: branding, search, filter, list, product detail,
cart, payment/checkout, offers, menus. Plus real-app (Settings) cases for realism.

Fixture build paths come from the caller (env in the CLI). Objectives + oracles match
fixtures/ios (see its README).
"""

from __future__ import annotations

from tamam.evals.models import EvalCase, Target

FIXTURE_BUNDLE = "dev.tamam.shoply"


def _steps(difficulty: str) -> int:
    return {"easy": 15, "medium": 20, "hard": 30}.get(difficulty, 20)


# Matched defect pairs. Each: correct build → PASS, buggy build → FAIL.
# c_* = oracle on the correct build; b_* = oracle on the buggy build. The oracle is
# an independent drift-guard on the captured final accessibility text.
SPECS = [
    dict(did="branding", cat="branding", diff="easy",
         obj="Verify the shop's brand name reads exactly 'Shoply'",
         c_req=["Shoply"], c_forbid=[], b_req=[], b_forbid=["Shoply"]),
    dict(did="offer", cat="offers", diff="easy",
         obj="Open the Offers tab and verify it advertises 20% off",
         c_req=["20% off"], c_forbid=[], b_req=[], b_forbid=["20% off"]),
    dict(did="list-price", cat="list-view", diff="medium",
         obj="In the Shop, verify the Headphones price is $80",
         c_req=["$80"], c_forbid=[], b_req=[], b_forbid=["$80"]),
    dict(did="add-to-cart", cat="list-view", diff="medium",
         obj="Open the Wireless Mouse product and tap Add to Cart, verify it shows 'Added to cart'",
         c_req=["Added to cart"], c_forbid=[], b_req=[], b_forbid=["Added to cart"]),
    dict(did="search", cat="search", diff="medium",
         obj="In the Shop, type 'Mouse' into Search and verify only the Wireless Mouse is listed",
         c_req=["Wireless Mouse"], c_forbid=["Swift Book"], b_req=["Swift Book"], b_forbid=[]),
    dict(did="filter", cat="filter", diff="medium",
         obj="In the Shop, tap the Books filter and verify only book products are shown, not the Wireless Mouse",
         c_req=["Swift Book"], c_forbid=["Wireless Mouse"], b_req=["Wireless Mouse"], b_forbid=[]),
    dict(did="menu-orders", cat="menus", diff="medium",
         obj="Open the More tab, tap Orders, and verify the Orders screen shows 'No orders yet'",
         c_req=["No orders yet"], c_forbid=[], b_req=[], b_forbid=["No orders yet"]),
    dict(did="cart-total", cat="payment", diff="hard",
         obj="Add the Wireless Mouse and the USB Cable to the cart, then open Cart and verify the total is $35",
         c_req=["Total: $35"], c_forbid=[], b_req=[], b_forbid=["Total: $35"]),
    # Off-screen discovery. The category bar scrolls sideways and holds more chips than
    # fit, so the tail of the row is absent from the tree until it is scrolled into view.
    # Labels are intact — this isolates reach, not readability. Correct build: Garden is
    # there, past the edge,
    # and must be found. Buggy build: Garden is genuinely gone, and "not found" is the
    # RIGHT answer — which is what keeps the premature-conclusion guard honest, since a
    # guard that merely forbade giving up would turn this case green by never finishing.
    dict(did="offscreen-category", cat="filter", diff="hard",
         obj="In the Shop, select the Garden category and verify only garden products are listed",
         c_req=["Garden", "Watering Can"], c_forbid=["Wireless Mouse"],
         b_req=[], b_forbid=["Garden"]),
    # Icon-only controls. The chips carry only their asset name in the tree and render
    # their real name ONLY while selected, so neither the tree nor a screenshot can say
    # what they are — the meaning has to be learned by picking one and reading the
    # result. Correct build: the collection filters. Buggy: it selects but never
    # filters, so the agent must notice the list did not change.
    dict(did="icon-only-collection", cat="offers", diff="hard",
         obj="On the Offers tab, select the Garden collection and verify only garden products are listed",
         c_req=["Garden", "Watering Can"], c_forbid=["Wireless Mouse"],
         b_req=["Wireless Mouse"], b_forbid=[]),
    # Lifecycle. Whether state survives leaving and re-entering the app cannot be
    # reached by tapping inside it — the agent needs a backgrounding capability at all.
    # Correct build: the filter is still applied on resume. Buggy: it silently resets.
    dict(did="resume-filter", cat="lifecycle", diff="medium",
         obj="In the Shop, select the Books category, send the app to the background and "
             "bring it back, then verify the Books filter is still applied",
         c_req=["Swift Book"], c_forbid=["Wireless Mouse"],
         b_req=["Wireless Mouse"], b_forbid=[]),
    # Slow load. The screen shows only a spinner for a beat before its content arrives,
    # so a verdict reached on the first frame is a verdict about nothing. Correct build:
    # the reviews turn up if you wait. Buggy: they never do, and saying so is right.
    dict(did="slow-reviews", cat="menus", diff="medium",
         obj="Open the More tab, tap Reviews, and verify at least one customer review is listed",
         c_req=["Great shop"], c_forbid=[], b_req=[], b_forbid=["Great shop"]),
    # Clause coverage — the dominant real-app failure (agent stops before every clause
    # is covered). Early clauses walk paths that WORK on both builds; the planted defect
    # sits in the LAST clause. An agent that stops early therefore passes the buggy
    # build — a false PASS — so these pairs measure coverage, not just perception.
    dict(did="coverage-late-filter", cat="coverage", diff="hard",
         obj="Open the Offers tab and read the discount it advertises. Then go to the Shop "
             "and open the Wireless Mouse product. Then go back to the Shop, tap the Books "
             "filter, and verify only book products are shown, not the Wireless Mouse",
         c_req=["Swift Book"], c_forbid=["Wireless Mouse"],
         b_req=["Wireless Mouse"], b_forbid=[]),
    dict(did="coverage-late-orders", cat="coverage", diff="hard",
         obj="In the Shop, find the Headphones and read their price, then open the More "
             "tab, tap Orders, and verify the Orders screen shows 'No orders yet'",
         c_req=["No orders yet"], c_forbid=[], b_req=[], b_forbid=["No orders yet"]),
    # Infinite list (defect #14). The Catalog loads the next page whenever its last
    # loaded row is requested, the way real infinite feeds do, so a harness that walks the
    # whole tree pages in data nobody scrolled to and slows the app down.
    # The ticket sits at row 70: reaching it takes ~10 legitimate scrolls, which must not
    # trip the repeated-action guardrail and must stay visible under the children cap.
    # Oracle: the ticket's exact label ("$5" alone would also match "$50").
    dict(did="infinite-catalog", cat="infinite-list", diff="hard",
         obj="Open the More tab, tap Catalog, scroll down until you find the Golden Ticket, "
             "and verify its price is $5",
         c_req=["Golden Ticket — $5"], c_forbid=["Golden Ticket — $50"],
         b_req=["Golden Ticket — $50"], b_forbid=[]),
    # App dialogs (defects #15, #16). WebDriverAgent's autoAcceptAlerts used to accept
    # every alert — the app's own included — within 2s: a "Couldn't save" error vanished
    # before the agent saw it (false PASS) and "Remove all items?" was confirmed for it.
    # App dialogs are now the agent's to read and answer.
    dict(did="error-alert", cat="alerts", diff="medium",
         obj="Open the More tab, tap Settings, tap Save, and verify the changes are saved "
             "without any error message",
         c_req=["Saved"], c_forbid=["Couldn't save"], b_req=[], b_forbid=[]),
    dict(did="confirm-cancel", cat="alerts", diff="medium",
         obj="Open the More tab, tap Settings, tap Sign out, choose Cancel when asked to "
             "confirm, and verify you are still signed in",
         c_req=["Signed in as Sam"], c_forbid=["Signed out"],
         b_req=["Signed out"], b_forbid=["Signed in as Sam"]),
    # Two-state control (defect #17). The switch's state must come from the app, not the
    # screenshot: an agent once passed "the switch is on" while the tree said value="0".
    dict(did="toggle-switch", cat="controls", diff="medium",
         obj="Open the More tab, tap Settings, turn on the Dark mode switch, and verify it is on",
         # Oracle reads the app's own text, not our serialized switch state, so a bug in
         # how the perceiver reads switches cannot make agent and oracle agree wrongly.
         c_req=["Theme: Dark"], c_forbid=["Theme: Light"],
         b_req=["Theme: Light"], b_forbid=["Theme: Dark"]),
    # Look-only defects (#18–#21), worded the way real test plans word them ("X opens",
    # "loads fully", "without errors"). Each page opens with the expected title, so only a
    # look at what the page SHOWS can tell the builds apart — the AI look must not pass them.
    dict(did="blank-terms", cat="look", diff="hard",
         obj="Open the More tab, tap Help & Legal, tap Terms of Service, and verify the Terms of Service opens "
             "with its content",
         c_req=["By creating an account"], c_forbid=[], b_req=[], b_forbid=["By creating an account"]),
    dict(did="half-loaded-help", cat="look", diff="hard",
         obj="Open the More tab, tap Help & Legal, tap Help Topics, and verify the help topics list loads fully",
         c_req=["All 6 topics"], c_forbid=[], b_req=["Loading more topics"], b_forbid=["All 6 topics"]),
    dict(did="wrong-page", cat="look", diff="hard",
         obj="Open the More tab, tap Help & Legal, tap Shipping, and verify the shipping information page opens",
         c_req=["Orders ship within"], c_forbid=["Return any item"],
         b_req=["Return any item"], b_forbid=["Orders ship within"]),
    dict(did="error-banner", cat="look", diff="hard",
         obj="Open the More tab, tap Help & Legal, tap Rewards, and verify the rewards balance loads without errors",
         c_req=["Balance: 120 points"], c_forbid=["Couldn't load"],
         b_req=["Couldn't load"], b_forbid=[]),
    # Coordinate-only target: the stars are hidden from the accessibility tree, so the
    # agent must tap by position, read off the screenshot's grid.
    dict(did="rate-stars", cat="look", diff="hard",
         obj="Open the More tab, tap Help & Legal, tap Rate Us, tap the fourth star, and verify "
             "the screen says 'Thanks for 4 stars'",
         c_req=["Thanks for 4 stars"], c_forbid=[], b_req=["Thanks for 3 stars"],
         b_forbid=["Thanks for 4 stars"]),
    # Keyboard-covered form: the first field auto-focuses, and the keyboard then covers
    # the next fields and the Save button.
    dict(did="keyboard-form", cat="keyboard", diff="hard",
         obj="Open the More tab, tap Help & Legal, tap Delivery Address, enter '1 Main St' as "
             "the street, 'Springfield' as the city and '12345' as the postcode, tap Save "
             "address, and verify 'Address saved' appears",
         c_req=["Address saved"], c_forbid=["required"], b_req=["Postcode is required"],
         b_forbid=["Address saved"]),
    # Long-press: the redeem control reacts only to a press-and-hold.
    dict(did="hold-redeem", cat="gestures", diff="medium",
         obj="Open the More tab, tap Help & Legal, tap Gift Card, press and hold 'Hold to "
             "redeem', and verify 'Gift card redeemed' appears",
         c_req=["Gift card redeemed"], c_forbid=[], b_req=[], b_forbid=["Gift card redeemed"]),
    # Swipe a card aside: only a flick on the card reveals the code under it.
    dict(did="swipe-coupon", cat="gestures", diff="medium",
         obj="Open the More tab, tap Help & Legal, tap Coupon, swipe the coupon card to the left, "
             "and verify the code SAVE20 is revealed",
         c_req=["Code: SAVE20"], c_forbid=[], b_req=[], b_forbid=["Code: SAVE20"]),
    # Multi-line box + merged agreement row (defect #26): the comments field is a TextView,
    # and only the checkbox square toggles the "I agree" row — a centre tap hits the label.
    dict(did="feedback", cat="controls", diff="hard",
         obj="Open the More tab, tap Help & Legal, tap Feedback, write 'Love the app' in the "
             "comments, agree to be contacted, tap Send feedback, and verify 'Thanks for your "
             "feedback' appears",
         c_req=["Thanks for your feedback"], c_forbid=["Please"],
         b_req=["Please agree to be contacted"], b_forbid=["Thanks for your feedback"]),
    # A flattened form (defect #27): label, field and button are ONE accessibility element, so
    # the field and Subscribe exist only in the pixels.
    dict(did="newsletter", cat="perception", diff="hard",
         obj="Open the More tab, tap Help & Legal, tap Newsletter, enter ada@example.com as the "
             "email, tap Subscribe, and verify it says 'Subscribed: ada@example.com'",
         c_req=["Subscribed: ada@example.com"], c_forbid=[],
         b_req=[], b_forbid=["Subscribed: ada@example.com"]),
    dict(did="checkout", cat="payment", diff="hard",
         obj="Add a product to the cart, open Cart, tap Checkout, tap Pay, and verify 'Order Confirmed' appears",
         c_req=["Order Confirmed"], c_forbid=[], b_req=[], b_forbid=["Order Confirmed"]),
]


def _target(app: str, device: str) -> Target:
    return Target(platform="ios", executor_id="ios_agent",
                  app_build_ref=app, app_bundle_id=FIXTURE_BUNDLE, device=device)


def ios_cases(*, correct_app: str | None, buggy_app: str | None,
              device: str = "iPhone 15", include_real: bool = True) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for s in SPECS:
        steps = _steps(s["diff"])
        if correct_app:
            cases.append(EvalCase(
                id=f"{s['did']}-ok", category=s["cat"], difficulty=s["diff"],
                objective=s["obj"], expected="pass", target=_target(correct_app, device),
                require_text=s["c_req"], forbid_text=s["c_forbid"], max_steps=steps))
        if buggy_app:
            cases.append(EvalCase(
                id=f"{s['did']}-bug", category=s["cat"], difficulty=s["diff"],
                objective=s["obj"], expected="fail", target=_target(buggy_app, device),
                require_text=s["b_req"], forbid_text=s["b_forbid"], max_steps=steps))

    if correct_app:  # happy-path (not a planted bug)
        cases.append(EvalCase(
            id="shop-loads", category="happy-path", difficulty="easy",
            objective="Verify the app opens showing a Shop with a product list and a Search field",
            expected="pass", target=_target(correct_app, device),
            require_text=["Search"], max_steps=15))
        # iOS system dialog: the notification-permission prompt autoAcceptAlerts existed
        # for. It must still be got past (accepted by the harness, on the record). Needs a
        # fresh install — iOS asks once per install.
        cases.append(EvalCase(
            id="system-permission", category="alerts", difficulty="medium",
            objective="Open the More tab, tap Settings, tap Enable order updates, and verify "
                      "it shows 'Order updates: On'",
            expected="pass", target=_target(correct_app, device),
            require_text=["Order updates: On"], forbid_text=["Order updates: Off"],
            max_steps=15))

    if include_real:
        cases += [
            EvalCase(id="settings-general", category="real-app", difficulty="easy",
                     objective="Verify the Settings screen shows a General option", expected="pass",
                     target=Target(app_bundle_id="com.apple.Preferences", device=device),
                     require_text=["General"], max_steps=15),
            EvalCase(id="settings-xyzzy", category="real-app", difficulty="medium",
                     objective="Verify there is a settings row labelled 'XYZZY Mode'", expected="fail",
                     target=Target(app_bundle_id="com.apple.Preferences", device=device),
                     forbid_text=["XYZZY"], max_steps=20),
        ]
    return cases
