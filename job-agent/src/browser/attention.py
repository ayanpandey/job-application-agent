"""
Window attention: genuine focus/blur telemetry and away-from-keyboard periods.

A session in which the browser window holds focus continuously for two hours is not a
session a person produces. People alt-tab to mail, to Slack, to the job description they
opened in another tab; LinkedIn's client telemetry listens for window `blur`/`focus`, so
an unbroken focused stretch is a cheap, session-level anomaly that no amount of pointer
realism compensates for.

The events emitted here are real, not dispatched. `Emulation.setFocusEmulationEnabled`
is the CDP switch Playwright itself flips to keep a driven page believing it is focused;
turning it off makes Chrome fire a genuine `blur` (isTrusted: true) and flips
`document.hasFocus()`, and turning it back on fires a genuine `focus`. Measured
round-trip on this build: blur/focus pairs, all isTrusted true.

What is deliberately NOT faked: `document.visibilityState`. There is no CDP command on
this build that produces a trusted `hidden` state (`Emulation.setPageVisibilityOverride`
does not exist, and `Page.setWebLifecycleState` only accepts frozen/active and leaves
visibility untouched). Dispatching a synthetic `visibilitychange` would set
isTrusted: false on the one event a detector is most likely to be checking, which is
strictly worse than emitting nothing. So the page stays honestly "visible" and only
focus changes.

Headed only. Measured on this build: with headless=False the toggle flips
document.hasFocus() and fires trusted blur/focus; with headless=True it is accepted by
CDP and does nothing at all -- hasFocus stays true and no event is emitted. The break
still happens either way, it just carries no focus telemetry, and _probe_focus_support
says so once rather than letting a headless run quietly lose the signal.
"""

import asyncio
import contextlib
import random
import weakref
from typing import Any, Optional

from playwright.async_api import Page

from src.core.logger import logger


_CDP_CACHE: "weakref.WeakKeyDictionary[Page, Any]" = weakref.WeakKeyDictionary()


async def _cdp_session(page: Page) -> Optional[Any]:
    """Returns a cached CDP session for the page, or None if one cannot be attached."""
    try:
        cached = _CDP_CACHE.get(page)
    except TypeError:
        cached = None
    if cached is not None:
        return cached

    try:
        session = await page.context.new_cdp_session(page)
    except Exception as e:
        logger.debug(f"CDP attach unavailable for attention control: {e}")
        return None

    with contextlib.suppress(TypeError):
        _CDP_CACHE[page] = session
    return session


async def set_window_focused(page: Page, focused: bool) -> bool:
    """
    Turns the window's focused state on or off, producing a genuine focus/blur event.

    Returns False if CDP is unreachable, so callers can still honour the wait rather
    than skipping it entirely.
    """
    session = await _cdp_session(page)
    if session is None:
        return False
    try:
        await session.send("Emulation.setFocusEmulationEnabled", {"enabled": focused})
        return True
    except Exception as e:
        # A stale session (page navigated to a new target) -- drop it and let the next
        # call re-attach rather than leaving the window stuck unfocused.
        logger.debug(f"Focus emulation toggle failed: {e}")
        with contextlib.suppress(Exception, TypeError):
            del _CDP_CACHE[page]
        return False


async def _sleep_watching_page(page: Page, seconds: float):
    """Sleeps in short chunks, giving up early if the page goes away mid-wait."""
    remaining = float(seconds)
    while remaining > 0:
        chunk = min(4.0, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk
        try:
            if page.is_closed():
                return
        except Exception:
            return


_FOCUS_SUPPORTED: Optional[bool] = None


async def _probe_focus_support(page: Page) -> bool:
    """
    Checks once whether unfocusing actually takes effect on this browser.

    CDP accepts the command in headless mode and then ignores it, so a return value of
    True from set_window_focused proves only that the message was delivered. Reading
    document.hasFocus() back is the only way to know the event really fired; without
    this check a headless run would silently emit no focus telemetry while every log
    line claimed otherwise.
    """
    global _FOCUS_SUPPORTED
    if _FOCUS_SUPPORTED is not None:
        return _FOCUS_SUPPORTED

    if not await set_window_focused(page, False):
        _FOCUS_SUPPORTED = False
        return False

    try:
        await asyncio.sleep(0.25)
        _FOCUS_SUPPORTED = not await page.evaluate("() => document.hasFocus()")
    except Exception:
        _FOCUS_SUPPORTED = False
    finally:
        await set_window_focused(page, True)

    if not _FOCUS_SUPPORTED:
        logger.warning(
            "[yellow]Window focus emulation has no effect on this browser (this is what "
            "headless does). Breaks will still be taken, but the session will emit no "
            "blur/focus telemetry -- run headed to get it.[/yellow]"
        )
    return _FOCUS_SUPPORTED


async def simulate_away(page: Page, seconds: float) -> None:
    """
    Spends `seconds` away from the browser with the window genuinely unfocused.

    This is what a break should look like. Sitting on the page for twenty minutes with
    the cursor twitching is a worse trace than no break at all: it says the user never
    once looked at anything else, which is exactly what an unattended script does.
    """
    supported = await _probe_focus_support(page)
    if supported:
        await set_window_focused(page, False)
    try:
        await _sleep_watching_page(page, seconds)
    finally:
        if supported:
            await set_window_focused(page, True)
            # Nobody resumes clicking the instant they return to a window.
            await asyncio.sleep(random.uniform(0.8, 2.4))


async def maybe_glance_away(
    page: Page,
    probability: float = 0.22,
    seconds_range: tuple = (12.0, 95.0),
) -> bool:
    """
    Occasionally looks at another window for a few seconds, the way people actually work.

    Called at safe points (between jobs, not mid-form) so a blur never lands in the
    middle of a keystroke sequence, where it would truncate typing telemetry in a way
    no real user produces.
    """
    if random.random() >= probability:
        return False
    await simulate_away(page, random.uniform(*seconds_range))
    return True
