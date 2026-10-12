"""
The LinkedIn Easy Apply step, now that it runs on the shared FormFiller.

`_fill_current_step_fields` used to be a 200-line reader of its own. It is now a
delegation, which is the point -- the rule that a refused question must stop the
submission has one implementation instead of two. These tests cover what the
delegation has to preserve, because the LinkedIn path is the live one:

  - the phone number is answered from the profile, and never becomes a question
    for the solver (which has no reason to know it, and every reason not to
    guess it)
  - the solver is asked for the rest of the step in a single batch
  - a refusal comes back to the caller, which abandons the application rather
    than submitting it part-filled

Needs a Chromium binary; skips cleanly without one:

    ./venv/bin/python tests/test_easy_apply_fill.py
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.modules.easy_apply import EasyApplyEngine
from src.modules.form_filler import FormFiller

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PHONE = "+91 98765 43210"

# What the solver is willing to answer. The salary question is refused, which is
# the realistic case: it is required, and country-scoped compensation is exactly
# what the solver is built to decline rather than invent.
ANSWERS = {
    "How many years of IT audit experience do you have?": "4",
    "Are you legally authorized to work in India?": "Yes",
    "Will you now or in the future require sponsorship?": "No",
}


class _RecordingSolver:
    """Stands in for FormSolver, and records what it was asked."""

    def __init__(self):
        self.asked = []
        self.calls = 0

    def solve_questions(self, questions, profile, job_location, batch):
        self.calls += 1
        self.asked.extend(q.text for q in questions)
        out = []
        for q in questions:
            answer = ANSWERS.get(q.text.strip())
            if answer is None:
                out.append(SimpleNamespace(
                    answer="", needs_human=True,
                    reasoning="No salary figure in the profile; will not invent one.",
                ))
            else:
                out.append(SimpleNamespace(answer=answer, needs_human=False, reasoning=""))
        return out


class EasyApplyStepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        try:
            cls._loop.run_until_complete(cls._run_step())
        except unittest.SkipTest:
            cls._loop.close()
            raise
        except Exception as exc:
            cls._loop.close()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")

    @classmethod
    async def _run_step(cls):
        from playwright.async_api import async_playwright

        cls._pw = await async_playwright().start()
        cls._browser = await cls._pw.chromium.launch()
        page = await cls._browser.new_page()
        await page.set_content((FIXTURES / "linkedin_modal.html").read_text(encoding="utf-8"))

        cls.solver = _RecordingSolver()
        engine = EasyApplyEngine.__new__(EasyApplyEngine)
        engine.page = page
        engine.solver = cls.solver
        engine.profile = SimpleNamespace(
            personal_info=SimpleNamespace(phone_number=PHONE),
            resume_path="",  # no upload in this test; the file input is left alone
        )
        engine.config = SimpleNamespace(
            speed=SimpleNamespace(action_pace=0.15, batch_form_questions=True),
        )

        cls.modal = page.locator("div.artdeco-modal")
        cls.unfilled = await engine._fill_current_step_fields(cls.modal, None, None)
        cls.page = page

    @classmethod
    def tearDownClass(cls):
        async def _close():
            await cls._browser.close()
            await cls._pw.stop()

        cls._loop.run_until_complete(_close())
        cls._loop.close()

    def test_the_phone_is_answered_from_the_profile(self):
        value = self._loop.run_until_complete(
            self.modal.locator("input[id*='phoneNumber']").first.input_value()
        )
        self.assertEqual(value, PHONE)

    def test_the_phone_is_never_asked_of_the_solver(self):
        # It already holds a value by the time the reader runs, so it is not a
        # question. Sending it would invite a guessed number onto the application.
        self.assertNotIn(
            "Mobile phone number",
            " | ".join(self.solver.asked),
        )

    def test_the_step_is_solved_in_one_batch(self):
        # The reason the step is read in full before anything is answered: a
        # per-field round trip held the modal open for one request each.
        self.assertEqual(self.solver.calls, 1)
        self.assertEqual(len(self.solver.asked), 4)

    def test_the_answers_reach_the_form(self):
        years = self._loop.run_until_complete(
            self.modal.locator("input[type='number']").first.input_value()
        )
        self.assertEqual(years, "4")
        auth = self._loop.run_until_complete(
            self.modal.locator("select").first.input_value()
        )
        self.assertEqual(auth, "Yes")
        checked = self._loop.run_until_complete(
            self.modal.locator("input[name='sponsorship'][value='No']").first.is_checked()
        )
        self.assertTrue(checked)

    def test_a_refusal_comes_back_to_the_caller(self):
        # `_handle_modal_steps` abandons the application while this is non-empty,
        # which is the whole reason it is returned rather than logged.
        self.assertEqual(len(self.unfilled), 1)
        self.assertIn("salary", self.unfilled[0].lower())

    def test_a_refused_required_question_is_marked(self):
        # LinkedIn blocks on any refusal, but the marking is what lets the
        # external-portal path tell a blocking question from an optional one.
        self.assertTrue(self.unfilled[0].endswith("[required]"), self.unfilled[0])
        self.assertEqual(len(FormFiller.blocking(self.unfilled)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
