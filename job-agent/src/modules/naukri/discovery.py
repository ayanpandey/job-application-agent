"""
Naukri Job Discovery Module: Searches Naukri listings, scrapes requirements,
and evaluates match scores using LLM with organic mouse movements, inertial scrolling, and idle drift.
"""

import asyncio
import re
import urllib.parse
from typing import Any, Awaitable, Callable, List, Optional
from playwright.async_api import Page

from src.core.config import AppConfig, CandidateProfile
from src.core.database import Database
from src.core.logger import logger, console
from src.core.models import JobListing, JobStatus
from src.llm.matcher import JobMatcher
from src.browser.utils import human_delay, paced_delay, smooth_scroll, safe_click
from src.browser.human_behavior import (
    organic_click,
    organic_mouse_move,
    organic_scroll,
    OrganicIdleDrift,
    human_navigate,
)
from src.browser.session_rhythm import SessionRhythm


class NaukriDiscoveryEngine:
    def __init__(
        self,
        page: Page,
        config: AppConfig,
        profile: CandidateProfile,
        db: Database,
        matcher: JobMatcher,
        rhythm: Optional[SessionRhythm] = None,
        on_match: Optional[Callable[[Any], Awaitable[bool]]] = None,
    ):
        self.page = page
        self.rhythm = rhythm
        # Same contract as the LinkedIn engine: called the moment a posting is
        # judged a match, returns False when the session should stop. Batching
        # the applications until the whole search had been paged through put the
        # first one 15-20 minutes into a session.
        self.on_match = on_match
        self.config = config
        self.profile = profile
        self.db = db
        self.matcher = matcher

    def _build_search_url(self, keyword: str, location: str) -> str:
        kw_slug = keyword.lower().replace(" ", "-")
        loc_slug = location.lower().replace(" ", "-")
        base_url = f"https://www.naukri.com/{kw_slug}-jobs-in-{loc_slug}?"
        params = {
            "k": keyword,
            "l": location,
        }

        # Naukri's experience filter is an integer number of years. This used to
        # be fed `limits.min_delay_between_actions_seconds` -- the pause between
        # two clicks, 3.5 in the shipped config -- so every search was filtered
        # to an arbitrary experience level that had nothing to do with the
        # candidate, and 3.5 is not even a value the filter accepts.
        #
        # The floor of the tolerance band is used rather than the exact figure:
        # Naukri reads the parameter as a minimum, so asking for one year less
        # surfaces the roles whose stated range starts just above the candidate.
        years = self.config.naukri.experience_years
        if years is None:
            years = self.profile.screening_answers.total_years_experience
        if years is not None:
            tol = self.config.matching.experience_tolerance
            params["experience"] = max(0, int(round(float(years) * (1 - tol))))

        return base_url + urllib.parse.urlencode(params)

    async def search_and_evaluate(self, keyword: str, location: str) -> List[JobListing]:
        search_url = self._build_search_url(keyword, location)
        logger.info(f"Navigating to Naukri Search: [bold cyan]{keyword}[/bold cyan] in [bold cyan]{location}[/bold cyan]")
        
        # Referer-chained navigation: see human_navigate. A search results page reached
        # with no referrer at all, every time, is a server-side observation that needs
        # no JavaScript to make.
        if not await human_navigate(self.page, search_url, settle=(3.0, 5.0)):
            logger.error("Error opening Naukri search.")
            return []

        discovered_jobs: List[JobListing] = []
        llm_evaluations = 0
        max_llm = self.config.matching.max_llm_evaluations_per_search
        await smooth_scroll(self.page, scroll_count=3, distance=380)

        # Locate Naukri job tuple cards
        cards = self.page.locator("div.srp-jobtuple-wrapper, article.jobTuple, .cust-job-tuple")
        card_count = await cards.count()
        logger.info(f"Found {card_count} job listings on Naukri page.")

        for i in range(min(card_count, self.config.search.max_results_per_search)):
            card = cards.nth(i)
            try:
                # Title & URL
                title_elem = card.locator("a.title").first
                if await title_elem.count() == 0:
                    continue
                
                title = (await title_elem.inner_text()).strip()
                url = await title_elem.get_attribute("href") or ""
                
                # Derive Job ID from URL (e.g. ...-jobid-12345 or regex)
                match = re.search(r"-(\d{10,})", url) or re.search(r"jobId=(\d+)", url)
                job_id = f"naukri_{match.group(1)}" if match else f"naukri_{abs(hash(url))}"

                # Check if already processed
                existing_job = self.db.get_job(job_id)
                if existing_job and existing_job.status in (JobStatus.APPLIED, JobStatus.EVALUATED_REJECT, JobStatus.SKIPPED):
                    continue

                # Organic mouse glance over card
                box = await card.bounding_box()
                if box:
                    await organic_mouse_move(self.page, box["x"] + 30, box["y"] + 20)
                    await asyncio.sleep(0.3)

                # Company
                comp_elem = card.locator("a.comp-name, .comp-name").first
                company = (await comp_elem.inner_text()).strip() if await comp_elem.count() > 0 else "Unknown Company"

                # Location
                loc_elem = card.locator(".locWdth, .loc-wrap span").first
                loc_text = (await loc_elem.inner_text()).strip() if await loc_elem.count() > 0 else location

                # Description snippet
                desc_elem = card.locator(".job-desc, .job-description").first
                snippet = (await desc_elem.inner_text()).strip() if await desc_elem.count() > 0 else ""

                job = JobListing(
                    job_id=job_id,
                    title=title,
                    company=company,
                    location=loc_text,
                    url=url,
                    is_easy_apply=True,
                    description=snippet,
                )

                # Deterministic pre-screen first: a card whose title and snippet
                # do not overlap the profile at all is not worth a model call,
                # and Naukri's search returns a lot of those.
                verdict = self.matcher.screen(job)
                min_score = self.config.naukri.min_match_score or self.config.search.min_match_score

                if verdict is not None and not verdict.needs_llm:
                    evaluation = self.matcher.evaluate(
                        job=job, profile=self.profile, min_score=min_score, verdict=verdict
                    )
                elif llm_evaluations >= max_llm > 0:
                    # Out of model calls for this search; leave it for a later run
                    # rather than scoring it on partial information.
                    job.status = JobStatus.DISCOVERED
                    job.match_score = verdict.score if verdict else None
                    job.match_reason = "[Deferred] Search's evaluation budget was already spent."
                    self.db.save_job(job)
                    continue
                else:
                    llm_evaluations += 1
                    # Off the event loop, so the idle-drift gaze motion actually
                    # runs during the request instead of being starved by it.
                    with console.status(f"[bold cyan]Evaluating Naukri match for {title} at {company}...[/bold cyan]"):
                        async with OrganicIdleDrift(self.page):
                            evaluation = await self.matcher.evaluate_async(
                                job=job, profile=self.profile,
                                min_score=min_score, verdict=verdict,
                            )

                job.match_score = evaluation.match_score
                job.match_reason = f"[{evaluation.match_tier}] {evaluation.summary_reasoning}"

                if evaluation.is_recommended:
                    job.status = JobStatus.EVALUATED_MATCH
                    console.print(
                        f"[bold green]✓ NAUKRI MATCH ({evaluation.match_score}%):[/bold green] "
                        f"[bold white]{title}[/bold white] at [cyan]{company}[/cyan] - {evaluation.summary_reasoning}"
                    )
                else:
                    job.status = JobStatus.EVALUATED_REJECT
                    console.print(
                        f"[dim yellow]✗ REJECT ({evaluation.match_score}%):[/dim yellow] {title} at {company}"
                    )

                self.db.save_job(job)
                discovered_jobs.append(job)

                if self.on_match is not None and job.status == JobStatus.EVALUATED_MATCH:
                    try:
                        keep_going = await self.on_match(job)
                    except Exception as exc:
                        logger.warning(f"Apply step failed for {job.title}: {exc}")
                        keep_going = True

                    if self.page.url != search_url:
                        await human_navigate(self.page, search_url, settle=(2.0, 3.8))

                    if not keep_going:
                        logger.info("Session rhythm ended the Naukri run during the apply step.")
                        break

                if self.rhythm is not None:
                    self.rhythm.note_action("evaluate")
                    if not await self.rhythm.pace(self.page, "evaluate"):
                        logger.info("Session rhythm ended the Naukri discovery batch.")
                        break

                await paced_delay(2.0, 4.0, pace=self.config.speed.action_pace)

            except Exception as card_err:
                logger.debug(f"Error parsing Naukri card {i}: {card_err}")
                continue

        return discovered_jobs
