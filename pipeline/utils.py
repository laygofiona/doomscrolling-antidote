import time
import sqlite3
import logging
import json

# Configure logging output format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

WAIT_SECONDS = 5


# utility functions
# Get papers associated with a dailyrun and return readily-accessible papers json objects
def get_papers(dailyrun_id):
    # connect to SQL database file
    cursor = None
    conn = None
    try:
        # connect to SQL database file
        conn = sqlite3.connect("app.db")
        # Set row_factory to sqlite3.Row to access columns by name
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
    except Exception as e:
        logging.error(
            f"Exception on get_papers(): Failed to connect to SQL database file: {e}"
        )
        return []

    # get the papers_ids JSON array from the dailyRun row and decode it into individual arxiv_ids
    paper_ids = []
    try:
        cursor.execute("SELECT papers_ids FROM dailyRun WHERE id = ?", (dailyrun_id,))
        row = cursor.fetchone()
        if row and row["papers_ids"]:
            paper_ids = json.loads(row["papers_ids"])

    except Exception as e:
        logging.error(
            f"Exception on get_papers(): Extracting papers_IDS from dailyRun {e}"
        )

    papers_processed = []
    # Extract papers with IDs from paper_ids from the papers table
    for arxiv_id in paper_ids:
        try:
            cursor.execute("SELECT * FROM papers WHERE arxiv_id = ?", (arxiv_id,))

            res = cursor.fetchone()

            # add paper to papers_processed
            if res is not None:
                papers_processed.append(res)

        except Exception as e:
            logging.error(f"Exception on get_papers(): Extracting papers {e}")

    # close the read connection
    if conn is not None:
        conn.close()

    # Prepare paper data (works for both Pydantic models and sqlite3.Row)
    formatted_papers = [
        p.model_dump() if hasattr(p, "model_dump") else dict(p)
        for p in papers_processed
    ]

    return formatted_papers


def get_id(type: str, dailyRun_id):
    if type == "newsletter":
        column = "newsletter_id"
    elif type == "podcastEpisode":
        column = "podcast_id"
    else:
        logging.error(f"Exception on get_id(): Unknown type '{type}'")
        return None

    conn = None
    result_id = None
    try:
        conn = sqlite3.connect("app.db")
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(f"SELECT {column} FROM dailyRun WHERE id = ?", (dailyRun_id,))
        row = cursor.fetchone()
        if row:
            result_id = row[column]
    except Exception as e:
        logging.error(
            f"Exception on get_id(): Failed to get {column} for dailyRun {dailyRun_id}: {e}"
        )
    finally:
        if conn is not None:
            conn.close()

    return result_id
