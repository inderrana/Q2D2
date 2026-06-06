"""Document upload safety: size cap + SSRF + IDOR on avatar."""
import io


def test_upload_cap(auth_client, monkeypatch):
    # Force a tiny cap so we can exercise the 413 branch.
    from app import config as cfg
    monkeypatch.setattr(cfg, "MAX_UPLOAD_BYTES", 1024)
    # The router imported the constant by value; patch it there too.
    from app.routers import documents as docs_mod
    monkeypatch.setattr(docs_mod, "MAX_UPLOAD_BYTES", 1024)

    big = io.BytesIO(b"a" * 4096)
    r = auth_client.post(
        "/api/documents/upload",
        files={"file": ("big.txt", big, "text/plain")},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 413


def test_url_ssrf_blocked(auth_client):
    for url in ("http://127.0.0.1/x", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.5/", "file:///etc/passwd"):
        r = auth_client.post(
            "/api/documents/url",
            json={"url": url},
            headers={"X-Requested-With": "fetch"},
        )
        assert r.status_code in (400, 403, 422), (url, r.status_code, r.text)


def test_avatar_idor(auth_client):
    # Reading another arbitrary user id must 404, not leak anyone's avatar.
    r = auth_client.get("/api/settings/avatar/some-other-user-id")
    assert r.status_code == 404
