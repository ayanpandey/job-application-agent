"""
Regression tests for the screening-answer solver.

These cover the answers that silently cost a candidate the role: a wrong work
authorization or sponsorship answer is an ATS knockout, and the application
still submits successfully, so nothing in the logs ever shows it happened.

Runs on the standard library alone -- no pytest, so it works inside the frozen
double-click builds:

    ./venv/bin/python tests/test_screening_answers.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.models import CandidateProfile, PersonalInfo, ScreeningAnswers
from src.llm.form_solver import FormSolver


def _india_profile() -> CandidateProfile:
    """An Indian citizen in Bengaluru -- the shipped profile shape for both agents."""
    return CandidateProfile(
        personal_info=PersonalInfo(
            first_name="Test", last_name="Candidate", email="t@example.com",
            phone_country_code="+91", phone_number="9999999999",
            city="Bangalore", state="Karnataka", country="India",
        ),
        screening_answers=ScreeningAnswers(
            total_years_experience=2,
            years_experience_by_skill={"python": 4, "machine_learning": 3, "sql": 3, "llms": 2},
            work_authorization={
                "legally_authorized_in_us": False,
                "requires_sponsorship_now_or_future": True,
                "legally_authorized_in_india": True,
                "citizenship": "India",
            },
            preferences={
                "notice_period_weeks": 4, "notice_period_days": 30,
                "desired_salary_inr_lakhs": 28.0, "desired_salary_usd": 120000,
                "willing_to_relocate": True,
            },
        ),
    )


def _solver() -> FormSolver:
    """
    A solver with the LLM path unavailable, so only deterministic rules run.

    Built through the real constructor rather than `__new__`: bypassing
    `__init__` meant every attribute added to the class had to be remembered
    here too, and forgetting one failed as an AttributeError deep inside a rule
    that had nothing to do with the change.

    `llm=None` makes any call into the model path raise, which the solver
    catches and turns into an escalation -- exactly what these tests assert
    should happen when no rule applies.
    """
    solver = FormSolver(None, cache=None)
    solver.prompts = {}
    return solver


YES_NO = ["Yes", "No"]


class WorkAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.solver = _solver()
        self.profile = _india_profile()

    def test_authorized_in_home_country(self):
        """The regression: an Indian citizen must not answer "No" for India."""
        r = self.solver.solve_question(
            "Are you legally authorized to work in India?", "radio", YES_NO, self.profile
        )
        self.assertEqual(r.answer, "Yes")
        self.assertFalse(r.needs_human)

    def test_not_authorized_in_us(self):
        r = self.solver.solve_question(
            "Are you legally authorized to work in the United States?", "radio", YES_NO, self.profile
        )
        self.assertEqual(r.answer, "No")

    def test_no_sponsorship_needed_at_home(self):
        """Sponsorship for India is No -- the old code answered Yes."""
        r = self.solver.solve_question(
            "Will you now or in the future require sponsorship to work in India?",
            "radio", YES_NO, self.profile,
        )
        self.assertEqual(r.answer, "No")

    def test_sponsorship_needed_for_us(self):
        r = self.solver.solve_question(
            "Will you now or in the future require visa sponsorship in the US?",
            "radio", YES_NO, self.profile,
        )
        self.assertEqual(r.answer, "Yes")

    def test_unnamed_country_falls_back_to_job_location(self):
        r = self.solver.solve_question(
            "Are you legally authorized to work for this employer?", "radio", YES_NO,
            self.profile, job_location="Bengaluru, Karnataka, India",
        )
        self.assertEqual(r.answer, "Yes")

    def test_unknown_country_escalates_instead_of_guessing(self):
        r = self.solver.solve_question(
            "Are you legally authorized to work in Japan?", "radio", YES_NO, self.profile
        )
        self.assertTrue(r.needs_human)
        self.assertEqual(r.answer, "")

    def test_work_for_us_is_not_the_united_states(self):
        """"work for us" is the employer, not the country."""
        r = self.solver.solve_question(
            "Are you authorized to work for us?", "radio", YES_NO,
            self.profile, job_location="Pune, India",
        )
        self.assertEqual(r.answer, "Yes")

    def test_answers_use_the_forms_own_option_text(self):
        r = self.solver.solve_question(
            "Are you legally authorized to work in India?", "radio",
            ["Yes, I am authorized", "No, I am not authorized"], self.profile,
        )
        self.assertEqual(r.answer, "Yes, I am authorized")


class CompensationAndNoticeTests(unittest.TestCase):
    def setUp(self):
        self.solver = _solver()
        self.profile = _india_profile()

    def test_ctc_in_lakhs(self):
        r = self.solver.solve_question("Expected CTC (in LPA)?", "number", None, self.profile)
        self.assertEqual(r.answer, "28")

    def test_unmarked_salary_uses_home_market(self):
        """The old code returned 120000 into a field an Indian recruiter reads as lakhs."""
        r = self.solver.solve_question("What is your expected salary?", "text", None, self.profile)
        self.assertEqual(r.answer, "28")

    def test_explicit_usd_still_returns_usd(self):
        r = self.solver.solve_question("Expected annual salary in USD?", "number", None, self.profile)
        self.assertEqual(r.answer, "120000")

    def test_notice_period_numeric_field_returns_days(self):
        r = self.solver.solve_question("Notice period (in days)?", "number", None, self.profile)
        self.assertEqual(r.answer, "30")

    def test_notice_period_in_months(self):
        r = self.solver.solve_question("Notice period in months?", "number", None, self.profile)
        self.assertEqual(r.answer, "1")


class SkillYearsTests(unittest.TestCase):
    def setUp(self):
        self.solver = _solver()
        self.profile = _india_profile()

    def test_word_boundary_prevents_substring_match(self):
        """"sql" must not fire on "PostgreSQL"; unknown skills use the total."""
        r = self.solver.solve_question(
            "How many years of experience do you have with PostgreSQL?", "number", None, self.profile
        )
        self.assertEqual(r.answer, "2")  # total_years_experience, not sql=3

    def test_underscored_skill_matches_spoken_form(self):
        r = self.solver.solve_question(
            "How many years of experience do you have with machine learning?", "number", None, self.profile
        )
        self.assertEqual(r.answer, "3")

    def test_known_skill_beats_total(self):
        r = self.solver.solve_question(
            "How many years of experience do you have in Python?", "number", None, self.profile
        )
        self.assertEqual(r.answer, "4")


class OptionMatchingTests(unittest.TestCase):
    def setUp(self):
        self.solver = _solver()

    def test_no_match_returns_none_not_first_option(self):
        """The old fallback silently answered "Yes" to anything unmatched."""
        self.assertIsNone(self.solver._match_best_option("Maybe someday", YES_NO))

    def test_no_does_not_match_not_applicable(self):
        opts = ["Not applicable", "Yes"]
        self.assertIsNone(self.solver._match_best_option("No", opts))

    def test_sentence_option_matches_on_leading_token(self):
        opts = ["Yes, I have a valid licence", "No, I do not"]
        self.assertEqual(self.solver._match_best_option("No", opts), "No, I do not")

    def test_exact_match_wins(self):
        self.assertEqual(self.solver._match_best_option("Yes", YES_NO), "Yes")


class MissingProfileDataTests(unittest.TestCase):
    def test_absent_preference_escalates(self):
        solver = _solver()
        bare = _india_profile()
        bare.screening_answers.preferences = {}
        r = solver.solve_question("Are you willing to relocate?", "radio", YES_NO, bare)
        self.assertTrue(r.needs_human)


if __name__ == "__main__":
    unittest.main(verbosity=2)
