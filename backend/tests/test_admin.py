"""Admin / multi-user flows: cascade delete, last-admin protection."""


def _register(client, username, password="a-very-long-passphrase-1", **extra):
    return client.post(
        "/api/auth/register",
        json={"username": username, "password": password, **extra},
        headers={"X-Requested-With": "fetch"},
    )


def test_admin_can_list_users(auth_client):
    r = auth_client.get("/api/admin/users")
    assert r.status_code == 200
    users = r.json()["users"]
    assert any(u["username"] == "alice" for u in users)


def test_cannot_demote_last_admin(auth_client):
    me = auth_client.get("/api/auth/me").json()
    r = auth_client.patch(
        f"/api/admin/users/{me['id']}",
        json={"is_admin": False},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 400


def test_cannot_delete_self(auth_client):
    me = auth_client.get("/api/auth/me").json()
    r = auth_client.delete(
        f"/api/admin/users/{me['id']}",
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 400


def test_admin_can_delete_other_user(auth_client):
    # Register a second user (signup is allowed in the test fixture).
    r = _register(auth_client, "bob")
    assert r.status_code == 200, r.text
    bob_id = r.json()["id"]

    # `auth_client` is logged in as bob now (auth cookie was rewritten on
    # register). Log back in as alice.
    auth_client.post(
        "/api/auth/logout",
        headers={"X-Requested-With": "fetch"},
    )
    r = auth_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct horse battery staple"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200, r.text

    r = auth_client.delete(
        f"/api/admin/users/{bob_id}",
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200

    # Audit log should show the delete.
    log = auth_client.get("/api/admin/audit").json()["events"]
    assert any(e["action"] == "admin.delete_user" and e["target_id"] == bob_id
               for e in log)
