"""
Naukri Automated Apply Engine:
Handles applications on Naukri.com and answers recruiter screening questionnaires with organic interactions.
"""

import asyncio
from typing import Optional
from playwright.async_api import Page, Locator

from src.core.config import AppConfig, CandidateProfile
from src.core.database import Database
from src.core.logger import logger, console
from src.core.models import JobListing, JobStatus
from src.llm.form_solver import FormSolver
from src.browser.utils import human_delay, safe_click
from src.browser.human_behavior import (
    organic_click,
    organic_type,
    organic_scroll,
    OrganicIdleDrift,
    human_navigate,
)
from src.browser.session_rhythm import SessionRhythm


class NaukriApplyEngine:
    def __init__(
        self,
        page: Page,
        config: AppConfig,
        profile: CandidateProfile,
        db: Database,
        form_solver: FormSolver,
        rhythm: Optional[SessionRhythm] = None,
    ):
        self.page = page
        self.config = config
        self.profile = profile
        self.db = db
        self.solver = form_solver
        self.rhythm = rhythm

    async def apply_to_job(self, job: JobListing) -> bool:
        """Applies to one Naukri posting. Never raises; one bad posting costs one posting."""
        try:
            return await self._apply_to_job(job)
        except Exception as exc:
            logger.error(f"[red]Unhandled error applying to {job.title} at {job.company}: {exc}[/red]")
            self.db.update_job_status(
                job.job_id, JobStatus.FAILED, match_reason=f"Error: {exc}"[:500]
            )
            return False

    async def _apply_to_job(self, job: JobListing) -> bool:
        daily_applies = self.db.get_daily_activity_count("apply")
        if daily_applies >= self.config.limits.max_applications_per_day:
            logger.warning("[yellow]Daily application limit reached. Stopping for safety.[/yellow]")
            return False

        logger.info(f"Opening Naukri Job: [bold]{job.title}[/bold] at [cyan]{job.company}[/cyan] ({job.url})")
        
        if not await human_navigate(self.page, job.url, settle=(2.5, 4.5)):
            logger.error("Failed to open Naukri job page.")
            return False

        # Check if already applied
        already_applied = self.page.locator(".already-applied, .apply-message:has-text('Applied')")
        if await already_applied.count() > 0:
            logger.info("Already applied to this Naukri job.")
            self.db.mark_job_applied(job.job_id)
            return True

        # Locate Apply Button
        apply_btn = self.page.locator("button#apply-button, button.apply-button, button:has-text('Apply')").first
        if await apply_btn.count() == 0 or not await apply_btn.is_visible():
            logger.info("Apply button not available on Naukri (may require external company site apply).")
            self.db.update_job_status(job.job_id, JobStatus.SKIPPED, match_reason="External site apply")
            return False

        # 1. Dry Run Check
        if self.config.general.dry_run:
            console.print(
                f"\n[bold yellow]══ [DRY RUN] Naukri Application Ready ══[/bold yellow]\n"
                f"Job: [bold white]{job.title}[/bold white] at [cyan]{job.company}[/cyan]\n"
                f"Match Score: [green]{job.match_score}%[/green]\n"
                f"[dim]Dry-run enabled: Stopping before clicking Apply on Naukri.[/dim]\n"
            )
            self.db.update_job_status(job.job_id, JobStatus.EVALUATED_MATCH, match_reason="Dry run completed")
            return True

        # 2. Human Confirmation if enabled
        if self.config.general.human_in_the_loop:
            console.print(f"\n[bold yellow]Apply to {job.title} at {job.company} on Naukri?[/bold yellow]")
            resp = input("Submit on Naukri? (y/n, default: y): ").strip().lower()
            if resp in ("n", "no"):
                self.db.update_job_status(job.job_id, JobStatus.SKIPPED, match_reason="Skipped by user")
                return False

        # Organic click on Apply
        await organic_click(self.page, apply_btn)
        await human_delay(2.0, 3.5)

        # Handle Naukri Chatbot / Questionnaire modal if it appears
        unanswered = await self._handle_naukri_chatbot_modal(job)
        if unanswered:
            detail = "; ".join(dict.fromkeys(unanswered))
            console.print(
                f"[bold yellow]⚠ Needs manual review: {job.title} at {job.company} "
                f"— unanswered screening questions ({detail})[/bold yellow]"
            )
            self.db.update_job_status(
                job.job_id, JobStatus.FAILED,
                match_reason=f"Needs manual review: {detail}"[:500],
            )
            return False

        # Confirm Naukri actually recorded the application before writing it down.
        # Clicking Apply is not applying: an unfinished questionnaire leaves the
        # drawer open, and recording APPLIED there means the candidate never comes
        # back to a role they believe is covered.
        if not await self._application_confirmed():
            logger.error(f"[red]Naukri submission not confirmed for {job.title} at {job.company}.[/red]")
            console.print(
                f"[bold red]✗ Could not confirm Naukri submission for {job.title} "
                f"at {job.company}. Left for manual review.[/bold red]"
            )
            self.db.update_job_status(
                job.job_id, JobStatus.FAILED, match_reason="Naukri submission not confirmed"
            )
            return False

        console.print(f"[bold green]✓ Successfully applied to {job.title} at {job.company} on Naukri![/bold green]")
        self.db.mark_job_applied(job.job_id)
        if self.rhythm is not None:
            self.rhythm.note_action("apply")
        return True

    async def _application_confirmed(self, timeout_ms: int = 12000) -> bool:
        """Waits for Naukri's applied confirmation; returns False on timeout."""
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        confirmation = self.page.locator(
            ".already-applied, "
            ".apply-message:has-text('Applied'), "
            "span:has-text('You have successfully applied'), "
            "div:has-text('Application sent successfully'), "
            ".appliedSuccess, .apply-status-header"
        )
        while asyncio.get_event_loop().time() < deadline:
            try:
                if await confirmation.count() > 0 and await confirmation.first.is_visible():
                    return True
            except Exception as exc:
                logger.debug(f"Naukri confirmation probe: {exc}")
            await asyncio.sleep(0.4)
        return False

    async def _handle_naukri_chatbot_modal(self, job: Optional[JobListing] = None) -> list:
        """
        Answers the Naukri screening questionnaire, one question at a time.

        The drawer is a chat: it asks a question, waits for an answer, then asks
        the next. The previous implementation clicked the first available chip
        once and returned, which answered whichever question was on screen with
        whatever option happened to be first -- CTC, notice period, eligibility,
        all decided by DOM order -- and left the rest of the questionnaire
        untouched while the application was recorded as sent.

        Returns the questions it could not answer.
        """
        chatbot_modal = self.page.locator(".chatbot_Drawer, .chat-container, div[role='dialog']").first
        if await chatbot_modal.count() == 0:
            return []

        logger.info("Handling Naukri screening questionnaire popup...")
        job_location = job.location if job else ""
        unanswered: list = []
        max_questions = 15
        last_question = ""
        repeats = 0

        for _ in range(max_questions):
            if await chatbot_modal.count() == 0:
                break

            question = await self._current_chatbot_question(chatbot_modal)
            if not question:
                break

            # The drawer not moving on means our answer was rejected or the
            # question is one we cannot fill; either way, stop rather than loop.
            if question == last_question:
                repeats += 1
                if repeats >= 2:
                    unanswered.append(question[:80])
                    break
            else:
                repeats = 0
            last_question = question

            chips = await chatbot_modal.locator(
                ".chip, .bot-option, button.option-btn, .ssrc__radio-btn-container label, "
                "div[class*='chatbot_Chip'], li[class*='chatbot_Chip']"
            ).all()
            visible_chips = []
            for chip in chips:
                try:
                    if await chip.is_visible():
                        text = (await chip.inner_text()).strip()
                        if text:
                            visible_chips.append((text, chip))
                except Exception:
                    continue

            if visible_chips:
                options = [text for text, _ in visible_chips]
                async with OrganicIdleDrift(self.page):
                    result = self.solver.solve_question(
                        question, "radio", options=options,
                        profile=self.profile, job_location=job_location,
                    )
                if result.needs_human or not str(result.answer).strip():
                    logger.warning(f"[yellow]Declined Naukri question '{question[:60]}': {result.reasoning}[/yellow]")
                    unanswered.append(question[:80])
                    break
                target = next((c for text, c in visible_chips if text == result.answer), None)
                if target is None:
                    logger.warning(f"[yellow]No Naukri chip matched '{result.answer}' for '{question[:50]}'.[/yellow]")
                    unanswered.append(question[:80])
                    break
                await organic_click(self.page, target)
                logger.info(f"Answered: [dim]'{question[:50]}'[/dim] -> [bold cyan]{result.answer}[/bold cyan]")
                await human_delay(1.0, 2.0)
                await self._send_chatbot_message(chatbot_modal)
                continue

            text_input = chatbot_modal.locator(
                "textarea, input[type='text'], div[contenteditable='true']"
            ).first
            if await text_input.count() > 0 and await text_input.is_visible():
                async with OrganicIdleDrift(self.page):
                    result = self.solver.solve_question(
                        question, "text", profile=self.profile, job_location=job_location
                    )
                if result.needs_human or not str(result.answer).strip():
                    logger.warning(f"[yellow]Declined Naukri question '{question[:60]}': {result.reasoning}[/yellow]")
                    unanswered.append(question[:80])
                    break
                await organic_type(self.page, text_input, str(result.answer), enable_typos=False)
                logger.info(f"Answered: [dim]'{question[:50]}'[/dim] -> [bold cyan]{result.answer}[/bold cyan]")
                await human_delay(0.8, 1.6)
                await self._send_chatbot_message(chatbot_modal)
                continue

            break

        return unanswered

    async def _current_chatbot_question(self, modal: Locator) -> str:
        """Reads the most recent question the chatbot has put on screen."""
        try:
            messages = await modal.locator(
                ".botMsg, .bot-message, div[class*='botItem'], li[class*='botItem'], "
                ".chatbot_MessageContainer, .msg"
            ).all()
            for message in reversed(messages):
                try:
                    if not await message.is_visible():
                        continue
                    text = (await message.inner_text()).strip()
                    if text:
                        return text[:300]
                except Exception:
                    continue
        except Exception as exc:
            logger.debug(f"Could not read Naukri chatbot question: {exc}")
        return ""

    async def _send_chatbot_message(self, modal: Locator) -> None:
        """Clicks the drawer's save/send control when it exposes one."""
        try:
            send_btn = modal.locator(
                ".sendMsg, .sendMsgbtn, div[class*='sendMsg'], button:has-text('Save'), "
                "button:has-text('Send'), button[type='submit']"
            ).first
            if await send_btn.count() > 0 and await send_btn.is_visible():
                await organic_click(self.page, send_btn)
                await human_delay(1.0, 2.0)
        except Exception as exc:
            logger.debug(f"Naukri send control not actioned: {exc}")
