"""
Finding the application form on a page the code has never seen.

This is the test file for the change that makes the agent work anywhere, and
every case in it is about one of the two ways that goes wrong:

  - **Missing a real form.** The vendor whitelist this replaced could not see a
    Greenhouse form embedded in a company's careers page, because the top-level
    URL is the company's and the form is in an iframe. That is not an exotic
    layout; it is the vendor's own recommended integration.
  - **Filling something that is not a form.** Far worse. A page with a
    newsletter box, a contact form and a search field is form-shaped all over,
    and a scorer that counts inputs will happily type a candidate's phone number
    into a marketing list. `not_an_application.html` exists so that failure has
    a test rather than a customer.

The scoring cases need a browser. Vendor detection and the deny-list do not, so
they run everywhere including inside the frozen builds.

    ./venv/bin/python tests/test_form_scout.py
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.modules.form_scout import (
    MIN_FORM_SCORE,
    _plausible_application,
    _score,
    blocked_vendor,
    detect_ats,
    detect_bot_wall,
    find_application_form,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class VendorDetectionTests(unittest.TestCase):
    """URL-based vendor matching, which still runs first when it can."""

    def test_known_vendors_resolve(self):
        for url, expected in [
            ("https://boards.greenhouse.io/acme/jobs/1", "Greenhouse"),
            ("https://jobs.lever.co/acme/abc", "Lever"),
            ("https://jobs.ashbyhq.com/acme/xyz", "Ashby"),
            ("https://acme.smartrecruiters.com/oc/job", "SmartRecruiters"),
            ("https://apply.workable.com/acme/j/ABC", "Workable"),
            ("https://acme.recruitee.com/o/staff-engineer", "Recruitee"),
            ("https://acme.applytojob.com/apply/xyz", "JazzHR"),
        ]:
            with self.subTest(url=url):
                ats, why = detect_ats(url)
                self.assertIsNotNone(ats, why)
                self.assertEqual(ats.name, expected)

    def test_account_required_portals_are_refused_by_name(self):
        for url, expected in [
            ("https://acme.wd5.myworkdayjobs.com/careers/job/1", "Workday"),
            ("https://acme.taleo.net/careersection/x", "Taleo"),
            ("https://careers-acme.icims.com/jobs/1", "iCIMS"),
            ("https://career5.successfactors.eu/career?x=1", "SuccessFactors"),
        ]:
            with self.subTest(url=url):
                ats, why = detect_ats(url)
                self.assertIsNone(ats)
                self.assertIn(expected, why)

    def test_an_unknown_portal_has_no_profile_but_is_not_refused(self):
        # This is the behaviour change. An unrecognised URL yields no vendor
        # profile and, crucially, no refusal reason -- which is the signal to
        # try the structural path rather than to give up. The old contract
        # treated the empty reason as "skip", and that is what limited the agent
        # to four vendors.
        ats, why = detect_ats("https://apply.acme.com/job/9")
        self.assertIsNone(ats)
        self.assertEqual(why, "")

    def test_an_empty_url_is_not_a_portal(self):
        self.assertIsNone(detect_ats("")[0])
        self.assertIn("no application URL", detect_ats("")[1])

    def test_the_deny_list_reads_every_frame_not_just_the_top_url(self):
        # A careers page that embeds Workday in an iframe is still Workday, and
        # its own URL says nothing about that.
        self.assertIn(
            "Workday",
            blocked_vendor([
                "https://acme.com/careers/staff-engineer",
                "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1",
            ]),
        )

    def test_an_ordinary_page_is_not_blocked(self):
        self.assertEqual(
            blocked_vendor(["https://acme.com/careers", "https://cdn.acme.com/x.js"]), ""
        )


class _PageCase(unittest.TestCase):
    """Loads one fixture into a real browser."""

    fixture: str = ""

    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        try:
            cls._loop.run_until_complete(cls._open())
        except Exception as exc:  # no browser binary, or it failed to start
            cls._loop.close()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")

    @classmethod
    async def _open(cls):
        from playwright.async_api import async_playwright

        cls._pw = await async_playwright().start()
        cls._browser = await cls._pw.chromium.launch()
        cls.page = await cls._browser.new_page()
        await cls.page.set_content((FIXTURES / cls.fixture).read_text(encoding="utf-8"))
        # srcdoc frames mount asynchronously; without this the frame list is
        # still just the main frame and the embedded case would pass for the
        # wrong reason.
        await cls.page.wait_for_timeout(400)
        cls.target = await find_application_form(cls.page)

    @classmethod
    def tearDownClass(cls):
        async def shut():
            await cls._browser.close()
            await cls._pw.stop()

        cls._loop.run_until_complete(shut())
        cls._loop.close()


class EmbeddedBoardTests(_PageCase):
    """The Greenhouse embed, which the URL says nothing about."""

    fixture = "embedded_board.html"

    def test_the_form_is_found_inside_the_iframe(self):
        self.assertIsNotNone(self.target, "the embedded application form was not found")
        self.assertIsNot(
            self.target.frame, self.page.main_frame,
            "the form was matched in the top-level page, not the embed -- "
            "the newsletter or the search box has been mistaken for it",
        )

    def test_it_is_recognised_as_greenhouse_by_its_markup(self):
        self.assertEqual(self.target.vendor, "Greenhouse")

    def test_it_found_the_upload_and_the_questions(self):
        self.assertTrue(self.target.has_file_input)
        # first, last, email, phone, notice period.
        self.assertGreaterEqual(self.target.controls, 5)


class ModernAtsTests(_PageCase):
    """A form built entirely out of ARIA widgets."""

    fixture = "modern_ats.html"

    def test_the_widget_form_scores_as_an_application(self):
        self.assertIsNotNone(self.target)
        self.assertGreaterEqual(self.target.score, MIN_FORM_SCORE)

    def test_it_prefers_the_form_over_the_main_that_wraps_it(self):
        # Scoping matters downstream: a <main>-scoped read picks up the page
        # heading and any navigation as "questions".
        tag = self._loop.run_until_complete(
            self.target.root.evaluate("el => el.tagName.toLowerCase()")
        )
        self.assertEqual(tag, "form")


class NotAnApplicationTests(_PageCase):
    """The page that must not be filled under any circumstances."""

    fixture = "not_an_application.html"

    def test_nothing_on_a_marketing_page_is_treated_as_an_application(self):
        self.assertIsNone(
            self.target,
            "a newsletter, contact or login form scored as a job application -- "
            "this is the path that types a candidate's details into a mailing list",
        )


class CaptchaDetectionTests(unittest.TestCase):
    """
    Telling a challenge from a badge.

    Both fixtures carry reCAPTCHA. Only one of them asks the applicant for
    anything, and the difference decides whether the agent can apply through
    Greenhouse and Lever at all -- both ship invisible v3 on ordinary forms, so
    a detector that fires on "reCAPTCHA is present" refuses every application to
    the two most widely used ATSs there are.
    """

    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        try:
            cls._loop.run_until_complete(cls._start())
        except Exception as exc:
            cls._loop.close()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")

    @classmethod
    async def _start(cls):
        from playwright.async_api import async_playwright

        cls._pw = await async_playwright().start()
        cls._browser = await cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        async def shut():
            await cls._browser.close()
            await cls._pw.stop()

        cls._loop.run_until_complete(shut())
        cls._loop.close()

    def _wall_for(self, fixture: str) -> str:
        async def check():
            page = await self._browser.new_page()
            # Offline: the fixture references google.com, and a test must not
            # depend on the network to decide what it asserts.
            await page.route("**://www.google.com/**", lambda route: route.abort())
            await page.set_content((FIXTURES / fixture).read_text(encoding="utf-8"))
            await page.wait_for_timeout(250)
            found = await detect_bot_wall(page)
            await page.close()
            return found

        return self._loop.run_until_complete(check())

    def test_an_invisible_recaptcha_badge_is_not_a_challenge(self):
        self.assertEqual(
            self._wall_for("invisible_recaptcha.html"), "",
            "an ordinary Greenhouse-style form with invisible reCAPTCHA v3 was "
            "reported as a challenge -- this refuses every Greenhouse and Lever "
            "application on the platform",
        )

    def test_a_rendered_checkbox_widget_is_a_challenge(self):
        self.assertIn("reCAPTCHA", self._wall_for("real_captcha.html"))

    def test_a_form_behind_an_invisible_recaptcha_is_still_fillable(self):
        # The other half of the same guarantee: not flagging the badge is only
        # useful if the form itself is then found and filled.
        async def find():
            page = await self._browser.new_page()
            await page.route("**://www.google.com/**", lambda route: route.abort())
            await page.set_content(
                (FIXTURES / "invisible_recaptcha.html").read_text(encoding="utf-8")
            )
            await page.wait_for_timeout(250)
            target = await find_application_form(page)
            await page.close()
            return target

        target = self._loop.run_until_complete(find())
        self.assertIsNotNone(target)
        self.assertTrue(target.has_file_input)


class PlausibilityFloorTests(unittest.TestCase):
    """
    The floor that the weighted score alone does not enforce.

    A contact form that accepts an attachment scores well over the threshold on
    control count and file input alone. What it does not have is two independent
    pieces of identity evidence, which every real application asks for.
    """

    def test_a_contact_form_with_an_attachment_is_refused(self):
        # name + email + message + a file. "email" is the only identity hit --
        # a bare "name" deliberately is not one -- so this clears the score but
        # not the floor.
        stats = {"controls": 3, "idHits": 1, "files": 1, "submits": 1,
                 "spam": 0, "passwords": 0, "tag": "form"}
        self.assertGreater(_score(stats), MIN_FORM_SCORE)
        self.assertFalse(_plausible_application(stats))

    def test_a_minimal_real_application_is_accepted(self):
        # Full name + email + resume: the smallest genuine application there is,
        # and three hits, because "resume" counts as one.
        stats = {"controls": 2, "idHits": 3, "files": 1, "submits": 1,
                 "spam": 0, "passwords": 0, "tag": "form"}
        self.assertGreater(_score(stats), MIN_FORM_SCORE)
        self.assertTrue(_plausible_application(stats))

    def test_a_password_field_disqualifies_outright(self):
        stats = {"controls": 6, "idHits": 4, "files": 1, "submits": 1,
                 "spam": 0, "passwords": 1, "tag": "form"}
        self.assertFalse(_plausible_application(stats))


if __name__ == "__main__":
    unittest.main(verbosity=2)
