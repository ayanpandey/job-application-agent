"""
Physics-Based Organic Human Behavior & Anti-Detection Engine for Playwright.

Implements true bio-mechanical interaction models:
1. Stateful Continuous Mouse Tracking: Zero coordinate teleportation across actions.
2. WindMouse & Fitts's Law Trajectory Kinematics: Organic acceleration, wind drift,
   target overshooting, and low-frequency neuromuscular tremor.
3. Inertial Momentum Scrolling: Multi-pulse exponential decay wheel dynamics mimicking
   real Apple trackpads and weighted mouse wheels.
4. Cognitive Keystroke Dynamics: QWERTY distance modeling, variable dwell/flight times,
   realistic typo detection/backspacing, and keyboard-based input clearing.
5. Background Gaze & Idle Drift: Subtle non-blocking cursor fidgeting during LLM/API delays.
6. Non-Linear Exploration: Stochastic pacing and reading behavior.
"""

import asyncio
import contextlib
import math
import random
import sys
import time
import weakref
from typing import Dict, List, Optional, Tuple, Union
from playwright.async_api import Locator, Page

from src.core.logger import logger, console
from src.browser.attention import simulate_away


# ==============================================================================
# 1. Continuous Mouse State Tracker
# ==============================================================================

_VIEWPORT_CACHE: "weakref.WeakKeyDictionary[Page, Dict[str, int]]" = weakref.WeakKeyDictionary()
_VIEWPORT_FALLBACK: Dict[int, Dict[str, int]] = {}


def _viewport_of(page: Page) -> Dict[str, int]:
    """
    Returns the live viewport size.

    The context runs with no_viewport so the viewport tracks the real OS window; in that
    mode page.viewport_size is None, so we rely on the size measured from the page itself
    (see _sync_viewport). Guessing a fixed size here would clamp the cursor to a box
    smaller than the window and leave the pointer repeatedly pinned to an invisible edge.
    """
    try:
        cached = _VIEWPORT_CACHE.get(page)
    except TypeError:
        cached = _VIEWPORT_FALLBACK.get(id(page))
    if cached:
        return cached

    try:
        vp = page.viewport_size
    except Exception:
        vp = None
    if vp and vp.get("width") and vp.get("height"):
        return vp
    return {"width": 1440, "height": 900}


async def _sync_viewport(page: Page) -> Dict[str, int]:
    """Measures the real window inner size from the page and caches it."""
    try:
        size = await page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
        if size and size.get("width") and size.get("height"):
            try:
                _VIEWPORT_CACHE[page] = size
            except TypeError:
                _VIEWPORT_FALLBACK[id(page)] = size
            return size
    except Exception:
        pass
    return _viewport_of(page)


class MouseState:
    """
    Tracks continuous mouse cursor coordinates across the browser session.

    Keyed by a WeakKeyDictionary rather than id(page): CPython recycles id() values
    after a page is garbage collected, so an id-keyed dict can hand a brand-new page
    the stale coordinates of a dead one -- which produces exactly the teleport this
    class exists to prevent.
    """
    _positions: "weakref.WeakKeyDictionary[Page, Tuple[float, float]]" = weakref.WeakKeyDictionary()
    _fallback: Dict[int, Tuple[float, float]] = {}

    @classmethod
    def _read(cls, page: Page) -> Optional[Tuple[float, float]]:
        try:
            return cls._positions.get(page)
        except TypeError:
            return cls._fallback.get(id(page))

    @classmethod
    def _write(cls, page: Page, pos: Tuple[float, float]):
        try:
            cls._positions[page] = pos
        except TypeError:
            cls._fallback[id(page)] = pos

    @classmethod
    def get_position(cls, page: Page) -> Tuple[float, float]:
        existing = cls._read(page)
        if existing is not None:
            return existing

        # Playwright's virtual mouse genuinely starts at (0, 0). Seeding this tracker
        # with an invented "plausible" starting point does not move the real cursor --
        # it only makes the first trajectory begin somewhere the pointer has never been,
        # so the page's very first mousemove is a jump from the corner to that invented
        # point. Starting from the true origin lets the first movement interpolate
        # continuously, exactly like a cursor entering from the top-left edge.
        initial = (0.0, 0.0)
        cls._write(page, initial)
        return initial

    @classmethod
    def set_position(cls, page: Page, x: float, y: float):
        cls._write(page, (float(x), float(y)))

    @classmethod
    def clamp(cls, page: Page, x: float, y: float) -> Tuple[float, float]:
        """Keeps a coordinate inside the viewport so the cursor never pins to an edge."""
        viewport = _viewport_of(page)
        cx = max(2.0, min(float(viewport["width"]) - 2.0, x))
        cy = max(2.0, min(float(viewport["height"]) - 2.0, y))
        return cx, cy


# ==============================================================================
# 2. WindMouse Physics & Fitts's Law Mouse Movement
# ==============================================================================

