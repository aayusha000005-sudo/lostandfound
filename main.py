"""FastAPI application and HTTP routing for Campus Lost & Found."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import hmac
import logging
import os
from pathlib import Path
import secrets
import sqlite3
from typing import Annotated, Optional
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status as http_status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

import models
from models import get_db, init_db
from schemas import (
    ActionResponse,
    ErrorDetail,
    ErrorResponse,
    ItemCreate,
    ItemCreateResponse,
    ItemFilterQuery,
    ItemResponse,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = STATIC_DIR / "uploads"

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_IMAGE_MIME_BY_EXT: dict[str, set[str]] = {
    ".jpg": {"image/jpeg", "image/jpg"},
    ".jpeg": {"image/jpeg", "image/jpg"},
    ".png": {"image/png"},
    ".webp": {"image/webp"},
}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
UPLOAD_CHUNK_SIZE = 64 * 1024  # 64 KiB


def _configure_logging() -> logging.Logger:
    """Configure structured logging once for the application process."""
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
    return logging.getLogger("campus_lost_found")


logger = _configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Initialize upload storage and SQLite schema on startup."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    init_db()
    logger.info("Campus Lost & Found startup complete (uploads=%s)", UPLOAD_DIR)
    yield
    logger.info("Campus Lost & Found shutdown complete")


app = FastAPI(title="Campus Lost & Found API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Attach defensive HTTP security headers to all outgoing responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def _build_error_response(
    status_code: int,
    code: str,
    message: str,
    errors: Optional[list[dict[str, object]]] = None,
) -> JSONResponse:
    """Build a consistent JSON error envelope while preserving top-level 'detail'."""
    payload = ErrorResponse(
        detail=message,
        error=ErrorDetail(code=code, message=message, errors=errors),
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Normalize HTTP exceptions into the centralized JSON error envelope."""
    detail = str(exc.detail) if exc.detail else "Request failed"
    logger.warning(
        "HTTP %s on %s %s: %s",
        exc.status_code,
        request.method,
        request.url.path,
        detail,
    )
    return _build_error_response(exc.status_code, "HTTP_ERROR", detail)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Format FastAPI request validation errors into the JSON error envelope."""
    sanitized_errors = [
        {k: v for k, v in err.items() if k != "ctx"} for err in exc.errors()
    ]
    first_msg = (
        sanitized_errors[0].get("msg", "Validation error")
        if sanitized_errors
        else "Validation error"
    )
    logger.warning(
        "Request validation error on %s %s: %s",
        request.method,
        request.url.path,
        first_msg,
    )
    return _build_error_response(
        http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        "VALIDATION_ERROR",
        first_msg,
        errors=sanitized_errors,
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    """Format Pydantic model validation errors into the JSON error envelope."""
    sanitized_errors = exc.errors(include_url=False, include_context=False)
    first_msg = (
        sanitized_errors[0].get("msg", "Validation error")
        if sanitized_errors
        else "Validation error"
    )
    logger.warning(
        "Payload validation error on %s %s: %s",
        request.method,
        request.url.path,
        first_msg,
    )
    return _build_error_response(
        http_status.HTTP_422_UNPROCESSABLE_CONTENT,
        "VALIDATION_ERROR",
        first_msg,
        errors=sanitized_errors,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch unexpected errors without leaking stack traces to clients."""
    logger.exception(
        "Unhandled server error on %s %s", request.method, request.url.path
    )
    return _build_error_response(
        http_status.HTTP_500_INTERNAL_SERVER_ERROR,
        "INTERNAL_ERROR",
        "Internal server error",
    )


os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    """Serve the Campus Lost & Found single-page web UI."""
    return FileResponse(STATIC_DIR / "index.html")


async def _save_upload_image(image: UploadFile) -> str:
    """Validate extension, MIME type, and 5 MB max size before saving upload."""
    ext = Path(image.filename or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        await image.close()
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Invalid image extension. Allowed: .jpg, .jpeg, .png, .webp.",
        )

    raw_content_type = (image.content_type or "").split(";", 1)[0].strip().lower()
    allowed_mimes = ALLOWED_IMAGE_MIME_BY_EXT[ext]
    if raw_content_type not in allowed_mimes:
        await image.close()
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid image MIME type '{raw_content_type or 'unknown'}' "
                f"for {ext} upload."
            ),
        )

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    filename = f"{uuid4().hex}{ext}"
    destination = UPLOAD_DIR / filename
    total_bytes = 0

    try:
        with open(destination, "wb") as out_file:
            while chunk := await image.read(UPLOAD_CHUNK_SIZE):
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="Image size exceeds the 5 MB limit.",
                    )
                out_file.write(chunk)

        if total_bytes == 0:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="Uploaded image file cannot be empty.",
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await image.close()

    return filename


