"""
Job Discovery Module: Searches LinkedIn for relevant listings, scrapes descriptions,
evaluates candidate fit with LLM, and records opportunities in the database.
Includes anti-detection human warm-up, reading dynamics, non-linear card scanning,
and stochastic distraction browsing.
"""

import asyncio
import re
import random
import urllib.parse
from typing import Any, Awaitable, Callable, List, Optional
from bs4 import BeautifulSoup
from playwright.async_api import Page

from src.core.config import AppConfig, CandidateProfile
from src.core.database import Database
from src.core.logger import logger, console
from src.core.models import JobListing, JobStatus
from src.llm.matcher import JobMatcher
from src.browser.utils import (
    human_delay,
    paced_delay,
)
from src.browser.human_behavior import (
    organic_click,
    organic_mouse_move,
    organic_scroll,
    organic_scroll_over,
    OrganicIdleDrift,
    simulate_human_reading,
    perform_warm_up_browsing,
    explore_random_company_page,
    take_natural_human_break,
    human_navigate,
    human_go_back,
)
from src.browser.session_rhythm import SessionRhythm


# LinkedIn serves 25 results per page. The number is theirs, not a tuning knob:
# the `start` query parameter is an offset into that fixed page size.
RESULTS_PER_PAGE = 25

_CARD_SELECTOR = "li.jobs-search-results__list-item, div.job-card-container, div[data-job-id]"
_LIST_SELECTOR = ".jobs-search-results-list, .scaffold-layout__list"


def _describe_filters(filters: dict) -> str:
    """Human-readable summary of one widening tier, for the run log."""
    parts = []
    if "date_posted" in filters:
        parts.append(f"posted {filters['date_posted'].replace('-', ' ')}")
    if "experience_levels" in filters and not filters["experience_levels"]:
        parts.append("any seniority")
    if filters.get("easy_apply_only") is False:
        parts.append("including external applications")
    return ", ".join(parts) or "configured filters"


def _build_scan_order(count: int) -> List[int]:
    """
    Produces a human scan order over the results list.

    A strict 0,1,2,...,N walk is a strong sequence signal: no person opens every
    listing in perfect order without ever skipping one, doubling back to something
    they glanced past, or jumping ahead to a title that caught their eye. This keeps
    the overall top-to-bottom bias -- people do read downward -- while introducing
    local reordering, occasional skips and the odd backtrack.
    """
    if count <= 2:
        return list(range(count))

    remaining = list(range(count))
    order: List[int] = []
    skipped: List[int] = []

    while remaining:
        roll = random.random()

        if roll < 0.68:
            # Usual case: take the next listing down the page.
            order.append(remaining.pop(0))
        elif roll < 0.84 and len(remaining) > 1:
            # Eye jumped to a title slightly further down.
            jump = random.randint(1, min(3, len(remaining) - 1))
            order.append(remaining.pop(jump))
        elif roll < 0.93 and len(remaining) > 1:
            # Scrolled past one; may come back to it later.
            skipped.append(remaining.pop(0))
        else:
            # Backtrack to something skipped earlier.
            if skipped:
                order.append(skipped.pop(random.randrange(len(skipped))))
            else:
                order.append(remaining.pop(0))

    # Every skipped listing is still revisited before the batch ends -- the point is to
    # break the *order*, not to lose coverage. Dropping listings here would quietly
    # reduce how many jobs the agent actually evaluates.
    random.shuffle(skipped)
    order.extend(skipped)

    return order


