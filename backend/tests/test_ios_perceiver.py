"""IOSPerceiver parse — pure, over a captured XCUITest source fixture.
Viewport scoping, interactive filtering, reading order, and coordinate grounding."""

from tamam.executors.ios.perceiver import parse_source

SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Sample" label="Sample" enabled="true" visible="true" x="0" y="0" width="390" height="844">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" enabled="true" visible="true" x="0" y="0" width="390" height="844">
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="login" label="Log In" enabled="true" visible="true" x="20" y="700" width="350" height="48"/>
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" name="title" label="Welcome" enabled="true" visible="true" x="20" y="100" width="200" height="30"/>
    <XCUIElementTypeTextField type="XCUIElementTypeTextField" name="email" label="" value="Email" enabled="true" visible="true" x="20" y="300" width="350" height="44"/>
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="offscreen" label="Below" enabled="true" visible="true" x="20" y="1200" width="100" height="40"/>
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="hidden" label="Hidden" enabled="true" visible="false" x="20" y="200" width="100" height="40"/>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""


def test_parse_scopes_and_orders():
    els = parse_source(SOURCE, 390, 844)
    # hidden (visible=false) and offscreen (y>=844) are excluded
    assert len(els) == 3
    # reading order by y: Welcome(100), Email(300), Log In(700)
    assert [e.ref for e in els] == ["e1", "e2", "e3"]
    assert [e.type for e in els] == ["StaticText", "TextField", "Button"]
    # label precedence is label→name→value: the empty-label field falls back to its name
    assert [e.label for e in els] == ["Welcome", "email", "Log In"]


def test_grounding_is_frame_center():
    els = parse_source(SOURCE, 390, 844)
    login = next(e for e in els if e.label == "Log In")
    # center of x=20,y=700,w=350,h=48
    assert login.frame.center == (195.0, 724.0)


def test_empty_or_bad_source_is_safe():
    assert parse_source("", 390, 844) == []
    assert parse_source("not xml", 390, 844) == []


# ---------- input fill-state: the silent-form-submit fix ----------

from tamam.executors.ios.perceiver import serialize  # noqa: E402

FORM = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="390" height="844" visible="true" enabled="true">
  <XCUIElementTypeTextField type="XCUIElementTypeTextField" label="Email Address" value="Email Address" enabled="true" visible="true" x="20" y="100" width="350" height="44"/>
  <XCUIElementTypeSecureTextField type="XCUIElementTypeSecureTextField" label="Password" value="••••••••" enabled="true" visible="true" x="20" y="160" width="350" height="44"/>
  <XCUIElementTypeButton type="XCUIElementTypeButton" label="Log In" enabled="true" visible="true" x="20" y="220" width="350" height="48"/>
</XCUIElementTypeApplication>"""

FILLED_EMAIL = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="390" height="844" visible="true" enabled="true">
  <XCUIElementTypeTextField type="XCUIElementTypeTextField" label="Email Address" value="test+1234@example.com" enabled="true" visible="true" x="20" y="100" width="350" height="44"/>
  <XCUIElementTypeSecureTextField type="XCUIElementTypeSecureTextField" label="Password" value="Password" enabled="true" visible="true" x="20" y="160" width="350" height="44"/>
</XCUIElementTypeApplication>"""


def test_empty_email_field_is_flagged_empty():
    """A bug seen on a real app: an empty field whose value echoes its placeholder must read
    as (empty) so the agent knows to fill it before submitting."""
    text = serialize(parse_source(FORM, 390, 844))
    assert 'TextField "Email Address" (empty)' in text
    # a filled secure field shows only (filled) — never its dots/contents
    assert 'SecureTextField "Password" (filled)' in text
    assert "••••" not in text


def test_filled_field_shows_value_but_secure_stays_masked():
    els = parse_source(FILLED_EMAIL, 390, 844)
    text = serialize(els)
    assert 'TextField "Email Address" = "test+1234@example.com"' in text  # value visible
    # secure field empty (value == label placeholder) → (empty), and never contents
    assert 'SecureTextField "Password" (empty)' in text
    email = next(e for e in els if e.type == "TextField")
    assert email.filled is True and email.value == "test+1234@example.com"
    secure = next(e for e in els if e.type == "SecureTextField")
    assert secure.value == ""  # secure contents never captured on the Element


