"""
Recruiter & Networking Outreach Engine (Option 3):
Finds hiring managers/recruiters for applied or matched jobs, generates personalized
connection notes under LinkedIn's 200-character limit, and automates or queues outreach.
Includes organic profile browsing, natural mouse curves, and realistic typing.
"""

import asyncio
import random
from typing import List, Optional, Any
from playwright.async_api import Page

from src.core.config import AppConfig, CandidateProfile
from src.core.database import Database
from src.core.logger import logger, console
from src.core.models import JobListing, OutreachRecord, OutreachStatus
from src.llm.outreach_gen import OutreachGenerator
from src.browser.utils import human_delay, safe_click, smooth_scroll
from src.browser.human_behavior import (
    organic_click,
    organic_type,
    organic_mouse_move,
    organic_scroll,
    OrganicIdleDrift,
    natural_click,
    natural_type,
    human_navigate,
)


class RecruiterOutreachEngine:
    def __init__(
        self,
        page: Page,
        config: AppConfig,
        profile: CandidateProfile,
        db: Database,
        outreach_gen: OutreachGenerator,
        browser_session: Optional[Any] = None
    ):
        self.page = page
        self.config = config
        self.profile = profile
        self.db = db
        self.generator = outreach_gen
        self.browser_session = browser_session

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

    async def prepare_or_send_outreach(self, job: JobListing) -> Optional[OutreachRecord]:
        """
        Prepares a personalized connection note for the job poster or recruiter.
        If auto_send_connection is enabled, sends the invitation; otherwise queues it for review.
        """
        recipient_name = job.recruiter_name or f"Recruiting Team at {job.company}"
        recipient_title = "Talent Acquisition / Hiring Manager"
        profile_url = job.recruiter_profile_url or f"https://www.linkedin.com/company/{job.company.lower().replace(' ', '-')}/people/"

        # Generate personalized connection note with idle drift
        with console.status(f"[bold cyan]Generating tailored connection note for {recipient_name}...[/bold cyan]"):
            async with OrganicIdleDrift(self.page):
                note = self.generator.generate_connection_note(
                    job=job,
                    recipient_name=recipient_name,
                    recipient_title=recipient_title,
                    profile=self.profile,
                    max_chars=self.config.outreach.max_note_length,
                )

        record = OutreachRecord(
            job_id=job.job_id,
            company=job.company,
            recipient_name=recipient_name,
            recipient_title=recipient_title,
            profile_url=profile_url,
            note_text=note,
            status=OutreachStatus.QUEUED,
        )

        record_id = self.db.save_outreach(record)
        record.id = record_id

        console.print(
            f"\n[bold magenta]══ Outreach Note Drafted ══[/bold magenta]\n"
            f"Company: [cyan]{job.company}[/cyan] | Role: [white]{job.title}[/white]\n"
            f"Recipient: [bold]{recipient_name}[/bold] ({recipient_title})\n"
            f"Note ({len(note)} chars):\n[italic green]\"{note}\"[/italic green]\n"
        )

        if self.config.outreach.auto_send_connection and job.recruiter_profile_url:
            await self.send_connection_request(record)

        return record

    async def send_connection_request(self, record: OutreachRecord) -> bool:
        """Navigates to recruiter profile, browses background naturally, and sends a connection invite."""
        self.page = await self._ensure_page()
        daily_outreach = self.db.get_daily_activity_count("outreach")
        if daily_outreach >= self.config.limits.max_outreach_messages_per_day:
            logger.warning(
                f"[yellow]Daily outreach limit reached ({daily_outreach}/{self.config.limits.max_outreach_messages_per_day}). Halting outreach for safety.[/yellow]"
            )
            return False

        logger.info(f"Navigating to recipient profile: {record.profile_url}")
        # Referer-chained: a recruiter profile reached from the job posting you just
        # read is an ordinary path. The same profile opened with no referrer at all,
        # repeatedly, is not -- and profile views are exactly the traffic LinkedIn
        # scrutinises hardest, because scraping is the abuse they care most about.
        if not await human_navigate(self.page, record.profile_url, settle=(3.0, 5.5)):
            if self.browser_session:
                logger.warning("Browser page was closed. Re-opening session...")
                self.page = await self._ensure_page()
                if not await human_navigate(self.page, record.profile_url, settle=(3.0, 5.5)):
                    logger.error(f"Failed to navigate on retry to {record.profile_url}")
                    return False
            else:
                logger.error(f"Failed to navigate to {record.profile_url}")
                return False

        try:
            # 1. Natural Profile Browsing: Scroll down to view experience/about section
            await smooth_scroll(self.page, scroll_count=2, distance=320)
            await asyncio.sleep(random.uniform(1.8, 3.5))
            
            # Inertial scroll back to top
            await organic_scroll(self.page, total_distance=480, direction=-1)
            await asyncio.sleep(random.uniform(1.0, 2.2))

            # 2. Locate 'Connect' button
            connect_btn = self.page.locator("button:has-text('Connect'), button[aria-label*='Invite']").first
            
            # If not directly visible, check the 'More' action dropdown
            if await connect_btn.count() == 0 or not await connect_btn.is_visible():
                more_btn = self.page.locator("button[aria-label='More actions'], button:has-text('More')").first
                if await more_btn.count() > 0:
                    await organic_click(self.page, more_btn)
                    await asyncio.sleep(random.uniform(0.6, 1.2))
                    connect_btn = self.page.locator("div[aria-label*='Invite'], span:has-text('Connect')").first

            if await connect_btn.count() == 0:
                logger.info("Could not find Connect button (might already be connected or pending).")
                return False

            await organic_click(self.page, connect_btn)
            await human_delay(1.5, 3.0)

            # 3. Click 'Add a note' in the modal
            add_note_btn = self.page.locator("button[aria-label='Add a note'], button:has-text('Add a note')").first
            if await add_note_btn.count() > 0:
                await organic_click(self.page, add_note_btn)
                await human_delay(1.0, 2.0)

                # Type note in textarea with natural typing and micro-pauses
                textarea = self.page.locator("textarea[name='message'], #custom-message").first
                if await textarea.count() > 0:
                    await organic_type(self.page, textarea, record.note_text, enable_typos=True)
                    await human_delay(1.5, 3.0)

            # 4. Send invitation
            send_btn = self.page.locator("button[aria-label='Send invitation'], button:has-text('Send')").first
            if await send_btn.count() > 0:
                await organic_click(self.page, send_btn)
                await human_delay(2.5, 4.5)
                
                if record.id:
                    self.db.mark_outreach_sent(record.id)
                console.print(f"[bold green]✓ Connection request sent to {record.recipient_name}![/bold green]")
                return True

        except Exception as e:
            logger.error(f"Error sending connection request: {e}")

        return False
