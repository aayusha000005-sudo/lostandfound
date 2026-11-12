"""Pydantic v2 request validation and API response schemas."""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models import VALID_CATEGORIES, VALID_LOCATIONS, VALID_STATUSES, VALID_TYPES


def _clean_optional_text(value: Any) -> Optional[str]:
    """Strip text values and normalize blank strings to None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Value must be a string.")
    cleaned = value.strip()
    return cleaned or None


class ItemCreate(BaseModel):
    """Validated payload for creating a lost or found campus item report."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["lost", "found"]
    title: str = Field(..., min_length=3, max_length=150)
    category: str
    location: str
    date_reported: str
    contact_phone: str
    description: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: Any) -> str:
        """Normalize and validate notice type against allowed values."""
        if not isinstance(value, str):
            raise ValueError("type must be a string.")
        cleaned = value.strip().lower()
        if cleaned not in VALID_TYPES:
            raise ValueError(f"type must be one of: {', '.join(VALID_TYPES)}.")
        return cleaned

    @field_validator("title", mode="before")
    @classmethod
    def clean_title(cls, value: Any) -> str:
        """Trim surrounding whitespace from title before length checks."""
        if not isinstance(value, str):
            raise ValueError("title must be a string.")
        return value.strip()

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, value: Any) -> str:
        """Ensure category matches one of the configured campus categories."""
        if not isinstance(value, str):
            raise ValueError("category must be a string.")
        cleaned = value.strip()
        if cleaned not in VALID_CATEGORIES:
            raise ValueError(
                f"Invalid category. Must be one of: {', '.join(VALID_CATEGORIES)}."
            )
        return cleaned

    @field_validator("location", mode="before")
    @classmethod
    def validate_location(cls, value: Any) -> str:
        """Ensure location matches one of the configured campus locations."""
        if not isinstance(value, str):
            raise ValueError("location must be a string.")
        cleaned = value.strip()
        if cleaned not in VALID_LOCATIONS:
            raise ValueError(
                f"Invalid location. Must be one of: {', '.join(VALID_LOCATIONS)}."
            )
        return cleaned

    @field_validator("date_reported", mode="before")
    @classmethod
    def validate_date_reported(cls, value: Any) -> str:
        """Verify date_reported is a valid calendar date in YYYY-MM-DD format."""
        if not isinstance(value, str):
            raise ValueError("date_reported must be a string.")
        cleaned = value.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            raise ValueError("date_reported must be in YYYY-MM-DD format.")
        try:
            date.fromisoformat(cleaned)
        except ValueError as exc:
            raise ValueError("date_reported is not a valid calendar date.") from exc
        return cleaned

    @field_validator("contact_phone", mode="before")
    @classmethod
    def validate_contact_phone(cls, value: Any) -> str:
        """Strip non-digits and enforce a 10-digit mobile number starting with 6-9."""
        if not isinstance(value, str):
            raise ValueError("contact_phone must be a string.")
        digits = re.sub(r"\D", "", value)
        if not re.fullmatch(r"[6-9]\d{9}", digits):
            raise ValueError("contact_phone must contain 10 digits starting with 6-9.")
        return digits

    @field_validator("description", mode="before")
    @classmethod
    def clean_description(cls, value: Any) -> Optional[str]:
        """Trim description text and convert empty strings to None."""
        return _clean_optional_text(value)


class ItemFilterQuery(BaseModel):
    """Validated query parameters for filtering the public item feed."""

    model_config = ConfigDict(extra="forbid")

    type: Optional[Literal["lost", "found"]] = None
    category: Optional[str] = Field(default=None, max_length=100)
    location: Optional[str] = Field(default=None, max_length=100)
    status: Literal["open", "resolved", "all"] = "open"
    search: Optional[str] = Field(default=None, max_length=200)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_optional_type(cls, value: Any) -> Optional[str]:
        """Normalize empty type filter to None and lowercase valid values."""
        cleaned = _clean_optional_text(value)
        return cleaned.lower() if cleaned is not None else None

    @field_validator("category", "location", "search", mode="before")
    @classmethod
    def normalize_optional_filters(cls, value: Any) -> Optional[str]:
        """Trim optional filter strings and map blanks to None."""
        return _clean_optional_text(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: Any) -> str:
        """Normalize status query parameter, defaulting blanks to 'open'."""
        cleaned = _clean_optional_text(value)
        if cleaned is None:
            return "open"
        lowered = cleaned.lower()
        if lowered not in (*VALID_STATUSES, "all"):
            raise ValueError("status must be one of: open, resolved, all.")
        return lowered


class ItemResponse(BaseModel):
    """Public representation of a campus lost or found notice (excludes manage_token)."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., ge=1)
    type: Literal["lost", "found"]
    title: str
    category: str
    location: str
    date_reported: str
    description: Optional[str] = None
    image_filename: Optional[str] = None
    contact_phone: str
    status: Literal["open", "resolved"]
    created_at: str


class ItemCreateResponse(BaseModel):
    """Response returned when a new campus notice is created."""

    status: Literal["success"] = "success"
    item_id: int = Field(..., ge=1)
    manage_token: str = Field(..., min_length=12, max_length=64)


class ActionResponse(BaseModel):
    """Standard success response for resolve and delete operations."""

    status: Literal["success"] = "success"
    message: str


class ErrorDetail(BaseModel):
    """Structured error payload included in centralized error responses."""

    code: str
    message: str
    errors: Optional[list[dict[str, Any]]] = None


class ErrorResponse(BaseModel):
    """Consistent JSON error envelope across HTTP and validation failures."""

    status: Literal["error"] = "error"
    detail: str
    error: ErrorDetail

