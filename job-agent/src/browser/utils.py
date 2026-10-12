"""
Human-like interaction utilities for Playwright browser automation.
Re-exports advanced human physics simulation, natural typing, and pacing.
"""

import asyncio
import random
from typing import Optional
from playwright.async_api import Page, Locator

from src.core.logger import logger
from src.browser.human_behavior import (
    organic_mouse_move,
    organic_click,
    organic_type,
    organic_scroll,
    organic_scroll_over,
    organic_select_option,
    organic_bring_into_view,
    smooth_scroll,
    OrganicIdleDrift,
    simulate_human_reading,
    perform_warm_up_browsing,
    explore_random_company_page,
    take_natural_human_break,
    natural_mouse_move,
    natural_click,
    natural_type,
    human_navigate,
    human_go_back,
    MouseState,
)
from src.browser.attention import simulate_away, maybe_glance_away, set_window_focused
from src.browser.session_rhythm import SessionRhythm, RhythmConfig, human_gap


async def human_delay(min_sec: float = 2.0, max_sec: float = 5.0):
    """
    Wait for a log-normally distributed duration to simulate human reaction/reading time.

    A Gaussian is symmetric about its mean, so it produces as many unusually fast gaps as
    slow ones. Human inter-action gaps are right-skewed: a person cannot beat their own
    reaction time, but can always be distracted. Over a few hundred actions that
    asymmetry is what a gap histogram actually shows.
    """
    await human_gap(min_sec, max_sec)


# Nothing below this survives being scaled away. A pause shorter than roughly a
# third of a second between two deliberate UI actions is not a fast human, it is
# a script, and the gap histogram says so immediately.
MIN_HUMAN_GAP_SECONDS = 0.35


async def paced_delay(min_sec: float, max_sec: float, pace: float = 1.0):
    """
    `human_delay` with a session-wide scale factor applied.

    Used for the discretionary waits -- between form steps, after filling a field
    -- where the original numbers were chosen to look unhurried rather than to
    defeat any specific detection. The distribution shape and the floor are kept:
    scaling shifts where the mass sits, it does not turn the pause into a
    constant, and it cannot drive a gap below MIN_HUMAN_GAP_SECONDS.
    """
    scale = max(0.15, min(2.0, pace))
    lo = max(MIN_HUMAN_GAP_SECONDS, min_sec * scale)
    hi = max(lo + 0.2, max_sec * scale)
    await human_gap(lo, hi)


async def human_type(page: Page, selector: str, text: str, enable_typos: bool = True):
    """Types text character by character with physical QWERTY dynamics and keyboard clearing."""
    try:
        elem = page.locator(selector).first
        await organic_type(page, elem, text, enable_typos=enable_typos, clear_field_first=True)
    except Exception as e:
        logger.debug(f"human_type fallback for {selector}: {e}")
        try:
            # Last resort only: organic_type already falls back internally, so this runs
            # only if the locator itself is broken. Submitting the form beats leaving a
            # required field blank, at the cost of one keystroke-free field.
            await page.fill(selector, text)
        except Exception:
            pass


async def safe_click(locator: Locator, timeout_ms: int = 8000) -> bool:
    """Clicks an element using physics-based WindMouse movement and organic dwell time."""
    try:
        page = locator.page
        return await organic_click(page, locator, timeout_ms=timeout_ms)
    except Exception as e:
        logger.debug(f"safe_click fallback: {e}")
        try:
            await locator.click(timeout=timeout_ms)
            return True
        except Exception:
            return False
