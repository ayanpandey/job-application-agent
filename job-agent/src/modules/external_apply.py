"""
Applications completed on the company's own ATS, for listings LinkedIn cannot submit.

This is the `external_apply: "apply"` path. It is deliberately the most
conservative engine in the system, because it is the one that types a
candidate's name into a form nobody at LinkedIn is validating:

  - It runs only when the operator has set `external_apply: "apply"`. Under
    "off" or "discover" it opens nothing at all -- "discover" exists precisely so
    a listing can be surfaced for outreach without a portal ever being touched.
  - It will fill a form on a site nobody has configured, but only one that
    `form_scout` is confident is a job application. The old whitelist of four
    vendors was safe and far too narrow: it could not see the same vendors'
    forms when they were embedded in a company's own careers page, and it
    skipped the entire long tail of Workable, Recruitee, BambooHR and
    hand-rolled `<form>`s. Confidence, not familiarity, is what gates a
    submission now -- see MIN_FORM_SCORE.
  - It stops at a captcha, an anti-bot interstitial or a login wall rather than
    working around one. A challenge is a site asking for a human, and an account
    that answers them automatically is a louder signal than whatever provoked
    the challenge. Workday, Taleo, iCIMS and SuccessFactors stay hard skips for
    the same reason they always were: they need a registered account per
    employer, and a run that starts one leaves an abandoned half-built profile.
  - Identity fields are filled from the profile, never asked of the solver. The
    solver's job is judgement about screening questions; an email address is a
    fact, and a model that guesses one has invented a candidate.
  - A refused *required* question stops the submission. An optional one does
    not -- on an ATS form the optional questions are EEO demographics and "how
    did you hear about us", and abandoning a finished application over one would
    cost more roles than it saves. That asymmetry is the whole reason
    `FormFiller` marks its refusals.
  - Nothing is recorded as APPLIED until the portal itself confirms. Clicking
    Submit is not applying: a failed validation leaves the form on screen. A
    false APPLIED is the worst outcome this system has, because the candidate
    believes the role is covered and never applies by hand.

The form reading and filling is `FormFiller`, the same code the LinkedIn modal
runs. Only the navigation, the vendor detection and the submit-and-confirm are
new here.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

from playwright.async_api import Page, Locator

from src.core import paths
from src.core.config import AppConfig, CandidateProfile
from src.core.database import Database
from src.core.logger import logger, console
from src.core.models import JobListing, JobStatus
from src.llm.form_solver import FormSolver
from src.modules.form_filler import FormFiller
from src.modules.resume_generator import ResumeTailorEngine
from src.browser.utils import human_delay, paced_delay
from src.browser.human_behavior import (
    organic_click,
    organic_type,
    organic_scroll,
    human_navigate,
)
from src.browser.session_rhythm import SessionRhythm


# Vendor knowledge and form discovery live in form_scout, which has no browser
# dependency worth speaking of and can be tested on a saved page. They are
# re-exported here because this module is where callers and tests have always
# looked for them.
from src.modules.form_scout import (  # noqa: E402  (grouped with its own kind)
    AtsProfile,
    FormTarget,
    KNOWN_UNSUPPORTED,
    SUPPORTED,
    blocked_vendor,
    detect_ats,
    detect_bot_wall,
    detect_login_wall,
    dismiss_consent_banner,
    find_application_form,
    frame_urls,
)


class ExternalApplyEngine:
    """Completes an application on a company's own ATS."""

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
        self.db = db
        self.solver = form_solver
        self.resume_tailor = resume_tailor
        self.browser_session = browser_session
        self.rhythm = rhythm

    # -- entry point ---------------------------------------------------------

    async def apply_to_job(self, job: JobListing) -> bool:
        """
        Applies to one external posting. Never raises.

        Same contract as EasyApplyEngine.apply_to_job: the callers loop over jobs
        with no guard of their own, so one bad portal must cost one posting
        rather than the session.
        """
        # Held on the instance rather than in a local, because the local is only
        # bound if _apply_to_job returns -- and the run that raises after opening
        # a tab is exactly the one that must not leave it behind. One
        # application is in flight at a time, so a single slot is honest.
        self._portal = None
        try:
            _, applied = await self._apply_to_job(job)
            return applied
        except Exception as exc:
            logger.error(
                f"[red]Unhandled error on external portal for {job.title} at {job.company}: {exc}[/red]"
            )
            self.db.update_job_status(
                job.job_id, JobStatus.FAILED, match_reason=f"External apply error: {exc}"[:500]
            )
            return False
        finally:
            # The portal is a tab of our own opening; leaving it behind would
            # stack one per external job for the rest of the session.
            await self._close_portal(self._portal)
            self._portal = None

    async def _apply_to_job(self, job: JobListing) -> Tuple[Optional[Page], bool]:
        # 1. The switch. Fails closed: anything other than an explicit "apply"
        #    means no portal is opened, and the listing is left as a match so the
        #    outreach path can still work it.
        if not getattr(self.config.search, "external_apply_submits", False):
            logger.info(
                f"external_apply is not 'apply'; leaving {job.title} at {job.company} for outreach."
            )
            return None, False

        # 2. The same daily ceiling the LinkedIn path observes. An external
        #    application is an application.
        daily = self.db.get_daily_activity_count("apply")
        if daily >= self.config.limits.max_applications_per_day:
            logger.warning(
                f"[yellow]Daily application limit reached "
                f"({daily}/{self.config.limits.max_applications_per_day}).[/yellow]"
            )
            return None, False

        # 3. Open the company's portal, and find out whose it is.
        portal = await self._open_portal(job)
        self._portal = portal  # closed by apply_to_job, whatever happens next
        if portal is None:
            self.db.update_job_status(
                job.job_id, JobStatus.FAILED, match_reason="Could not open the external portal"
            )
            return None, False

        # 4. A cookie banner is a physical obstacle before it is anything else:
        #    it overlays the submit control, so the click lands on the banner
        #    and the application silently never sends.
        await dismiss_consent_banner(portal, organic_click)

        # 5. The deny-list, checked across every frame rather than just the
        #    top-level URL. A careers page that embeds Workday in an iframe is
        #    still Workday, and its own URL says nothing about that.
        url = portal.url or ""
        blocked = blocked_vendor([url] + frame_urls(portal))
        if blocked:
            return portal, self._skip(job, blocked)

        # 6. A challenge means the site has asked for a human. Nothing here
        #    tries to get past one.
        wall = await detect_bot_wall(portal)
        if wall:
            logger.warning(f"[yellow]Stopping on {_host(url)}: {wall}.[/yellow]")
            console.print(
                f"[bold yellow]⚠ {job.title} at {job.company} — {wall}. "
                f"Left for you to finish by hand.[/bold yellow]"
            )
            self.db.update_job_status(
                job.job_id, JobStatus.FAILED,
                match_reason=f"Stopped at a challenge: {wall}"[:500],
            )
            return portal, False

        # 7. Find the form. A known vendor supplies its own selectors; anything
        #    else is found structurally, in whatever frame it turns out to be in.
        profile_hint, _ = detect_ats(url)
        target = await self._await_form(portal, profile_hint)
        if target is None:
            login = await detect_login_wall(portal, None)
            detail = login or f"no application form found on {_host(url)}"
            return portal, self._skip(job, detail)

        login = await detect_login_wall(portal, target)
        if login:
            return portal, self._skip(job, login)

        logger.info(f"{target.describe()} for {job.title} at {job.company}.")

        # 8. A glance over the form before touching it, as on any other page.
        await self._survey(portal, target)

        # 9. Fill it, across as many steps as it has. Identity first, so those
        #    fields are already answered and never reach the solver.
        resume_path = await self._resume_for(job)
        filler = FormFiller(portal, self.profile, self.solver, self.config)

        target, unanswered, stop_reason = await self._fill_all_steps(
            portal, target, filler, job, resume_path
        )
        if stop_reason:
            return portal, self._needs_review(job, target.vendor, [stop_reason])

        # 10. The refusal rule. Only a required refusal blocks; see the module
        #     docstring for why the optional ones must not.
        blocking = FormFiller.blocking(unanswered)
        if blocking:
            return portal, self._needs_review(job, target.vendor, blocking)

        if unanswered:
            logger.info(
                f"Left {len(unanswered)} optional question(s) blank on {target.vendor}: "
                f"{'; '.join(unanswered)}"
            )

        # 11. Submit, or don't.
        return portal, await self._submit(portal, target, job, resume_path, filler)

    # -- outcomes ------------------------------------------------------------

    def _skip(self, job: JobListing, detail: str) -> bool:
        """Records a portal we deliberately did not work. Always returns False."""
        logger.info(f"Skipping external application for {job.title}: {detail}.")
        console.print(f"[yellow]⤼ Skipped {job.title} at {job.company} — {detail}.[/yellow]")
        self.db.update_job_status(
            job.job_id, JobStatus.SKIPPED,
            match_reason=f"External portal skipped: {detail}"[:500],
        )
        return False

    def _needs_review(self, job: JobListing, vendor: str, reasons: List[str]) -> bool:
        """Records a form that was reached but must be finished by a person."""
        detail = "; ".join(dict.fromkeys(r for r in reasons if r)) or "unknown form state"
        console.print(
            f"[bold yellow]⚠ Needs manual review: {job.title} at {job.company} — {detail}[/bold yellow]"
        )
        self.db.update_job_status(
            job.job_id, JobStatus.FAILED,
            match_reason=f"Needs manual review ({vendor}): {detail}"[:500],
        )
        return False

    # -- discovery -----------------------------------------------------------

    async def _await_form(
        self, portal: Page, hint: Optional[AtsProfile], attempts: int = 5
    ) -> Optional[FormTarget]:
        """
        Waits for the application form to render, then returns it.

        Polled rather than waited on a selector, because on the generic path
        there is no selector to wait for -- the question is whether *anything*
        on the page scores as an application yet, and that only becomes true
        once the vendor's client-side app has mounted. An embedded board is
        slower still: the outer page loads, then the iframe, then the form
        inside it.
        """
        for attempt in range(attempts):
            target = await find_application_form(portal, prefer=hint)
            if target is not None:
                return target
            await asyncio.sleep(1.2 + attempt * 0.8)
        return None

    # -- navigation ----------------------------------------------------------

    async def _open_portal(self, job: JobListing) -> Optional[Page]:
        """
        Follows LinkedIn's Apply button out to the company's site.

        LinkedIn opens external applications in a new tab, so the popup is what
        we want; but some postings navigate in place instead, and a few carry a
        direct link. All three end the same way -- a page that is no longer on
        linkedin.com -- so that, rather than the mechanism, is what is checked.
        """
        page = await self._ensure_page()
        if job.job_id not in (page.url or ""):
            if not await human_navigate(page, job.url, settle=(2.5, 4.5)):
                logger.error(f"Failed to open the LinkedIn posting {job.url}")
                return None

        # LinkedIn renders the apply control more than once on a posting -- the
        # top card and a sticky header both carry it -- and `.first` in DOM
        # order is regularly the hidden one. Clicking a hidden element does
        # nothing, produces no popup and no navigation, which is exactly the
        # "did not leave LinkedIn; nothing to fill" that every external attempt
        # ended in. Take the first *visible* candidate instead.
        candidates = page.locator(
            "button.jobs-apply-button, a.jobs-apply-button, "
            "button[aria-label*='Apply'], a[aria-label*='Apply']"
        )
        apply_btn = None
        try:
            for i in range(min(await candidates.count(), 6)):
                candidate = candidates.nth(i)
                if await candidate.is_visible():
                    apply_btn = candidate
                    break
        except Exception:
            apply_btn = None

        if apply_btn is None:
            logger.info(f"No visible Apply button on the posting for {job.title}.")
            return None

        # Brought into view before clicking. A control below the fold can report
        # visible and still not take a real mouse click at its coordinates.
        try:
            await apply_btn.scroll_into_view_if_needed(timeout=4000)
            await human_delay(0.4, 1.1)
        except Exception:
            pass

        try:
            async with page.context.expect_page(timeout=15000) as popup:
                await organic_click(page, apply_btn)
            portal = await popup.value
        except Exception:
            # No new tab: either it navigated in place, or the click did nothing.
            await asyncio.sleep(random.uniform(1.5, 2.5))
            portal = page

        try:
            await portal.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:
            pass
        await human_delay(1.5, 3.0)

        if "linkedin.com" in (portal.url or "").lower():
            logger.info(f"Apply did not leave LinkedIn for {job.title}; nothing to fill.")
            return None
        return portal

    async def _close_portal(self, portal: Optional[Page]) -> None:
        """Closes the portal tab, unless it is the session's own page."""
        try:
            if portal is None or portal.is_closed() or portal is self.page:
                return
            await portal.close()
        except Exception as exc:
            logger.debug(f"Could not close the portal tab: {exc}")

    async def _ensure_page(self) -> Page:
        if self.browser_session:
            self.page = await self.browser_session.ensure_active_page()
        return self.page

    async def _survey(self, page: Page, target: Optional[FormTarget] = None) -> None:
        """
        A look down the form before filling it, scaled to how much there is.

        The fixed 3-6 seconds this used to spend was the same for a two-field
        form and a four-page one, and the second case is where it reads wrong:
        an applicant who lands on a long form and begins typing immediately has
        not read it. ATS vendors surface time-on-form to their customers, so the
        number is not merely observable, it is already on a dashboard somewhere.

        Roughly 200 words per minute over the form's own text, bounded at both
        ends -- fast enough not to waste a session, slow enough to be a person.
        """
        words = 0
        if target is not None:
            try:
                text = await target.root.inner_text(timeout=4000)
                words = len((text or "").split())
            except Exception:
                words = 0

        budget = max(3.0, min(28.0, words / 200.0 * 60.0)) if words else random.uniform(3.0, 6.0)
        deadline = asyncio.get_event_loop().time() + budget

        await asyncio.sleep(random.uniform(0.8, 1.8))
        while asyncio.get_event_loop().time() < deadline:
            await organic_scroll(page, total_distance=random.randint(140, 320), direction=1)
            await asyncio.sleep(random.uniform(0.9, 2.2))
            if random.random() < 0.25:
                await organic_scroll(page, total_distance=random.randint(60, 150), direction=-1)
                await asyncio.sleep(random.uniform(0.6, 1.4))

        # Back to the top, because that is where filling starts.
        await organic_scroll(page, total_distance=random.randint(400, 900), direction=-1)
        await asyncio.sleep(random.uniform(0.5, 1.2))

    # -- stepping ------------------------------------------------------------

    # A form with more pages than this is either not an application or is one
    # that has stopped advancing; both end in manual review.
    MAX_STEPS = 8

    async def _fill_all_steps(
        self,
        portal: Page,
        target: FormTarget,
        filler: FormFiller,
        job: JobListing,
        resume_path: Optional[Path],
    ) -> Tuple[FormTarget, List[str], str]:
        """
        Fills the form through as many pages as it has.

        Returns (final target, unanswered labels, stop reason). A non-empty stop
        reason means the caller must not submit.

        Multi-page forms are the norm on Workable, SmartRecruiters and most
        hand-rolled portals, and the single-shot fill this replaced simply typed
        into page one and then hunted for a submit control that was two pages
        away. It found "Next" instead -- which matches `button[type=submit]` on
        plenty of these forms -- and clicked it, leaving a part-filled
        application behind with no record that anything had gone wrong.

        Advancing is only ever believed when the page actually changes. Clicking
        Next into a failing validation looks identical from here to clicking
        Next successfully, so the signature is compared and a form that has not
        moved twice in a row is abandoned rather than hammered.
        """
        unanswered: List[str] = []
        signatures: List[str] = []
        resume_placed = False

        for step in range(1, self.MAX_STEPS + 1):
            # Identity fields first on every page: several of these forms ask
            # for the email again on the last step, and a field the profile can
            # answer must never become a question for a model to guess at.
            await self._fill_identity(portal, target.root, filler)

            # The resume goes on at most once, on the first page that asks for
            # it. Driven from here rather than left to `fill`, because `fill`
            # cannot know what earlier pages already did: a later step carrying
            # any file input at all -- a portfolio, a reference letter -- would
            # otherwise be handed the CV again, since an unlabelled upload on a
            # job application is treated as a resume slot.
            if not resume_placed and target.has_file_input:
                resume_placed = await filler.upload_resume(target.root, resume_path)

            unanswered += await filler.fill(
                target.root,
                resume_path=resume_path,
                job_location=job.location or "",
                upload_resume=False,
            )

            submit_btn, next_btn = await self._find_controls(portal, target)
            if submit_btn is not None and next_btn is None:
                return target, unanswered, ""
            if next_btn is None:
                # Neither control: on step one that means we misread the page.
                if step == 1:
                    return target, unanswered, "no submit or continue control on the form"
                return target, unanswered, ""

            signature = await self._step_signature(target)
            if signature and signature in signatures:
                return target, unanswered, "the form stopped advancing"
            signatures.append(signature)

            await asyncio.sleep(random.uniform(0.6, 1.4))
            await organic_click(portal, next_btn)
            await human_delay(1.6, 3.2)

            errors = await FormFiller.collect_validation_errors(target.root)
            if errors:
                return target, unanswered, "; ".join(errors[:4])

            # The next page is very often a different DOM subtree -- some of
            # these forms replace the container entirely -- so the target is
            # re-resolved rather than reused.
            refreshed = await find_application_form(portal, prefer=None)
            if refreshed is not None:
                refreshed.vendor = target.vendor
                target = refreshed

        return target, unanswered, f"more than {self.MAX_STEPS} form pages"

    async def _step_signature(self, target: FormTarget) -> str:
        """A cheap fingerprint of the page currently rendered, for stall detection."""
        try:
            return await target.root.evaluate(
                """el => {
                    const ids = Array.from(el.querySelectorAll('input,select,textarea,[role=combobox]'))
                        .map(e => e.id || e.name || e.type || e.getAttribute('aria-label') || '')
                        .join('|');
                    const head = (el.querySelector('h1,h2,h3,legend') || {}).innerText || '';
                    return (head + '##' + ids).slice(0, 400);
                }"""
            ) or ""
        except Exception:
            return ""

    async def _find_controls(
        self, portal: Page, target: FormTarget
    ) -> Tuple[Optional[Locator], Optional[Locator]]:
        """
        The submit control and the advance control, told apart.

        This distinction is the single most consequential judgement in the whole
        engine, because the two are frequently the same element type: on a
        multi-page form the "Next" button is very often `<button type="submit">`
        for its own page. Getting it wrong in one direction abandons a finished
        application; in the other it sends a form that is one page from done.

        So the rule is deliberately asymmetric. A control only counts as Submit
        on the strength of its *words* -- "Submit application", "Send
        application" -- or when there is no advance control anywhere on the
        page. A bare `type=submit` sitting next to a "Continue" button is the
        Continue button's own mechanism, not a submission.
        """
        next_btn = await self._first_visible(target, target.next_control)
        explicit = await self._first_visible(
            target,
            "button:has-text('Submit application'), button:has-text('Submit Application'), "
            "button:has-text('Send application'), button:has-text('Submit my application')",
        )
        if explicit is not None:
            return explicit, None

        generic = await self._first_visible(target, target.submit)
        if next_btn is not None:
            return None, next_btn
        return generic, None

    @staticmethod
    async def _first_visible(target: FormTarget, selector: str) -> Optional[Locator]:
        """
        The first visible match inside the form, else on the page around it.

        Falling outside the form matters: SmartRecruiters and several embedded
        boards render the submit button in a sticky footer that is a sibling of
        the form rather than a child of it.
        """
        for scope in (target.root, target.frame.locator("body")):
            try:
                for handle in (await scope.locator(selector).all())[:6]:
                    try:
                        if await handle.is_visible() and await handle.is_enabled():
                            return handle
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    # -- filling -------------------------------------------------------------

    async def _resume_for(self, job: JobListing) -> Optional[Path]:
        """The tailored resume if one can be built, the profile's own otherwise."""
        default = paths.master_resume(self.profile.resume_path)
        if not self.resume_tailor:
            return default
        try:
            return await self.resume_tailor.tailor_resume(job, self.profile)
        except Exception as exc:
            logger.warning(f"Resume tailor warning, using the default resume: {exc}")
            return default

    def _identity_values(self) -> List[Tuple[str, str, str]]:
        """(logical field, selector list, value) for everything the profile knows."""
        info = self.profile.personal_info
        return [
            ("first name",
             "input[name='first_name'], input#first_name, input[autocomplete='given-name'], "
             "input[name*='firstName'], input[id*='first_name']",
             info.first_name),
            ("last name",
             "input[name='last_name'], input#last_name, input[autocomplete='family-name'], "
             "input[name*='lastName'], input[id*='last_name']",
             info.last_name),
            # Lever and Ashby ask for one name field. Tried after the split pair
            # so a Greenhouse form does not get its first name overwritten.
            ("full name",
             "input[name='name'], input#name, input[autocomplete='name'], input[id*='_systemfield_name']",
             info.full_name),
            ("email",
             "input[type='email'], input[name='email'], input#email, "
             "input[autocomplete='email'], input[id*='_systemfield_email']",
             info.email),
            ("phone",
             "input[type='tel'], input[name='phone'], input#phone, "
             "input[autocomplete='tel'], input[id*='phone']",
             info.phone_number),
            ("LinkedIn URL",
             "input[name*='urls[LinkedIn]'], input[autocomplete*='linkedin'], "
             "input[name*='linkedin'], input[id*='linkedin']",
             info.linkedin_url or ""),
        ]

    async def _fill_identity(self, page: Page, form: Locator, filler: FormFiller) -> None:
        """
        Answers the who-are-you fields from the profile.

        Done before `FormFiller.fill` on purpose: the reader skips any control
        that already holds a value, so filling these here is also what keeps them
        out of the solver's batch. A model asked "First Name" will answer it, and
        the answer is a guess about a fact the profile already holds.
        """
        for label, selector, value in self._identity_values():
            if not value or not str(value).strip():
                continue
            try:
                elem = form.locator(selector).first
                if await elem.count() == 0 or not await elem.is_visible():
                    continue
                if (await elem.input_value()).strip():
                    continue  # the portal already knows it
            except Exception:
                continue

            try:
                # No typos here. A corrected typo in an email or a phone number
                # is a keystroke sequence some ATS validators reject outright,
                # and neither field is prose.
                await organic_type(page, elem, str(value), enable_typos=False)
                if (await elem.input_value()).strip() != str(value).strip():
                    await elem.fill(str(value))
                logger.info(f"Filled {label}: [bold cyan]{value}[/bold cyan]")
                await paced_delay(0.3, 0.8, pace=self.config.speed.action_pace)
            except Exception as exc:
                logger.debug(f"Could not fill {label}: {exc}")

    # -- submitting ----------------------------------------------------------

    async def _submit(
        self,
        page: Page,
        target: FormTarget,
        job: JobListing,
        resume_path: Optional[Path],
        filler: Optional[FormFiller] = None,
    ) -> bool:
        """Dry-run, human confirmation, or a real submission that must be confirmed."""
        ats = SimpleNamespace(name=target.vendor, submit=target.submit)
        form = target.root
        if self.config.general.dry_run:
            console.print(
                f"\n[bold yellow]══ [DRY RUN] {ats.name} application ready ══[/bold yellow]\n"
                f"Job: [bold white]{job.title}[/bold white] at [cyan]{job.company}[/cyan]\n"
                f"Match Score: [green]{job.match_score}%[/green]\n"
                f"[dim]Form filled on {_host(page.url)}. Not submitting: dry run.[/dim]\n"
            )
            self.db.update_job_status(
                job.job_id, JobStatus.EVALUATED_MATCH,
                match_reason=f"Dry run completed on {ats.name}",
            )
            return False

        if self.config.general.human_in_the_loop:
            console.print(
                f"\n[bold yellow]══ Human confirmation required ══[/bold yellow]\n"
                f"Job: [bold white]{job.title}[/bold white] at [cyan]{job.company}[/cyan]\n"
                f"Portal: [cyan]{_host(page.url)}[/cyan] ({ats.name})\n"
            )
            if input("Submit this application? (y/n, default: y): ").strip().lower() in ("n", "no"):
                console.print("[dim yellow]Skipping submission by user request.[/dim yellow]")
                self.db.update_job_status(
                    job.job_id, JobStatus.SKIPPED, match_reason="Skipped by user"
                )
                return False

        # Resolved through _first_visible, which searches the form and then the
        # form's own frame. The previous fallback was `page.locator(...)`, and a
        # Page locator searches the main frame only: on an embedded board the
        # submit control lives in the iframe, so a form whose button sat in a
        # sticky footer outside the <form> element could never be submitted at
        # all. Measured on tests/fixtures/embedded_board.html: page.locator finds
        # 0 of a control frame.locator finds 1 of.
        submit_btn = await self._first_visible(target, ats.submit)
        if submit_btn is None:
            logger.warning(f"No submit control found on the {ats.name} form for {job.title}.")
            self.db.update_job_status(
                job.job_id, JobStatus.FAILED,
                match_reason=f"{ats.name}: no submit control found",
            )
            return False

        logger.info(f"Submitting {ats.name} application for {job.title} at {job.company}...")
        await asyncio.sleep(random.uniform(0.8, 1.8))
        await organic_click(page, submit_btn)
        await human_delay(3.0, 5.0)

        if not await self._submission_confirmed(page, target):
            # One repair pass before giving up.
            #
            # The overwhelmingly common reason a submit bounces is a field the
            # reader could not see -- a combobox that had not mounted yet, or a
            # consent box rendered below the fold. The form is now telling us
            # exactly which ones, and it has kept everything already typed, so
            # re-reading it costs one LLM call rather than a whole application.
            # Only once: a second identical failure is a form we do not
            # understand, and repeated submits on one posting is its own signal.
            errors = await FormFiller.collect_validation_errors(form)
            repaired = False
            if errors and filler is not None:
                logger.info(
                    f"Submission bounced on {ats.name} ({'; '.join(errors[:3])}); "
                    f"re-reading the form once."
                )
                repaired = await self._repair(page, target, filler, job, resume_path)

            if repaired:
                await asyncio.sleep(random.uniform(0.9, 1.9))
                retry_btn = await self._first_visible(target, ats.submit)
                if retry_btn is not None:
                    await organic_click(page, retry_btn)
                    await human_delay(3.0, 5.0)

            if not repaired or not await self._submission_confirmed(page, target):
                errors = await FormFiller.collect_validation_errors(form) or errors
                detail = f" ({'; '.join(errors)})" if errors else ""
                logger.error(
                    f"[red]Submission not confirmed on {ats.name} for {job.title}{detail}[/red]"
                )
                console.print(
                    f"[bold red]✗ Could not confirm submission for {job.title} at {job.company}."
                    f"{detail} Left for manual review.[/bold red]"
                )
                self.db.update_job_status(
                    job.job_id, JobStatus.FAILED,
                    match_reason=f"{ats.name} submission not confirmed{detail}"[:500],
                )
                return False

        console.print(
            f"[bold green]✓ Applied to {job.title} at {job.company} via {ats.name}![/bold green]"
        )
        self.db.mark_job_applied(
            job.job_id, resume_file=resume_path.name if resume_path else ""
        )
        self.db.record_external_status(job.job_id, "submitted", source=ats.name.lower())
        if self.rhythm is not None:
            self.rhythm.note_action("apply")
        return True

    async def _repair(
        self,
        page: Page,
        target: FormTarget,
        filler: FormFiller,
        job: JobListing,
        resume_path: Optional[Path],
    ) -> bool:
        """
        Answers whatever the failed submission exposed as still missing.

        Returns True only when every remaining required question was resolved.
        A repair that leaves a required field blank is not a repair, and
        clicking Submit again would produce the identical bounce -- so the
        caller must take False as final and hand the application to a person.

        `FormFiller` skips any control that already holds a value, so this
        re-read naturally covers exactly the fields that are still empty.
        """
        try:
            await organic_scroll(page, total_distance=random.randint(200, 420), direction=1)
            await asyncio.sleep(random.uniform(0.5, 1.1))
            await self._fill_identity(page, target.root, filler)
            remaining = await filler.fill(
                target.root,
                resume_path=resume_path,
                job_location=job.location or "",
                upload_resume=False,  # already attached, and re-attaching duplicates it
            )
        except Exception as exc:
            logger.debug(f"Repair pass failed: {exc}")
            return False

        blocking = FormFiller.blocking(remaining)
        if blocking:
            logger.info(f"Repair could not answer: {'; '.join(blocking)}")
            return False
        return True

    @staticmethod
    async def _submission_confirmed(
        page: Page, target: FormTarget, timeout_ms: int = 20000
    ) -> bool:
        """
        Whether the portal actually took the application.

        Two independent signals, because no two of these vendors agree on one:
        a confirmation appears, or the form itself goes away. Polled rather than
        waited on a single selector, so whichever arrives first ends it. A
        timeout is a "no" -- the caller must not record an application on the
        strength of a click.

        The confirmation is looked for in the *form's* frame first and then in
        the top-level page, and both are needed. An embedded board replaces the
        contents of its own iframe with "Thank you", which a page-level search
        cannot see -- and a page-level search is what this used to do, so on an
        embedded board a successful application would have been recorded as
        "not confirmed" and handed back for manual review. A vendor hosted on
        its own domain does the opposite and navigates the whole page, which the
        frame search alone would miss.
        """
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        confirmation = (
            "text=/thank you/i, text=/application (has been )?(received|submitted)/i, "
            "text=/successfully applied/i, text=/we('| ha)?ve received/i, "
            "[class*='confirmation'], [class*='thank-you'], [data-testid*='confirmation']"
        )
        while asyncio.get_event_loop().time() < deadline:
            for scope in (target.frame, page):
                try:
                    if await scope.locator(confirmation).first.count() > 0:
                        return True
                except Exception:
                    continue
            try:
                if await target.root.count() == 0:
                    return True
            except Exception:
                # The form's frame went away, which on these portals means the
                # page navigated to a confirmation.
                return True
            await asyncio.sleep(0.75)
        return False


