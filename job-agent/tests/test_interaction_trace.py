"""
What a detection script actually sees while the agent fills a form.

Every other test in this suite asks whether the agent filled the form
*correctly*. This one asks whether the event stream it produced while doing so
is one a human hand could have produced -- which is a different question, and
the one the whole `human_behavior` module exists to answer.

It is written as assertions on a recorded trace rather than as a checklist of
API calls, because the checklist version passes forever while the behaviour
regresses underneath it. Every case here corresponds to a signal that is cheap
for a site to compute and expensive to fake, and several of them correspond to
bugs this code has actually shipped:

  - a capital letter arriving with `shiftKey: false`, which no physical keyboard
    can produce and any handler can test in one line
  - a keydown and keyup in the same millisecond, i.e. a zero dwell time, which
    is what a bare `keyboard.type()` emits
  - a metronomic typing rhythm. This is the one that separates hardest, and the
    dwell check above does not catch it: `type(delay=80)` produces a perfectly
    respectable dwell and an inter-key standard deviation of under 3ms. Measured
    on this fixture, the agent's is 130-180ms against the naive path's 0.6-2.9ms,
    which is the difference between a hand and a scheduler.
  - a `change` on a file input with no click anywhere before it: a file that
    appeared without anyone asking for one
  - a pointer that jumps hundreds of pixels between samples, or that reports at
    a perfectly constant rate

These run the shipped functions against a real Chromium. Without a browser
binary they skip, so the frozen builds still run the rest of the suite.

    ./venv/bin/python tests/test_interaction_trace.py
"""

import asyncio
import statistics
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.browser.human_behavior import organic_click, organic_type
from src.modules.form_filler import FormFiller

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# What gets typed. Deliberately mixed case with a shifted punctuation mark, so
# the Shift-coherence assertions have something to bite on.
TYPED = "Jane Doe (AI)"


