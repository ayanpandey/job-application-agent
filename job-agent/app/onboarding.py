"""
Turns the wizard's answers into the two YAML files the agent actually runs on.

The delicate part is work authorization. A screening question like "are you
legally authorized to work here?" is only answerable relative to a country, and
answering it wrong is an invisible failure: the application still submits, and
the ATS discards it. So the wizard asks which country the candidate can work in,
and this module writes the country-scoped keys that `form_solver` looks for --
`legally_authorized_in_india`, and so on -- with every other country explicitly
false rather than absent. Absent reads as unknown and gets escalated; false is a
real answer. Neither is a guess.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.core import paths

# Imported rather than restated: the solver's spelling of these slugs is the one
# that matters, and a second copy here would drift out of sync silently.
from src.llm.form_solver import _COUNTRY_KEY_SLUGS, _normalize_country


SKILL_BUCKETS = ("languages", "frameworks_and_libraries", "cloud_and_devops", "databases")


def _clean_list(values: Any) -> List[str]:
    if isinstance(values, str):
        values = [v for v in values.replace("\n", ",").split(",")]
    return [str(v).strip() for v in (values or []) if str(v).strip()]


def _read_profile() -> Dict[str, Any]:
    """The profile currently on disk, or {} when there is none or it is unreadable."""
    path = paths.profile_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _same_post(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Whether two experience entries describe the same job."""
    def norm(entry: Dict[str, Any], key: str) -> str:
        return str(entry.get(key) or "").strip().casefold()

    return norm(a, "role") == norm(b, "role") and norm(a, "company") == norm(b, "company")


def _merge_skills(incoming: List[str], existing: Dict[str, Any]) -> Dict[str, List[str]]:
    """
    Fold the wizard's one flat skill list back into the four buckets.

    The wizard deliberately shows a single list -- making somebody sort their own
    skills into "frameworks" and "cloud and devops" during onboarding buys
    nothing, and matching reads `all_skills()`, which concatenates them anyway.
    The resume template does render the buckets separately though, so tipping
    every skill into `languages` on each save would quietly flatten a profile
    that arrived categorised. Keep each skill in whatever bucket it already sits
    in, drop the ones removed in the UI, and file genuinely new ones under
    `languages`.
    """
    buckets = {name: list(existing.get(name) or []) for name in SKILL_BUCKETS}
    wanted = {skill.casefold(): skill for skill in incoming}

    seen: set = set()
    for name, values in buckets.items():
        surviving = []
        for skill in values:
            key = skill.casefold()
            if key in wanted and key not in seen:
                surviving.append(skill)
                seen.add(key)
        buckets[name] = surviving

    for key, original in wanted.items():
        if key not in seen:
            buckets["languages"].append(original)
    return buckets


def _adopt_resume(source: Any, strict: bool = False) -> str:
    """
    Copy a chosen resume into the app's own resumes folder, returning where it landed.

    The file dialog hands back wherever the customer happens to keep the file --
    Downloads, a synced drive, a memory stick. Recording that path verbatim means
    the ±10% fidelity audit and the fallback upload in `easy_apply` both break
    the day the file moves, and during a run that reads as a failed application
    rather than as an error anyone sees. Copying it under `resumes/` makes the
    app's own copy the one of record, and puts the document where
    `get_resumes()` already looks for the master.

    `strict` is for the Settings picker, where a bad choice has a UI to report
    into. A profile save is lenient instead: if the original has since been
    moved or deleted there is nothing to re-copy, and dropping the recorded path
    would leave the agent with no resume at all.
    """
    raw = str(source or "").strip()
    if not raw:
        if strict:
            raise ValueError("Choose a PDF first.")
        return ""

    src = Path(raw).expanduser()
    if not src.is_file():
        if strict:
            raise FileNotFoundError(f"There is no file at {src}")
        return raw
    if src.suffix.lower() != ".pdf":
        if strict:
            raise ValueError("The base resume has to be a PDF.")
        return str(src)

    paths.ensure_dirs()
    dest = paths.resumes_dir() / src.name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return str(dest)


