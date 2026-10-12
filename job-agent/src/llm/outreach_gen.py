"""
Generates personalized, high-converting recruiter connection request notes and InMails.
"""

from typing import Optional
from src.core.models import CandidateProfile, JobListing
from src.llm.client import LLMClient
from src.core.logger import logger


class OutreachGenerator:
    def __init__(self, llm_client: LLMClient):
        self.llm = llm_client

    def generate_connection_note(
        self,
        job: JobListing,
        recipient_name: str,
        recipient_title: str,
        profile: CandidateProfile,
        max_chars: int = 200,
    ) -> str:
        """
        Generates a concise LinkedIn connection invite note strictly under max_chars limit.
        """
        data = self.llm.run_task_json(
            "recruiter_outreach",
            {
                "candidate_name": profile.personal_info.full_name,
                "candidate_summary": profile.professional_summary[:200],
                "job_title": job.title,
                "company_name": job.company,
                "recipient_name": recipient_name,
                "recipient_title": recipient_title,
            },
        )
        note = data.get("note", "").strip()

        if not note:
            # Fallback template, built from the candidate's own profile. It used
            # to claim "4+ yrs building AI/cloud systems" for everyone, which is
            # a fabricated credential the moment the app has more than one user.
            first_name = recipient_name.split()[0] if recipient_name else "there"
            years = profile.screening_answers.total_years_experience
            pitch = ((profile.outreach or {}).get("elevator_pitch") or "").strip()
            if not pitch:
                pitch = (profile.professional_summary or "").strip()
            # Keep room for the greeting and the ask inside the 200-char limit.
            pitch = " ".join(pitch.split())[:80].rstrip(" ,.;:")
            credential = f"With {years} yrs' experience" if years else "With my background"
            note = (
                f"Hi {first_name}, I saw the {job.title} opening at {job.company}. "
                f"{credential} in {pitch}, I'd love to connect!"
            )

        # Ensure strict character count constraint
        if len(note) > max_chars:
            # Truncate gracefully to last sentence or word
            note = note[:max_chars - 3].rstrip() + "..."

        return note
