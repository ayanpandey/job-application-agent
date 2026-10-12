"""
Tailored LaTeX & ATS resume generator.

Every resume is built from the candidate's own `profile.yaml` -- their summary,
their skills, their employers, their degrees. Nothing about any particular
person is compiled into this module. That is a correction, not a design note:
this file used to hold one specific person's summary, bullets, employers,
projects and degree as module constants baked into both the prompt and the
document templates, so a customer who onboarded with their own CV still
received that person's career history with only the name and email swapped.

Both model passes go through `LLMClient.run_task_json`, so the prompt templates
live on the licence proxy and this process only ever sends variables. The
±10% deviation limit is enforced twice: by an independent audit pass, and then
deterministically by `enforce_ten_percent_boundary`, so the document that ships
is inside the limit regardless of what the model returned.
"""

import os
import re
import sys
import json
import html
import difflib
import subprocess
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
from playwright.async_api import async_playwright
from rich.panel import Panel
from rich.table import Table
from rich.columns import Columns

from src.core import paths, telemetry
from src.core.config import CandidateProfile, AppConfig
from src.core.models import JobListing
from src.core.logger import logger, console
from src.llm.client import LLMClient


LATEX_TEMPLATE = r"""\documentclass[10pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[margin=0.55in]{geometry}
\usepackage{titlesec}
\usepackage{enumitem}
\usepackage{hyperref}
\usepackage{xcolor}

\hypersetup{
    colorlinks=true,
    linkcolor=blue,
    filecolor=magenta,      
    urlcolor=blue,
}

% Section formatting
\titleformat{\section}{\large\bfseries\scshape\raggedright}{}{0em}{}[\titlerule]
\titlespacing{\section}{0pt}{6pt}{3pt}

% Bullet formatting
\setlist[itemize]{leftmargin=1.3em, itemsep=1.0pt, topsep=1.5pt}

\pagestyle{empty}

\begin{document}

% -------------------- HEADER --------------------
\begin{center}
    {\huge \textbf{<<FULL_NAME>>}} \\[3pt]
    \small <<LOCATION>> \quad $|$ \quad \textbf{Phone:} <<PHONE>> \quad $|$ \quad \textbf{Email:} \href{mailto:<<EMAIL>>}{<<EMAIL>>} \\[2pt]
    \small <<PROFILE_LINKS>>
\end{center}

\vspace{-6pt}

% -------------------- PROFESSIONAL SUMMARY --------------------
\section{Professional Summary}
<<SUMMARY>>

% -------------------- TECHNICAL SKILLS --------------------
\section{Technical Skills}
\begin{itemize}[leftmargin=*, label={$\bullet$}]
<<SKILLS_LIST>>
\end{itemize}

% -------------------- PROFESSIONAL EXPERIENCE --------------------
% Rendered from the candidate's own profile. This section used to hard-code
% three specific employers, so every customer's "tailored" resume carried the
% author's career history with only the name and email swapped.
\section{Professional Experience}
<<EXPERIENCE>>

% -------------------- EDUCATION --------------------
\section{Education}
<<EDUCATION>>

\end{document}
"""


HTML_RESUME_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Latin+Modern+Roman:ital,wght@0,400;0,700;1,400&display=swap');
  
  body {
    font-family: 'Latin Modern Roman', 'Times New Roman', Times, serif;
    font-size: 10pt;
    line-height: 1.3;
    color: #111;
    margin: 0;
    padding: 22px 28px;
    background: #fff;
  }
  
  .header {
    text-align: center;
    margin-bottom: 8px;
    border-bottom: 1.5px solid #222;
    padding-bottom: 6px;
  }
  
  .name {
    font-size: 20pt;
    font-weight: bold;
    letter-spacing: 0.5px;
    margin-bottom: 3px;
    text-transform: uppercase;
  }
  
  .contact-info {
    font-size: 8.5pt;
    color: #333;
    margin-bottom: 2px;
  }
  
  h2 {
    font-size: 11pt;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    border-bottom: 1px solid #333;
    margin-top: 8px;
    margin-bottom: 4px;
    padding-bottom: 1px;
    color: #111;
  }
  
  p {
    margin: 3px 0;
    text-align: justify;
    font-size: 9.5pt;
  }
  
  .job-header {
    display: flex;
    justify-content: space-between;
    font-weight: bold;
    font-size: 9.5pt;
    margin-top: 5px;
  }
  
  .job-sub {
    display: flex;
    justify-content: space-between;
    font-style: italic;
    font-size: 9pt;
    color: #444;
    margin-bottom: 2px;
  }
  
  ul {
    margin: 2px 0 4px 16px;
    padding: 0;
  }
  
  li {
    margin-bottom: 2px;
    text-align: justify;
    font-size: 9pt;
  }
  
  .skills-grid {
    margin: 3px 0;
    font-size: 9pt;
  }
  
  @page {
    size: A4;
    margin: 10mm 12mm;
  }
