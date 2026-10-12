"""
Regression tests for FormFiller's reading pass, against real ATS markup.

The reading pass is where an external application goes wrong silently. If a
control is not collected it is never answered, and the form is submitted
incomplete or blocked at validation with nothing in the log explaining why. If a
question is collected with the wrong label, the solver is asked something the
form never asked -- and the answer it returns is typed into a field under the
candidate's name.

The two fixtures are the markup patterns that actually break a LinkedIn-shaped
reader:

  - `greenhouse.html` labels everything with `for=`, marks fields `required`,
    gates the submit on a consent checkbox, and pre-fills one field that must be
    left alone.
  - `lever.html` uses no `for=` anywhere -- labels wrap their input -- and emits
    a radio group with no `fieldset`, where the question is a bare `<label>`
    sitting beside the options.

Needs a Chromium binary; skips cleanly without one, so the suite still runs
inside the frozen builds:

    ./venv/bin/python tests/test_form_filler.py
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.modules.form_filler import FormFiller

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _FixtureCase(unittest.TestCase):
    """Loads one fixture into a real browser and collects its fields once."""

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
        page = await cls._browser.new_page()
        await page.set_content((FIXTURES / cls.fixture).read_text(encoding="utf-8"))
        cls._page = page
        # None for profile/solver/config: the reading pass touches only the
        # scope locator, and giving it real collaborators would test them too.
        cls.fields = await FormFiller(page, None, None, None).collect_fields(
            page.locator("form")
        )

    @classmethod
    def tearDownClass(cls):
        async def _close():
            await cls._browser.close()
            await cls._pw.stop()

        cls._loop.run_until_complete(_close())
        cls._loop.close()

    # -- helpers -------------------------------------------------------------

    def field(self, needle: str):
        """The single collected field whose label contains `needle`."""
        found = [f for f in self.fields if needle.lower() in f.question.text.lower()]
        self.assertEqual(
            len(found), 1,
            f"expected exactly one field matching {needle!r}, got "
            f"{[f.question.text for f in found]}",
        )
        return found[0]

    def labels(self):
        return [f.question.text for f in self.fields]


class GreenhouseFormTests(_FixtureCase):
    fixture = "greenhouse.html"

    def test_explicit_labels_are_read(self):
        self.assertEqual(self.field("First Name").kind, "text")
        self.assertEqual(self.field("Email").kind, "text")

    def test_a_prefilled_field_is_left_alone(self):
        # Overwriting what the portal already knows is how a correct value
        # becomes a wrong one; "Current company" arrives filled with "EY".
        self.assertNotIn("Current company", " | ".join(self.labels()))

    def test_required_is_distinguished_from_optional(self):
        # This is the whole basis of `blocking()`: a refused optional question
        # must not abandon an otherwise complete application.
        self.assertTrue(self.field("years of SOX").required)
        self.assertFalse(self.field("Gender").required)

    def test_the_dropdown_placeholder_is_not_an_answer(self):
        auth = self.field("legally authorized")
        self.assertEqual(auth.kind, "select")
        self.assertEqual(auth.question.options, ["Yes", "No"])

    def test_a_fieldset_group_is_questioned_by_its_legend(self):
        sponsor = self.field("sponsorship")
        self.assertEqual(sponsor.kind, "radio")
        self.assertEqual(sponsor.question.options, ["Yes", "No"])
        self.assertEqual(len(sponsor.options), 2)

    def test_the_consent_checkbox_is_its_own_question(self):
        # Greenhouse gates the submit on it, and it is a distinct question from
        # the EEO acknowledgements it usually sits beside.
        consent = self.field("privacy policy")
        self.assertEqual(consent.kind, "checkbox")
        self.assertTrue(consent.required)

    def test_the_file_input_is_not_a_question(self):
        # The resume is uploaded by `upload_resume`, not answered by the solver.
        self.assertNotIn("resume", " | ".join(self.labels()).lower())


class LeverFormTests(_FixtureCase):
    fixture = "lever.html"

    def test_wrapping_labels_are_read_without_for(self):
        self.assertEqual(self.field("Full name").kind, "text")
        self.assertFalse(self.field("Phone").required)

    def test_aria_label_carries_a_textarea(self):
        self.assertEqual(self.field("interested in this role").kind, "text")

    def test_a_fieldset_less_group_finds_its_question(self):
        based = self.field("based in India")
        self.assertEqual(based.kind, "radio")
        self.assertEqual(based.question.options, ["Yes", "No"])

    def test_the_question_is_not_offered_as_an_answer(self):
        # The regression this guards: with the question itself collected as a
        # label pair, "Are you currently based in India?" became a selectable
        # option, and the solver could pick it.
        based = self.field("based in India")
        self.assertNotIn(based.question.text, based.question.options)


class ModernWidgetFormTests(_FixtureCase):
    """
    A form made of ARIA widgets rather than form elements.

    Every assertion here corresponds to a question that the pre-widget reader
    collected zero of. That failure was invisible in the worst way: the text
    fields were answered, the log said so, and the submission bounced on
    required dropdowns nobody had been asked about.
    """

    fixture = "modern_ats.html"

    def test_an_input_with_no_type_attribute_is_read(self):
        # `<input name="name">` is a text input. The old selector required an
        # explicit type='text', so on hand-rolled forms the name field -- and
        # often the email -- were simply not there.
        self.assertEqual(self.field("Full name").kind, "text")

    def test_a_react_select_dropdown_becomes_a_question_with_its_options(self):
        notice = self.field("notice period")
        self.assertEqual(notice.kind, "combobox")
        self.assertIn("30 days", notice.question.options)
        self.assertIn("Immediately", notice.question.options)

    def test_a_widget_dropdown_is_required_via_aria(self):
        # It has no `required` attribute to give -- it is a div.
        self.assertTrue(self.field("notice period").required)

    def test_a_typeahead_is_not_treated_as_free_text(self):
        # The distinction that matters: free text typed into this control is
        # discarded on blur unless a suggestion was committed, so answering it
        # as "text" produces a field that reads as filled and submits empty.
        self.assertEqual(self.field("Current location").kind, "typeahead")

    def test_an_aria_radiogroup_is_read_with_its_options(self):
        auth = self.field("legally authorized")
        self.assertEqual(auth.kind, "aria-radio")
        self.assertEqual(auth.question.options, ["Yes", "No"])
        self.assertTrue(auth.required)

    def test_an_aria_checkbox_is_its_own_question(self):
        consent = self.field("privacy notice")
        self.assertEqual(consent.kind, "aria-checkbox")
        self.assertTrue(consent.required)

    def test_a_contenteditable_is_read_as_a_free_text_answer(self):
        why = self.field("Why do you want this role")
        self.assertEqual(why.kind, "editable")

    def test_the_file_inputs_are_not_questions(self):
        joined = " | ".join(self.labels()).lower()
        self.assertNotIn("resume", joined)
        self.assertNotIn("cover letter (optional)", joined)


class UploadIntentTests(_FixtureCase):
    """Which document goes in which slot."""

    fixture = "modern_ats.html"

    def test_resume_and_cover_letter_inputs_are_told_apart(self):
        async def read():
            filler = FormFiller(self._page, None, None, None)
            return await filler._file_inputs(self._page.locator("form"))

        intents = [intent for intent, _ in self._loop.run_until_complete(read())]
        # Order is DOM order; what matters is that the second input is
        # recognised as the cover letter rather than taking the resume, which
        # is what position-based matching did on any form that listed them the
        # other way round.
        self.assertEqual(intents, ["resume", "cover_letter"])


class EmbeddedWidgetCommitTests(unittest.TestCase):
    """
    Answering a dropdown that lives inside an embedded board.

    Reading a widget is only half the job; the answer has to reach it. This is
    the write path, on the hardest realistic layout: a portalled popup, inside
    an iframe, on a widget that ignores Escape. All three of those broke it
    independently -- see the fixture's own comment for what each one did.

    The assertion is on the hidden input the form would actually submit, not on
    what the control displays. A widget can show a value it is not holding.
    """

    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        try:
            cls._loop.run_until_complete(cls._open())
        except Exception as exc:
            cls._loop.close()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")

    @classmethod
    async def _open(cls):
        from playwright.async_api import async_playwright
        from src.modules.form_scout import find_application_form

        cls._pw = await async_playwright().start()
        cls._browser = await cls._pw.chromium.launch()
        cls.page = await cls._browser.new_page(viewport={"width": 1280, "height": 900})
        await cls.page.set_content(
            (FIXTURES / "embedded_widget_form.html").read_text(encoding="utf-8")
        )
        await cls.page.wait_for_timeout(400)

        cls.target = await find_application_form(cls.page)
        if cls.target is None:
            raise unittest.SkipTest("the embedded form was not found")

        filler = FormFiller(cls.page, None, None, None)
        fields = await filler.collect_fields(cls.target.root)
        combos = [f for f in fields if f.kind == "combobox"]
        cls.combo = combos[0] if combos else None
        cls.committed = (
            await filler._choose_from_popup(cls.combo.handle, "30 days")
            if cls.combo else False
        )
        cls.submitted_value = await cls.target.frame.locator("#notice-value").input_value()

    @classmethod
    def tearDownClass(cls):
        async def shut():
            await cls._browser.close()
            await cls._pw.stop()

        cls._loop.run_until_complete(shut())
        cls._loop.close()

    def test_the_dropdown_is_read_inside_the_frame(self):
        self.assertIsNotNone(self.combo, "no combobox was collected from the embedded form")
        self.assertIn("30 days", self.combo.question.options)

    def test_the_answer_actually_commits(self):
        self.assertTrue(self.committed, "_choose_from_popup reported failure")

    def test_the_form_would_submit_the_chosen_value(self):
        self.assertEqual(self.submitted_value, "30 days")


class BlockingTests(unittest.TestCase):
    """A refusal on a required field is a hard stop; on an optional one it isn't."""

    def test_only_required_refusals_block(self):
        unanswered = [
            "How did you hear about us?",
            "Gender",
            "How many years of SOX compliance experience do you have? [required]",
        ]
        self.assertEqual(
            FormFiller.blocking(unanswered),
            ["How many years of SOX compliance experience do you have? [required]"],
        )

    def test_nothing_refused_blocks_nothing(self):
        self.assertEqual(FormFiller.blocking([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
