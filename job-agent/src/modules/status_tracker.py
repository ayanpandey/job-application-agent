"""
Application Status Tracker: reads what LinkedIn says happened to applications
we already submitted, and records the changes.

The rest of this agent tracks what *it* did -- discovered, evaluated, applied.
That stops at the submit button. Whether a recruiter opened the application, or
closed it, is information LinkedIn holds and never volunteers, so it has to be
read back from the "My Items -> Applied" list.

This is a read-only sweep. It navigates, scrolls and parses; it clicks nothing
that changes state, which is what makes it safe to run on a schedule alongside
the applying agent. It also runs *slowly* on purpose: a logged-in session that
pages through its own application history at machine speed looks nothing like
the person who owns the account, and this feature is not worth a restriction on
the account it reports about.
"""

import random
import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from playwright.async_api import Page

from src.core.database import Database
from src.core.logger import console, logger
from src.browser.human_behavior import (
    OrganicIdleDrift,
    human_navigate,
    organic_scroll,
    simulate_human_reading,
)
from src.browser.utils import human_delay, paced_delay

APPLIED_URL = "https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED"

# LinkedIn writes these as prose in a footer line under each card, and the
# wording drifts between locales and A/B tests. Matching on a distinctive
# substring rather than the whole string is what keeps this working when they
# reword the sentence around it.
STATUS_PATTERNS = [
    ("rejected", ("no longer under consideration", "not selected", "not moving forward")),
    ("interview", ("interview", "scheduled a", "moved to interview")),
    ("viewed", ("application viewed", "viewed by", "resume viewed")),
    ("downloaded", ("application downloaded", "resume downloaded")),
    ("submitted", ("application submitted", "applied on", "applied ")),
]

# How far to page. Each scroll costs a few seconds of dwell, and applications
# older than a few pages have almost always reached a terminal state already.
MAX_SCROLLS = 12


def classify(text: str) -> Optional[str]:
    """
    Maps a card's status line onto one of our statuses.

    Ordered most-specific first: a card reading "Application viewed · No longer
    under consideration" is a rejection, and testing "viewed" first would file it
    as progress. The list above is in that priority order, not alphabetical.
    """
    lowered = (text or "").lower()
    for status, needles in STATUS_PATTERNS:
        if any(needle in lowered for needle in needles):
            return status
    return None


def _job_id_from_href(href: str) -> str:
    """LinkedIn puts the job id in the path (/jobs/view/<id>/) and sometimes in a
    currentJobId query parameter; either is authoritative."""
    if not href:
        return ""
    match = re.search(r"/jobs/view/(\d+)", href)
    if match:
        return match.group(1)
    match = re.search(r"currentJobId=(\d+)", href)
    return match.group(1) if match else ""


class StatusTracker:
    def __init__(self, page: Page, db: Database):
        self.page = page
        self.db = db

    async def sweep(self, max_scrolls: int = MAX_SCROLLS) -> Dict[str, Any]:
        """
        Walks the applied list once and records every status that changed.

        Returns a summary rather than raising on a bad page: this runs as a
        background chore, and a LinkedIn layout change should show up as "0 seen"
        in the operator's board, not as a crash that takes down the run that
        called it.
        """
        summary = {"seen": 0, "changed": 0, "unknown": 0, "statuses": {}}

        if not await human_navigate(self.page, APPLIED_URL, settle=(3.0, 5.0)):
            logger.warning("Status tracker could not open the applied-jobs list.")
            return summary

        # Read the page like a person checking on their applications before
        # touching the scroll wheel.
        await simulate_human_reading(self.page, min_seconds=4.0, max_seconds=9.0)

        async with OrganicIdleDrift(self.page):
            cards = await self._collect(max_scrolls)

        for card in cards:
            summary["seen"] += 1
            status = card["status"]
            if not status:
                summary["unknown"] += 1
                continue
            summary["statuses"][status] = summary["statuses"].get(status, 0) + 1
            try:
                if self.db.record_external_status(card["job_id"], status):
                    summary["changed"] += 1
                    console.print(
                        f"  [cyan]{card['title'][:40]}[/cyan] "
                        f"[dim]at {card['company'][:24]}[/dim] → [bold]{status}[/bold]"
                    )
            except Exception as exc:
                logger.debug(f"Could not record status for {card['job_id']}: {exc}")

        console.print(
            f"[dim]Status sweep: {summary['seen']} applications read, "
            f"{summary['changed']} changed.[/dim]"
        )
        return summary

    async def _collect(self, max_scrolls: int) -> List[Dict[str, str]]:
        """
        Scrolls the list, parsing after each step.

        Parsed on every pass rather than once at the end because the list is
        virtualised -- cards scrolled well past are removed from the DOM, so a
        single parse of the final state would miss everything above the fold.
        Duplicates are collapsed by job id.
        """
        found: Dict[str, Dict[str, str]] = {}

        for step in range(max_scrolls):
            for card in self._parse(await self.page.content()):
                found.setdefault(card["job_id"], card)

            before = await self.page.evaluate("() => window.scrollY")
            await organic_scroll(self.page, total_distance=random.randint(420, 680))
            await paced_delay(1.4, 3.2)

            # Pause to "read" every few steps, and stop early once the page has
            # nothing left to give rather than scrolling into an empty footer.
            if step % 4 == 3:
                await human_delay(2.0, 4.5)
            after = await self.page.evaluate("() => window.scrollY")
            if after <= before:
                break

        return list(found.values())

    def _parse(self, html: str) -> List[Dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        out: List[Dict[str, str]] = []

        # The applied list is an <li> per application in a results container.
        # Selectors are tried in order and the first that matches anything wins,
        # so a class rename on the newer layout falls back to the older one
        # instead of returning nothing.
        containers = (
            soup.select("li.reusable-search__result-container")
            or soup.select("div.entity-result")
            or soup.select("li.scaffold-layout__list-item")
            or soup.select("ul.reusable-search__entity-result-list > li")
        )

        for node in containers:
            link = node.find("a", href=re.compile(r"/jobs/view/|currentJobId="))
            if not link:
                continue
            job_id = _job_id_from_href(link.get("href", ""))
            if not job_id:
                continue

            title = link.get_text(strip=True)
            company = ""
            subtitle = node.select_one(
                ".entity-result__primary-subtitle, .job-card-container__primary-description"
            )
            if subtitle:
                company = subtitle.get_text(strip=True)

            # The status line has no stable class of its own, so the whole card's
            # text is classified. That is why classify() is ordered by
            # specificity -- the card also contains the title and company, and a
            # company literally called "Interview" would otherwise mislead it.
            text = node.get_text(" ", strip=True)
            out.append(
                {
                    "job_id": job_id,
                    "title": title,
                    "company": company,
                    "status": classify(text) or "",
                }
            )

        return out


async def run_status_sweep(page: Page, db: Database) -> Dict[str, Any]:
    """Entry point used by the launcher and by the end of an apply run."""
    try:
        return await StatusTracker(page, db).sweep()
    except Exception as exc:
        logger.warning(f"Status sweep failed: {exc}")
        return {"seen": 0, "changed": 0, "unknown": 0, "statuses": {}, "error": str(exc)}
