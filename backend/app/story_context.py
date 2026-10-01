import json
import re
from dataclasses import dataclass


@dataclass
class ContextChunk:
    index: int
    pages: list[int]
    text: str


class ContextBuilder:
    """Builds bounded, source-labelled context; it never calls an LLM."""

    def build(self, pages, panels_by_page, ocr_by_panel, visual_by_panel) -> str:
        rows = []
        for page in sorted(pages, key=lambda p: p.page_number):
            rows.append(f"Page {page.page_number}")
            rows.append(f"Page ID: {page.id}")
            for panel in sorted(panels_by_page.get(page.id, []), key=lambda p: p.reading_order):
                ocr = ocr_by_panel.get(panel.id, {})
                visual = visual_by_panel.get(panel.id, {})
                rows.append(f"Panel {panel.panel_index}\nReading order: {panel.reading_order}")
                rows.append(f"Panel ID: {panel.id}\nOCR result ID: {ocr.get('id', '')}")
                rows.append(f"OCR: {ocr.get('text', '')[:4000]}")
                rows.append(f"Visual: {json.dumps(visual, ensure_ascii=False)[:1200]}")
        return "\n\n".join(rows)


class ContextChunker:
    def __init__(self, max_chars: int = 18000, overlap_pages: int = 1, max_pages: int = 8):
        if max_chars < 32 or max_pages < 1 or not 0 <= overlap_pages < max_pages:
            raise ValueError("Invalid story chunk limits")
        self.max_chars = max_chars
        self.overlap_pages = overlap_pages
        self.max_pages = max_pages

    def chunk(self, context: str) -> list[ContextChunk]:
        if not context.strip():
            return []
        pages = re.split(r"\n\n(?=Page \d+\n|Page \d+$)", context.strip())
        chunks, current, current_pages = [], [], []

        def emit():
            chunks.append(ContextChunk(len(chunks), list(current_pages), "\n\n".join(current)))

        for raw in pages:
            match = re.match(r"Page ([1-9]\d*)(?:\n|$)", raw)
            if not match:
                raise ValueError("Context must contain numbered page sections")
            number = int(match.group(1))
            if len(raw) > self.max_chars:
                # Fail closed rather than split away panel/OCR source labels or silently
                # truncate evidence. Structured panel batching is needed for such pages.
                raise ValueError(f"Page {number} exceeds the story context limit")
            if current and (
                len("\n\n".join(current + [raw])) > self.max_chars or len(current) >= self.max_pages
            ):
                emit()
                keep = self.overlap_pages
                current = current[-keep:] if keep else []
                current_pages = current_pages[-keep:] if keep else []
                # Overlap is best-effort; it must never defeat the hard request limit.
                while current and len("\n\n".join(current + [raw])) > self.max_chars:
                    current.pop(0)
                    current_pages.pop(0)
            current.append(raw)
            current_pages.append(number)
        if current:
            emit()
        return chunks
