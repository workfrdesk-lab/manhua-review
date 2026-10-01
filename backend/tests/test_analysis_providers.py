from PIL import Image, ImageDraw

from app.analysis_providers import (
    BasicVisualAnalyzer,
    DetectedPanel,
    full_page,
    group_text,
    reading_order,
)


def synthetic_page():
    image = Image.new("RGB", (400, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 390, 290), fill="#d5e8f2")
    draw.rectangle((10, 310, 390, 590), fill="#f0d5d5")
    return image


def test_fallback_covers_entire_page():
    panel = full_page(synthetic_page())[0]
    assert (panel.x, panel.y, panel.width, panel.height) == (0, 0, 400, 600)
    assert panel.kind == "FULL_PAGE"


def test_reading_order_ltr_and_rtl():
    panels = [
        DetectedPanel(220, 0, 100, 100),
        DetectedPanel(10, 0, 100, 100),
        DetectedPanel(10, 150, 100, 100),
    ]
    assert [p.x for p in reading_order(panels, "ltr")] == [10, 220, 10]
    assert [p.x for p in reading_order(panels, "rtl")] == [220, 10, 10]


def test_ocr_regions_are_grouped_into_bubbles():
    regions = [
        {"text": "Who", "confidence": 0.9, "box": [10, 10, 30, 15]},
        {"text": "are you?", "confidence": 0.8, "box": [10, 28, 55, 15]},
        {"text": "Goodbye", "confidence": 0.9, "box": [250, 300, 50, 15]},
    ]
    bubbles = group_text(regions, "ltr")
    assert [bubble["text"] for bubble in bubbles] == ["Who\nare you?", "Goodbye"]


def test_visual_analysis_is_explicitly_heuristic():
    result = BasicVisualAnalyzer().analyze(synthetic_page())
    assert result["heuristic"] is True
    assert 0 <= result["brightness"] <= 1
