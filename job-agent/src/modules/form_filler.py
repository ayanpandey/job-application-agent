"""
Generic application-form filling, shared by LinkedIn Easy Apply and external ATS portals.

This is the machinery that used to live inside
`EasyApplyEngine._fill_current_step_fields`. Almost none of it was ever
LinkedIn-specific -- it reads text inputs, dropdowns, radio groups and file
uploads out of a container, resolves the whole lot in one FormSolver request,
and reports back the questions it refused to answer. Only the phone-number
selector and the modal scoping belonged to LinkedIn.

It is extracted rather than copied because of the one rule that matters most in
this system: **a question the solver declined must never be answered by
guessing, and must stop the submission.** A blank required field fails
validation loudly and costs nothing. A guessed one submits silently under the
candidate's name and costs them the role. Two implementations of that rule would
mean two chances to get it wrong, and the second one would drift.

What is added here beyond what the LinkedIn modal needed:

  - checkboxes, which gate most Greenhouse submissions (consent, EEO)
  - labels that wrap their input or sit beside it with no `for=`, as Lever emits
  - `[required]` / `[aria-required]` detection, so a refused *optional* question
    does not block an application that is otherwise complete

And beyond what any *native* form needs, because the modern ATSs stopped using
native controls some years ago:

  - **Comboboxes.** `<select>` is now the exception. Greenhouse's current UI,
    Ashby, Workable and anything built on react-select render a `div` with
    `role="combobox"`, a popup of `role="option"` nodes, and a hidden input that
    holds the value. The old reader saw none of it, so every dropdown question
    on those portals was silently skipped -- and a skipped required dropdown is
    a failed submission with no explanation.
  - **Typeaheads.** Location, school and degree fields accept free text but
    *discard it on blur* unless a suggestion was committed. Typing "Bengaluru"
    and moving on leaves the field empty, which is the most confusing possible
    failure: the log says it was answered and the form says it is required.
  - **ARIA radios and checkboxes**, which have no `input` behind them at all.
  - **`contenteditable`** cover-letter editors.
  - **More than one file input.** A form asking for a resume *and* a cover
    letter used to get the resume in whichever slot came first in the DOM.

One rule governs all of them and is worth stating once: a control is only
recorded as answered when the control itself is read back and agrees. Every
widget here can accept a value visually and hold nothing, and a form that
submits with an empty required field is a rejection the candidate never sees.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

from playwright.async_api import Locator, Page

from src.core import paths
from src.core.logger import logger
from src.core.models import CandidateProfile
from src.llm.form_solver import FormSolver, FormQuestion
from src.browser.utils import paced_delay
from src.browser.human_behavior import (
    organic_click,
    organic_type,
    organic_mouse_move,
    organic_select_option,
    OrganicIdleDrift,
)


@dataclass
class FormField:
    """One unanswered control on the form, resolved to a live handle."""

    # text | select | radio | checkbox | combobox | typeahead | aria-radio
    # | aria-checkbox | editable | date
    kind: str
    question: FormQuestion
    handle: Optional[Locator] = None
    # Radio groups keep (label text, label handle) pairs so an answer maps back
    # to a real element instead of being re-found by a text substring match.
    options: List[Tuple[str, Locator]] = field(default_factory=list)
    required: bool = False


class FormFiller:
    """Reads and fills whatever form controls are inside a given container."""

    def __init__(
        self,
        page: Page,
        profile: CandidateProfile,
        solver: FormSolver,
        config: Any,
    ):
        self.page = page
        self.profile = profile
        self.solver = solver
        self.config = config

    # -- reading -------------------------------------------------------------

    @staticmethod
    async def _is_required(elem: Locator) -> bool:
        try:
            if await elem.get_attribute("required") is not None:
                return True
            return (await elem.get_attribute("aria-required") or "").lower() == "true"
        except Exception:
            return False

    async def _label_for(self, scope: Locator, elem: Locator) -> str:
        """
        The human-readable prompt for a control.

        Four sources in descending reliability: an explicit `label[for=...]`, an
        `aria-label`, a wrapping or preceding `<label>` (Lever and hand-rolled
        forms rarely bother with `for=`), and finally the placeholder.
        """
        try:
            elem_id = await elem.get_attribute("id") or ""
            if elem_id:
                label = scope.locator(f"label[for={json.dumps(elem_id)}]").first
                if await label.count() > 0:
                    text = (await label.inner_text()).strip()
                    if text:
                        return text

            aria = (await elem.get_attribute("aria-label") or "").strip()
            if aria:
                return aria

            # Wrapping or adjacent label, resolved in the page so the DOM walk
            # is one round trip rather than several.
            text = await elem.evaluate(
                """el => {
                    const wrap = el.closest('label');
                    if (wrap && wrap.innerText.trim()) return wrap.innerText.trim();
                    let n = el.previousElementSibling;
                    while (n) {
                        if (n.tagName === 'LABEL' && n.innerText.trim()) return n.innerText.trim();
                        n = n.previousElementSibling;
                    }
                    const grp = el.closest('div,fieldset,li');
                    const lbl = grp && grp.querySelector('label');
                    return lbl && lbl.innerText.trim() ? lbl.innerText.trim() : '';
                }"""
            )
            if text and str(text).strip():
                return str(text).strip()

            # Last resorts. `name` is often a usable prompt on hand-rolled forms
            # ("why_this_role"), but it is the weakest signal here, so it only
            # applies once every real label source has come up empty.
            for source in ("placeholder", "name"):
                value = (await elem.get_attribute(source) or "").strip()
                if value:
                    return value.replace("_", " ").strip()
            return "Question"
        except Exception:
            return "Question"

    async def collect_fields(self, scope: Locator) -> List[FormField]:
        """
        Every control on `scope` that still needs an answer.

        Handles are resolved up front rather than walked by index: a resume
        upload re-renders part of the form, and indexing a live locator after
        that silently skips fields as the positions shift underneath.
        """
        fields: List[FormField] = []

        # Text, number and free-text areas.
        #
        # `input:not([type])` is in the list because an input with no type
        # attribute *is* a text input, and hand-rolled forms -- exactly the ones
        # the generic path exists to handle -- write them that way constantly.
        # The old selector matched only an explicit type='text', so on those
        # forms the name and email fields were invisible to the reader.
        for elem in await scope.locator(
            "input[type='text']:visible, input[type='number']:visible, "
            "input[type='email']:visible, input[type='tel']:visible, "
            "input[type='url']:visible, input:not([type]):visible, "
            "textarea:visible"
        ).all():
            try:
                if (await elem.input_value()).strip():
                    continue
            except Exception:
                continue  # detached between snapshot and read
            label = await self._label_for(scope, elem)
            field_type = (await elem.get_attribute("type")) or "text"

            # An input that is also a combobox must not be treated as free text.
            # Typing into one and walking away leaves the field empty: the
            # widget only keeps a value that came from its own option list.
            role = await self._combobox_role(elem)
            if role == "typeahead":
                fields.append(FormField(
                    "typeahead", FormQuestion(label, "text"), elem,
                    required=await self._is_required(elem),
                ))
                continue
            if role == "combobox":
                options = await self._peek_options(elem)
                fields.append(FormField(
                    "combobox", FormQuestion(label, "dropdown", options), elem,
                    required=await self._is_required(elem),
                ))
                continue

            fields.append(FormField(
                "text", FormQuestion(label, field_type), elem,
                required=await self._is_required(elem),
            ))

        # Date pickers. Asked for as an ISO date so the answer has one shape
        # regardless of how the control chooses to render it.
        for elem in await scope.locator("input[type='date']:visible").all():
            try:
                if (await elem.input_value()).strip():
                    continue
            except Exception:
                continue
            label = await self._label_for(scope, elem)
            fields.append(FormField(
                "date", FormQuestion(f"{label} (answer as YYYY-MM-DD)", "date"), elem,
                required=await self._is_required(elem),
            ))

        # Native dropdowns.
        for elem in await scope.locator("select:visible").all():
            try:
                options = await elem.locator("option").all_inner_texts()
            except Exception:
                continue
            options = [
                o.strip() for o in options
                if o.strip() and "select an option" not in o.lower()
            ]
            if not options:
                continue
            label = await self._label_for(scope, elem)
            fields.append(FormField(
                "select", FormQuestion(label, "dropdown", options), elem,
                required=await self._is_required(elem),
            ))

        # Radio groups. A fieldset is the well-formed case; ATS portals also
        # emit bare groups sharing a `name`, so both are collected.
        seen_groups: set = set()
        for fieldset in await scope.locator("fieldset:visible").all():
            try:
                if await fieldset.locator("input[type='radio']:checked").count() > 0:
                    continue
                radios = fieldset.locator("input[type='radio']")
                if await radios.count() == 0:
                    continue
                legend = fieldset.locator("legend").first
                question = (
                    (await legend.inner_text()).strip()
                    if await legend.count() > 0 else "Options"
                )
                name = await radios.first.get_attribute("name") or ""
            except Exception:
                continue

            pairs = await self._label_pairs(fieldset, options_only=True)
            if not pairs:
                continue
            if name:
                seen_groups.add(name)
            fields.append(FormField(
                "radio", FormQuestion(question, "radio", [t for t, _ in pairs]),
                options=pairs,
                required=await self._is_required(radios.first),
            ))

        for group in await self._loose_radio_groups(scope, seen_groups):
            fields.append(group)

        # Checkboxes. Individually, not as a group: consent and EEO
        # acknowledgements are independent questions and answering one does not
        # imply the other.
        for elem in await scope.locator("input[type='checkbox']:visible").all():
            try:
                if await elem.is_checked():
                    continue
            except Exception:
                continue
            label = await self._label_for(scope, elem)
            fields.append(FormField(
                "checkbox", FormQuestion(label, "checkbox", ["Yes", "No"]), elem,
                required=await self._is_required(elem),
            ))

        fields.extend(await self._collect_widgets(scope))
        return fields

    # -- non-native widgets --------------------------------------------------

    async def _collect_widgets(self, scope: Locator) -> List[FormField]:
        """
        The controls that are not HTML form elements at all.

        Every ATS built in the last few years renders its dropdowns, and often
        its radios, as ARIA widgets over plain divs. They carry no value, no
        `required` attribute and no label association, so none of the native
        readers above can see them -- which meant that on a current-generation
        Greenhouse form the agent read the text inputs, answered them, and
        submitted with every dropdown blank.
        """
        found: List[FormField] = []

        # Comboboxes that are not inputs: react-select and its many clones.
        for elem in await scope.locator(
            "[role='combobox']:visible, [role='listbox']:visible, "
            "[class*='select__control']:visible, [class*='Select-control']:visible"
        ).all():
            try:
                tag = (await elem.evaluate("el => el.tagName.toLowerCase()")) or ""
                # An <input> or <select> with role=combobox was already handled
                # by the native readers; picking it up again would ask the same
                # question twice and answer it into a stale handle.
                if tag in ("input", "select", "textarea"):
                    continue
                if await self._widget_has_value(elem):
                    continue
            except Exception:
                continue

            label = await self._widget_label(scope, elem)
            options = await self._peek_options(elem)
            found.append(FormField(
                "combobox", FormQuestion(label, "dropdown", options), elem,
                required=await self._aria_required(elem),
            ))

        # ARIA radio groups: a group element whose children carry role="radio".
        for group in await scope.locator("[role='radiogroup']:visible").all():
            try:
                if await group.locator("[role='radio'][aria-checked='true']").count() > 0:
                    continue
                radios = await group.locator("[role='radio']").all()
                if not radios:
                    continue
            except Exception:
                continue

            pairs: List[Tuple[str, Locator]] = []
            for radio in radios:
                name = await self._accessible_name(radio)
                if name:
                    pairs.append((name, radio))
            if not pairs:
                continue

            label = await self._widget_label(scope, group)
            found.append(FormField(
                "aria-radio", FormQuestion(label, "radio", [t for t, _ in pairs]),
                options=pairs, required=await self._aria_required(group),
            ))

        # ARIA checkboxes, individually -- consent and EEO are separate questions.
        for elem in await scope.locator("[role='checkbox']:visible").all():
            try:
                if (await elem.get_attribute("aria-checked") or "").lower() == "true":
                    continue
            except Exception:
                continue
            label = await self._accessible_name(elem) or await self._widget_label(scope, elem)
            found.append(FormField(
                "aria-checkbox", FormQuestion(label, "checkbox", ["Yes", "No"]), elem,
                required=await self._aria_required(elem),
            ))

        # Rich-text cover-letter editors.
        for elem in await scope.locator("[contenteditable='true']:visible").all():
            try:
                if (await elem.inner_text()).strip():
                    continue
            except Exception:
                continue
            label = await self._widget_label(scope, elem)
            found.append(FormField(
                "editable", FormQuestion(label, "textarea"), elem,
                required=await self._aria_required(elem),
            ))

        return found

    async def _combobox_role(self, elem: Locator) -> str:
        """
        Classifies an `<input>` as plain text, a typeahead, or a combobox.

        The distinction decides whether free text is allowed to stand. A
        typeahead (`aria-autocomplete="list"`) accepts anything but keeps only
        what came from its suggestion list; a combobox input has a fixed option
        set. Both must commit a suggestion, and neither may simply be typed into.
        """
        try:
            return await elem.evaluate(
                """el => {
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const auto = (el.getAttribute('aria-autocomplete') || '').toLowerCase();
                    const pop  = (el.getAttribute('aria-haspopup') || '').toLowerCase();
                    const owns = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
                    const hasList = !!el.getAttribute('list');
                    const inSelect = !!el.closest("[class*='select__control'],[class*='Select-control']");
                    if (auto === 'list' || auto === 'both' || hasList) return 'typeahead';
                    if (role === 'combobox' || pop === 'listbox' || inSelect) return 'combobox';
                    if (owns) {
                        const t = el.ownerDocument.getElementById(owns);
                        if (t && (t.getAttribute('role') || '') === 'listbox') return 'combobox';
                    }
                    return 'text';
                }"""
            ) or "text"
        except Exception:
            return "text"

    async def _peek_options(self, elem: Locator) -> List[str]:
        """
        Opens a combobox far enough to read its choices, then closes it again.

        The options usually do not exist in the DOM until the popup is opened,
        and the solver needs them: an unconstrained answer to "Which office?"
        is a guess, while a list turns it into a choice. Opening a dropdown to
        see what is in it is also exactly what a person filling the form does,
        so this costs nothing in plausibility.
        """
        try:
            await organic_click(self.page, elem, post_delay_range=(0.25, 0.5))
        except Exception:
            return []

        options: List[str] = []
        try:
            await asyncio.sleep(random.uniform(0.25, 0.5))
            options = await self._read_open_options(elem)
        except Exception:
            options = []

        # Close it again so the next control is not read through an open popup,
        # and *verify* that it closed.
        #
        # Escape is what a person presses and what most of these widgets honour,
        # but not all of them do -- and the ones that do not used to break every
        # dropdown answer on the form. The popup stayed open, and the click that
        # `_choose_from_popup` makes to open it later toggled it shut instead;
        # the options were then unreadable and a perfectly answerable question
        # was reported as a refusal. So if Escape did not work, toggle it closed
        # the same way it was opened.
        try:
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(random.uniform(0.15, 0.35))
            if await self._popup_is_open(elem):
                await organic_click(self.page, elem, post_delay_range=(0.15, 0.3))
        except Exception:
            pass
        return options

    @staticmethod
    async def _popup_is_open(elem: Locator) -> bool:
        """Whether this control's option list is currently on screen."""
        try:
            return bool(await elem.evaluate(
                """el => {
                    const doc = el.ownerDocument;
                    const owns = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
                    const named = owns ? doc.getElementById(owns) : null;
                    const lists = named ? [named] : Array.from(doc.querySelectorAll(
                        "[role='listbox'],[class*='select__menu'],[class*='menu-list']"));
                    return lists.some(n => {
                        if (!n || n.hidden) return false;
                        const r = n.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                }"""
            ))
        except Exception:
            return False

    async def _read_open_options(self, elem: Locator) -> List[str]:
        """The visible choices of a combobox whose popup is currently open."""
        try:
            texts = await elem.evaluate(
                """el => {
                    const doc = el.ownerDocument;
                    let list = null;
                    const owns = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
                    if (owns) list = doc.getElementById(owns);
                    if (!list) {
                        const near = el.closest('div,section,fieldset,li');
                        list = near && near.querySelector("[role='listbox'],[class*='select__menu'],[class*='menu-list']");
                    }
                    // Popups are very often portalled to <body>, outside the
                    // control's own subtree, so the document-wide sweep is the
                    // case that actually fires on react-select.
                    if (!list) {
                        const all = Array.from(doc.querySelectorAll(
                            "[role='listbox'],[class*='select__menu'],[class*='menu-list']"));
                        list = all.find(n => n.getBoundingClientRect().height > 0) || null;
                    }
                    if (!list) return [];
                    return Array.from(list.querySelectorAll("[role='option'],li,[class*='select__option']"))
                        .map(n => (n.innerText || '').trim())
                        .filter(t => t && t.length < 120);
                }"""
            )
        except Exception:
            return []

        seen: List[str] = []
        for text in texts or []:
            cleaned = str(text).strip()
            if cleaned and cleaned not in seen and "select..." not in cleaned.lower():
                seen.append(cleaned)
        return seen[:40]

    @staticmethod
    async def _widget_has_value(elem: Locator) -> bool:
        """Whether a non-native control already shows a chosen value."""
        try:
            return bool(await elem.evaluate(
                """el => {
                    const t = (el.innerText || '').trim();
                    if (t && !/^select\\.?\\.?\\.?$|^choose|^-+$/i.test(t)) return true;
                    const hidden = el.querySelector('input[type=hidden]');
                    return !!(hidden && hidden.value);
                }"""
            ))
        except Exception:
            return False

    @staticmethod
    async def _aria_required(elem: Locator) -> bool:
        """`required` for controls that have no `required` attribute to give."""
        try:
            return bool(await elem.evaluate(
                """el => {
                    if ((el.getAttribute('aria-required') || '') === 'true') return true;
                    if (el.hasAttribute('required')) return true;
                    const group = el.closest('div,fieldset,li,section');
                    if (!group) return false;
                    // The asterisk convention, which is how most of these forms
                    // actually mark a required field.
                    const label = group.querySelector('label,legend,[class*="label"]');
                    return !!(label && /\\*\\s*$|\\*\\s/.test(label.innerText || ''));
                }"""
            ))
        except Exception:
            return False

    @staticmethod
    async def _accessible_name(elem: Locator) -> str:
        """An element's accessible name, as a screen reader would compute it."""
        try:
            name = await elem.evaluate(
                """el => {
                    const byId = el.getAttribute('aria-labelledby');
                    if (byId) {
                        const parts = byId.split(/\\s+/)
                            .map(id => el.ownerDocument.getElementById(id))
                            .filter(Boolean)
                            .map(n => (n.innerText || '').trim())
                            .filter(Boolean);
                        if (parts.length) return parts.join(' ');
                    }
                    const aria = (el.getAttribute('aria-label') || '').trim();
                    if (aria) return aria;
                    return (el.innerText || el.getAttribute('title') || '').trim();
                }"""
            )
            return str(name or "").strip()[:200]
        except Exception:
            return ""

    async def _widget_label(self, scope: Locator, elem: Locator) -> str:
        """
        The prompt for a widget that has no `<label for=>` to point at it.

        Falls back through the accessible name and then the nearest preceding
        text in the widget's own field group, which is where these forms
        actually put the question.
        """
        name = await self._accessible_name(elem)
        if name and len(name) < 160:
            return name
        try:
            text = await elem.evaluate(
                """el => {
                    const group = el.closest('div,fieldset,li,section');
                    if (!group) return '';
                    const lbl = group.querySelector('label,legend,[class*="label"],[class*="Label"]');
                    if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                    let n = el.previousElementSibling;
                    while (n) {
                        if (n.innerText && n.innerText.trim()) return n.innerText.trim();
                        n = n.previousElementSibling;
                    }
                    return '';
                }"""
            )
            return str(text or "").strip()[:200] or "Question"
        except Exception:
            return "Question"

    @staticmethod
    async def _label_pairs(scope: Locator, options_only: bool = False) -> List[Tuple[str, Locator]]:
        """
        (text, handle) for each label under `scope`, keeping the handle.

        With `options_only`, labels that are not attached to a choice are left
        out. In a fieldset-less group the question is itself a bare `<label>`
        sitting beside the options, and treating it as one made "Are you
        currently based in India?" a selectable answer.
        """
        pairs: List[Tuple[str, Locator]] = []
        for handle in await scope.locator("label").all():
            try:
                text = (await handle.inner_text()).strip()
                if not text:
                    continue
                if options_only and not await handle.evaluate(
                    "el => !!(el.querySelector('input') || el.htmlFor)"
                ):
                    continue
            except Exception:
                continue
            pairs.append((text, handle))
        return pairs

    async def _loose_radio_groups(self, scope: Locator, skip: set) -> List[FormField]:
        """Radio groups sharing a `name` but with no enclosing fieldset."""
        out: List[FormField] = []
        try:
            names = await scope.locator("input[type='radio']:visible").evaluate_all(
                "els => [...new Set(els.map(e => e.name).filter(Boolean))]"
            )
        except Exception:
            return out

        for name in names or []:
            if name in skip:
                continue
            group = scope.locator(f"input[type='radio'][name={json.dumps(name)}]")
            try:
                if await group.count() == 0:
                    continue
                if await scope.locator(
                    f"input[type='radio'][name={json.dumps(name)}]:checked"
                ).count() > 0:
                    continue
                container = group.first.locator(
                    "xpath=ancestor::*[self::div or self::li or self::section][1]"
                )
                pairs = await self._label_pairs(container, options_only=True)
                # The group's question is the label in the container that is not
                # attached to one of the choices. `_label_for` on a radio finds
                # its own wrapping label, which is the option text ("Yes"), not
                # the question.
                question = await container.evaluate(
                    """el => {
                        for (const l of el.querySelectorAll('label')) {
                            if (!l.querySelector('input') && !l.htmlFor && l.innerText.trim())
                                return l.innerText.trim();
                        }
                        const lead = el.querySelector('legend, .question, [class*=label]');
                        return lead && lead.innerText.trim() ? lead.innerText.trim() : '';
                    }"""
                )
                question = str(question or "").strip() or "Options"
            except Exception:
                continue
            if pairs:
                out.append(FormField(
                    "radio", FormQuestion(question, "radio", [t for t, _ in pairs]),
                    options=pairs,
                    required=await self._is_required(group.first),
                ))
        return out

    # -- writing -------------------------------------------------------------

    async def type_value(self, elem: Locator, value: str, numeric: bool = False) -> bool:
        """
        Types a value and confirms the field actually holds it.

        Necessary because of a genuinely nasty interaction: a mouse wheel over a
        *focused* `<input type="number">` increments its value in Chromium, one
        step per tick. The agent scrolls constantly -- to survey a step, to read
        a posting, and through the idle-drift gaze motion -- so a numeric
        screening answer typed as "4" was arriving as "96". Measured on the
        fixtures in tests/fixtures: six consecutive attempts produced 96, 81, 84,
        93, 88 and 90. "How many years of SOX experience do you have?" was being
        answered with two-digit nonsense, and nothing in the logs disagreed
        because the code never read the field back.

        So: type, verify, repair once, verify again, and blur numeric inputs so
        a later scroll cannot mutate what was just confirmed. Returns False if
        the field still does not hold the intended value -- the caller then
        treats it as unanswered rather than submitting a wrong number, which is
        the same rule the solver's refusals follow.
        """
        want = str(value).strip()
        # Typos are for prose. On a number the correction keystrokes are what
        # the field rejects, and a single digit has no realistic typo anyway.
        await organic_type(self.page, elem, want, enable_typos=not numeric)

        for attempt in range(2):
            try:
                got = (await elem.input_value()).strip()
            except Exception:
                return False
            if got == want:
                # Leave the field, always -- not just for numbers.
                #
                # Two separate reasons, and each one alone would justify it.
                # Numeric: a mouse wheel over a *focused* number input mutates
                # it, and the agent scrolls constantly. Everything else: a
                # controlled React input commits on blur, and validation runs
                # there too. A field typed into and never left holds a value the
                # form has not accepted yet, so it reads as answered here and
                # arrives empty at the server. blur() is a real focus change, so
                # the focusout and change events it produces are trusted ones --
                # this is what happens when a person moves to the next field,
                # not a synthesised event.
                try:
                    await elem.evaluate("el => el.blur()")
                except Exception:
                    pass
                return True
            if attempt == 0:
                logger.debug(f"Field held {got!r} after typing {want!r}; correcting.")
                try:
                    await elem.fill("")
                    await elem.type(want, delay=random.uniform(45, 95))
                except Exception:
                    return False
        return False

    # Words that identify which document a file input is asking for. Ordered
    # longest-intent-first: "cover letter" must be tested before "letter", and a
    # field named "resume_or_cv" must not be read as a cover letter.
    _UPLOAD_INTENTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ("cover_letter", ("cover letter", "cover_letter", "coverletter", "covering letter")),
        ("resume", ("resume", "résumé", "cv", "curriculum vitae")),
    )

    async def _file_inputs(self, scope: Locator) -> List[Tuple[str, Locator]]:
        """
        Every file input on the form, tagged with what it is asking for.

        Not filtered by visibility: essentially every modern form hides the real
        input behind a styled drop zone, so requiring it to be visible would
        find nothing at all on exactly the portals that matter.
        """
        out: List[Tuple[str, Locator]] = []
        try:
            handles = await scope.locator("input[type='file']").all()
        except Exception:
            return out

        for handle in handles:
            try:
                # Two strings, and the order they are consulted in is the whole
                # point. `own` is what this input says about itself; `group` is
                # the text around it. Reading them together got the resume slot
                # wrong on any form that listed both uploads as siblings: the
                # nearest shared container's text mentions the cover letter, so
                # every file input on the form matched "cover letter" and the CV
                # was filed under the wrong heading with the resume slot empty.
                own, group = await handle.evaluate(
                    """el => {
                        const label = el.id
                            ? el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]')
                            : el.closest('label');
                        const own = [el.name, el.id, el.getAttribute('aria-label'),
                                     el.getAttribute('data-testid'), label && label.innerText]
                                    .filter(Boolean).join(' ');
                        const box = el.closest('div,fieldset,li,section');
                        // Only trust surrounding text when it can only be about
                        // this input.
                        const alone = box && box.querySelectorAll("input[type='file']").length === 1;
                        return [own.toLowerCase().slice(0, 300),
                                alone ? (box.innerText || '').toLowerCase().slice(0, 300) : ''];
                    }"""
                )
            except Exception:
                own, group = "", ""

            intent = "unknown"
            for source in (own or "", group or ""):
                if not source:
                    continue
                for name, needles in self._UPLOAD_INTENTS:
                    if any(n in source for n in needles):
                        intent = name
                        break
                if intent != "unknown":
                    break
            out.append((intent, handle))
        return out

    async def _attach(self, upload: Locator, path: Path) -> bool:
        """
        Puts a file on an input the way a person does: click first, then attach.

        `set_input_files` alone fires a `change` event on an input that nothing
        ever clicked. That pairing does not occur in a real session -- a file
        arrives because the user activated a control and picked something in the
        OS dialog -- and any form instrumenting its own upload button records
        the change with no preceding pointer activity. So the visible control
        (the styled drop zone or its label, since the input itself is usually
        `display: none`) is clicked first, and the file chooser that opens is
        answered rather than bypassed.

        The click is best-effort: if there is no visible control to press, the
        attachment still happens. A resume that reaches the form imperfectly
        beats an application with no resume on it.
        """
        try:
            opener = await self._visible_opener(upload)
        except Exception:
            opener = None

        if opener is not None:
            try:
                # expect_file_chooser both performs the realistic click and
                # answers the dialog it opens, so no synthetic change event is
                # needed at all on the forms that use a real button.
                async with self.page.expect_file_chooser(timeout=6000) as chooser_info:
                    await organic_click(self.page, opener, post_delay_range=(0.2, 0.5))
                chooser = await chooser_info.value
                await chooser.set_files(str(path))
                return True
            except Exception as exc:
                logger.debug(f"File chooser did not open ({exc}); attaching directly.")

        try:
            await upload.set_input_files(str(path))
            return True
        except Exception as exc:
            logger.debug(f"Could not attach {path.name}: {exc}")
            return False

    @staticmethod
    async def _visible_opener(upload: Locator) -> Optional[Any]:
        """
        The clickable control that stands in for a hidden file input.

        Resolved to an element handle in the page rather than re-expressed as a
        selector. The selector version of this could not be written correctly:
        the opener is "the label whose `for` is this input's id, or failing that
        the nearest button in this input's field group", which is a relationship
        to a specific element and not a query. The attempt to encode it as XPath
        produced an expression that always threw, so the file-chooser path
        silently never ran and every upload fell through to the bare
        `set_input_files` this method exists to avoid.

        An ElementHandle is enough: `organic_click` only needs `bounding_box`
        and `click`, both of which it has.
        """
        try:
            handle = await upload.evaluate_handle(
                """el => {
                    const big = n => n && n.getBoundingClientRect().height > 2;
                    const s = getComputedStyle(el);
                    if (s.display !== 'none' && s.visibility !== 'hidden' && big(el)) return el;
                    const label = el.id
                        ? el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]')
                        : el.closest('label');
                    if (big(label)) return label;
                    const group = el.closest('div,section,fieldset');
                    const btn = group && group.querySelector('button,[role=button],label');
                    return big(btn) ? btn : null;
                }"""
            )
        except Exception:
            return None

        try:
            return handle.as_element()
        except Exception:
            return None

    async def upload_resume(
        self,
        scope: Locator,
        resume_path: Optional[Path],
        cover_letter_path: Optional[Path] = None,
    ) -> bool:
        """
        Attaches the candidate's documents to the file inputs that want them.

        Matched by intent rather than by position. A form asking for a resume
        and a cover letter used to receive the resume in whichever input came
        first in the DOM, so on any portal that put the cover letter above the
        resume the candidate's CV was filed as their cover letter and the resume
        slot was left empty.
        """
        target = resume_path or paths.master_resume(self.profile.resume_path)
        if not target or not Path(target).exists():
            return False
        target = Path(target)

        inputs = await self._file_inputs(scope)
        if not inputs:
            return False

        attached = False
        used_resume = False
        for intent, handle in inputs:
            if intent == "cover_letter":
                if cover_letter_path and Path(cover_letter_path).exists():
                    if await self._attach(handle, Path(cover_letter_path)):
                        logger.info(f"Uploaded cover letter: {Path(cover_letter_path).name}")
                        attached = True
                        await asyncio.sleep(random.uniform(1.0, 1.9))
                # No cover letter to give: leave the field alone rather than
                # filing the resume under the wrong heading.
                continue

            # Resume slots, and unknown ones -- an unlabelled single upload on a
            # job application is asking for a CV.
            if used_resume:
                continue
            if await self._attach(handle, target):
                logger.info(f"Uploaded resume: {target.name}")
                attached = True
                used_resume = True
                await asyncio.sleep(random.uniform(1.2, 2.2))

        return attached

    async def fill(
        self,
        scope: Locator,
        resume_path: Optional[Path] = None,
        job_location: str = "",
        upload_resume: bool = True,
        cover_letter_path: Optional[Path] = None,
    ) -> List[str]:
        """
        Fills everything answerable on `scope`.

        Returns the labels of the questions that were NOT answered. The caller
        must not submit while that list contains a required question -- see the
        module docstring.
        """
        if upload_resume:
            await self.upload_resume(scope, resume_path, cover_letter_path)

        fields = await self.collect_fields(scope)
        if not fields:
            return []

        # One request for the whole form. Rules and cache answer most of it for
        # free; the rest goes up together, off the event loop so the idle-drift
        # gaze motion can actually run during the wait rather than being starved
        # by a blocking HTTP call.
        async with OrganicIdleDrift(self.page):
            results = await asyncio.to_thread(
                self.solver.solve_questions,
                [f.question for f in fields],
                self.profile,
                job_location,
                self.config.speed.batch_form_questions,
            )

        return await self.apply_answers(fields, results)

    async def apply_answers(self, fields: Sequence[FormField], results: Sequence[Any]) -> List[str]:
        """Types the solver's answers in. Returns the labels it could not fill."""
        unanswered: List[str] = []
        pace = self.config.speed.action_pace

        for field_obj, result in zip(fields, results):
            label = field_obj.question.text
            answer = str(getattr(result, "answer", "") or "").strip()

            if getattr(result, "needs_human", False) or not answer:
                logger.warning(
                    f"[yellow]Declined to answer '{label[:60]}': "
                    f"{getattr(result, 'reasoning', '')}[/yellow]"
                )
                unanswered.append(self._mark(field_obj, label))
                continue

            try:
                if field_obj.kind == "text":
                    numeric = field_obj.question.input_type == "number"
                    if not await self.type_value(field_obj.handle, answer, numeric=numeric):
                        logger.warning(
                            f"[yellow]'{label[:50]}' would not hold the value "
                            f"'{answer}'; leaving it for a human.[/yellow]"
                        )
                        unanswered.append(self._mark(field_obj, label))
                        continue
                    logger.info(f"Answered: [dim]'{label[:50]}'[/dim] -> [bold cyan]{answer}[/bold cyan]")
                    await paced_delay(0.5, 1.1, pace=pace)

                elif field_obj.kind == "select":
                    # Keyboard-driven: emits trusted keydown/keyup and a real change
                    # event, unlike select_option() which sets .value from the driver.
                    if await organic_select_option(self.page, field_obj.handle, answer):
                        logger.info(f"Selected: [dim]'{label[:50]}'[/dim] -> [bold cyan]{answer}[/bold cyan]")
                    else:
                        logger.warning(f"[yellow]Could not select '{answer}' for '{label[:50]}'.[/yellow]")
                        unanswered.append(self._mark(field_obj, label))
                    await paced_delay(0.4, 0.9, pace=pace)

                elif field_obj.kind == "radio":
                    target = next((h for text, h in field_obj.options if text == answer), None)
                    if target is None:
                        logger.warning(
                            f"[yellow]No option matched '{answer}' for '{label[:50]}'.[/yellow]"
                        )
                        unanswered.append(self._mark(field_obj, label))
                        continue
                    box = await target.bounding_box()
                    if box:
                        await organic_mouse_move(self.page, box["x"] + 15, box["y"] + 10)
                        await asyncio.sleep(random.uniform(0.12, 0.28))
                    await organic_click(self.page, target)
                    logger.info(f"Selected: [dim]'{label[:50]}'[/dim] -> [bold cyan]{answer}[/bold cyan]")
                    await paced_delay(0.4, 0.9, pace=pace)

                elif field_obj.kind in ("checkbox", "aria-checkbox"):
                    if answer.lower() in ("yes", "true", "1", "agree", "i agree", "accept"):
                        await organic_click(self.page, field_obj.handle)
                        logger.info(f"Checked: [dim]'{label[:50]}'[/dim]")
                        await paced_delay(0.3, 0.8, pace=pace)
                    elif field_obj.required:
                        # A required box the solver said "no" to cannot be
                        # satisfied by leaving it clear; that is a human decision.
                        unanswered.append(self._mark(field_obj, label))

                elif field_obj.kind == "aria-radio":
                    target = next((h for text, h in field_obj.options if text == answer), None)
                    if target is None:
                        logger.warning(
                            f"[yellow]No option matched '{answer}' for '{label[:50]}'.[/yellow]"
                        )
                        unanswered.append(self._mark(field_obj, label))
                        continue
                    await organic_click(self.page, target)
                    # An ARIA radio carries no state of its own, so the only
                    # evidence it took the click is aria-checked flipping.
                    if not await self._aria_checked(target):
                        logger.warning(
                            f"[yellow]'{label[:50]}' did not register '{answer}'.[/yellow]"
                        )
                        unanswered.append(self._mark(field_obj, label))
                        continue
                    logger.info(f"Selected: [dim]'{label[:50]}'[/dim] -> [bold cyan]{answer}[/bold cyan]")
                    await paced_delay(0.4, 0.9, pace=pace)

                elif field_obj.kind in ("combobox", "typeahead"):
                    if await self._choose_from_popup(field_obj.handle, answer):
                        logger.info(f"Selected: [dim]'{label[:50]}'[/dim] -> [bold cyan]{answer}[/bold cyan]")
                        await paced_delay(0.4, 0.9, pace=pace)
                    else:
                        logger.warning(
                            f"[yellow]Could not commit '{answer}' in the '{label[:50]}' "
                            f"picker; leaving it for a human.[/yellow]"
                        )
                        unanswered.append(self._mark(field_obj, label))

                elif field_obj.kind == "editable":
                    await organic_type(self.page, field_obj.handle, answer, enable_typos=True)
                    try:
                        await field_obj.handle.evaluate("el => el.blur()")
                    except Exception:
                        pass
                    try:
                        written = (await field_obj.handle.inner_text()).strip()
                    except Exception:
                        written = ""
                    if not written:
                        unanswered.append(self._mark(field_obj, label))
                        continue
                    logger.info(f"Wrote: [dim]'{label[:50]}'[/dim] ({len(written)} chars)")
                    await paced_delay(0.5, 1.1, pace=pace)

                elif field_obj.kind == "date":
                    if not await self._fill_date(field_obj.handle, answer):
                        logger.warning(
                            f"[yellow]'{label[:50]}' would not accept the date "
                            f"'{answer}'.[/yellow]"
                        )
                        unanswered.append(self._mark(field_obj, label))
                        continue
                    logger.info(f"Answered: [dim]'{label[:50]}'[/dim] -> [bold cyan]{answer}[/bold cyan]")
                    await paced_delay(0.4, 0.9, pace=pace)

            except Exception as exc:
                logger.warning(f"Could not fill '{label[:50]}': {exc}")
                unanswered.append(self._mark(field_obj, label))

        return unanswered

    @staticmethod
    async def _aria_checked(elem: Locator) -> bool:
        try:
            return (await elem.get_attribute("aria-checked") or "").lower() == "true"
        except Exception:
            return False

    async def _choose_from_popup(self, control: Locator, answer: str) -> bool:
        """
        Picks `answer` out of a combobox or typeahead, and proves it stuck.

        The sequence is the one a person performs -- click the control, type
        enough to filter, click the matching option -- and it has to be, because
        these widgets ignore anything else. Setting the underlying hidden
        input's value does not update the React state that the form actually
        submits, and typing without picking leaves a field that clears itself on
        blur. Both of those fail *silently*, which is why this verifies rather
        than assuming.
        """
        # Open it only if it is not already open. These controls toggle, so a
        # blind click on an open popup closes it -- which is the state
        # `_peek_options` can leave behind on a widget that ignores Escape.
        try:
            if not await self._popup_is_open(control):
                await organic_click(self.page, control, post_delay_range=(0.2, 0.45))
        except Exception:
            return False

        # Type into whatever the widget focused. react-select moves focus to an
        # inner input on click, so the visible control is not necessarily where
        # the keystrokes belong -- the keyboard follows focus, which is correct
        # in both layouts.
        try:
            await self.page.keyboard.type(answer[:40], delay=random.uniform(55, 115))
        except Exception:
            pass
        await asyncio.sleep(random.uniform(0.45, 0.85))

        options = await self._read_open_options(control)
        wanted = answer.strip().lower()
        match = next((o for o in options if o.strip().lower() == wanted), None)
        if match is None:
            match = next((o for o in options if wanted in o.strip().lower()), None)
        if match is None and options:
            # A typeahead that filtered down to a single suggestion has already
            # answered the question, even if its wording differs from ours
            # ("Bengaluru" -> "Bengaluru, Karnataka, India").
            match = options[0] if len(options) == 1 else None
        if match is None:
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

        # The option is resolved from the control's own document, never through
        # `page.locator`. A Page locator searches the main frame only -- measured
        # on tests/fixtures/embedded_board.html, it finds 0 matches for a control
        # that frame.locator finds 1 of -- so on any embedded board, which is the
        # whole reason the frame-walking scout exists, every dropdown answer would
        # have failed to commit and been reported as a refusal.
        option = None
        try:
            handle = await control.evaluate_handle(
                """(el, wanted) => {
                    const doc = el.ownerDocument;
                    const nodes = Array.from(doc.querySelectorAll(
                        "[role='option'],[class*='select__option'],[role='listbox'] li"));
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const target = norm(wanted);
                    return nodes.find(n => norm(n.innerText) === target)
                        || nodes.find(n => norm(n.innerText).includes(target))
                        || null;
                }""",
                match,
            )
            option = handle.as_element()
        except Exception:
            option = None

        try:
            if option is None:
                raise RuntimeError("no option element resolved")
            await organic_click(self.page, option, post_delay_range=(0.25, 0.5))
        except Exception:
            # Keyboard commit, which every one of these widgets supports and
            # which is what a keyboard user does anyway.
            try:
                await self.page.keyboard.press("Enter")
            except Exception:
                return False

        await asyncio.sleep(random.uniform(0.3, 0.6))
        return await self._popup_value_matches(control, match)

    async def _popup_value_matches(self, control: Locator, expected: str) -> bool:
        """Reads back what a combobox is now showing, whatever it is made of."""
        try:
            shown = await control.evaluate(
                """el => {
                    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') return el.value || '';
                    const hidden = el.querySelector('input[type=hidden]');
                    if (hidden && hidden.value) return hidden.value;
                    const inner = el.querySelector('input');
                    if (inner && inner.value) return inner.value;
                    return (el.innerText || '').trim();
                }"""
            )
        except Exception:
            return False

        shown = str(shown or "").strip().lower()
        if not shown:
            return False
        want = expected.strip().lower()
        return want in shown or shown in want

    async def _fill_date(self, elem: Locator, answer: str) -> bool:
        """
        Enters an ISO date into a native date input.

        Typed rather than set, because a date input's segments respond to
        keystrokes and a real user produces them. The order of the segments is
        the browser's locale, not the value's, so the digits are typed in the
        order the control displays them and then the result is read back --
        which is the only way to know a locale mismatch did not silently swap
        the day and the month.
        """
        want = answer.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", want):
            return False

        year, month, day = want.split("-")
        try:
            await organic_click(self.page, elem, post_delay_range=(0.15, 0.35))
            order = await elem.evaluate(
                """() => {
                    const parts = new Intl.DateTimeFormat().formatToParts(new Date());
                    return parts.filter(p => ['day','month','year'].includes(p.type))
                                .map(p => p.type).join('');
                }"""
            )
        except Exception:
            order = "monthdayyear"

        segments = {"day": day, "month": month, "year": year}
        sequence = [segments[k] for k in re.findall(r"day|month|year", order or "monthdayyear")]
        try:
            for chunk in sequence:
                for ch in chunk:
                    await self.page.keyboard.press(ch, delay=random.uniform(55, 110))
                await asyncio.sleep(random.uniform(0.06, 0.14))
            await elem.evaluate("el => el.blur()")
            return (await elem.input_value()).strip() == want
        except Exception:
            try:
                await elem.fill(want)
                return (await elem.input_value()).strip() == want
            except Exception:
                return False

    @staticmethod
    def _mark(field_obj: FormField, label: str) -> str:
        """Tags a refusal so the caller can tell a blocking one from an optional one."""
        text = label.strip()[:80]
        return f"{text} [required]" if field_obj.required else text

    @staticmethod
    def blocking(unanswered: Sequence[str]) -> List[str]:
        """
        The refusals that must stop a submission.

        An optional question left blank is normal on an ATS form -- EEO
        demographics, "how did you hear about us" -- and abandoning an otherwise
        complete application over one would cost more roles than it saves.
        A required one is a hard stop.
        """
        return [u for u in unanswered if u.endswith("[required]")]

    # -- reading back --------------------------------------------------------

    @staticmethod
    async def collect_validation_errors(scope: Locator) -> List[str]:
        """Whatever inline validation the form is currently showing."""
        try:
            err = scope.locator(
                ".artdeco-inline-feedback--error, "
                "[data-test-form-element-error-messages], "
                "[role='alert'], .error, .field-error, .invalid-feedback, "
                "[aria-invalid='true'] ~ .error"
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