def _generate_windmouse_trajectory(
    start: Tuple[float, float],
    end: Tuple[float, float],
    target_width: float = 40.0,
    target_height: float = 20.0,
    g_0: float = 9.0,
    w_0: float = 3.0,
    m_0: float = 15.0,
    d_0: float = 12.0,
) -> List[Tuple[float, float, float]]:
    """
    Generates a bio-mechanical mouse trajectory using the WindMouse algorithm
    combined with Fitts's law and low-frequency pink noise.
    
    Returns list of (x, y, sleep_seconds) tuples.
    """
    start_x, start_y = start
    dest_x, dest_y = end
    
    dist = math.hypot(dest_x - start_x, dest_y - start_y)
    if dist < 1.0:
        return [(dest_x, dest_y, 0.01)]

    # Dynamic step sizing derived from Fitts's Law
    effective_width = max(10.0, min(target_width, target_height))
    fitts_difficulty = math.log2(1.0 + (dist / effective_width))
    expected_duration = 0.12 + 0.08 * fitts_difficulty + random.uniform(0.02, 0.08)

    # 40% chance of a slight overshoot when distance is large (> 220px)
    should_overshoot = dist > 220.0 and random.random() < 0.45
    if should_overshoot:
        overshoot_mag = random.uniform(0.03, 0.07) * dist
        angle = math.atan2(dest_y - start_y, dest_x - start_x)
        mid_dest_x = dest_x + overshoot_mag * math.cos(angle + random.uniform(-0.2, 0.2))
        mid_dest_y = dest_y + overshoot_mag * math.sin(angle + random.uniform(-0.2, 0.2))
    else:
        mid_dest_x, mid_dest_y = dest_x, dest_y

    current_x, current_y = start_x, start_y
    v_x, v_y = 0.0, 0.0
    w_x, w_y = 0.0, 0.0
    
    points: List[Tuple[float, float, float]] = []
    
    # Pink noise filter memory for hand tremor
    tremor_x, tremor_y = 0.0, 0.0

    target_threshold = max(2.0, target_width * 0.15)
    step_count = 0
    max_steps = 180

    while step_count < max_steps:
        step_count += 1
        d_to_target = math.hypot(mid_dest_x - current_x, mid_dest_y - current_y)
        
        if d_to_target < target_threshold:
            if should_overshoot:
                # Snap back from overshoot to real target
                mid_dest_x, mid_dest_y = dest_x, dest_y
                should_overshoot = False
                continue
            break

        # Wind & Gravity calculation
        w_x = (w_x * 0.75) + (random.uniform(-1.0, 1.0) * w_0)
        w_y = (w_y * 0.75) + (random.uniform(-1.0, 1.0) * w_0)
        
        v_x += (w_x + g_0 * (mid_dest_x - current_x) / (d_to_target + 0.1))
        v_y += (w_y + g_0 * (mid_dest_y - current_y) / (d_to_target + 0.1))

        # Velocity damping / friction
        current_v = math.hypot(v_x, v_y)
        if current_v > m_0:
            v_clip = m_0 * (0.8 + 0.2 * random.random())
            v_x = (v_x / current_v) * v_clip
            v_y = (v_y / current_v) * v_clip

        current_x += v_x
        current_y += v_y

        # Low-frequency neuromuscular tremor (1/f noise)
        tremor_x = 0.7 * tremor_x + random.gauss(0, 0.35)
        tremor_y = 0.7 * tremor_y + random.gauss(0, 0.35)

        # Micro sleep time per tick
        step_sleep = max(0.005, min(0.018, expected_duration / max(15, dist / 8.0)))
        points.append((current_x + tremor_x, current_y + tremor_y, step_sleep))

    # Final approach: corrective sub-movement.
    #
    # The loop above stops once it is within target_threshold of the destination, and
    # that threshold scales with the target's width -- on a 600px-wide element it is
    # 90px. Appending the exact destination straight after would emit a single 90px
    # hop as the very last sample before the click, which is both the largest jump in
    # the whole path and the one a sensor is most likely to be recording. Humans close
    # that last gap with a short burst of small decelerating corrections (the corrective
    # phase Fitts's law predicts), so we do the same.
    last_x = points[-1][0] if points else start_x
    last_y = points[-1][1] if points else start_y
    gap = math.hypot(dest_x - last_x, dest_y - last_y)

    if gap > 3.0:
        n_corrections = max(2, min(6, int(gap / 7.0) + 1))
        for i in range(1, n_corrections + 1):
            # Ease-out: each correction covers less ground than the one before it.
            t = i / float(n_corrections)
            eased = 1.0 - (1.0 - t) ** 2
            cx = last_x + (dest_x - last_x) * eased
            cy = last_y + (dest_y - last_y) * eased
            if i < n_corrections:
                cx += random.gauss(0, 0.5)
                cy += random.gauss(0, 0.5)
            points.append((cx, cy, random.uniform(0.012, 0.030)))

    # Final exact landing
    points.append((dest_x, dest_y, random.uniform(0.01, 0.03)))
    return points


async def organic_mouse_move(
    page: Page,
    target_x: float,
    target_y: float,
    target_width: float = 30.0,
    target_height: float = 20.0
):
    """
    Moves mouse smoothly from current continuous position to target using WindMouse kinematics.
    Never teleports or creates discontinuous coordinate jumps.
    """
    await _sync_viewport(page)
    start_pos = MouseState.get_position(page)
    target_x, target_y = MouseState.clamp(page, target_x, target_y)

    trajectory = _generate_windmouse_trajectory(
        start=start_pos,
        end=(target_x, target_y),
        target_width=target_width,
        target_height=target_height,
    )

    for px, py, dt in trajectory:
        # Wind and tremor can push an intermediate point past the viewport edge.
        # Playwright would silently clamp it, producing a run of identical coordinates
        # that reads as a cursor pinned to the frame -- clamp ourselves so the path
        # stays continuous and in-bounds.
        cx, cy = MouseState.clamp(page, px, py)
        await page.mouse.move(cx, cy)
        MouseState.set_position(page, cx, cy)
        await asyncio.sleep(dt)


async def organic_bring_into_view(page: Page, locator: Locator, attempts: int = 4) -> Optional[Dict[str, float]]:
    """
    Brings an element into a comfortable reading band using real wheel events.

    scroll_into_view_if_needed() sets scrollTop directly: the document jumps hundreds
    of pixels in a single frame with no wheel events at all, which is one of the
    loudest signals in a scroll telemetry stream. Here we emit genuine inertial wheel
    bursts and converge on the target, only falling back to the instant snap if the
    element genuinely will not come into range (e.g. it lives in a nested scroller).
    """
    viewport = await _sync_viewport(page)
    comfortable_top = viewport["height"] * 0.22
    comfortable_bottom = viewport["height"] * 0.78

    box = None
    for _ in range(attempts):
        try:
            box = await locator.bounding_box()
        except Exception:
            box = None

        if not box:
            break

        centre_y = box["y"] + box["height"] / 2.0
        if comfortable_top <= centre_y <= comfortable_bottom:
            return box

        # Aim for roughly the vertical middle, slightly high, like a person does.
        desired_y = viewport["height"] * random.uniform(0.38, 0.52)
        delta = int(centre_y - desired_y)
        if abs(delta) < 24:
            return box

        await organic_scroll(page, total_distance=abs(delta), direction=1 if delta > 0 else -1)
        await asyncio.sleep(random.uniform(0.18, 0.42))

    # Last resort: the element is in a nested scroller we cannot reach with the wheel.
    try:
        await locator.scroll_into_view_if_needed(timeout=4000)
        await asyncio.sleep(random.uniform(0.2, 0.45))
        return await locator.bounding_box()
    except Exception:
        return box