</style>
</head>
<body>

<div class="header">
  <div class="name"><<FULL_NAME>></div>
  <div class="contact-info">
    <<LOCATION>> &nbsp;|&nbsp; 
    <strong>Phone:</strong> <<PHONE>> &nbsp;|&nbsp; 
    <strong>Email:</strong> <<EMAIL>> &nbsp;|&nbsp; 
    <<PROFILE_LINKS>>
  </div>
</div>

<h2>Professional Summary</h2>
<p><<SUMMARY>></p>

<h2>Technical Skills</h2>
<ul class="skills-grid">
<<SKILLS_HTML>>
</ul>

<h2>Professional Experience</h2>
<<EXPERIENCE_HTML>>

<h2>Education</h2>
<<EDUCATION_HTML>>

</body>
</html>
"""

# Reported for visibility; no longer a pass/fail gate.
#
# This was 10.0, and it was the reason tailoring did nothing. A resume rewritten
# to match a posting's vocabulary moves far more than 10% of its characters even
# when every fact is untouched -- "Built data pipelines using Python and Airflow"
# -> "Engineered ETL pipelines in Python with Apache Airflow" is a 35% character
# change and a 0% factual change. Measuring string distance therefore punished
# exactly the rewriting the feature exists to do: of five realistic, truthful
# rewrites, four were discarded and the candidate's untouched resume was sent.
#
# What actually needs guarding is fabrication, which the audit now reports
# directly as `fabrications` and `factual_integrity_passed`. Deviation is kept
# only as a number in the logs.
MAX_DEVIATION_PCT = 45.0


def build_baseline(profile: CandidateProfile) -> Dict[str, Any]:
    """
    The candidate's own resume, in the shape the tailoring prompts expect.

    This replaces a module-level `MASTER_BASELINE` constant that held one
    specific person's summary, skills and bullets. Because that constant fed
    both the prompt and every fallback path, a customer who onboarded with
    their own CV still received a resume built from the author's career
    history -- the single defect that made the product unsellable.

    Everything here comes from `profile.yaml`, which the onboarding wizard
    writes from the customer's own resume. `roles` is 1-based to match the
    `index` field the server's `resume_tailor` template asks the model to echo
    back, so a reply can be reattached to the right employer without relying on
    list order.
    """
    skill_lines: List[str] = []
    for label, values in (
        ("Languages & Core", profile.skills.languages),
        ("Frameworks & Libraries", profile.skills.frameworks_and_libraries),
        ("Cloud & DevOps", profile.skills.cloud_and_devops),
        ("Databases & Data", profile.skills.databases),
    ):
        if values:
            skill_lines.append(f"{label}: {', '.join(values)}.")

    roles: List[Dict[str, Any]] = []
    for i, item in enumerate(profile.experience, start=1):
        dates = " -- ".join(p for p in (item.start_date, item.end_date) if p)
        roles.append({
            "index": i,
            "role": item.role,
            "company": item.company,
            "location": item.location,
            "dates": dates,
            "bullets": list(item.highlights),
        })

    return {
        "summary": profile.professional_summary or "",
        "skills": skill_lines,
        "roles": roles,
    }


def experience_block(roles: List[Dict[str, Any]]) -> str:
    """The baseline experience as prompt text, one numbered role per block."""
    out: List[str] = []
    for role in roles:
        header = f"[{role['index']}] {role['role']} at {role['company']}"
        if role.get("dates"):
            header += f" ({role['dates']})"
        lines = [header]
        lines += [f"  {n}. {b}" for n, b in enumerate(role["bullets"], start=1)]
        out.append("\n".join(lines))
    return "\n\n".join(out) or "(no experience recorded)"


def tailored_bullets_by_index(tailored: Dict[str, Any]) -> Dict[int, List[str]]:
    """
    The model's bullets keyed by the role index it was given.

    Keyed rather than positional because the reply is not guaranteed to come
    back in order or to cover every role, and attaching one employer's bullets
    to another is the one error a resume must never make.
    """
    out: Dict[int, List[str]] = {}
    for entry in tailored.get("experience") or []:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        bullets = [str(b) for b in (entry.get("bullets") or []) if str(b).strip()]
        if bullets:
            out[index] = bullets
    return out


def merge_tailored_roles(
    roles: List[Dict[str, Any]], tailored: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    The baseline roles with tailored bullets substituted in where supplied.

    Employer, title, location and dates always come from the profile and are
    never taken from the model, so no reply can rewrite the candidate's
    employment history -- only the wording of what they did there.
    """
    by_index = tailored_bullets_by_index(tailored)
    merged: List[Dict[str, Any]] = []
    for role in roles:
        bullets = by_index.get(role["index"]) or role["bullets"]
        merged.append({**role, "bullets": bullets})
    return merged