def test_secure_field_value_never_captured_even_when_filled():
    src = FORM  # password value="••••••••"
    secure = next(e for e in parse_source(src, 390, 844) if e.type == "SecureTextField")
    assert secure.filled is True
    assert secure.value == ""  # filled, but contents are never surfaced


# real iOS: an empty field has label="", value=placeholder, and a placeholderValue attr
EMPTY_WITH_PLACEHOLDER = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="390" height="844" visible="true" enabled="true">
  <XCUIElementTypeTextField type="XCUIElementTypeTextField" label="" value="Email Address" placeholderValue="Email Address" enabled="true" visible="true" x="20" y="60" width="350" height="44"/>
  <XCUIElementTypeSecureTextField type="XCUIElementTypeSecureTextField" label="" value="Password" placeholderValue="Password" enabled="true" visible="true" x="20" y="120" width="350" height="44"/>
</XCUIElementTypeApplication>"""


def test_placeholder_as_value_reads_empty_not_filled():
    """A bug seen on a real app: an empty SecureTextField reports value="Password"
    (its placeholder) with an empty label. It must read (empty), not (filled)."""
    text = serialize(parse_source(EMPTY_WITH_PLACEHOLDER, 390, 844))
    assert 'SecureTextField "Password" (empty)' in text
    assert 'TextField "Email Address" (empty)' in text
    assert "(filled)" not in text


# ---------- occlusion-aware tapping (floating CTA over a target) ----------

def _app(*children):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0"'
            ' width="400" height="800" visible="true" enabled="true">'
            + "".join(children) + "</XCUIElementTypeApplication>")

def _cell(x, y, w, h, label):
    return (f'<XCUIElementTypeCell type="XCUIElementTypeCell" label="{label}" enabled="true"'
            f' visible="true" x="{x}" y="{y}" width="{w}" height="{h}"/>')

def _btn(x, y, w, h, label):
    return (f'<XCUIElementTypeButton type="XCUIElementTypeButton" label="{label}" enabled="true"'
            f' visible="true" x="{x}" y="{y}" width="{w}" height="{h}"/>')


def test_occlusion_redirects_tap_off_covered_centre():
    """A small Save CTA drawn over a wide cell's centre: the cell is flagged covered,
    and its tap point moves into a clear side-band instead of the (stolen) centre."""
    src = _app(_cell(0, 100, 400, 80, "Rage Room"), _btn(150, 110, 100, 60, "Save filter"))
    els = {e.label: e for e in parse_source(src, 400, 800)}
    cell = els["Rage Room"]
    assert cell.covered_by  # named the overlay
    assert cell.tap_point != cell.frame.center  # redirected off the covered centre
    # clear band is on the left/right (x within the cell, outside the button)
    tx, _ = cell.tap_point
    assert 0 <= tx < 150 or 250 < tx <= 400
    assert "covered by" in serialize(parse_source(src, 400, 800))


def test_fully_covered_target_keeps_centre_but_is_flagged():
    """No usable band → we can't redirect; fall back to centre but still flag covered
    so the brain scrolls it clear."""
    src = _app(_cell(100, 100, 200, 100, "Row"), _btn(105, 105, 190, 90, "X"))
    cell = {e.label: e for e in parse_source(src, 400, 800)}["Row"]
    assert cell.covered_by
    assert cell.tap_point == cell.frame.center  # fallback


def test_non_button_overlay_is_not_treated_as_occluder():
    """A StaticText over the centre doesn't steal taps → not flagged (no false positive)."""
    src = _app(_cell(0, 100, 400, 80, "Row"),
               '<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="badge"'
               ' visible="true" enabled="true" x="150" y="110" width="100" height="60"/>')
    cell = {e.label: e for e in parse_source(src, 400, 800)}["Row"]
    assert cell.covered_by == ""
    assert cell.tap_point == cell.frame.center