async def organic_click(
    page: Page,
    locator: Locator,
    timeout_ms: int = 8000,
    post_delay_range: Tuple[float, float] = (0.35, 0.75)
) -> bool:
    """
    Interacts with an element with full bio-mechanical fidelity:
    1. Smoothly brings element into view with inertial wheel scrolling if off-screen.
    2. Moves mouse to a Gaussian-distributed point inside the element (away from exact edges).
    3. Micro-hesitation before mouse down.
    4. Realistic mouse down -> dwell -> mouse up click cycle.
    """
    try:
        box = await locator.bounding_box()
        viewport = await _sync_viewport(page)

        if not box or box["y"] < 50 or (box["y"] + box["height"]) > (viewport["height"] - 50):
            box = await organic_bring_into_view(page, locator)

        if not box:
            await locator.click(timeout=timeout_ms)
            await _resync_cursor_to(page, locator)
            return True

        # Target a natural point inside the bounding box (Gaussian distribution centered in middle 60%)
        margin_x = box["width"] * 0.2
        margin_y = box["height"] * 0.2
        target_x = box["x"] + margin_x + random.betavariate(2.5, 2.5) * (box["width"] - 2 * margin_x)
        target_y = box["y"] + margin_y + random.betavariate(2.5, 2.5) * (box["height"] - 2 * margin_y)

        # Move mouse with WindMouse physics
        await organic_mouse_move(page, target_x, target_y, box["width"], box["height"])

        # Pre-click micro-hesitation (eye-hand verification)
        await asyncio.sleep(random.uniform(0.08, 0.22))

        # Confirm the cursor is genuinely over the target before committing.
        #
        # If the page moved under us during the traversal, re-aim and make a
        # second, short approach rather than pressing into empty space. The
        # correction is itself the human behaviour: a person whose page shifts
        # mid-reach adjusts and clicks again, they do not press where the button
        # used to be. It also stays continuous -- `organic_mouse_move` walks from
        # the tracked position, so no teleport is emitted.
        for _ in range(2):
            if await cursor_is_over(page, locator, target_x, target_y):
                break
            aim = await _aim_at(page, locator)
            if aim is None:
                break
            target_x, target_y, box = aim
            # A short corrective reach, with the small pause of noticing.
            await asyncio.sleep(random.uniform(0.05, 0.14))
            await organic_mouse_move(page, target_x, target_y, box["width"], box["height"])
            await asyncio.sleep(random.uniform(0.06, 0.16))

        # Physical mouse down and up with realistic contact dwell time
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.065, 0.135))
        await page.mouse.up()
        
        # Post-click human reaction pause
        await asyncio.sleep(random.uniform(*post_delay_range))
        return True

    except Exception as e:
        logger.debug(f"Organic click fallback: {e}")
        try:
            # Playwright's own click teleports the cursor straight onto the element.
            # Walk there first so the page still sees a continuous path, and record
            # where it landed -- otherwise the *next* organic move starts from a stale
            # coordinate and emits the exact discontinuity this module exists to avoid.
            with contextlib.suppress(Exception):
                box = await locator.bounding_box()
                if box:
                    await organic_mouse_move(
                        page,
                        box["x"] + box["width"] / 2.0,
                        box["y"] + box["height"] / 2.0,
                        box["width"],
                        box["height"],
                    )
            await locator.click(timeout=3000)
            await _resync_cursor_to(page, locator)
            return True
        except Exception:
            return False


async def cursor_is_over(page: Page, locator: Locator, x: float, y: float) -> bool:
    """
    Is the point (x, y) actually on top of this element right now?

    Asked with the page's own hit-testing, which is the same answer a real click
    would get: `elementFromPoint` accounts for overlays, sticky headers and
    anything else covering the target, none of which a bounding box knows about.

    This exists because a press at stale coordinates fails silently. The cursor
    is walked to a point computed from a bounding box read before the traversal
    began; the walk takes several hundred milliseconds, LinkedIn lazy-loads the
    top card in that window, and the button moves. `mouse.down()` then lands on
    whatever is now there -- usually nothing -- and raises no error, so the click
    reports success while doing nothing at all.
    """
    try:
        handle = await locator.element_handle(timeout=2000)
        if handle is None:
            return False
        # `handle.evaluate` passes the element as the first argument. Passing an
        # ElementHandle inside a list to `page.evaluate` instead does not
        # serialise -- it throws, which the fallback below swallowed, so the
        # probe silently answered "yes" for every point on the page including
        # empty space. A guard that always passes is worse than no guard,
        # because it looks like one.
        return bool(await handle.evaluate(
            """(el, pt) => {
                const hit = document.elementFromPoint(pt.x, pt.y);
                if (!hit) return false;
                // The hit must be the element or something inside it -- clicking
                // a <span> inside the button still hits the button.
                //
                // Deliberately NOT `hit.contains(el)`: every ancestor contains
                // the target, so <body> would satisfy it and the probe would
                // answer "yes" for any empty point on the page.
                return hit === el || el.contains(hit);
            }""",
            {"x": x, "y": y},
        ))
    except Exception as exc:
        # Unknowable rather than false: refusing to click on a failed probe
        # would be worse than clicking where we intended to. Logged, so a probe
        # that is failing every time cannot hide the way the last one did.
        logger.debug(f"Cursor hit-test unavailable, clicking as aimed: {exc}")
        return True


async def _aim_at(page: Page, locator: Locator) -> Optional[Tuple[float, float, Dict[str, float]]]:
    """A fresh Gaussian-distributed point inside the element's current box."""
    box = await locator.bounding_box()
    if not box or box["width"] <= 0 or box["height"] <= 0:
        return None
    margin_x = box["width"] * 0.2
    margin_y = box["height"] * 0.2
    x = box["x"] + margin_x + random.betavariate(2.5, 2.5) * (box["width"] - 2 * margin_x)
    y = box["y"] + margin_y + random.betavariate(2.5, 2.5) * (box["height"] - 2 * margin_y)
    return x, y, box


