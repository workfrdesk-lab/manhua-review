"""Independent analysis algorithms. No third-party project source is incorporated."""

from dataclasses import dataclass, field
from typing import Protocol

from PIL import Image, ImageFilter, ImageStat


@dataclass
class DetectedPanel:
    x: int
    y: int
    width: int
    height: int
    confidence: float = 0.7
    kind: str = "detected"


@dataclass
class OCRData:
    regions: list[dict] = field(default_factory=list)
    language: str | None = None
    raw: dict = field(default_factory=dict)


class PanelDetector(Protocol):
    def detect(self, image: Image.Image) -> list[DetectedPanel]: ...


class OCRProvider(Protocol):
    def extract(self, image: Image.Image) -> OCRData: ...


class ImageAnalyzer(Protocol):
    def analyze(self, image: Image.Image) -> dict: ...


def full_page(image: Image.Image) -> list[DetectedPanel]:
    return [DetectedPanel(0, 0, image.width, image.height, 0.1, "FULL_PAGE")]


def reading_order(panels: list[DetectedPanel], direction: str) -> list[DetectedPanel]:
    """Cluster rows by vertical overlap, then order within rows explicitly."""
    rows: list[list[DetectedPanel]] = []
    for panel in sorted(panels, key=lambda p: (p.y, p.x)):
        for row in rows:
            anchor = row[0]
            overlap = min(anchor.y + anchor.height, panel.y + panel.height) - max(anchor.y, panel.y)
            if overlap >= 0.5 * min(anchor.height, panel.height):
                row.append(panel)
                break
        else:
            rows.append([panel])
    return [p for row in rows for p in sorted(row, key=lambda p: p.x, reverse=direction == "rtl")]


class OpenCVPanelDetector:
    def detect(self, image: Image.Image) -> list[DetectedPanel]:
        import cv2
        import numpy as np

        original = image.size
        small = image.convert("RGB")
        small.thumbnail((1600, 2400))
        rgb = np.asarray(small)
        height, width = rgb.shape[:2]
        # Uniform rows/columns identify white, black AND colored gutters.
        regions = [(0, 0, width, height)]
        for _ in range(8):
            next_regions = []
            changed = False
            for x, y, w, h in regions:
                section = rgb[y : y + h, x : x + w].astype(float)
                split = None
                for axis, size in ((1, h), (0, w)):
                    uniform = np.max(np.std(section, axis=axis), axis=1) < 5
                    indices = np.flatnonzero(uniform)
                    groups = np.split(indices, np.where(np.diff(indices) > 1)[0] + 1)
                    valid = [
                        g
                        for g in groups
                        if len(g) >= max(3, size * 0.008)
                        and g[0] > size * 0.12
                        and g[-1] < size * 0.88
                    ]
                    if valid:
                        g = max(valid, key=len)
                        split = (axis, int(g[0]), int(g[-1]) + 1)
                        break
                if split and len(regions) < 64:
                    axis, start, end = split
                    if axis == 1:
                        next_regions.extend([(x, y, w, start), (x, y + end, w, h - end)])
                    else:
                        next_regions.extend([(x, y, start, h), (x + end, y, w - end, h)])
                    changed = True
                else:
                    next_regions.append((x, y, w, h))
            regions = next_regions
            if not changed:
                break
        if len(regions) == 1:
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            edges = cv2.Canny(gray, 50, 150)
            edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            regions = []
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                # Bounding rectangles are approximations, not assertions of panel shape.
                if w * h >= width * height * 0.06 and w > width * 0.12 and h > height * 0.08:
                    if cv2.contourArea(contour) > w * h * 0.45:
                        regions.append((x, y, w, h))
        if not regions:
            return full_page(image)
        sx, sy = original[0] / width, original[1] / height
        return [
            DetectedPanel(
                round(x * sx),
                round(y * sy),
                min(original[0] - round(x * sx), max(1, round(w * sx))),
                min(original[1] - round(y * sy), max(1, round(h * sy))),
            )
            for x, y, w, h in regions[:64]
        ]


class PaddleOCRProvider:
    """Lazy adapter for PaddleOCR 3.x. Requires separately installed paddlepaddle."""

    def __init__(self, language: str = "en"):
        self.language = language
        self.engine = None

    def extract(self, image: Image.Image) -> OCRData:
        import numpy as np
        from paddleocr import PaddleOCR

        if self.engine is None:
            self.engine = PaddleOCR(
                lang=self.language,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        regions = []
        raw = []
        for result in self.engine.predict(np.asarray(image.convert("RGB"))):
            payload = result.json
            if isinstance(payload, str):
                import json

                payload = json.loads(payload)
            raw.append(payload)
            value = payload.get("res", payload)
            for text, score, polygon in zip(
                value["rec_texts"], value["rec_scores"], value["rec_polys"], strict=True
            ):
                xs, ys = zip(*polygon, strict=True)
                regions.append(
                    {
                        "text": text,
                        "confidence": float(score),
                        "polygon": polygon,
                        "box": [
                            float(min(xs)),
                            float(min(ys)),
                            float(max(xs) - min(xs)),
                            float(max(ys) - min(ys)),
                        ],
                    }
                )
        return OCRData(regions, self.language, {"results": raw, "provider": "paddleocr"})


def group_text(regions: list[dict], direction: str) -> list[dict]:
    """Connected spatial components; heuristic grouping, not bubble recognition."""
    groups: list[list[dict]] = []
    for region in regions:
        x, y, w, h = region["box"]
        matches = []
        for group in groups:
            for other in group:
                ox, oy, ow, oh = other["box"]
                dx = max(0, max(x, ox) - min(x + w, ox + ow))
                dy = max(0, max(y, oy) - min(y + h, oy + oh))
                if dx <= min(h, oh) * 1.5 and dy <= max(h, oh) * 1.2:
                    matches.append(group)
                    break
        merged = [region]
        for group in matches:
            merged.extend(group)
            groups.remove(group)
        groups.append(merged)
    groups.sort(
        key=lambda g: (
            min(r["box"][1] for r in g),
            (-1 if direction == "rtl" else 1) * min(r["box"][0] for r in g),
        )
    )
    bubbles = []
    for index, group in enumerate(groups, 1):
        ordered = reading_order([DetectedPanel(*r["box"]) for r in group], direction)
        lines = [
            group[next(i for i, r in enumerate(group) if r["box"] == [p.x, p.y, p.width, p.height])]
            for p in ordered
        ]
        bubbles.append(
            {
                "reading_order": index,
                "text": "\n".join(r["text"] for r in lines),
                "regions": lines,
                "heuristic": True,
            }
        )
    return bubbles


class BasicVisualAnalyzer:
    def analyze(self, image: Image.Image) -> dict:
        small = image.convert("RGB")
        small.thumbnail((512, 512))
        gray = small.convert("L")
        stats = ImageStat.Stat(gray)
        edges = gray.filter(ImageFilter.FIND_EDGES)
        density = sum(n for value, n in enumerate(edges.histogram()) if value > 40) / (
            gray.width * gray.height
        )
        rgb = ImageStat.Stat(small).mean
        return {
            "heuristic": True,
            "brightness": stats.mean[0] / 255,
            "contrast": stats.stddev[0] / 255,
            "dominant_tone": "warm"
            if rgb[0] > rgb[2] + 10
            else "cool"
            if rgb[2] > rgb[0] + 10
            else "neutral",
            "edge_density": density,
            "face_presence": None,
            "action_score": density,
            "focal_region": None,
            "limitations": (
                "Edge density is only an action proxy; faces and focal region are not inferred."
            ),
        }
