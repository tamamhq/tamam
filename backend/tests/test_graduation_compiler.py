"""Graduation compiler: trace + checklist + test text → script, or the reasons it can't be."""

from tamam.graduation.compiler import compile_run, instructions_hash


def by_code(d):
    """Clause → checked by code? (unchecked clauses get an AI look at replay)."""
    return {c.clause: c.checked_by_code for c in d.script.coverage}


def rec(step, action, target=None, texts=(), toggles=None, notes=(), anchors=None, typed=None):
    a = dict(action)
    if typed:
        a["typed"] = typed
    return {"schema": 2, "step": step, "action": a,
            "target": target, "notes": list(notes),
            "screen": {"anchors": list(anchors if anchors is not None else texts[:3]),
                       "tree_hash": f"h{step}", "size_pt": [402, 874],
                       "texts": list(texts), "toggles": toggles or {}}}


def tgt(label, type_="Button", identifier=""):
    return {"type": type_, "label": label, "identifier": identifier, "frame_frac": [0.1, 0.1, 0.2, 0.05]}


CART = "Add the Wireless Mouse and the USB Cable to the cart, then open Cart and verify the total is $35"


def cart_run():
    recs = [
        rec(1, {"action": "tap", "ref": "e3"}, tgt("Wireless Mouse", "Cell"), texts=["Shop", "Wireless Mouse"]),
        rec(2, {"action": "tap", "ref": "e5"}, tgt("Add to Cart", identifier="addToCart"), texts=["Wireless Mouse", "Add to Cart"]),
        rec(3, {"action": "tap", "ref": "e9"}, tgt("Cart", "Button"), texts=["Added to cart", "Cart"]),
        rec(4, {"action": "done", "passed": True}, texts=["Cart", "Wireless Mouse", "Total: $35"]),
    ]
    clauses = [{"id": "c1", "text": "Add the Wireless Mouse and the USB Cable to the cart", "status": "done", "step": 3, "evidence": ""},
               {"id": "c2", "text": "Open Cart and verify the total is $35", "status": "done", "step": 4, "evidence": "Total: $35"}]
    return recs, clauses


def test_a_passing_run_compiles_with_a_locked_check_from_the_test():
    recs, clauses = cart_run()
    d = compile_run(recs, clauses, CART, passed=True, source_run_id="r1")
    assert d.ready, d.reasons
    s = d.script
    assert [x.do for x in s.steps] == ["tap", "tap", "tap"]
    assert s.steps[1].target.locators[0].value == "addToCart"       # identifier first
    assert any(c.kind == "visible" and c.text == "$35" and c.after == "s3" for c in s.checks)
    assert s.checked_summary == "1 of 1 checks by code, 0 by an AI look on the screen"


def test_a_value_not_in_the_test_is_never_locked():
    recs, clauses = cart_run()
    clauses[1]["text"] = "Open Cart and verify the total is $40"   # planner paraphrase, not the test
    d = compile_run(recs, clauses, CART, passed=True)
    assert d.script.checks == [] and by_code(d)[clauses[1]["text"]] is False   # → AI look


def test_a_verification_with_nothing_checkable_blocks():
    recs, clauses = cart_run()
    clauses[1]["text"] = "Verify the cart looks right"
    d = compile_run(recs, clauses, CART + ". Verify the cart looks right", passed=True)
    assert d.ready and by_code(d)["Verify the cart looks right"] is False   # replayed with an AI look


def test_failed_or_old_runs_do_not_graduate():
    recs, clauses = cart_run()
    assert not compile_run(recs, clauses, CART, passed=False).ready
    old = [dict(r, schema=1) for r in recs]
    d = compile_run(old, clauses, CART, passed=True)
    assert not d.ready and "before fast mode" in d.reasons[0]