async def _resync_cursor_to(page: Page, locator: Locator):
    """Records the cursor position after a fallback click so continuity is not broken."""
    try:
        box = await locator.bounding_box()
        if box:
            MouseState.set_position(
                page,
                box["x"] + box["width"] / 2.0,
                box["y"] + box["height"] / 2.0,
            )
    except Exception:
        pass


# ==============================================================================
# 3. Inertial Momentum Scrolling
# ==============================================================================

async def organic_scroll(
    page: Page,
    total_distance: int,
    direction: int = 1,
    num_pulses: Optional[int] = None,
    enable_micro_swerves: bool = True
):
    """
    Simulates real trackpad or mouse-wheel scrolling physics:
    Emits a burst of micro-wheel events with an initial acceleration phase
    followed by an exponential inertial decay curve (decaying velocity).

    The pulse count is randomized per gesture. A fixed count (every scroll being
    exactly N wheel events) is itself a periodic signature -- real swipes vary with
    finger speed and travel.
    """
    total_scroll = total_distance * direction
    if abs(total_scroll) < 5:
        return

    if num_pulses is None:
        # Longer flicks carry more inertia and therefore emit more decaying events.
        base = 14 + int(abs(total_scroll) / 45.0)
        num_pulses = max(12, min(38, base + random.randint(-3, 5)))

    # Generate exponentially decaying delta weights
    decay_rate = random.uniform(2.8, 3.8)
    accel_fraction = random.uniform(0.14, 0.26)
    raw_weights = []
    for i in range(num_pulses):
        t = i / float(num_pulses - 1)
        if t < accel_fraction:
            # Acceleration phase: finger building speed
            w = math.sin((t / accel_fraction) * (math.pi / 2))
        else:
            # Exponential inertial decay phase
            w = math.exp(-decay_rate * (t - accel_fraction) / (1.0 - accel_fraction))
        raw_weights.append(max(0.01, w))

    sum_weights = sum(raw_weights)
    deltas = [int(round((w / sum_weights) * total_scroll)) for w in raw_weights]

    # Absorb the rounding remainder across the *leading* (largest) pulses rather than
    # dumping it on the final one -- the tail of a real flick is always the smallest
    # event, so a fat last delta inverts the decay curve.
    diff = total_scroll - sum(deltas)
    idx = 0
    step = 1 if diff > 0 else -1
    while diff != 0 and deltas:
        deltas[idx % len(deltas)] += step
        diff -= step
        idx += 1

    cur_x, cur_y = MouseState.get_position(page)

    for delta in deltas:
        if delta == 0:
            continue

        await page.mouse.wheel(0, delta)

        # Trackpad micro-swerve (slight horizontal mouse drift while scrolling)
        if enable_micro_swerves and random.random() < 0.25:
            cur_x, cur_y = MouseState.clamp(
                page,
                cur_x + random.uniform(-1.2, 1.2),
                cur_y + random.uniform(-0.8, 0.8),
            )
            await page.mouse.move(cur_x, cur_y)
            MouseState.set_position(page, cur_x, cur_y)

        await asyncio.sleep(random.uniform(0.009, 0.022))


async def organic_scroll_over(page: Page, container: Locator, total_distance: int, direction: int = 1):
    """
    Scrolls with the cursor parked over a specific container.

    Wheel events are delivered to whatever element is under the pointer. LinkedIn's
    job search is a two-column layout with independent scrollers, so scrolling the
    description while the cursor happens to sit over the results list silently moves
    the wrong pane -- and produces a telemetry trace where scrolling never correlates
    with what the user is supposedly reading.
    """
    try:
        box = await container.bounding_box()
        if box:
            await organic_mouse_move(
                page,
                box["x"] + box["width"] * random.uniform(0.35, 0.65),
                box["y"] + box["height"] * random.uniform(0.35, 0.6),
                box["width"],
                box["height"],
            )
            await asyncio.sleep(random.uniform(0.08, 0.2))
    except Exception:
        pass

    await organic_scroll(page, total_distance=total_distance, direction=direction)


async def smooth_scroll(page: Page, scroll_count: int = 3, distance: int = 350):
    """
    Replaces old discrete wheel jumps with organic multi-burst human scrolling.
    Includes natural pauses and reading hesitations.
    """
    for i in range(scroll_count):
        actual_dist = distance + random.randint(-50, 70)
        await organic_scroll(page, total_distance=actual_dist, direction=1)
        await asyncio.sleep(random.uniform(0.7, 1.8))
        
        # 25% chance of a micro-upward scroll (re-reading or scanning adjustment)
        if random.random() < 0.25 and i > 0:
            await organic_scroll(page, total_distance=random.randint(40, 110), direction=-1)
            await asyncio.sleep(random.uniform(0.5, 1.2))


# ==============================================================================
# 4. Cognitive Keystroke Dynamics & Typo Engine
# ==============================================================================

# QWERTY Key Coordinate Mapping for Bi-Gram Travel Distance
QWERTY_COORDS: Dict[str, Tuple[float, float]] = {
    'q': (0, 0), 'w': (0, 1), 'e': (0, 2), 'r': (0, 3), 't': (0, 4),
    'y': (0, 5), 'u': (0, 6), 'i': (0, 7), 'o': (0, 8), 'p': (0, 9),
    'a': (1, 0.3), 's': (1, 1.3), 'd': (1, 2.3), 'f': (1, 3.3), 'g': (1, 4.3),
    'h': (1, 5.3), 'j': (1, 6.3), 'k': (1, 7.3), 'l': (1, 8.3),
    'z': (2, 0.8), 'x': (2, 1.8), 'c': (2, 2.8), 'v': (2, 3.8), 'b': (2, 4.8),
    'n': (2, 5.8), 'm': (2, 6.8), ' ': (3, 4.5)
}