def test_larger_container_is_not_an_occluder():
    """A bigger element covering the centre is a container/ancestor, not an overlay."""
    src = _app(_btn(150, 110, 60, 40, "small"), _cell(0, 0, 400, 800, "backdrop"))
    small = {e.label: e for e in parse_source(src, 400, 800)}["small"]
    assert small.covered_by == ""  # the big cell after it is not an occluder


def test_selected_state_is_surfaced():
    """The active/selected control is annotated so the agent can verify a tap's effect."""
    src = _app(
        '<XCUIElementTypeButton type="XCUIElementTypeButton" label="square.grid.2x2" selected="true"'
        ' enabled="true" visible="true" x="20" y="100" width="80" height="40"/>',
        '<XCUIElementTypeButton type="XCUIElementTypeButton" label="video"'
        ' enabled="true" visible="true" x="120" y="100" width="80" height="40"/>',
    )
    text = serialize(parse_source(src, 400, 800))
    assert 'Button "square.grid.2x2" (selected)' in text
    assert 'Button "video" (selected)' not in text


def test_edge_cropped_row_is_flagged():
    """A chip cut off at the right edge → flagged as a scrollable row so the agent
    swipes to reveal off-screen items (e.g. a Toys category chip) instead of
    concluding it's absent."""
    src = _app(
        _btn(0, 100, 90, 40, "All Products"),
        _btn(320, 100, 120, 40, "video"),  # x+w=440 > screen_w=400 → cropped right
    )
    els = {e.label: e for e in parse_source(src, 400, 800)}
    assert els["video"].edge_cropped == "right"
    assert els["All Products"].edge_cropped == ""
    text = serialize(parse_source(src, 400, 800))
    assert "cut off at the right edge" in text and "swipe left" in text


def test_screen_signature_stable_content_based_and_deterministic():
    """The screen signature (change-detection basis) must be a real deterministic hash of
    the MEANINGFUL element state — not Python's per-process-salted hash(), not ref/pixel
    dependent — so guardrail change-detection is reliable and reproducible across runs."""
    from tamam.executors.core.contracts import Element, Frame
    from tamam.executors.ios.perceiver import screen_signature

    base = [Element(ref="e1", type="Button", label="Save", frame=Frame(0, 0, 10, 10))]
    sig = screen_signature(base)
    # deterministic lowercase hex (not salted hash())
    assert sig == screen_signature(base)
    assert sig and all(c in "0123456789abcdef" for c in sig)
    # different ref + different pixel frame, same content → SAME signature
    moved = [Element(ref="e9", type="Button", label="Save", frame=Frame(5, 7, 10, 10))]
    assert screen_signature(moved) == sig
    # a meaningful change (label, or enabled state) → DIFFERENT signature
    assert screen_signature(
        [Element(ref="e1", type="Button", label="Saved", frame=Frame(0, 0, 10, 10))]) != sig
    assert screen_signature(
        [Element(ref="e1", type="Button", label="Save", frame=Frame(0, 0, 10, 10),
                 enabled=False)]) != sig


# ---------- parent/child de-dup: a Cell and its inner StaticText are ONE target ----------

def test_nested_statictext_subsumed_by_actionable_cell():
    """A Cell whose label aggregates its inner StaticText(s) must emit ONE tappable target
    (the Cell), not the Cell PLUS each child label — that was token bloat + tap ambiguity."""
    src = _app(
        '<XCUIElementTypeCell type="XCUIElementTypeCell" label="Download over Wi-Fi, On" enabled="true"'
        ' visible="true" x="0" y="100" width="400" height="60">'
        '  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Download over Wi-Fi"'
        '   enabled="true" visible="true" x="16" y="110" width="200" height="20"/>'
        '  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="On"'
        '   enabled="true" visible="true" x="300" y="110" width="60" height="20"/>'
        '</XCUIElementTypeCell>',
    )
    els = parse_source(src, 400, 800)
    assert [e.type for e in els] == ["Cell"]
    assert els[0].label == "Download over Wi-Fi, On"


