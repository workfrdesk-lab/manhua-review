from uuid import UUID


def register(client, email):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    return response.json()


def write_headers(client):
    return {"x-csrf-token": client.cookies["recap_csrf"]}


def test_project_and_chapter_crud_with_ownership(client, database):
    user = register(client, "owner@example.com")
    project_response = client.post(
        "/api/v1/projects", json={"name": "First project"}, headers=write_headers(client)
    )
    assert project_response.status_code == 201
    project = project_response.json()
    assert UUID(project["user_id"]) == UUID(user["id"])

    assert client.get("/api/v1/projects").json()[0]["id"] == project["id"]
    updated = client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"name": "Renamed"},
        headers=write_headers(client),
    )
    assert updated.status_code == 200
    chapter_response = client.post(
        f"/api/v1/projects/{project['id']}/chapters",
        json={"name": "Chapter 1"},
        headers=write_headers(client),
    )
    assert chapter_response.status_code == 201
    chapter = chapter_response.json()
    assert client.get(f"/api/v1/projects/{project['id']}/chapters").json()[0]["id"] == chapter["id"]
    assert client.get(f"/api/v1/chapters/{chapter['id']}").status_code == 200
    assert (
        client.delete(
            f"/api/v1/chapters/{chapter['id']}", headers=write_headers(client)
        ).status_code
        == 200
    )
    assert (
        client.delete(
            f"/api/v1/projects/{project['id']}", headers=write_headers(client)
        ).status_code
        == 200
    )


def test_project_isolation(client, database):
    register(client, "first@example.com")
    first = client.post(
        "/api/v1/projects", json={"name": "Private"}, headers=write_headers(client)
    ).json()
    client.post("/api/v1/auth/logout", headers=write_headers(client))
    register(client, "second@example.com")
    assert client.get(f"/api/v1/projects/{first['id']}").status_code == 404
    assert (
        client.patch(
            f"/api/v1/projects/{first['id']}",
            json={"name": "Stolen"},
            headers=write_headers(client),
        ).status_code
        == 404
    )

    assert (
        client.delete(f"/api/v1/projects/{first['id']}", headers=write_headers(client)).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/projects/{first['id']}/chapters",
            json={"name": "Nope"},
            headers=write_headers(client),
        ).status_code
        == 404
    )


def test_chapter_isolation(client):
    register(client, "chapter-owner@example.com")
    project = client.post(
        "/api/v1/projects", json={"name": "Private"}, headers=write_headers(client)
    ).json()
    route = f"/api/v1/projects/{project['id']}/chapters"
    chapter = client.post(
        route, json={"name": "Private chapter"}, headers=write_headers(client)
    ).json()
    chapter_route = f"/api/v1/chapters/{chapter['id']}"
    assert (
        client.patch(
            chapter_route, json={"name": "Updated"}, headers=write_headers(client)
        ).status_code
        == 200
    )
    client.post("/api/v1/auth/logout", headers=write_headers(client))
    register(client, "outsider@example.com")
    assert client.get(route).status_code == 404
    assert client.get(chapter_route).status_code == 404
    assert (
        client.patch(
            chapter_route, json={"name": "Stolen"}, headers=write_headers(client)
        ).status_code
        == 404
    )
    assert client.delete(chapter_route, headers=write_headers(client)).status_code == 404
    assert client.get("/api/v1/projects").json() == []
