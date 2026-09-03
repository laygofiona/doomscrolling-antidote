"""Pydantic models and dataclasses shared across the pipeline and LLM agents."""

# pylint: disable=too-few-public-methods

import datetime
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ValidationInfo, field_validator


class StatusEnum(str, Enum):
    """Lifecycle status of a daily pipeline run."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class Preferences(BaseModel):
    """User-configured preferences loaded from config.json."""

    arxiv_categories: list[str]
    keywords: list[str]
    email: str
    delivery_time: datetime.time
    timezone: str
    papers_per_digest: int
    max_papers_fetched_per_category: int
    user_intention: str
    tone: str


class Paper(BaseModel):
    """An arXiv paper, along with its AI-generated summary fields."""

    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    categories: list[str]
    primary_category: str
    pdf_url: str
    arxiv_url: str
    updated_at: datetime.datetime
    ai_summary: Optional[str] = None
    ai_why_relevant: Optional[str] = None
    fetched_at: datetime.datetime


class Newsletter(BaseModel):
    """A generated newsletter issue."""

    id: int
    title: str
    body_content: str
    sent_at: Optional[datetime.datetime] = None
    run_id: str


class PodcastEpisode(BaseModel):
    """A generated podcast episode."""

    id: int
    title: str
    description: str
    s3_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    file_size_bytes: Optional[int] = None
    published_at: Optional[datetime.datetime] = None
    script: str
    run_id: str


class DailyRun(BaseModel):
    """A single end-to-end run of the pipeline."""

    id: str
    started_at: datetime.datetime
    completed_at: Optional[datetime.datetime] = None
    status: StatusEnum
    error_message: Optional[str] = None
    newsletter_id: Optional[int] = None
    podcast_id: Optional[int] = None
    papers_ids: Optional[list[str]] = None


class SelectedPaperIDs(BaseModel):
    """Output schema for the filter_papers() agent task."""

    selected_ids: list[str]

    # adds a validation step to ensure the number of selected paper IDs matches the expected count
    @field_validator("selected_ids")
    @classmethod
    # cls is the model class, v is the value being validated, info contains context
    def validate_exact_count(cls, v: list[str], info: ValidationInfo) -> list[str]:
        """Ensure only appropriate number of paper IDs are selected by the agent."""
        # PydanticAI automatically places the `deps` object into `info.context`
        deps = info.context if isinstance(info.context, dict) else {}
        expected_count = deps.get("papers_per_digest")

        if expected_count is not None and len(v) != expected_count:
            raise ValueError(
                f"You must return exactly {expected_count} paper IDs, but returned {len(v)}."
            )
        return v


class PaperSummary(BaseModel):
    """Output schema for generating ai_summary and ai_why_relevant sections."""

    ai_summary: str
    ai_why_relevant: str


class NewsletterContent(BaseModel):
    """Output schema for generating newsletter title and body."""

    title: str
    body: str


@dataclass
class PapersContext:
    """Agent dependency context carrying user preferences and paper data."""

    user_intention: str
    tone: str
    papers: list[dict]


class PodcastEpisodeContent(BaseModel):
    """Output schema for generating podcast script, title, and description."""

    script_body: str
    podcast_title: str
    description: str
