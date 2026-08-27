from pydantic import BaseModel, field_validator, ValidationInfo
import datetime
from enum import Enum
from typing import Optional, List
from dataclasses import dataclass


class StatusEnum(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"

class Preferences(BaseModel):
    arxiv_categories: list[str]
    keywords: list[str]
    email: str
    delivery_time: datetime.time
    timezone: str
    papers_per_digest: int
    max_papers_fetched_per_category: int
    llm_provider: str
    llm_model: str
    user_intention: str
    tone: str
    

    
class paper(BaseModel):
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
    

class newsletter(BaseModel):
    id: int
    title: str
    body_content: str
    sent_at: Optional[datetime.datetime] = None
    run_id: str
    
class podcastEpisode(BaseModel):
    id: int
    title: str
    description: str
    s3_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    file_size_bytes: Optional[int] = None
    published_at: Optional[datetime.datetime] = None
    script: str
    run_id: str
    
class dailyRun(BaseModel):
    id: str
    started_at: datetime.datetime
    completed_at: Optional[datetime.datetime] = None
    status: StatusEnum
    error_message: Optional[str] = None
    newsletter_id: Optional[int] = None
    podcast_id: Optional[int] = None
    papers_ids: Optional[list[str]] = None
    
# For filter_papers() agent task
class SelectedPaperIDs(BaseModel):
    selected_ids: list[str]
    # Ensure only appropriate number of paper IDs are selected by the agent
    @field_validator("selected_ids")
    @classmethod
    def validate_exact_count(cls, v: list[str], info: ValidationInfo) -> list[str]:
        # PydanticAI automatically places the `deps` object into `info.context`
        deps = info.context if isinstance(info.context, dict) else {}
        expected_count = deps.get("papers_per_digest")
        
        if expected_count is not None and len(v) != expected_count:
            raise ValueError(
                f"You must return exactly {expected_count} paper IDs, but returned {len(v)}."
            )
        return v


# For generating ai_summary and ai_why_relevant sections
class PaperSummary(BaseModel):
    ai_summary: str
    ai_why_relevant: str
    

# For generating newsletter title and body
class Newsletter_Content(BaseModel):
    title: str
    body: str
    

@dataclass
class PapersContext:
    user_intention: str
    tone: str
    papers: list[dict]

class Podcast_Episode_Content(BaseModel):
    script_body: str
    podcast_title: str
    description: str