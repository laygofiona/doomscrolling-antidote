"""Shared lookup helpers used by both the pipeline and the LLM agents."""

# pylint: disable=duplicate-code
# The connect/cursor/log-and-bail boilerplate below is intentionally repeated in
# llm/agents.py rather than hidden behind another layer of indirection.

import json
import logging
import sqlite3

from pipeline.database import get_connection
from pipeline.logging_config import configure_logging

configure_logging()

WAIT_SECONDS = 5


def get_papers(dailyrun_id):
    """Return the papers associated with a dailyrun as JSON-ready dicts."""
    cursor = None
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
    except sqlite3.Error as e:
        logging.error(
            "Exception on get_papers(): Failed to connect to SQL database file: %s", e
        )
        return []

    # get the papers_ids JSON array from the dailyRun row and decode it into individual arxiv_ids
    paper_ids = []
    try:
        cursor.execute("SELECT papers_ids FROM dailyRun WHERE id = ?", (dailyrun_id,))
        row = cursor.fetchone()
        if row and row["papers_ids"]:
            paper_ids = json.loads(row["papers_ids"])

    except (sqlite3.Error, json.JSONDecodeError) as e:
        logging.error("Exception on get_papers(): Extracting papers_IDS from dailyRun %s", e)

    papers_processed = []
    # Extract papers with IDs from paper_ids from the papers table
    for arxiv_id in paper_ids:
        try:
            cursor.execute("SELECT * FROM papers WHERE arxiv_id = ?", (arxiv_id,))

            res = cursor.fetchone()

            # add paper to papers_processed
            if res is not None:
                papers_processed.append(res)

        except sqlite3.Error as e:
            logging.error("Exception on get_papers(): Extracting papers %s", e)

    # close the read connection
    if conn is not None:
        conn.close()

    # Prepare paper data (works for both Pydantic models and sqlite3.Row)
    formatted_papers = [
        p.model_dump() if hasattr(p, "model_dump") else dict(p)
        for p in papers_processed
    ]

    return formatted_papers


def get_id(id_type: str, dailyrun_id):
    """Return the newsletter_id or podcast_id column value for a dailyrun."""
    if id_type == "newsletter":
        column = "newsletter_id"
    elif id_type == "podcastEpisode":
        column = "podcast_id"
    else:
        logging.error("Exception on get_id(): Unknown type '%s'", id_type)
        return None

    conn = None
    result_id = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {column} FROM dailyRun WHERE id = ?", (dailyrun_id,))
        row = cursor.fetchone()
        if row:
            result_id = row[column]
    except sqlite3.Error as e:
        logging.error(
            "Exception on get_id(): Failed to get %s for dailyRun %s: %s", column, dailyrun_id, e
        )
    finally:
        if conn is not None:
            conn.close()

    return result_id