def _work_authorization(country_raw: str, requires_sponsorship: bool) -> Dict[str, Any]:
    """
    Country-scoped authorization flags.

    True for the country the candidate named, explicitly False everywhere else.
    """
    canonical = _normalize_country(country_raw) or "india"
    auth: Dict[str, Any] = {
        "citizenship": country_raw.strip() or canonical.title(),
        "requires_sponsorship_now_or_future": bool(requires_sponsorship),
    }
    for country, slugs in _COUNTRY_KEY_SLUGS.items():
        authorized = country == canonical
        for slug in slugs:
            auth[f"legally_authorized_in_{slug}"] = authorized
    return auth


def build_profile(data: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Wizard payload -> a dict that validates as a CandidateProfile.

    `existing` is the profile already on disk, when there is one.

    The wizard collects a deliberately small slice of what a profile holds: it
    has no field for education, for employers before the current one, for
    demographics, or for a GitHub or portfolio URL. Rebuilding the file from the
    payload alone therefore meant that opening Settings, changing a phone number
    and pressing Save silently erased all of them -- and the erasure was
    invisible until a later run produced a resume with no degree on it. Anything
    the wizard does not own is carried across untouched.
    """
    existing = existing or {}
    existing_personal = existing.get("personal_info", {}) or {}
    existing_screening = existing.get("screening_answers", {}) or {}
    existing_prefs = existing_screening.get("preferences", {}) or {}
    existing_demographics = existing_screening.get("demographics", {}) or {}

    titles = _clean_list(data.get("job_titles"))
    skills = _clean_list(data.get("skills"))
    years = int(data.get("total_years_experience") or 0)
    country = str(data.get("work_country") or data.get("country") or "").strip()

    # A blank box falls back to the stored summary before it falls back to a
    # generated one: the field is pre-filled from disk, so arriving here empty is
    # far more likely to be a hydration failure than a deliberate wipe, and
    # replacing a written summary with "AI Engineer with 3 years of experience"
    # is the kind of quiet downgrade nobody reviews.
    summary = str(data.get("professional_summary") or "").strip()
    if not summary:
        summary = str(existing.get("professional_summary") or "").strip()
    if not summary:
        role = str(data.get("current_role") or (titles[0] if titles else "professional")).strip()
        summary = (
            f"{role} with {years} years of experience"
            + (f" across {', '.join(skills[:6])}." if skills else ".")
        )

    # Only the current post is editable, so earlier employers ride along
    # untouched rather than being dropped on every save.
    existing_experience = [e for e in (existing.get("experience") or []) if isinstance(e, dict)]
    experience: List[Dict[str, Any]] = list(existing_experience)
    if data.get("current_role") or data.get("current_company"):
        current = {
            "role": str(data.get("current_role") or "").strip(),
            "company": str(data.get("current_company") or "").strip(),
            "location": str(data.get("city") or "").strip(),
            "start_date": str(data.get("current_start_date") or "").strip(),
            "end_date": "Present",
            "highlights": _clean_list(data.get("current_highlights")),
        }
        head = existing_experience[0] if existing_experience else {}
        # Still the same job: the wizard has no start-date or highlights field,
        # so take those from the record instead of blanking them. A different
        # role or employer means they describe a post the candidate has left,
        # and carrying them over would attach the wrong dates to the new one.
        if _same_post(head, current):
            current["start_date"] = current["start_date"] or str(head.get("start_date") or "").strip()
            current["highlights"] = current["highlights"] or list(head.get("highlights") or [])
            current["location"] = current["location"] or str(head.get("location") or "").strip()
        experience = [current] + existing_experience[1:]

    education: List[Dict[str, Any]] = [
        e for e in (existing.get("education") or []) if isinstance(e, dict)
    ]
    if data.get("degree"):
        year = data.get("graduation_year")
        entry = {
            "degree": str(data.get("degree")).strip(),
            "institution": str(data.get("institution") or "").strip(),
            "graduation_year": int(year) if str(year or "").isdigit() else None,
        }
        education = [entry] + education[1:]

    # Preserved wholesale so any preference key the wizard has no control over
    # -- a hybrid-commute answer, a USD figure -- survives the round trip.
    preferences = dict(existing_prefs)
    preferences.update(
        {
            "notice_period_days": int(data.get("notice_period_days") or 30),
            "notice_period_weeks": max(1, int(data.get("notice_period_days") or 30) // 7),
            "desired_salary_inr_lakhs": float(data.get("expected_ctc_lakhs") or 0) or None,
            "minimum_salary_inr_lakhs": float(data.get("minimum_ctc_lakhs") or 0) or None,
            "desired_salary_usd": int(
                data.get("desired_salary_usd") or existing_prefs.get("desired_salary_usd") or 0
            )
            or None,
            "willing_to_relocate": bool(data.get("willing_to_relocate", True)),
            "willing_to_commute_hybrid": bool(
                data.get("willing_to_commute_hybrid", existing_prefs.get("willing_to_commute_hybrid", True))
            ),
            "can_work_remotely": bool(data.get("can_work_remotely", True)),
        }
    )

    # Derived from the skill list, but a value already recorded for a skill wins:
    # nothing in the UI can express "8 years of Python, 1 of Go", and recomputing
    # would flatten it back to the headline number on the next save.
    existing_years_by_skill = existing_screening.get("years_experience_by_skill", {}) or {}
    years_by_skill = {}
    for skill in skills[:12]:
        key = skill.lower().replace(" ", "_")
        years_by_skill[key] = existing_years_by_skill.get(key, years)

    outreach = dict(existing.get("outreach") or {})
    outreach.setdefault("tone", "Professional and direct")
    outreach.setdefault(
        "call_to_action", "Would love to connect and discuss the role with your team."
    )
    # The pitch tracks the summary only while it is still the summary. Once it
    # has been edited into something of its own, a summary change stops
    # overwriting it.
    if not outreach.get("elevator_pitch") or outreach.get("elevator_pitch") == str(
        existing.get("professional_summary") or ""
    ):
        outreach["elevator_pitch"] = summary

    profile = {
        "personal_info": {
            "first_name": str(data.get("first_name") or "").strip(),
            "last_name": str(data.get("last_name") or "").strip(),
            "email": str(data.get("email") or "").strip(),
            "phone_country_code": str(data.get("phone_country_code") or "+91").strip(),
            "phone_number": str(data.get("phone_number") or "").strip(),
            "city": str(data.get("city") or "").strip(),
            "state": str(data.get("state") or "").strip(),
            "country": country or "India",
            # No wizard field for these three, so they fall back to what is
            # already recorded rather than being written away as empty.
            "postal_code": str(
                data.get("postal_code") or existing_personal.get("postal_code") or ""
            ).strip(),
            "linkedin_url": str(data.get("linkedin_url") or "").strip(),
            "github_url": str(
                data.get("github_url") or existing_personal.get("github_url") or ""
            ).strip(),
            "portfolio_url": str(
                data.get("portfolio_url") or existing_personal.get("portfolio_url") or ""
            ).strip(),
        },
        "resume_path": str(data.get("resume_path") or existing.get("resume_path") or "").strip(),
        "professional_summary": summary,
        "skills": _merge_skills(skills, existing.get("skills", {}) or {}),
        "experience": experience,
        "education": education,
        "screening_answers": {
            "total_years_experience": years,
            "years_experience_by_skill": years_by_skill,
            # Rebuilt from scratch every time, never merged. Every country is
            # written explicitly, so a merge could only ever resurrect a stale
            # `legally_authorized_in_*: true` from a country the candidate has
            # since corrected -- which submits a false answer and gets the
            # application binned without a word.
            "work_authorization": _work_authorization(
                country, bool(data.get("requires_sponsorship"))
            ),
            "preferences": preferences,
            "demographics": {
                "gender": str(
                    data.get("gender") or existing_demographics.get("gender") or "Prefer not to say"
                ),
                "veteran_status": str(
                    data.get("veteran_status") or existing_demographics.get("veteran_status") or "No"
                ),
                "disability_status": str(
                    data.get("disability_status")
                    or existing_demographics.get("disability_status")
                    or "No"
                ),
            },
        },
        "outreach": outreach,
    }
    return profile


def build_config_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """The subset of config.yaml the wizard is allowed to set."""
    titles = _clean_list(data.get("job_titles"))
    locations = _clean_list(data.get("locations")) or ["Remote"]
    return {
        "search": {
            "keywords": titles,
            "locations": locations,
            "min_match_score": int(data.get("min_match_score") or 75),
        },
        "naukri": {
            "keywords": titles,
            "locations": locations,
            "experience_years": int(data.get("total_years_experience") or 0),
            "min_match_score": int(data.get("min_match_score") or 75),
        },
        "limits": {
            "max_applications_per_day": int(data.get("max_applications_per_day") or 20),
        },
        "llm": {"provider": "proxy"},
    }


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def save(data: Dict[str, Any]) -> Dict[str, str]:
    """
    Writes profile.yaml and config.yaml into the user's config directory.

    Both files are updated in place rather than regenerated: the wizard sets a
    small subset of each, and everything else it has no field for -- education
    and past employers in the profile, pacing and pre-filter thresholds in the
    config -- has to survive a re-save from the Settings screen.
    """
    paths.ensure_dirs()

    profile = build_profile(data, _read_profile())
    # The wizard's picker hands back wherever the file was chosen from, so the
    # copy happens here too rather than only on the Settings path.
    profile["resume_path"] = _adopt_resume(profile.get("resume_path"))

    # Read the CV the customer just chose. This is the first-run path, so it is
    # the one that decides whether their profile has a career history at all --
    # the wizard collects a summary and a current employer, but has no field for
    # bullet points, and a profile without those renders a blank resume.
    # Best-effort by design: a CV that cannot be parsed must not block setup.
    if profile.get("resume_path"):
        try:
            from src.core.config import load_app_config
            from src.llm.client import LLMClient
            from src.modules import profile_import

            extracted = profile_import.extract_profile(
                profile["resume_path"], LLMClient(load_app_config().llm)
            )
            profile = profile_import.merge_into_profile(extracted, profile)
        except Exception as exc:
            from src.core.logger import logger

            logger.warning(
                f"[Onboarding] Could not read the CV into the profile: {exc} "
                "Setup will continue; re-import from Settings once resolved."
            )

    profile_path = paths.profile_path()
    profile_path.write_text(
        yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )

    # The user's own config comes first and the bundled copy is the first-run
    # fallback. Starting from the bundle every time would roll back anything
    # tuned since setup on each trip through Settings.
    base: Dict[str, Any] = {}
    for candidate in (paths.app_config_path(), paths.bundled_dir() / "config" / "config.yaml"):
        if candidate.exists():
            try:
                base = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                break
            except Exception:
                continue
    merged = _deep_merge(base, build_config_overrides(data))
    merged.setdefault("general", {})["setup_completed_at"] = dt.datetime.now().isoformat(timespec="seconds")

    config_path = paths.app_config_path()
    config_path.write_text(
        yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )

    return {"profile": str(profile_path), "config": str(config_path)}


def set_base_resume(source: str, import_content: bool = True) -> Dict[str, str]:
    """
    Adopt a new master resume, and build the profile's career sections from it.

    `resume_path` used to be the only thing this wrote -- the CV was copied to
    disk and never opened. Since the wizard has no field for job bullet points
    (see `_build_profile` below), that left `experience[].highlights` empty for
    every customer, and the generated resume rendered an empty Professional
    Experience section. Reading the document the customer just handed us is the
    fix.

    The import is best-effort: a scanned or unreadable PDF still gets adopted as
    the base resume, and the caller is told what went wrong so it can be shown
    rather than swallowed.
    """
    stored = _adopt_resume(source, strict=True)

    profile = _read_profile()
    profile["resume_path"] = stored

    imported: Dict[str, Any] = {}
    import_error = ""
    if import_content:
        try:
            from src.core.config import load_app_config
            from src.llm.client import LLMClient
            from src.modules import profile_import

            extracted = profile_import.extract_profile(stored, LLMClient(load_app_config().llm))
            profile = profile_import.merge_into_profile(extracted, profile)
            imported = extracted
        except Exception as exc:
            # Surfaced, never silent: a profile that stayed empty is exactly the
            # failure this function exists to prevent, and the customer can act
            # on "your CV could not be read" in a way they cannot act on a
            # resume that is quietly blank.
            import_error = str(exc)

    paths.ensure_dirs()
    paths.profile_path().write_text(
        yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )

    result: Dict[str, Any] = {"path": stored, "name": Path(stored).name}
    if imported:
        roles = imported.get("experience") or []
        result["imported"] = {
            "roles": len(roles),
            "bullets": sum(len(r.get("highlights") or []) for r in roles),
            "skills": sum(len(v) for v in (imported.get("skills") or {}).values()),
            "education": len(imported.get("education") or []),
        }
    if import_error:
        result["import_error"] = import_error
    return result


def load_existing() -> Dict[str, Any]:
    """
    Re-hydrates the wizard from a saved profile, for the Settings screen.

    Returns {} on a first run or if the file is unreadable -- a corrupt profile
    should reopen the wizard, not crash the app on launch.
    """
    profile = _read_profile()
    if not profile:
        return {}

    personal = profile.get("personal_info", {}) or {}
    screening = profile.get("screening_answers", {}) or {}
    prefs = screening.get("preferences", {}) or {}
    auth = screening.get("work_authorization", {}) or {}
    skills = profile.get("skills", {}) or {}
    experience = (profile.get("experience") or [{}])[0]

    config: Dict[str, Any] = {}
    config_path = paths.app_config_path()
    if config_path.exists():
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            config = {}
    search = config.get("search", {}) or {}

    # De-duplicated across buckets: a skill filed under both "languages" and
    # "databases" -- SQL, typically -- would otherwise put two identical chips in
    # the tag box, and the reader cannot tell which of them a delete removes.
    all_skills: List[str] = []
    seen_skills: set = set()
    for bucket in SKILL_BUCKETS:
        for skill in skills.get(bucket) or []:
            key = str(skill).casefold()
            if key not in seen_skills:
                seen_skills.add(key)
                all_skills.append(skill)

    return {
        **{k: personal.get(k, "") for k in (
            "first_name", "last_name", "email", "phone_country_code", "phone_number",
            "city", "state", "country", "postal_code", "linkedin_url",
        )},
        "job_titles": search.get("keywords") or [],
        "locations": search.get("locations") or [],
        "min_match_score": search.get("min_match_score", 75),
        "total_years_experience": screening.get("total_years_experience", 0),
        "skills": all_skills,
        "professional_summary": profile.get("professional_summary", ""),
        "resume_path": profile.get("resume_path", ""),
        "current_role": experience.get("role", ""),
        "current_company": experience.get("company", ""),
        "work_country": auth.get("citizenship") or personal.get("country", ""),
        "requires_sponsorship": bool(auth.get("requires_sponsorship_now_or_future")),
        "notice_period_days": prefs.get("notice_period_days", 30),
        "expected_ctc_lakhs": prefs.get("desired_salary_inr_lakhs") or "",
        "minimum_ctc_lakhs": prefs.get("minimum_salary_inr_lakhs") or "",
        "willing_to_relocate": prefs.get("willing_to_relocate", True),
        "can_work_remotely": prefs.get("can_work_remotely", True),
        "max_applications_per_day": (config.get("limits", {}) or {}).get("max_applications_per_day", 20),
    }