def test_independent_button_inside_cell_is_kept():
    """A genuinely actionable child (a Button in a row) is NOT passive — it must survive so
    the agent can tap it distinctly from the row."""
    src = _app(
        '<XCUIElementTypeCell type="XCUIElementTypeCell" label="Sam Lee comment" enabled="true"'
        ' visible="true" x="0" y="100" width="400" height="80">'
        '  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Sam Lee"'
        '   enabled="true" visible="true" x="16" y="110" width="200" height="20"/>'
        '  <XCUIElementTypeButton type="XCUIElementTypeButton" label="Approve"'
        '   enabled="true" visible="true" x="300" y="120" width="80" height="40"/>'
        '</XCUIElementTypeCell>',
    )
    labels = {(e.type, e.label) for e in parse_source(src, 400, 800)}
    assert ("Button", "Approve") in labels          # actionable child survives
    assert ("Cell", "Sam Lee comment") in labels
    assert ("StaticText", "Sam Lee") not in labels  # passive child subsumed


def test_bare_statictext_not_in_container_is_kept():
    """A StaticText that is NOT inside an actionable ancestor stays — some screens are just
    lists of tappable/visible StaticText, and the OCR-invisible case aside, we must not lose it."""
    src = _app(
        '<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Welcome back"'
        ' enabled="true" visible="true" x="16" y="80" width="300" height="30"/>',
    )
    els = parse_source(src, 400, 800)
    assert [(e.type, e.label) for e in els] == [("StaticText", "Welcome back")]


def test_child_text_absent_from_ancestor_label_is_kept():
    """Conservative guard: if the child's text is NOT already in the actionable ancestor's
    label, keep it — dropping it would lose information the container doesn't convey."""
    src = _app(
        '<XCUIElementTypeCell type="XCUIElementTypeCell" label="Row" enabled="true"'
        ' visible="true" x="0" y="100" width="400" height="60">'
        '  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Unique detail"'
        '   enabled="true" visible="true" x="16" y="110" width="200" height="20"/>'
        '</XCUIElementTypeCell>',
    )
    labels = {(e.type, e.label) for e in parse_source(src, 400, 800)}
    assert ("StaticText", "Unique detail") in labels  # not conveyed by "Row" → kept


def test_keyboard_keys_excluded_from_action_space():
    """Keyboard Key elements must NOT appear as tappable targets — the agent types via the
    `type` action, and on a keyboard-up screen the letter keys otherwise bury the real
    controls (the rich-text-editor stuck-loop cause)."""
    src = """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="393" height="852">
  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Note title" enabled="true" visible="true" x="20" y="120" width="300" height="40"/>
  <XCUIElementTypeButton type="XCUIElementTypeButton" label="Publish" enabled="true" visible="true" x="300" y="40" width="80" height="40"/>
  <XCUIElementTypeKey type="XCUIElementTypeKey" label="Q" enabled="true" visible="true" x="0" y="600" width="30" height="40"/>
  <XCUIElementTypeKey type="XCUIElementTypeKey" label="W" enabled="true" visible="true" x="30" y="600" width="30" height="40"/>
  <XCUIElementTypeKey type="XCUIElementTypeKey" label="space" enabled="true" visible="true" x="60" y="640" width="200" height="40"/>
</XCUIElementTypeApplication>"""
    els = parse_source(src, 393, 852)
    types = {e.type for e in els}
    assert "Key" not in types, f"keyboard keys leaked into the action space: {[e.label for e in els]}"
    # the real controls survive
    labels = {e.label for e in els}
    assert "Note title" in labels and "Publish" in labels


