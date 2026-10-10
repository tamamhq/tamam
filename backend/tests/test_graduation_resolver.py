"""Finding a step's target on replay: exactly one element, or none — never a guess."""

from types import SimpleNamespace as NS

from tamam.graduation.resolver import resolve
from tamam.graduation.script import Locator, Target

H = 874.0


def el(label, y, type_="Button", identifier="", h=44.0):
    return NS(type=type_, label=label, identifier=identifier, frame=NS(x=0, y=y, width=100, height=h))


def space(*els):
    return NS(elements=list(els), screen_w=402.0, screen_h=H)


def tgt(label, y=None, h=44.0, identifier=""):
    locs = [Locator(kind="identifier", value=identifier)] if identifier else []
    locs.append(Locator(kind="type_label", value=f"Button|{label}"))
    ff = [0, y / H, 0.25, h / H] if y is not None else []
    return Target(type="Button", label=label, locators=locs, frame_frac=ff)


def test_one_match_is_found_and_none_is_none():
    assert resolve(tgt("Cart"), space(el("Cart", 800))).how == "type_label"
    assert resolve(tgt("Cart"), space(el("Shop", 800))).element is None


def test_a_back_button_and_a_tab_with_the_same_name_are_told_apart():
    back, tab = el("Shop", 50), el("Shop", 795)
    assert resolve(tgt("Shop", y=795), space(back, tab)).element is tab
    assert resolve(tgt("Shop", y=50), space(back, tab)).element is back


def test_identical_rows_in_the_content_are_never_guessed():
    rows = space(el("Add to cart", 200), el("Add to cart", 600))
    r = resolve(tgt("Add to cart", y=200), rows)
    assert r.element is None and r.how == "ambiguous"


def test_two_matches_in_the_same_bar_stay_ambiguous():
    r = resolve(tgt("Shop", y=795), space(el("Shop", 790), el("Shop", 800)))
    assert r.element is None


def test_no_recorded_position_means_no_tie_break():
    assert resolve(tgt("Shop"), space(el("Shop", 50), el("Shop", 795))).element is None


def test_a_row_and_the_text_inside_it_are_one_control_but_list_rows_stay_ambiguous():
    """#26 Feedback: the merged 'I agree' row and its own label share the name, nested — one
    control. Two identical rows side by side (a list) remain ambiguous: no positional guess."""
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.graduation.resolver import resolve
    from tamam.graduation.script import Locator, Target
    t = Target(type="StaticText", label="I agree", frame_frac=[0.04, 0.4, 0.7, 0.05],
               locators=[Locator(kind="type_label", value="StaticText|I agree")])
    row = Element(ref="e5", type="StaticText", label="I agree", frame=Frame(16, 359, 285, 47), toggled=False)
    text = Element(ref="e6", type="StaticText", label="I agree", frame=Frame(72, 370, 200, 24))
    r = resolve(t, ActionSpace(elements=[text, row], screen_w=402, screen_h=874))
    assert r.element is row and r.how == "nested"
    other = Element(ref="e7", type="StaticText", label="I agree", frame=Frame(16, 420, 285, 47))
    assert resolve(t, ActionSpace(elements=[row, other], screen_w=402, screen_h=874)).element is None
