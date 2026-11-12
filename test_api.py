"""Comprehensive API, security, database, and UI regression tests."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

from fastapi.testclient import TestClient
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main
import models

VALID_PAYLOAD = {
    "type": "lost",
    "title": "Blue Thapar Smart Card",
    "category": "Smart Card / ID",
    "location": "Central Library",
    "date_reported": "2026-10-15",
    "contact_phone": "98765-43210",
    "description": "Dropped near the second-floor reading room desks.",
}

# Minimal 1x1 transparent PNG bytes.
SAMPLE_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc`\x00\x02\x00"
    b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Point DB_PATH and UPLOAD_DIR to isolated per-test temporary paths."""
    db_path = tmp_path / "test_items.db"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    return db_path, upload_dir


@pytest.fixture()
def client(isolated_env: tuple[Path, Path]) -> TestClient:
    """Create a FastAPI TestClient backed by an isolated temporary database."""
    with TestClient(main.app) as test_client:
        yield test_client


def _create_item(
    client: TestClient,
    overrides: dict[str, str] | None = None,
    files: dict | None = None,
) -> dict:
    """Submit a POST /api/items form request and assert 201 creation."""
    payload = {**VALID_PAYLOAD, **(overrides or {})}
    response = client.post("/api/items", data=payload, files=files)
    assert response.status_code == 201, response.text
    return response.json()