# ── two-state controls: state from the app, one target per control ──────────────────
# Captured shape (SwiftUI Form Toggle, iOS 26): a labelled row-wide Switch wrapping an
# unlabelled knob Switch. Before: the agent saw `Switch "Order updates"` (no state) and
# `Switch "0"`, tapped the row centre (no effect) and passed "switch is on" from a misread
# screenshot while value="0".
SWITCH_ROW = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="App" label="App" x="0" y="0" width="402" height="874">
  <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="{v}" name="Order updates" label="Order updates" enabled="true" visible="true" x="16" y="168" width="370" height="54" traits="ToggleButton, Button">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" value="Order updates" name="Order updates" label="Order updates" enabled="true" visible="true" x="32" y="183" width="110" height="24"/>
    <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="{v}" enabled="true" visible="true" x="309" y="181" width="63" height="28" traits="ToggleButton, Button"/>
  </XCUIElementTypeSwitch>
</XCUIElementTypeApplication>"""


def test_switch_row_is_one_target_with_its_state_and_knob_tap_point():
    from tamam.executors.ios.perceiver import serialize
    els = parse_source(SWITCH_ROW.format(v="0"), 402, 874)
    switches = [e for e in els if e.type == "Switch"]
    assert len(switches) == 1                       # the knob is folded into the row
    sw = switches[0]
    assert sw.label == "Order updates" and sw.toggled is False
    assert sw.tap_point == (309 + 63 / 2, 181 + 28 / 2)   # the knob, not the row centre
    assert '[' + sw.ref + '] Switch "Order updates" (off)' in serialize(els)
    on = [e for e in parse_source(SWITCH_ROW.format(v="1"), 402, 874) if e.type == "Switch"][0]
    assert on.toggled is True


def test_flipping_a_switch_is_a_screen_change():
    from tamam.executors.ios.perceiver import screen_signature
    off = screen_signature(parse_source(SWITCH_ROW.format(v="0"), 402, 874))
    on = screen_signature(parse_source(SWITCH_ROW.format(v="1"), 402, 874))
    assert off != on


def test_a_lone_unlabelled_switch_keeps_its_state_but_not_its_value_as_a_name():
    src = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="App" x="0" y="0" width="402" height="874">
  <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="1" enabled="true" visible="true" x="300" y="100" width="63" height="28"/>
</XCUIElementTypeApplication>"""
    [sw] = parse_source(src, 402, 874)
    assert sw.label == "" and sw.toggled is True


def test_toggle_state_only_for_two_state_controls():
    src = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="App" x="0" y="0" width="402" height="874">
  <XCUIElementTypeButton type="XCUIElementTypeButton" name="Bold" label="Bold" value="1" enabled="true" visible="true" x="20" y="100" width="60" height="40" traits="ToggleButton, Button"/>
  <XCUIElementTypeButton type="XCUIElementTypeButton" name="Count" label="Count" value="1" enabled="true" visible="true" x="20" y="200" width="60" height="40" traits="Button"/>
</XCUIElementTypeApplication>"""
    by = {e.label: e for e in parse_source(src, 402, 874)}
    assert by["Bold"].toggled is True       # ToggleButton trait: a two-state control
    assert by["Count"].toggled is None      # a plain button's value is not an on/off state


def test_a_separate_switch_in_another_row_is_not_folded():
    src = SWITCH_ROW.format(v="0").replace("</XCUIElementTypeApplication>", """
  <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="1" name="Sound" label="Sound" enabled="true" visible="true" x="16" y="240" width="370" height="54"/>
