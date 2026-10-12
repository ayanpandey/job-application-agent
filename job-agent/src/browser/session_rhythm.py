"""
Session-level temporal governor.

Pointer physics and keystroke dynamics decide whether a single *action* looks human.
They say nothing about the thing that actually separates a job seeker from an agent
when a platform looks at weeks of history: the shape of the session. Volume, hour of
day, session length, how the count varies day to day, and whether there are days with
no activity at all. Those are cheap for a platform to aggregate per account and
expensive for a script to fake, because the fake has to be consistent over time rather
than convincing for one frame.

Three properties this enforces that the per-action layer cannot:

  1. Activity lands inside plausible waking hours for the profile's timezone. A burst
     of applications at 03:40 local is a flag no trajectory model can undo.
  2. Sessions end. A person searches for 25-70 minutes and stops; they do not grind for
     four hours without a gap.
  3. The daily count varies, and some days are zero. Applying to exactly the configured
     maximum every single day is the single most legible pattern the whole system can
     emit -- it is a number no human converges on by accident.

The daily figures are drawn from a *date-seeded* RNG, so they stay stable if the agent
is restarted mid-day (restarting must not roll a fresh, larger quota) while still
differing from one day to the next.
"""

import asyncio
import datetime as dt
import random
import time
import zlib
from dataclasses import dataclass
from typing import Optional, Tuple

from playwright.async_api import Page

from src.core.logger import logger, console
from src.browser.attention import simulate_away, maybe_glance_away


@dataclass
class RhythmConfig:
    """Tunables for the temporal governor. All times are in the machine's local zone."""

    enabled: bool = True

    # Wall-clock window in which the agent is allowed to act, [start, end).
    active_hours: Tuple[int, int] = (8, 23)

    # How long one uninterrupted working session lasts before the agent stops.
    session_minutes: Tuple[float, float] = (25.0, 70.0)

    # A real break: away from the window entirely.
    long_break_minutes: Tuple[float, float] = (6.0, 22.0)

    # Shorter pauses between clusters of work.
    actions_between_micro_breaks: Tuple[int, int] = (4, 11)
    micro_break_seconds: Tuple[float, float] = (35.0, 180.0)

    # Odds of looking at another window between two units of work.
    glance_away_probability: float = 0.22
    glance_away_seconds: Tuple[float, float] = (12.0, 95.0)

    # Daily volume is the configured limit scaled by +/- this fraction.
    daily_quota_jitter: float = 0.35

    # Fraction of days with no activity at all.
    rest_day_probability: float = 0.10


def _day_rng(salt: str, today: Optional[dt.date] = None) -> random.Random:
    """
    A RNG that is stable for a given calendar day and salt.

    Python's hash() is salted per process, so it cannot be used here: the whole point is
    that restarting the agent at 3pm must draw the same daily quota it drew at 9am.
    """
    day = (today or dt.date.today()).isoformat()
    seed = zlib.crc32(f"{day}:{salt}".encode("utf-8"))
    return random.Random(seed)


