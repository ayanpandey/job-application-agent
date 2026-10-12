"""
Naukri Daily Profile Booster:
Automatically refreshes your Naukri resume headline / summary daily.
This triggers Naukri's algorithm to mark your profile as 'Updated Today',
keeping you at the very top of recruiter search results (Resdex ranking).
"""

import asyncio
from playwright.async_api import Page

from src.core.config import CandidateProfile
from src.core.logger import logger, console
from src.browser.utils import human_delay, safe_click
from src.browser.human_behavior import organic_type, human_navigate


class NaukriProfileBooster:
    def __init__(self, page: Page, profile: CandidateProfile):
        self.page = page
        self.profile = profile

    async def boost_profile(self) -> bool:
        """Navigates to Naukri profile and re-saves the resume headline to update timestamp."""
        console.print("\n[bold magenta]══ Naukri Daily Profile Booster ══[/bold magenta]")
        logger.info("Navigating to Naukri Profile page to refresh recruiter ranking...")

        if not await human_navigate(self.page, "https://www.naukri.com/mnjuser/profile", settle=(3.0, 5.0)):
            logger.error("Failed to navigate to Naukri profile.")
            return False

        try:
            # Locate Resume Headline edit button
            headline_edit_btn = self.page.locator("span.edit.icon:has-text('edit'), .resumeHeadline .widgetHead .edit").first
            if await headline_edit_btn.count() == 0:
                # Try generic edit icon near headline
                headline_edit_btn = self.page.locator(".resumeHeadline .edit").first

            if await headline_edit_btn.count() > 0:
                await safe_click(headline_edit_btn)
                await human_delay(1.0, 2.0)

                # Find headline textarea
                headline_textarea = self.page.locator("textarea#resumeHeadlineTxt, .resumeHeadlineTxt textarea").first
                if await headline_textarea.count() > 0:
                    current_text = await headline_textarea.input_value()
                    # Micro-update (toggle a trailing period or whitespace) to trigger dirty state
                    if current_text.endswith("."):
                        new_text = current_text[:-1]
                    else:
                        new_text = current_text + "."

                    # Real keystrokes: Naukri scores profile edits for automation, and a
                    # textarea that changes value with no keydown/keyup trace is the tell.
                    await organic_type(self.page, headline_textarea, new_text, enable_typos=False)
                    await human_delay(0.8, 1.5)

                    # Click Save
                    save_btn = self.page.locator("button.btn-dark-ot:has-text('Save'), button:has-text('Save')").first
                    if await save_btn.count() > 0:
                        await safe_click(save_btn)
                        await human_delay(2.0, 3.5)
                        console.print("[bold green]✓ Naukri profile timestamp updated! Profile is now ranked at the top of recruiter searches for today.[/bold green]\n")
                        return True

            console.print("[yellow]Could not locate resume headline edit widget on page.[/yellow]")
            return False

        except Exception as e:
            logger.error(f"Error boosting Naukri profile: {e}")
            return False
