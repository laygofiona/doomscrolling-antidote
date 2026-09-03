"""LLM agent tasks: filtering, summarizing, and generating newsletter/podcast content."""

# pylint: disable=duplicate-code


import io
import json
import logging
import re
import secrets
import sqlite3
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from pydantic_ai import Agent
from pypdf import PdfReader

from pipeline.database import get_connection, save_to_db, update_row_db
from pipeline.logging_config import configure_logging
from pipeline.models import (
    DailyRun,
    Newsletter,
    NewsletterContent,
    Paper,
    PapersContext,
    PaperSummary,
    PodcastEpisode,
    PodcastEpisodeContent,
    SelectedPaperIDs,
    StatusEnum,
)
from pipeline.utils import get_papers

# load environment variables
load_dotenv()

configure_logging()

WAIT_SECONDS = 5


def _extract_pdf_text_from_url(url: str) -> str:
    """Download a PDF from url and return its concatenated page text."""
    # fetch raw PDF content in bytes over HTTP
    response = requests.get(url, timeout=30)
    # checks for the response code and errors out if 4xx/5xx
    response.raise_for_status()

    # wraps bytes in a binary stream so PdfReader can read it as a file-like object
    pdf_file = io.BytesIO(response.content)
    reader = PdfReader(pdf_file)

    # Extract text
    return "\n".join([page.extract_text() or "" for page in reader.pages])