def test_root_returns_html_and_security_headers(client: TestClient) -> None:
    """GET / serves the HTML app and attaches HTTP security headers."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<html" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_database_pragmas_indexes_and_empty_seed(
    client: TestClient,
    isolated_env: tuple[Path, Path],
) -> None:
    """Isolated DB starts empty with WAL pragmas and composite feed indexes."""
    db_path, _ = isolated_env

    # Startup leaves database completely empty (no dummy seed items).
    feed = client.get("/api/items")
    assert feed.status_code == 200
    assert feed.json() == []

    with models.get_connection(db_path) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous;").fetchone()[0]
        temp_store = conn.execute("PRAGMA temp_store;").fetchone()[0]
        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list('items');").fetchall()
        }

    assert str(journal_mode).lower() == "wal"
    assert synchronous == 1  # NORMAL
    assert temp_store == 2  # MEMORY
    assert "idx_items_filter" in indexes
    assert "idx_items_status_feed" in indexes


def test_happy_path_create_fetch_and_filter_items(
    client: TestClient,
    isolated_env: tuple[Path, Path],
) -> None:
    """POST creates items, normalizes phones, excludes tokens on GET, and filters."""
    _, upload_dir = isolated_env

    first = _create_item(
        client,
        overrides={
            "type": "lost",
            "title": "Casio Scientific Calculator",
            "category": "Electronics / Audio",
            "location": "LT / LP Complex",
            "contact_phone": "(987) 654-3210",
            "description": "Left in LP-101 after 50% quiz",
        },
    )
    second = _create_item(
        client,
        overrides={
            "type": "found",
            "title": "Hostel B Room Keys",
            "category": "Keys",
            "location": "Hostel Area",
            "contact_phone": "8123456789",
            "description": "Silver keyring near mess entrance",
        },
        files={"image": ("keys.png", SAMPLE_PNG_BYTES, "image/png")},
    )

    assert first["status"] == "success"
    assert isinstance(first["item_id"], int)
    assert len(first["manage_token"]) == 12
    assert second["item_id"] > first["item_id"]

    # Default GET returns open items newest-first without exposing manage_token.
    all_open = client.get("/api/items")
    assert all_open.status_code == 200
    items = all_open.json()
    assert [item["id"] for item in items] == [second["item_id"], first["item_id"]]
    for item in items:
        assert "manage_token" not in item
    assert items[1]["contact_phone"] == "9876543210"
    assert items[0]["image_filename"] is not None
    assert (upload_dir / items[0]["image_filename"]).is_file()

    # Filter by type, category, location, and case-insensitive / wildcard-safe search.
    by_type = client.get("/api/items", params={"type": "found"}).json()
    assert [i["id"] for i in by_type] == [second["item_id"]]

    by_category = client.get(
        "/api/items", params={"category": "Electronics / Audio"}
    ).json()
    assert [i["id"] for i in by_category] == [first["item_id"]]

    by_location = client.get("/api/items", params={"location": "LT / LP Complex"}).json()
    assert [i["id"] for i in by_location] == [first["item_id"]]

    by_search = client.get("/api/items", params={"search": "calculator"}).json()
    assert [i["id"] for i in by_search] == [first["item_id"]]

    # SQL LIKE wildcard '%' must match literal '%' only, not every row.
    by_literal_percent = client.get("/api/items", params={"search": "50%"}).json()
    assert [i["id"] for i in by_literal_percent] == [first["item_id"]]


@pytest.mark.parametrize(
    "bad_overrides",
    [
        {"type": "stolen"},
        {"title": "ab"},
        {"title": "   "},
        {"category": "Nonexistent Category"},
        {"location": "Unknown Building"},
        {"date_reported": "15-10-2026"},
        {"date_reported": "2026-02-30"},
        {"contact_phone": "12345"},
        {"contact_phone": "5123456789"},
        {"contact_phone": "98765432101"},
    ],
)
def test_post_validation_errors_return_422_envelope(
    client: TestClient,
    bad_overrides: dict[str, str],
) -> None:
    """Malformed inputs fail with 422 and the standardized JSON error envelope."""
    response = client.post("/api/items", data={**VALID_PAYLOAD, **bad_overrides})
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert isinstance(body["detail"], str) and body["detail"]
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert isinstance(body["error"]["errors"], list)


def test_get_items_invalid_query_params_return_422(client: TestClient) -> None:
    """Invalid type or status query parameters return 422."""
    bad_type = client.get("/api/items", params={"type": "invalid"})
    assert bad_type.status_code == 422
    assert bad_type.json()["status"] == "error"

    bad_status = client.get("/api/items", params={"status": "archived"})
    assert bad_status.status_code == 422
    assert bad_status.json()["status"] == "error"


def test_upload_hardening_rejects_bad_ext_mime_empty_and_oversized(
    client: TestClient,
    isolated_env: tuple[Path, Path],
) -> None:
    """Uploads enforce extension whitelist, MIME match, non-empty, and 5 MB max."""
    _, upload_dir = isolated_env

    bad_ext = client.post(
        "/api/items",
        data=VALID_PAYLOAD,
        files={"image": ("script.svg", b"<svg></svg>", "image/svg+xml")},
    )
    assert bad_ext.status_code == 400
    assert bad_ext.json()["status"] == "error"

    bad_mime = client.post(
        "/api/items",
        data=VALID_PAYLOAD,
        files={"image": ("fake.png", b"not-an-image", "text/plain")},
    )
    assert bad_mime.status_code == 400
    assert bad_mime.json()["status"] == "error"

    empty_file = client.post(
        "/api/items",
        data=VALID_PAYLOAD,
        files={"image": ("empty.png", b"", "image/png")},
    )
    assert empty_file.status_code == 400
    assert empty_file.json()["status"] == "error"

    oversized_bytes = b"a" * (main.MAX_UPLOAD_BYTES + 1)
    too_large = client.post(
        "/api/items",
        data=VALID_PAYLOAD,
        files={"image": ("huge.jpg", oversized_bytes, "image/jpeg")},
    )
    assert too_large.status_code == 413
    assert too_large.json()["status"] == "error"
    assert too_large.json()["error"]["code"] == "HTTP_ERROR"

    # No partial upload artifacts remain after rejected requests.
    assert list(upload_dir.iterdir()) == []


def test_token_ownership_and_resolve_delete_lifecycle(
    client: TestClient,
    isolated_env: tuple[Path, Path],
) -> None:
    """Resolve and delete enforce 404/403 token ownership and remove media files."""
    _, upload_dir = isolated_env

    created = _create_item(
        client,
        files={"image": ("card.png", SAMPLE_PNG_BYTES, "image/png")},
    )
    item_id = created["item_id"]
    token = created["manage_token"]

    open_feed = client.get("/api/items").json()
    assert len(open_feed) == 1
    saved_filename = open_feed[0]["image_filename"]
    assert saved_filename and (upload_dir / saved_filename).is_file()

    # Nonexistent ID -> 404
    assert (
        client.patch(
            "/api/items/99999/resolve",
            headers={"X-Manage-Token": token},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            "/api/items/99999",
            headers={"X-Manage-Token": token},
        ).status_code
        == 404
    )

    # Missing or wrong token -> 403
    for bad_token_kwargs in [{}, {"headers": {"X-Manage-Token": "wrongtoken00"}}]:
        res_patch = client.patch(f"/api/items/{item_id}/resolve", **bad_token_kwargs)
        assert res_patch.status_code == 403
        assert res_patch.json()["detail"] == "Forbidden: Invalid manage token"

        res_del = client.delete(f"/api/items/{item_id}", **bad_token_kwargs)
        assert res_del.status_code == 403
        assert res_del.json()["detail"] == "Forbidden: Invalid manage token"

    # Valid token marks open -> resolved
    resolved = client.patch(
        f"/api/items/{item_id}/resolve",
        headers={"X-Manage-Token": token},
    )
    assert resolved.status_code == 200
    assert resolved.json() == {
        "status": "success",
        "message": "Item marked as resolved",
    }

    # Item moves from open feed to resolved/all feeds
    assert client.get("/api/items", params={"status": "open"}).json() == []
    resolved_items = client.get("/api/items", params={"status": "resolved"}).json()
    assert len(resolved_items) == 1
    assert resolved_items[0]["id"] == item_id
    assert resolved_items[0]["status"] == "resolved"

    all_items = client.get("/api/items", params={"status": "all"}).json()
    assert len(all_items) == 1

    # Re-resolving remains idempotent with valid query token support
    re_resolved = client.patch(
        f"/api/items/{item_id}/resolve",
        params={"token": token},
    )
    assert re_resolved.status_code == 200

    # Valid token deletes DB row and cleans uploaded image file
    deleted = client.delete(
        f"/api/items/{item_id}",
        headers={"X-Manage-Token": token},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {
        "status": "success",
        "message": "Item deleted successfully",
    }
    assert client.get("/api/items", params={"status": "all"}).json() == []
    assert not (upload_dir / saved_filename).exists()


def test_xss_payload_is_stored_literally_and_frontend_escapes_dom(
    client: TestClient,
) -> None:
    """XSS strings are preserved safely as plain text and frontend avoids raw innerHTML."""
    xss_title = '<img src=x onerror="alert(1)"> Lost Bottle'
    xss_desc = '<script>window.__xss=1</script>Near COS'
    created = _create_item(
        client,
        overrides={"title": xss_title, "description": xss_desc},
    )

    items = client.get("/api/items").json()
    assert len(items) == 1
    assert items[0]["id"] == created["item_id"]
    assert items[0]["title"] == xss_title
    assert items[0]["description"] == xss_desc

    # Ensure UI includes escapeHTML, safe DOM construction, lazy images, and a11y hooks.
    html = (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert '<html lang="en">' in html
    assert "function escapeHTML(" in html
    assert "textContent =" in html
    assert "loading = 'lazy'" in html
    assert "DEBOUNCE_DELAY_MS = 250" in html
    assert 'aria-live="polite"' in html
    assert 'for="searchInput"' in html
    assert 'for="fileInput"' in html
    assert ":focus-visible" in html
    assert "e.key === 'Escape'" in html


def test_sql_injection_search_probe_does_not_compromise_db(client: TestClient) -> None:
    """SQL injection payload in search behaves safely as a literal parameter."""
    _create_item(client)
    probe = client.get(
        "/api/items",
        params={"search": "' OR 1=1; DROP TABLE items; --"},
    )
    assert probe.status_code == 200
    assert probe.json() == []

    # Table and row remain intact after injection attempt.
    still_present = client.get("/api/items")
    assert still_present.status_code == 200
    assert len(still_present.json()) == 1