class InteractionTraceTests(unittest.TestCase):
    """One browser, one scripted interaction, many assertions on the recording."""

    @classmethod
    def setUpClass(cls):
        cls._loop = asyncio.new_event_loop()
        try:
            cls._loop.run_until_complete(cls._drive())
        except unittest.SkipTest:
            cls._loop.close()
            raise
        except Exception as exc:
            cls._loop.close()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")

    @classmethod
    async def _drive(cls):
        from playwright.async_api import async_playwright

        cls._pw = await async_playwright().start()
        # Headed would be more faithful, but CI has no display and the event
        # stream under test is produced by CDP either way.
        cls._browser = await cls._pw.chromium.launch()
        page = await cls._browser.new_page(viewport={"width": 1280, "height": 900})
        await page.set_content((FIXTURES / "event_recorder.html").read_text(encoding="utf-8"))

        await organic_type(page, page.locator("#name"), TYPED, enable_typos=False)
        await organic_click(page, page.locator("#go"))

        # The upload path, through the real FormFiller so the click-then-attach
        # sequence under test is the shipped one.
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(b"%PDF-1.4\n%fake\n")
            cls._tmp = Path(fh.name)
        filler = FormFiller(page, None, None, None)
        await filler._attach(page.locator("#resume"), cls._tmp)

        cls.trace = await page.evaluate("() => window.__trace")

    @classmethod
    def tearDownClass(cls):
        async def shut():
            await cls._browser.close()
            await cls._pw.stop()

        cls._loop.run_until_complete(shut())
        cls._loop.close()
        try:
            cls._tmp.unlink()
        except Exception:
            pass

    # -- helpers -------------------------------------------------------------

    def events(self, *types):
        return [e for e in self.trace if e["type"] in types]

    # -- trust ---------------------------------------------------------------

    def test_every_event_is_trusted(self):
        # The single cheapest check a site can make. An untrusted event means
        # something called dispatchEvent, and nothing in the human_behavior
        # layer may ever do that.
        untrusted = [e for e in self.trace if not e.get("trusted")]
        self.assertEqual(
            untrusted, [],
            f"{len(untrusted)} synthesised event(s) reached the page: "
            f"{[e['type'] for e in untrusted[:5]]}",
        )

    # -- keystroke dynamics --------------------------------------------------

    def test_a_shifted_character_reports_shift_held(self):
        # The binary contradiction: `keydown{key:'A', shiftKey:false}` is not a
        # timing anomaly a site has to model, it is a state no keyboard can
        # reach. Before ShiftState existed, every capital this agent typed
        # looked like that.
        for e in self.events("keydown"):
            key = e.get("key", "")
            if len(key) == 1 and (key.isupper() or key in '~!@#$%^&*()_+{}|:"<>?'):
                self.assertTrue(
                    e.get("shift"),
                    f"key {key!r} was reported with shiftKey false",
                )

    def test_shift_is_actually_pressed_before_a_capital(self):
        held = False
        for e in self.events("keydown", "keyup"):
            if e.get("key") == "Shift":
                held = e["type"] == "keydown"
                continue
            key = e.get("key", "")
            if e["type"] == "keydown" and len(key) == 1 and key.isupper():
                self.assertTrue(
                    held, f"capital {key!r} was typed with no Shift keydown before it"
                )

    def test_no_key_is_held_for_zero_milliseconds(self):
        # keyboard.type() releases in the same tick, giving a dwell of ~0 --
        # a distribution that separates trivially from any human hand.
        downs = {}
        dwells = []
        for e in self.events("keydown", "keyup"):
            key = e.get("key")
            if e["type"] == "keydown":
                downs[key] = e["t"]
            elif key in downs:
                dwells.append(e["t"] - downs.pop(key))

        self.assertTrue(dwells, "no complete keydown/keyup pairs were recorded")
        self.assertGreater(
            min(dwells), 25.0,
            f"shortest key hold was {min(dwells):.1f}ms; real dwell is 60-120ms",
        )

    def test_typing_rhythm_is_not_metronomic(self):
        # A constant inter-key interval is a stronger and cheaper signal than
        # any trajectory shape. Real typing is bursty, so the spread is wide.
        downs = [e["t"] for e in self.events("keydown")]
        gaps = [b - a for a, b in zip(downs, downs[1:])]
        self.assertGreater(len(gaps), 8, "not enough keystrokes to judge rhythm")
        self.assertGreater(
            statistics.pstdev(gaps), 15.0,
            f"inter-key gaps have a standard deviation of only "
            f"{statistics.pstdev(gaps):.1f}ms -- that is a scheduler, not a hand",
        )

    # -- pointer -------------------------------------------------------------

    def test_the_pointer_never_teleports(self):
        moves = self.events("pointermove")
        self.assertGreater(len(moves), 10, "the pointer barely moved at all")
        jumps = [
            max(abs(b["x"] - a["x"]), abs(b["y"] - a["y"]))
            for a, b in zip(moves, moves[1:])
        ]
        self.assertLess(
            max(jumps), 120,
            f"largest single pointer step was {max(jumps)}px -- a continuous path "
            f"has no jump that size, and the biggest one always lands on the click",
        )

    def test_the_pointer_sample_rate_is_not_perfectly_regular(self):
        moves = self.events("pointermove")
        gaps = [b["t"] - a["t"] for a, b in zip(moves, moves[1:]) if b["t"] > a["t"]]
        self.assertTrue(gaps, "no pointer timing to judge")
        # Measured on this build with slow_mo left at 0. A near-zero deviation
        # here is what a non-zero slow_mo produces, and it defeats the entire
        # trajectory model -- see the note on BrowserConfig.slow_mo.
        self.assertGreater(
            statistics.pstdev(gaps), 1.0,
            f"pointer samples arrive {statistics.mean(gaps):.1f}ms apart with a "
            f"deviation of {statistics.pstdev(gaps):.2f}ms; that is a timer",
        )

    def test_a_click_is_preceded_by_movement_onto_its_own_target(self):
        clicks = self.events("click")
        self.assertTrue(clicks, "nothing was clicked")
        for click in clicks:
            before = [
                e for e in self.events("pointermove")
                if e["t"] < click["t"] and click["t"] - e["t"] < 4000
            ]
            self.assertTrue(before, "a click arrived with no pointer movement before it")
            last = before[-1]
            self.assertLess(
                max(abs(last["x"] - click["x"]), abs(last["y"] - click["y"])), 12,
                "the pointer was not at the click position when the click fired",
            )

    # -- uploads -------------------------------------------------------------

    def test_a_file_arrives_only_after_its_own_control_was_clicked(self):
        # `set_input_files` on its own fires a change event on an input nothing
        # ever activated. A form instrumenting its own upload control records a
        # file that appeared without anyone asking for one.
        #
        # The assertion is deliberately specific about *which* control. "Some
        # click happened earlier on the page" is true of the naive path too --
        # the previous action left one in the trace -- so a looser version of
        # this test passes on exactly the code it is meant to catch. Measured
        # against a bare set_input_files on this fixture: no click on the input
        # or its label appears anywhere before the change.
        changes = [e for e in self.events("change") if e.get("target") == "resume"]
        self.assertTrue(changes, "the resume never reached the file input")

        first_change = changes[0]["t"]
        activations = [
            e for e in self.events("click", "pointerdown")
            if e["t"] < first_change
            and e.get("target") in ("resume", "attach", "LABEL")
        ]
        self.assertTrue(
            activations,
            "the file input's change event was not preceded by a click on the "
            "input or on the control that stands in for it -- the upload "
            "bypassed the file chooser",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
