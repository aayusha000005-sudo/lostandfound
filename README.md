# Campus Lost & Found — Thapar Institute of Engineering & Technology (TIET)

A fast, zero-friction **Lost & Found portal** built for the Thapar University campus community. Instead of lost smart cards, hostel keys, calculators, drafters, or cycles getting buried inside dozens of WhatsApp groups and hostel noticeboards, students get one searchable, location-aware campus feed with one-tap WhatsApp coordination.

---

## Problem Statement & Design Rationale

### Why Traditional Campus Channels Fail
- **High message churn:** Critical posts about lost TIET Smart Cards, room keys, or AirPods vanish within minutes in multi-hundred-member WhatsApp groups.
- **Zero structured search:** Free-form chat logs cannot be filtered by campus hotspot (`Central Library`, `LT / LP Complex`, `Hostel Area`, `COS / Sports`, `Other`) or item category (`Smart Card / ID`, `Keys`, `Electronics / Audio`, `Stationery / Drafter`, `Cycle / Other`).
- **Friction kills reporting:** Requiring account registration, email OTPs, or SSO passwords stops a passerby who just picked up a dropped key near LT from posting a quick 15-second report.

### Architectural Choice: Zero-Friction Device Tokens (`manage_token`)
- **Immediate posting:** Any student can file a **Lost** or **Found** report right away without creating an account.
- **Cryptographic ownership without login walls:** On creation (`POST /api/items`), the backend generates a random 12-hex-character `manage_token` (`secrets.token_hex(6)`), stores it in SQLite, excludes it from all public `GET /api/items` feeds, and returns it once to the reporting browser.
- **Client-side ownership map:** `static/index.html` saves `{ [item_id]: manage_token }` in `localStorage` under `campus_lostfound_tokens`. Only the reporting device renders **Mark Resolved** and **Delete** actions, while every other student sees the direct **WhatsApp** contact button (`https://wa.me/91<contact_phone>?text=...`).
- **Constant-time verification:** `PATCH /api/items/{item_id}/resolve` and `DELETE /api/items/{item_id}` verify `X-Manage-Token` (or `?token=`) with `hmac.compare_digest()` to prevent timing side-channel leaks.

---

## Key Engineering Features