def _get_authorized_item(
    db: sqlite3.Connection,
    item_id: int,
    x_manage_token: Optional[str],
    token: Optional[str],
) -> sqlite3.Row:
    """Fetch an item by ID and verify the caller's manage token in constant time."""
    item = models.get_item_for_auth(db, item_id)
    if item is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Item not found",
        )

    provided_token = (x_manage_token or "").strip() or (token or "").strip()
    if not provided_token or not hmac.compare_digest(
        provided_token, item["manage_token"]
    ):
        logger.warning("Unauthorized management attempt for item_id=%s", item_id)
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid manage token",
        )
    return item


@app.post(
    "/api/items",
    status_code=http_status.HTTP_201_CREATED,
    response_model=ItemCreateResponse,
)
async def create_item_endpoint(
    type: Annotated[str, Form(...)],
    title: Annotated[str, Form(...)],
    category: Annotated[str, Form(...)],
    location: Annotated[str, Form(...)],
    date_reported: Annotated[str, Form(...)],
    contact_phone: Annotated[str, Form(...)],
    description: Annotated[Optional[str], Form()] = None,
    image: Optional[UploadFile] = File(default=None),
    db: sqlite3.Connection = Depends(get_db),
) -> ItemCreateResponse:
    """Validate and store a new lost or found campus item report."""
    payload = ItemCreate(
        type=type,
        title=title,
        category=category,
        location=location,
        date_reported=date_reported,
        contact_phone=contact_phone,
        description=description,
    )

    image_filename: Optional[str] = None
    if image is not None:
        if image.filename:
            image_filename = await _save_upload_image(image)
        else:
            await image.close()

    manage_token = secrets.token_hex(6)

    try:
        new_id = models.create_item(
            db,
            item_type=payload.type,
            title=payload.title,
            category=payload.category,
            location=payload.location,
            date_reported=payload.date_reported,
            description=payload.description,
            image_filename=image_filename,
            contact_phone=payload.contact_phone,
            manage_token=manage_token,
        )
    except Exception:
        if image_filename is not None:
            (UPLOAD_DIR / image_filename).unlink(missing_ok=True)
        raise

    logger.info(
        "Created item_id=%s type=%s category=%s location=%s",
        new_id,
        payload.type,
        payload.category,
        payload.location,
    )
    return ItemCreateResponse(item_id=new_id, manage_token=manage_token)


@app.get("/api/items", response_model=list[ItemResponse])
def list_items_endpoint(
    type: Annotated[Optional[str], Query()] = None,
    category: Annotated[Optional[str], Query()] = None,
    location: Annotated[Optional[str], Query()] = None,
    status: Annotated[str, Query()] = "open",
    search: Annotated[Optional[str], Query()] = None,
    db: sqlite3.Connection = Depends(get_db),
) -> list[ItemResponse]:
    """Return filtered lost/found items ordered newest-first without manage_token."""
    filters = ItemFilterQuery(
        type=type,
        category=category,
        location=location,
        status=status,
        search=search,
    )
    rows = models.list_items(
        db,
        item_type=filters.type,
        category=filters.category,
        location=filters.location,
        status=filters.status,
        search=filters.search,
    )
    return [ItemResponse.model_validate(row) for row in rows]


@app.patch("/api/items/{item_id}/resolve", response_model=ActionResponse)
def resolve_item_endpoint(
    item_id: int,
    x_manage_token: Annotated[Optional[str], Header(alias="X-Manage-Token")] = None,
    token: Annotated[Optional[str], Query()] = None,
    db: sqlite3.Connection = Depends(get_db),
) -> ActionResponse:
    """Mark an item as resolved after authenticating with manage_token."""
    _get_authorized_item(db, item_id, x_manage_token, token)
    models.mark_item_resolved(db, item_id)
    logger.info("Marked item_id=%s as resolved", item_id)
    return ActionResponse(message="Item marked as resolved")


@app.delete("/api/items/{item_id}", response_model=ActionResponse)
def delete_item_endpoint(
    item_id: int,
    x_manage_token: Annotated[Optional[str], Header(alias="X-Manage-Token")] = None,
    token: Annotated[Optional[str], Query()] = None,
    db: sqlite3.Connection = Depends(get_db),
) -> ActionResponse:
    """Delete an item and any associated uploaded image after verifying manage_token."""
    item = _get_authorized_item(db, item_id, x_manage_token, token)

    image_filename = item["image_filename"]
    if image_filename:
        image_path = UPLOAD_DIR / Path(image_filename).name
        if image_path.is_file():
            try:
                os.remove(image_path)
            except OSError as exc:
                logger.warning("Failed to remove upload %s: %s", image_path, exc)

    models.delete_item_by_id(db, item_id)
    logger.info("Deleted item_id=%s", item_id)
    return ActionResponse(message="Item deleted successfully")
