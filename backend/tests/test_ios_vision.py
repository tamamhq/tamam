"""AXFirstVisionPolicy — when to fall back to vision.

Vision fires when the accessibility text can't carry the screen: too few elements,
or the tappable controls are icon-only / unlabeled (e.g. an SF-Symbol filter bar).
"""
from tamam.executors.core.contracts import ActionSpace, Element, Frame
from tamam.executors.ios.vision import AXFirstVisionPolicy, _content_dead_zone, _uninformative


def _el(ref, typ, label):
    return Element(ref=ref, type=typ, label=label, frame=Frame(0, 0, 40, 40))


def _elf(ref, typ, label, frame):
    return Element(ref=ref, type=typ, label=label, frame=frame)


def test_symbolish_labels_are_uninformative():
    for s in ("square.grid.2x2", "tag.circle", "", "video.badge.checkmark"):
        assert _uninformative(s)
    for s in ("Toys", "Log In", "Save filter"):
        assert not _uninformative(s)


def test_fires_when_too_few_elements():
    sp = ActionSpace(elements=[_el("e1", "Button", "OK")])
    assert AXFirstVisionPolicy().should_see(sp)


def test_fires_on_icon_only_control_bar():
    """A filter bar of SF-Symbol buttons amid labeled content still triggers vision."""
    els = [_el(f"e{i}", "Cell", f"Product {i}") for i in range(1, 6)]  # labeled content
    els += [_el("f1", "Button", "square.grid.2x2"), _el("f2", "Button", "shippingbox"),
            _el("f3", "Button", "tag.circle")]  # 3 icon-only controls
    assert AXFirstVisionPolicy().should_see(ActionSpace(elements=els))


def test_does_not_fire_on_well_labeled_screen():
    els = [_el(f"e{i}", "Button", name) for i, name in
           enumerate(["Electronics", "Books", "Toys", "Garden"], 1)]
    els += [_el(f"c{i}", "Cell", f"Product {i}") for i in range(1, 4)]
    assert not AXFirstVisionPolicy().should_see(ActionSpace(elements=els))


def test_fires_on_content_dead_zone():
    """A product-detail page whose body is custom-drawn / not in the a11y tree exposes
    only nav chrome (Back, arrows, tab bar, a few icons) — the content region is a
    dead zone, so vision must fire even though the chrome elements look labeled.
    Without it the agent was blind to the 'Product details' section."""
    W, H = 402, 874
    chrome = [
        _elf("e1", "Button", "Back", Frame(16, 60, 44, 44)),
        _elf("e2", "Button", "go down", Frame(300, 60, 44, 44)),
        _elf("e3", "Button", "go up", Frame(350, 60, 44, 44)),
    ]
    tab = [_elf(f"t{i}", "TabBarButton", name, Frame(10 + i * 78, 815, 60, 50))
           for i, name in enumerate(["Shop", "Offers", "Cart", "Orders", "More"])]
    icons = [_elf(f"m{i}", "Image", n, Frame(10 + i * 78, 830, 30, 30))
             for i, n in enumerate(["bag", "tag", "cart", "list.bullet", "ellipsis"])]
    sp = ActionSpace(elements=chrome + tab + icons, screen_w=W, screen_h=H)
    assert _content_dead_zone(sp) >= 0.6
    assert AXFirstVisionPolicy().should_see(sp)


def test_dead_zone_does_not_fire_when_content_present():
    """A feed of full-width cards covers the content region — the tree is describing
    the screen, so the dead-zone trigger stays quiet (no wasted screenshot)."""
    W, H = 402, 874
    cards = [_elf(f"c{i}", "Cell", f"Product card {i}", Frame(10, 120 + i * 165, 382, 150))
             for i in range(4)]
    chips = [_elf(f"h{i}", "Button", n, Frame(10 + i * 95, 70, 85, 36))
             for i, n in enumerate(["Electronics", "Books", "Garden", "Toys"])]
    sp = ActionSpace(elements=cards + chips, screen_w=W, screen_h=H)
    assert _content_dead_zone(sp) < 0.6
    assert not AXFirstVisionPolicy().should_see(sp)


def test_low_info_region_targets_icon_cluster():
    """The ROI is the bounding box of the icon-only control cluster (the ambiguous
    chips a grounder must disambiguate — here the square.grid.2x2 vs tag.circle
    pair), not the whole screen. NOTE: dotted SF-symbol names are what
    the current _SYMBOLISH recognizes; broadening it to single-word icon names
    (shippingbox) is a tracked follow-up that would only widen this ROI."""
    from tamam.executors.ios.vision import low_info_region
    els = [
        _elf("e1", "Button", "square.grid.2x2", Frame(120, 70, 40, 40)),
        _elf("e2", "Button", "tag.circle", Frame(220, 70, 40, 40)),
        _elf("e3", "Cell", "Some Product Card", Frame(10, 300, 380, 120)),  # labeled, ignored
    ]
    r = low_info_region(ActionSpace(elements=els, screen_w=402, screen_h=874))
    assert r is not None
    x, y, w, h = r
    assert 110 <= x <= 130 and 60 <= y <= 80        # starts at the first icon chip
    assert w >= 130 and h <= 60                       # spans the icon pair, not the card


def test_low_info_region_none_when_labeled():
    from tamam.executors.ios.vision import low_info_region
    els = [_elf(f"c{i}", "Cell", f"Product {i}", Frame(10, 120 + i * 165, 382, 150)) for i in range(4)]
    assert low_info_region(ActionSpace(elements=els, screen_w=402, screen_h=874)) is None


