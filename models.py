"""SQLite database engine and repository helpers for Campus Lost & Found."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import closing
import logging
from pathlib import Path
import sqlite3
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent / "items.db"

VALID_TYPES = ("lost", "found")
VALID_CATEGORIES = (
    "Smart Card / ID",
    "Keys",
    "Electronics / Audio",
    "Stationery / Drafter",
    "Cycle / Other",
)
VALID_LOCATIONS = (
    "Central Library",
    "LT / LP Complex",
    "Hostel Area",
    "COS / Sports",
    "Other",
)
VALID_STATUSES = ("open", "resolved")

logger = logging.getLogger(__name__)


def _sql_in_list(values: tuple[str, ...]) -> str:
    """Format a tuple of constant strings into a SQL IN (...) literal list."""
    escaped = (value.replace("'", "''") for value in values)
    return ", ".join(f"'{value}'" for value in escaped)


def _escape_like_term(term: str) -> str:
    """Escape SQL LIKE wildcard characters so search matches literal substrings."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Create and configure a SQLite connection with performance PRAGMAs."""
    target_path = Path(db_path) if db_path is not None else DB_PATH
    conn = sqlite3.connect(target_path, timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Yield a scoped SQLite connection for FastAPI request dependencies."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | Path | None = None) -> None:
    """Initialize the SQLite schema and composite indexes without seed data."""
    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK (type IN ({_sql_in_list(VALID_TYPES)})),
            title TEXT NOT NULL,
            category TEXT NOT NULL CHECK (category IN ({_sql_in_list(VALID_CATEGORIES)})),
            location TEXT NOT NULL CHECK (location IN ({_sql_in_list(VALID_LOCATIONS)})),
            date_reported TEXT NOT NULL,
            description TEXT,
            image_filename TEXT,
            contact_phone TEXT NOT NULL,
            status TEXT DEFAULT 'open' CHECK (status IN ({_sql_in_list(VALID_STATUSES)})),
            manage_token TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """
    create_filter_index_sql = """
        CREATE INDEX IF NOT EXISTS idx_items_filter
        ON items (type, status, category, location);
    """
    create_feed_index_sql = """
        CREATE INDEX IF NOT EXISTS idx_items_status_feed
        ON items (status, type, category, location, id DESC);
    """

    with closing(get_connection(db_path)) as conn:
        conn.execute(create_table_sql)
        conn.execute(create_filter_index_sql)
        conn.execute(create_feed_index_sql)
        conn.commit()
    logger.debug("SQLite schema initialized at %s", db_path or DB_PATH)


def create_item(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    title: str,
    category: str,
    location: str,
    date_reported: str,
    description: Optional[str],
    image_filename: Optional[str],
    contact_phone: str,
    manage_token: str,
) -> int:
    """Insert a new item notice with status 'open' and return its row ID."""
    cursor = conn.execute(
        """
        INSERT INTO items (
            type,
            title,
            category,
            location,
            date_reported,
            description,
            image_filename,
            contact_phone,
            status,
            manage_token
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
        """,
        (
            item_type,
            title,
            category,
            location,
            date_reported,
            description,
            image_filename,
            contact_phone,
            manage_token,
        ),
    )
    conn.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite insert did not yield a row ID.")
    return int(cursor.lastrowid)


def list_items(
    conn: sqlite3.Connection,
    *,
    item_type: Optional[str] = None,
    category: Optional[str] = None,
    location: Optional[str] = None,
    status: str = "open",
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Query filtered public item records ordered newest-first without manage_token."""
    conditions: list[str] = []
    params: list[str] = []

    if item_type:
        conditions.append("type = ?")
        params.append(item_type)

    if category:
        conditions.append("category = ?")
        params.append(category)

    if location:
        conditions.append("location = ?")
        params.append(location)

    if status != "all":
        conditions.append("status = ?")
        params.append(status)

    if search:
        pattern = f"%{_escape_like_term(search)}%"
        conditions.append(
            "(LOWER(title) LIKE LOWER(?) ESCAPE '\\' "
            "OR LOWER(COALESCE(description, '')) LIKE LOWER(?) ESCAPE '\\')"
        )
        params.extend([pattern, pattern])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
        SELECT
            id,
            type,
            title,
            category,
            location,
            date_reported,
            description,
            image_filename,
            contact_phone,
            status,
            created_at
        FROM items
        {where_clause}
        ORDER BY id DESC
    """
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def get_item_for_auth(conn: sqlite3.Connection, item_id: int) -> Optional[sqlite3.Row]:
    """Fetch minimal item fields required for token verification and deletion."""
    return conn.execute(
        "SELECT id, status, image_filename, manage_token FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()


def mark_item_resolved(conn: sqlite3.Connection, item_id: int) -> None:
    """Update an item's status to 'resolved' and commit the transaction."""
    conn.execute("UPDATE items SET status = 'resolved' WHERE id = ?", (item_id,))
    conn.commit()


def delete_item_by_id(conn: sqlite3.Connection, item_id: int) -> None:
    """Delete an item row by ID and commit the transaction."""
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully")
