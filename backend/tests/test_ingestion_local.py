import base64
import io
import zipfile

import pytest
from PIL import Image
from sqlalchemy import text
from test_auth import CREDENTIALS

from app.config import get_settings
from app.jobs import LocalJobQueue
from app.storage import get_storage


def make_png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (32, 48), "blue").save(output, format="PNG")
    return output.getvalue()


def csrf(client):
    return {"x-csrf-token": client.cookies["recap_csrf"]}


def setup_chapter(client):
    assert client.post("/api/v1/auth/register", json=CREDENTIALS).status_code == 201
    project = client.post("/api/v1/projects", json={"name": "Project"}, headers=csrf(client)).json()
    chapter = client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"name": "Chapter"}, headers=csrf(client)
    ).json()
    return f"/api/v1/chapters/{chapter['id']}"


def sample(kind):
    output = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(output, "w") as archive:
            for number in [10, 2, 1]:
                image = io.BytesIO()
                Image.new("RGB", (number + 30, 60)).save(image, "PNG")
                archive.writestr(f"{number}.png", image.getvalue())
            archive.writestr("../evil.png", make_png())
            archive.writestr("/evil.png", make_png())
            archive.writestr(".hidden.png", make_png())
            archive.writestr("run.exe", b"malicious")
        return output.getvalue(), "application/zip", 3
    if kind == "pdf":
        Image.new("RGB", (80, 120)).save(
            output, "PDF", save_all=True, append_images=[Image.new("RGB", (90, 130))]
        )
        return output.getvalue(), "application/pdf", 2
    Image.new("RGB", (80, 120)).save(output, "JPEG")
    return output.getvalue(), "image/jpeg", 1


@pytest.mark.parametrize("kind", ["jpg", "jpeg", "pdf", "zip"])
def test_real_upload_reorder_delete_and_persistence(client, database, kind):
    route = setup_chapter(client)
    content, mime, count = sample(kind)
    response = client.post(
        route + "/upload",
        files={"file": (f"../../unsafe.{kind}", content, mime)},
        headers=csrf(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    queue = LocalJobQueue()
    job_id = response.json()["job_id"]
    assert queue.get_status(job_id) == "completed"
    queue.submit(job_id)  # duplicate dispatch is a no-op
    assert queue.cancel(job_id) is False
    pages = client.get(route + "/pages").json()
    assert len(pages) == count
    assert client.get(route + "/processing-status").json()["status"] == "ready"
    if kind == "zip":
        assert [page["width"] for page in pages] == [31, 32, 40]
    for page in pages:
        assert page["status"] == "ready" and page["file_size"] > 0
        thumb = client.get(page["thumbnail_url"]).json()["data_url"]
        with Image.open(io.BytesIO(base64.b64decode(thumb.split(",")[1]))) as image:
            assert image.width <= 320 and image.height <= 480
    ids = [page["id"] for page in reversed(pages)]
    assert (
        client.patch(
            route + "/pages/reorder", json={"page_ids": ids}, headers=csrf(client)
        ).status_code
        == 200
    )
    assert [p["id"] for p in client.get(route + "/pages").json()] == ids
    assert (
        client.patch(
            route + "/pages/reorder", json={"page_ids": ids + ids}, headers=csrf(client)
        ).status_code
        == 400
    )
    with database[0].connect() as db:
        key = db.scalar(
            text("select storage_key from pages where id=:id"), {"id": ids[0].replace("-", "")}
        )
    assert get_storage().exists(key)
    assert client.delete(f"/api/v1/pages/{ids[0]}", headers=csrf(client)).status_code == 200
    assert not get_storage().exists(key)
    assert len(client.get(route + "/pages").json()) == count - 1


@pytest.mark.parametrize(
    "name,mime,content",
    [
        ("bad.pdf", "application/pdf", b"%PDF-1.7 broken"),
        ("bad.zip", "application/zip", b"PK\x03\x04broken"),
        ("bad.png", "image/png", b"\x89PNG\r\n\x1a\nbroken"),
    ],
)
def test_corruption_fails_without_partial_pages(client, name, mime, content):
    route = setup_chapter(client)
    result = client.post(
        route + "/upload", files={"file": (name, content, mime)}, headers=csrf(client)
    )
    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    assert client.get(route + "/pages").json() == []
    assert client.get(route + "/processing-status").json()["status"] == "failed"


def test_size_mime_csrf_and_foreign_page_protection(client, monkeypatch):
    route = setup_chapter(client)
    assert (
        client.post(
            route + "/upload", files={"file": ("a.png", make_png(), "image/png")}
        ).status_code
        == 403
    )
    assert (
        client.post(
            route + "/upload",
            files={"file": ("a.exe", make_png(), "image/png")},
            headers=csrf(client),
        ).status_code
        == 415
    )
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 100)
    assert (
        client.post(
            route + "/upload",
            files={"file": ("a.png", b"x" * 101, "image/png")},
            headers=csrf(client),
        ).status_code
        == 413
    )
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 1000000)
    assert (
        client.post(
            route + "/upload",
            files={"file": ("a.png", make_png(), "image/png")},
            headers=csrf(client),
        ).status_code
        == 200
    )
    page = client.get(route + "/pages").json()[0]
    assert "storage_key" not in page
    client.post("/api/v1/auth/logout", headers=csrf(client))
    client.post("/api/v1/auth/register", json={**CREDENTIALS, "email": "other@example.com"})
    assert client.get(route + "/pages").status_code == 404
    assert client.get(route + "/processing-status").status_code == 404
    assert client.get(f"/api/v1/pages/{page['id']}").status_code == 404
    assert client.get(page["thumbnail_url"]).status_code == 404
    assert client.delete(f"/api/v1/pages/{page['id']}", headers=csrf(client)).status_code == 404
    assert (
        client.patch(
            route + "/pages/reorder", json={"page_ids": [page["id"]]}, headers=csrf(client)
        ).status_code
        == 404
    )
    assert (
        client.post(
            route + "/upload",
            files={"file": ("a.png", make_png(), "image/png")},
            headers=csrf(client),
        ).status_code
        == 404
    )


@pytest.mark.parametrize("key", ["../escape", "/absolute", "C:/windows", "a/../b", "a\\b"])
def test_local_storage_refuses_unsafe_keys(database, key):
    with pytest.raises(ValueError):
        get_storage().put(key, b"no", "text/plain")


def test_local_png_upload_creates_page_and_thumbnail(client):
    assert client.post("/api/v1/auth/register", json=CREDENTIALS).status_code == 201
    project = client.post("/api/v1/projects", json={"name": "Local"}, headers=csrf(client)).json()
    chapter = client.post(
        f"/api/v1/projects/{project['id']}/chapters", json={"name": "Chapter"}, headers=csrf(client)
    ).json()
    response = client.post(
        f"/api/v1/chapters/{chapter['id']}/upload",
        files={"file": ("page.png", make_png(), "image/png")},
        headers=csrf(client),
    )
    assert response.status_code == 200, response.text
    pages = client.get(f"/api/v1/chapters/{chapter['id']}/pages").json()
    assert len(pages) == 1
    assert pages[0]["page_number"] == 1
    thumbnail = client.get(f"/api/v1/pages/{pages[0]['id']}/thumbnail")
    assert thumbnail.status_code == 200
    assert thumbnail.json()["data_url"].startswith("data:image/jpeg;base64,")