ADJACENT_KEYS = {
    'a': ['s', 'q', 'z'], 'b': ['v', 'g', 'h', 'n'], 'c': ['x', 'd', 'v'],
    'd': ['s', 'e', 'r', 'f', 'x', 'c'], 'e': ['w', 's', 'd', 'r'],
    'f': ['d', 'r', 't', 'g', 'v', 'c'], 'g': ['f', 't', 'y', 'h', 'b', 'v'],
    'h': ['g', 'y', 'u', 'j', 'n', 'b'], 'i': ['u', 'j', 'k', 'o'],
    'j': ['h', 'u', 'i', 'k', 'm', 'n'], 'k': ['j', 'i', 'o', 'l', 'm'],
    'l': ['k', 'o', 'p'], 'm': ['n', 'j', 'k'], 'n': ['b', 'h', 'j', 'm'],
    'o': ['i', 'k', 'l', 'p'], 'p': ['o', 'l'], 'r': ['e', 'd', 'f', 't'],
    's': ['a', 'w', 'e', 'd', 'x', 'z'], 't': ['r', 'f', 'g', 'y'],
    'u': ['y', 'h', 'j', 'i'], 'v': ['c', 'f', 'g', 'b'], 'w': ['q', 'a', 's', 'e'],
    'x': ['z', 's', 'd', 'c'], 'y': ['t', 'g', 'h', 'u'], 'z': ['a', 's', 'x']
}

def _calculate_flight_time(char1: str, char2: str) -> float:
    """Calculates realistic flight time between two characters based on QWERTY geometry."""
    c1 = char1.lower()
    c2 = char2.lower()
    
    if c1 in QWERTY_COORDS and c2 in QWERTY_COORDS:
        r1, col1 = QWERTY_COORDS[c1]
        r2, col2 = QWERTY_COORDS[c2]
        dist = math.hypot(r2 - r1, col2 - col1)
    else:
        dist = 2.5

    # Base flight time scaled by physical key distance
    base_time = 0.055 + (dist * 0.014)
    # Add log-normal variance
    flight_time = random.gauss(base_time, 0.018)
    return max(0.035, min(0.24, flight_time))


def _dwell_time(char: str) -> float:
    """
    Hold duration between keydown and keyup for a single character.

    Keystroke-dynamics models score dwell time as heavily as flight time. Playwright's
    keyboard.type() releases the key in the same tick, so every keydown/keyup pair has
    a delta of ~0ms -- a value no human hand can produce and a trivially separable
    distribution. Real dwell sits around 60-120ms, and modified keys are held longer.
    """
    base = random.gauss(0.082, 0.021)
    if char.isupper() or char in '!@#$%^&*()_+{}|:"<>?~':
        base += random.uniform(0.015, 0.04)   # shift held across the stroke
    elif char == " ":
        base += random.uniform(0.005, 0.02)   # thumb strike is a touch heavier
    return max(0.035, min(0.16, base))


# Characters that a US-QWERTY keyboard can only produce with Shift held. Uppercase
# letters are handled separately via str.isupper() so non-ASCII letters are covered too.
_SHIFTED_PUNCTUATION = set('~!@#$%^&*()_+{}|:"<>?')


def _needs_shift(char: str) -> bool:
    return char.isupper() or char in _SHIFTED_PUNCTUATION


class ShiftState:
    """
    Tracks whether Shift is physically held, so runs of shifted characters hold it once.

    Without this, Playwright emits a capital as keydown{key:'A', shiftKey:false} with no
    Shift event anywhere in the stream -- a combination no physical keyboard can produce.
    Unlike a timing anomaly, which is statistical, this is a binary contradiction: any
    handler listening for keydown can check `e.key !== e.key.toLowerCase() && !e.shiftKey`
    and be certain. Measured before this existed: typing "Aa!7" produced key 'A' and key
    '!' both with shiftKey false and zero Shift keydowns.

    Holding Shift across a run ("AI", "SOX") rather than tapping it per character also
    matches what a hand actually does, and keeps the modifier's own dwell time plausible.
    """

    def __init__(self, page: Page):
        self.page = page
        self.down = False

    async def want(self, needed: bool):
        if needed and not self.down:
            await self.page.keyboard.down("Shift")
            # The modifier lands slightly before the key it modifies.
            await asyncio.sleep(random.uniform(0.02, 0.06))
            self.down = True
        elif not needed and self.down:
            # ...and lifts slightly after it.
            await asyncio.sleep(random.uniform(0.015, 0.045))
            await self.page.keyboard.up("Shift")
            self.down = False

    async def release(self):
        with contextlib.suppress(Exception):
            await self.want(False)


async def _press_char(page: Page, char: str, shift: Optional[ShiftState] = None):
    """
    Emits a character as a real Shift-aware keydown -> hold -> keyup sequence.

    Falls back to keyboard.type for anything the key map cannot express (accents,
    emoji, CJK), where insertText is genuinely what a real IME would produce anyway.
    """
    owned = shift is None
    state = shift or ShiftState(page)
    try:
        await state.want(_needs_shift(char))
        await page.keyboard.down(char)
        await asyncio.sleep(_dwell_time(char))
        await page.keyboard.up(char)
        if owned:
            await state.release()
    except Exception:
        await state.release()
        await page.keyboard.type(char)


