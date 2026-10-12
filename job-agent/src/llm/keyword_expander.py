"""
Search-keyword expansion.

The keyword list is the ceiling on everything downstream. Onboarding writes
`search.keywords` as verbatim whatever the customer typed into the wizard, and
nothing has ever widened it -- so a candidate who wrote "IT Sox" and "ITGC"
searched LinkedIn for two strings that essentially never appear in a job title,
and the agent reported an empty market. It was not an empty market. It was two
bad queries.

This derives additional titles from the candidate's own profile and their seed
keywords, once, and persists them to the user's config so they are visible and
prunable rather than invented silently on every run.

Two properties matter more than the quality of the suggestions:

- It fails closed. No model, an undeployed proxy, a malformed reply, anything
  unexpected at all -- `expand()` returns nothing and the agent searches exactly
  the seed keywords, which is today's behaviour. A keyword expander that guesses
  when it cannot reach the model would quietly point a whole session at the
  wrong field.
- Seeds always win. The expansions are additive and are searched after every
  seed pair (see `search_grid` in matcher.py). The user's own words are the
  highest-signal input in the system and nothing here may displace them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from src.core.logger import logger
from src.core.models import CandidateProfile
from src.llm.cache import LLMCache
from src.llm.client import LLMClient
from src.llm.prefilter import _contains_word


# Expansions live a long time: they are a function of the candidate's career, not
# of the job market, and `expanded_from` invalidates them the moment either input
# changes. Re-deriving weekly would spend a model call to produce the same list.
CACHE_DAYS = 90.0

# A job title is a short noun phrase. Anything much longer is the model writing a
# sentence, and anything without a letter is not a title at all.
MAX_KEYWORD_CHARS = 40


@dataclass
class ExpansionResult:
    """What `expand` produced, and where it came from."""

    keywords: List[str] = field(default_factory=list)
    rationale: Dict[str, str] = field(default_factory=dict)
    # "model", "cache", or the reason nothing came back. Printed at run start so
    # an empty expansion is never mistaken for "the model had no ideas".
    source: str = "unavailable"

    def __bool__(self) -> bool:
        return bool(self.keywords)


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _norm(text: str) -> str:
    """Comparison form: case and punctuation folded, so 'IT-Audit' == 'it audit'."""
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def filter_keywords(
    candidates: Sequence[Any],
    seeds: Sequence[str],
    exclude_title_keywords: Sequence[str] = (),
    limit: int = 8,
) -> List[str]:
    """
    Applies the guardrails to whatever the model proposed.

    Pure and separately tested, because this is the part that decides where a
    session's time goes. A model that returns "Recruiter" for an auditor, or the
    seed keyword back with different capitalisation, costs real search minutes.
    """
    seen = {_norm(s) for s in seeds if _norm(s)}
    excluded = [str(x).strip().lower() for x in exclude_title_keywords if str(x).strip()]
    out: List[str] = []

    for raw in candidates:
        keyword = _clean(raw)
        if not keyword or len(keyword) > MAX_KEYWORD_CHARS:
            continue
        if not re.search(r"[a-zA-Z]", keyword):
            continue

        norm = _norm(keyword)
        if not norm or norm in seen:
            continue
        # Whole-word, exactly as the pre-filter reads the same list
        # (`_contains_word` in prefilter.py). A suggestion of "Audit Intern"
        # must die here for the same reason the posting would -- but a bare
        # substring test would also kill "Internal Audit", which for this
        # profile is the single most valuable title the expander can produce.
        if any(_contains_word(keyword.lower(), bad) for bad in excluded):
            continue

        seen.add(norm)
        out.append(keyword)
        if len(out) >= max(0, limit):
            break

    return out


class KeywordExpander:
    """Derives extra search titles from a candidate profile and their seed keywords."""

    def __init__(self, llm: LLMClient, cache: Optional[LLMCache] = None):
        self.llm = llm
        self.cache = cache

    # -- inputs --------------------------------------------------------------

    @staticmethod
    def fingerprint(profile: CandidateProfile, seeds: Sequence[str]) -> str:
        """
        Identifies the inputs an expansion was derived from.

        Stored beside the result as `search.expanded_from`. When the customer
        edits their job titles in Settings, or their profile changes, this stops
        matching and the expansions are re-derived instead of quietly describing
        the career they used to have.
        """
        roles = sorted({_norm(e.role) for e in (profile.experience or []) if _norm(e.role)})
        skills = sorted({_norm(s) for s in (profile.skills.all_skills() or []) if _norm(s)})
        seed_list = sorted({_norm(s) for s in seeds if _norm(s)})
        blob = json.dumps([seed_list, roles, skills], sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _variables(profile: CandidateProfile, seeds: Sequence[str], limit: int) -> Dict[str, Any]:
        roles = [_clean(e.role) for e in (profile.experience or []) if _clean(e.role)]
        return {
            "seed_keywords": ", ".join(_clean(s) for s in seeds if _clean(s)),
            "current_roles": ", ".join(roles[:6]) or "(not provided)",
            "skills": ", ".join(_clean(s) for s in (profile.skills.all_skills() or [])[:40])
            or "(not provided)",
            "summary": _clean(profile.professional_summary)[:900] or "(not provided)",
            "max_keywords": limit,
        }

    # -- entry point ---------------------------------------------------------

    def expand(
        self,
        profile: CandidateProfile,
        seeds: Sequence[str],
        limit: int = 8,
        exclude_title_keywords: Sequence[str] = (),
    ) -> ExpansionResult:
        """
        Returns additional search titles. Never raises, never guesses.

        Every failure path returns an empty result carrying the reason, so the
        caller logs something true rather than an empty list that looks like a
        considered opinion.
        """
        seeds = [s for s in (seeds or []) if _clean(s)]
        if not seeds:
            return ExpansionResult(source="no seed keywords to expand from")
        if limit <= 0:
            return ExpansionResult(source="expansion limit is zero")

        key = f"keyword_expansion:{self.fingerprint(profile, seeds)}:{limit}"

        if self.cache is not None:
            cached = self.cache.get(key, max_age_days=CACHE_DAYS)
            if cached:
                return ExpansionResult(
                    keywords=list(cached.get("keywords") or []),
                    rationale=dict(cached.get("rationale") or {}),
                    source="cache",
                )

        try:
            payload = self.llm.run_task_json(
                "keyword_expansion", self._variables(profile, seeds, limit)
            )
        except Exception as exc:
            logger.warning(f"Keyword expansion unavailable ({exc}); using your keywords as entered.")
            return ExpansionResult(source=f"error: {exc}")

        # `_parse_json` hands back {} for unparseable output, and the client's
        # `_fallback_response` marks an unreachable model rather than inventing
        # an answer. Both mean "no opinion", and neither may become keywords.
        if not payload or payload.get("llm_unavailable"):
            logger.warning(
                "Keyword expansion unavailable (the AI service has no 'keyword_expansion' "
                "task yet); using your keywords as entered."
            )
            return ExpansionResult(source="model unavailable")

        raw = payload.get("keywords")
        if isinstance(raw, dict):
            # Tolerated shape: {"Internal Audit": "why", ...}
            proposed = list(raw.keys())
            rationale = {_clean(k): _clean(v) for k, v in raw.items()}
        elif isinstance(raw, list):
            proposed, rationale = [], {}
            for item in raw:
                if isinstance(item, dict):
                    title = _clean(item.get("keyword") or item.get("title"))
                    if title:
                        proposed.append(title)
                        rationale[title] = _clean(item.get("reason") or item.get("rationale"))
                else:
                    proposed.append(_clean(item))
        else:
            return ExpansionResult(source="model returned no keywords")

        keywords = filter_keywords(proposed, seeds, exclude_title_keywords, limit)
        if not keywords:
            return ExpansionResult(source="every suggestion was filtered out")

        result = ExpansionResult(
            keywords=keywords,
            rationale={k: v for k, v in rationale.items() if k in keywords and v},
            source="model",
        )
        if self.cache is not None:
            self.cache.put(
                key,
                "keyword_expansion",
                {"keywords": result.keywords, "rationale": result.rationale},
            )
        return result


def build_keyword_expander(config, llm: LLMClient) -> KeywordExpander:
    """Expander sharing the on-disk cache the matcher and form solver already use."""
    cache = LLMCache(config.general.database_path, enabled=config.matching.cache_enabled)
    return KeywordExpander(llm, cache=cache)