class SessionRhythm:
    """
    Governs when the agent may work, for how long, and how often it rests.

    One instance per CLI invocation owns the *shared* session clock (active hours,
    session budget, breaks) while tracking a separate daily quota per kind of work --
    evaluating a listing and submitting an application are not interchangeable, and
    counting them in one bucket would let a long browsing session silently consume the
    application allowance, or vice versa.

    Usage:
        rhythm = SessionRhythm(cfg, quotas={"apply": 25, "evaluate": 80})
        ok, why = rhythm.preflight()
        if not ok: ...stop...
        ...
        for job in jobs:
            ...do one unit of work...
            rhythm.note_action("apply")
            if not await rhythm.pace(page, "apply"):
                break        # session is over for today
    """

    def __init__(
        self,
        config: Optional[RhythmConfig] = None,
        quotas: Optional[dict] = None,
        salt: str = "session",
    ):
        self.config = config or RhythmConfig()
        self.salt = salt

        self.started_at = time.time()
        self.counts: dict = {}

        rng = _day_rng(salt)
        self._is_rest_day = rng.random() < self.config.rest_day_probability

        # Each quota gets its own jitter draw from the same date-seeded stream, so the
        # day's numbers are reproducible across a restart but unrelated to each other.
        jitter = self.config.daily_quota_jitter
        self.daily_targets: dict = {}
        for kind, base in sorted((quotas or {"apply": 20}).items()):
            self.daily_targets[kind] = max(
                1, int(round(max(1, int(base)) * rng.uniform(1.0 - jitter, 1.0 + jitter)))
            )

        # Session length is drawn per run, not per day: two sessions on the same day
        # should not be identical twins.
        self.session_budget_seconds = random.uniform(*self.config.session_minutes) * 60.0
        self._next_micro_break_at = random.randint(*self.config.actions_between_micro_breaks)

    # ------------------------------------------------------------------ state

    @property
    def elapsed_minutes(self) -> float:
        return (time.time() - self.started_at) / 60.0

    @property
    def total_actions(self) -> int:
        return sum(self.counts.values())

    def within_active_hours(self, now: Optional[dt.datetime] = None) -> bool:
        start, end = self.config.active_hours
        hour = (now or dt.datetime.now()).hour
        if start <= end:
            return start <= hour < end
        # Window wraps past midnight (e.g. 20 -> 2).
        return hour >= start or hour < end

    def is_rest_day(self) -> bool:
        return self._is_rest_day

    def budget_exhausted(self) -> bool:
        return (time.time() - self.started_at) >= self.session_budget_seconds

    def quota_reached(self, kind: str = "apply") -> bool:
        target = self.daily_targets.get(kind)
        if target is None:
            return False
        return self.counts.get(kind, 0) >= target

    # ------------------------------------------------------------------ gates

    def preflight(self) -> Tuple[bool, str]:
        """Checks whether a session should start at all. Returns (ok, human-readable reason)."""
        if not self.config.enabled:
            return True, "Session rhythm disabled."

        # Both refusals name the setting that caused them and where it lives.
        # They used to explain the reasoning but not the remedy, so a run that
        # stopped looked like a failure rather than a configured choice, and
        # there was nothing to act on -- the settings were being honoured all
        # along and nothing ever said they existed.
        if self._is_rest_day:
            return False, (
                "Today is a scheduled rest day. Accounts that act every single day without "
                "exception look automated no matter how good the per-action behaviour is. "
                "To change this, set rhythm.rest_day_probability to 0.0 in Settings "
                "(or in config.yaml under the app's data folder)."
            )

        if not self.within_active_hours():
            start, end = self.config.active_hours
            now = dt.datetime.now().strftime("%H:%M")
            return False, (
                f"Outside the configured active window ({start:02d}:00-{end:02d}:00, now {now}). "
                f"Activity at unusual local hours is a session-level flag. "
                f"To run at any hour, set rhythm.active_hours_start to 0 and "
                f"rhythm.active_hours_end to 24 in Settings (or in config.yaml under the "
                f"app's data folder). Note 0-24 means always; 0-0 would block every hour."
            )

        targets = ", ".join(f"{k} {v}" for k, v in sorted(self.daily_targets.items()))
        return True, (
            f"Session budget {self.session_budget_seconds / 60:.0f} min, "
            f"today's targets: {targets}."
        )

    def note_action(self, kind: str = "apply"):
        self.counts[kind] = self.counts.get(kind, 0) + 1

    # ------------------------------------------------------------------ pacing

    async def pace(self, page: Optional[Page], kind: str = "apply") -> bool:
        """
        Runs after each completed unit of work.

        Returns False when the session should stop -- the caller is expected to break
        out of its loop rather than keep going. Stopping is the point: an agent that
        never voluntarily stops is the easiest kind to spot.
        """
        if not self.config.enabled:
            return True

        if self.quota_reached(kind):
            console.print(
                f"\n[bold cyan]Daily '{kind}' target reached "
                f"({self.counts.get(kind, 0)}/{self.daily_targets.get(kind)}). "
                f"Ending the session here rather than running to the hard limit.[/bold cyan]"
            )
            return False

        if not self.within_active_hours():
            console.print("\n[bold cyan]Left the active-hours window. Ending the session.[/bold cyan]")
            return False

        if self.budget_exhausted():
            console.print(
                f"\n[bold cyan]Session budget spent ({self.elapsed_minutes:.0f} min). "
                f"Ending the session.[/bold cyan]"
            )
            return False

        if page is None:
            return True

        # Between two units of work a person quite often looks at something else.
        try:
            await maybe_glance_away(
                page,
                probability=self.config.glance_away_probability,
                seconds_range=self.config.glance_away_seconds,
            )
        except Exception as e:
            logger.debug(f"Glance-away skipped: {e}")

        if self.total_actions >= self._next_micro_break_at:
            self._next_micro_break_at = self.total_actions + random.randint(
                *self.config.actions_between_micro_breaks
            )
            await self.micro_break(page)

        return True

    async def micro_break(self, page: Optional[Page]):
        """A short step away -- coffee, a message, a glance at something else."""
        seconds = random.uniform(*self.config.micro_break_seconds)
        console.print(
            f"[dim yellow]Stepping away for {seconds / 60:.1f} min "
            f"(window unfocused, as a real break looks).[/dim yellow]"
        )
        if page is None:
            await asyncio.sleep(seconds)
        else:
            await simulate_away(page, seconds)

    async def long_break(self, page: Optional[Page]):
        """A real interruption between two working sessions."""
        seconds = random.uniform(*self.config.long_break_minutes) * 60.0
        console.print(f"[bold yellow]Long break: {seconds / 60:.0f} min away from the browser.[/bold yellow]")
        if page is None:
            await asyncio.sleep(seconds)
        else:
            await simulate_away(page, seconds)
        # A break resets the working session, it does not reset the day's quota.
        self.started_at = time.time()
        self.session_budget_seconds = random.uniform(*self.config.session_minutes) * 60.0


async def human_gap(min_sec: float, max_sec: float):
    """
    A log-normal pause, right-skewed the way human inter-action gaps actually are.

    A uniform (or symmetric Gaussian) delay is symmetric about its mean; real gaps have
    a long right tail, because people are occasionally distracted and never occasionally
    faster than their own reaction time. Over a few hundred actions the difference is
    visible in a histogram.
    """
    if max_sec <= min_sec:
        await asyncio.sleep(max(0.0, min_sec))
        return

    span = max_sec - min_sec
    # mu/sigma chosen so the bulk of the mass sits in the lower half of the range and
    # the tail reaches past it, then clipped to keep pathological draws bounded.
    draw = random.lognormvariate(mu=-0.9, sigma=0.6)
    delay = min_sec + min(span * 2.0, span * draw)
    await asyncio.sleep(max(min_sec * 0.75, min(max_sec * 1.8, delay)))