async def organic_type(
    page: Page,
    locator: Locator,
    text: str,
    enable_typos: bool = True,
    clear_field_first: bool = True
):
    """
    Types text with cognitive rhythm:
    1. Clears field using realistic keyboard select-all + backspace (no synthetic .fill('')).
    2. Calculates physical flight times and dwell times based on QWERTY geometry.
    3. Simulates realistic typo creation, delayed realization, backspacing, and correction.
    4. Adds human thinking pauses at word and punctuation boundaries.
    """
    try:
        # Click naturally into input field
        await organic_click(page, locator, post_delay_range=(0.15, 0.35))
        
        # Organic Field Clearing via Keyboard Shortcuts
        if clear_field_first:
            existing_val = await locator.input_value()
            if existing_val:
                mod_key = "Meta" if sys.platform == "darwin" else "Control"
                await page.keyboard.press(f"{mod_key}+A", delay=random.uniform(55, 110))
                await asyncio.sleep(random.uniform(0.08, 0.18))
                await page.keyboard.press("Backspace", delay=random.uniform(50, 100))
                await asyncio.sleep(random.uniform(0.12, 0.25))

        prev_char = " "
        shift = ShiftState(page)

        # Typing is bursty. People emit a run of characters at near-peak speed, then
        # stop while the next chunk is composed or the eyes flick back to the screen.
        # Drawing every inter-key gap independently from one distribution produces a
        # flat, unimodal histogram; real keystroke-dynamics traces are bimodal, with a
        # tight within-burst mode and a long between-burst mode. Detectors that score
        # typing rhythm at all are scoring precisely that shape.
        burst_remaining = random.randint(3, 9)

        for idx, char in enumerate(text):
            # 1. Realistic Typo Simulation (1.2% probability on alphabetic characters)
            if enable_typos and char.lower() in ADJACENT_KEYS and random.random() < 0.012:
                wrong_char = random.choice(ADJACENT_KEYS[char.lower()])
                if char.isupper():
                    wrong_char = wrong_char.upper()
                
                await _press_char(page, wrong_char, shift)
                await asyncio.sleep(random.uniform(0.12, 0.26))

                # Let Shift up before Backspace: holding it would send Shift+Backspace,
                # which is a different chord than the one a person uses to fix a typo.
                await shift.release()
                await page.keyboard.press("Backspace", delay=random.uniform(50, 100))
                await asyncio.sleep(random.uniform(0.09, 0.19))

            # 2. Type actual character with a real key-hold duration
            await _press_char(page, char, shift)

            # Flight time to next key
            flight_time = _calculate_flight_time(prev_char, char)
            
            # Punctuation or boundary hesitation
            if char in ".,?!;:\n":
                flight_time += random.uniform(0.18, 0.42)
            elif char == " ":
                flight_time += random.uniform(0.06, 0.16)
            elif char.isupper() or char.isdigit():
                flight_time += random.uniform(0.05, 0.12)
                
            await asyncio.sleep(flight_time)

            # End of a burst: the pause between chunks, longer than any within-burst gap.
            burst_remaining -= 1
            if burst_remaining <= 0:
                # Nobody keeps Shift pressed through a pause between chunks -- the hand
                # comes off the key. Leaving it held would report a multi-second modifier
                # dwell that no typist produces.
                await shift.release()
                await asyncio.sleep(random.uniform(0.19, 0.78))
                burst_remaining = random.randint(3, 9)
            elif random.random() < 0.02:
                # Rarer, deeper hesitation -- rethinking the phrasing mid-burst.
                await shift.release()
                await asyncio.sleep(random.uniform(0.4, 1.1))

            prev_char = char

        await shift.release()

    except Exception as e:
        logger.debug(f"Organic typing fallback: {e}")
        with contextlib.suppress(Exception):
            await ShiftState(page).release()
            await page.keyboard.up("Shift")
        await locator.fill(text)


async def organic_select_option(page: Page, select_locator: Locator, target_label: str) -> bool:
    """
    Chooses an option in a native <select> using real keyboard navigation.

    locator.select_option() sets `.value` from the driver and dispatches a change event
    whose isTrusted is false, with no focus, no keydown and no keyup anywhere around it.

    A native <select> popup is an OS-level window that CDP mouse events cannot reach,
    and on macOS the arrow keys do not cycle a *closed* select either. What does work,
    and is exactly what a keyboard user does, is type-ahead: with the control focused,
    typing the leading characters of an option selects it and fires a genuinely trusted
    change event. So: click the control for real pointer telemetry, dismiss the popup,
    then type the option name.
    """
    try:
        options = await select_locator.locator("option").all()
        labels: List[str] = []
        for opt in options:
            labels.append((await opt.inner_text() or "").strip())

        if not labels:
            return False

        wanted = target_label.strip().lower()
        target_index = next((i for i, t in enumerate(labels) if t.lower() == wanted), -1)
        if target_index == -1:
            target_index = next((i for i, t in enumerate(labels) if wanted in t.lower()), -1)
        if target_index == -1:
            return False

        expected_value = await options[target_index].get_attribute("value")
        if (await select_locator.input_value()) == expected_value:
            return True

        target_text = labels[target_index]

        # Real click: genuine pointermove, mousedown and mouseup on the control.
        await organic_click(page, select_locator, post_delay_range=(0.12, 0.3))
        # The click opened the OS popup; close it so the keystrokes go to the element.
        with contextlib.suppress(Exception):
            await page.keyboard.press("Escape", delay=random.uniform(50, 100))
            await asyncio.sleep(random.uniform(0.12, 0.26))

        # Type-ahead. Chrome accumulates the typed prefix, so we add characters until
        # the selection lands on the option we want -- which also handles options that
        # share a leading word ("Bachelor's Degree" vs "Bachelor of Science").
        # Option labels are almost always capitalised ("Bachelor's Degree", "Yes"), so
        # this path needs the same Shift handling as organic_type -- keyboard.press('B')
        # on its own reports a capital with shiftKey false.
        typed = 0
        shift = ShiftState(page)
        for ch in target_text:
            if not ch.strip():
                # A space restarts type-ahead in some builds; stop and check instead.
                break
            await _press_char(page, ch, shift)
            typed += 1
            await asyncio.sleep(random.uniform(0.07, 0.15))
            if (await select_locator.input_value()) == expected_value and typed >= 1:
                break
        await shift.release()

        await asyncio.sleep(random.uniform(0.2, 0.45))

        if (await select_locator.input_value()) == expected_value:
            return True

        # The control ignored type-ahead (custom widget, disabled, etc.). Falling back
        # keeps the application answerable rather than silently leaving a blank field.
        logger.debug("Type-ahead did not register; falling back to select_option.")
        await select_locator.select_option(index=target_index)
        return True

    except Exception as e:
        logger.debug(f"Organic select fallback: {e}")
        try:
            await select_locator.select_option(label=target_label)
            return True
        except Exception:
            return False


# ==============================================================================
# 5. Background Gaze & Idle Drift during LLM / Network Delays
# ==============================================================================

