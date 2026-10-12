"""
The external ATS path: vendor detection, routing, and the fail-closed switch.

Every test here is about something that must NOT happen. This is the one engine
that submits an application to a third party under the candidate's name with no
LinkedIn in the middle validating anything, so its interesting behaviour is all
refusal:

  - it does nothing at all unless the operator set `external_apply: "apply"`
  - it will not touch a portal that needs an account (Workday, Taleo, iCIMS) or
    one it does not recognise
  - it fills the candidate's identity from the profile, so those fields never
    become questions for a model to guess at

Detection and routing need no browser. The identity pass does; it skips cleanly
without Chromium.

    ./venv/bin/python tests/test_external_apply.py
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import SearchConfig
from src.core.models import JobListing, JobStatus, PersonalInfo
from src.modules.external_apply import (
    ApplyRouter,
    ExternalApplyEngine,
    build_apply_router,
    detect_ats,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _config(external_apply="apply", dry_run=True):
    return SimpleNamespace(
        search=SearchConfig(keywords=["IT Audit"], external_apply=external_apply),
        general=SimpleNamespace(dry_run=dry_run, human_in_the_loop=False),
        limits=SimpleNamespace(max_applications_per_day=30),
        speed=SimpleNamespace(action_pace=0.15, batch_form_questions=True),
    )


def _job(is_easy_apply=False):
    return JobListing(
        job_id="4455644312", title="IT Audit Manager", company="Acme",
        location="Bengaluru", url="https://www.linkedin.com/jobs/view/4455644312",
        is_easy_apply=is_easy_apply,
    )


class _RecordingDb:
    def __init__(self):
        self.statuses = []
        self.applied = []
        self.daily = 0

    def update_job_status(self, job_id, status, match_reason=""):
        self.statuses.append((job_id, status, match_reason))

    def mark_job_applied(self, job_id, resume_file=""):
        self.applied.append(job_id)

    def get_daily_activity_count(self, activity_type):
        return self.daily

    def record_external_status(self, job_id, status, source="linkedin"):
        return True


class DetectionTests(unittest.TestCase):
    def test_the_four_supported_vendors(self):
        for url, expected in (
            ("https://boards.greenhouse.io/acme/jobs/123", "Greenhouse"),
            ("https://acme.com/careers?gh_jid=99", "Greenhouse"),
            ("https://jobs.lever.co/acme/abc-123", "Lever"),
            ("https://jobs.ashbyhq.com/acme/xyz", "Ashby"),
            ("https://careers.smartrecruiters.com/Acme/74", "SmartRecruiters"),
        ):
            with self.subTest(url=url):
                ats, why = detect_ats(url)
                self.assertIsNotNone(ats, why)
                self.assertEqual(ats.name, expected)

    def test_account_walled_portals_are_skipped_by_name(self):
        # Named rather than lumped in with "unrecognised" so the run log says
        # why. Workday in particular is not a selector problem: starting one
        # leaves a half-built account behind on the employer's tenant.
        for url, expected in (
            ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/123", "Workday"),
            ("https://acme.taleo.net/careersection/2/jobdetail.ftl", "Taleo"),
            ("https://careers-acme.icims.com/jobs/1234/login", "iCIMS"),
        ):
            with self.subTest(url=url):
                ats, why = detect_ats(url)
                self.assertIsNone(ats)
                self.assertIn(expected, why)

    def test_an_unknown_portal_has_no_vendor_profile(self):
        # An unrecognised URL yields no profile and, importantly, no refusal
        # reason. The empty reason is what sends the engine down the structural
        # path in form_scout rather than skipping the posting; a *non-empty*
        # reason is the deliberate refusal (see the deny-list case above).
        #
        # The old contract treated both as "skip", which is what limited the
        # agent to four vendors and made an embedded Greenhouse form -- the
        # vendor's own recommended integration -- unreachable.
        ats, why = detect_ats("https://apply.acme.com/job/9")
        self.assertIsNone(ats)
        self.assertEqual(why, "")

    def test_an_empty_url_is_not_a_portal(self):
        self.assertIsNone(detect_ats("")[0])


class RoutingTests(unittest.TestCase):
    """Which engine a listing reaches, and what happens when there isn't one."""

    def setUp(self):
        self.db = _RecordingDb()
        self.easy = SimpleNamespace(seen=[])

        async def easy_apply(job):
            self.easy.seen.append(job.job_id)
            return True

        self.easy.apply_to_job = easy_apply

    def test_an_easy_apply_listing_goes_to_the_linkedin_engine(self):
        router = ApplyRouter(self.easy, self.db, external=None)
        self.assertTrue(asyncio.run(router.apply_to_job(_job(is_easy_apply=True))))
        self.assertEqual(self.easy.seen, ["4455644312"])

    def test_an_external_listing_is_skipped_when_no_engine_exists(self):
        # This is the "discover" case: the listing was surfaced deliberately, and
        # must be left for outreach rather than applied to or reported as failed.
        router = ApplyRouter(self.easy, self.db, external=None)
        self.assertFalse(asyncio.run(router.apply_to_job(_job())))
        self.assertEqual(self.easy.seen, [])
        self.assertEqual(len(self.db.statuses), 1)
        self.assertEqual(self.db.statuses[0][1], JobStatus.SKIPPED)

    def test_an_external_listing_reaches_the_external_engine(self):
        seen = []

        async def external_apply(job):
            seen.append(job.job_id)
            return True

        router = ApplyRouter(
            self.easy, self.db, external=SimpleNamespace(apply_to_job=external_apply)
        )
        self.assertTrue(asyncio.run(router.apply_to_job(_job())))
        self.assertEqual(seen, ["4455644312"])
        self.assertEqual(self.easy.seen, [])


