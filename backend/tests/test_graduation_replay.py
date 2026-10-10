"""Replay a compiled script against a scripted device: no LLM, checks in code, fail closed."""

from tamam.executors.core.contracts import ActionSpace, Element, Frame
from tamam.graduation.replay import ReplayRunner, check_holds
from tamam.graduation.script import (
    Check, ClauseCoverage, CompiledScript, Locator, Step, Target, TypedText,
)


def el(ref, label, type_="Button", y=100, identifier="", toggled=None, selected=False):
    return Element(ref=ref, type=type_, label=label, frame=Frame(20, y, 300, 40),
                   identifier=identifier, toggled=toggled, selected=selected)


def screen(*els, h="h"):
    return ActionSpace(elements=list(els), screen_w=402, screen_h=874, source_hash=h)


class FakeSession:
    """Serves screens in order; each act() advances to the next 'after' screen."""

    def __init__(self, screens):
        self.screens = list(screens)
        self.acts = []
        self.act_timed_out = False

    async def observe(self, heartbeat=None):
        return self.screens[0]

    async def act(self, space, action):
        self.acts.append(action)
        if len(self.screens) > 1:
            self.screens.pop(0)
        return f"[step] {action.action}"

    async def screenshot(self):
        return None


async def _nosleep(_):
    return None


def tgt(label, type_="Button", identifier=""):
    locs = ([Locator(kind="identifier", value=identifier)] if identifier else []) + \
           [Locator(kind="type_label", value=f"{type_}|{label}")]
    return Target(type=type_, label=label, locators=locs, frame_frac=[0.05, 0.1, 0.75, 0.05])


def script(steps, checks, coverage=None):
    return CompiledScript(test_text_hash="h", steps=steps, checks=checks, coverage=coverage or [])


CART_STEPS = [Step(id="s1", say='Tap "Cart"', do="tap", target=tgt("Cart"), pre=["Shop"], source_step=1)]
TOTAL = Check(id="k1", clause_id="c1", clause="verify the total is $35", after="s1", kind="visible",
              text="$35", with_text="Total", on_screen=["Cart", "Checkout"])


async def _run(screens, sc, look=None, text="Open Cart, verify the total is $35"):
    sess = FakeSession(screens)
    out = await ReplayRunner(sess, sc, text, clause_look=look, sleep=_nosleep).run()
    return out, sess


async def test_a_good_app_passes_with_no_model_call():
    out, sess = await _run([screen(el("e1", "Shop"), el("e2", "Cart"), h="a"),
                            screen(el("e1", "Cart"), el("e2", "Total: $35"), el("e3", "Checkout"), h="b")],
                           script(CART_STEPS, [TOTAL]))
    assert out.verdict == "passed", out.reason
    assert [c.how for c in out.checks] == ["code"] and sess.acts[0].ref == "e2"


async def test_a_wrong_value_on_the_right_screen_is_an_app_bug():
    out, _ = await _run([screen(el("e1", "Shop"), el("e2", "Cart"), h="a"),
                         screen(el("e1", "Cart"), el("e2", "Total: $30"), el("e3", "Checkout"), h="b")],
                        script(CART_STEPS, [TOTAL]))
    assert out.verdict == "failed" and "$35" in out.reason


async def test_a_check_failing_on_a_different_screen_is_a_miss_not_a_bug():
    out, _ = await _run([screen(el("e1", "Shop"), el("e2", "Cart"), h="a"),
                         screen(el("e1", "Something went wrong"), h="b")],
                        script(CART_STEPS, [TOTAL]))
    assert out.verdict == "miss"


async def test_a_missing_target_is_a_miss():
    out, _ = await _run([screen(el("e1", "Shop"), el("e2", "Basket"), h="a")], script(CART_STEPS, [TOTAL]))
    assert out.verdict == "miss" and "not on the screen" in out.reason


async def test_an_action_that_changes_nothing_is_a_miss():
    out, _ = await _run([screen(el("e1", "Shop"), el("e2", "Cart"), h="same")], script(CART_STEPS, [TOTAL]))
    assert out.verdict == "miss" and "changed nothing" in out.reason