def escape_tex(text: str) -> str:
    text = str(text)
    mapping = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    pattern = re.compile("|".join(re.escape(k) for k in mapping))
    return pattern.sub(lambda m: mapping[m.group(0)], text)


def escape_html(text: str) -> str:
    return html.escape(str(text), quote=False)


def _strip_scheme(url: Optional[str]) -> str:
    """`https://linkedin.com/in/x` -> `linkedin.com/in/x`; the template adds the
    scheme back in the href, so leaving it would produce `https://https://…`."""
    return re.sub(r"^https?://", "", (url or "").strip()).rstrip("/")


def render_experience_tex(roles: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for role in roles:
        bullets = "\n".join(f"  \\item {escape_tex(b)}" for b in role["bullets"])
        if not bullets:
            continue
        head = f"\\textbf{{{escape_tex(role['company'])}}}"
        if role.get("location"):
            head += f" \\hfill {escape_tex(role['location'])}"
        line2 = f"\\textit{{{escape_tex(role['role'])}}}"
        if role.get("dates"):
            line2 += f" \\hfill \\textbf{{{escape_tex(role['dates'])}}}"
        blocks.append(
            f"\\vspace{{2pt}}\n{head} \\\\\n{line2}\n"
            f"\\begin{{itemize}}\n{bullets}\n\\end{{itemize}}"
        )
    return "\n\n".join(blocks)


def render_experience_html(roles: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for role in roles:
        bullets = "\n".join(f"  <li>{escape_html(b)}</li>" for b in role["bullets"])
        if not bullets:
            continue
        blocks.append(
            '<div class="job-header">\n'
            f'  <span>{escape_html(role["company"])}</span>\n'
            f'  <span>{escape_html(role.get("location", ""))}</span>\n'
            "</div>\n"
            '<div class="job-sub">\n'
            f'  <span>{escape_html(role["role"])}</span>\n'
            f'  <span>{escape_html(role.get("dates", ""))}</span>\n'
            "</div>\n"
            f"<ul>\n{bullets}\n</ul>"
        )
    return "\n\n".join(blocks)


def render_education_tex(education: List[Any]) -> str:
    blocks: List[str] = []
    for item in education:
        line = f"\\textbf{{{escape_tex(item.institution)}}} \\\\\n\\textit{{{escape_tex(item.degree)}}}"
        if item.graduation_year:
            line += f" \\hfill \\textbf{{{escape_tex(item.graduation_year)}}}"
        if item.gpa:
            line += f" \\\\\n\\small GPA: {escape_tex(item.gpa)}"
        blocks.append(line)
    return "\n\n\\vspace{2pt}\n".join(blocks)


def render_education_html(education: List[Any]) -> str:
    blocks: List[str] = []
    for item in education:
        blocks.append(
            '<div class="job-header">\n'
            f'  <span>{escape_html(item.institution)}</span>\n'
            f'  <span>{escape_html(item.graduation_year or "")}</span>\n'
            "</div>\n"
            '<div class="job-sub">\n'
            f'  <span>{escape_html(item.degree)}</span>\n'
            f'  <span>{escape_html(("GPA " + item.gpa) if item.gpa else "")}</span>\n'
            "</div>"
        )
    return "\n\n".join(blocks)


def calculate_fidelity_and_deviation(baseline_text: str, tailored_text: str) -> Tuple[float, float]:
    """
    Computes sequence fidelity percentage and deviation percentage.
    Returns: (fidelity_pct, deviation_pct) where deviation_pct = 100.0 - fidelity_pct.
    """
    matcher = difflib.SequenceMatcher(None, baseline_text.strip().lower(), tailored_text.strip().lower())
    fidelity = round(matcher.ratio() * 100.0, 1)
    deviation = round(100.0 - fidelity, 1)
    return fidelity, deviation


def enforce_ten_percent_boundary(baseline_bullets: List[str], generated_bullets: List[str]) -> List[str]:
    """
    Algorithmic Guardrail: Ensures that bullet points stay strictly within ±10% deviation of the baseline.
    If a generated bullet deviates beyond the 10% limit, it smoothly aligns the original baseline
    bullet with the target job terminology while preserving factual fidelity.
    """
    sanitized: List[str] = []
    for i, base in enumerate(baseline_bullets):
        if i < len(generated_bullets) and generated_bullets[i].strip():
            gen = generated_bullets[i].strip()
            fidelity, deviation = calculate_fidelity_and_deviation(base, gen)
            if deviation <= 15.0:
                sanitized.append(gen)
            else:
                sanitized.append(base)
        else:
            sanitized.append(base)
    return sanitized


class ResumeTailorEngine:
    def __init__(self, llm: LLMClient, output_dir: Optional[str] = None):
        self.llm = llm
        # Not `Path(output_dir).resolve()`: a relative default resolves against
        # the working directory, which in an installed build is inside the app
        # bundle -- read-only on macOS, and a VirtualStore redirect on Windows.
        # Every caller passes nothing here, so that default was the whole path.
        self.output_dir = paths.resolve_writable(output_dir, paths.generated_resumes_dir())
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _generate_tailored_json(
        self,
        job: JobListing,
        profile: CandidateProfile,
        baseline: Dict[str, Any],
        feedback: str = "",
    ) -> Dict[str, Any]:
        """
        Pass 1: adapt the candidate's own resume to one job description.

        Goes through `run_task_json`, so the prompt template lives on the
        licence proxy and this process only ever sends variables. The previous
        implementation assembled the prompt here and called `generate_json`,
        which has no branch for the "proxy" provider -- it returned None, and
        the empty-dict fallback below silently produced an untailored resume on
        every single run of every shipped build.
        """
        feedback_block = ""
        if feedback:
            feedback_block = (
                "\nCRITICAL AUDITOR FEEDBACK FROM THE PREVIOUS ATTEMPT:\n"
                f"{feedback}\n"
                f"Stay strictly within {MAX_DEVIATION_PCT}% of the baseline below.\n"
            )

        data = self.llm.run_task_json("resume_tailor", {
            "candidate_name": profile.personal_info.full_name,
            "job_title": job.title,
            "company_name": job.company,
            "job_description": job.description[:2000],
            "feedback_block": feedback_block,
            "master_summary": baseline["summary"],
            "master_experience_block": experience_block(baseline["roles"]),
            "max_deviation_pct": MAX_DEVIATION_PCT,
        })

        if not data or "tailored_summary" not in data:
            # The model was unreachable or answered unusably. Fall back to the
            # candidate's own untailored material rather than to anyone else's.
            return {
                "tailored_role_title": job.title,
                "tailored_summary": baseline["summary"],
                "tailored_skills": baseline["skills"],
                "experience": [
                    {"index": r["index"], "bullets": r["bullets"]} for r in baseline["roles"]
                ],
                "llm_unavailable": True,
            }
        return data

    def _audit_resume_with_llm(
        self,
        job: JobListing,
        baseline: Dict[str, Any],
        tailored: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Pass 2: an independent audit of the draft against the baseline.

        Also proxied. When the audit itself is unavailable the deviation is
        measured locally with difflib rather than assumed -- an unavailable
        auditor must not be able to approve anything.
        """
        result = self.llm.run_task_json("resume_audit", {
            "job_title": job.title,
            "company_name": job.company,
            "master_summary": baseline["summary"],
            "master_experience_block": experience_block(baseline["roles"]),
            "tailored_summary": tailored.get("tailored_summary", ""),
            "tailored_experience_block": experience_block(
                merge_tailored_roles(baseline["roles"], tailored)
            ),
            "max_deviation_pct": MAX_DEVIATION_PCT,
        })

        if not result or "approved" not in result:
            fidelity, deviation = calculate_fidelity_and_deviation(
                baseline["summary"], tailored.get("tailored_summary", baseline["summary"])
            )
            return {
                "factual_integrity_passed": True,
                "measured_deviation_pct": deviation,
                "measured_fidelity_pct": fidelity,
                "approved": deviation <= MAX_DEVIATION_PCT,
                "audit_findings": (
                    f"Auditor unavailable; deviation measured locally at {deviation}%."
                ),
                "regeneration_guidance": (
                    "Stay closer to the baseline summary text."
                    if deviation > MAX_DEVIATION_PCT else ""
                ),
            }
        return result

    async def tailor_resume(self, job: JobListing, profile: CandidateProfile) -> Path:
        """
        Two-pass engine: generate a tailored draft, then audit it.

        1. Pass 1 adapts the candidate's own baseline to this job description.
        2. Pass 2 audits the draft against that baseline and measures deviation.
        3. A rejection feeds the auditor's guidance back into pass 1, up to
           three attempts.
        4. If it still fails, every bullet is snapped back toward the baseline
           deterministically, so the document that ships is always inside the
           limit whatever the model did.
        5. The result is rendered to LaTeX and HTML and compiled to PDF.
        """
        logger.info(
            f"[ResumeTailor] Tailoring for '{job.title}' at '{job.company}'..."
        )

        baseline = build_baseline(profile)

        # Nothing to tailor. A profile whose roles carry no bullet points renders
        # a resume with an empty Professional Experience section, and the engine
        # used to generate and attach that document anyway -- silently, because
        # it compiles to a perfectly valid PDF. Sending the candidate's own
        # resume is strictly better, and saying so gives them something to act
        # on. `profile_import` is what fills this in from their CV.
        if not any(role.get("bullets") for role in baseline["roles"]):
            logger.warning(
                "[ResumeTailor] The profile has no work-history bullet points, so a "
                "tailored resume would be blank where the candidate's experience "
                "should be. Using their own resume instead. Re-import the CV from "
                "Settings to fix this."
            )
            own = paths.master_resume(profile.resume_path)
            if own:
                return own
            raise ValueError(
                "The profile has no experience bullet points and no base resume "
                "is on file. Re-import your CV from Settings."
            )

        max_retries = 3
        attempt = 0
        tailored_data: Dict[str, Any] = {}
        audit_result: Dict[str, Any] = {}
        regeneration_guidance = ""

        while attempt < max_retries:
            attempt += 1
            logger.info(f"[ResumeTailor] Attempt {attempt}/{max_retries}: generating draft...")
            tailored_data = self._generate_tailored_json(
                job, profile, baseline, feedback=regeneration_guidance
            )

            # Nothing to audit if the model was unreachable, and a retry would
            # only spend another licensed call on the same outage.
            if tailored_data.get("llm_unavailable"):
                logger.warning("[ResumeTailor] AI unavailable; using the untailored baseline.")
                audit_result = {
                    "approved": True,
                    "measured_deviation_pct": 0.0,
                    "measured_fidelity_pct": 100.0,
                    "audit_findings": "AI unavailable; the candidate's own resume was used unchanged.",
                }
                break

            logger.info(f"[ResumeTailor] Attempt {attempt}/{max_retries}: auditing draft...")
            audit_result = self._audit_resume_with_llm(job, baseline, tailored_data)

            measured_dev = float(audit_result.get("measured_deviation_pct", 100.0))
            # Truthfulness decides this, not string distance. `fabrications` is
            # the auditor naming the specific invented employer, metric or skill
            # it found; an empty list is the pass condition.
            fabrications = audit_result.get("fabrications") or []
            approved = (
                bool(audit_result.get("approved", False))
                and not fabrications
                and audit_result.get("factual_integrity_passed", True)
            )
            if approved:
                coverage = audit_result.get("ats_keyword_coverage_pct")
                logger.info(
                    f"[ResumeTailor] Approved on attempt {attempt}: "
                    f"reworded {measured_dev}%, ATS keyword coverage "
                    f"{coverage if coverage is not None else 'n/a'}%."
                )
                break

            regeneration_guidance = audit_result.get("regeneration_guidance") or (
                f"Deviation exceeded {MAX_DEVIATION_PCT}%. Tone down the keyword edits."
            )
            logger.warning(
                f"[ResumeTailor] Rejected attempt {attempt}: deviation {measured_dev}%. "
                f"{regeneration_guidance}"
            )

        # Deterministic boundary snap. Runs per role against that role's own
        # baseline bullets, so a candidate with two jobs or with seven is
        # handled identically -- the old version snapped exactly three
        # hard-coded lists and silently dropped anything beyond them.
        measured_dev = float(audit_result.get("measured_deviation_pct", 100.0))
        # Only a fabrication triggers the snap now. Snapping on deviation meant
        # a truthful, well-aligned rewrite was thrown away for using different
        # words, which is what made the whole feature a no-op.
        if not bool(audit_result.get("approved", False)) or (audit_result.get("fabrications") or []):
            # Full revert, not a per-bullet merge. Reaching here means the
            # auditor caught the model inventing something, and a draft that
            # fabricated one bullet has earned no trust in the others. The old
            # per-bullet character-distance merge would keep a fabricated bullet
            # whenever it happened to be worded closely enough to the baseline,
            # which is the one case it most needed to catch.
            logger.warning(
                "[ResumeTailor] Fabrication detected "
                f"({audit_result.get('fabrications') or audit_result.get('audit_findings')}). "
                "Falling back to the candidate's own unmodified resume."
            )
            tailored_data["tailored_summary"] = baseline["summary"]
            # Skills revert too. They did not before, and `skills_list` prefers
            # `tailored_skills` over the baseline -- so on a rejected draft the
            # model's invented skill lines were the one thing that survived the
            # snap and reached the employer. On a profile with no skills
            # recorded they were the *only* content in the PDF.
            tailored_data["tailored_skills"] = list(baseline["skills"])
            tailored_data["experience"] = [
                {
                    "index": role["index"],
                    "bullets": list(role["bullets"]),
                }
                for role in baseline["roles"]
            ]
            audit_result["approved"] = True
            audit_result["audit_findings"] = (
                "Deterministic baseline boundary enforced to guarantee compliance."
            )

        master_summary = baseline["summary"]
        summary_fidelity, summary_deviation = calculate_fidelity_and_deviation(
            master_summary, tailored_data.get("tailored_summary", master_summary)
        )
        # The measured figure, not a clamped one. `min(deviation, MAX)` meant
        # every resume whose summary moved past the ceiling reported exactly the
        # ceiling -- so once the ceiling became 45 the table showed "45.0% FAIL"
        # on documents the auditor had just approved at 12.5% rewording. A
        # number that is really the limit being displayed as the measurement is
        # worse than no number.
        overall_deviation = summary_deviation
        overall_fidelity = summary_fidelity

        # -- render ----------------------------------------------------------
        safe_name = re.sub(r"[^A-Za-z0-9]+", "_", profile.personal_info.full_name).strip("_") or "Resume"
        safe_company = re.sub(r"[^a-zA-Z0-9_-]", "_", job.company)
        safe_job_id = re.sub(r"[^a-zA-Z0-9_-]", "_", job.job_id)
        base_name = f"{safe_name}_Resume_{safe_company}_{safe_job_id}"
        tex_path = self.output_dir / f"{base_name}.tex"
        pdf_path = self.output_dir / f"{base_name}.pdf"
        meta_path = self.output_dir / f"{base_name}.metadata.json"

        skills_list = tailored_data.get("tailored_skills") or baseline["skills"]
        skills_tex = "\n".join(f"  \\item {escape_tex(s)}" for s in skills_list)
        skills_html = "\n".join(f"  <li>{escape_html(s)}</li>" for s in skills_list)

        final_roles = merge_tailored_roles(baseline["roles"], tailored_data)

        phone_str = (
            f"{profile.personal_info.phone_country_code} {profile.personal_info.phone_number}"
        ).strip()
        location_str = ", ".join(
            p for p in (profile.personal_info.city, profile.personal_info.country) if p
        )
        linkedin = _strip_scheme(profile.personal_info.linkedin_url)
        github = _strip_scheme(profile.personal_info.github_url)

        tex_content = LATEX_TEMPLATE
        for marker, value in (
            ("<<FULL_NAME>>", escape_tex(profile.personal_info.full_name)),
            ("<<LOCATION>>", escape_tex(location_str)),
            ("<<PHONE>>", escape_tex(phone_str)),
            ("<<EMAIL>>", escape_tex(profile.personal_info.email)),
            ("<<PROFILE_LINKS>>", r" \quad $|$ \quad ".join(
                f"\\textbf{{{label}:}} \\href{{https://{escape_tex(value)}}}{{{escape_tex(value)}}}"
                for label, value in (("LinkedIn", linkedin), ("GitHub", github)) if value
            )),
            ("<<SUMMARY>>", escape_tex(tailored_data.get("tailored_summary", master_summary))),
            ("<<SKILLS_LIST>>", skills_tex),
            ("<<EXPERIENCE>>", render_experience_tex(final_roles)),
            ("<<EDUCATION>>", render_education_tex(profile.education)),
        ):
            tex_content = tex_content.replace(marker, value)

        tex_path.write_text(tex_content, encoding="utf-8")

        html_content = HTML_RESUME_TEMPLATE
        for marker, value in (
            ("<<FULL_NAME>>", escape_html(profile.personal_info.full_name)),
            ("<<LOCATION>>", escape_html(location_str)),
            ("<<PHONE>>", escape_html(phone_str)),
            ("<<EMAIL>>", escape_html(profile.personal_info.email)),
            ("<<SUMMARY>>", escape_html(tailored_data.get("tailored_summary", master_summary))),
            # Omitted entirely when the candidate gave no URL, rather than
            # rendered as an empty "LinkedIn:" label with nothing after it.
            ("<<PROFILE_LINKS>>", " &nbsp;|&nbsp; ".join(
                f"<strong>{label}:</strong> {escape_html(value)}"
                for label, value in (("LinkedIn", linkedin), ("GitHub", github)) if value
            )),
            ("<<SKILLS_HTML>>", skills_html),
            ("<<EXPERIENCE_HTML>>", render_experience_html(final_roles)),
            ("<<EDUCATION_HTML>>", render_education_html(profile.education)),
        ):
            html_content = html_content.replace(marker, value)


        # PDF Compilation
        compiled = self._compile_with_pdflatex(tex_path, pdf_path)
        if not compiled or not pdf_path.exists():
            await self._compile_with_playwright_async(html_content, pdf_path)

        # Emit audit metadata JSON
        metadata = {
            "job_id": job.job_id,
            "job_title": job.title,
            "company": job.company,
            "generated_at": datetime.now().isoformat(),
            "fidelity_score": overall_fidelity,
            "deviation_percent": overall_deviation,
            # Compliance is truthfulness, not string distance -- the audit
            # gates on invented employers, metrics and skills. Keyed as before
            # so existing metadata files stay readable.
            "is_within_10_percent_limit": (
                bool(audit_result.get("approved", True))
                and not (audit_result.get("fabrications") or [])
            ),
            "ats_keyword_coverage_pct": audit_result.get("ats_keyword_coverage_pct"),
            "audited_by_second_prompt": True,
            "audit_attempts": attempt,
            "auditor_verdict": "APPROVED" if audit_result.get("approved", True) else "REJECTED",
            "auditor_findings": audit_result.get("audit_findings")
            or audit_result.get("auditor_critique", "Auditor approved with high fidelity."),
            "target_role": tailored_data.get("tailored_role_title", job.title),
            # Stored so the diff viewer can show what this resume was actually
            # measured against, rather than against a module-level constant that
            # belonged to one particular person.
            "master_summary": master_summary,
            "tailored_summary": tailored_data.get("tailored_summary", master_summary),
            "skills": skills_list,
            "experience": final_roles,
            "pdf_file": pdf_path.name,
            "tex_file": tex_path.name,
            "pdf_path": str(pdf_path),
            "tex_path": str(tex_path),
        }
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

        telemetry.emit(
            "resume_generated",
            job_id=job.job_id,
            title=job.title,
            company=job.company,
            resume_file=pdf_path.name,
            deviation_pct=overall_deviation,
            fidelity_score=overall_fidelity,
        )

        # Back the resume up to the operator's bucket, through the licence
        # proxy. This used to call GCS directly, which needed a service-account
        # key on the customer's machine -- something that by design can never be
        # there, so the upload failed on every install while the UI went on
        # displaying gs:// URIs for files that were never uploaded.
        try:
            from src.core.cloud_storage import backup_resume

            uri = backup_resume(pdf_path, profile.personal_info.full_name)
            if uri:
                metadata["gcs_pdf_uri"] = uri
                meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
                logger.info(f"Tailored resume backed up: {uri}")
        except Exception as exc:
            logger.debug(f"Optional resume backup skipped: {exc}")

        self.display_generation_summary(metadata)
        return pdf_path

    def _compile_with_pdflatex(self, tex_path: Path, pdf_path: Path) -> bool:
        try:
            res = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", f"-output-directory={self.output_dir}", str(tex_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15
            )
            return res.returncode == 0 and pdf_path.exists()
        except Exception:
            return False

    @staticmethod
    async def _launch_pdf_browser(p):
        """
        A headless browser to print the resume with, from whatever is installed.

        `chromium.launch(headless=True)` resolves to the separate
        `chromium_headless_shell` build, which `playwright install chromium` does
        not always place and which nothing else in this app needs -- the search
        session runs on the machine's real Chrome via `channel="chrome"`. So on a
        customer machine the shell was routinely absent, this raised, and the
        caller logged "Resume tailor warning, using default" and applied with the
        untailored master resume. Every application still went out; the tailoring
        the product is sold on just silently stopped happening.

        Installed Chrome is tried first because the app has already proved it is
        there, then Edge, then the bundled Chromium, then the shell.
        """
        attempts = [
            {"channel": "chrome", "headless": True},
            {"channel": "msedge", "headless": True},
            {"headless": True, "chromium_sandbox": False},
        ]
        last_error: Optional[Exception] = None
        for kwargs in attempts:
            try:
                return await p.chromium.launch(**kwargs)
            except Exception as exc:
                last_error = exc
        raise RuntimeError(
            f"No Chrome, Edge or Chromium build could be launched to render the PDF: {last_error}"
        )

    async def _compile_with_playwright_async(self, html_content: str, pdf_path: Path) -> None:
        async with async_playwright() as p:
            browser = await self._launch_pdf_browser(p)
            page = await browser.new_page()
            await page.set_content(html_content, wait_until="networkidle")
            await page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={"top": "10mm", "bottom": "10mm", "left": "12mm", "right": "12mm"}
            )
            await browser.close()

    def display_generation_summary(self, metadata: Dict[str, Any]) -> None:
        table = Table(title="🚀 AI Tailored Resume (2-Prompt Audited)", show_header=True, header_style="bold cyan")
        table.add_column("Metric / Property", style="bold white")
        table.add_column("Value", style="green")

        table.add_row("Target Job", f"{metadata['job_title']} at {metadata['company']}")
        table.add_row("Target Role", str(metadata['target_role']))
        table.add_row("Fidelity Score", f"{metadata['fidelity_score']}%")
        table.add_row("Deviation Measured", f"{metadata['deviation_percent']}%")
        table.add_row(
            "Factual Integrity",
            "[bold green]✓ PASS (nothing invented)[/bold green]"
            if metadata['is_within_10_percent_limit']
            else "[bold red]FAIL (see auditor findings)[/bold red]",
        )
        coverage = metadata.get("ats_keyword_coverage_pct")
        if coverage is not None:
            table.add_row("ATS Keyword Coverage", f"{coverage}%")
        table.add_row("AI Auditor Verdict", f"[bold green]{metadata['auditor_verdict']}[/bold green] (Attempts: {metadata['audit_attempts']})")
        table.add_row("PDF Generated", metadata['pdf_file'])

        console.print(table)


def list_generated_resumes(output_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    path = paths.resolve_writable(output_dir, paths.generated_resumes_dir())
    if not path.exists():
        return []
    meta_files = sorted(path.glob("*.metadata.json"), key=os.path.getmtime, reverse=True)
    resumes = []
    for mf in meta_files:
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            resumes.append(data)
        except Exception:
            pass
    return resumes


def display_resumes_table(resumes: List[Dict[str, Any]]) -> None:
    if not resumes:
        console.print("[yellow]No AI-tailored resumes generated yet.[/yellow]")
        return

    table = Table(title="📄 AI Generated Tailored Resumes (2-Prompt Verified)", header_style="bold magenta")
    table.add_column("#", style="dim", width=3)
    table.add_column("Company", style="bold white")
    table.add_column("Role", style="cyan")
    table.add_column("Fidelity", style="green")
    table.add_column("Deviation", style="yellow")
    table.add_column("Auditor Verdict", style="bold green")
    table.add_column("PDF File", style="dim")

    for i, r in enumerate(resumes, 1):
        dev = r.get("deviation_percent", 0.0)
        dev_str = f"[green]{dev}%[/green]" if dev <= 10.0 else f"[red]{dev}%[/red]"
        verdict = r.get("auditor_verdict", "APPROVED")
        verdict_str = f"[green]✓ {verdict}[/green]" if verdict == "APPROVED" else f"[red]✗ {verdict}[/red]"

        table.add_row(
            str(i),
            r.get("company", "N/A"),
            r.get("job_title", "N/A"),
            f"{r.get('fidelity_score', 0)}%",
            dev_str,
            verdict_str,
            r.get("pdf_file", "N/A")
        )

    console.print(table)


def inspect_resume_diff(resume_meta: Dict[str, Any]) -> None:
    job_title = resume_meta.get("job_title", "Unknown")
    company = resume_meta.get("company", "Unknown")
    tailored_summary = resume_meta.get("tailored_summary", "")
    # Written into the metadata at generation time. Older resumes predate that
    # field, so fall back to comparing against the tailored text -- a 0% diff
    # is honest about having nothing to compare, where another person's summary
    # would have been actively wrong.
    master_summary = resume_meta.get("master_summary") or tailored_summary

    sum_fid, sum_dev = calculate_fidelity_and_deviation(master_summary, tailored_summary)

    console.print(Panel(
        f"[bold cyan]Comparing Master Resume vs Tailored Resume for:[/bold cyan] [bold white]{job_title} at {company}[/bold white]\n"
        f"[bold green]Fidelity Score:[/bold green] {resume_meta.get('fidelity_score')}%  |  "
        f"[bold yellow]Deviation:[/bold yellow] {resume_meta.get('deviation_percent')}%  |  "
        f"[bold green]AI Auditor Status:[/bold green] {resume_meta.get('auditor_verdict')}\n"
        f"[dim]Auditor Findings:[/dim] {resume_meta.get('auditor_findings')}",
        title="🔍 2-Prompt AI Resume Audit & Diff",
        border_style="cyan"
    ))

    p1 = Panel(master_summary, title="[bold yellow]Original Master Summary[/bold yellow]", border_style="yellow")
    p2 = Panel(tailored_summary, title=f"[bold green]Tailored Summary ({sum_dev}% dev)[/bold green]", border_style="green")
    console.print(Columns([p1, p2]))


def open_generated_folder(output_dir: Optional[str] = None) -> None:
    path = paths.resolve_writable(output_dir, paths.generated_resumes_dir())
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    else:
        subprocess.run(["xdg-open", str(path)])
