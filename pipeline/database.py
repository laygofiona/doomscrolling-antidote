"""SQLite persistence helpers for the pipeline's papers, newsletter, and podcast tables."""

import json
import sqlite3
from enum import Enum


def get_connection(db_path: str = "app.db") -> sqlite3.Connection:
    """Open a SQLite connection with row_factory set for dict-like row access."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = "app.db"):
    """Create the papers, podcastEpisode, newsletter, and dailyRun tables if missing."""
    # Connects to file (creates app.db if it doesn't exist)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # WAL mode lets reads and writes happen concurrently without "database is locked" errors
    conn.execute("PRAGMA journal_mode = WAL")

    # Create table for reddit posts, podcast episodes, newsletters, and daily runs
    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            arxiv_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            authors TEXT NOT NULL,           -- Stored as a JSON string, e.g. '["Author A"]'
            abstract TEXT NOT NULL,
            categories TEXT NOT NULL,        -- Stored as JSON string (e.g., '["cs.AI", "cs.LG"]')
            primary_category TEXT NOT NULL,
            pdf_url TEXT NOT NULL,
            arxiv_url TEXT NOT NULL,
            updated_at TEXT NOT NULL,        -- ISO 8601 string
            ai_summary TEXT,                 -- Nullable
            ai_why_relevant TEXT,            -- Nullable
            fetched_at TEXT NOT NULL         -- ISO 8601 string
        );

        CREATE TABLE IF NOT EXISTS podcastEpisode (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            s3_url TEXT,
            duration_seconds INTEGER,
            file_size_bytes INTEGER,
            published_at TEXT,
            script TEXT NOT NULL,
            run_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS newsletter (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            body_content TEXT NOT NULL,
            sent_at TEXT,
            run_id TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dailyRun (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            newsletter_id INTEGER,
            podcast_id INTEGER,
            papers_ids TEXT
        );
    """
    )

    conn.commit()
    conn.close()


def save_to_db(obj, table_name: str, db_path: str = "app.db"):
    """Insert a Paper, PodcastEpisode, Newsletter, or DailyRun object into its table."""
    conn = sqlite3.connect(db_path, timeout=30)
    # Wait for locks held by other connections instead of failing immediately
    conn.execute("PRAGMA busy_timeout = 30000")
    cursor = conn.cursor()

    if table_name == "papers":
        # Use INSERT OR IGNORE since the same arxiv_id can appear more than once
        # in a single batch (e.g. matched under multiple keywords/categories)
        cursor.execute(
            """
            INSERT OR IGNORE INTO papers (
                arxiv_id, title, authors, abstract, categories, primary_category,
                pdf_url, arxiv_url, updated_at, ai_summary, ai_why_relevant, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                obj.arxiv_id,
                obj.title,
                json.dumps(obj.authors),
                obj.abstract,
                json.dumps(obj.categories),
                obj.primary_category,
                obj.pdf_url,
                obj.arxiv_url,
                obj.updated_at.isoformat(),
                obj.ai_summary,
                obj.ai_why_relevant,
                obj.fetched_at.isoformat(),
            ),
        )

    elif table_name == "podcastEpisode":
        cursor.execute(
            """
            INSERT INTO podcastEpisode (
                id, title, description, s3_url, duration_seconds,
                file_size_bytes, published_at, script, run_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                obj.id,
                obj.title,
                obj.description,
                obj.s3_url,
                obj.duration_seconds,
                obj.file_size_bytes,
                obj.published_at.isoformat() if obj.published_at else None,
                obj.script,
                obj.run_id,
            ),
        )

    elif table_name == "newsletter":
        cursor.execute(
            """
            INSERT INTO newsletter (id, title, body_content, sent_at, run_id)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                obj.id,
                obj.title,
                obj.body_content,
                obj.sent_at.isoformat() if obj.sent_at else None,
                obj.run_id,
            ),
        )

    elif table_name == "dailyRun":
        cursor.execute(
            """
            INSERT INTO dailyRun (
                id, started_at, completed_at, status, error_message,
                newsletter_id, podcast_id, papers_ids
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                obj.id,
                obj.started_at.isoformat(),
                obj.completed_at.isoformat() if obj.completed_at else None,
                obj.status.value if isinstance(obj.status, Enum) else str(obj.status),
                obj.error_message if hasattr(obj, "error_message") else None,
                obj.newsletter_id if hasattr(obj, "newsletter_id") else None,
                obj.podcast_id if hasattr(obj, "podcast_id") else None,
                (
                    json.dumps(obj.papers_ids)
                    if getattr(obj, "papers_ids", None)
                    else None
                ),
            ),
        )

    conn.commit()
    conn.close()


def update_row_db(
    col_name: str, new_value, row_id: str, table_name: str, db_path: str = "app.db"
):
    """Update a single column for the row identified by row_id in table_name."""
    # Column/table names can't be passed as parameterized query params like values can,
    # so validate them against known columns before building the SQL string.
    allowed_columns = {
        "papers": {
            "title",
            "authors",
            "abstract",
            "categories",
            "primary_category",
            "pdf_url",
            "arxiv_url",
            "updated_at",
            "ai_summary",
            "ai_why_relevant",
            "fetched_at",
        },
        "newsletter": {"id", "title", "body_content", "sent_at", "run_id"},
        "podcastEpisode": {
            "id",
            "title",
            "description",
            "s3_url",
            "duration_seconds",
            "file_size_bytes",
            "published_at",
            "script",
            "run_id",
        },
        "dailyRun": {
            "id",
            "started_at",
            "completed_at",
            "status",
            "error_message",
            "newsletter_id",
            "podcast_id",
            "papers_ids",
        },
    }
    if table_name not in allowed_columns or col_name not in allowed_columns[table_name]:
        raise ValueError(f"Cannot update column '{col_name}' on table '{table_name}'")

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    cursor = conn.cursor()

    if table_name == "papers":
        cursor.execute(
            f"UPDATE {table_name} SET {col_name} = ? WHERE arxiv_id = ?",
            (new_value, row_id),
        )
    else:
        cursor.execute(
            f"UPDATE {table_name} SET {col_name} = ? WHERE id = ?", (new_value, row_id)
        )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