- **Clean Modular Architecture:**
  - [`models.py`](models.py) — SQLite connection factory (`PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `temp_store=MEMORY`), composite indexes (`idx_items_filter`, `idx_items_status_feed`), and parameterized `?` repository queries.
  - [`schemas.py`](schemas.py) — Pydantic v2 request validators (`ItemCreate`, `ItemFilterQuery`) and response/error envelopes (`ItemResponse`, `ItemCreateResponse`, `ActionResponse`, `ErrorResponse`).
  - [`main.py`](main.py) — FastAPI HTTP routing, structured Python `logging`, centralized exception handlers, security response headers middleware, and hardened multipart uploads.
  - [`static/index.html`](static/index.html) — Responsive Tailwind CSS interface with XSS-safe DOM rendering, debounced filters (`250ms`), lazy-loaded images, and WCAG 2.1 AA accessibility.
  - [`tests/test_api.py`](tests/test_api.py) — Isolated `pytest` + FastAPI `TestClient` suite using `tmp_path` so production `items.db` and `static/uploads/` are never modified.
- **Security Hardening:**
  - `escapeHTML()` sanitizer + safe DOM node construction (`textContent`) eliminating raw user-string `innerHTML` interpolation.
  - Response security headers on every route: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`.
  - Image upload validation enforcing allowed extensions (`.jpg`, `.jpeg`, `.png`, `.webp`), matching image MIME types, non-empty payloads, UUID filenames, and a **5 MB (`5 * 1024 * 1024` bytes)** ceiling (`413` on oversize).
- **Accessibility (WCAG 2.1 AA):**
  - Semantic landmarks (`<header>`, `<main>`, `<section>`, `<nav>`), explicit `<label for="...">` pairs (with `sr-only` for search/upload controls), descriptive `aria-label`s on icon-only controls, `aria-live="polite"` status/feed announcements, visible `:focus-visible` outlines, and modal `Escape` handling with focus trapping & focus restoration.

---

## Project Structure

```text
lost-and-found/
├── main.py              # FastAPI app, middleware, exception handlers, API routes
├── models.py            # SQLite engine, WAL pragmas, indexes, parameterized DB helpers
├── schemas.py           # Pydantic v2 validation & response models
├── requirements.txt     # Runtime & test dependencies
├── static/
│   ├── index.html       # Single-page frontend connected to FastAPI API
│   └── uploads/         # Uploaded item images (created/ensured on startup)
├── tests/
│   ├── __init__.py
│   └── test_api.py      # Automated API, security, DB, upload, and UI checks
└── README.md
```

---

## API Reference

### `GET /`
Serves the interactive frontend (`static/index.html`).

### `POST /api/items` (`201 Created`)
Create a new Lost or Found report (`multipart/form-data`).

| Field | Required | Rules |
| :--- | :--- | :--- |
| `type` | Yes | `"lost"` or `"found"` |
| `title` | Yes | `3` to `150` characters (trimmed) |
| `category` | Yes | `"Smart Card / ID"`, `"Keys"`, `"Electronics / Audio"`, `"Stationery / Drafter"`, `"Cycle / Other"` |
| `location` | Yes | `"Central Library"`, `"LT / LP Complex"`, `"Hostel Area"`, `"COS / Sports"`, `"Other"` |
| `date_reported` | Yes | Valid calendar date formatted `YYYY-MM-DD` |
| `contact_phone` | Yes | Non-digits stripped; must resolve to 10 digits starting with `6-9` |
| `description` | No | Up to `2000` characters |
| `image` | No | `.jpg`, `.jpeg`, `.png`, or `.webp` with matching MIME type, max `5 MB` |

**Response (`201`):**
```json
{
  "status": "success",
  "item_id": 1,
  "manage_token": "a1b2c3d4e5f6"
}
```

### `GET /api/items` (`200 OK`)
Fetch items ordered newest-first (`id DESC`). Public responses omit `manage_token`.

| Query Param | Default | Notes |
| :--- | :--- | :--- |
| `type` | `None` | Optional `"lost"` or `"found"` |
| `category` | `None` | Optional category filter |
| `location` | `None` | Optional campus location filter |
| `status` | `"open"` | `"open"`, `"resolved"`, or `"all"` |
| `search` | `None` | Case-insensitive literal substring match across `title` and `description` |

### `PATCH /api/items/{item_id}/resolve` (`200 OK`)
Marks an item as `status = 'resolved'`. Requires `X-Manage-Token` header or `token` query parameter.
- `404` if item does not exist
- `403` if token is missing or invalid (`"Forbidden: Invalid manage token"`)

### `DELETE /api/items/{item_id}` (`200 OK`)
Deletes an item row and removes its uploaded file from `static/uploads/` if present. Requires `X-Manage-Token` header or `token` query parameter (`404` / `403` on missing item or token mismatch).

### Standard Error Envelope
Validation (`422`), upload (`400` / `413`), auth (`403`), and missing-resource (`404`) errors return a consistent JSON structure:
```json
{
  "status": "error",
  "detail": "Human-readable error summary",
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Human-readable error summary",
    "errors": []
  }
}
```

---

## Local Development

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) Initialize SQLite schema standalone (creates empty items.db)
python models.py

# 4. Run the development server
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in your browser. Interactive OpenAPI docs are available at `http://localhost:8000/docs`.

---

## Running the Automated Test Suite

The test suite uses FastAPI's `TestClient` and `pytest`'s `tmp_path` fixture so every test runs against an isolated temporary SQLite database and upload directory:

```bash
pytest -v
```

---

## Render Deployment

Compatible with standard Render Web Service settings out of the box:

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