</XCUIElementTypeApplication>""")
    switches = [e for e in parse_source(src, 402, 874) if e.type == "Switch"]
    assert [(s.label, s.toggled) for s in switches] == [("Order updates", False), ("Sound", True)]


def test_a_knob_outside_its_row_is_not_trusted_as_the_tap_point():
    # Only redirect to an inner control that really sits inside the row; otherwise tap the
    # row's trailing edge, where iOS draws the knob.
    src = SWITCH_ROW.format(v="0").replace('x="309" y="181"', 'x="309" y="400"')
    [sw] = [e for e in parse_source(src, 402, 874) if e.type == "Switch"]
    assert sw.tap_point == (16 + 370 - 40, 168 + 54 / 2) and sw.toggled is False


def test_switch_row_label_is_not_a_second_target():
    els = parse_source(SWITCH_ROW.format(v="0"), 402, 874)
    assert [(e.type, e.label) for e in els] == [("Switch", "Order updates")]


# Captured shape (iOS 26, UIKit settings rows): a named Cell holding a Button with
# the row's text and an UNLABELLED knob-only Switch. The switch must carry its row's name.
UIKIT_ROWS = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="SampleApp" label="SampleApp" x="0" y="0" width="402" height="874">
  <XCUIElementTypeCell type="XCUIElementTypeCell" name="Email Notifications" enabled="true" visible="true" x="20" y="269" width="362" height="62" traits="">
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="Email Notifications" label="Email Notifications" enabled="true" visible="true" x="40" y="269" width="254" height="62" traits="StaticText, Button"/>
    <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="1" enabled="true" visible="true" x="302" y="287" width="62" height="25" traits="ToggleButton, Button"/>
    <XCUIElementTypeButton type="XCUIElementTypeButton" enabled="true" visible="true" x="20" y="269" width="362" height="62" traits="Button"/>
  </XCUIElementTypeCell>
  <XCUIElementTypeCell type="XCUIElementTypeCell" name="Push Notifications" enabled="true" visible="true" x="20" y="330" width="362" height="62" traits="">
    <XCUIElementTypeButton type="XCUIElementTypeButton" name="Push Notifications" label="Push Notifications" enabled="true" visible="true" x="40" y="330" width="254" height="62" traits="StaticText, Button"/>
    <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" enabled="true" visible="true" x="302" y="348" width="62" height="25" traits="ToggleButton, Button"/>
  </XCUIElementTypeCell>
</XCUIElementTypeApplication>"""


def test_uikit_row_switch_takes_its_rows_name_and_taps_its_own_centre():
    sw = {e.label: e for e in parse_source(UIKIT_ROWS, 402, 874) if e.type == "Switch"}
    assert set(sw) == {"Email Notifications", "Push Notifications"}
    assert sw["Email Notifications"].toggled is True and sw["Push Notifications"].toggled is False
    # knob-only switch (not row-wide): its own centre is the knob
    assert sw["Push Notifications"].tap_point == (302 + 31, 348 + 12.5)


def test_an_unnamed_toggle_button_is_kept():
    src = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="App" x="0" y="0" width="402" height="874">
  <XCUIElementTypeButton type="XCUIElementTypeButton" value="1" enabled="true" visible="true" x="20" y="100" width="60" height="40" traits="ToggleButton, Button"/>
</XCUIElementTypeApplication>"""
    [b] = parse_source(src, 402, 874)
    assert b.type == "Button" and b.toggled is True


def test_two_knobs_under_one_row_are_not_folded():
    src = SWITCH_ROW.format(v="0").replace("</XCUIElementTypeSwitch>\n</XCUIElementTypeApplication>", """  <XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="1" enabled="true" visible="true" x="200" y="181" width="63" height="28"/>
  </XCUIElementTypeSwitch>
</XCUIElementTypeApplication>""")
    assert src.count("XCUIElementTypeSwitch type") == 3
    assert len([e for e in parse_source(src, 402, 874) if e.type == "Switch"]) == 3


def test_an_overlay_never_moves_the_tap_off_the_knob():
    src = SWITCH_ROW.format(v="0").replace("</XCUIElementTypeApplication>", """
  <XCUIElementTypeButton type="XCUIElementTypeButton" name="Help" label="Help" enabled="true" visible="true" x="320" y="185" width="40" height="20"/>
</XCUIElementTypeApplication>""")
    [sw] = [e for e in parse_source(src, 402, 874) if e.type == "Switch"]
    assert sw.tap_point == (309 + 63 / 2, 181 + 28 / 2)
    assert sw.covered_by  # reported, so the agent knows the knob is covered


def test_accessibility_identifier_is_kept_only_when_distinct():
    src = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="App" x="0" y="0" width="402" height="874">
  <XCUIElementTypeButton type="XCUIElementTypeButton" name="addToCart" label="Add to Cart" enabled="true" visible="true" x="20" y="100" width="100" height="40"/>
  <XCUIElementTypeButton type="XCUIElementTypeButton" name="Save" label="Save" enabled="true" visible="true" x="20" y="200" width="100" height="40"/>
</XCUIElementTypeApplication>"""
    by = {e.label: e for e in parse_source(src, 402, 874)}
    assert by["Add to Cart"].identifier == "addToCart"
    assert by["Save"].identifier == ""