async def test_identifier_beats_label_and_ambiguity_escalates():
    steps = [Step(id="s1", say='Tap "Add"', do="tap", target=tgt("Add", identifier="addToCart"),
                  pre=["Shop"], source_step=1)]
    sc = script(steps, [Check(id="k1", clause_id="c1", clause="verify 'Added' shows", after="s1",
                              kind="visible", text="Added", on_screen=["Added"])])
    out, sess = await _run([screen(el("e1", "Shop"), el("e2", "Add", y=100), el("e3", "Add", y=200, identifier="addToCart"), h="a"),
                            screen(el("e1", "Added"), h="b")], sc)
    assert out.verdict == "passed" and sess.acts[0].ref == "e3"
    steps[0].target = tgt("Add")
    steps[0].target.frame_frac = []          # no recorded position → cannot pick between two
    out, _ = await _run([screen(el("e1", "Shop"), el("e2", "Add"), el("e3", "Add", y=200), h="a")], sc)
    assert out.verdict == "miss" and "matches 2 elements" in out.reason


async def test_unchecked_clauses_need_the_ai_look_which_can_only_pass_or_escalate():
    cov = [ClauseCoverage(clause_id="c2", clause="Verify Help opens", kind="verification",
                          checked_by_code=False, after="s1", on_screen=["Cart"])]
    screens = lambda: [screen(el("e1", "Shop"), el("e2", "Cart"), h="a"),  # noqa: E731
                       screen(el("e1", "Cart"), el("e2", "Total: $35"), el("e3", "Checkout"), h="b")]

    async def holds(c, s, shot): return "holds", 'the screen shows "Total: $35"' 
    async def fails(c, s, shot): return "fails", "a login screen is shown"
    async def boom(c, s, shot): raise RuntimeError("model down")

    out, _ = await _run(screens(), script(CART_STEPS, [TOTAL], cov), look=holds)
    assert out.verdict == "passed" and [c.how for c in out.checks] == ["code", "ai"]
    out, _ = await _run(screens(), script(CART_STEPS, [TOTAL], cov), look=fails)
    assert out.verdict == "miss"            # never "failed" on an AI glance alone
    out, _ = await _run(screens(), script(CART_STEPS, [TOTAL], cov), look=boom)
    assert out.verdict == "miss"
    out, _ = await _run(screens(), script(CART_STEPS, [TOTAL], cov), look=None)
    assert out.verdict == "miss"            # no AI look available → never an unchecked pass


async def test_credentials_are_filled_in_from_the_test_text():
    steps = [Step(id="s1", say="Type your password (hidden)", do="type", target=tgt("Password", "SecureTextField"),
                  text=TypedText(credential=0), pre=["Sign in"], source_step=1)]
    sc = script(steps, [Check(id="k1", clause_id="c1", clause="verify 'Welcome' shows", after="s1",
                              kind="visible", text="Welcome", on_screen=["Welcome"])])
    out, sess = await _run([screen(el("e1", "Sign in"), el("e2", "Password", "SecureTextField"), h="a"),
                            screen(el("e1", "Welcome"), h="b")], sc,
                           text="Sign in with password hunter2pw, verify 'Welcome' shows")
    assert out.verdict == "passed" and sess.acts[0].text == "hunter2pw"


async def test_scroll_until_the_named_row_appears():
    steps = [Step(id="s1", say='Scroll until "Golden Ticket" appears', do="scroll_until", direction="up",
                  max=5, until_text="$5", until_with="Golden Ticket", pre=["Catalog"], source_step=1)]
    sc = script(steps, [Check(id="k1", clause_id="c1", clause="verify its price is $5", after="s1",
                              kind="visible", text="$5", with_text="Golden Ticket", on_screen=["Catalog"])])
    out, sess = await _run([screen(el("e1", "Catalog"), el("e2", "Item 5 — $5"), h="a"),
                            screen(el("e1", "Catalog"), el("e2", "Item 40 — $4"), h="b"),
                            screen(el("e1", "Catalog"), el("e2", "Golden Ticket — $5"), h="c")], sc)
    assert out.verdict == "passed" and len(sess.acts) == 2
    out, _ = await _run([screen(el("e1", "Catalog"), el("e2", "Item 5 — $5"), h="a"),
                         screen(el("e1", "Catalog"), el("e2", "Golden Ticket — $50"), h="c")], sc)
    assert out.verdict != "passed"           # $50 is never $5