def test_crop_region_shrinks_image():
    from io import BytesIO
    from PIL import Image
    from tamam.executors.ios.vision import crop_region
    buf = BytesIO(); Image.new("RGB", (804, 1748)).save(buf, format="PNG")  # 2x of 402x874
    out = crop_region(buf.getvalue(), (100.0, 100.0, 80.0, 40.0), screen_w=402)
    w, h = Image.open(BytesIO(out)).size
    assert w < 804 and h < 1748  # cropped to the (padded) region, not the full frame


def test_the_agent_view_carries_a_labelled_coordinate_grid(tmp_path):
    """a coordinate tap is read off the image, not estimated from a guessed size."""
    import io

    from PIL import Image

    from tamam.executors.core.contracts import ActionSpace
    from tamam.executors.ios.vision import set_of_marks
    buf = io.BytesIO()
    Image.new("RGB", (400, 800), (255, 255, 255)).save(buf, format="PNG")
    out = set_of_marks(buf.getvalue(), ActionSpace(elements=[], text="", screen_w=200, screen_h=400,
                                                    source_hash="h"), tmp_path / "som.png")
    img = Image.open(out).convert("RGB")
    assert img.getpixel((200, 400)) != (255, 255, 255)      # the 0.5 lines cross mid-image
    assert img.getpixel((40, 760)) != (255, 255, 255)       # the x=.1 line runs full height


def test_merging_ocr_targets_keeps_everything_else_the_read_carried():
    """merge_grounded rebuilt the space and dropped the keyboard flag (and an app dialog's
    title, harness notes, the step). With OCR on, a screen with a keyboard read as keyboard-free:
    the under-keyboard tap guard and the keyboard hook never ran."""
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    from tamam.executors.ios.grounder import GroundedElement, merge_grounded
    space = ActionSpace(elements=[Element(ref="e1", type="TextField", label="search-input", frame=Frame(0, 100, 390, 40))],
                        text='[e1] TextField "search-input"', screen_w=390, screen_h=844, source_hash="h",
                        keyboard=True, alert="Delete draft?", step=4, harness_notes=["[harness] accepted"],
                        context="checklist")
    out = merge_grounded(space, [GroundedElement(label="< Back", frame=Frame(0, 50, 80, 30))])
    assert len(out.elements) == 2 and out.elements[1].ref == "e2"
    assert (out.keyboard, out.alert, out.step, out.harness_notes, out.context) == \
           (True, "Delete draft?", 4, ["[harness] accepted"], "checklist")


def _form_png(tmp_path, *, bg=240, ink=190, box=(60, 104), underline=None, name="f.png"):
    """A 402x300 pt form at 1x: a label at y=40..52 and, below it, a bordered box or an underline."""
    from PIL import Image, ImageDraw
    im = Image.new("L", (402, 300), bg)
    d = ImageDraw.Draw(im)
    d.rectangle([24, 40, 110, 52], fill=ink)                       # the label's text
    if box:
        d.rectangle([16, box[0], 386, box[1]], outline=ink, width=2)
    if underline:
        d.line([16, underline, 386, underline], fill=ink, width=2)
    p = tmp_path / name
    im.save(p)
    return str(p)


def test_the_input_box_under_a_label_is_found_in_the_pixels(tmp_path):
    """a flattened form hides its fields from the tree; the box under 'First name' is found
    by its border lines (offline on an RN screenshot: all four boxes to the point)."""
    from tamam.executors.core.contracts import Frame
    from tamam.executors.ios.grounder import input_box_below
    label = Frame(24, 40, 86, 12)
    b = input_box_below(_form_png(tmp_path), label, 402)
    assert b is not None and abs(b.y - 60) <= 2 and abs(b.y + b.height - 104) <= 2
    dark = input_box_below(_form_png(tmp_path, bg=20, ink=90, name="d.png"), label, 402)
    assert dark is not None and abs(dark.center[1] - 82) <= 2           # dark mode too
    under = input_box_below(_form_png(tmp_path, box=None, underline=90, name="u.png"), label, 402)
    assert under is not None and 60 <= under.center[1] < 90              # just above the line
    assert input_box_below(_form_png(tmp_path, box=None, name="n.png"), label, 402) is None
    assert input_box_below(_form_png(tmp_path, box=(60, 66), name="t.png"), label, 402) is None


def test_a_merged_form_container_sends_the_screenshot():
    """a flattened sign-in form is ONE element ('Use …, Email, Password, SIGN IN') covering the
    form; the tree looks complete, so OCR never ran and the agent never saw 'Email'."""
    from tamam.executors.core.contracts import ActionSpace, Element, Frame
    nav = Element(ref="e1", type="Button", label="Sample Screens", frame=Frame(16, 62, 220, 44))
    title = Element(ref="e2", type="StaticText", label="Sign In", frame=Frame(248, 70, 130, 24))
    form = Element(ref="e3", type="Other", label="Use demo@example.com / demo1234, Email, Password, SIGN IN",
                   frame=Frame(0, 116, 402, 600))
    assert AXFirstVisionPolicy().should_see(ActionSpace(elements=[nav, title, form], screen_w=402, screen_h=874))
    from tamam.executors.ios.vision import _merged_container
    row = Element(ref="e3", type="Cell", label="Wireless Mouse, $25, In stock", frame=Frame(16, 300, 370, 60))
    assert not _merged_container(ActionSpace(elements=[nav, title, row], screen_w=402, screen_h=874))  # a row
