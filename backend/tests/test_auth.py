from alembic import command
from sqlalchemy import inspect, text

CREDENTIALS = {"email": "User@example.com", "password": "correct horse battery staple"}


def register(client):
    return client.post("/api/v1/auth/register", json=CREDENTIALS)


def test_session_lifecycle(client, database):
    assert client.get("/api/v1/auth/me").status_code == 401
    response = register(client)
    assert response.status_code == 201
    assert response.json()["email"] == "user@example.com"
    assert "password_hash" not in response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/api/v1/auth/me").json()["id"] == response.json()["id"]
    raw_token = client.cookies["recap_session"]
    with database[0].connect() as connection:
        assert connection.scalar(text("select token_hash from sessions")) != raw_token
        assert connection.scalar(text("select password_hash from users")).startswith("$argon2id$")
    assert client.post("/api/v1/auth/logout").status_code == 403
    assert (
        client.post(
            "/api/v1/auth/logout", headers={"x-csrf-token": client.cookies["recap_csrf"]}
        ).status_code
        == 204
    )
    client.cookies.set("recap_session", raw_token)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_and_duplicates(client):
    assert register(client).status_code == 201
    assert register(client).status_code == 409
    client.cookies.clear()
    assert (
        client.post("/api/v1/auth/login", json={**CREDENTIALS, "password": "wrong"}).status_code
        == 401
    )
    assert client.post("/api/v1/auth/login", json=CREDENTIALS).status_code == 200


def test_origin_and_session_bound_csrf(client):
    assert (
        client.post(
            "/api/v1/auth/register", json=CREDENTIALS, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    register(client)
    del client.cookies["recap_csrf"]
    client.cookies.set("recap_csrf", "forged")
    assert client.post("/api/v1/auth/logout", headers={"x-csrf-token": "forged"}).status_code == 403


def test_expired_and_disabled_sessions(client, database):
    register(client)
    with database[0].begin() as connection:
        connection.execute(text("update users set is_active = false"))
    assert client.get("/api/v1/auth/me").status_code == 401
    with database[0].begin() as connection:
        connection.execute(text("update users set is_active = true"))
        connection.execute(text("update sessions set expires_at = '2000-01-01'"))
    assert client.get("/api/v1/auth/me").status_code == 401


def test_migration_roundtrip(database):
    engine, config = database
    assert {"users", "sessions", "oauth_identities"} <= set(inspect(engine).get_table_names())
    command.check(config)
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    command.upgrade(config, "head")