def test_state_checks_read_the_apps_own_state():
    s_on = screen(el("e1", "Dark mode", "Switch", toggled=True), el("e2", "Books", selected=True))
    s_off = screen(el("e1", "Dark mode", "Switch", toggled=False), el("e2", "Books"))
    sw = Check(id="k", clause_id="c", clause="x", after="", kind="toggled", text="Dark mode", expected=True)
    sel = Check(id="k", clause_id="c", clause="x", after="", kind="selected", text="Books", expected=True)
    assert check_holds(sw, s_on) and not check_holds(sw, s_off)
    assert check_holds(sel, s_on) and not check_holds(sel, s_off)


async def test_an_absence_must_hold_after_a_pause():
    absent = Check(id="k1", clause_id="c1", clause="not the Error", after="", kind="absent",
                   text="Error banner", on_screen=["Shop"])
    sc = script([], [absent])
    # The banner arrives late: the second read shows it.
    out, _ = await _run([screen(el("e1", "Shop"), h="a")], sc)
    assert out.verdict == "passed"
    sess = FakeSession([screen(el("e1", "Shop"), h="a")])
    reads = [screen(el("e1", "Shop"), h="a"), screen(el("e1", "Shop"), el("e2", "Error banner"), h="b")]

    async def observe(heartbeat=None):
        return reads.pop(0) if len(reads) > 1 else reads[0]
    sess.observe = observe
    out = await ReplayRunner(sess, sc, "x", sleep=_nosleep).run()
    assert out.verdict == "failed"


async def test_only_an_unsure_look_gets_a_second_look():
    cov = [ClauseCoverage(clause_id="c2", clause="Verify a review is listed", kind="verification",
                          checked_by_code=False, after="", on_screen=["Reviews"])]
    sc = script([], [], cov)
    shows = [screen(el("e1", "Reviews"), el("e2", "Great shop"), h="a")]

    looks = iter([("unsure", "still loading"), ("holds", 'a review "Great shop" is listed')])
    async def slow(c, s, shot): return next(looks)
    out, _ = await _run(list(shows), sc, look=slow)
    assert out.verdict == "passed"

    looks2 = iter([("fails", "no reviews"), ("holds", '"Great shop"')])
    async def flip(c, s, shot): return next(looks2)
    out, _ = await _run(list(shows), sc, look=flip)
    assert out.verdict == "miss"            # a "fails" escalates at once; no best-of-two


async def test_the_looks_quote_must_be_specific_screen_text():
    cov = [ClauseCoverage(clause_id="c2", clause="Verify a review is listed", kind="verification",
                          checked_by_code=False, after="", on_screen=["Reviews"])]
    async def title_only(c, s, shot): return "holds", 'the "Reviews" page'
    out, _ = await _run([screen(el("e1", "Reviews"), h="a")], script([], [], cov), look=title_only)
    assert out.verdict == "miss"            # quoting the screen's title proves nothing


async def test_a_sideways_scroll_swipes_the_row_not_the_screen():
    steps = [Step(id="s1", say='Scroll until "Garden" appears', do="scroll_until", direction="left",
                  target=tgt("Garden"), scroll_on=tgt("Category bar", "ScrollView"), max=5,
                  pre=["Shop"], source_step=1)]
    sc = script(steps, [])
    out, sess = await _run([screen(el("e1", "Shop"), el("e2", "Category bar", "ScrollView"), h="a"),
                            screen(el("e1", "Shop"), el("e2", "Category bar", "ScrollView"), el("e3", "Garden"), h="b")], sc)
    assert out.verdict == "passed" and sess.acts[0].ref == "e2"


async def test_an_ai_holds_without_on_screen_evidence_escalates():
    cov = [ClauseCoverage(clause_id="c2", clause="Verify Help opens", kind="verification",
                          checked_by_code=False, after="", on_screen=["Help"])]

    async def made_up(c, s, shot): return "holds", 'it shows "Help Center"'
    out, _ = await _run([screen(el("e1", "Help"), el("e2", "Contact"), h="a")], script([], [], cov), look=made_up)
    assert out.verdict == "miss"          # "Help Center" is not on this screen


