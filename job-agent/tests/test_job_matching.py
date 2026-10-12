"""
Regression tests for the deterministic pre-screen and the +/-20% experience band.

Two failure modes matter here and they cost in opposite directions. Rejecting a
listing the candidate would have been shortlisted for is invisible -- nothing in
the logs records the job that was never applied to. Sending everything to the
model is visible only as a slow session, but it is what made the agent slower
than a person doing this by hand.

Runs on the standard library alone -- no pytest, so it works inside the frozen
double-click builds:

    ./venv/bin/python tests/test_job_matching.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.models import (
    CandidateProfile, PersonalInfo, ScreeningAnswers, SkillsConfig, ExperienceItem, JobListing,
)
from src.llm.prefilter import JobPreFilter, parse_experience_requirement
from src.llm.matcher import compress_description, search_grid


KEYWORDS = ["ITGC", "Internal Auditor", "IT SOX", "IT Audit", "SOX Compliance", "Technology Risk"]


def _profile(years: int = 3) -> CandidateProfile:
    return CandidateProfile(
        personal_info=PersonalInfo(
            first_name="Test", last_name="Candidate", email="t@example.com",
            phone_country_code="+91", phone_number="9999999999",
            city="Kolkata", state="West Bengal", country="India",
        ),
        skills=SkillsConfig(
            languages=["SQL", "Python"],
            frameworks_and_libraries=["ITGC", "SOX Compliance", "Internal Audit", "Access Management"],
            cloud_and_devops=["SAP", "Workday"],
        ),
        experience=[ExperienceItem(role="Analyst, Digital Risk Consulting", company="Acme Consulting")],
        screening_answers=ScreeningAnswers(
            total_years_experience=years,
            years_experience_by_skill={"itgc": years, "sox": years},
        ),
    )


def _filter(profile=None, **kw) -> JobPreFilter:
    return JobPreFilter(
        profile=profile or _profile(),
        search_keywords=KEYWORDS,
        exclude_title_keywords=kw.pop("exclude_title_keywords", ["intern", "internship"]),
        **kw,
    )


def _job(title: str, description: str = "", company: str = "Acme") -> JobListing:
    return JobListing(
        job_id="1", title=title, company=company, location="Bengaluru",
        url="https://example.com", description=description,
    )


class ExperienceParsingTests(unittest.TestCase):
    def test_range(self):
        band = parse_experience_requirement("We need 3-5 years of experience in audit.")
        self.assertEqual((band.low, band.high), (3.0, 5.0))

    def test_minimum(self):
        band = parse_experience_requirement("Minimum 5 years of experience required.")
        self.assertEqual(band.low, 5.0)
        self.assertIsNone(band.high)

    def test_plus_form(self):
        self.assertEqual(parse_experience_requirement("8+ years of ITGC exp").low, 8.0)

    def test_incidental_year_counts_are_not_requirements(self):
        """"We tripled in the last 5 years" is company copy, not a hiring bar."""
        self.assertIsNone(parse_experience_requirement("We have tripled in size in the last 5 years."))

    def test_months(self):
        band = parse_experience_requirement("6 months of relevant experience.")
        self.assertAlmostEqual(band.low, 0.5, places=2)

    def test_no_statement(self):
        self.assertIsNone(parse_experience_requirement("A great role on a great team."))

    def test_title_implies_a_band_when_the_body_does_not(self):
        """Most postings state no number, so seniority has to carry the test."""
        band = _filter().resolve_band(_job("Senior IT Auditor", "Audit things."))
        self.assertEqual(band.source, "title")
        self.assertEqual(band.low, 5.0)


class ExperienceToleranceTests(unittest.TestCase):
    """The +/-20% band is the point of the feature: near-misses must survive."""

    def test_exact_requirement_is_in_band(self):
        v = _filter().screen(_job("IT Auditor", "3 years of experience in ITGC and SOX required."))
        self.assertEqual(v.years_verdict, "in_band")

    def test_one_year_over_the_bar_is_still_in_band(self):
        """4 years asked, 3 held: the relaxed floor is 3.2, so this is a near-miss, not a reject."""
        v = _filter().screen(_job("Internal Auditor", "Minimum 4 years of experience in internal audit."))
        self.assertNotEqual(v.decision, "reject")

    def test_tolerance_floor_admits_a_20_percent_shortfall(self):
        """5 years asked with a 4-year candidate: 4.0 is exactly the relaxed floor."""
        v = _filter(profile=_profile(years=4)).screen(
            _job("IT Auditor", "5 years of experience in ITGC required.")
        )
        self.assertEqual(v.years_verdict, "in_band")

    def test_large_shortfall_is_rejected_without_an_llm_call(self):
        v = _filter().screen(_job("IT Auditor", "12+ years of experience in audit leadership."))
        self.assertEqual(v.decision, "reject")
        self.assertIn("below", v.summary())

    def test_shortfall_inside_the_grace_window_still_reaches_the_model(self):
        """5+ years asked, 3 held: floor 4.0, shortfall 1.0, inside max_years_shortfall."""
        v = _filter().screen(
            _job("IT SOX Auditor", "5+ years of hands-on experience with ITGC, SOX and access management.")
        )
        self.assertEqual(v.decision, "evaluate")

    def test_being_over_the_band_is_not_a_reject(self):
        """Overqualified by a year or two is how a large share of hires happen."""
        v = _filter(profile=_profile(years=6)).screen(
            _job("IT Auditor", "2-3 years of experience in ITGC and SOX compliance testing.")
        )
        self.assertEqual(v.years_verdict, "over")
        self.assertNotEqual(v.decision, "reject")

    def test_unstated_requirement_is_neutral(self):
        v = _filter().screen(_job("IT Audit Analyst", "Work on ITGC and SOX controls with our team."))
        self.assertEqual(v.years_verdict, "unstated")
        self.assertNotEqual(v.decision, "reject")

    def test_tolerance_is_configurable(self):
        wide = _filter(tolerance=0.5).screen(_job("IT Auditor", "6 years of ITGC experience required."))
        self.assertEqual(wide.years_verdict, "in_band")  # floor drops to 3.0
        narrow = _filter(tolerance=0.0).screen(_job("IT Auditor", "6 years of ITGC experience required."))
        self.assertEqual(narrow.years_verdict, "under")


class SeniorityTests(unittest.TestCase):
    def test_director_is_rejected_for_a_three_year_candidate(self):
        v = _filter().screen(_job("Director of Internal Audit", "Lead the internal audit function."))
        self.assertEqual(v.decision, "reject")

    def test_senior_is_not_rejected_for_a_three_year_candidate(self):
        """Senior implies 5 years; 3 is inside the grace window and worth an opinion."""
        v = _filter().screen(_job("Senior IT Auditor", "ITGC, SOX and access management."))
        self.assertNotEqual(v.decision, "reject")

    def test_internship_is_rejected_for_an_experienced_candidate(self):
        v = _filter(exclude_title_keywords=[]).screen(_job("Audit Intern", "Internship in audit."))
        self.assertEqual(v.decision, "reject")


class ExclusionTests(unittest.TestCase):
    def test_intern_exclusion_does_not_match_internal(self):
        """
        The regression this guards: 'intern' is a substring of 'internal', so a
        substring-matched exclusion list silently discarded every Internal
        Auditor posting -- the most important keyword on an audit profile.
        """
        v = _filter().screen(
            _job("Internal Auditor", "3 years of internal audit and SOX experience required.")
        )
        self.assertNotEqual(v.decision, "reject")

    def test_intern_exclusion_still_matches_an_internship(self):
        self.assertEqual(_filter().screen(_job("Audit Intern", "For students.")).decision, "reject")

    def test_company_exclusion(self):
        v = _filter(exclude_companies=["acme"]).screen(_job("IT Auditor", "ITGC.", company="Acme Corp"))
        self.assertEqual(v.decision, "reject")


class RelevanceTests(unittest.TestCase):
    def test_unrelated_posting_costs_no_model_call(self):
        v = _filter().screen(_job("Registered Nurse - ICU", "Critical care nursing, BLS certification."))
        self.assertEqual(v.decision, "reject")

    def test_unrelated_field_with_matching_seniority_is_still_rejected(self):
        v = _filter().screen(_job("Frontend Developer", "React, TypeScript, 2-4 years of experience."))
        self.assertEqual(v.decision, "reject")

    def test_strong_match_can_fast_track(self):
        v = _filter().screen(_job(
            "IT Audit Analyst - ITGC & SOX",
            "ITGC testing, SOX compliance, access management, internal audit, SAP and Workday. "
            "SQL and Python useful. 2-4 years of experience.",
        ))
        self.assertEqual(v.decision, "fast_track")

    def test_related_but_ambiguous_goes_to_the_model(self):
        v = _filter().screen(_job(
            "Technology Risk Consultant",
            "Advise clients on technology risk frameworks and controls design. 3-5 years of experience.",
        ))
        self.assertEqual(v.decision, "evaluate")


class DescriptionCompressionTests(unittest.TestCase):
    def test_boilerplate_is_stripped(self):
        text = (
            "Requirements: ITGC and SOX testing.\n\n"
            "We are an equal opportunity employer and all applicants will receive "
            "consideration without regard to race, colour or creed.\n\n"
            "Apply now."
        )
        out = compress_description(text)
        self.assertIn("ITGC", out)
        self.assertNotIn("equal opportunity", out.lower())

    def test_long_text_keeps_both_ends(self):
        text = "HEAD requirements " + ("filler " * 2000) + " TAIL qualifications"
        out = compress_description(text, limit=600)
        self.assertLessEqual(len(out), 700)
        self.assertIn("HEAD", out)
        self.assertIn("TAIL", out)


class SearchGridTests(unittest.TestCase):
    """A session never finishes the grid, so its ordering decides what is reachable."""

    class _Cfg:
        class search:
            keywords = ["a", "b", "c"]
            locations = ["x", "y"]
            rotate_search_grid = True
            interleave_locations = True

    def test_covers_every_pair_exactly_once(self):
        pairs = search_grid(self._Cfg())
        self.assertEqual(len(pairs), 6)
        self.assertEqual(len(set(pairs)), 6)

    def test_interleaving_spreads_keywords_before_repeating_a_location(self):
        cfg = self._Cfg()
        cfg.search.rotate_search_grid = False
        pairs = search_grid(cfg)
        self.assertEqual([kw for kw, _ in pairs[:3]], ["a", "b", "c"])
        cfg.search.rotate_search_grid = True

    def test_empty_inputs_do_not_raise(self):
        cfg = self._Cfg()
        cfg.search.keywords = []
        self.assertEqual(search_grid(cfg), [])
        cfg.search.keywords = ["a", "b", "c"]


class SearchWideningTests(unittest.TestCase):
    """
    An empty search is usually the filters intersecting to nothing, not an
    empty market. These cover the ladder that re-issues it with them relaxed.
    """

    def _engine(self, **search_overrides):
        from src.modules.discovery import JobDiscoveryEngine
        from src.core.config import SearchConfig

        # The real SearchConfig rather than a stand-in: `external_apply` is read
        # through a derived property, and a hand-rolled stub answers a getattr
        # for it with whatever it happens to define -- including, if the stub
        # spells it as a property on a class that is never instantiated, a bare
        # property object, which is truthy. Building the real model is what
        # keeps this test measuring the shipped semantics.
        fields = {
            "keywords": ["IT Audit"],
            "locations": ["Remote"],
            "easy_apply_only": True,
            "date_posted": "past-week",
            "experience_levels": ["entry_level", "associate", "mid_senior"],
            "widen_when_thin": True,
            "widen_below_results": 8,
        }
        fields.update(search_overrides)
        search = SearchConfig(**fields)

        class _Cfg:
            pass

        engine = JobDiscoveryEngine.__new__(JobDiscoveryEngine)
        engine.config = _Cfg()
        engine.config.search = search
        return engine

    def test_first_tier_is_always_the_configured_filters(self):
        self.assertEqual(self._engine()._filter_tiers()[0], {})

    def test_recency_then_seniority_are_relaxed_in_that_order(self):
        tiers = self._engine()._filter_tiers()
        self.assertEqual(tiers[1], {"date_posted": "past-month"})
        self.assertEqual(tiers[2], {"date_posted": "past-month", "experience_levels": []})

    def test_easy_apply_is_kept_until_the_operator_asks_for_external(self):
        # "off" is the default, and it must never surface a listing the agent
        # has not been told it may act on.
        self.assertTrue(all(t.get("easy_apply_only", True) for t in self._engine()._filter_tiers()))

    def test_both_external_modes_open_the_widest_tier(self):
        # Surfacing and submitting are separate settings, but the search that
        # finds the listing is the same one: "discover" has to widen too, or
        # there is nothing for the outreach path to work with.
        for mode in ("discover", "apply"):
            with self.subTest(mode=mode):
                widest = self._engine(external_apply=mode)._filter_tiers()[-1]
                self.assertIs(widest["easy_apply_only"], False)

    def test_widening_can_be_turned_off_entirely(self):
        self.assertEqual(self._engine(widen_when_thin=False)._filter_tiers(), [{}])

    def test_no_redundant_tier_when_already_at_the_widest_date(self):
        tiers = self._engine(date_posted="past-month")._filter_tiers()
        self.assertNotIn({"date_posted": "past-month"}, tiers)

    def test_relaxed_filters_reach_the_url(self):
        engine = self._engine()
        tight = engine._build_search_url("IT Audit", "Delhi")
        wide = engine._build_search_url("IT Audit", "Delhi", 25, {"date_posted": "past-month", "experience_levels": []})
        self.assertIn("f_TPR=r604800", tight)
        self.assertIn("f_E=", tight)
        self.assertIn("f_TPR=r2592000", wide)
        self.assertNotIn("f_E=", wide)
        # Paging is what reaches results 26+; page one alone was all the agent
        # ever saw of a 25-result page.
        self.assertIn("start=25", wide)
        self.assertIn("f_AL=true", wide)


if __name__ == "__main__":
    unittest.main(verbosity=2)
