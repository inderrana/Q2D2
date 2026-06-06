"""Auth flows: first-admin promotion, signup gate, password length, logout."""


def test_first_user_becomes_admin(app_client):
    r = app_client.post(
        "/api/auth/register",
        json={"username": "root", "password": "a-very-long-passphrase-1"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200
    assert r.json()["is_admin"] is True


def test_password_min_length(app_client):
    r = app_client.post(
        "/api/auth/register",
        json={"username": "shorty", "password": "tooshort"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code in (400, 422)


def test_login_logout(auth_client):
    r = auth_client.post(
        "/api/auth/logout",
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200
    # /me now 401
    r = auth_client.get("/api/auth/me")
    assert r.status_code == 401


def test_csrf_blocks_state_change_without_header(auth_client):
    # POST /api/chats without X-Requested-With → 403
    r = auth_client.post("/api/chats", json={"title": "x"})
    assert r.status_code == 403
