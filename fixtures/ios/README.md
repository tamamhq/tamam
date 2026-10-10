# iOS eval fixture app

The ground-truth backbone of the eval suite: one SwiftUI app built two ways —
**Correct** (features work) and **Buggy** (planted defects). Each defect is on an
independent, reachable path, so one bug never masks another. Bundle id:
`dev.tamam.shoply`.

## "Shoply" — a realistic commerce app, two builds

The fixture is a small commerce app exercising the flows real apps have. Each planted
defect (buggy build) is a matched pair with the correct build. All defects live in one
`enum BUG` in the source.

| # | Flow | Defect (buggy build) | Eval case | Objective |
|---|---|---|---|---|
| 1 | branding | brand misspelled "Shopply" | `branding-*` | brand reads exactly "Shoply" |
| 2 | offers | offer shows "5% off" not 20% | `offer-*` | Offers advertises 20% off |
| 3 | list-view | Headphones price shows $8 not $80 | `list-price-*` | Headphones price is $80 |
| 4 | list-view | Add to Cart is a no-op | `add-to-cart-*` | Add to Cart shows "Added to cart" |
| 5 | search | search doesn't filter | `search-*` | search "Mouse" → only Wireless Mouse |
| 6 | filter | Books filter doesn't filter | `filter-*` | Books filter → only books |
| 7 | menus | Orders row doesn't navigate | `menu-orders-*` | tap Orders → "No orders yet" |
| 8 | payment | cart total is wrong | `cart-total-*` | add two items → total is $35 |
| 9 | payment | Pay is a no-op → no confirmation | `checkout-*` | checkout + pay → "Order Confirmed" |

Defect 10 is the off-screen discovery case: the category bar **scrolls sideways** and holds
more chips than fit, so the tail of the row is past the screen edge and simply not present in
the accessibility tree until the row is scrolled. On the correct build Garden is there and must
be found by scrolling; on the buggy build it is genuinely absent and "not found" is the correct
verdict. The chips keep their text labels on purpose — this case isolates *reach*, not
*readability*, so a green result has exactly one meaning. The pair is deliberately two-sided: it proves the premature-conclusion guard makes
the agent look, without letting it refuse to ever conclude.

Defect 11 is the icon-only case: the Offers tab's collection chips carry only their
asset name (`leaf`, `bolt`, `book`) in the accessibility tree and render their real name
ONLY while selected. Neither the tree nor a screenshot can say what a chip is, so the
agent has to learn it by picking one and reading what it turns out to be. This is the
readability counterpart to defect 10's reach.

Defect 14 is the infinite list: More → Catalog is a UIKit table that loads the next 20
items whenever its last loaded row is requested, the way many real feeds are built. A
harness that walks the whole accessibility tree pages in data nobody scrolled to; on a
real app's feed that made every read slower until sessions wedged. The "Loaded: N
items" header shows how much got loaded. The Golden Ticket sits at row 70, so reaching it
takes about 10 scrolls. On the buggy build it costs $50 instead of $5.

Defects 15 and 16 are dialogs, on More → Settings. The harness must tell the app's own
dialogs (the agent's to read and answer) from iOS system dialogs (accepted for it, on the
record). WebDriverAgent's `autoAcceptAlerts` accepted both, blindly, every 2 s.
- **15, error alert:** on the buggy build Save raises "Couldn't save your changes" while the
  screen still says "Saved". An error auto-dismissed before the agent reads it is a false PASS.
- **16, confirmation:** "Sign out?" → Cancel must keep you signed in; on the buggy build it
  signs you out anyway. Auto-accepting the dialog presses "Sign out" (a false FAIL).
- **System prompt (not a defect):** "Enable order updates" asks iOS for notification
  permission. `system-permission` must PASS: the harness accepts the prompt and reports it.
  iOS asks once per install, so uninstall the fixture before re-running that case.

Defect 17 is a settings switch (More → Settings → Dark mode). SwiftUI exposes it as a
labelled row wrapping an unlabelled knob; only the knob flips it, and its state is the
accessibility `value` ("0"/"1"). On the buggy build it snaps back off. An agent that judges
the switch from the screenshot can pass "it is on" while it is off — the perceiver must show
the state from the app (`(on)`/`(off)`) and tap the knob.

Plus a happy-path (`shop-loads`, must PASS) and real-app Settings cases.

The oracle checks the captured final accessibility text **independently** of the
agent/judge — it confirms the true state so a green eval isn't just the judge
agreeing with itself.

## Build (both variants)

No Xcode project needed — `build.sh` compiles the single-file app straight to a
simulator `.app` with `swiftc` (the `BUGGY` build just adds `-D BUGGY`), so it works
across Xcode versions:

```bash
./build.sh
```

It prints the two `.app` paths as `export` lines. Then run the suite:

```bash
export TAMAM_FIXTURE_CORRECT_APP=".../build/Correct/Shoply.app"
export TAMAM_FIXTURE_BUGGY_APP=".../build/Buggy/Shoply.app"
cd ../../backend && uv run python scripts/run_evals.py --repeats 5 --device "iPhone 15"
```

(Appium must be running; it boots the simulator.)