async def test_an_absence_is_judged_only_after_the_positive_content_is_there():
    vis = Check(id="k1", clause_id="c1", clause="Golden Ticket listed, not Sold out", after="",
                kind="visible", text="Golden Ticket", on_screen=["Catalog"])
    absent = Check(id="k2", clause_id="c1", clause="Golden Ticket listed, not Sold out", after="",
                   kind="absent", text="Sold out", on_screen=["Catalog"])
    sc = script([], [absent, vis])          # compiled order: absent first
    reads = [screen(el("e1", "Catalog"), el("e2", "Loading"), h="a"),
             screen(el("e1", "Catalog"), el("e2", "Golden Ticket"), el("e3", "Sold out"), h="b")]
    sess = FakeSession([reads[0]])

    async def observe(heartbeat=None):
        return reads.pop(0) if len(reads) > 1 else reads[0]
    sess.observe = observe
    out = await ReplayRunner(sess, sc, "x", sleep=_nosleep).run()
    assert out.verdict != "passed"


async def test_no_check_passes_under_an_unexpected_app_dialog():
    vis = Check(id="k1", clause_id="c1", clause="name is saved", after="", kind="visible",
                text="Ann", on_screen=["Settings"])
    s = screen(el("e1", "Settings"), el("e2", "Ann"), h="a")
    s.alert = "Couldn't save your changes"
    out, _ = await _run([s], script([], [vis]))
    assert out.verdict == "miss" and "dialog" in out.reason


async def test_a_check_never_passes_on_a_different_screen():
    vis = Check(id="k1", clause_id="c1", clause="verify 'Done' shows", after="", kind="visible",
                text="Done", on_screen=["Receipt", "Order"])
    out, _ = await _run([screen(el("e1", "Cart"), el("e2", "Done"), h="a")], script([], [vis]))
    assert out.verdict == "miss"


async def test_a_failure_without_a_recorded_screen_is_a_miss_not_a_bug():
    vis = Check(id="k1", clause_id="c1", clause="x", after="", kind="visible", text="Done", on_screen=[])
    out, _ = await _run([screen(el("e1", "Cart"), h="a")], script([], [vis]))
    assert out.verdict == "miss"


async def test_the_first_screen_waits_for_the_app_to_launch_and_a_miss_says_what_it_saw():
    import tamam.graduation.replay as rp
    steps = [Step(id="s1", say='Tap "Skip"', do="tap", target=tgt("Skip"), pre=["Skip", "Welcome"], source_step=1)]
    sc = script(steps, [])
    reads = [screen(el("e1", "Loading"), h="a")] * 6 + [screen(el("e1", "Welcome"), el("e2", "Skip"), h="b")]
    sess = FakeSession([screen(el("e1", "Sign in"), h="c")])

    async def launching(heartbeat=None):          # the app takes 6 reads to show its first screen
        return reads.pop(0) if reads else sess.screens[0]
    sess.observe = launching
    out = await ReplayRunner(sess, sc, "x", sleep=_nosleep).run()
    assert out.verdict == "passed", out.reason          # 6 slow reads at launch are fine
    old = rp.LAUNCH_WAIT_S
    rp.LAUNCH_WAIT_S = 0.0
    try:
        out = await ReplayRunner(FakeSession([screen(el("e1", "Loading"), h="a")]), sc, "x", sleep=_nosleep).run()
    finally:
        rp.LAUNCH_WAIT_S = old
    assert out.verdict == "miss" and out.seen == ["Loading"]



async def test_a_miss_while_waiting_for_a_screen_says_how_far_replay_got():
    steps = [Step(id="s1", say='Tap "Cart"', do="tap", target=tgt("Cart"), pre=["Shop"], source_step=1),
             Step(id="s2", say='Tap "Pay"', do="tap", target=tgt("Pay"), pre=["Checkout"], source_step=2)]
    out, _ = await _run([screen(el("e1", "Shop"), el("e2", "Cart"), h="a"),
                         screen(el("e1", "Basket"), h="b")], script(steps, []))
    assert out.verdict == "miss" and out.steps_done == 1


