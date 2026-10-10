"""OCRGrounder — deterministic Tesseract grounding. The critical property is the
coordinate mapping: retina PIXELS → screen POINTS, so a tap lands where the text is.
Skips cleanly if the tesseract binary isn't installed (CI without it)."""
import io

import pytest

from tamam.executors.core.contracts import ActionSpace
from tamam.executors.ios.grounder import OCRGrounder, build_grounder

pytest.importorskip("pytesseract")
try:
    import pytesseract
    pytesseract.get_tesseract_version()
except Exception:
    pytest.skip("tesseract binary not installed", allow_module_level=True)


def _image_with_text(text: str, px: int, py: int, screen_pt: tuple[int, int], scale: int):
    from PIL import Image, ImageDraw, ImageFont
    w_pt, h_pt = screen_pt
    img = Image.new("RGB", (w_pt * scale, h_pt * scale), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for path in ("/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/System/Library/Fonts/Helvetica.ttc"):
        try:
            font = ImageFont.truetype(path, 64)
            break
        except Exception:
            continue
    draw.text((px, py), text, fill="black", font=font or ImageFont.load_default())
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_ocr_grounder_maps_pixels_to_screen_points():
    screen_pt = (393, 852)
    scale = 3  # retina: 1179x2556 px for a 393x852-pt screen
    png = _image_with_text("Edit target alpha", 300, 400, screen_pt, scale)
    space = ActionSpace(elements=[], text="", screen_w=393, screen_h=852, source_hash="x")

    grounded = await OCRGrounder(min_conf=0).detect(png, None, space)

    labels = " ".join(g.label for g in grounded).lower()
    assert "edit" in labels and "alpha" in labels, f"OCR missed the text; saw: {labels!r}"
    g = next(g for g in grounded if "edit" in g.label.lower())
    # Drawn at pixel (300, 400) with scale 3 → ~ (100, 133) POINTS. Frame must be in points.
    assert 80 <= g.frame.x <= 150, f"x not mapped to points: {g.frame.x}"
    assert 110 <= g.frame.y <= 180, f"y not mapped to points: {g.frame.y}"


async def test_ocr_grounder_skips_text_already_in_a11y_tree():
    from tamam.executors.core.contracts import Element, Frame
    screen_pt = (393, 852)
    png = _image_with_text("Publish", 300, 400, screen_pt, 3)
    # a11y already exposes a "Publish" button → OCR must NOT duplicate it
    space = ActionSpace(
        elements=[Element(ref="e1", type="Button", label="Publish", frame=Frame(0, 0, 10, 10))],
        text='[e1] Button "Publish"', screen_w=393, screen_h=852, source_hash="x")
    grounded = await OCRGrounder(min_conf=0).detect(png, None, space)
    assert all("publish" != g.label.strip().lower() for g in grounded)


def test_build_grounder_factory():
    from tamam.executors.ios.grounder import NullGrounder
    assert isinstance(build_grounder("ocr"), OCRGrounder)
    assert isinstance(build_grounder("null"), NullGrounder)
    assert isinstance(build_grounder("anything-else"), NullGrounder)


# ── aggressive edge cases ──

async def test_ocr_blank_image_returns_nothing():
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (1179, 2556), "white").save(buf, format="PNG")
    space = ActionSpace(elements=[], text="", screen_w=393, screen_h=852, source_hash="x")
    assert await OCRGrounder().detect(buf.getvalue(), None, space) == []


async def test_ocr_invalid_bytes_never_crashes():
    space = ActionSpace(elements=[], text="", screen_w=393, screen_h=852, source_hash="x")
    assert await OCRGrounder().detect(b"not a png", None, space) == []
    assert await OCRGrounder().detect(b"", None, space) == []


