"""
Evaluator for matching Job Descriptions against the Candidate Profile.

Three passes, cheapest first:

  1. `JobPreFilter` -- title, skill and experience-band arithmetic, no network.
     Rejects the obvious mismatches and fast-tracks the obvious fits.
  2. `LLMCache` -- the same posting reached through a different search keyword,
     or on a later day, is scored once and reused.
  3. The model, for everything genuinely undecided.

Only the third costs a round-trip, and on a normal results page it now runs on
roughly a third of the listings instead of all of them.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, Optional

from src.core.models import CandidateProfile, JobListing, MatchEvaluation
from src.core.logger import logger
from src.llm.client import LLMClient
from src.llm.cache import LLMCache, make_key, profile_fingerprint
from src.llm.prefilter import JobPreFilter, PreFilterVerdict


# Blocks that appear in most postings, carry no signal about fit, and cost
# tokens (and therefore latency) in every single evaluation.
_BOILERPLATE = re.compile(
    r"(equal opportunity employer|we are an equal|without regard to race|"
    r"reasonable accommodation|e-verify|drug[- ]free workplace|"
    r"applicants will receive consideration|protected veteran|"
    r"privacy policy|cookie policy|by applying (?:to|for) this)"
    r".*?(?=\n\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def compress_description(description: str, limit: int = 3000) -> str:
    """
    Strips legal boilerplate and collapses whitespace before the model sees it.

    A LinkedIn description is routinely half EEO statement and benefits copy.
    Truncating at a fixed 4000 characters therefore often cut the requirements
    section off while keeping the disclaimers, which is exactly backwards for
    scoring fit -- and made the call slower for the privilege.
    """
    if not description:
        return ""
    text = _BOILERPLATE.sub(" ", description)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    # Keep the head (role summary, requirements) and the tail (often the
    # "what you'll need" list), dropping the middle rather than the end.
    head = text[: int(limit * 0.7)]
    tail = text[-int(limit * 0.3):]
    return f"{head}\n[...]\n{tail}"


class JobMatcher:
    def __init__(
        self,
        llm_client: LLMClient,
        prefilter: Optional[JobPreFilter] = None,
        cache: Optional[LLMCache] = None,
        blend: float = 0.25,
        cache_days: float = 14.0,
        tolerance: float = 0.20,
    ):
        self.llm = llm_client
        self.prefilter = prefilter
        self.cache = cache
        self.blend = max(0.0, min(1.0, blend))
        self.cache_days = cache_days
        self.tolerance = tolerance
        self._profile_key: Optional[str] = None
        # Session counters, printed at the end of a run so the saving is visible
        # rather than merely asserted.
        self.stats = {"prefilter_reject": 0, "fast_track": 0, "cached": 0, "llm": 0}

    # -- helpers -------------------------------------------------------------

    def _fingerprint(self, profile: CandidateProfile) -> str:
        if self._profile_key is None:
            self._profile_key = profile_fingerprint(profile)
        return self._profile_key

    def screen(self, job: JobListing) -> Optional[PreFilterVerdict]:
        """Runs the deterministic pass alone, for callers that want it early."""
        return self.prefilter.screen(job) if self.prefilter else None

    def _from_prefilter(self, verdict: PreFilterVerdict, recommended: bool) -> MatchEvaluation:
        tier = "Strong Fit" if verdict.score >= 80 else ("Moderate Fit" if verdict.score >= 60 else "Weak Fit")
        return MatchEvaluation(
            match_score=verdict.score,
            is_recommended=recommended,
            match_tier=tier,
            key_matching_skills=verdict.matched_skills,
            missing_or_gap_skills=[],
            summary_reasoning=verdict.summary(),
        )

    # -- entry point ---------------------------------------------------------

    def evaluate(
        self,
        job: JobListing,
        profile: CandidateProfile,
        min_score: int = 75,
        verdict: Optional[PreFilterVerdict] = None,
    ) -> MatchEvaluation:
        """
        Scores one posting.

        `verdict` may be supplied by a caller that already ran the pre-filter --
        discovery does, because it decides how long to spend reading the posting
        based on the outcome. When it is None the pre-filter runs here.
        """
        if verdict is None and self.prefilter is not None:
            verdict = self.prefilter.screen(job)

        if verdict is not None:
            if verdict.rejected:
                self.stats["prefilter_reject"] += 1
                return self._from_prefilter(verdict, recommended=False)
            if verdict.decision == "fast_track":
                self.stats["fast_track"] += 1
                # Still checked against min_score: fast-tracking says "no model
                # call needed", not "ignore the threshold". Raising min_score
                # above fast_track_above must keep meaning what it says.
                return self._from_prefilter(verdict, recommended=verdict.score >= min_score)

        compressed = compress_description(job.description)
        cache_key = make_key(
            "match", self._fingerprint(profile), job.title, job.company, compressed, min_score
        )
        if self.cache is not None:
            cached = self.cache.get(cache_key, max_age_days=self.cache_days)
            if cached:
                self.stats["cached"] += 1
                try:
                    return MatchEvaluation(**cached)
                except Exception:
                    pass  # shape changed between versions; fall through and re-ask

        self.stats["llm"] += 1
        evaluation = self._ask_model(job, profile, min_score, verdict, compressed)

        if self.cache is not None:
            self.cache.put(cache_key, "match", evaluation.model_dump())
        return evaluation

    async def evaluate_async(
        self,
        job: JobListing,
        profile: CandidateProfile,
        min_score: int = 75,
        verdict: Optional[PreFilterVerdict] = None,
    ) -> MatchEvaluation:
        """
        The same evaluation, off the event loop.

        The provider SDKs are synchronous, so calling `evaluate` directly from
        async code blocked every other coroutine for the duration of the HTTP
        request -- including the idle-drift task that is supposed to be running
        during exactly that window. Handing it to a worker thread lets the
        caller read the posting while the model is thinking, which turns
        `read + score` into `max(read, score)`.
        """
        return await asyncio.to_thread(self.evaluate, job, profile, min_score, verdict)

    # -- the expensive path --------------------------------------------------

    def _ask_model(
        self,
        job: JobListing,
        profile: CandidateProfile,
        min_score: int,
        verdict: Optional[PreFilterVerdict],
        compressed: str,
    ) -> MatchEvaluation:
        skills_str = ", ".join(profile.skills.all_skills())
        exp_summary = "; ".join([
            f"{exp.role} at {exp.company} ({exp.start_date} - {exp.end_date}): {', '.join(exp.highlights[:2])}"
            for exp in profile.experience
        ])

        years = profile.screening_answers.total_years_experience
        band = verdict.band if verdict else None
        if band is not None:
            low = band.low if band.low is not None else 0.0
            high = band.high
            relaxed_low = max(0.0, low * (1 - self.tolerance))
            relaxed_high = f"{high * (1 + self.tolerance):.1f}" if high is not None else "no ceiling"
            experience_note = (
                f"This posting asks for {band.describe()} (read from the {band.source}). "
                f"The candidate has {years}. Applying a +/-{self.tolerance:.0%} tolerance, "
                f"an acceptable range for this role is {relaxed_low:.1f} to {relaxed_high} years. "
                f"The candidate is {'INSIDE' if verdict.years_verdict == 'in_band' else 'OUTSIDE'} "
                f"that range ({verdict.years_verdict}). Do NOT reduce the score for a years gap "
                f"that falls inside the tolerance band; only a gap beyond it is a real concern, "
                f"and being above the band is not a concern at all."
            )
        else:
            experience_note = (
                f"This posting does not state a years requirement. The candidate has {years} "
                f"years. Do not invent a requirement or penalise the candidate for one."
            )

        signals = (
            f"Deterministic pre-screen: title relevance {verdict.title_score:.0%}, "
            f"skill overlap {verdict.skill_score:.0%}, heuristic score {verdict.score}/100."
            if verdict else "Deterministic pre-screen: not run."
        )

        # Variables only. The template that consumes them lives on the licence
        # proxy, so a customer's machine never holds the rubric -- and the old
        # KeyError dance around a stale local prompts.yaml is gone with it,
        # because the side that formats the prompt is the side that owns it.
        data: Dict[str, Any] = self.llm.run_task_json(
            "job_matcher",
            {
                "candidate_name": profile.personal_info.full_name,
                "summary": profile.professional_summary,
                "skills": skills_str,
                "experience": exp_summary,
                "years_experience": years,
                "job_title": job.title,
                "company_name": job.company,
                "job_location": job.location,
                "job_description": compressed,
                "min_score": min_score,
                "experience_note": experience_note,
                "prefilter_signals": signals,
            },
        ) or {}

        if data.get("llm_unavailable"):
            # No model reachable. Scoring from the deterministic pass is honest;
            # inventing a number is not, and this path used to invent 85.
            logger.warning("[yellow]LLM unavailable — scoring from deterministic signals.[/yellow]")
            if verdict is not None:
                return self._from_prefilter(verdict, recommended=verdict.score >= min_score)
            return MatchEvaluation(
                match_score=0, is_recommended=False, match_tier="Weak Fit",
                summary_reasoning="LLM unavailable and no deterministic signal to fall back on.",
            )

        try:
            score = int(float(data.get("match_score", 70)))
        except (TypeError, ValueError):
            score = 70
        score = max(0, min(100, score))

        # Blend in the deterministic score. The model is the better judge of
        # substance but its scores drift a few points run to run; anchoring them
        # to arithmetic that cannot drift makes the min_score threshold mean the
        # same thing on Tuesday as it did on Monday.
        if verdict is not None and self.blend > 0:
            score = int(round(score * (1 - self.blend) + verdict.score * self.blend))

        raw_rec = data.get("is_recommended")
        is_rec = bool(raw_rec) if isinstance(raw_rec, bool) else score >= min_score
        # The blend can move a score across the threshold; the threshold wins,
        # otherwise the printed score and the decision disagree.
        if is_rec and score < min_score:
            is_rec = False
        elif not is_rec and score >= min_score and raw_rec is None:
            is_rec = True

        return MatchEvaluation(
            match_score=score,
            is_recommended=is_rec,
            match_tier=data.get("match_tier", "Moderate Fit"),
            key_matching_skills=data.get("key_matching_skills", []) or (verdict.matched_skills if verdict else []),
            missing_or_gap_skills=data.get("missing_or_gap_skills", []),
            summary_reasoning=data.get("summary_reasoning", "Evaluated based on profile alignment."),
        )

    def report(self) -> str:
        """One-line accounting of where the evaluations went."""
        total = sum(self.stats.values())
        if not total:
            return ""
        saved = self.stats["prefilter_reject"] + self.stats["fast_track"] + self.stats["cached"]
        return (
            f"Evaluated {total} listings: {self.stats['llm']} via LLM, "
            f"{self.stats['prefilter_reject']} pre-filtered out, "
            f"{self.stats['fast_track']} fast-tracked, {self.stats['cached']} from cache "
            f"({saved}/{total} needed no model call)."
        )


def build_matcher(config, profile: CandidateProfile, llm: LLMClient) -> JobMatcher:
    """
    Assembles matcher + pre-filter + cache from config.

    One place, because every entry point in the CLI needs the same three objects
    wired the same way and they were previously constructing a bare JobMatcher.
    """
    m = config.matching
    cache = LLMCache(config.general.database_path, enabled=m.cache_enabled)

    prefilter = None
    if m.prefilter_enabled:
        prefilter = JobPreFilter(
            profile=profile,
            search_keywords=list(config.search.keywords) + list(config.naukri.keywords or []),
            tolerance=m.experience_tolerance,
            max_shortfall_years=m.max_years_shortfall,
            reject_below=m.prefilter_reject_below,
            fast_track_above=m.fast_track_above,
            exclude_title_keywords=m.exclude_title_keywords,
            exclude_companies=m.exclude_companies,
        )

    return JobMatcher(
        llm,
        prefilter=prefilter,
        cache=cache,
        blend=m.prefilter_blend,
        cache_days=m.cache_days,
        tolerance=m.experience_tolerance,
    )


def build_form_solver(config, llm: LLMClient):
    """Form solver sharing the same on-disk answer cache."""
    from src.llm.form_solver import FormSolver

    cache = LLMCache(config.general.database_path, enabled=config.matching.cache_enabled)
    return FormSolver(llm, cache=cache)


def search_grid(config, keywords=None) -> list:
    """
    The (keyword, location) pairs to work through, in the order to work them.

    The grid is 54 pairs in the shipped config and a session gets through a
    fraction of it, so a fixed `for kw: for loc:` walk meant the last two thirds
    of the keyword list were never searched at all -- an entire category of
    relevant jobs invisible to the agent forever. Locations are interleaved so a
    single keyword cannot consume a whole session, and the starting offset
    advances by day so every pair comes up within a week.

    `keywords` overrides the configured list, which is how derived keywords get
    their own block: callers build `search_grid(config) + search_grid(config,
    expansions)` so every seed pair is worked before any derived one, and each
    block still rotates on its own. The customer's own words are the strongest
    signal in the system and must never be pushed out of a session by a guess.
    """
    import datetime as _dt

    source = config.search.keywords if keywords is None else keywords
    keywords = [k for k in (source or []) if k and k.strip()]
    locations = [l for l in config.search.locations if l and l.strip()]
    if not keywords or not locations:
        return []

    if config.search.interleave_locations:
        # Round-robin over locations: kw0/loc0, kw1/loc1, kw2/loc2, ... so an
        # early stop still leaves a spread across both axes rather than every
        # location for one keyword.
        pairs = [
            (keywords[i % len(keywords)], locations[(i // len(keywords)) % len(locations)])
            for i in range(len(keywords) * len(locations))
        ]
    else:
        pairs = [(kw, loc) for kw in keywords for loc in locations]

    if config.search.rotate_search_grid and pairs:
        # Date-derived, not random: a restart must not re-roll the offset and
        # re-run the pairs this machine already worked through today.
        offset = _dt.date.today().toordinal() % len(pairs)
        pairs = pairs[offset:] + pairs[:offset]

    return pairs