async def test_content_that_arrives_before_the_screenshot_counts_as_evidence():
    """The look sees a screenshot taken AFTER the screen's text was read; reviews that loaded
    in between are real, so the quote is re-checked against a fresh read ( gate run)."""
    cov = [ClauseCoverage(clause_id="c2", clause="Verify a review is listed", kind="verification",
                          checked_by_code=False, after="", on_screen=["Reviews"])]
    loading = screen(el("e1", "Reviews"), el("e2", "Loading reviews"), h="a")
    loaded = screen(el("e1", "Reviews"), el("e2", "Great shop, fast delivery"), h="b")
    sess = FakeSession([loading])

    async def shot():
        sess.screens = [loaded]           # the reviews land while the screenshot is taken
    sess.screenshot = shot

    async def look(c, s, sh): return "holds", 'a review "Great shop, fast delivery" is listed'
    out = await ReplayRunner(sess, script([], [], cov), "x", clause_look=look, sleep=_nosleep).run()
    assert out.verdict == "passed", out.reason


async def test_content_recorded_as_an_anchor_is_still_evidence_but_a_title_is_not():
    """On a sparse screen the review itself is among the anchors; citing it is fine. Citing
    the title of the page the script tapped into is not ( gate run)."""
    cov = [ClauseCoverage(clause_id="c2", clause="Verify a review is listed", kind="verification",
                          checked_by_code=False, after="s1",
                          on_screen=["More", "Reviews", "Great shop, fast delivery"])]
    steps = [Step(id="s1", say='Tap "Reviews"', do="tap", target=tgt("Reviews"), pre=["More"], source_step=1)]
    shows = [screen(el("e1", "More"), el("e2", "Reviews"), h="a"),
             screen(el("e1", "More"), el("e2", "Reviews", "StaticText"), el("e3", "Great shop, fast delivery", "StaticText"), h="b")]

    async def review(c, s, sh): return "holds", 'the review "Great shop, fast delivery" is listed'
    out, _ = await _run(list(shows), script(steps, [], cov), look=review)
    assert out.verdict == "passed", out.reason

    async def title(c, s, sh): return "holds", 'the "Reviews" page is open'
    out, _ = await _run(list(shows), script(steps, [], cov), look=title)
    assert out.verdict == "miss"


async def test_a_miss_says_when_the_target_is_there_but_no_longer_a_button():
    """A dead row rendered as plain text: "not on the screen" would be false — say what changed."""
    out, _ = await _run([screen(el("e1", "Shop"), el("e2", "Cart", "StaticText"), h="a")],
                        script(CART_STEPS, [TOTAL]))
    assert out.verdict == "miss"
    assert 'no "Cart" button on the screen; "Cart" appears only as plain text' in out.reason


async def test_a_redundant_position_tap_is_fine_only_when_the_next_screen_is_already_there():
    """Batch-1 gate: a tap that dismissed a keyboard during recording changes nothing in replay."""
    steps = [Step(id="s1", say="Tap a spot", do="tap_point", point=[0.5, 0.2], pre=["Form"],
                  post=["Form", "Save address"], source_step=1)]
    same = screen(el("e1", "Form"), el("e2", "Save address"), h="a")
    out, _ = await _run([same], script(steps, []))
    assert out.verdict == "passed", out.reason
    other = [Step(id="s1", say="Tap a spot", do="tap_point", point=[0.5, 0.2], pre=["Form"],
                  post=["Checkout", "Pay"], source_step=1)]
    out, _ = await _run([same], script(other, []))
    assert out.verdict == "miss"              # it was meant to lead somewhere else


async def test_a_first_run_prompt_that_does_not_appear_is_skipped():
    """a fresh device showed no 'Maybe Later' prompt this time; the app's screen is there."""
    steps = [Step(id="s1", say='Tap "Maybe Later"', do="tap", target=tgt("Maybe Later"),
                  pre=["Don't Miss Out!", "Maybe Later"], source_step=1, optional=True),
             Step(id="s2", say='Tap "Cart"', do="tap", target=tgt("Cart"), pre=["Shop"], source_step=2)]
    out, sess = await _run([screen(el("e1", "Shop"), el("e2", "Cart"), h="a"),
                            screen(el("e1", "Cart"), el("e2", "Total: $35"), el("e3", "Checkout"), h="b")],
                           script(steps, [TOTAL.model_copy(update={"after": "s2"})]))
    assert out.verdict == "passed", out.reason
    assert [a.ref for a in sess.acts] == ["e2"] and any("skipped" in n for n in out.notes)


