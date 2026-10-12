"""
Find a control when its hard-coded selector has stopped matching.

Every applying failure in this product so far has been a selector problem, not
a logic problem: `.first` binding to a hidden duplicate of a button, one class
serving two different controls, a container selector that swallowed half a page
of metadata. LinkedIn changes its markup and the agent goes quiet -- it does not
crash, it reports "button not found" and moves on, and that reads like a posting
with no button rather than a broken selector.

So the selectors stay, and this is what happens when they miss. The model is
shown the pruned DOM and asked where the control actually is.

Deliberately a fallback and not the primary path:

  * A selector that works costs nothing. This costs a model call, a round trip,
    and a slice of the licence's daily quota, which is also paying for job
    matching, resume tailoring and screening answers.
  * A hard-coded selector fails predictably and identically every time, which is
    debuggable. A model can fail differently on each attempt, which is not.

What it buys is that the agent heals itself between releases instead of going
quiet until someone reads the logs.

Results are cached for the life of the session, keyed by the description and the
page's own URL shape, so one resolution serves every subsequent posting rather
than re-asking per job.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from playwright.async_api import Locator, Page

from src.core.logger import logger

# The proxy refuses a prompt over 24k characters and the template needs room, so
# the fragment is capped well under it. A pruned apply region is comfortably
# smaller than this; a whole LinkedIn page is not, which is the point of pruning.
MAX_FRAGMENT_CHARS = 14000

# Attributes worth keeping. Everything else is styling noise that costs prompt
# budget and tells the model nothing durable -- hashed class names change on
# every deploy and are actively misleading as anchors.
KEEP_ATTRS = (
    "id", "name", "type", "role", "value", "placeholder", "href",
    "aria-label", "aria-labelledby", "data-testid", "data-test", "data-control-name",
)

_PRUNE_JS = """
(args) => {
    const { rootSelector, keep, maxChars } = args;
    const root = rootSelector ? document.querySelector(rootSelector) : document.body;
    if (!root) return "";

    const INTERESTING = new Set(["BUTTON","A","INPUT","SELECT","TEXTAREA","LABEL","FORM","DIALOG"]);

    function describe(el) {
        const attrs = [];
        for (const a of keep) {
            const v = el.getAttribute(a);
            if (v) attrs.push(`${a}="${String(v).slice(0, 80)}"`);
        }
        // Class names are kept but truncated: the stable ones are readable words
        // and the generated ones are hashes, and the model is told which to trust.
        const cls = (el.getAttribute("class") || "").split(/\\s+/).slice(0, 4).join(" ");
        if (cls) attrs.push(`class="${cls.slice(0, 90)}"`);
        const text = (el.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 60);
        const vis = el.offsetParent !== null || el.getClientRects().length > 0;
        return `<${el.tagName.toLowerCase()} ${attrs.join(" ")}${vis ? "" : " hidden"}>${text}</${el.tagName.toLowerCase()}>`;
    }

    const out = [];
    for (const el of root.querySelectorAll("button, a, input, select, textarea, label, form, dialog, [role=dialog], [role=button]")) {
        out.push(describe(el));
        if (out.join("\\n").length > maxChars) break;
    }
    return out.join("\\n").slice(0, maxChars);
}
"""


class DomResolver:
    """Model-assisted element lookup, cached per session."""

    def __init__(self, llm: Any):
        self.llm = llm
        self._cache: Dict[str, List[str]] = {}
        self.calls = 0
        self.hits = 0

    @staticmethod
    def _page_key(page: Page) -> str:
        """URL shape, not the URL. Every job view should share one cache entry."""
        url = (page.url or "").split("?")[0]
        return re.sub(r"/\d{6,}/?", "/<id>/", url)

    async def prune_dom(self, page: Page, root_selector: Optional[str] = None) -> str:
        """The page's interactive controls, as a compact listing."""
        try:
            return await page.evaluate(
                _PRUNE_JS,
                {"rootSelector": root_selector, "keep": list(KEEP_ATTRS),
                 "maxChars": MAX_FRAGMENT_CHARS},
            )
        except Exception as exc:
            logger.debug(f"DOM prune failed: {exc}")
            return ""

    async def resolve(
        self,
        page: Page,
        description: str,
        root_selector: Optional[str] = None,
        page_context: str = "",
    ) -> Optional[Locator]:
        """
        A visible element matching `description`, or None.

        Never raises: this runs on a path that has already failed once, and an
        exception here would turn a recoverable miss into a dead posting.
        """
        cache_key = f"{self._page_key(page)}::{description}"

        # Try what worked last time before spending a call.
        for selector in self._cache.get(cache_key, []):
            found = await self._first_visible(page, selector)
            if found is not None:
                self.hits += 1
                logger.debug(f"Resolver cache hit for '{description}': {selector}")
                return found

        fragment = await self.prune_dom(page, root_selector)
        if not fragment:
            return None

        try:
            self.calls += 1
            data = self.llm.run_task_json("element_resolver", {
                "element_description": description,
                "page_context": page_context or "LinkedIn job posting",
                "dom_fragment": fragment,
            })
        except Exception as exc:
            logger.debug(f"Element resolver unavailable: {exc}")
            return None

        selectors = (data or {}).get("selectors") or []
        if not selectors:
            logger.info(f"Model could not locate '{description}' on this page.")
            return None

        for selector in selectors[:4]:
            if not isinstance(selector, str) or not selector.strip():
                continue
            found = await self._first_visible(page, selector.strip())
            if found is not None:
                self._cache.setdefault(cache_key, []).insert(0, selector.strip())
                logger.info(
                    f"Selector for '{description}' had stopped matching; "
                    f"the model found it at `{selector.strip()}` "
                    f"({(data or {}).get('reasoning', '')[:90]})"
                )
                return found

        logger.info(
            f"Model proposed {len(selectors)} selector(s) for '{description}', "
            f"none of which resolved to a visible element."
        )
        return None

    @staticmethod
    async def _first_visible(page: Page, selector: str) -> Optional[Locator]:
        """The first visible match, or None. A model selector may be invalid CSS."""
        try:
            candidates = page.locator(selector)
            for i in range(min(await candidates.count(), 5)):
                candidate = candidates.nth(i)
                if await candidate.is_visible():
                    return candidate
        except Exception:
            return None
        return None

    def report(self) -> str:
        if not self.calls and not self.hits:
            return ""
        return (
            f"selector healing: {self.calls} model call(s), "
            f"{self.hits} served from cache"
        )
