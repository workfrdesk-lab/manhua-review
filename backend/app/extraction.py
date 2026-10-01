"""Bounded ingestion; archives are never extracted onto the filesystem."""

import io
import re
import stat
import threading
import warnings
import zipfile
from contextlib import closing
from pathlib import PurePosixPath

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import get_settings

PDF_LOCK = threading.Lock()  # PDFium is not thread-safe.
MAX_PIXELS = 25_000_000
MAX_PAGES = 300


def natural_key(name: str):
    return [
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", name)
    ]


def image_bytes(data: bytes) -> tuple[bytes, bytes, int, int]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in {"PNG", "JPEG"}:
                    raise ValueError("Only PNG and JPEG images are supported")
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError("Image pixel limit exceeded")
                source.verify()
            with Image.open(io.BytesIO(data)) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                try:
                    width, height = image.size
                    output = io.BytesIO()
                    image.save(output, format="JPEG", quality=92)
                    image.thumbnail((320, 480))
                    thumb = io.BytesIO()
                    image.save(thumb, format="JPEG", quality=80)
                    return output.getvalue(), thumb.getvalue(), width, height
                finally:
                    image.close()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid or corrupt image") from exc


def detect(data: bytes, filename: str, mime: str) -> str:
    extension = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if data.startswith(b"%PDF-"):
        kind, extensions, mimes = "pdf", {".pdf"}, {"application/pdf"}
    elif data.startswith(b"PK\x03\x04"):
        kind, extensions, mimes = (
            "zip",
            {".zip"},
            {"application/zip", "application/x-zip-compressed"},
        )
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind, extensions, mimes = "png", {".png"}, {"image/png"}
    elif data.startswith(b"\xff\xd8\xff"):
        kind, extensions, mimes = "jpg", {".jpg", ".jpeg"}, {"image/jpeg"}
    else:
        raise ValueError("Unsupported or corrupt file signature")
    if extension not in extensions or mime.lower() not in mimes:
        raise ValueError("File extension, MIME type and signature must match")
    return kind


def extract(data: bytes, kind: str):
    """Yield normalized page bytes, thumbnail bytes, width, height in order."""
    if kind in {"png", "jpg"}:
        yield image_bytes(data)
    elif kind == "pdf":
        with PDF_LOCK, closing(pdfium.PdfDocument(data)) as document:
            if not 0 < len(document) <= MAX_PAGES:
                raise ValueError("PDF page limit exceeded")
            for index in range(len(document)):
                with closing(document[index]) as page:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0 or width * height * 4 > MAX_PIXELS:
                        raise ValueError("PDF page pixel limit exceeded")
                    bitmap = page.render(scale=2)
                    try:
                        image = bitmap.to_pil()
                        output = io.BytesIO()
                        image.save(output, format="PNG")
                        image.close()
                        yield image_bytes(output.getvalue())
                    finally:
                        bitmap.close()
    elif kind == "zip":
        settings = get_settings()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > settings.max_zip_entries:
                raise ValueError("Too many ZIP entries")
            total = sum(entry.file_size for entry in entries)
            if total > settings.max_extracted_bytes:
                raise ValueError("ZIP expanded size limit exceeded")
            accepted = []
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if (
                    entry.is_dir()
                    or path.is_absolute()
                    or "\\" in entry.filename
                    or ":" in entry.filename
                    or any(p.startswith(".") for p in path.parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    continue
                if entry.flag_bits & 1 or entry.file_size > settings.max_upload_bytes:
                    raise ValueError("Encrypted or oversized ZIP entry")
                if entry.file_size > max(entry.compress_size, 1) * 200:
                    raise ValueError("ZIP compression ratio limit exceeded")
                if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                    accepted.append(entry)
            if not 0 < len(accepted) <= MAX_PAGES:
                raise ValueError("ZIP must contain 1 to 300 safe images")
            for entry in sorted(accepted, key=lambda e: natural_key(e.filename)):
                with archive.open(entry) as stream:
                    content = stream.read(settings.max_upload_bytes + 1)
                    if len(content) > settings.max_upload_bytes:
                        raise ValueError("ZIP entry size limit exceeded")
                yield image_bytes(content)
    else:
        raise ValueError("Unsupported file kind")