class SwitchTests(unittest.TestCase):
    """The engine exists only under "apply", and does nothing without it."""

    def test_the_router_builds_no_external_engine_unless_asked(self):
        for mode in ("off", "discover"):
            with self.subTest(mode=mode):
                router = build_apply_router(
                    None, _config(external_apply=mode), None, _RecordingDb(), None
                )
                self.assertIsNone(router.external)

    def test_apply_mode_builds_one(self):
        router = build_apply_router(None, _config("apply"), None, _RecordingDb(), None)
        self.assertIsNotNone(router.external)

    def test_the_engine_opens_nothing_if_the_switch_is_off(self):
        # Belt and braces: the router already declines to build it, so this is
        # the second of two independent guards on the same door.
        db = _RecordingDb()
        engine = ExternalApplyEngine.__new__(ExternalApplyEngine)
        engine.config = _config(external_apply="discover")
        engine.db = db
        engine.page = None  # touching the browser at all would raise

        portal, applied = asyncio.run(engine._apply_to_job(_job()))
        self.assertIsNone(portal)
        self.assertFalse(applied)
        self.assertEqual(db.statuses, [])

    def test_the_daily_ceiling_is_shared_with_linkedin(self):
        db = _RecordingDb()
        db.daily = 30
        engine = ExternalApplyEngine.__new__(ExternalApplyEngine)
        engine.config = _config("apply")
        engine.db = db
        engine.page = None

        portal, applied = asyncio.run(engine._apply_to_job(_job()))
        self.assertIsNone(portal)
        self.assertFalse(applied)


class PortalCleanupTests(unittest.TestCase):
    """The portal tab is ours; it does not outlive the application."""

    def test_the_tab_is_closed_even_when_the_run_raises(self):
        # The failing run is exactly the one that leaves a tab behind, and one
        # per external job would accumulate for the rest of the session.
        closed = []

        class _FakePortal:
            def is_closed(self):
                return False

            async def close(self):
                closed.append(True)

        db = _RecordingDb()
        engine = ExternalApplyEngine.__new__(ExternalApplyEngine)
        engine.db = db
        engine.page = object()  # the session page, which must NOT be closed

        async def boom(job):
            engine._portal = _FakePortal()
            raise RuntimeError("the portal fell over mid-application")

        engine._apply_to_job = boom

        self.assertFalse(asyncio.run(engine.apply_to_job(_job())))
        self.assertEqual(closed, [True])
        self.assertEqual(db.statuses[0][1], JobStatus.FAILED)

    def test_the_session_page_is_never_closed(self):
        # An in-place navigation makes the portal *be* the session page. Closing
        # it would take the browser out from under the rest of the run.
        engine = ExternalApplyEngine.__new__(ExternalApplyEngine)

        class _Page:
            closed = False

            def is_closed(self):
                return False

            async def close(self):
                _Page.closed = True

        page = _Page()
        engine.page = page
        asyncio.run(engine._close_portal(page))
        self.assertFalse(_Page.closed)


class IdentityFillTests(unittest.TestCase):
    """
    The candidate's own details are typed from the profile, not asked of a model.
    """

    PROFILE = PersonalInfo(
        first_name="Jane", last_name="Doe",
        email="jane@example.com", phone_number="9876543210",
        linkedin_url="https://linkedin.com/in/janedoe",
    )

    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        try:
            cls._loop.run_until_complete(cls._fill())
        except Exception as exc:
            cls._loop.close()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")

    @classmethod
    async def _fill(cls):
        from playwright.async_api import async_playwright
        from src.modules.form_filler import FormFiller

        cls._pw = await async_playwright().start()
        cls._browser = await cls._pw.chromium.launch()
        page = await cls._browser.new_page()
        await page.set_content((FIXTURES / "greenhouse.html").read_text(encoding="utf-8"))

        engine = ExternalApplyEngine.__new__(ExternalApplyEngine)
        engine.config = _config("apply")
        engine.profile = SimpleNamespace(personal_info=cls.PROFILE, resume_path="")

        form = page.locator("form#application_form")
        filler = FormFiller(page, engine.profile, None, engine.config)
        await engine._fill_identity(page, form, filler)

        cls.form = form
        cls.remaining = [f.question.text for f in await filler.collect_fields(form)]

    @classmethod
    def tearDownClass(cls):
        async def _close():
            await cls._browser.close()
            await cls._pw.stop()

        cls._loop.run_until_complete(_close())
        cls._loop.close()

    def _value(self, selector):
        return self._loop.run_until_complete(
            self.form.locator(selector).first.input_value()
        )

    def test_the_identity_fields_are_filled_from_the_profile(self):
        self.assertEqual(self._value("input#first_name"), "Jane")
        self.assertEqual(self._value("input#last_name"), "Doe")
        self.assertEqual(self._value("input#email"), "jane@example.com")
        self.assertEqual(self._value("input#phone"), "9876543210")

    def test_they_are_no_longer_questions_for_the_solver(self):
        # The point of filling them first: a control that already holds a value
        # is skipped by the reader, so no model is ever asked to produce this
        # candidate's email address.
        joined = " | ".join(self.remaining).lower()
        for gone in ("first name", "last name", "email", "phone"):
            with self.subTest(field=gone):
                self.assertNotIn(gone, joined)

    def test_the_real_questions_still_are(self):
        # And nothing else was swallowed on the way past.
        joined = " | ".join(self.remaining).lower()
        self.assertIn("sox compliance", joined)
        self.assertIn("legally authorized", joined)
        self.assertIn("sponsorship", joined)

    def test_a_field_the_portal_already_filled_is_left_alone(self):
        self.assertEqual(self._value("input#prefilled"), "EY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