def _generate_time_id(random_length=4) -> str:
    """Build a sortable ID from the current timestamp plus a random hex suffix."""
    # Timestamp down to seconds
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    # Append random hex bytes
    random_suffix = secrets.token_hex(random_length // 2)
    return f"{timestamp}_{random_suffix}"


def _get_new_papers(papers):
    """Return only the papers not already present in the papers table."""
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
    except sqlite3.Error as e:
        logging.error(
            "Exception on filter_papers(): Failed to connect to SQL database file: %s", e
        )
        return []

    new_papers = []
    for paper in papers:
        try:
            cursor.execute(
                "SELECT * FROM papers WHERE arxiv_id = ?", (str(paper.arxiv_id),)
            )
            # if there is no result, paper does not exist in the database yet
            if not cursor.fetchone():
                new_papers.append(paper)
        except sqlite3.Error as e:
            logging.error(
                "Exception on filter_papers(): Failed to execute select SQL query "
                "for layer1_papers: %s",
                e,
            )

    if conn is not None:
        conn.close()

    return new_papers


def _filter_by_keywords(papers, keywords):
    """Keep only papers whose abstract matches at least one keyword."""
    # convert keywords to a regex pattern that matches whole words, case-insensitive
    # e.g: r"\b(python|c\+\+|data\ science)\b"
    pattern = r"\b(" + "|".join(map(re.escape, keywords)) + r")\b"
    matched_papers = []

    for paper in papers:
        try:
            match = re.search(pattern, paper.abstract, re.IGNORECASE)
        except re.error as e:
            logging.error(
                "Exception on filter_papers(): Failed to execute REGEX search "
                "pattern for layer2_papers: %s",
                e,
            )
            continue

        if match:
            matched_papers.append(paper)

    return matched_papers


def _select_top_papers(papers, papers_per_digest, user_intention, keywords):
    """Rank papers by relevance to user_intention and return the top N with their IDs (arxiv_id)."""
    # structured LLM call with judgement on relevance to user intention and keywords
    if len(papers) <= papers_per_digest:
        # automatically returns papers and their IDs if there are fewer than the requested number
        return papers, [p.arxiv_id for p in papers]

    try:
        agent = Agent(
            "openai:gpt-5-nano",
            output_type=SelectedPaperIDs,
            system_prompt=(
                "You are an expert research assistant and teacher. Analyze the "
                "provided list of papers and return only those relevant to the "
                "user prompt that will help the user's learning and knowledge growth."
            ),
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        # Agent() talks to an external LLM provider; failures span network, auth,
        # and validation errors we want to log and recover from, not enumerate.
        logging.error("Exception on filter_papers(): Initializing filtering agent %s", e)
        return [], []

    user_prompt = (
        f"Select top relevant papers for user intention '{user_intention}' "
        f"and keywords: {keywords}."
    )

    selected_ids = []
    top_papers = []
    try:
        time.sleep(WAIT_SECONDS)
        # pass context so Pydantic's validator knows the dynamic limit
        # execute agent loop
        result = agent.run_sync(
            f"User Query: {user_prompt}\n\n"
            f"Candidate Papers:\n{[p.model_dump() for p in papers]}",
            deps={"papers_per_digest": papers_per_digest},
        )
        selected_ids.extend(result.output.selected_ids)

        for selected_id in selected_ids:
            for paper in papers:
                if paper.arxiv_id == selected_id:
                    top_papers.append(paper)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Exception on filter_papers(): Running agent.run_sync() %s", e)

    return top_papers, selected_ids


def _save_filtered_run(papers, selected_ids):
    """Persist the selected papers and create the dailyRun row."""
    for paper in papers:
        try:
            save_to_db(paper, "papers")
        except sqlite3.Error as e:
            logging.error("Exception on filter_papers(): Saving layer3_papers to DB: %s", e)

    dailyrun_id = _generate_time_id()
    try:
        # create a new dailyRun row with the selected paper IDs
        daily_run = DailyRun(
            id=dailyrun_id,
            started_at=datetime.now(),
            status=StatusEnum.RUNNING,
            papers_ids=selected_ids,
        )
        # save the dailyRun row to the database dailyRun table
        save_to_db(daily_run, "dailyRun")
    except sqlite3.Error as e:
        logging.error("Exception on filter_papers(): Creating daily_run row: %s", e)

    # return the dailyrun_id so it can be used for subsequent steps in the pipeline
    return dailyrun_id


class LLMClient:
    """Namespace for the LLM-driven steps of the pipeline."""

    @staticmethod
    def filter_papers(papers, keywords, papers_per_digest, user_intention):
        """Filter papers down to the top papers_per_digest relevant to user_intention."""
        # get new papers not already in the database
        new_papers = _get_new_papers(papers)
        # filter new papers by keywords in abstract
        matched_papers = _filter_by_keywords(new_papers, keywords)
        # only select the top papers_per_digest most relevant to user_intention
        # uses an LLM agent to rank papers and return the top N with their IDs (arxiv_id)
        top_papers, selected_ids = _select_top_papers(
            matched_papers, papers_per_digest, user_intention, keywords
        )
        # return dailyRun ID after saving the selected papers and creating the dailyRun row
        return _save_filtered_run(top_papers, selected_ids)

    @staticmethod
    def summarize_papers(user_intention, tone):
        """Summarize today's papers, writing ai_summary/ai_why_relevant back to the DB."""
        # simple LLM call
        # Get all paper rows that were fetched today
        papers_list: list[Paper] = []

        conn = None
        try:
            conn = get_connection()
            cursor = conn.cursor()
        except sqlite3.Error as e:
            logging.error(
                "Exception on summarize_papers(): Failed to connect to SQL database file: %s", e
            )
            return

        # get today's date prefix (e.g., '2026-08-24')
        today_date = datetime.now().strftime("%Y-%m-%d")
        try:
            # use LIKE to match any timestamp starting with today's date
            cursor.execute(
                "SELECT * FROM papers WHERE fetched_at LIKE ?", (f"{today_date}%",)
            )

            # get all resulting papers that were fetched today
            res = cursor.fetchall()

            # add res papers to papers_list
            papers_list.extend(res)

        except sqlite3.Error as e:
            logging.error("Exception on summarize_papers(): Extracting row: %s", e)

        # close the read connection
        if conn is not None:
            conn.close()

        # Initializing summarizer agent
        try:
            agent = Agent(
                "openai:gpt-5-nano",
                output_type=PaperSummary,
                system_prompt=(
                    "You are an expert research assistant and teacher. Analyze and "
                    "understand the academic paper so that you can write a comprehensive "
                    "full summary of the paper and a text on why its relevant for your "
                    "student's intetion"
                ),
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logging.error("Exception on filter_papers(): Initializing agent %s", e)
            return

        # Extract pdf from each paper row and summarize
        for paper_row in papers_list:
            try:
                # Must read entire paper first
                pdf_text = _extract_pdf_text_from_url(paper_row["pdf_url"])

                # Summarize the paper using OpenAI API via Pydantic AI
                # LLM writes short summary per selected paper, saves to ai_summary column
                # and ai_why_relevant column
                result = agent.run_sync(
                    f"Student Intention: {user_intention}\n\n"
                    f"Here is the full text of the paper:\n\n{pdf_text}\n\n"
                    "Understand it thoroughly and write a less than 150 word summary and "
                    "less than 150 word why_relevant section."
                    f"Must use the following tone: {tone}. Stick to strict word count!"
                )

                summary_data = result.output.ai_summary
                relevant_section = result.output.ai_why_relevant

                time.sleep(WAIT_SECONDS)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logging.error(
                    "Exception for summarize_papers(): error on running summarizer agent: %s", e
                )
                continue

            try:
                # Write summary_data to ai_summary column
                update_row_db(
                    col_name="ai_summary",
                    new_value=summary_data,
                    row_id=paper_row["arxiv_id"],
                    table_name="papers",
                )

                # Write relevant_section to ai_why_relevant column
                update_row_db(
                    col_name="ai_why_relevant",
                    new_value=relevant_section,
                    row_id=paper_row["arxiv_id"],
                    table_name="papers",
                )
            except sqlite3.Error as e:
                logging.error(
                    "Exception for summarize_papers(): error on updating ai_summary and "
                    "ai_why_relevant sections: %s",
                    e,
                )

            time.sleep(WAIT_SECONDS)

    @staticmethod
    def generate_newsletter_content(dailyrun_id, tone, user_intention):
        """Generate the newsletter title/intro body and save it to the newsletter table."""
        # simple LLM call
        # get papers associated with dailyrun_id
        formatted_papers = get_papers(dailyrun_id)
        deps = PapersContext(
            user_intention=user_intention, tone=tone, papers=formatted_papers
        )

        # initialize newsletter agent
        try:
            agent = Agent(
                "openai:gpt-5-nano",
                output_type=NewsletterContent,
                deps_type=PapersContext,
                system_prompt="You are a marketer, writer, and scientific researcher. "
                "You are writing parts of a newsletter dedicated to helping the user's intention: "
                f"'{deps.user_intention}'. Generate a captivating title and short intro body "
                f"summarizing the papers with the following tone: {deps.tone}.",
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logging.error(
                "Exception on generate_newsletter_content(): Initializing agent %s", e
            )
            return

        # Generate newsletter opening body and title summarizing all articles using OpenAI
        # API via Pydantic AI
        title = None
        body = None
        try:
            result = agent.run_sync(
                f"Here are the papers to summarize:\n{json.dumps(formatted_papers, indent=2)}\n\n"
                "Write a clear, captivating intro body under 150 words.",
                deps=deps,
            )

            title = result.output.title
            body = result.output.body

            time.sleep(WAIT_SECONDS)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logging.error(
                "Exception for generate_newsletter_content(): error on running "
                "newsletter agent: %s",
                e,
            )

        # generating ID
        now = datetime.now()
        int_id = int(now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}")
        # Save to newsletter table
        try:
            newsletter_obj = Newsletter(
                id=int_id, title=title, body_content=body, run_id=dailyrun_id
            )
            save_to_db(obj=newsletter_obj, table_name="newsletter")
        except sqlite3.Error as e:
            logging.error(
                "Exception for generate_newsletter_content(): adding newsletter row "
                "to newsletter table: %s",
                e,
            )

        # Update dailyRun row to add newsletter_id
        try:
            update_row_db(
                col_name="newsletter_id",
                new_value=int_id,
                row_id=dailyrun_id,
                table_name="dailyRun",
            )
        except sqlite3.Error as e:
            logging.error(
                "Exception for generate_newsletter_content(): updating newsletter_id "
                "in dailyRun table: %s",
                e,
            )

    @staticmethod
    def generate_podcast_script(dailyrun_id, tone, user_intention):
        """Generate the podcast script/title/description and save it to the DB."""
        # simple LLM call
        # Generate podcast script using OpenAI API via Pydantic AI
        # LLM writes podcast script based on the summaries of the papers, saves to
        # podcast_script column.

        # get papers associated with dailyrun_id
        formatted_papers = get_papers(dailyrun_id)
        deps = PapersContext(
            user_intention=user_intention, tone=tone, papers=formatted_papers
        )

        # initialize newsletter agent
        try:
            agent = Agent(
                "openai:gpt-5-nano",
                output_type=PodcastEpisodeContent,
                deps_type=PapersContext,
                system_prompt="You are a podcaster, writer, and scientific researcher for "
                "the show Research Digest. "
                "You are writing an engaging podcast script body and title dedicated to "
                "helping the user's intention: "
                f"'{deps.user_intention}'. Generate a podcast script body and title where "
                "you are the only speaker"
                f"summarizing the papers with the following tone: {deps.tone}."
                "Also generate a short description of the podcast episode"
                "STRICTLY FOLLOW: When creating the script, do not add any labels, "
                "sections, or headings. Just add the text you would say. ",
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            logging.error(
                "Exception on generate_podcast_script(): Initializing agent %s", e
            )
            return

        # Generate title and script via Pydantic AI and Open AI API
        title = None
        try:
            result = agent.run_sync(
                f"Here are the papers to summarize:\n{json.dumps(formatted_papers, indent=2)}\n\n"
                "Write a clear, captivating podcast script under 800 words, title, and a "
                "short description (under 100 words) of the podcast episode",
                deps=deps,
            )

            title = result.output.podcast_title
            body = result.output.script_body
            description = result.output.description
            time.sleep(WAIT_SECONDS)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logging.error(
                "Exception for generate_podcast_script(): error on running podcast "
                "writer agent: %s",
                e,
            )

        # generating ID
        now = datetime.now()
        int_id = int(now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}")

        # Save to podcastEpisode table
        try:
            podcast_episode_obj = PodcastEpisode(
                id=int_id,
                title=title,
                description=description,
                script=body,
                run_id=dailyrun_id,
            )
            save_to_db(obj=podcast_episode_obj, table_name="podcastEpisode")
        except sqlite3.Error as e:
            logging.error(
                "Exception for generate_podcast_script(): adding podcast row to "
                "podcastEpisode table: %s",
                e,
            )

        # Update dailyRun row to add podcast_id
        try:
            update_row_db(
                col_name="podcast_id",
                new_value=int_id,
                row_id=dailyrun_id,
                table_name="dailyRun",
            )
        except sqlite3.Error as e:
            logging.error(
                "Exception for generate_podcast_script(): updating podcast_id in "
                "dailyRun table: %s",
                e,
            )