# ── React Native touchables with no accessibility role ────────────────

RN_HOME = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="RNSample" label="RNSample" x="0" y="0" width="402" height="874" visible="true">
 <XCUIElementTypeOther type="XCUIElementTypeOther" accessible="false" x="0" y="0" width="402" height="874" visible="true">
  <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Screens" name="Screens" x="16" y="60" width="200" height="30" visible="true" accessible="true"/>
  <XCUIElementTypeOther type="XCUIElementTypeOther" accessible="true" label="Sign In. Email and password" name="Sign In. Email and password" x="16" y="120" width="370" height="60" visible="true"/>
  <XCUIElementTypeOther type="XCUIElementTypeOther" accessible="true" label="Catalog. 500 items" name="Catalog. 500 items" x="16" y="190" width="370" height="60" visible="true"/>
  <XCUIElementTypeOther type="XCUIElementTypeOther" accessible="true" label="Form card" name="Form card" x="16" y="260" width="370" height="200" visible="true">
   <XCUIElementTypeButton type="XCUIElementTypeButton" label="Save" name="Save" x="30" y="400" width="100" height="40" visible="true" accessible="true"/>
  </XCUIElementTypeOther>
 </XCUIElementTypeOther>
</XCUIElementTypeApplication>"""


def test_a_role_less_react_native_touchable_is_a_tappable_element():
    els = parse_source(RN_HOME, 402, 874)
    labels = [e.label for e in els]
    assert "Sign In. Email and password" in labels
    assert "Catalog. 500 items" in labels


def test_a_container_or_a_screen_wrapper_is_not_listed_as_a_control():
    labels = [e.label for e in parse_source(RN_HOME, 402, 874)]
    assert "Form card" not in labels          # wraps a real control (Save), which is listed
    assert "Save" in labels
    assert not any(e.type == "Other" and e.frame.height >= 800 for e in parse_source(RN_HOME, 402, 874))


KB_FORM = """<XCUIElementTypeApplication type="XCUIElementTypeApplication" name="Shoply" label="Shoply" x="0" y="0" width="402" height="874" visible="true">
 <XCUIElementTypeTextField type="XCUIElementTypeTextField" label="Street" name="Street" x="16" y="200" width="370" height="44" visible="true"/>
 <XCUIElementTypeTextField type="XCUIElementTypeTextField" label="Postcode" name="Postcode" x="16" y="600" width="370" height="44" visible="true"/>
 <XCUIElementTypeButton type="XCUIElementTypeButton" label="Save address" name="Save address" x="16" y="700" width="370" height="44" visible="true"/>
 <XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" x="0" y="540" width="402" height="334" visible="true">
  <XCUIElementTypeButton type="XCUIElementTypeButton" label="return" name="return" x="300" y="820" width="100" height="44" visible="true"/>
 </XCUIElementTypeKeyboard>