class ApplyRouter:
    """
    Sends each listing to the engine that can actually submit it.

    Discovery's widest tier surfaces non-Easy-Apply listings once the operator
    asks for them, so a single run now holds two kinds of job. The rule lives
    here rather than at each call site because there are three of them -- the
    launcher, `pipeline` and `apply` -- and a routing rule copied three times is
    a routing rule that will be corrected in two.

    It presents the same `apply_to_job` as the engines it wraps, so the callers'
    loops do not change at all.
    """

    def __init__(
        self,
        easy_apply: Any,
        db: Database,
        external: Optional["ExternalApplyEngine"] = None,
    ):
        self.easy_apply = easy_apply
        self.db = db
        self.external = external

    async def apply_to_job(self, job: JobListing) -> bool:
        """
        Try Easy Apply, then fall through to the external engine.

        `is_easy_apply` is a hint about which to try FIRST, never a gate. It is
        set at discovery time from the text of a button on the search card, and
        in practice it has been True for every posting ever recorded -- so
        branching on it sent everything to Easy Apply and nothing to the
        external engine, which was built, enabled, and unreachable. Postings
        whose button turns out not to be there were marked SKIPPED inside the
        Easy Apply engine and never retried, so an entire session could end with
        matches found and nothing applied to.
        """
        tried_easy = False
        if getattr(job, "is_easy_apply", True):
            tried_easy = True
            if await self.easy_apply.apply_to_job(job):
                return True
            # Only a missing button justifies trying the other engine. A failure
            # part-way through a modal has already interacted with the posting,
            # and starting again elsewhere would risk a duplicate application.
            if not getattr(self.easy_apply, "_last_no_easy_apply", False):
                return False
            logger.info(
                f"No Easy Apply button on {job.title}; trying the external route."
            )

        if self.external is not None:
            return await self.external.apply_to_job(job)

        if tried_easy:
            self.db.update_job_status(
                job.job_id, JobStatus.SKIPPED,
                match_reason="No Easy Apply button, and external_apply is not 'apply'",
            )
            return False

        # Surfaced under "discover", which is exactly the setting that says to
        # find these and leave them alone. Recorded as skipped rather than
        # failed: nothing went wrong, and the outreach path still wants it.
        logger.info(
            f"{job.title} at {job.company} is not Easy Apply; "
            f"left for outreach (external_apply is not 'apply')."
        )
        self.db.update_job_status(
            job.job_id, JobStatus.SKIPPED,
            match_reason="External application not enabled (external_apply is not 'apply')",
        )
        return False