async def test_ocr_multiline_returns_multiple_targets():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (393 * 3, 852 * 3), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            font = ImageFont.truetype(p, 60)
            break
        except Exception:
            pass
    font = font or ImageFont.load_default()
    draw.text((200, 300), "Alpha heading", fill="black", font=font)
    draw.text((200, 700), "Beta section", fill="black", font=font)
    buf = io.BytesIO(); img.save(buf, format="PNG")
    space = ActionSpace(elements=[], text="", screen_w=393, screen_h=852, source_hash="x")
    g = await OCRGrounder(min_conf=0).detect(buf.getvalue(), None, space)
    labels = " | ".join(x.label.lower() for x in g)
    assert "alpha" in labels and "beta" in labels
    # the two lines are vertically separated in POINTS (300/3≈100 vs 700/3≈233)
    ys = sorted(x.frame.y for x in g if "alpha" in x.label.lower() or "beta" in x.label.lower())
    assert ys and (ys[-1] - ys[0]) > 80


async def test_ocr_respects_max_targets_cap():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (393 * 3, 852 * 3), "white")
    draw = ImageDraw.Draw(img)
    font = None
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            font = ImageFont.truetype(p, 34)
            break
        except Exception:
            pass
    font = font or ImageFont.load_default()
    for i in range(30):
        draw.text((100, 150 + i * 75), f"Line number {i}", fill="black", font=font)
    buf = io.BytesIO(); img.save(buf, format="PNG")
    space = ActionSpace(elements=[], text="", screen_w=393, screen_h=852, source_hash="x")
    g = await OCRGrounder(min_conf=0, max_targets=5).detect(buf.getvalue(), None, space)
    assert len(g) <= 5


@pytest.mark.parametrize("scale", [2, 3])
async def test_ocr_coordinate_mapping_across_scales(scale):
    png = _image_with_text("Findme", 240, 360, (393, 852), scale)
    space = ActionSpace(elements=[], text="", screen_w=393, screen_h=852, source_hash="x")
    g = await OCRGrounder(min_conf=0).detect(png, None, space)
    hit = next((x for x in g if "findme" in x.label.lower()), None)
    assert hit is not None
    # 240px / scale points, 360px / scale points — independent of the retina scale
    assert abs(hit.frame.x - 240 / scale) < 30
    assert abs(hit.frame.y - 360 / scale) < 40


async def test_ocr_merges_into_action_space_as_tappable_ref():
    """End-to-end seam: an OCR detection the a11y tree lacks becomes a normal [e#] ref the
    brain can tap, with real coordinates — the browser-use 'model picks what, we compute
    where' model, sourced from pixels."""
    from tamam.executors.ios.grounder import merge_grounded
    png = _image_with_text("Edit target alpha", 300, 400, (393, 852), 3)
    space = ActionSpace(elements=[], text="", screen_w=393, screen_h=852, source_hash="x")
    grounded = await OCRGrounder(min_conf=0).detect(png, None, space)
    merged = merge_grounded(space, grounded)
    assert len(merged.elements) > 0
    title = next((e for e in merged.elements if "edit" in e.label.lower()), None)
    assert title is not None and title.ref.startswith("e")
    assert title.frame.center[1] > 0  # has a real screen-point coordinate to tap


async def test_text_just_above_the_real_keyboard_is_kept():
    """with a keyboard up, OCR dropped everything below 55% of the screen; a form's
    'Password' label just above the keyboard (keyboard_top from the tree) was lost."""
    from tamam.executors.core.contracts import Element, Frame
    screen_pt, scale = (402, 874), 3
    png = _image_with_text("Password", 60 * scale, 494 * scale, screen_pt, scale)
    keys = [Element(ref="k1", type="Button", label="shift", frame=Frame(0, 700, 60, 40))]
    with_top = ActionSpace(elements=keys, screen_w=402, screen_h=874, keyboard=True, keyboard_top=560)
    found = await OCRGrounder().detect(png, None, with_top)
    assert any(g.label == "Password" for g in found)
    guessed = ActionSpace(elements=keys, screen_w=402, screen_h=874)        # no tree top: 55% guess
    assert not any(g.label == "Password" for g in await OCRGrounder().detect(png, None, guessed))