class OrganicIdleDrift:
    """
    Keeps the pointer alive during LLM/network waits the way a resting hand does.

    The naive version of this -- a 3-5px nudge every 0.6-1.6s for as long as the wait
    lasts -- is its own periodic signature. It describes a pointer that never stops
    moving, never moves far, and does so at a steady rate for thirty seconds straight:
    a metronome, and one no hand produces. Real idle telemetry is the opposite shape,
    long silences broken by short clusters of movement when the person shifts their
    grip, and very often nothing at all because the hand is not on the mouse.

    So: with probability `still_probability` this emits nothing for the entire wait,
    and otherwise it alternates multi-second stillness with 2-5 sample micro-bursts
    delivered at a real pointer's sample rate.
    """
    def __init__(self, page: Page, max_drifts: int = 15, still_probability: float = 0.35):
        self.page = page
        self.max_drifts = max_drifts
        self.still_probability = still_probability
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def __aenter__(self):
        self._running = True
        self._task = asyncio.create_task(self._drift_loop())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _drift_loop(self):
        # The hand is simply not on the mouse for this wait. Emitting nothing is a
        # completely ordinary trace; emitting a steady tick is not.
        if random.random() < self.still_probability:
            return

        emitted = 0
        while self._running and emitted < self.max_drifts:
            try:
                # Long stillness between bursts -- this is most of the timeline.
                await asyncio.sleep(random.uniform(2.5, 7.5))
                if not self._running or self.page.is_closed():
                    break

                # One burst: a few samples at a real pointer's cadence, as the hand
                # settles into a slightly different resting position.
                burst = random.randint(2, 5)
                cur_x, cur_y = MouseState.get_position(self.page)
                for _ in range(burst):
                    if not self._running:
                        break
                    cur_x, cur_y = MouseState.clamp(
                        self.page,
                        cur_x + random.gauss(0, 1.7),
                        cur_y + random.gauss(0, 1.2),
                    )
                    await self.page.mouse.move(cur_x, cur_y)
                    MouseState.set_position(self.page, cur_x, cur_y)
                    await asyncio.sleep(random.uniform(0.011, 0.028))
                    emitted += 1
                    if emitted >= self.max_drifts:
                        break
            except Exception:
                break


# ==============================================================================
# 6. Natural Reading, Warm-Up & Exploration Browsing
# ==============================================================================

async def simulate_human_reading(
    page: Page,
    min_seconds: float = 6.0,
    max_seconds: float = 14.0,
    description_container_selector: Optional[str] = None
):
    """
    Simulates human reading dynamics:
    - Inertial downward scrolling in paragraph chunks.
    - Gaze pauses and reading hesitations.
    - Occasional upward scroll adjustments (re-reading requirements).
    """
    total_reading_time = random.uniform(min_seconds, max_seconds)
    start_time = time.time()

    viewport = await _sync_viewport(page)

    # Park the cursor over the pane we are supposed to be reading. Wheel events go to
    # whatever sits under the pointer, so on LinkedIn's two-column job search a cursor
    # left over the results list scrolls the wrong column entirely -- the telemetry then
    # shows a user who "reads" a description they never scrolled.
    reading_box: Optional[Dict[str, float]] = None
    selector = description_container_selector or "#job-details, .jobs-description__content, .jobs-box__html-content"
    try:
        container = page.locator(selector).first
        if await container.count() > 0:
            reading_box = await container.bounding_box()
    except Exception:
        reading_box = None

    if reading_box:
        await organic_mouse_move(
            page,
            reading_box["x"] + reading_box["width"] * random.uniform(0.3, 0.65),
            reading_box["y"] + reading_box["height"] * random.uniform(0.25, 0.5),
        )
        await asyncio.sleep(random.uniform(0.2, 0.5))

    while (time.time() - start_time) < total_reading_time:
        roll = random.random()

        if roll < 0.60:
            # Scroll down like reading next paragraph
            scroll_dist = random.randint(140, 320)
            await organic_scroll(page, total_distance=scroll_dist, direction=1)

            # Gentle gaze drift over the text being read
            if reading_box:
                target_x = reading_box["x"] + reading_box["width"] * random.uniform(0.2, 0.7)
                target_y = reading_box["y"] + reading_box["height"] * random.uniform(0.2, 0.6)
            else:
                target_x = viewport["width"] * random.uniform(0.35, 0.68)
                target_y = viewport["height"] * random.uniform(0.28, 0.65)
            await organic_mouse_move(page, target_x, target_y)
            await asyncio.sleep(random.uniform(1.2, 2.8))

        elif roll < 0.82:
            # Pause in place (contemplating / reading)
            await asyncio.sleep(random.uniform(1.5, 3.2))

        else:
            # Re-read: scroll back up slightly
            scroll_up = random.randint(70, 160)
            await organic_scroll(page, total_distance=scroll_up, direction=-1)
            await asyncio.sleep(random.uniform(1.0, 2.0))


async def human_navigate(
    page: Page,
    url: str,
    referer: Optional[str] = None,
    wait_until: str = "domcontentloaded",
    settle: Tuple[float, float] = (1.8, 3.6),
) -> bool:
    """
    Navigates with a plausible Referer and a post-load settle.

    page.goto() sends no Referer at all, so on the wire every navigation looks like a
    URL pasted into the address bar. A person reaching a filtered job-search results
    page has essentially always arrived from another LinkedIn page, and an entire
    session with no referrer chain is a server-side observation that costs the platform
    nothing to make and needs no JavaScript at all.

    Passing the current page as the default referer builds that chain automatically:
    feed -> search -> job -> search, exactly the shape real navigation produces.
    """
    if not page:
        return False
    try:
        if page.is_closed():
            return False
    except Exception:
        return False

    if referer is None:
        try:
            current = page.url or ""
        except Exception:
            current = ""
        # Only same-site referrers; inventing a cross-site one would contradict the
        # navigation history the browser itself reports.
        if current.startswith("http") and "linkedin.com" in current and "linkedin.com" in url:
            referer = current
        elif current.startswith("http") and "naukri.com" in current and "naukri.com" in url:
            referer = current

    try:
        if referer:
            await page.goto(url, wait_until=wait_until, referer=referer)
        else:
            await page.goto(url, wait_until=wait_until)
        await asyncio.sleep(random.uniform(*settle))
        return True
    except Exception as e:
        logger.debug(f"Navigation to {url} failed: {e}")
        return False