def build_apply_router(
    page: Page,
    config: AppConfig,
    profile: CandidateProfile,
    db: Database,
    form_solver: FormSolver,
    resume_tailor: Optional[ResumeTailorEngine] = None,
    browser_session: Optional[Any] = None,
    rhythm: Optional[SessionRhythm] = None,
) -> ApplyRouter:
    """
    The apply engine(s) for this run.

    The external engine is built only under `external_apply: "apply"`. Under
    "off" and "discover" it does not exist, which is a stronger guarantee than
    building one that checks a flag before every action.
    """
    from src.modules.easy_apply import EasyApplyEngine

    easy = EasyApplyEngine(
        page, config, profile, db, form_solver,
        resume_tailor=resume_tailor, browser_session=browser_session, rhythm=rhythm,
    )

    external = None
    if getattr(config.search, "external_apply_submits", False):
        external = ExternalApplyEngine(
            page, config, profile, db, form_solver,
            resume_tailor=resume_tailor, browser_session=browser_session, rhythm=rhythm,
        )
        logger.info(
            f"External applications enabled: {len(SUPPORTED)} known ATS vendors "
            f"plus any site carrying a recognisable application form. "
            f"{', '.join(n for n, _, _ in KNOWN_UNSUPPORTED)} are skipped (each needs an account)."
        )

    return ApplyRouter(easy, db, external)


def _host(url: str) -> str:
    """The hostname of a URL, for log lines that should not carry a query string."""
    try:
        from urllib.parse import urlparse

        return urlparse(url or "").netloc or (url or "")[:60]
    except Exception:
        return (url or "")[:60]