def test_swipes_before_a_tap_become_scroll_until_it_appears():
    recs = [rec(1, {"action": "swipe", "direction": "up"}, texts=["Catalog"]),
            rec(2, {"action": "swipe", "direction": "up"}, texts=["Catalog"]),
            rec(3, {"action": "tap", "ref": "e7"}, tgt("Golden Ticket — $5", "Cell"), texts=["Golden Ticket — $5"]),
            rec(4, {"action": "done", "passed": True}, texts=["Golden Ticket", "Price: $5"])]
    clauses = [{"id": "c1", "text": "verify its price is $5", "status": "done", "step": 4, "evidence": ""}]
    d = compile_run(recs, clauses, "scroll to the Golden Ticket and verify its price is $5", passed=True)
    assert d.ready, d.reasons
    assert [x.do for x in d.script.steps] == ["scroll_until", "tap"]
    assert d.script.steps[0].target.label == "Golden Ticket — $5" and d.script.steps[0].max == 7


def test_secrets_become_references_never_values():
    test = "Sign in with password hunter2pw then verify 'Welcome' shows"
    recs = [rec(1, {"action": "type", "ref": "e2", "text": "[typed text]"}, tgt("Password", "SecureTextField"),
                texts=["Password"], typed={"chars": 9, "secure": True, "in_test_text": True}),
            rec(2, {"action": "done", "passed": True}, texts=["Welcome"])]
    clauses = [{"id": "c1", "text": "verify 'Welcome' shows", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert d.ready, d.reasons
    assert d.script.steps[0].text.credential == 0 and d.script.steps[0].text.literal is None
    assert "hunter2pw" not in d.script.model_dump_json()


def test_invented_input_and_one_time_codes_block():
    recs = [rec(1, {"action": "type", "ref": "e2", "text": "[typed text]"}, tgt("Email", "TextField"),
                texts=["Email"], typed={"chars": 12, "secure": False, "in_test_text": False}),
            rec(2, {"action": "done", "passed": True}, texts=["Welcome"])]
    clauses = [{"id": "c1", "text": "verify 'Welcome' shows", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, "Sign up with a new email, verify 'Welcome' shows", passed=True)
    assert not d.ready and any("doesn't contain" in r for r in d.reasons)
    d = compile_run(recs[1:], clauses, "Enter the OTP and verify 'Welcome' shows", passed=True)
    assert any("one-time code" in r for r in d.reasons)


def test_switch_state_and_absence_checks():
    test = "In Settings, turn on the Dark mode switch and verify it is on, not the Light theme"
    recs = [rec(1, {"action": "tap", "ref": "e3"}, tgt("Dark mode", "Switch"), texts=["Settings", "Dark mode"]),
            rec(2, {"action": "done", "passed": True}, texts=["Settings", "Dark mode"], toggles={"Dark mode": True})]
    clauses = [{"id": "c1", "text": "verify the Dark mode switch is on, not the Light theme", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert d.ready, d.reasons
    # A state claim is covered by its state check only (never by "the label is on screen").
    assert [(c.kind, c.text, c.expected) for c in d.script.checks] == [("toggled", "Dark mode", True)]


def test_a_tap_that_changed_nothing_is_dropped():
    recs, clauses = cart_run()
    recs.insert(1, rec(2, {"action": "tap", "ref": "e4"}, tgt("Reviews"), notes=['[step 2] tapped e4 "Reviews" — screen did not change']))
    d = compile_run(recs, clauses, CART, passed=True)
    assert "Reviews" not in [s.target.label for s in d.script.steps if s.target]


def test_hash_ignores_whitespace():
    assert instructions_hash("a  b\nc") == instructions_hash("a b c")


def test_a_price_is_bound_to_the_row_the_test_names():
    test = "Open the Catalog, scroll down until you find the Golden Ticket, and verify its price is $5"
    recs = [rec(1, {"action": "swipe", "direction": "up"}, texts=["Catalog item 5 — $5"]),
            rec(2, {"action": "swipe", "direction": "up"}, texts=["Catalog item 14 — $5"]),
            rec(3, {"action": "done", "passed": True}, texts=["Catalog item 70 — $7", "Golden Ticket — $5"])]
    clauses = [{"id": "c1", "text": "verify its price is $5", "status": "done", "step": 3, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert d.ready, d.reasons
    [check] = d.script.checks
    assert (check.text, check.with_text) == ("$5", "Golden Ticket")
    scroll = d.script.steps[0]
    assert scroll.do == "scroll_until" and scroll.until_text == "$5" and scroll.until_with == "Golden Ticket"


def test_quotes_survive_apostrophes():
    test = "Verify the shop's brand name reads exactly 'Shoply'"
    recs = [rec(1, {"action": "done", "passed": True}, texts=["Shoply", "Search"])]
    clauses = [{"id": "c1", "text": test, "status": "done", "step": 1, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert d.ready and ("visible", "Shoply") in [(c.kind, c.text) for c in d.script.checks]
    assert d.script.steps == []                     # a check on the first screen needs no steps


def test_an_absence_needs_a_positive_check_on_the_same_screen():
    recs = [rec(1, {"action": "tap", "ref": "e2"}, tgt("Books"), texts=["Books", "Wireless Mouse"]),
            rec(2, {"action": "done", "passed": True}, texts=["Books", "Swift Book"])]
    alone = "Tap the Books filter, then verify the Wireless Mouse is not shown"
    clauses = [{"id": "c1", "text": "Verify the Wireless Mouse is not shown", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, alone, passed=True)
    assert d.script.checks == []            # an absence alone would pass an empty list → AI look
    both = "Tap the Books filter, then verify the Swift Book is shown, not the Wireless Mouse"
    clauses = [{"id": "c1", "text": "Verify the Swift Book is shown, not the Wireless Mouse", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, both, passed=True)
    assert d.ready, d.reasons
    assert sorted((c.kind, c.text) for c in d.script.checks) == [("absent", "Wireless Mouse"), ("visible", "Swift Book")]


def test_verify_it_is_on_uses_the_switch_just_tapped():
    test = "In Settings, turn on the Dark mode switch, and verify it is on"
    recs = [rec(1, {"action": "tap", "ref": "e3"}, tgt("Dark mode", "Switch"), texts=["Dark mode"]),
            rec(2, {"action": "done", "passed": True}, texts=["Dark mode"], toggles={"Dark mode": True})]
    clauses = [{"id": "c1", "text": "Verify it is on", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert d.ready and [(c.kind, c.text, c.expected) for c in d.script.checks] == [("toggled", "Dark mode", True)]


def test_shows_as_selected_becomes_a_selected_check():
    test = "Tap the Electronics category, then verify the Electronics category shows as selected"
    recs = [rec(1, {"action": "tap", "ref": "e4"}, tgt("Electronics"), texts=["All Products", "Electronics"]),
            rec(2, {"action": "done", "passed": True}, texts=["All Products", "Electronics"])]
    recs[1]["screen"]["selected"] = ["Electronics"]
    clauses = [{"id": "c1", "text": "Verify the Electronics category shows as selected", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert d.ready, d.reasons
    assert ("selected", "Electronics", True) in [(c.kind, c.text, c.expected) for c in d.script.checks]


# ── false-pass guards (adversarial review): each would have produced a weak "ready" script ──

def _one(clause, test, before, after, before_target=None, sel=None, toggles=None):
    recs = [rec(1, {"action": "tap", "ref": "e1"}, before_target or tgt("Go"), texts=before),
            rec(2, {"action": "done", "passed": True}, texts=after, toggles=toggles)]
    if sel is not None:
        recs[1]["screen"]["selected"] = sel
    clauses = [{"id": "c1", "text": clause, "status": "done", "step": 2, "evidence": ""}]
    return compile_run(recs, clauses, test, passed=True)


def test_unrecognised_claims_are_verifications_not_silent_actions():
    for clause in ["The product page loads with its 'Product details' section",
                   "The iOS share sheet opens", "Pass if the profile prompt appears; fail otherwise"]:
        d = _one(clause, clause, ["Shop"], ["Loading..."])
        assert by_code(d)[clause] is False and d.script.coverage[0].kind == "verification", clause
    assert compile_run([rec(1, {"action": "tap", "ref": "e1"}, tgt("More"), texts=["Shop"]),
                        rec(2, {"action": "done", "passed": True}, texts=["More"])],
                       [{"id": "c1", "text": "Open the More tab", "status": "done", "step": 2, "evidence": ""}],
                       "Open the More tab", passed=True).ready     # a real action is fine


def test_negation_never_becomes_a_presence_check():
    for clause in ["Verify the item is no longer in the Cart", "Verify the user is not signed in to Shoply",
                   "Verify not only Books are shown"]:
        d = _one(clause, clause, ["Shop"], ["Cart", "Shoply", "Books"])
        assert not any(c.kind == "visible" for c in d.script.checks), clause


def test_state_claims_need_the_apps_own_state():
    d = _one("Verify the Electronics category shows as selected", "Tap Electronics. Verify the Electronics category shows as selected",
             ["Electronics", "Garden"], ["Electronics", "Garden", "Products: 12"], sel=[])
    assert d.script.checks == []                        # no selected trait → never visible("Electronics")
    d = _one("Verify the Dark mode switch is on", "Turn on Dark mode. Verify the Dark mode switch is on",
             ["Dark mode"], ["Dark mode", "Theme"], toggles={})
    assert d.script.checks == []


def test_a_total_is_never_satisfied_by_a_line_item():
    d = _one("Verify the total is $25", "Add the Wireless Mouse to the cart and verify the total is $25",
             ["Wireless Mouse", "Add to Cart"], ["Wireless Mouse, $25", "Total: $0"])
    assert d.script.checks == []


def test_labels_that_never_change_are_not_checks():
    # The tab label was already on screen before the tap that "landed" there.
    d = _one("Confirm the app lands on the Offers tab", "Confirm the app lands on the Offers tab",
             ["Offers", "Cart"], ["Offers", "Cart", "Shop"])
    assert d.script.checks == []
    # The row just tapped: still there if the tap did nothing.
    d = _one("Verify Terms of Service opens", "Verify Terms of Service opens",
             ["Terms of Service", "Privacy"], ["Terms of Service", "Privacy"], before_target=tgt("Terms of Service"))
    assert d.script.checks == []


def test_a_password_without_digits_is_never_stored_as_text():
    test = "Sign in with password sunshine, then verify 'Welcome' shows"
    recs = [rec(1, {"action": "type", "ref": "e2", "text": "sunshine"}, tgt("Password", "TextField"),
                texts=["Password"], typed={"chars": 8, "secure": False, "in_test_text": True}),
            rec(2, {"action": "done", "passed": True}, texts=["Welcome"])]
    clauses = [{"id": "c1", "text": "verify 'Welcome' shows", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert "sunshine" not in d.script.model_dump_json()
    assert d.script.steps[0].text.credential is not None


def test_a_tap_that_changed_nothing_is_found_by_screen_hash():
    recs = [rec(1, {"action": "tap", "ref": "e1"}, tgt("More"), texts=["Shop"]),
            rec(2, {"action": "tap", "ref": "e4"}, tgt("Reviews"), texts=["More", "Reviews"]),
            rec(3, {"action": "tap", "ref": "e4"}, tgt("Reviews"), texts=["More", "Reviews"]),
            rec(4, {"action": "done", "passed": True}, texts=["Great shop"])]
    recs[2]["screen"]["tree_hash"] = recs[1]["screen"]["tree_hash"]   # step 2's tap changed nothing
    clauses = [{"id": "c1", "text": "verify 'Great shop' is listed", "status": "done", "step": 4, "evidence": ""}]
    d = compile_run(recs, clauses, "Open Reviews and verify 'Great shop' is listed", passed=True)
    assert [s.source_step for s in d.script.steps] == [1, 3]


def test_prohibitions_are_constraints_not_claims():
    from tamam.harness.clauses import is_verification
    assert not is_verification("Do not create a profile")
    assert not is_verification("Don't change any toggle")


def test_a_row_bound_price_is_a_code_check_even_if_the_row_was_already_shown():
    test = "Scroll down until you find the Golden Ticket, and verify its price is $5"
    recs = [rec(1, {"action": "swipe", "direction": "up"}, texts=["Golden Ticket — $5", "Item 4 — $5"]),
            rec(2, {"action": "done", "passed": True}, texts=["Golden Ticket — $5", "Item 4 — $5"])]
    clauses = [{"id": "c1", "text": "Verify its price is $5", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert [(c.kind, c.text, c.with_text) for c in d.script.checks] == [("visible", "$5", "Golden Ticket")]


def test_confirm_inside_a_clause_is_not_a_claim():
    from tamam.harness.clauses import is_verification
    assert not is_verification("Choose Cancel when asked to confirm")
    assert is_verification("Confirm the user can browse offers")


def test_a_sideways_scroll_remembers_the_row_it_scrolled():
    test = "In the Shop, select the Garden category and verify 'Watering Can' is listed"
    recs = [rec(1, {"action": "swipe", "direction": "left", "ref": "e3"}, tgt("Category bar", "ScrollView"), texts=["Shop", "All"]),
            rec(2, {"action": "tap", "ref": "e9"}, tgt("Garden"), texts=["Shop", "Garden"]),
            rec(3, {"action": "done", "passed": True}, texts=["Garden", "Watering Can"])]
    clauses = [{"id": "c1", "text": "verify 'Watering Can' is listed", "status": "done", "step": 3, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert d.script.steps[0].do == "scroll_until" and d.script.steps[0].scroll_on.label == "Category bar"


def test_a_name_in_many_rows_never_binds_a_price():
    test = "Open the More tab, tap Catalog, scroll down until you find the Golden Ticket, and verify its price is $5"
    recs = [rec(1, {"action": "swipe", "direction": "up"}, texts=["Catalog item 5 — $5", "Golden Ticket — $5"]),
            rec(2, {"action": "done", "passed": True},
                texts=["Catalog item 5 — $5", "Catalog item 14 — $5", "Golden Ticket — $5"])]
    clauses = [{"id": "c1", "text": "Verify its price is $5", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert [(c.text, c.with_text) for c in d.script.checks] == [("$5", "Golden Ticket")]


def test_a_clause_with_an_unchecked_claim_also_gets_the_ai_look():
    test = "Pay and verify the screen shows 'Order placed' and the total is $25"
    recs = [rec(1, {"action": "tap", "ref": "e1"}, tgt("Pay"), texts=["Pay"]),
            rec(2, {"action": "done", "passed": True}, texts=["Order placed", "Mouse $25", "Cable $25"])]
    clause = "verify the screen shows 'Order placed' and the total is $25"
    clauses = [{"id": "c1", "text": clause, "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert ("visible", "Order placed") in [(c.kind, c.text) for c in d.script.checks]
    assert by_code(d)[clause] is False      # "$25" got no check → the AI look covers the clause


def test_a_test_that_deletes_pays_or_sends_is_not_replayed_unless_allowed():
    test = "Open Orders, tap Delete account, and verify 'Account deleted' shows"
    recs = [rec(1, {"action": "tap", "ref": "e1"}, tgt("Delete account"), texts=["Orders", "Delete account"]),
            rec(2, {"action": "done", "passed": True}, texts=["Account deleted"])]
    clauses = [{"id": "c1", "text": "verify 'Account deleted' shows", "status": "done", "step": 2, "evidence": ""}]
    d = compile_run(recs, clauses, test, passed=True)
    assert not d.ready and any('taps "Delete account"' in r for r in d.reasons)
    assert compile_run(recs, clauses, test, passed=True, allow_risky=True).ready


def test_risky_words_are_whole_words():
    from tamam.graduation.compiler import _RISKY
    for label in ("Pay", "Buy now", "Sign Out", "Log out", "Place order", "Send message", "Apply Now"):
        assert _RISKY.search(label), label
    for label in ("Payment methods", "Apply filter", "Display", "Sender info", "Cart"):
        assert not _RISKY.search(label), label


def _tap(label="", identifier="", context=(), do="tap"):
    from tamam.graduation.script import Locator, Step, Target
    locs = [Locator(kind="identifier", value=identifier)] if identifier else []
    target = Target(type="Button", label=label, locators=locs) if do == "tap" else None
    return Step(id="s1", do=do, say="x", source_step=3, target=target, context=list(context),
                point=[0.5, 0.5] if do == "tap_point" else None)


def test_risk_is_seen_through_icons_dialogs_and_positions():
    from tamam.graduation.compiler import risky_step_reasons
    assert risky_step_reasons([_tap(identifier="trashButton")])          # icon-only button
    assert risky_step_reasons([_tap(identifier="cart.buy_now")])
    assert risky_step_reasons([_tap("OK", context=["Delete account?", "Cancel", "OK"])])
    assert risky_step_reasons([_tap(do="tap_point", context=["Confirm payment of $20"])])
    assert not risky_step_reasons([_tap("OK", context=["Notifications are on", "OK"])])
    assert not risky_step_reasons([_tap("Settings", context=["Delete account"])])  # named button: its own label decides


def test_harmless_uses_of_risky_words_do_not_block():
    from tamam.graduation.compiler import risky_step_reasons
    for label in ("Send code", "Send me a link", "Resend", "Submit", "Reset filters", "Forgot password? Reset password"):
        assert not risky_step_reasons([_tap(label)]), label
    for label in ("Submit order", "Send message", "Checkout", "Delete"):
        assert risky_step_reasons([_tap(label)]), label


def test_a_keyboard_return_or_submit_is_judged_by_its_screen():
    """submitting a payment form with the keyboard is as risky as tapping "Pay"."""
    from tamam.graduation.compiler import risky_step_reasons
    assert risky_step_reasons([_tap("return", context=["Card number", "Place order", "return"])])
    assert risky_step_reasons([_tap("Submit", context=["Confirm payment of $20"])])
    assert not risky_step_reasons([_tap("return", context=["Search products", "return"])])


def test_a_long_press_compiles_to_a_replayable_step_and_counts_for_risk():
    from tamam.graduation.compiler import risky_step_reasons
    recs = [rec(1, {"action": "long_press", "ref": "e1", "seconds": 1.0}, target=tgt("Hold to redeem"),
                texts=["Gift Card", "Hold to redeem"]),
            rec(2, {"action": "done", "passed": True}, texts=["Gift Card", "Gift card redeemed"])]
    clauses = [{"id": "c1", "text": "verify 'Gift card redeemed' appears", "status": "done", "step": 2,
                "evidence": "'Gift card redeemed' shown"}]
    d = compile_run(recs, clauses, "press and hold 'Hold to redeem', verify 'Gift card redeemed' appears",
                    passed=True)
    assert d.script is not None and [s.do for s in d.script.steps] == ["long_press"]
    held = d.script.steps[0]
    risky = held.model_copy(update={"target": held.target.model_copy(update={"label": "Delete photo"})})
    assert risky_step_reasons([risky])                      # a long-press on "Delete" is risky too


def test_a_scroll_to_or_hide_keyboard_that_changed_nothing_is_not_compiled():
    """Batch-1 gate: a scroll_to for text already on screen became a replay step that swiped
    20 times looking for it."""
    same = dict(texts=["Shop", "Add to Cart"])
    recs = [rec(1, {"action": "scroll_to", "text": "Add to Cart"}, **same),
            rec(2, {"action": "hide_keyboard"}, **same),
            rec(3, {"action": "tap", "ref": "e2"}, target=tgt("Add to Cart"), **same),
            rec(4, {"action": "done", "passed": True}, texts=["Shop", "Added to cart"])]
    recs[0]["screen"]["tree_hash"] = recs[1]["screen"]["tree_hash"] = recs[2]["screen"]["tree_hash"] = "same"
    d = compile_run(recs, [], "tap Add to Cart", passed=True)
    assert d.script is not None and [s.do for s in d.script.steps] == ["tap"]


def test_two_scroll_to_calls_for_the_same_text_compile_to_one_scroll():
    """Batch-1 gate: the second step scrolled past the item the first had already reached."""
    recs = [rec(1, {"action": "scroll_to", "text": "Golden Ticket"}, texts=["Catalog", "Item 10"]),
            rec(2, {"action": "scroll_to", "text": "Golden Ticket"}, texts=["Catalog", "Item 40"]),
            rec(3, {"action": "done", "passed": True}, texts=["Catalog", "Golden Ticket — $5"])]
    d = compile_run(recs, [], "scroll until you find the Golden Ticket", passed=True)
    scrolls = [s for s in (d.script.steps if d.script else []) if s.do == "scroll_until"]
    assert len(scrolls) == 1 and scrolls[0].until_text == "Golden Ticket" and scrolls[0].max >= 35


def test_an_action_replay_cannot_perform_blocks_graduation_instead_of_vanishing():
    recs = [rec(1, {"action": "drag", "ref": "e1", "to_ref": "e2"}, target=tgt("Card"), texts=["Board"]),
            rec(2, {"action": "done", "passed": True}, texts=["Board", "Moved"])]
    d = compile_run(recs, [], "drag the card to Done", passed=True)
    assert not d.ready and any("'drag' step cannot be replayed" in r for r in d.reasons)


def test_gesture_steps_are_actions_not_claims():
    """An RN loading-states screen: 'Pull down to refresh' was treated as a claim needing a quote."""
    from tamam.harness.clauses import is_verification
    for t in ("Pull down to refresh", "Long-press the Hold me button", "Hold the card",
              "Drag the slider to 50", "Double-tap the photo", "Claim reward"):
        assert not is_verification(t), t
    for t in ("Verify 'Reward claimed' is shown", "The list shows 3 items"):
        assert is_verification(t), t


def test_evidence_cited_one_step_early_compiles_from_the_screen_that_showed_it():
    """Batch-4 gate: the agent cited the step it tapped Cart on (step 3) for 'Total: $35',
    which the screen of step 4 showed. The checklist accepts a neighbouring step, so the
    compiler must too — else the check is never locked and replay misses on a good build."""
    recs, clauses = cart_run()
    clauses[1]["step"] = 3
    d = compile_run(recs, clauses, CART, passed=True, source_run_id="r1")
    assert d.ready, d.reasons
    assert any(c.kind == "visible" and c.text == "$35" and c.after == "s3" for c in d.script.checks)


def test_a_neighbour_step_never_lends_a_value_the_test_does_not_state():
    recs, clauses = cart_run()
    clauses[1]["step"] = 3
    recs[3]["screen"]["texts"] = ["Cart", "Wireless Mouse", "Total: $30"]
    d = compile_run(recs, clauses, CART, passed=True, source_run_id="r1")
    assert not any(c.text == "$30" for c in (d.script.checks if d.script else []))


def test_merged_scroll_to_calls_keep_the_direction_that_found_the_item():
    """Batch-4 gate: the first scroll_to went "down" (toward the top, already there) and found
    nothing; the second went "up" and found it. Replay must scroll the way that worked."""
    recs = [rec(1, {"action": "scroll_to", "text": "Golden Ticket", "direction": "down"}, texts=["Catalog", "Item 1"]),
            rec(2, {"action": "scroll_to", "text": "Golden Ticket", "direction": "up"}, texts=["Catalog", "Item 1"]),
            rec(3, {"action": "done", "passed": True}, texts=["Catalog", "Golden Ticket — $5"])]
    d = compile_run(recs, [], "scroll until you find the Golden Ticket", passed=True)
    scrolls = [s for s in d.script.steps if s.do == "scroll_until"]
    assert len(scrolls) == 1 and scrolls[0].direction == "up"


def test_setup_steps_with_plain_verbs_are_actions_and_nouns_stay_claims():
    """Wikipedia: 'Skip the introduction if it appears' was read as a claim, so a correct
    pass was refused for having no on-screen evidence of it. Unambiguous verbs are actions; a
    word that is also a noun keeps the clause a claim, so no check is silently dropped."""
    from tamam.harness.clauses import is_verification
    for action in ("Skip the introduction if it appears", "Continue to the next step",
                   "Agree to the terms", "Pay with the saved card", "Retry until the drafts load",
                   "Sign up with a new email", "Download the receipt", "Tap Save button",
                   "Open Settings tab", "Share the article"):
        assert not is_verification(action), action
    for claim in ("Order total is $5", "Filter shows only books", "Rate is 5%",
                  "Book title reads Swift", "Skip button is hidden after onboarding",
                  "Save button is greyed out", "Share icon appears in the toolbar"):
        assert is_verification(claim), claim
