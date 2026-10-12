"""
Automated Easy Apply Form Filler: Handles LinkedIn multi-step Easy Apply dialogs,
auto-fills contact details, dynamically generates and uploads tailored LaTeX/ATS resumes,
answers custom screening questions, and supports Dry-Run and Human-in-the-Loop review modes.
Employs organic physical mouse kinematics, cognitive typing, inertial scrolling, and background idle drift.
"""

import asyncio
import random
from pathlib import Path
from typing import Dict, List, Optional, Any
from playwright.async_api import Page, Locator

from src.core.config import AppConfig, CandidateProfile
from src.core.database import Database
from src.core import paths
from src.core.logger import logger, console
from src.core.models import JobListing, JobStatus
from src.llm.form_solver import FormSolver
from src.modules.form_filler import FormFiller
from src.modules.resume_generator import ResumeTailorEngine
from src.browser.utils import human_delay, paced_delay, safe_click
from src.browser.human_behavior import (
    organic_click,
    organic_type,
    organic_scroll,
    smooth_scroll,
    OrganicIdleDrift,
    natural_click,
    natural_type,
    human_navigate,
)
from src.browser.session_rhythm import SessionRhythm


class EasyApplyEngine:
    def __init__(
        self,
        page: Page,
        config: AppConfig,
        profile: CandidateProfile,
        db: Database,
        form_solver: FormSolver,
        resume_tailor: Optional[ResumeTailorEngine] = None,
        browser_session: Optional[Any] = None,
        rhythm: Optional[SessionRhythm] = None,
    ):
        self.page = page
        self.config = config
        self.profile = profile
        # Selector healing, used only when a hard-coded selector misses. Built
        # from the solver's own model client so it needs no new plumbing, and
        # left as None when there is none rather than failing to construct.
        self._resolver = None
        _llm = getattr(form_solver, "llm", None)
        if _llm is not None:
            from src.browser.dom_resolver import DomResolver
            self._resolver = DomResolver(_llm)
        self.db = db
        self.solver = form_solver
        self.resume_tailor = resume_tailor
        self.browser_session = browser_session
        self.rhythm = rhythm

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

    async def _job_already_open(self, job: JobListing) -> bool:
        """
        True when this job's details pane is the one currently rendered.

        Checks the URL first (the search page carries ?currentJobId=<id>, the detail
        page carries /jobs/view/<id>), then falls back to confirming an Easy Apply
        button is actually on screen -- a stale URL with no live button would send us
        clicking into the wrong posting, which is worse than one redundant load.
        """
        try:
            current = self.page.url or ""
            if job.job_id not in current:
                return False
            btn = self.page.locator("button.jobs-apply-button, button[aria-label*='Easy Apply']").first
            return await btn.count() > 0 and await btn.is_visible()
        except Exception:
            return False

    async def apply_to_job(self, job: JobListing) -> bool:
        """
        Applies to one posting. Never raises.

        Every caller runs this in a `for job in ...` loop with no guard of its
        own, so a single Playwright timeout used to abort the entire session and
        strand an open modal over the next job. One bad posting should cost one
        posting.
        """
        # Reset per attempt. The router reads this straight after the call to
        # tell "no Easy Apply button on this posting" apart from every other
        # reason an attempt returns False, and a stale True would send an
        # already-handled job to the external engine a second time.
        self._last_no_easy_apply = False
        try:
            return await self._apply_to_job(job)
        except Exception as exc:
            logger.error(f"[red]Unhandled error applying to {job.title} at {job.company}: {exc}[/red]")
            self.db.update_job_status(
                job.job_id, JobStatus.FAILED, match_reason=f"Error: {exc}"[:500]
            )
            await self._recover_to_clean_state()
            return False

    async def _recover_to_clean_state(self) -> None:
        """Best-effort dismissal of whatever dialog we crashed inside."""
        try:
            page = await self._ensure_page()
            if page.is_closed():
                return
            dialog = page.locator("div[role='dialog']").first
            if await dialog.count() > 0:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.6)
                discard = page.locator(
                    "button[data-control-name='discard_application_confirm_btn'], "
                    "button:has-text('Discard')"
                ).first
                if await discard.count() > 0 and await discard.is_visible():
                    await discard.click()
        except Exception as exc:
            logger.debug(f"Recovery cleanup skipped: {exc}")

    async def _apply_to_job(self, job: JobListing) -> bool:
        """Navigates to job view and executes the automated application flow with organic pacing."""
        self.page = await self._ensure_page()
        daily_applies = self.db.get_daily_activity_count("apply")
        if daily_applies >= self.config.limits.max_applications_per_day:
            logger.warning(
                f"[yellow]Daily application limit reached ({daily_applies}/{self.config.limits.max_applications_per_day}). Halting applications for safety.[/yellow]"
            )
            return False

        # The resume is tailored further down, once the Easy Apply button has
        # actually been found. It used to be generated here, before the job page
        # was even opened -- so a posting that turned out to be external or
        # expired still cost a full two-pass tailoring run, and the document was
        # thrown away with the job marked SKIPPED. That is the "skipped, with a
        # resume attached" the operator kept seeing.
        self._current_resume = None

        # In the full pipeline this job's details pane is *already* open -- discovery
        # just clicked its card. Navigating to /jobs/view/<id>/ anyway forced a fresh
        # document load for a page that was on screen, which is both slower and a
        # navigation no person performs: nobody re-enters the URL of the posting they
        # are looking at. Applying in place keeps the trace inside LinkedIn's own SPA,
        # which is where a real Easy Apply happens.
        if await self._job_already_open(job):
            logger.info(f"Applying in place (already open): [bold]{job.title}[/bold] at [cyan]{job.company}[/cyan]")
            await human_delay(1.2, 2.6)
        else:
            logger.info(f"Opening job page: [bold]{job.title}[/bold] at [cyan]{job.company}[/cyan] ({job.url})")
            if not await human_navigate(self.page, job.url, settle=(2.5, 4.5)):
                if self.browser_session:
                    logger.warning("Browser page closed during job view. Re-opening session...")
                    self.page = await self._ensure_page()
                    if not await human_navigate(self.page, job.url, settle=(2.5, 4.5)):
                        logger.error(f"Failed to navigate on retry to {job.url}")
                        return False
                else:
                    logger.error(f"Failed to navigate to {job.url}")
                    return False

        # Check if already applied
        applied_badge = self.page.locator(".jobs-s-apply__applied-date, .artdeco-inline-feedback--success")
        if await applied_badge.count() > 0:
            logger.info(f"Already applied to {job.title} at {job.company}. Updating database.")
            self.db.mark_job_applied(job.job_id)
            return True

        # Locate Easy Apply button.
        #
        # Waited for, not sampled once. Navigation settles on
        # `domcontentloaded`, which fires before LinkedIn's client has rendered
        # the top card, so an immediate `count()` can look at a page whose apply
        # button simply has not been drawn yet -- and the old code then wrote the
        # posting off permanently. The modal lookup forty lines below already
        # learned this lesson; the button lookup never did.
        # First *visible* match, not first in DOM order. LinkedIn renders the
        # apply control more than once on a posting -- the top card and a sticky
        # header both carry it, and the one that is off-screen or display:none
        # is regularly first in the document. `.first` therefore selected an
        # element that never became visible, and the posting was written off as
        # "Easy Apply button not found" while the real button sat next to it in
        # the DOM, perfectly clickable. The external engine was already fixed
        # this way; this path was not.
        candidates = self.page.locator(
            "button.jobs-apply-button, button[aria-label*='Easy Apply']"
        )
        easy_apply_btn = candidates.first

        # One wait for the client to render the top card at all, then pick from
        # whatever it drew.
        try:
            await candidates.first.wait_for(state="attached", timeout=8000)
            await self.page.wait_for_timeout(600)
        except Exception:
            pass

        found_visible = False
        try:
            for i in range(min(await candidates.count(), 6)):
                candidate = candidates.nth(i)
                if await candidate.is_visible():
                    easy_apply_btn = candidate
                    found_visible = True
                    break
        except Exception:
            pass

        # Nothing visible matched the selector we ship. Before writing the
        # posting off, ask the model where the control actually is -- LinkedIn
        # changing its markup is the single most common cause of this, and it
        # otherwise presents as "this job has no apply button".
        if not found_visible and self._resolver is not None:
            healed = await self._resolver.resolve(
                self.page,
                "the Easy Apply button that opens the application modal "
                "(not an external 'Apply' link to another site)",
                page_context=f"LinkedIn posting: {job.title} at {job.company}",
            )
            if healed is not None:
                easy_apply_btn = healed
                found_visible = True

        # The button must actually say "Easy Apply".
        #
        # `button.jobs-apply-button` is the class LinkedIn puts on BOTH its
        # in-page Easy Apply button and the external "Apply" button that opens
        # the employer's site in a new tab. Matching on the class alone meant
        # every external posting was clicked as though it were Easy Apply: the
        # click opened a new tab, no modal ever appeared in this one, and the
        # job was abandoned with "modal did not appear" after a full resume
        # tailoring had already been paid for. Discovery has always checked the
        # text (`is_easy_apply = "easy apply" in btn_text.lower()`); the apply
        # step never did, and that asymmetry is the bug.
        #
        # A button that says only "Apply" is an external application, which is
        # a fact for the router to act on, not a failure.
        if await easy_apply_btn.count() > 0:
            try:
                label = (
                    (await easy_apply_btn.inner_text())
                    or (await easy_apply_btn.get_attribute("aria-label"))
                    or ""
                )
            except Exception:
                label = ""
            if label.strip() and "easy apply" not in label.lower():
                logger.info(
                    f"{job.title} has an external Apply button ('{label.strip()[:40]}'), "
                    f"not Easy Apply."
                )
                self._last_no_easy_apply = True
                return False

        if await easy_apply_btn.count() == 0 or not await easy_apply_btn.is_visible():
            logger.info(f"Easy Apply button not found for {job.title} (might be external apply or expired).")
            # Reported to the router, not decided here. Writing SKIPPED at this
            # point ended the job permanently: the router had already committed
            # to Easy Apply from the discovery-time `is_easy_apply` flag, so the
            # external engine never saw a single posting and was unreachable
            # dead code even with external_apply set to "apply". The flag was
            # True on every row ever recorded, so it carried no information.
            #
            # The router decides what a missing button means, because only it
            # knows whether there is another way to apply.
            self._last_no_easy_apply = True
            return False

        # This job really is applicable, so the tailoring is worth paying for.
        # The idle drift keeps the page alive in a human way while the two model
        # passes run, which is a natural thing to be doing on a posting you have
        # just opened and are reading.
        tailored_resume_path: Optional[Path] = None
        if self.resume_tailor:
            try:
                async with OrganicIdleDrift(self.page):
                    tailored_resume_path = await self.resume_tailor.tailor_resume(job, self.profile)
            except Exception as r_err:
                logger.warning(f"Resume tailor warning, using default: {r_err}")
                tailored_resume_path = paths.master_resume(self.profile.resume_path)
        else:
            tailored_resume_path = paths.master_resume(self.profile.resume_path)

        # Remembered on the instance because the resume is chosen here but the
        # application is recorded four call-frames deeper, in _handle_submission,
        # which is reached through the modal-stepping code and has no reason to
        # carry a resume path through it. One application is in flight at a time,
        # so a single slot is sufficient and honest.
        self._current_resume = tailored_resume_path

        # Organic click on Easy Apply with WindMouse trajectory
        await organic_click(self.page, easy_apply_btn)
        await paced_delay(1.8, 3.2, pace=self.config.speed.action_pace)

        # Handle modal multi-step flow
        modal = self.page.locator("div.jobs-easy-apply-modal, div[role='dialog']").first

        # Wait for it rather than sampling once. The modal is rendered by
        # LinkedIn's client after a round trip, and the fixed 1.8-3.2s pause
        # above is frequently shorter than that -- a single count() then
        # reported "did not appear" for a modal that opened a moment later,
        # abandoning a job already scored as a match. The job keeps its
        # EVALUATED_MATCH status either way, so a genuine failure is retried on
        # the next run rather than lost.
        try:
            await modal.wait_for(state="visible", timeout=12000)
        except Exception:
            # The click may simply have missed.
            #
            # `organic_click` drives a real cursor to coordinates read from the
            # element's bounding box and then presses the physical mouse there,
            # and it reports success whether or not anything was underneath: a
            # press on empty space raises nothing. LinkedIn lazy-loads the top
            # card, so the page frequently shifts between reading the box and
            # pressing, and the press lands next to the button rather than on
            # it. Twelve seconds later this looked exactly like "no modal", and
            # the posting was abandoned after a full resume tailoring had been
            # paid for.
            #
            # Playwright's own click re-resolves the element, waits for it to be
            # stable and actionable, and raises if it is not -- so it both fixes
            # the miss and tells us when something else is wrong.
            logger.info(
                f"No modal after the organic click on {job.title}; "
                f"retrying with a direct click."
            )
            try:
                await easy_apply_btn.click(timeout=8000)
                await modal.wait_for(state="visible", timeout=12000)
            except Exception as exc:
                logger.warning(
                    f"Easy Apply modal did not appear for {job.title} at {job.company} "
                    f"({type(exc).__name__}); will retry next run."
                )
                return False

        success = await self._handle_modal_steps(modal, job, tailored_resume_path)
        return success

    async def _step_signature(self, modal: Locator) -> str:
        """
        A cheap fingerprint of the step currently rendered.

        Used to tell "advanced to the next step" from "clicked Next and the form
        refused to move", which otherwise look identical from the caller's side.
        """
        try:
            header = ""
            head_loc = modal.locator("h3, h2").first
            if await head_loc.count() > 0:
                header = (await head_loc.inner_text()).strip()
            ids = await modal.locator("input, select, textarea").evaluate_all(
                "els => els.map(e => e.id || e.name || e.type).join('|')"
            )
            return f"{header}##{ids}"
        except Exception:
            return ""

    async def _handle_modal_steps(self, modal: Locator, job: JobListing, resume_path: Optional[Path]) -> bool:
        """Iterates through multi-step form pages until submit or error."""
        max_steps = 10
        current_step = 0
        unfilled: List[str] = []
        last_signature = ""
        stalled_rounds = 0
        pace = self.config.speed.action_pace

        while current_step < max_steps:
            current_step += 1

            # Orientation cost is real on the first screen of a form and close to
            # zero by the fourth -- people who have filled the same Easy Apply
            # flow fifty times do not re-read the contact-details step. A flat
            # 1.5-3s on every step of every application was both slower than the
            # behaviour it models and, repeated identically, more regular.
            step_min = 1.5 if current_step == 1 else 0.7
            step_max = 3.0 if current_step == 1 else 1.6
            await paced_delay(step_min, step_max, pace=pace)

            # Check buttons
            submit_btn = modal.locator("button[aria-label='Submit application'], button:has-text('Submit application')").first
            review_btn = modal.locator("button[aria-label='Review your application'], button:has-text('Review')").first
            next_btn = modal.locator("button[aria-label='Continue to next step'], button:has-text('Next')").first

            # Visual scanning: slight natural scroll to survey the active step fields
            if random.random() < 0.4:
                await organic_scroll(self.page, total_distance=random.randint(60, 140), direction=1)
                await asyncio.sleep(random.uniform(0.3, 0.7))

            # 1. Fill current step form fields
            unfilled += await self._fill_current_step_fields(modal, resume_path, job)

            # 2. Check if final submission button is ready
            if await submit_btn.count() > 0 and await submit_btn.is_visible():
                if unfilled:
                    # Submitting with questions the solver refused to answer sends an
                    # incomplete application under the candidate's name. Stop and let
                    # a human finish it instead.
                    return await self._abandon_for_review(modal, job, unfilled)
                return await self._handle_submission(modal, job, submit_btn)

            signature_before = await self._step_signature(modal)

            # 3. If Review button is present, click it naturally
            if await review_btn.count() > 0 and await review_btn.is_visible():
                await organic_click(self.page, review_btn)
                # On review step, simulate human glancing over summary
                await paced_delay(1.2, 2.4, pace=pace)
                await organic_scroll(self.page, total_distance=180, direction=1)
            # 4. If Next button is present, click it naturally to proceed
            elif await next_btn.count() > 0 and await next_btn.is_visible():
                await organic_click(self.page, next_btn)
            else:
                break

            # 5. Confirm the form actually moved. Re-clicking Next into a failing
            # validation just burns the step budget and hammers the same control.
            await asyncio.sleep(random.uniform(0.9, 1.6))
            errors = await self._collect_validation_errors(modal)
            if errors:
                logger.warning(
                    f"Form validation blocked step {current_step} for {job.job_id}: {'; '.join(errors)}"
                )
                return await self._abandon_for_review(modal, job, unfilled + errors)

            if await self._step_signature(modal) == signature_before:
                stalled_rounds += 1
                if stalled_rounds >= 2:
                    logger.warning(f"Form stopped advancing for {job.job_id}; leaving for manual review.")
                    return await self._abandon_for_review(
                        modal, job, unfilled + ["Form stopped advancing"]
                    )
            else:
                stalled_rounds = 0
            last_signature = signature_before

        logger.warning(f"Exceeded max steps or encountered stuck state for job {job.job_id}.")
        return await self._abandon_for_review(modal, job, unfilled + ["Exceeded max form steps"])

    async def _abandon_for_review(self, modal: Locator, job: JobListing, reasons: List[str]) -> bool:
        """Discards the part-filled application and flags it for a human."""
        detail = "; ".join(dict.fromkeys(r for r in reasons if r)) or "Unknown form state"
        console.print(
            f"[bold yellow]⚠ Needs manual review: {job.title} at {job.company} — {detail}[/bold yellow]"
        )
        await self._dismiss_modal(modal)
        self.db.update_job_status(
            job.job_id, JobStatus.FAILED, match_reason=f"Needs manual review: {detail}"[:500]
        )
        return False

    async def _fill_current_step_fields(
        self, modal: Locator, resume_path: Optional[Path], job: Optional[JobListing] = None
    ) -> List[str]:
        """
        Inspects and fills form fields on the active modal page with cognitive typing
        and tailored resume upload.

        Returns the questions it declined to answer. The caller must not submit
        while that list is non-empty: a blank required field fails validation
        loudly, but a *guessed* one submits silently and costs the candidate the
        role, so the solver's refusals have to survive all the way up.

        The reading, solving and typing all live in the shared `FormFiller` --
        the same code the external ATS path runs. Only the phone field is
        LinkedIn's own, because it is the one control here answered from the
        profile instead of by the solver.

        Note that this returns FormFiller's *marked* labels, where a required
        question carries a "[required]" suffix. LinkedIn treats every refusal as
        blocking regardless, so the caller ignores the distinction; it exists for
        the external portals, where abandoning an otherwise complete application
        over an optional EEO question would cost more roles than it saves.
        """
        filler = FormFiller(self.page, self.profile, self.solver, self.config)

        # The phone number, which the profile already knows. Answering it here
        # also keeps it out of the solver's batch: FormFiller skips any control
        # that already holds a value.
        phone_input = modal.locator("input[id*='phoneNumber'], input[type='tel']").first
        if await phone_input.count() > 0 and await phone_input.is_visible():
            current_val = await phone_input.input_value()
            if not current_val.strip() and self.profile.personal_info.phone_number:
                await organic_type(self.page, phone_input, self.profile.personal_info.phone_number, enable_typos=False)
                await asyncio.sleep(random.uniform(0.4, 0.8))

        return await filler.fill(
            modal,
            resume_path=resume_path,
            job_location=job.location if job else "",
        )

    async def _handle_submission(self, modal: Locator, job: JobListing, submit_btn: Locator) -> bool:
        """Handles Dry-Run, Human confirmation, or automated submission."""
        
        # 1. Dry-Run Mode (Safety first: do not submit)
        if self.config.general.dry_run:
            console.print(
                f"\n[bold yellow]══ [DRY RUN] Application Ready ══[/bold yellow]\n"
                f"Job: [bold white]{job.title}[/bold white] at [cyan]{job.company}[/cyan]\n"
                f"Match Score: [green]{job.match_score}%[/green]\n"
                f"[dim]Dry-run enabled: Form was filled completely with tailored resume. Canceling modal without submitting.[/dim]\n"
            )
            await self._dismiss_modal(modal)
            self.db.update_job_status(job.job_id, JobStatus.EVALUATED_MATCH, match_reason="Dry run completed")
            return True

        # 2. Human-in-the-Loop Confirmation
        if self.config.general.human_in_the_loop:
            console.print(
                f"\n[bold yellow]══ Human Confirmation Required ══[/bold yellow]\n"
                f"Job: [bold white]{job.title}[/bold white] at [cyan]{job.company}[/cyan]\n"
                f"Match Score: [green]{job.match_score}%[/green]\n"
            )
            response = input("Submit this application on LinkedIn? (y/n, default: y): ").strip().lower()
            if response in ("n", "no"):
                console.print("[dim yellow]Skipping submission by user request.[/dim yellow]")
                await self._dismiss_modal(modal)
                self.db.update_job_status(job.job_id, JobStatus.SKIPPED, match_reason="Skipped by user")
                return False

        # 3. Final Submission
        logger.info(f"Submitting application for {job.title} at {job.company}...")
        # Human hesitation before clicking final submit
        await asyncio.sleep(random.uniform(0.8, 1.8))
        await organic_click(self.page, submit_btn)
        await human_delay(3.0, 5.0)

        # Confirm LinkedIn actually accepted it before recording anything. Clicking
        # Submit is not the same as having applied: a failed validation leaves the
        # modal open with the button still there. Recording that as APPLIED is the
        # worst failure this system has -- the candidate believes the role is
        # covered, never applies by hand, and nothing in the logs disagrees.
        if not await self._submission_confirmed(modal):
            errors = await self._collect_validation_errors(modal)
            detail = f" ({'; '.join(errors)})" if errors else ""
            logger.error(
                f"[red]Submission not confirmed for {job.title} at {job.company}{detail}[/red]"
            )
            console.print(
                f"[bold red]✗ Could not confirm submission for {job.title} at {job.company}."
                f"{detail} Left for manual review.[/bold red]"
            )
            await self._dismiss_modal(modal)
            self.db.update_job_status(
                job.job_id, JobStatus.FAILED,
                match_reason=f"Submission not confirmed{detail}"[:500],
            )
            return False

        # Success dialog: dismiss it so the next job starts from a clean page.
        done_btn = self.page.locator("button:has-text('Done'), button[aria-label='Dismiss']").first
        if await done_btn.count() > 0 and await done_btn.is_visible():
            await organic_click(self.page, done_btn)

        console.print(f"[bold green]✓ Successfully applied to {job.title} at {job.company}![/bold green]")
        resume = getattr(self, "_current_resume", None)
        self.db.mark_job_applied(job.job_id, resume_file=resume.name if resume else "")
        if self.rhythm is not None:
            self.rhythm.note_action("apply")
        return True

    async def _submission_confirmed(self, modal: Locator, timeout_ms: int = 12000) -> bool:
        """
        Waits for positive evidence that the application went through.

        Accepts either the post-submit confirmation ("Application sent" / Done)
        or the submit control disappearing along with the form. Returns False on
        timeout rather than assuming success.
        """
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        confirmation = self.page.locator(
            ".artdeco-modal__content:has-text('Your application was sent'), "
            "h2:has-text('Application sent'), "
            "h3:has-text('Application sent'), "
            ".jobs-s-apply__applied-date, "
            ".artdeco-inline-feedback--success, "
            "button:has-text('Done')"
        )
        while asyncio.get_event_loop().time() < deadline:
            try:
                if await confirmation.count() > 0 and await confirmation.first.is_visible():
                    return True
                if await self._collect_validation_errors(modal):
                    return False
                # Form gone entirely, with no error: LinkedIn closed the modal on us.
                still_open = (
                    await modal.count() > 0
                    and await modal.locator(
                        "button[aria-label='Submit application'], "
                        "button:has-text('Submit application')"
                    ).count() > 0
                )
                if not still_open:
                    return True
            except Exception as exc:
                logger.debug(f"Submission confirmation probe: {exc}")
            await asyncio.sleep(0.4)
        return False

    async def _collect_validation_errors(self, modal: Locator) -> List[str]:
        """Returns the inline validation messages LinkedIn is currently showing."""
        try:
            err = modal.locator(
                ".artdeco-inline-feedback--error, "
                "[data-test-form-element-error-messages], "
                "[role='alert']"
            )
            texts: List[str] = []
            for handle in await err.all():
                try:
                    if not await handle.is_visible():
                        continue
                    text = (await handle.inner_text()).strip()
                    if text and text not in texts:
                        texts.append(text[:120])
                except Exception:
                    continue
            return texts
        except Exception:
            return []

    async def _dismiss_modal(self, modal: Locator):
        """Closes and discards the open modal with organic clicks."""
        try:
            dismiss_btn = modal.locator("button[aria-label='Dismiss'], button.artdeco-modal__dismiss").first
            if await dismiss_btn.count() > 0:
                await organic_click(self.page, dismiss_btn)
                await asyncio.sleep(random.uniform(0.8, 1.4))

                # Confirm discard dialog if prompted
                discard_btn = self.page.locator("button[data-control-name='discard_application_confirm_btn'], button:has-text('Discard')").first
                if await discard_btn.count() > 0:
                    await organic_click(self.page, discard_btn)
        except Exception as e:
            logger.debug(f"Modal dismiss error: {e}")
