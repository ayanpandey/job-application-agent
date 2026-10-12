"""
Build the candidate's structured profile from their own CV.

The onboarding wizard used to be the only source of profile data, and it has no
field for job bullet points -- `app/onboarding.py` says so in a comment. So
`experience[].highlights` was always empty, and since the generated resume is
rendered from the profile, every customer's "tailored" resume went out with an
empty Professional Experience section. The uploaded CV was copied to disk by
`_adopt_resume` and never read: `pypdf` has been a declared dependency all along
with nothing importing it.

This module is the missing step. The CV is the authoritative record of the
candidate's career, so it is what the profile is built from.

Only the resume-content sections are replaced. `personal_info`,
`screening_answers` and `outreach` are the customer's own answers, entered
deliberately and often country-specific, and are never touched -- see
`india-screening-answers`: a screening answer overwritten by a model guess is an
ATS knockout.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.logger import logger
from src.llm.client import LLMClient

# Sections this module owns. Anything outside this set belongs to the customer.
OWNED_SECTIONS = ("professional_summary", "skills", "experience", "education")

SKILL_BUCKETS = ("languages", "frameworks_and_libraries", "cloud_and_devops", "databases")

# Enough of a CV to be the whole CV. The proxy refuses over 24k characters, and
# the prompt wrapper needs room, so a long academic CV is truncated rather than
# rejected outright -- the recent roles that matter are at the top.
MAX_CV_CHARS = 18000


class ProfileImportError(RuntimeError):
    """Raised when a CV cannot be turned into a usable profile."""


def extract_pdf_text(resume_path: str | Path) -> str:
    """
    The CV's text, page by page.

    Deliberately not silent on failure. A scanned CV is an image and yields
    nothing extractable; returning "" there would write an empty profile and
    recreate the exact bug this module exists to fix.
    """
    path = Path(resume_path).expanduser()
    if not path.exists():
        raise ProfileImportError(f"No CV found at {path}.")

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ProfileImportError(f"PDF support is unavailable: {exc}") from exc

    try:
        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as exc:
        raise ProfileImportError(f"Could not read {path.name}: {exc}") from exc

    text = "\n".join(pages).strip()
    # Collapse the ragged whitespace PDF extraction leaves behind; it wastes
    # prompt budget and makes the layout harder for the model to follow.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if len(text) < 200:
        raise ProfileImportError(
            f"{path.name} yielded almost no text ({len(text)} characters). "
            "If it is a scanned image rather than a text PDF, export a text "
            "version and try again."
        )
    return text[:MAX_CV_CHARS]


def _clean(value: Any) -> str:
    """Trim OCR debris off an extracted value.

    Real profiles in the field carry things like ``location: "Bengaluru`"`` --
    a stray backtick that then renders into the PDF sent to an employer.
    """
    text = str(value or "").strip()
    return text.strip(" \t\r\n`'\",;:|").strip()


def _clean_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    seen: List[str] = []
    for v in values:
        cleaned = _clean(v)
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def _year(value: Any) -> Optional[int]:
    """A four-digit year, or None. `EducationItem.graduation_year` is an int."""
    match = re.search(r"(19|20)\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def normalise(extracted: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the model's reply into exactly the shapes the profile models accept."""
    skills_in = extracted.get("skills") or {}
    skills = {bucket: _clean_list(skills_in.get(bucket)) for bucket in SKILL_BUCKETS}

    experience: List[Dict[str, Any]] = []
    for item in extracted.get("experience") or []:
        if not isinstance(item, dict):
            continue
        role, company = _clean(item.get("role")), _clean(item.get("company"))
        if not (role or company):
            continue
        experience.append({
            "role": role,
            "company": company,
            "location": _clean(item.get("location")),
            "start_date": _clean(item.get("start_date")),
            "end_date": _clean(item.get("end_date")) or "Present",
            "highlights": _clean_list(item.get("highlights")),
        })

    education: List[Dict[str, Any]] = []
    for item in extracted.get("education") or []:
        if not isinstance(item, dict):
            continue
        degree, institution = _clean(item.get("degree")), _clean(item.get("institution"))
        if not (degree or institution):
            continue
        entry: Dict[str, Any] = {"degree": degree, "institution": institution}
        year = _year(item.get("graduation_year"))
        if year:
            entry["graduation_year"] = year
        gpa = _clean(item.get("gpa"))
        if gpa:
            entry["gpa"] = gpa
        education.append(entry)

    return {
        "professional_summary": _clean(extracted.get("professional_summary")),
        "skills": skills,
        "experience": experience,
        "education": education,
    }


def extract_profile(resume_path: str | Path, llm: LLMClient) -> Dict[str, Any]:
    """The CV at `resume_path`, as the profile sections it describes."""
    text = extract_pdf_text(resume_path)
    logger.info(f"[ProfileImport] Read {len(text)} characters from {Path(resume_path).name}.")

    data = llm.run_task_json("profile_extract", {"resume_text": text})
    if not data:
        raise ProfileImportError(
            "The AI service did not return a profile. Check your licence key and "
            "connection, then try again."
        )

    profile = normalise(data)

    # Fail closed. Writing a profile with no bullet points is precisely the
    # defect being fixed, and it fails invisibly -- the resume renders, it is
    # simply blank where the candidate's career should be.
    bulleted = [r for r in profile["experience"] if r["highlights"]]
    if not bulleted:
        raise ProfileImportError(
            "No work-history bullet points could be read from the CV, so the "
            "profile was left unchanged. A resume built from it would have an "
            "empty experience section."
        )

    total_bullets = sum(len(r["highlights"]) for r in profile["experience"])
    total_skills = sum(len(v) for v in profile["skills"].values())
    logger.info(
        f"[ProfileImport] Extracted {len(profile['experience'])} role(s), "
        f"{total_bullets} bullet(s), {total_skills} skill(s), "
        f"{len(profile['education'])} education entr(ies)."
    )
    return profile


def merge_into_profile(extracted: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    """
    The existing profile with only its resume content replaced.

    `personal_info`, `resume_path`, `screening_answers` and `outreach` survive
    untouched: they are the customer's own deliberate answers, and a model guess
    overwriting a work-authorisation answer would submit silently and cost them
    the application.
    """
    merged = dict(existing or {})
    for section in OWNED_SECTIONS:
        value = extracted.get(section)
        if value:
            merged[section] = value
    return merged


def summarise(profile: Dict[str, Any]) -> str:
    """A short human-readable digest, for confirming what was imported."""
    lines: List[str] = []
    summary = profile.get("professional_summary") or ""
    lines.append(f"Summary : {summary[:100]}{'...' if len(summary) > 100 else ''}")
    skills = profile.get("skills") or {}
    for bucket in SKILL_BUCKETS:
        values = skills.get(bucket) or []
        if values:
            lines.append(f"{bucket.replace('_', ' ').title()[:8]:<8}: {', '.join(values[:8])}")
    for role in profile.get("experience") or []:
        dates = " - ".join(p for p in (role.get("start_date"), role.get("end_date")) if p)
        lines.append(f"Role    : {role.get('role')} at {role.get('company')} ({dates}) "
                     f"- {len(role.get('highlights') or [])} bullets")
    for edu in profile.get("education") or []:
        lines.append(f"Edu     : {edu.get('degree')}, {edu.get('institution')} "
                     f"{edu.get('graduation_year') or ''}".rstrip())
    return "\n".join(lines)
