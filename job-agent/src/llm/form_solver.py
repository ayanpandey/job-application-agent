"""
AI Form Solver for answering LinkedIn application screening questions.

Answers are resolved in three passes, in descending order of trust:

  1. Deterministic rules over the candidate profile, scoped to the country the
     question is actually about.
  2. LLM reasoning, constrained to the offered options.
  3. Explicit refusal (`needs_human`), when neither pass produced an answer the
     solver is willing to stand behind.

Pass 3 matters as much as the other two. A screening answer that is merely
wrong still submits successfully, so a bad guess costs the candidate the role
with no error anywhere in the logs. Every unresolved path here fails closed.

Two things make pass 2 cheap. Answers are cached on the content of the question
rather than the job, because the same handful of questions ("notice period?",
"years with SOX?", "current CTC?") make up most of the Easy Apply forms on the
platform -- so the second time one appears it costs nothing. And when a step
carries several unresolved questions they go up in one request instead of N,
which is where most of the per-application latency was going.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.core.models import CandidateProfile, ScreeningAnswerResult
from src.llm.client import LLMClient
from src.llm.cache import LLMCache, make_key, profile_fingerprint
from src.core.logger import logger


@dataclass
class FormQuestion:
    """One unanswered field on the current step of a form."""
    text: str
    input_type: str = "text"
    options: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Country resolution
# ---------------------------------------------------------------------------
# Work-authorization questions are only answerable relative to a country. The
# previous implementation read `legally_authorized_in_us` for every such
# question, so an Indian citizen applying to a Bengaluru role answered "No, I
# am not authorized to work in India" -- a knockout on most ATS screens, with
# `legally_authorized_in_india: true` sitting unread in the same profile.

# Ambiguous short forms ("us", "eu") only count as a country when a preposition
# puts them in the right position: "authorized to work in the US" is a country,
# "are you authorized to work for us" is not.
_COUNTRY_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\bindia\b", "india"),
    (r"\bunited states\b|\bu\.?s\.?a\b|\bamerica\b", "united states"),
    (r"\bin (?:the )?u\.?s\.?\b|\bwithin (?:the )?u\.?s\.?\b", "united states"),
    (r"\bunited kingdom\b|\bgreat britain\b|\bengland\b|\bscotland\b|\bwales\b", "united kingdom"),
    (r"\bin (?:the )?u\.?k\.?\b", "united kingdom"),
    (r"\bcanada\b", "canada"),
    (r"\baustralia\b", "australia"),
    (r"\bnew zealand\b", "new zealand"),
    (r"\bsingapore\b", "singapore"),
    (r"\bgermany\b", "germany"),
    (r"\bnetherlands\b|\bholland\b", "netherlands"),
    (r"\bireland\b", "ireland"),
    (r"\bfrance\b", "france"),
    (r"\bswitzerland\b", "switzerland"),
    (r"\bjapan\b", "japan"),
    (r"\buae\b|\bunited arab emirates\b|\bdubai\b|\babu dhabi\b", "united arab emirates"),
    (r"\beuropean union\b|\bschengen\b|\bthe eu\b|\bin eu\b", "european union"),
)

# Canonical country -> the profile key slugs that may encode it. Profiles in the
# wild use short slugs (`legally_authorized_in_us`), so each canonical name
# carries every spelling worth probing.
_COUNTRY_KEY_SLUGS: Dict[str, Tuple[str, ...]] = {
    "india": ("india", "in"),
    "united states": ("united_states", "us", "usa", "america"),
    "united kingdom": ("united_kingdom", "uk", "gb", "britain"),
    "canada": ("canada", "ca"),
    "australia": ("australia", "au"),
    "new zealand": ("new_zealand", "nz"),
    "singapore": ("singapore", "sg"),
    "germany": ("germany", "de"),
    "netherlands": ("netherlands", "nl"),
    "ireland": ("ireland", "ie"),
    "france": ("france", "fr"),
    "switzerland": ("switzerland", "ch"),
    "japan": ("japan", "jp"),
    "united arab emirates": ("united_arab_emirates", "uae", "ae"),
    "european union": ("european_union", "eu"),
}

# Countries whose work-authorization status a citizen automatically holds, keyed
# by the free-text country string a profile is likely to carry.
_CITIZENSHIP_TO_COUNTRY: Dict[str, str] = {
    "india": "india",
    "indian": "india",
    "united states": "united states",
    "usa": "united states",
    "us": "united states",
    "american": "united states",
    "united kingdom": "united kingdom",
    "uk": "united kingdom",
    "british": "united kingdom",
    "canada": "canada",
    "canadian": "canada",
    "australia": "australia",
    "australian": "australia",
    "singapore": "singapore",
    "germany": "germany",
    "german": "germany",
    "ireland": "ireland",
    "irish": "ireland",
}

# EU citizenship confers authorization across the union.
_EU_MEMBERS = {"germany", "netherlands", "ireland", "france"}


def _normalize_country(raw: str) -> str:
    """Maps a free-text country or nationality onto a canonical country key."""
    if not raw:
        return ""
    key = re.sub(r"[^a-z ]", "", str(raw).lower()).strip()
    if key in _CITIZENSHIP_TO_COUNTRY:
        return _CITIZENSHIP_TO_COUNTRY[key]
    for pattern, canonical in _COUNTRY_PATTERNS:
        if re.search(pattern, key):
            return canonical
    return key


def _country_in_text(text: str) -> str:
    """Returns the canonical country named in `text`, or "" if none is."""
    lowered = (text or "").lower()
    for pattern, canonical in _COUNTRY_PATTERNS:
        if re.search(pattern, lowered):
            return canonical
    return ""


class FormSolver:
    def __init__(self, llm_client: LLMClient, cache: Optional[LLMCache] = None):
        self.llm = llm_client
        self.cache = cache
        self._profile_key: Optional[str] = None
        self.stats = {"deterministic": 0, "cached": 0, "llm": 0, "escalated": 0}

    def _fingerprint(self, profile: CandidateProfile) -> str:
        if self._profile_key is None:
            self._profile_key = profile_fingerprint(profile)
        return self._profile_key

    def _cache_key(self, q: FormQuestion, profile: CandidateProfile) -> str:
        # Normalised question text, so trailing whitespace or a changed asterisk
        # on a required-field label does not miss an otherwise identical answer.
        normalised = re.sub(r"\s+", " ", (q.text or "").strip().lower()).strip(" *:")
        return make_key(
            "answer",
            self._fingerprint(profile),
            normalised,
            q.input_type,
            "|".join(sorted(o.strip() for o in q.options)),
        )

    # -- country / authorization helpers ------------------------------------

    def _home_country(self, profile: CandidateProfile) -> str:
        """The country the candidate lives in and is presumed authorized for."""
        wa = profile.screening_answers.work_authorization or {}
        return (
            _normalize_country(wa.get("citizenship", ""))
            or _normalize_country(profile.personal_info.country)
        )

    def _target_country(
        self, question_text: str, profile: CandidateProfile, job_location: str
    ) -> str:
        """
        Which country an authorization question is about.

        Preference order is question text, then the posting's location, then the
        candidate's own country -- most specific evidence first, and never a
        hardcoded default.
        """
        return (
            _country_in_text(question_text)
            or _country_in_text(job_location)
            or self._home_country(profile)
        )

    def _lookup_country_flag(
        self, mapping: Dict[str, Any], prefix: str, country: str
    ) -> Optional[bool]:
        """Finds `<prefix>_<country slug>` in a profile dict, trying each spelling."""
        for slug in _COUNTRY_KEY_SLUGS.get(country, (country.replace(" ", "_"),)):
            key = f"{prefix}_{slug}"
            if key in mapping:
                return bool(mapping[key])
        return None

    def _authorized_in(
        self, profile: CandidateProfile, country: str
    ) -> Tuple[Optional[bool], str]:
        """
        Whether the candidate may work in `country`.

        Returns (answer, reasoning); answer is None when the profile does not
        say, which is escalated rather than guessed.
        """
        wa = profile.screening_answers.work_authorization or {}
        if not country:
            return None, "No country could be resolved for this question."

        explicit = self._lookup_country_flag(wa, "legally_authorized_in", country)
        if explicit is not None:
            return explicit, f"Profile states legal authorization in {country}: {explicit}."

        citizenship = _normalize_country(wa.get("citizenship", ""))
        if citizenship and citizenship == country:
            return True, f"Candidate is a citizen of {country}."
        if citizenship in _EU_MEMBERS and country == "european union":
            return True, f"Citizenship of {citizenship} confers EU work authorization."

        home = self._home_country(profile)
        if home and home == country:
            return True, f"Candidate's country of residence is {country}."

        return None, f"Profile does not state work authorization for {country}."

    # -- answer-shape helpers -----------------------------------------------

    def _yes_no(
        self, value: bool, options: List[str], reasoning: str
    ) -> ScreeningAnswerResult:
        """Renders a boolean using the form's own vocabulary where one is offered."""
        want = "yes" if value else "no"
        for opt in options:
            if opt.strip().lower() == want:
                return ScreeningAnswerResult(answer=opt, confidence=1.0, reasoning=reasoning)
        for opt in options:
            if opt.strip().lower().startswith(want):
                return ScreeningAnswerResult(answer=opt, confidence=0.95, reasoning=reasoning)
        return ScreeningAnswerResult(
            answer="Yes" if value else "No", confidence=1.0, reasoning=reasoning
        )

    def _escalate(self, reasoning: str) -> ScreeningAnswerResult:
        """Refuses to answer, so the caller leaves the field for a human."""
        return ScreeningAnswerResult(
            answer="", confidence=0.0, reasoning=reasoning, needs_human=True
        )

    # -- individual deterministic rules -------------------------------------

    def _years_for_skill(self, clean_q: str, profile: CandidateProfile) -> Optional[Tuple[str, int]]:
        """
        Longest skill name mentioned in the question wins.

        Longest-first matters: "deep learning" and "learning" can both be in the
        profile, and the more specific figure is the honest one. Matching is on
        word boundaries so "sql" does not fire on "postgresql".
        """
        best: Optional[Tuple[str, int]] = None
        for skill, yrs in (profile.screening_answers.years_experience_by_skill or {}).items():
            spoken = skill.replace("_", " ").strip().lower()
            if not spoken:
                continue
            pattern = r"\b" + r"[\s_-]*".join(re.escape(w) for w in spoken.split()) + r"\b"
            if re.search(pattern, clean_q):
                if best is None or len(spoken) > len(best[0]):
                    best = (spoken, int(yrs))
        return best

    def _salary_answer(
        self, clean_q: str, input_type: str, profile: CandidateProfile
    ) -> ScreeningAnswerResult:
        """
        Answers compensation questions in the unit the form is asking for.

        Indian postings ask for CTC in lakhs; returning a USD figure there reads
        as either an absurd number or a validation failure, and the profile
        already carries the rupee figures.
        """
        prefs = profile.screening_answers.preferences or {}
        lakhs = prefs.get("desired_salary_inr_lakhs")
        usd = prefs.get("desired_salary_usd")

        wants_lakhs = bool(re.search(r"\blakh|\blpa\b|\bctc\b|\bin lacs?\b", clean_q))
        wants_inr = bool(re.search(r"\binr\b|₹|\brupees?\b|\brs\.?\b", clean_q))
        wants_usd = bool(re.search(r"\busd\b|\$|\bdollar", clean_q))

        if not (wants_lakhs or wants_inr or wants_usd):
            # Unmarked field: follow the candidate's own market.
            if self._home_country(profile) == "india":
                wants_lakhs = True
            else:
                wants_usd = True

        if wants_lakhs and lakhs is not None:
            value = int(lakhs) if float(lakhs).is_integer() else float(lakhs)
            return ScreeningAnswerResult(
                answer=str(value), confidence=0.9,
                reasoning="Desired CTC in INR lakhs, matching the unit the question asks for.",
            )
        if wants_inr and lakhs is not None:
            return ScreeningAnswerResult(
                answer=str(int(float(lakhs) * 100_000)), confidence=0.85,
                reasoning="Desired CTC converted from lakhs to absolute INR.",
            )
        if wants_usd and usd is not None:
            return ScreeningAnswerResult(
                answer=str(int(usd)), confidence=0.9, reasoning="Desired salary in USD.",
            )

        return self._escalate(
            "Compensation question in a currency/unit the profile does not specify."
        )

    def _notice_period_answer(
        self, clean_q: str, input_type: str, profile: CandidateProfile
    ) -> ScreeningAnswerResult:
        """Answers notice-period questions in days, weeks, or months as asked."""
        prefs = profile.screening_answers.preferences or {}
        days = prefs.get("notice_period_days")
        weeks = prefs.get("notice_period_weeks")
        if days is None and weeks is None:
            return self._escalate("Profile does not state a notice period.")
        if days is None:
            days = int(weeks) * 7
        days = int(days)

        numeric = input_type in ("number", "numeric")
        if days == 0 and not numeric:
            return ScreeningAnswerResult(
                answer="Immediately", confidence=0.9, reasoning="Zero-day notice period."
            )
        if "month" in clean_q:
            months = max(1, round(days / 30))
            return ScreeningAnswerResult(
                answer=str(months) if numeric else f"{months} month{'s' if months > 1 else ''}",
                confidence=0.85, reasoning="Notice period expressed in months.",
            )
        if "week" in clean_q:
            wks = weeks if weeks is not None else max(1, round(days / 7))
            return ScreeningAnswerResult(
                answer=str(int(wks)) if numeric else f"{int(wks)} weeks",
                confidence=0.9, reasoning="Notice period expressed in weeks.",
            )
        # Days is both the explicit "in days" case and the safest numeric default.
        return ScreeningAnswerResult(
            answer=str(days) if numeric or "day" in clean_q else f"{days} days",
            confidence=0.9, reasoning="Notice period expressed in days.",
        )

    # -- pass 1: deterministic rules -----------------------------------------

    def _deterministic_answer(
        self,
        question_text: str,
        input_type: str,
        options: List[str],
        profile: CandidateProfile,
        job_location: str,
    ) -> Optional[ScreeningAnswerResult]:
        """
        Pass 1: rules over the profile. Returns None when no rule applies, which
        is the signal to fall through to the model.

        Note that an explicit refusal (`needs_human`) IS a result and is returned
        as one -- "the profile does not state a relocation preference" is a fact
        the model cannot improve on, and sending it upstream would only invite a
        confident guess.
        """
        clean_q = (question_text or "").lower().strip()

        # Years of experience with a specific technology
        if "how many years" in clean_q or "years of experience" in clean_q or "years of work experience" in clean_q:
            hit = self._years_for_skill(clean_q, profile)
            if hit:
                skill, yrs = hit
                return ScreeningAnswerResult(
                    answer=str(yrs), confidence=1.0,
                    reasoning=f"Matched skill '{skill}' in profile screening database.",
                )
            return ScreeningAnswerResult(
                answer=str(profile.screening_answers.total_years_experience),
                confidence=0.85,
                reasoning="Used general total years of professional experience.",
            )

        # Work authorization, scoped to the country actually being asked about
        if "legally authorized" in clean_q or "authorized to work" in clean_q or "right to work" in clean_q:
            country = self._target_country(question_text, profile, job_location)
            authorized, why = self._authorized_in(profile, country)
            if authorized is None:
                return self._escalate(why)
            return self._yes_no(authorized, options, why)

        # Sponsorship is the inverse of authorization for the country in question
        if "sponsorship" in clean_q or "sponsor" in clean_q:
            country = self._target_country(question_text, profile, job_location)
            wa = profile.screening_answers.work_authorization or {}

            explicit = self._lookup_country_flag(wa, "requires_sponsorship_in", country)
            if explicit is not None:
                return self._yes_no(
                    explicit, options,
                    f"Profile states sponsorship requirement for {country}: {explicit}.",
                )

            authorized, why = self._authorized_in(profile, country)
            if authorized is True:
                # Already authorized there, so no sponsorship is needed -- this is
                # the case the old global flag got wrong for home-country roles.
                return self._yes_no(
                    False, options,
                    f"No sponsorship required: {why.rstrip('.')}.",
                )
            if authorized is False:
                return self._yes_no(
                    True, options, f"Sponsorship required: not authorized in {country}."
                )
            if "requires_sponsorship_now_or_future" in wa:
                return self._yes_no(
                    bool(wa["requires_sponsorship_now_or_future"]), options,
                    "Fell back to the profile's country-agnostic sponsorship flag.",
                )
            return self._escalate(why)

        # Notice period
        if "notice period" in clean_q or "how soon can you start" in clean_q or "when can you join" in clean_q:
            return self._notice_period_answer(clean_q, input_type, profile)

        # Salary expectations
        if any(t in clean_q for t in ("salary", "compensation", "desired pay", "ctc", "lpa")):
            return self._salary_answer(clean_q, input_type, profile)

        # Commute / Relocation
        prefs = profile.screening_answers.preferences or {}
        if "willing to relocate" in clean_q or "open to relocat" in clean_q:
            if "willing_to_relocate" not in prefs:
                return self._escalate("Profile does not state a relocation preference.")
            return self._yes_no(
                bool(prefs["willing_to_relocate"]), options, "Matched relocation preference."
            )

        if "comfortable commuting" in clean_q or "hybrid" in clean_q:
            if "willing_to_commute_hybrid" not in prefs:
                return self._escalate("Profile does not state a commute preference.")
            return self._yes_no(
                bool(prefs["willing_to_commute_hybrid"]), options, "Matched commute preference."
            )

        return None

    # -- public API ----------------------------------------------------------

    def solve_question(
        self,
        question_text: str,
        input_type: str,
        options: Optional[List[str]] = None,
        profile: Optional[CandidateProfile] = None,
        job_location: str = "",
    ) -> ScreeningAnswerResult:
        """Resolves one question. Kept for callers that only have one to ask."""
        results = self.solve_questions(
            [FormQuestion(text=question_text, input_type=input_type, options=list(options or []))],
            profile=profile,
            job_location=job_location,
        )
        return results[0]

    def solve_questions(
        self,
        questions: Sequence[FormQuestion],
        profile: Optional[CandidateProfile] = None,
        job_location: str = "",
        allow_batch: bool = True,
    ) -> List[ScreeningAnswerResult]:
        """
        Resolves a whole form step at once.

        Rules and cache first, then a single request covering whatever is left.
        Answers come back positionally, one per input question, so the caller can
        zip them straight back onto its field handles.
        """
        if not profile:
            return [self._escalate("No profile provided.") for _ in questions]

        results: List[Optional[ScreeningAnswerResult]] = [None] * len(questions)
        pending: List[int] = []

        for i, q in enumerate(questions):
            opts = list(q.options or [])
            deterministic = self._deterministic_answer(
                q.text, q.input_type, opts, profile, job_location
            )
            if deterministic is not None:
                self.stats["deterministic"] += 1
                results[i] = deterministic
                continue

            if self.cache is not None:
                hit = self.cache.get(self._cache_key(q, profile))
                if hit:
                    try:
                        self.stats["cached"] += 1
                        results[i] = ScreeningAnswerResult(**hit)
                        continue
                    except Exception:
                        pass  # stored shape no longer valid; ask again

            pending.append(i)

        if pending:
            if allow_batch and len(pending) > 1:
                answered = self._ask_model_batch(
                    [questions[i] for i in pending], profile, job_location
                )
            else:
                answered = [
                    self._ask_model_single(questions[i], profile, job_location) for i in pending
                ]

            for slot, answer in zip(pending, answered):
                results[slot] = answer
                if self.cache is not None and not answer.needs_human and str(answer.answer).strip():
                    # Refusals are deliberately not cached: they usually mean the
                    # profile is missing a field, and once the user fills it in
                    # the next run should get a real answer rather than a stored
                    # "I could not tell".
                    self.cache.put(
                        self._cache_key(questions[slot], profile), "answer", answer.model_dump()
                    )

        final: List[ScreeningAnswerResult] = []
        for r in results:
            resolved = r or self._escalate("No answer was produced for this question.")
            if resolved.needs_human:
                self.stats["escalated"] += 1
            final.append(resolved)
        return final

    # -- the expensive path --------------------------------------------------

    def _validate(
        self, raw_answer: str, confidence: float, reasoning: str, options: List[str]
    ) -> ScreeningAnswerResult:
        """Shared post-processing: an answer must be one of the offered options."""
        raw_answer = str(raw_answer or "").strip()
        if not raw_answer:
            # The old default here was "Yes", which silently answered affirmatively
            # to sponsorship and compliance questions whenever the model returned
            # malformed JSON. Blank is the only safe default.
            return self._escalate("LLM returned no usable answer.")

        if options:
            matched = self._match_best_option(raw_answer, options)
            if matched is None:
                return self._escalate(
                    f"LLM answer '{raw_answer[:40]}' matched none of the offered options."
                )
            return ScreeningAnswerResult(
                answer=matched, confidence=min(confidence, 0.9), reasoning=reasoning
            )
        return ScreeningAnswerResult(answer=raw_answer, confidence=confidence, reasoning=reasoning)

    def _ask_model_single(
        self, q: FormQuestion, profile: CandidateProfile, job_location: str
    ) -> ScreeningAnswerResult:
        try:
            self.stats["llm"] += 1
            data = self.llm.run_task_json(
                "question_solver",
                {
                    "candidate_profile_json": json.dumps(profile.model_dump(), indent=2),
                    "question_text": q.text,
                    "input_type": q.input_type,
                    "options": ", ".join(q.options) if q.options else "N/A",
                },
            ) or {}
        except Exception as exc:
            logger.warning(f"Form solver LLM call failed for '{q.text[:60]}': {exc}")
            return self._escalate(f"LLM call failed: {exc}")

        if data.get("llm_unavailable"):
            return self._escalate("LLM unavailable; not guessing at a screening answer.")

        return self._validate(
            data.get("answer", ""),
            float(data.get("confidence", 0.75) or 0.0),
            data.get("reasoning", "Generated by LLM reasoning."),
            list(q.options or []),
        )

    def _ask_model_batch(
        self, questions: List[FormQuestion], profile: CandidateProfile, job_location: str
    ) -> List[ScreeningAnswerResult]:
        """
        Resolves several questions in one request.

        A six-question step used to be six sequential round-trips with the modal
        sitting open between them. Failure falls back to asking individually
        rather than escalating the whole step -- one malformed batch response
        should not send a fillable application to manual review.
        """
        listed = "\n".join(
            f"{i + 1}. Question: {q.text}\n"
            f"   Input type: {q.input_type}\n"
            f"   Options: {', '.join(q.options) if q.options else 'N/A'}"
            for i, q in enumerate(questions)
        )

        try:
            self.stats["llm"] += 1
            data = self.llm.run_task_json(
                "question_solver_batch",
                {
                    "candidate_profile_json": json.dumps(profile.model_dump(), indent=2),
                    "job_location": job_location or "not specified",
                    "questions_block": listed,
                    "question_count": len(questions),
                },
            ) or {}
        except Exception as exc:
            logger.warning(f"Batched form solve failed ({exc}); falling back to one call per question.")
            return [self._ask_model_single(q, profile, job_location) for q in questions]

        if data.get("llm_unavailable"):
            return [
                self._escalate("LLM unavailable; not guessing at a screening answer.")
                for _ in questions
            ]

        answers = data.get("answers")
        if not isinstance(answers, list) or not answers:
            logger.warning("Batched form solve returned no usable answers; retrying individually.")
            return [self._ask_model_single(q, profile, job_location) for q in questions]

        # Index by the model's own 1-based numbering rather than by position:
        # models occasionally drop or reorder an entry, and silently shifting
        # every subsequent answer up by one would put the wrong text in the
        # wrong field -- which submits cleanly and is invisible afterwards.
        by_index: Dict[int, Dict[str, Any]] = {}
        for entry in answers:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("index", 0))
            except (TypeError, ValueError):
                continue
            if 1 <= idx <= len(questions):
                by_index[idx] = entry

        results: List[ScreeningAnswerResult] = []
        missing: List[int] = []
        for i, q in enumerate(questions):
            entry = by_index.get(i + 1)
            if entry is None:
                missing.append(i)
                results.append(self._escalate("Batched response omitted this question."))
                continue
            results.append(
                self._validate(
                    entry.get("answer", ""),
                    float(entry.get("confidence", 0.75) or 0.0),
                    entry.get("reasoning", "Generated by LLM reasoning."),
                    list(q.options or []),
                )
            )

        # Anything the batch skipped is worth one targeted retry before it goes
        # to a human -- an omission is a formatting slip, not a refusal.
        for i in missing:
            results[i] = self._ask_model_single(questions[i], profile, job_location)

        return results

    def _match_best_option(self, target: str, options: List[str]) -> Optional[str]:
        """
        Maps a free-text answer onto one of the offered options.

        Returns None rather than a guess when nothing matches. The old fallback
        was `options[0]`, which on a Yes/No question meant an unmatched answer
        silently became "Yes" -- including on sponsorship questions, where it is
        a knockout.
        """
        target_lower = target.lower().strip()
        if not target_lower:
            return None

        for opt in options:
            if opt.lower().strip() == target_lower:
                return opt

        # Yes/No forms often phrase options as full sentences ("Yes, I am
        # authorized"). Anchor on the leading token so "No" cannot match
        # "Not applicable" through a bare substring test.
        head = re.split(r"[,\s]", target_lower, maxsplit=1)[0]
        if head in ("yes", "no"):
            for opt in options:
                if re.match(rf"^{head}\b", opt.lower().strip()):
                    return opt

        containment = [
            opt for opt in options
            if len(target_lower) > 2 and target_lower in opt.lower()
        ]
        if len(containment) == 1:
            return containment[0]

        reverse = [
            opt for opt in options
            if len(opt.strip()) > 2 and opt.lower().strip() in target_lower
        ]
        if len(reverse) == 1:
            return reverse[0]

        return None
