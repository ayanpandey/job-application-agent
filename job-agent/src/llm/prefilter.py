"""
Deterministic pre-screen that runs before any LLM call.

The matcher used to send every single job card to the model. On a results page of
20 listings that is 20 round-trips, and the majority of them are spent confirming
what a title alone already settles -- a Director of Internal Audit is not a match
for a three-year analyst no matter what the description says. Each of those calls
also sat on the event loop while the browser did nothing, so the cost was paid
twice: once in API latency and once in wall-clock time the session could have
spent looking at listings that might actually fit.

This module answers three questions from the title, the description text and the
candidate profile, with no network access at all:

  * Is this obviously not a match?  -> reject, no LLM call
  * Is this obviously a match?      -> fast-track, no LLM call
  * Anything else                   -> hand to the LLM, with the experience band
                                       already parsed so the model does not have
                                       to re-derive it

Experience is the part worth being careful about. Postings state a requirement
("5-8 years", "minimum 3 years") and hiring reality is that the band is soft at
both ends, so a hard equality test throws away roles the candidate would be
shortlisted for. The band is therefore widened by `tolerance` on each side
before it is tested, and falling under even the widened floor is only a hard
reject once the shortfall exceeds `max_shortfall_years`; anything closer than
that is still worth the model's opinion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from src.core.models import CandidateProfile, JobListing


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

# Role nouns that appear in almost every title in a given field and therefore
# carry very little signal about whether a posting is relevant. "Sales Engineer"
# and "AI Engineer" share a token; weighting them equally would score that pair
# at 0.5 and let unrelated postings through the cheap filter into the expensive
# one. Domain words ("audit", "sox", "pytorch") are deliberately NOT here -- they
# are exactly the tokens that decide relevance.
_GENERIC_TITLE_TOKENS = frozenset({
    "engineer", "engine", "develop", "developer", "analyst", "specialist",
    "consultant", "associate", "professional", "executive", "officer",
    "practitioner", "member", "technical", "technolog", "role", "position",
    "opening", "job", "team", "group", "new", "hiring", "urgent", "immediate",
    "i", "ii", "iii", "iv", "1", "2", "3", "4",
})

_GENERIC_TOKEN_WEIGHT = 0.35

_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "and", "or", "in", "on", "at", "to", "with",
    "&", "-", "/", "|", "with", "our", "your", "we", "you",
})


def _stem(token: str) -> str:
    """
    Light suffix stripping so 'auditor' matches 'audit' and 'engineering'
    matches 'engineer'.

    Deliberately crude: both sides of every comparison go through the same
    function, so consistency matters far more than linguistic correctness. A
    real stemmer would pull in a dependency to decide the same handful of cases.
    """
    t = token.lower().strip()
    if len(t) <= 4:
        return t
    for suffix in ("ing", "ers", "ors", "er", "or", "s"):
        if t.endswith(suffix) and len(t) - len(suffix) >= 3:
            return t[: -len(suffix)]
    return t


def _tokens(text: str) -> List[str]:
    raw = re.split(r"[^a-z0-9+#.]+", (text or "").lower())
    return [_stem(t) for t in raw if t and t not in _STOPWORDS and len(t) > 1]


def _token_weight(token: str) -> float:
    return _GENERIC_TOKEN_WEIGHT if token in _GENERIC_TITLE_TOKENS else 1.0


def _contains_word(haystack: str, needle: str) -> bool:
    """
    Whole-word containment.

    A bare substring test cannot be used for the exclusion lists: "intern"
    appears inside "internal", so excluding internships would have silently
    thrown away every Internal Auditor posting -- the single most important
    keyword for an audit profile.
    """
    needle = (needle or "").strip().lower()
    if not needle:
        return False
    pattern = re.escape(needle).replace(r"\ ", r"\s+")
    return bool(re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", haystack))


# ---------------------------------------------------------------------------
# Experience requirements
# ---------------------------------------------------------------------------

@dataclass
class ExperienceBand:
    """A years-of-experience requirement read off a posting or implied by its title."""
    low: Optional[float] = None
    high: Optional[float] = None
    source: str = "unstated"   # "description" | "title" | "unstated"
    raw: str = ""

    def describe(self) -> str:
        if self.low is None and self.high is None:
            return "not stated"
        if self.high is None:
            return f"{self.low:g}+ years"
        if self.low is None:
            return f"up to {self.high:g} years"
        if abs(self.low - self.high) < 0.01:
            return f"{self.low:g} years"
        return f"{self.low:g}-{self.high:g} years"


# "3-5 years", "3 to 5 yrs", "3 – 5 years"
_RANGE_RE = re.compile(
    r"(\d{1,2})(?:\.\d)?\s*(?:\+)?\s*(?:-|–|—|to|through)\s*(\d{1,2})(?:\.\d)?\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
# "minimum of 5 years", "at least 5 years", "5+ years", "5 years of experience"
_MIN_RE = re.compile(
    r"(?:minimum|min\.?|at ?least|atleast|over|more than|greater than|no less than)\s*"
    r"(?:of\s*)?(\d{1,2})(?:\.\d)?\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_PLUS_RE = re.compile(r"(\d{1,2})(?:\.\d)?\s*\+\s*(?:years?|yrs?)\b", re.IGNORECASE)
_PLAIN_RE = re.compile(r"(\d{1,2})(?:\.\d)?\s*(?:years?|yrs?)\b", re.IGNORECASE)
_MONTHS_RE = re.compile(r"(\d{1,2})\s*months?\b", re.IGNORECASE)

# A bare "3 years" only counts as a requirement when it sits near a word that
# makes it one. Postings are full of incidental year counts ("in the last 3
# years we have grown 4x"), and reading those as a hiring bar silently rejects
# perfectly good listings.
_EXPERIENCE_CONTEXT = re.compile(
    r"experien|background|track record|working with|hands[- ]?on|practice|exposure|"
    r"required|requirement|qualificat|must have|should have|looking for|seeking",
    re.IGNORECASE,
)
_CONTEXT_WINDOW = 90


def _has_experience_context(text: str, span: Tuple[int, int]) -> bool:
    start = max(0, span[0] - _CONTEXT_WINDOW)
    end = min(len(text), span[1] + _CONTEXT_WINDOW)
    return bool(_EXPERIENCE_CONTEXT.search(text[start:end]))


def parse_experience_requirement(description: str) -> Optional[ExperienceBand]:
    """
    Reads the years-of-experience requirement out of a job description.

    A posting normally states several -- a headline figure plus per-skill ones
    ("8+ years overall, 2+ years with Kubernetes"). The lowest stated floor is
    taken as the governing one and the highest ceiling as the top of the band.
    That is the generous reading, on purpose: this filter decides what is worth
    an LLM call, and the hard reject only fires well below the floor anyway, so
    an over-strict reading here costs opportunities while a loose one costs a
    single cheap API call.
    """
    if not description:
        return None

    text = description[:12000]
    lows: List[float] = []
    highs: List[float] = []
    raws: List[str] = []

    for m in _RANGE_RE.finditer(text):
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo > hi or hi > 40:
            continue
        lows.append(lo)
        highs.append(hi)
        raws.append(m.group(0).strip())

    for regex in (_MIN_RE, _PLUS_RE):
        for m in regex.finditer(text):
            val = float(m.group(1))
            if val > 40:
                continue
            lows.append(val)
            raws.append(m.group(0).strip())

    if not lows:
        for m in _PLAIN_RE.finditer(text):
            if not _has_experience_context(text, m.span()):
                continue
            val = float(m.group(1))
            if val > 40:
                continue
            lows.append(val)
            highs.append(val)
            raws.append(m.group(0).strip())

    if not lows:
        # Internship-style postings state the bar in months.
        for m in _MONTHS_RE.finditer(text):
            if not _has_experience_context(text, m.span()):
                continue
            lows.append(round(float(m.group(1)) / 12.0, 2))
            raws.append(m.group(0).strip())

    if not lows:
        return None

    low = min(lows)
    high = max(highs) if highs else None
    if high is not None and high < low:
        high = None

    return ExperienceBand(
        low=low,
        high=high,
        source="description",
        raw="; ".join(dict.fromkeys(raws))[:120],
    )


# Seniority implied by the title, used when the description never states a
# number -- which is most postings. Without this the experience test would be a
# no-op on the majority of listings.
#
# Each entry is (title markers, implied floor, implied ceiling).
_SENIORITY_TIERS: Tuple[Tuple[Tuple[str, ...], Optional[float], Optional[float]], ...] = (
    (("intern", "internship", "trainee", "apprentice", "articleship"), 0.0, 1.0),
    (("fresher", "graduate", "campus", "entry level", "entry-level"), 0.0, 2.0),
    (("junior", "jr"), 0.0, 3.0),
    (("chief", "cxo", "vp", "vice president", "president", "head of", "director",
      "partner", "general manager"), 10.0, None),
    (("staff", "principal", "architect", "distinguished"), 8.0, None),
    (("manager", "management"), 7.0, None),
    (("senior", "sr", "lead"), 5.0, None),
)


def _title_seniority_band(title: str) -> Optional[ExperienceBand]:
    lowered = f" {(title or '').lower()} "
    for markers, low, high in _SENIORITY_TIERS:
        for marker in markers:
            pattern = re.escape(marker).replace(r"\ ", r"\s+")
            if re.search(rf"(?<![a-z]){pattern}(?![a-z])", lowered):
                return ExperienceBand(low=low, high=high, source="title", raw=marker)
    return None


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

@dataclass
class PreFilterVerdict:
    """What the cheap pass concluded, and why."""
    decision: str                      # "reject" | "evaluate" | "fast_track"
    score: int                         # 0-100 deterministic fit estimate
    band: Optional[ExperienceBand] = None
    years_verdict: str = "unstated"    # "in_band" | "under" | "over" | "unstated"
    title_score: float = 0.0
    skill_score: float = 0.0
    experience_score: float = 0.0
    matched_skills: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return self.decision == "reject"

    @property
    def needs_llm(self) -> bool:
        return self.decision == "evaluate"

    def summary(self) -> str:
        return "; ".join(self.reasons) or "No deterministic signal."


class JobPreFilter:
    """
    Scores a posting against the candidate profile without an LLM call.

    Constructed once per session; all the profile-derived vocabulary is built up
    front so per-job cost is a handful of regex passes over the description.
    """

    def __init__(
        self,
        profile: CandidateProfile,
        search_keywords: Sequence[str] = (),
        tolerance: float = 0.20,
        max_shortfall_years: float = 2.0,
        reject_below: int = 34,
        fast_track_above: int = 88,
        exclude_title_keywords: Sequence[str] = (),
        exclude_companies: Sequence[str] = (),
    ):
        self.profile = profile
        self.tolerance = max(0.0, tolerance)
        self.max_shortfall_years = max(0.0, max_shortfall_years)
        self.reject_below = reject_below
        self.fast_track_above = fast_track_above
        self.exclude_title_keywords = [k.lower().strip() for k in exclude_title_keywords if k.strip()]
        self.exclude_companies = [c.lower().strip() for c in exclude_companies if c.strip()]

        self.candidate_years = float(profile.screening_answers.total_years_experience or 0)

        # Target titles: what the user searches for, plus the roles they have
        # actually held. The second half matters when the keyword list is narrow
        # -- a posting titled "IT Risk Analyst" is relevant to someone whose last
        # role was "Digital Risk Consulting" even if no keyword says so.
        phrases: List[str] = [k for k in search_keywords if k and k.strip()]
        phrases += [exp.role for exp in profile.experience if exp.role]
        self._title_targets: List[List[str]] = []
        for phrase in phrases:
            toks = _tokens(phrase)
            if toks:
                self._title_targets.append(toks)

        # Skill vocabulary, stemmed once. Keys of years_experience_by_skill are
        # included because they carry things the skills list often does not.
        skills = list(profile.skills.all_skills())
        skills += [k.replace("_", " ") for k in (profile.screening_answers.years_experience_by_skill or {})]
        self._skill_terms: Dict[str, str] = {}
        for skill in skills:
            cleaned = re.sub(r"\(.*?\)", " ", skill).strip().lower()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if len(cleaned) >= 2:
                self._skill_terms.setdefault(cleaned, skill)
        # Denominator for skill overlap. Capped so a 60-entry skills list does
        # not make every job look like a weak match by dilution.
        self._skill_denominator = max(4, min(len(self._skill_terms), 14))

    # -- individual signals --------------------------------------------------

    def _score_title(self, title: str) -> float:
        """Weighted token overlap against the best-matching target phrase."""
        title_tokens = set(_tokens(title))
        if not title_tokens or not self._title_targets:
            return 0.0

        best = 0.0
        for target in self._title_targets:
            total = sum(_token_weight(t) for t in target)
            if total <= 0:
                continue
            hit = sum(_token_weight(t) for t in target if t in title_tokens)
            best = max(best, hit / total)
        return min(1.0, best)

    def _score_skills(self, text: str) -> Tuple[float, List[str]]:
        lowered = (text or "").lower()
        if not lowered:
            return 0.0, []
        matched: List[str] = []
        for term, original in self._skill_terms.items():
            pattern = r"\b" + r"[\s\-_/]*".join(re.escape(w) for w in term.split()) + r"\b"
            if re.search(pattern, lowered):
                matched.append(original)
        score = min(1.0, len(matched) / self._skill_denominator)
        return score, matched[:12]

    def resolve_band(self, job: JobListing) -> Optional[ExperienceBand]:
        """Stated requirement if there is one, otherwise the one the title implies."""
        return parse_experience_requirement(job.description) or _title_seniority_band(job.title)

    def _score_experience(
        self, band: Optional[ExperienceBand]
    ) -> Tuple[float, str, Optional[str]]:
        """
        Returns (0-1 fit, verdict, hard-reject reason or None).

        The band is widened by `tolerance` on both sides before anything is
        tested -- that widening is the whole point. A posting asking for 5 years
        is treated as open at 4, and one asking for 3 stays open to someone with
        3.6. Below the widened floor the candidate is not cut immediately: the
        shortfall has to exceed `max_shortfall_years` first, so near-misses still
        reach the model instead of being thrown away by arithmetic.
        """
        years = self.candidate_years
        if band is None or (band.low is None and band.high is None):
            # Nothing to test. Neutral rather than zero: an unstated requirement
            # is not evidence against the candidate.
            return 0.65, "unstated", None

        low = band.low
        high = band.high
        relaxed_low = None if low is None else max(0.0, low * (1.0 - self.tolerance))
        relaxed_high = None if high is None else high * (1.0 + self.tolerance)

        if relaxed_low is not None and years < relaxed_low:
            shortfall = relaxed_low - years
            if shortfall > self.max_shortfall_years:
                return (
                    0.0,
                    "under",
                    f"Requires {band.describe()} ({band.source}); candidate has "
                    f"{years:g} — {shortfall:.1f}y below the ±{self.tolerance:.0%} floor of {relaxed_low:.1f}y",
                )
            # Inside the "worth an opinion" grey zone. Fit decays linearly with
            # the shortfall so it still costs the listing something.
            fade = 1.0 - (shortfall / max(self.max_shortfall_years, 0.01))
            return 0.35 + 0.30 * fade, "under", None

        if relaxed_high is not None and years > relaxed_high:
            excess = years - relaxed_high
            # Being over the ceiling is a much weaker signal than being under the
            # floor: people are hired above the posted band constantly. Only a
            # large overshoot (a real seniority mismatch) is worth penalising,
            # and even then it never hard-rejects here -- the seniority-tier rule
            # below handles the genuine intern-vs-professional cases.
            return max(0.45, 1.0 - 0.08 * excess), "over", None

        return 1.0, "in_band", None

    def _seniority_conflict(self, job: JobListing, band: Optional[ExperienceBand]) -> Optional[str]:
        """Catches the mismatches a score blend would smooth over."""
        tier = _title_seniority_band(job.title)
        if tier is None:
            return None
        years = self.candidate_years

        # Far too senior: a Director role needs a decade, not three years, and no
        # amount of keyword overlap changes that.
        if tier.low is not None and tier.low >= 7.0:
            if years < tier.low - self.max_shortfall_years:
                return (
                    f"Title implies {tier.low:g}+ years ('{tier.raw}'); candidate has {years:g}"
                )

        # Far too junior: applying to internships with three years of professional
        # history burns the daily quota on postings that will not convert.
        if tier.high is not None and years > tier.high * 1.2 + 1.0:
            return f"Title is a {tier.raw}-level posting; candidate has {years:g} years"

        return None

    # -- entry point ---------------------------------------------------------

    def screen(self, job: JobListing) -> PreFilterVerdict:
        """Classifies a posting as reject / evaluate / fast_track, with no LLM call."""
        title = job.title or ""
        reasons: List[str] = []

        lowered_title = title.lower()
        for bad in self.exclude_title_keywords:
            if _contains_word(lowered_title, bad):
                return PreFilterVerdict(
                    decision="reject", score=0,
                    reasons=[f"Title contains excluded keyword '{bad}'"],
                )

        lowered_company = (job.company or "").lower()
        for bad in self.exclude_companies:
            if bad and bad in lowered_company:
                return PreFilterVerdict(
                    decision="reject", score=0,
                    reasons=[f"Company '{job.company}' is on the exclusion list"],
                )

        title_score = self._score_title(title)
        skill_score, matched = self._score_skills(f"{title}\n{job.description}")
        band = self.resolve_band(job)
        exp_score, years_verdict, exp_reject = self._score_experience(band)

        base = PreFilterVerdict(
            decision="evaluate",
            score=0,
            band=band,
            years_verdict=years_verdict,
            title_score=title_score,
            skill_score=skill_score,
            experience_score=exp_score,
            matched_skills=matched,
        )

        if exp_reject:
            base.decision = "reject"
            base.reasons = [exp_reject]
            return base

        conflict = self._seniority_conflict(job, band)
        if conflict:
            base.decision = "reject"
            base.reasons = [conflict]
            return base

        # Nothing in the title and almost nothing in the body matched. This is a
        # posting that landed in the results because LinkedIn's search is fuzzy,
        # not because it is relevant.
        if title_score < 0.20 and skill_score < 0.10:
            base.decision = "reject"
            base.reasons = [
                f"Unrelated posting (title overlap {title_score:.0%}, skill overlap {skill_score:.0%})"
            ]
            return base

        score = 100.0 * (0.42 * title_score + 0.33 * skill_score + 0.25 * exp_score)

        # An exact-band hit on a posting that also matches the title is the
        # strongest cheap signal available; the bonus is what lets a listing
        # reach the fast-track threshold at all.
        if years_verdict == "in_band" and title_score >= 0.6:
            score += 6.0
        if job.is_easy_apply:
            score += 2.0

        base.score = int(max(0, min(100, round(score))))
        reasons.append(f"title {title_score:.0%}, skills {skill_score:.0%}")
        if band is not None:
            reasons.append(f"experience {band.describe()} → {years_verdict}")
        else:
            reasons.append("experience not stated")
        if matched:
            reasons.append("matched: " + ", ".join(matched[:5]))
        base.reasons = reasons

        if base.score < self.reject_below:
            base.decision = "reject"
        elif (
            base.score >= self.fast_track_above
            and years_verdict in ("in_band", "over")
            and title_score >= 0.75
            and skill_score >= 0.35
        ):
            base.decision = "fast_track"
        else:
            base.decision = "evaluate"

        return base
