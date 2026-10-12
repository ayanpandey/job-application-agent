"""
Data models and schemas for the LinkedIn AI Agent.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    DISCOVERED = "discovered"
    EVALUATED_MATCH = "evaluated_match"
    EVALUATED_REJECT = "evaluated_reject"
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"


class OutreachStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Candidate Profile Schemas
# ---------------------------------------------------------------------------

class PersonalInfo(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone_country_code: str = "+1"
    phone_number: str
    city: str = ""
    state: str = ""
    country: str = "United States"
    postal_code: str = ""
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class ExperienceItem(BaseModel):
    role: str
    company: str
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    highlights: List[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    degree: str
    institution: str
    graduation_year: Optional[int] = None
    gpa: Optional[str] = None


class SkillsConfig(BaseModel):
    languages: List[str] = Field(default_factory=list)
    frameworks_and_libraries: List[str] = Field(default_factory=list)
    cloud_and_devops: List[str] = Field(default_factory=list)
    databases: List[str] = Field(default_factory=list)

    def all_skills(self) -> List[str]:
        return (
            self.languages
            + self.frameworks_and_libraries
            + self.cloud_and_devops
            + self.databases
        )


class ScreeningAnswers(BaseModel):
    total_years_experience: int = 4
    years_experience_by_skill: Dict[str, int] = Field(default_factory=dict)
    work_authorization: Dict[str, Any] = Field(default_factory=dict)
    preferences: Dict[str, Any] = Field(default_factory=dict)
    demographics: Dict[str, Any] = Field(default_factory=dict)


class CandidateProfile(BaseModel):
    personal_info: PersonalInfo
    resume_path: str = "./resumes/resume.pdf"
    professional_summary: str = ""
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    experience: List[ExperienceItem] = Field(default_factory=list)
    education: List[EducationItem] = Field(default_factory=list)
    screening_answers: ScreeningAnswers = Field(default_factory=ScreeningAnswers)
    outreach: Dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Job & Evaluation Schemas
# ---------------------------------------------------------------------------

class JobListing(BaseModel):
    id: Optional[int] = None
    job_id: str  # LinkedIn unique job ID
    title: str
    company: str
    location: str
    url: str
    is_easy_apply: bool = True
    description: str = ""
    posted_date_text: str = ""
    recruiter_name: Optional[str] = None
    recruiter_profile_url: Optional[str] = None
    status: JobStatus = JobStatus.DISCOVERED
    match_score: Optional[int] = None
    match_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    applied_at: Optional[datetime] = None


class MatchEvaluation(BaseModel):
    match_score: int
    is_recommended: bool
    match_tier: str = "Moderate Fit"
    key_matching_skills: List[str] = Field(default_factory=list)
    missing_or_gap_skills: List[str] = Field(default_factory=list)
    summary_reasoning: str = ""


class ScreeningAnswerResult(BaseModel):
    answer: str
    confidence: float = 1.0
    reasoning: str = ""
    # Set when the solver could not determine an answer it is willing to stand
    # behind. Guessing on a screening question is worse than leaving it blank:
    # a wrong "No" on work authorization is an instant ATS knockout, and it is
    # invisible because the application still submits. Callers must skip the
    # field and surface it rather than fill it.
    needs_human: bool = False


class OutreachRecord(BaseModel):
    id: Optional[int] = None
    job_id: str
    company: str
    recipient_name: str
    recipient_title: str
    profile_url: str
    note_text: str
    status: OutreachStatus = OutreachStatus.QUEUED
    sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