async def test_an_unrecorded_prompt_is_declined_never_accepted():
    """a prompt the recording never saw is in the way: press its decline answer, once."""
    prompt = screen(el("e1", "Don't Miss Out!", "StaticText"), el("e2", "Yes, notify me"),
                    el("e3", "Maybe Later"), h="p")
    out, sess = await _run([prompt, screen(el("e1", "Shop"), el("e2", "Cart"), h="a"),
                            screen(el("e1", "Cart"), el("e2", "Total: $35"), el("e3", "Checkout"), h="b")],
                           script(CART_STEPS, [TOTAL]))
    assert out.verdict == "passed", out.reason
    assert sess.acts[0].ref == "e3"                       # "Maybe Later", never "Yes, notify me"
    assert any('declined an unrecorded prompt with "Maybe Later"' in n for n in out.notes)


def test_a_prompt_answer_compiles_as_an_optional_step():
    from tests.test_graduation_compiler import rec, tgt as ctgt
    from tamam.graduation.compiler import compile_run
    recs = [rec(1, {"action": "tap", "ref": "e3"}, target=ctgt("Maybe Later"), texts=["Don't Miss Out!", "Maybe Later"]),
            rec(2, {"action": "tap", "ref": "e2"}, target=ctgt("Cart"), texts=["Shop", "Cart"]),
            rec(3, {"action": "done", "passed": True}, texts=["Cart", "Total: $35"])]
    d = compile_run(recs, [], "open the Cart", passed=True)
    assert [(s.target.label, s.optional) for s in d.script.steps] == [("Maybe Later", True), ("Cart", False)]


class _CatalogSession:
    """A 100-row list opened at its top: swipe up goes down the list, swipe down does nothing."""

    act_timed_out = False

    def __init__(self, golden_at=84, length=100):
        self.top, self.golden_at, self.length, self.acts = 0, golden_at, length, []

    def _space(self):
        rows = [el(f"e{i}", f"Catalog item {self.top + i}", y=100 + 40 * i) for i in range(10)]
        if self.top <= self.golden_at < self.top + 10:
            rows.append(el("e99", "Golden Ticket — $5", y=520))
        return screen(el("e0", "Catalog"), *rows, h=f"top{self.top}")

    async def observe(self, heartbeat=None):
        return self._space()

    async def act(self, space, action):
        self.acts.append(action.direction)
        if action.direction == "up":
            self.top = min(self.top + 10, self.length - 10)
        return f"[step] {action.action}"

    async def screenshot(self):
        return None


GOLDEN = Step(id="s1", say='Scroll until "Golden Ticket" appears', do="scroll_until", direction="down",
              max=73, until_text="Golden Ticket", pre=["Catalog"], source_step=3)


async def test_a_search_scroll_recorded_the_wrong_way_turns_around_when_nothing_moves():
    """Batch-4 gate, infinite-catalog: the recording kept the agent's first direction ("down",
    toward the top of a list already at its top); replay swiped 73 times in place and missed."""
    sess = _CatalogSession()
    out = await ReplayRunner(sess, script([GOLDEN], []), "find the Golden Ticket", sleep=_nosleep).run()
    assert out.verdict == "passed", out.reason
    assert sess.acts[:3] == ["down"] * 3 and set(sess.acts[3:]) == {"up"}


async def test_a_search_scroll_stops_at_the_ends_instead_of_spending_every_swipe():
    """An item that is not there: one end, the other way once, then a miss — not 73 swipes."""
    sess = _CatalogSession(golden_at=500)
    step = GOLDEN.model_copy(update={"direction": "up"})
    out = await ReplayRunner(sess, script([step], []), "find the Golden Ticket", sleep=_nosleep).run()
    assert out.verdict == "miss"
    assert sess.acts[0] == "up" and "down" in sess.acts and len(sess.acts) <= 9 + 3 + 3
