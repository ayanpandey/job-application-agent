"""
Configuration loader for the LinkedIn AI Agent.
Loads settings from YAML files and environment variables.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from src.core.models import CandidateProfile
from src.core import paths

# Load .env file if present
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _resolve_config(name: str) -> Path:
    """
    Find a config file, preferring the user's copy over the shipped default.

    An installed app has two config roots: the read-only one inside the bundle
    holding the defaults we ship, and the writable one under the user's data
    directory holding what the onboarding wizard wrote and what they have edited
    since. The user's copy always wins; the bundled one is the seed and the
    fallback. From a source checkout both resolve to the repo, so nothing about
    development changes.
    """
    user_copy = paths.config_dir() / name
    if user_copy.exists():
        return user_copy
    return paths.bundled_dir() / "config" / name


class GeneralConfig(BaseModel):
    dry_run: bool = True
    # Applies when config.yaml omits the key -- which is what a frozen build with a
    # stale bundled config does. Defaulting this to True there would silently put an
    # interactive prompt back into an unattended run and stall it on the first job.
    human_in_the_loop: bool = False
    database_path: str = "./data/jobs.db"


class SearchConfig(BaseModel):
    # No default job titles. A wrong guess here is worse than none: the agent
    # would search, score and apply against somebody else's field. Onboarding
    # always writes these, and an empty list simply produces no searches.
    keywords: List[str] = Field(default_factory=list)
    locations: List[str] = Field(default_factory=lambda: ["Remote"])
    easy_apply_only: bool = True
    date_posted: str = "past-week"
    experience_levels: List[str] = Field(default_factory=lambda: ["entry_level", "associate", "mid_senior"])
    min_match_score: int = 25
    max_results_per_search: int = 20
    # The keyword x location grid is far larger than one session can work
    # through, and walking it in the same order every day meant the tail of the
    # list was never reached. Rotating the starting point by date covers all of
    # it over a week; interleaving stops one keyword from eating a whole session.
    rotate_search_grid: bool = True
    interleave_locations: bool = True
    # LinkedIn serves 25 results a page. Discovery used to read only whatever the
    # first page had mounted on arrival -- five or six of them -- so everything
    # past the fold was never seen at all. Cap the paging so one keyword in one
    # city cannot consume an entire session.
    max_pages_per_search: int = 3
    # Re-run a search with the filters relaxed when it surfaces almost nothing.
    # Easy Apply x date x seniority intersect multiplicatively, and on a niche
    # title in a single city the intersection is routinely empty; an empty result
    # is far more often a filter artefact than a statement about the market.
    widen_when_thin: bool = True
    widen_below_results: int = 8
    # How far to go with jobs that are not LinkedIn Easy Apply:
    #   "off"      -- ignore them entirely (the default)
    #   "discover" -- the widest widening tier drops the Easy Apply filter, so
    #                 they are scored and stored and can be worked through
    #                 recruiter outreach, but no portal is ever opened
    #   "apply"    -- as above, and matches are completed on the company's own
    #                 ATS by ExternalApplyEngine
    # Surfacing a listing and submitting an application to a third party under
    # the candidate's name are different levels of risk, so they are different
    # settings rather than one boolean.
    external_apply: str = "off"

    # Extra search titles derived from the candidate's profile and seed keywords.
    # Written back to the user's own config.yaml so they can be read and pruned;
    # `expanded_from` records the inputs they came from and re-derives them when
    # those change. See src/llm/keyword_expander.py.
    keyword_expansion: bool = True
    max_expanded_keywords: int = 8
    expanded_keywords: List[str] = Field(default_factory=list)
    expanded_from: str = ""

    @property
    def external_apply_enabled(self) -> bool:
        """True when non-Easy-Apply listings should be surfaced at all."""
        return str(self.external_apply or "off").lower() in ("discover", "apply")

    @property
    def external_apply_submits(self) -> bool:
        """True only when the agent may complete an application on a company portal."""
        return str(self.external_apply or "off").lower() == "apply"


class MatchingConfig(BaseModel):
    """
    Controls the deterministic pre-screen that runs before any LLM call.

    See src/llm/prefilter.py. The two thresholds are the whole tuning surface:
    raise `prefilter_reject_below` to spend fewer model calls on marginal
    listings, raise `fast_track_above` to send more of them to the model anyway.
    """
    prefilter_enabled: bool = True
    # Deterministic score under which a listing is rejected with no model call.
    prefilter_reject_below: int = 18
    # ...and over which it is accepted with no model call. Deliberately high:
    # this bypasses the only component that reads the description for meaning.
    fast_track_above: int = 88
    # Years-of-experience tolerance. A posting asking for N years is treated as
    # open to N*(1-tolerance) .. N*(1+tolerance).
    experience_tolerance: float = 0.20
    # How far below the relaxed floor is still worth a model opinion rather than
    # an immediate reject. In years, because that is how the gap is felt.
    max_years_shortfall: float = 2.0
    # Hard exclusions, substring-matched against title and company.
    exclude_title_keywords: List[str] = Field(default_factory=list)
    exclude_companies: List[str] = Field(default_factory=list)
    # Ceiling on model calls per search. Cards are still scanned up to
    # max_results_per_search; this only bounds the expensive part.
    max_llm_evaluations_per_search: int = 12
    # How much of the final score comes from the deterministic pass. Small, but
    # enough to stop the threshold drifting with model temperature.
    prefilter_blend: float = 0.25
    # Cached evaluations older than this are re-scored; postings get edited.
    cache_days: float = 14.0
    cache_enabled: bool = True


class SpeedConfig(BaseModel):
    """
    Pacing of the discretionary pauses, separate from the anti-detection layer.

    `action_pace` scales only the waits that exist to look unhurried -- between
    form steps, after filling a field. It does NOT touch mouse kinematics,
    typing dynamics, session rhythm, breaks or active hours, which are what
    actually carry the account risk (see src/browser/human_behavior.py and
    session_rhythm.py). Floors in the calling code keep a low value from
    producing burst timing no person could generate.
    """
    action_pace: float = 0.55
    # Seconds spent on a description the agent intends to act on, versus one the
    # pre-filter already rejected. Skimming a listing and moving on in two
    # seconds is what a person does with an obviously wrong result; spending ten
    # seconds "reading" it was pure waste dressed up as realism.
    read_seconds_match: List[float] = Field(default_factory=lambda: [4.0, 9.0])
    read_seconds_skim: List[float] = Field(default_factory=lambda: [1.2, 2.8])
    # Resolve every unanswered question on a form step in one request.
    batch_form_questions: bool = True
    # Run the match evaluation concurrently with reading the posting, so the two
    # overlap instead of queueing.
    overlap_evaluation_with_reading: bool = True

    @field_validator("read_seconds_match", "read_seconds_skim")
    @classmethod
    def _two_ascending_values(cls, v: List[float]) -> List[float]:
        """
        These are unpacked as `lo, hi = ...` at the call site, so a one-element
        or reversed list from a hand-edited config would fail inside a browser
        loop rather than at load time. Normalise here instead.
        """
        values = [float(x) for x in (v or []) if x is not None]
        if not values:
            return [1.5, 3.0]
        if len(values) == 1:
            return [values[0], values[0] + 1.0]
        lo, hi = min(values[:2]), max(values[:2])
        return [max(0.3, lo), max(lo + 0.3, hi)]


class NaukriConfig(BaseModel):
    """
    Naukri settings.

    This section existed in config.yaml but had no model behind it, so
    `AppConfig` silently dropped it and every Naukri command fell back to the
    hardcoded defaults in the CLI -- which meant the audit profile was searching
    Naukri for "AI Engineer" and "Python Developer".
    """
    enabled: bool = True
    user_data_dir: str = "./data/naukri_browser_profile"
    keywords: List[str] = Field(default_factory=list)
    locations: List[str] = Field(default_factory=list)
    experience_years: Optional[int] = None
    min_salary_lakhs: Optional[float] = None
    min_match_score: int = 25
    max_applications_per_day: int = 30
    auto_boost_profile: bool = True


class LimitsConfig(BaseModel):
    max_applications_per_day: int = 20
    max_outreach_messages_per_day: int = 15
    # Listings opened and scored per day. Tracked separately from applications so a
    # long browsing session cannot quietly eat the application allowance; reading far
    # more postings than you apply to is also what real job seeking looks like.
    max_jobs_evaluated_per_day: int = 80
    min_delay_between_actions_seconds: float = 3.5
    max_delay_between_actions_seconds: float = 8.0


class BrowserConfig(BaseModel):
    headless: bool = False
    user_data_dir: str = "./data/browser_profile"

    # slow_mo MUST stay 0. Playwright's slow_mo delays every individual driver
    # operation, and page.mouse.move() is an operation -- so it applies once per
    # trajectory sample, not once per click. Measured on this build: at slow_mo=120 a
    # 60-point WindMouse path takes 7.9s instead of 0.82s and the events arrive
    # 134.1ms apart with a standard deviation of 1.75ms. That is a 7.5Hz pointer with
    # 1.3% relative jitter, against 13.8ms +/- 2.9ms (21%) with slow_mo off. Real mice
    # and trackpads report at 60-125Hz. An inter-event histogram that spikes on a
    # single value is a stronger and far cheaper signal than any trajectory shape, so a
    # non-zero slow_mo defeats the entire human_behavior module. Pacing belongs in
    # human_behavior/session_rhythm, which vary it; slow_mo cannot.
    slow_mo: int = 0
    timeout_ms: int = 30000

    # Both empty by default, which means "use the host's own values".
    #
    # Chromium already reports the machine's real locale and timezone, and a
    # genuine value cannot contradict the IP the request arrives from. Setting
    # either of these pins the browser to something the machine may not be:
    # the previous build hardcoded Asia/Kolkata, so every customer outside India
    # ran a browser whose clock disagreed with its own network location, which
    # is a single deterministic check rather than a statistical one.
    #
    # Only set them if you are also controlling where the traffic appears to
    # come from, and set them to match it.
    timezone_id: str = ""
    locale: str = ""


class RhythmSettings(BaseModel):
    """
    Session-level pacing. See src/browser/session_rhythm.py for why these matter more
    than per-action realism once a platform aggregates behaviour per account.
    """
    enabled: bool = True
    active_hours_start: int = 8
    active_hours_end: int = 23
    session_minutes_min: float = 25.0
    session_minutes_max: float = 70.0
    long_break_minutes_min: float = 6.0
    long_break_minutes_max: float = 22.0
    actions_between_micro_breaks_min: int = 4
    actions_between_micro_breaks_max: int = 11
    micro_break_seconds_min: float = 35.0
    micro_break_seconds_max: float = 180.0
    glance_away_probability: float = 0.22
    glance_away_seconds_min: float = 12.0
    glance_away_seconds_max: float = 95.0
    daily_quota_jitter: float = 0.35
    rest_day_probability: float = 0.10


class LLMConfig(BaseModel):
    # "proxy" is what every shipped build runs: calls go to the licence server,
    # which holds the Google credentials and the prompt templates. The direct
    # providers remain for development and for a bring-your-own-key build.
    provider: str = "proxy"  # "proxy" | "gemini" | "openai" | "anthropic" | "groq" | "ollama"
    model: str = "gemini-3.7-flash"
    temperature: float = 0.2
    max_tokens: int = 1500
    api_key: Optional[str] = None
    ollama_base_url: str = "http://localhost:11434"


class OutreachSettings(BaseModel):
    auto_send_connection: bool = False
    max_note_length: int = 200
    target_recruiter_titles: List[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    speed: SpeedConfig = Field(default_factory=SpeedConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    outreach: OutreachSettings = Field(default_factory=OutreachSettings)
    rhythm: RhythmSettings = Field(default_factory=RhythmSettings)
    naukri: NaukriConfig = Field(default_factory=NaukriConfig)

    def build_rhythm_config(self):
        """Flattens the YAML-friendly settings into the RhythmConfig the governor uses."""
        from src.browser.session_rhythm import RhythmConfig

        r = self.rhythm
        return RhythmConfig(
            enabled=r.enabled,
            active_hours=(r.active_hours_start, r.active_hours_end),
            session_minutes=(r.session_minutes_min, r.session_minutes_max),
            long_break_minutes=(r.long_break_minutes_min, r.long_break_minutes_max),
            actions_between_micro_breaks=(
                r.actions_between_micro_breaks_min,
                r.actions_between_micro_breaks_max,
            ),
            micro_break_seconds=(r.micro_break_seconds_min, r.micro_break_seconds_max),
            glance_away_probability=r.glance_away_probability,
            glance_away_seconds=(r.glance_away_seconds_min, r.glance_away_seconds_max),
            daily_quota_jitter=r.daily_quota_jitter,
            rest_day_probability=r.rest_day_probability,
        )


def load_yaml(file_path: Path) -> Dict[str, Any]:
    if not file_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_app_config(config_path: Optional[Path] = None) -> AppConfig:
    path = config_path or _resolve_config("config.yaml")
    data = load_yaml(path)
    
    # Override LLM settings with env vars if present
    env_provider = os.getenv("LLM_PROVIDER")
    if env_provider:
        data.setdefault("llm", {})["provider"] = env_provider
    
    app_config = AppConfig(**data)
    
    # Set API Key based on provider
    provider = app_config.llm.provider.lower()
    if provider == "gemini":
        app_config.llm.api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    elif provider == "openai":
        app_config.llm.api_key = os.getenv("OPENAI_API_KEY")
    elif provider == "anthropic":
        app_config.llm.api_key = os.getenv("ANTHROPIC_API_KEY")
    elif provider == "groq":
        app_config.llm.api_key = os.getenv("GROQ_API_KEY")
    
    if os.getenv("OLLAMA_BASE_URL"):
        app_config.llm.ollama_base_url = os.getenv("OLLAMA_BASE_URL")

    # Re-root everything the app writes to into the per-user data directory.
    # The shipped config.yaml carries repo-relative paths like "./data/jobs.db",
    # which in an installed build would resolve next to the executable -- read-only
    # inside a macOS .app, and redirected into a VirtualStore on Windows.
    paths.ensure_dirs()
    app_config.general.database_path = str(
        paths.resolve_writable(app_config.general.database_path, paths.database_path())
    )
    app_config.browser.user_data_dir = str(
        paths.resolve_writable(app_config.browser.user_data_dir, paths.browser_profile_dir("linkedin"))
    )
    app_config.naukri.user_data_dir = str(
        paths.resolve_writable(app_config.naukri.user_data_dir, paths.browser_profile_dir("naukri"))
    )

    return app_config


def save_expanded_keywords(keywords: List[str], fingerprint: str) -> bool:
    """
    Records derived search keywords in the user's own config.yaml.

    Written to `paths.app_config_path()` and never to the bundled seed, which is
    documented as first-run-only and is read-only in an installed build anyway.
    The file is updated in place: the wizard, the pacing settings and everything
    else in it have to survive this.

    Persisting rather than holding it in memory is the whole point of the
    feature -- keywords the agent searches on the customer's behalf should be
    somewhere they can read them and delete the ones they disagree with.
    Returns False if it could not be written; that is not fatal, it just means
    the expansion is re-derived next run.
    """
    from src.core.logger import logger

    try:
        paths.ensure_dirs()
        path = paths.app_config_path()
        data = {}
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        search = data.setdefault("search", {})
        search["expanded_keywords"] = list(keywords)
        search["expanded_from"] = fingerprint

        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )
        return True
    except Exception as exc:
        logger.debug(f"Could not persist expanded keywords: {exc}")
        return False


def load_candidate_profile(profile_path: Optional[Path] = None) -> CandidateProfile:
    path = profile_path or _resolve_config("profile.yaml")
    data = load_yaml(path)
    return CandidateProfile(**data)


def load_prompts(prompts_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Development-only prompt loading.

    Shipped builds run the "proxy" provider, where templates are held by the
    licence server and this file is deliberately absent from the bundle. Only
    the direct-provider path calls this, so a missing file here is a normal
    state for a customer install, not an error.
    """
    path = prompts_path or _resolve_config("prompts.yaml")
    return load_yaml(path)