class JobDiscoveryEngine:
    def __init__(
        self,
        page: Page,
        config: AppConfig,
        profile: CandidateProfile,
        db: Database,
        matcher: JobMatcher,
        browser_session: Optional[Any] = None,
        rhythm: Optional[SessionRhythm] = None,
        on_match: Optional[Callable[[Any], Awaitable[bool]]] = None,
    ):
        self.page = page
        self.config = config
        self.profile = profile
        self.db = db
        self.matcher = matcher
        self.browser_session = browser_session
        self.rhythm = rhythm
        # Called the moment a posting is judged a match, before the scan moves
        # on. Applying used to wait until the whole keyword/location had been
        # paged through -- three pages plus widening tiers plus the breaks
        # between them -- so the first application of a session landed 15-20
        # minutes in, and a run that was going to fail at the apply step looked
        # healthy for a quarter of an hour first. Reading a posting and acting
        # on it there and then is also simply what a person does.
        #
        # Returns False when the session should stop.
        self.on_match = on_match
        self._warmed_up = False
        # Company pages seen in real search results, used as tangent destinations.
        # Looking up a company whose posting you just read is what a person does; a
        # fixed list of the same six pages is a per-account fingerprint of its own.
        self._seen_company_urls: List[str] = []
        # Job ids handled anywhere in this session. The keyword grid overlaps
        # heavily -- the same posting shows up under "Audit", "IT Audit" and
        # "SOX Compliance" -- and re-opening it each time cost a card click, a
        # read and (before the cache) a model call, for a listing already scored.
        self._seen_job_ids: set = set()

    async def _ensure_page(self) -> Page:
        """Guarantees the engine has an active, valid, and open page."""
        if self.browser_session:
            self.page = await self.browser_session.ensure_active_page()
            return self.page
        if self.page and not self.page.is_closed():
            return self.page
        try:
            for p in self.page.context.pages:
                if not p.is_closed():
                    self.page = p
                    return self.page
            self.page = await self.page.context.new_page()
            return self.page
        except Exception:
            return self.page

    def _build_search_url(
        self,
        keyword: str,
        location: str,
        start_index: int = 0,
        filters: Optional[dict] = None,
    ) -> str:
        """
        Constructs a LinkedIn job search URL.

        `filters` overrides the configured ones for a single request; see
        `_filter_tiers`, which uses it to re-run a search that came back thin
        with a wider net. Absent it, the configured filters apply unchanged.
        """
        f = filters or {}
        easy_apply_only = f.get("easy_apply_only", self.config.search.easy_apply_only)
        date_posted = f.get("date_posted", self.config.search.date_posted)
        experience_levels = f.get("experience_levels", self.config.search.experience_levels)

        base_url = "https://www.linkedin.com/jobs/search/?"
        params = {
            "keywords": keyword,
            "location": location,
            "start": start_index,
        }

        # Filter: Easy Apply
        if easy_apply_only:
            params["f_AL"] = "true"

        # Filter: Date Posted
        date_map = {
            "past-24h": "r86400",
            "past-week": "r604800",
            "past-month": "r2592000",
        }
        if date_posted in date_map:
            params["f_TPR"] = date_map[date_posted]

        # Filter: Experience levels
        exp_map = {
            "internship": "1",
            "entry_level": "2",
            "associate": "3",
            "mid_senior": "4",
            "director": "5",
            "executive": "6",
        }
        selected_exps = [exp_map[lvl] for lvl in experience_levels if lvl in exp_map]
        if selected_exps:
            params["f_E"] = ",".join(selected_exps)

        return base_url + urllib.parse.urlencode(params)

    def _filter_tiers(self) -> List[dict]:
        """
        The filter sets to try, widest-last.

        A search that returns four cards has not told you the market is empty --
        it has told you the filters are narrower than the market. LinkedIn's
        Easy Apply, date and seniority facets compose multiplicatively, and on a
        niche title in one city the intersection is routinely zero. Rather than
        record "no jobs found" and move on, the search is re-issued with each
        constraint lifted in turn, cheapest signal first: recency costs nothing
        to widen, seniority bands are frequently mislabelled by the poster, and
        Easy Apply is dropped only when the operator has asked for listings the
        agent cannot itself submit.
        """
        tiers: List[dict] = [{}]
        s = self.config.search
        if not getattr(s, "widen_when_thin", True):
            return tiers

        # 1. Recency. "past-week" on a niche title in a single city is often the
        #    entire reason a search is empty.
        if s.date_posted != "past-month":
            tiers.append({"date_posted": "past-month"})

        # 2. Seniority. Postings are tagged by whoever wrote the ad; a mid-level
        #    role filed under "Director" is invisible to an f_E-filtered search.
        if s.experience_levels:
            tiers.append({"date_posted": "past-month", "experience_levels": []})

        # 3. Easy Apply. Off by default. Under "discover" these listings are
        #    scored and stored for the outreach path; under "apply" they are
        #    also completed on the company's own ATS.
        if s.easy_apply_only and getattr(s, "external_apply_enabled", False):
            tiers.append({
                "date_posted": "past-month",
                "experience_levels": [],
                "easy_apply_only": False,
            })

        return tiers

    async def _load_all_cards(self, target: int) -> int:
        """
        Scrolls the results column until the card count stops growing.

        LinkedIn virtualises this list: it renders roughly the first handful of
        results and mounts the rest as the column is scrolled. Counting cards on
        arrival therefore reports 5-7 of the 25 that exist, and every result past
        the fold was invisible to this agent -- not filtered out, never seen. The
        previous three fixed scrolls also went to the *window* rather than to the
        column, which on a two-pane layout moves nothing at all.
        """
        cards = self.page.locator(_CARD_SELECTOR)
        container = self.page.locator(_LIST_SELECTOR).first
        has_container = await container.count() > 0

        seen = await cards.count()
        stalls = 0
        # Bounded: a wedged or infinitely-recycling list must not hold the search open.
        for _ in range(12):
            if seen >= target:
                break
            if has_container:
                await organic_scroll_over(self.page, container, total_distance=random.randint(520, 900))
            else:
                await organic_scroll(self.page, total_distance=random.randint(520, 900))
            await asyncio.sleep(random.uniform(0.6, 1.3))

            count = await cards.count()
            if count <= seen:
                stalls += 1
                # Two quiet passes means the list is genuinely exhausted, not
                # merely slow -- one is common while a batch is still fetching.
                if stalls >= 2:
                    break
            else:
                stalls = 0
            seen = max(seen, count)

        return seen

    async def search_and_evaluate(self, keyword: str, location: str) -> List[JobListing]:
        """
        Works one keyword + location across result pages and widening filter tiers.

        A single request to page one is what this used to do, and it saw whatever
        LinkedIn had mounted on arrival -- five or six of twenty-five. Results are
        now paged through with the `start` offset, and if the configured filters
        surface almost nothing the same search is repeated with them relaxed
        instead of being written off as an empty market.
        """
        page = await self._ensure_page()

        # 1. Human Warm-up before initiating automated search queries
        if not self._warmed_up:
            await perform_warm_up_browsing(page)
            self._warmed_up = True

        search = self.config.search
        max_results = search.max_results_per_search
        pages_allowed = max(1, min(
            getattr(search, "max_pages_per_search", 3),
            -(-max_results // RESULTS_PER_PAGE),
        ))
        thin_below = getattr(search, "widen_below_results", 8)

        discovered: List[JobListing] = []
        # Carried across pages and tiers: the model-call allowance and the scan
        # cap are per *search*, and paging used to reset both silently.
        budget = {"llm": 0, "scanned": 0, "stop": False}

        for tier_index, filters in enumerate(self._filter_tiers()):
            if budget["stop"] or budget["scanned"] >= max_results:
                break
            if tier_index > 0:
                if budget["scanned"] >= thin_below:
                    break
                logger.info(
                    f"Only {budget['scanned']} result(s) so far for "
                    f"'{keyword}' in '{location}'; widening ({_describe_filters(filters)})."
                )

            for page_index in range(pages_allowed):
                start = page_index * RESULTS_PER_PAGE
                url = self._build_search_url(keyword, location, start, filters)
                if page_index == 0:
                    logger.info(
                        f"Navigating to LinkedIn Job Search: [bold cyan]{keyword}[/bold cyan] "
                        f"in [bold cyan]{location}[/bold cyan]"
                    )
                else:
                    logger.info(f"Loading results page {page_index + 1} for '{keyword}' in '{location}'.")

                card_count = await self._scan_results_page(url, budget, discovered, max_results)
                if card_count is None:
                    # Navigation failed outright; a second page will not help.
                    break
                # A short page is the last page: LinkedIn fills 25 until it runs out.
                if budget["stop"] or budget["scanned"] >= max_results or card_count < RESULTS_PER_PAGE:
                    break

        return discovered

    async def _scan_results_page(
        self,
        search_url: str,
        budget: dict,
        discovered_jobs: List[JobListing],
        max_results: int,
    ) -> Optional[int]:
        """
        Opens one results page and scores what is on it.

        Returns the number of cards the page held, or None if it could not be
        reached. `budget` is mutated: "scanned" and "llm" accumulate across the
        whole search, and "stop" is set when the session governor ends the run.
        """
        page = await self._ensure_page()

        nav_success = False
        for attempt in range(2):
            try:
                page = await self._ensure_page()
                nav_success = await human_navigate(page, search_url, settle=(3.0, 5.5))
                if nav_success:
                    break
            except Exception as e:
                logger.warning(f"Navigation attempt {attempt + 1} encountered: {e}")

            if attempt == 0 and self.browser_session:
                logger.info("Re-synchronizing browser session and retrying navigation...")
                page = await self.browser_session.ensure_active_page()
                self.page = page
                await asyncio.sleep(1.5)

        if not nav_success:
            logger.error(f"Failed to navigate to LinkedIn Job Search: {search_url}")
            return None

        # Mount the rest of the virtualised list before counting anything.
        remaining = max(1, max_results - budget["scanned"])
        await self._load_all_cards(min(RESULTS_PER_PAGE, remaining))

        cards = self.page.locator(_CARD_SELECTOR)
        card_count = await cards.count()

        if card_count == 0:
            # Only now is "empty" a fact. The old code asked an empty-state
            # locator *before* scrolling and treated any match as proof of an
            # empty market -- so a search that had results but had not rendered
            # them yet was recorded as "No jobs found for this search
            # combination", which is most of what the logs contained.
            logger.info("No jobs found for this search combination.")
            return 0

        logger.info(f"Found {card_count} job cards on page.")

        jobs_evaluated_in_batch = 0
        max_llm = self.config.matching.max_llm_evaluations_per_search
        speed = self.config.speed

        scannable = min(card_count, max(0, max_results - budget["scanned"]))
        indices_to_evaluate = _build_scan_order(scannable)

        for i in indices_to_evaluate:
            card = cards.nth(i)
            try:
                # Hover over card organically before clicking (glance behavior)
                box = await card.bounding_box()
                if box:
                    await organic_mouse_move(self.page, box["x"] + random.uniform(20, 80), box["y"] + random.uniform(15, 35))
                    await asyncio.sleep(random.uniform(0.2, 0.6))

                # Extract Job ID
                job_id = await card.get_attribute("data-job-id") or await card.get_attribute("data-occludable-job-id")
                if not job_id:
                    link_elem = card.locator("a.job-card-list__title, a.job-card-container__link").first
                    if await link_elem.count() > 0:
                        href = await link_elem.get_attribute("href") or ""
                        match = re.search(r"/view/(\d+)", href) or re.search(r"currentJobId=(\d+)", href)
                        if match:
                            job_id = match.group(1)

                if not job_id:
                    continue

                if job_id in self._seen_job_ids:
                    continue

                budget["scanned"] += 1

                # Check if already processed in database
                existing_job = self.db.get_job(job_id)
                if existing_job and existing_job.status in (JobStatus.APPLIED, JobStatus.EVALUATED_REJECT, JobStatus.SKIPPED):
                    self._seen_job_ids.add(job_id)
                    continue
                if existing_job and existing_job.status == JobStatus.EVALUATED_MATCH:
                    # Already scored as a match on an earlier search and not yet
                    # applied to. Hand it straight to the caller: re-opening and
                    # re-scoring it would produce the same verdict at full cost.
                    self._seen_job_ids.add(job_id)
                    discovered_jobs.append(existing_job)
                    continue

                # Organic click on job card
                await organic_click(self.page, card)
                await human_delay(1.5, 3.2)

                # Extract details from right details pane
                job_details = await self._extract_job_details(job_id)
                if not job_details:
                    continue

                self._seen_job_ids.add(job_id)

                # Deterministic pre-screen: title, skills and the experience band,
                # all free. Its outcome decides both whether the model is worth
                # calling and how long this posting deserves to be looked at.
                verdict = self.matcher.screen(job_details)

                budget_spent = max_llm > 0 and budget["llm"] >= max_llm
                if budget_spent and (verdict is None or verdict.needs_llm):
                    # Out of model calls for this search. Rather than score it
                    # badly, leave it DISCOVERED so a later run picks it up.
                    job_details.status = JobStatus.DISCOVERED
                    job_details.match_score = verdict.score if verdict else None
                    job_details.match_reason = (
                        f"[Deferred] {verdict.summary()}" if verdict
                        else "[Deferred] Search's evaluation budget was already spent."
                    )
                    self.db.save_job(job_details)
                    await simulate_human_reading(
                        self.page, min_seconds=speed.read_seconds_skim[0],
                        max_seconds=speed.read_seconds_skim[1],
                    )
                    continue

                if verdict is not None and verdict.rejected:
                    # A person who opens an obviously wrong listing skims it and
                    # moves on in a couple of seconds. Sitting on it for ten to
                    # "look human" was the single largest source of wasted time
                    # in a search, and it is not what the behaviour it imitates
                    # actually looks like.
                    await simulate_human_reading(
                        self.page, min_seconds=speed.read_seconds_skim[0],
                        max_seconds=speed.read_seconds_skim[1],
                    )
                    evaluation = self.matcher.evaluate(
                        job=job_details, profile=self.profile,
                        min_score=self.config.search.min_match_score, verdict=verdict,
                    )
                else:
                    read_min, read_max = speed.read_seconds_match
                    needs_model = verdict is None or verdict.needs_llm
                    if needs_model:
                        budget["llm"] += 1

                    if needs_model and speed.overlap_evaluation_with_reading:
                        # Start scoring, then read the posting while it runs. The
                        # model call used to block the event loop, so the reading
                        # and the API latency were strictly additive -- and the
                        # idle-drift task that is supposed to run during the wait
                        # could not get a slot to run in.
                        async with OrganicIdleDrift(self.page):
                            eval_task = asyncio.create_task(
                                self.matcher.evaluate_async(
                                    job=job_details, profile=self.profile,
                                    min_score=self.config.search.min_match_score,
                                    verdict=verdict,
                                )
                            )
                            try:
                                await simulate_human_reading(
                                    self.page, min_seconds=read_min, max_seconds=read_max
                                )
                            finally:
                                evaluation = await eval_task
                    else:
                        await simulate_human_reading(
                            self.page, min_seconds=read_min, max_seconds=read_max
                        )
                        evaluation = await self.matcher.evaluate_async(
                            job=job_details, profile=self.profile,
                            min_score=self.config.search.min_match_score, verdict=verdict,
                        )

                job_details.match_score = evaluation.match_score
                job_details.match_reason = f"[{evaluation.match_tier}] {evaluation.summary_reasoning}"

                if evaluation.is_recommended:
                    job_details.status = JobStatus.EVALUATED_MATCH
                    console.print(
                        f"[bold green]✓ MATCH ({evaluation.match_score}%):[/bold green] "
                        f"[bold white]{job_details.title}[/bold white] at [cyan]{job_details.company}[/cyan] - {evaluation.summary_reasoning}"
                    )
                else:
                    job_details.status = JobStatus.EVALUATED_REJECT
                    console.print(
                        f"[dim yellow]✗ REJECT ({evaluation.match_score}%):[/dim yellow] "
                        f"{job_details.title} at {job_details.company} - {evaluation.summary_reasoning}"
                    )

                self.db.save_job(job_details)
                discovered_jobs.append(job_details)
                jobs_evaluated_in_batch += 1
                self._remember_company(job_details)

                # Act on a match straight away, while its details pane is still
                # the thing on screen. Easy Apply opens a modal over these very
                # results, so the scan keeps its place; an external application
                # navigates away, which is why the page is put back afterwards.
                if self.on_match is not None and job_details.status == JobStatus.EVALUATED_MATCH:
                    try:
                        keep_going = await self.on_match(job_details)
                    except Exception as exc:
                        # One posting failing to apply must not end the search.
                        logger.warning(f"Apply step failed for {job_details.title}: {exc}")
                        keep_going = True

                    if self.page.url != search_url:
                        await human_navigate(self.page, search_url, settle=(2.0, 3.8))

                    if not keep_going:
                        logger.info("Session rhythm ended the run during the apply step.")
                        budget["stop"] = True
                        break

                # 2. Tangent: look up a company whose posting was just read, then come
                #    back with the Back button. Re-issuing goto() on the search URL
                #    produced an A -> B -> A run of fresh document loads, which is a
                #    navigation shape a person does not generate -- people press Back.
                if jobs_evaluated_in_batch > 2 and random.random() < 0.18:
                    came_back = await explore_random_company_page(
                        self.page, pool=self._seen_company_urls or None
                    )
                    if not came_back:
                        await human_navigate(self.page, search_url, settle=(2.5, 4.5))

                # 3. Session pacing: the governor decides on breaks, glances away, and
                #    when to stop for the day. It returns False when the session is over,
                #    and stopping voluntarily is the behaviour being bought here.
                if self.rhythm is not None:
                    self.rhythm.note_action("evaluate")
                    if not await self.rhythm.pace(self.page, "evaluate"):
                        logger.info("Session rhythm ended the discovery batch.")
                        # Ends the whole search, not just this page: the caller
                        # would otherwise load page two of a session that has
                        # already been told to stop.
                        budget["stop"] = True
                        break
                elif jobs_evaluated_in_batch >= 4 and random.random() < 0.14:
                    await take_natural_human_break(self.page)

                # Gap before moving to the next card. Scaled by action_pace: the
                # session-level pacing (breaks, active hours, daily volume) is
                # what a platform actually aggregates on, and none of it is
                # touched here -- this is just the beat between two clicks.
                await paced_delay(
                    self.config.limits.min_delay_between_actions_seconds,
                    self.config.limits.max_delay_between_actions_seconds,
                    pace=speed.action_pace,
                )

            except Exception as card_err:
                logger.debug(f"Error processing card {i}: {card_err}")
                continue

        return card_count

    def _remember_company(self, job: JobListing):
        """Records a company page URL from a listing we actually looked at."""
        name = (job.company or "").strip().lower()
        if not name or name == "unknown company":
            return
        slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
        if not slug:
            return
        url = f"https://www.linkedin.com/company/{slug}/"
        if url not in self._seen_company_urls:
            self._seen_company_urls.append(url)
            # Keep the pool small so tangents stay recent rather than accumulating into
            # a long-lived list that would drift back towards a fixed fingerprint.
            del self._seen_company_urls[:-12]

    async def _extract_job_details(self, job_id: str) -> Optional[JobListing]:
        """Extracts title, company, location, full description, and recruiter info from the details pane."""
        try:
            # Title
            title_elem = self.page.locator(".job-details-jobs-unified-top-card__job-title, h1.t-24").first
            title = await title_elem.inner_text() if await title_elem.count() > 0 else "Unknown Title"
            title = title.strip()

            # Company
            company_elem = self.page.locator(".job-details-jobs-unified-top-card__company-name, .jobs-unified-top-card__company-name a").first
            company = await company_elem.inner_text() if await company_elem.count() > 0 else "Unknown Company"
            company = company.strip()

            # Location.
            #
            # The element this reads is the whole primary-description container,
            # so its text is the location *and* everything LinkedIn bolts onto
            # the same line:
            #
            #   "Munich, Bavaria, Germany · 4 days ago · 98 applicants
            #
            #    Promoted by hirer · Actively reviewing applicants"
            #
            # All of that was being stored as the job's location, which made the
            # column useless for grouping -- 56 distinct "locations" across 63
            # rows -- and put the posting age and applicant count into any prompt
            # that passes a location to the model. The location is the first
            # bullet-separated segment of the first line; the rest is metadata.
            loc_elem = self.page.locator(".job-details-jobs-unified-top-card__primary-description-container span, .jobs-unified-top-card__bullet").first
            location = await loc_elem.inner_text() if await loc_elem.count() > 0 else "Remote"
            location = location.splitlines()[0] if location.strip() else location
            location = location.split("·")[0].strip(" \t\r\n·|") or "Remote"

            # Description (click 'see more' if present)
            see_more = self.page.locator("button.jobs-description__footer-button, button[aria-label='Click to see more description']").first
            if await see_more.count() > 0 and await see_more.is_visible():
                await organic_click(self.page, see_more)
                await asyncio.sleep(0.4)

            desc_elem = self.page.locator("#job-details, .jobs-description__content, .jobs-box__html-content").first
            description = await desc_elem.inner_text() if await desc_elem.count() > 0 else ""

            # Check Easy Apply button
            easy_apply_btn = self.page.locator("button.jobs-apply-button").first
            is_easy_apply = False
            if await easy_apply_btn.count() > 0:
                btn_text = await easy_apply_btn.inner_text()
                is_easy_apply = "easy apply" in btn_text.lower()

            # Recruiter / Job Poster extraction (Option 3)
            recruiter_name = None
            recruiter_profile_url = None
            poster_elem = self.page.locator(".hirer-card__hirer-information, .jobs-poster").first
            if await poster_elem.count() > 0:
                name_elem = poster_elem.locator(".hirer-card__hirer-information span.jobs-poster__name, strong").first
                if await name_elem.count() > 0:
                    recruiter_name = (await name_elem.inner_text()).strip()
                
                link_elem = poster_elem.locator("a[href*='/in/']").first
                if await link_elem.count() > 0:
                    recruiter_profile_url = await link_elem.get_attribute("href")
                    if recruiter_profile_url and recruiter_profile_url.startswith("/"):
                        recruiter_profile_url = "https://www.linkedin.com" + recruiter_profile_url

            return JobListing(
                job_id=job_id,
                title=title,
                company=company,
                location=location,
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                is_easy_apply=is_easy_apply,
                description=description.strip(),
                recruiter_name=recruiter_name,
                recruiter_profile_url=recruiter_profile_url,
            )
        except Exception as e:
            logger.warning(f"Failed to extract details for job {job_id}: {e}")
            return None