async def human_go_back(page: Page, settle: Tuple[float, float] = (1.6, 3.2)) -> bool:
    """
    Returns to the previous page using the browser's own history.

    Re-issuing goto() on the URL you just came from produces an A -> B -> A chain of
    fresh document loads that no person generates: people press Back. A history
    traversal is a different navigation type on the wire (and restores scroll position
    the way the user expects), so preferring it where it is available removes a
    repeated, distinctive pattern from the trace.
    """
    try:
        response = await page.go_back(wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(*settle))
        return response is not None
    except Exception as e:
        logger.debug(f"Back navigation unavailable: {e}")
        return False


# Warm-up destinations. A person opening LinkedIn does not always land on the feed and
# go straight to job search; they check notifications, glance at invitations, read a
# message. Varying the entry path stops every session from beginning with the identical
# three requests.
WARM_UP_DESTINATIONS = [
    ("https://www.linkedin.com/feed/", "feed"),
    ("https://www.linkedin.com/notifications/", "notifications"),
    ("https://www.linkedin.com/mynetwork/", "network"),
    ("https://www.linkedin.com/jobs/", "jobs home"),
]


async def perform_warm_up_browsing(page: Page):
    """
    Opens the session the way a person does: one or two ordinary pages before working.

    The point is not the scrolling, it is that the first thing the account does after
    authenticating is not a fully-parameterised job-search URL with six filter codes.
    """
    stops = random.sample(WARM_UP_DESTINATIONS, k=random.randint(1, 3))
    console.print(
        f"[dim cyan]☕ Session warm-up: {', '.join(name for _, name in stops)} "
        f"before starting the search...[/dim cyan]"
    )

    try:
        for url, _name in stops:
            if not await human_navigate(page, url, settle=(2.4, 4.6)):
                continue

            for _ in range(random.randint(2, 5)):
                await organic_scroll(page, total_distance=random.randint(280, 620), direction=1)
                await asyncio.sleep(random.uniform(1.8, 4.6))

                # Occasionally hover something, as the eye tracks across a post.
                if random.random() < 0.3:
                    target = page.locator(
                        "button.reactions-react-button, .feed-shared-social-action-bar, "
                        ".artdeco-card, li.artdeco-list__item"
                    ).first
                    if await target.count() > 0 and await target.is_visible():
                        box = await target.bounding_box()
                        if box:
                            await organic_mouse_move(
                                page,
                                box["x"] + box["width"] * random.uniform(0.2, 0.7),
                                box["y"] + box["height"] * random.uniform(0.3, 0.7),
                            )
                            await asyncio.sleep(random.uniform(0.6, 1.6))

            # Scroll back up part of the way, the way people do before leaving a page.
            if random.random() < 0.5:
                await organic_scroll(page, total_distance=random.randint(200, 500), direction=-1)
                await asyncio.sleep(random.uniform(0.8, 2.0))

        console.print("[dim green]✓ Warm-up complete.[/dim green]\n")
    except Exception as e:
        logger.debug(f"Warm-up info: {e}")


# Fallback only. Preferring companies the agent has actually just seen in its own search
# results is both more natural (you look up a company whose posting you just read) and
# avoids the trap of a fixed list: the same six company pages visited forever is itself
# a durable per-account fingerprint, and a distinctive one.
FALLBACK_COMPANY_PAGES = [
    "https://www.linkedin.com/company/google/",
    "https://www.linkedin.com/company/microsoft/",
    "https://www.linkedin.com/company/meta/",
    "https://www.linkedin.com/company/nvidia/",
    "https://www.linkedin.com/company/amazon/",
    "https://www.linkedin.com/company/infosys/",
    "https://www.linkedin.com/company/tata-consultancy-services/",
    "https://www.linkedin.com/company/flipkart/",
    "https://www.linkedin.com/company/swiggy-in/",
    "https://www.linkedin.com/company/zomato/",
]


async def explore_random_company_page(
    page: Page,
    target_company_url: Optional[str] = None,
    pool: Optional[List[str]] = None,
) -> bool:
    """
    Follows a tangent to a company page, then comes back via the Back button.

    Returns True if the caller can rely on history to get back (i.e. Back worked), so
    the caller does not have to re-navigate to where it already was.
    """
    candidates = pool or FALLBACK_COMPANY_PAGES
    url = target_company_url or random.choice(candidates)
    logger.info(f"Tangent: looking at {url}...")

    if not await human_navigate(page, url, settle=(2.6, 4.4)):
        return False

    try:
        for _ in range(random.randint(2, 4)):
            await organic_scroll(page, total_distance=random.randint(240, 480), direction=1)
            await asyncio.sleep(random.uniform(1.8, 3.4))
        await asyncio.sleep(random.uniform(1.2, 2.6))
    except Exception as e:
        logger.debug(f"Company explore info: {e}")

    return await human_go_back(page)


async def take_natural_human_break(page: Optional[Page] = None, min_sec: int = 240, max_sec: int = 900):
    """
    Takes a real break: minutes, away from the window, not a sub-minute pause.

    A 30-second gap is not a break, it is a slow action -- and a script that "rests" for
    exactly 25-55 seconds and then resumes at full speed has described its own scheduler.
    Real interruptions are minutes long and the window loses focus for all of them,
    which is why this hands off to simulate_away rather than sleeping in place.
    """
    break_duration = random.randint(min_sec, max_sec)
    console.print(
        f"\n[bold yellow]☕ Break: {break_duration / 60:.1f} min away from the browser.[/bold yellow]"
    )

    if page is not None:
        await simulate_away(page, break_duration)
    else:
        await asyncio.sleep(break_duration)

    console.print("[dim green]Back at the keyboard.[/dim green]\n")


# ==============================================================================
# 7. Backwards Compatibility Aliases
# ==============================================================================
natural_mouse_move = organic_mouse_move
natural_click = organic_click
natural_type = organic_type
