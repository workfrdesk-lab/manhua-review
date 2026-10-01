import io
import zipfile

import pytest
from PIL import Image

from app.extraction import detect, extract, natural_key


def image(ext: str, color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (40, 60), color).save(output, format=ext)
    return output.getvalue()


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as result:
        for name, content in entries:
            result.writestr(name, content)
    return output.getvalue()


def test_image_validation_and_thumbnail():
    content = image("PNG")
    assert detect(content, "cover.png", "image/png") == "png"
    pages = list(extract(content, "png"))
    assert pages[0][2:] == (40, 60)
    with Image.open(io.BytesIO(pages[0][0])) as normalized:
        assert normalized.format == "JPEG"
    with Image.open(io.BytesIO(pages[0][1])) as thumbnail:
        assert thumbnail.width <= 320 and thumbnail.height <= 480


def test_natural_page_order_and_zip_safety(database):
    content = archive(
        [("10.jpg", image("JPEG")), ("2.jpg", image("JPEG")), ("1.jpg", image("JPEG"))]
    )
    assert natural_key("10.jpg") > natural_key("2.jpg")
    assert len(list(extract(content, "zip"))) == 3
    unsafe = archive([("../escape.jpg", image("JPEG"))])
    with pytest.raises(ValueError):
        list(extract(unsafe, "zip"))


def test_corrupt_and_mismatched_files_are_rejected():
    with pytest.raises(ValueError):
        detect(b"not a pdf", "broken.pdf", "application/pdf")
    with pytest.raises(ValueError):
        list(extract(b"bad image", "png"))
    with pytest.raises(ValueError):
        detect(image("PNG"), "wrong.jpg", "image/jpeg")