</XCUIElementTypeApplication>"""


def test_elements_under_the_keyboard_are_marked_but_its_own_keys_are_not():
    """a field or button under the keyboard cannot be tapped (the tap would type a key)."""
    from tamam.executors.ios.perceiver import KEYBOARD
    els = {e.label: e for e in parse_source(KB_FORM, 402, 874)}
    assert els["Postcode"].covered_by == KEYBOARD and els["Save address"].covered_by == KEYBOARD
    assert els["Street"].covered_by == "" and els["return"].covered_by == ""
    assert "under the keyboard" in serialize(list(els.values()))


def test_an_icon_overhanging_the_margin_or_a_full_bleed_backdrop_is_not_a_sideways_row():
    """Wikipedia's Explore feed: its ⋮ buttons overhang the right edge by 6 pt of touch
    area and a dimming image spans past both edges. Neither is a clipped chip; flagging them
    sent the agent hunting for a sideways scroll that does not exist."""
    src = _app(
        _btn(358, 82, 50, 50, "overflow"),                # 6 pt past a 402 pt screen
        _btn(-120, 300, 642, 200, "Backdrop"),            # past both edges
        _btn(330, 600, 120, 40, "video"),                 # a real clipped chip: 48 pt hidden
    )
    els = {e.label: e for e in parse_source(src, 402, 874)}
    assert els["overflow"].edge_cropped == ""
    assert els["Backdrop"].edge_cropped == ""
    assert els["video"].edge_cropped == "right"


def test_a_multiline_text_view_is_a_text_input():
    """an RN form: the Notes box (a React Native multiline TextInput, an
    XCUIElementTypeTextView) was dropped, and the agent spent 10 steps tap_pointing at it."""
    tv = ('<XCUIElementTypeTextView type="XCUIElementTypeTextView" name="notes-input" enabled="true"'
          ' visible="true" accessible="true" x="25" y="334" width="352" height="95"/>')
    els = parse_source(_app(_btn(25, 500, 120, 40, "Submit"), tv), 400, 800)
    notes = next(e for e in els if e.type == "TextView")
    assert notes.label == "notes-input" and not notes.filled
    assert 'TextView "notes-input" (empty)' in serialize(els)


def test_the_keyboard_top_is_read_from_the_tree():
    from tamam.executors.ios.perceiver import keyboard_top
    kb = ('<XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" visible="true" enabled="true"'
          ' x="0" y="540" width="400" height="260"/>')
    assert keyboard_top(_app(_btn(0, 100, 90, 40, "Go"), kb)) == 540
    assert keyboard_top(_app(_btn(0, 100, 90, 40, "Go"))) == 0


async def test_a_keyboard_that_slid_off_the_bottom_is_down():
    """the keyboard element stayed in the tree at y=952 on an 874-pt screen after it went
    away; the screen read as keyboard-up, so taps were refused 'on the keyboard'."""
    from tamam.executors.ios.perceiver import IOSPerceiver

    class D:
        def __init__(self, y): self.y = y
        async def accessibility(self):
            return _app(_btn(0, 100, 90, 40, "Go"),
                        f'<XCUIElementTypeKeyboard type="XCUIElementTypeKeyboard" visible="true" enabled="true"'
                        f' x="0" y="{self.y}" width="400" height="300"/>')
        def screen_size(self): return (400, 800)
        async def dismiss_alert(self): return None

    off = await IOSPerceiver().observe(D(952))
    on = await IOSPerceiver().observe(D(540))
    assert (off.keyboard, off.keyboard_top) == (False, 0.0)
    assert (on.keyboard, on.keyboard_top) == (True, 540)


def test_an_unlabelled_switch_is_named_by_the_text_on_its_own_row():
    """Flutter puts a whole form in one container; 'the first labelled sibling' named both
    unlabelled switches 'Date' (the form's first label), and a false pass followed."""
    def text(x, y, w, label):
        return (f'<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="{label}" name="{label}" '
                f'enabled="true" visible="true" x="{x}" y="{y}" width="{w}" height="24"/>')

    def switch(x, y):
        return (f'<XCUIElementTypeSwitch type="XCUIElementTypeSwitch" value="0" enabled="true" visible="true" '
                f'x="{x}" y="{y}" width="48" height="48" traits="ToggleButton, Button"/>')
    src = _app('<XCUIElementTypeOther type="XCUIElementTypeOther" x="0" y="0" width="400" height="800">'
               + text(20, 394, 38, "Date") + switch(20, 586) + text(68, 598, 114, "Brushed Teeth")
               + text(20, 670, 116, "Enable feature") + switch(322, 658) + "</XCUIElementTypeOther>")
    switches = [e for e in parse_source(src, 400, 800) if e.type == "Switch"]
    assert [s.label for s in switches] == ["Brushed Teeth", "Enable feature"]
